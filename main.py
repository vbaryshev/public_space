import os
import io
import json
import tempfile
from datetime import datetime
from typing import Dict, Any, Optional, List
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import inspect
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from scipy.stats import ttest_rel

# ВАЖНО: эти импорты предполагают наличие ваших модулей
from osmnx_population import OsmPopulation
from simulator import POIVisitSimulator

app = FastAPI(title="POI Visits — FastAPI + MapLibre")

# Раздача статики
app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS (на случай, если будете дергать из других источников)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Глобальное состояние (упрощенно)
# ----------------------------
class AppState:
    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="kasimov_")
        self.gpkg_path: Optional[str] = None

        # Результаты pipeline
        self.G = None
        self.bld = None
        self.pop_df: Optional[pd.DataFrame] = None
        self.poi_gdf: Optional[gpd.GeoDataFrame] = None

        # Симулятор и результаты
        self.sim: Optional[POIVisitSimulator] = None
        self.visits_long: Optional[pd.DataFrame] = None
        self.final_gdf: Optional[gpd.GeoDataFrame] = None
        self.params: Dict[str, Any] = {}

        # Мета
        self.bounds: Optional[List[float]] = None  # [minx, miny, maxx, maxy]
        self.center: Optional[List[float]] = None  # [lon, lat]

STATE = AppState()


# ----------------------------
# Утилиты
# ----------------------------
def gdf_to_geojson(gdf: gpd.GeoDataFrame) -> Dict[str, Any]:
    if gdf is None or gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    # Приведение к WGS84
    if gdf.crs is None:
        # предположим, что координаты уже в WGS84, иначе задайте верно
        gdf = gdf.set_crs(epsg=4326, allow_override=True)
    else:
        gdf = gdf.to_crs(epsg=4326)
    return json.loads(gdf.to_json())

def compute_bounds_center_from_gdf(gdf: gpd.GeoDataFrame):
    bounds = list(gdf.to_crs(epsg=4326).total_bounds)  # [minx, miny, maxx, maxy]
    minx, miny, maxx, maxy = bounds
    center = [(minx + maxx) / 2, (miny + maxy) / 2]
    return bounds, center

def ensure_time_col(df: pd.DataFrame) -> str:
    for c in ["time", "timestamp", "datetime", "date"]:
        if c in df.columns:
            return c
    raise ValueError("Не найден временной столбец в visits_long (ожидались: time/timestamp/datetime).")

import pandas as pd

def aggregate_daily_by_type(visits_long: pd.DataFrame, typ: str) -> pd.Series:
    """
    Возвращает по дням суммарные визиты для указанного типа POI.
    Ожидаем, что в visits_long есть колонки:
      - тип: 'type' / 'poi_type' / (любая с 'type' или 'category' в названии);
      - визиты: 'visits' / 'n_visits' / 'visit_count' / 'visits_count' / и т.п.;
      - время: 'date' / 'datetime' / 'timestamp' / 'time' или любая datetime-колонка.
    """

    df = visits_long.copy()

    # 1) Определим колонку типа POI
    type_col = None
    for c in ["type", "poi_type"]:
        if c in df.columns:
            type_col = c
            break
    if type_col is None:
        for c in df.columns:
            lc = c.lower()
            if "type" in lc or "category" in lc:
                type_col = c
                break
    if type_col is None:
        raise ValueError("В visits_long нет колонки с типом POI ('type'/'poi_type' или аналог с 'type'/'category' в названии).")

    # 2) Определим колонку визитов
    visit_col = None
    preferred = ["visits", "n_visits", "visit_count", "visits_count", "num_visits"]
    for c in preferred:
        if c in df.columns:
            visit_col = c
            break
    if visit_col is None:
        # Любая колонка, содержащая 'visit'
        visit_like = [c for c in df.columns if "visit" in c.lower()]
        if visit_like:
            visit_col = visit_like[0]
    if visit_col is None:
        # 'count' и родственные, исключая 'account'/'discount'
        cand = [
            c for c in df.columns
            if (
                ("count" in c.lower() and "account" not in c.lower() and "discount" not in c.lower())
                or c.lower() in {"n", "num", "value", "volume", "traffic", "footfall", "visitors"}
                or c.lower().startswith("n_") or c.lower().endswith("_n")
            )
        ]
        if cand:
            visit_col = cand[0]
    if visit_col is None:
        raise ValueError(
            "В visits_long не найдена колонка с количеством визитов. "
            "Ожидались варианты вроде 'visits', 'n_visits', 'visit_count', 'visits_count' и т.п. "
            f"Список колонок: {list(df.columns)}"
        )

    # Приводим визиты к числам и проверяем
    df[visit_col] = pd.to_numeric(df[visit_col], errors="coerce")
    if df[visit_col].notna().sum() == 0:
        sample = df[visit_col].head().tolist()
        raise ValueError(
            f"Колонка '{visit_col}' не содержит числовых значений визитов (или не приводится к числу). "
            f"Примеры значений: {sample}"
        )

    # 3) Определим временную колонку
    time_col = None
    # Сначала — уже datetime-колонки
    dt_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if dt_cols:
        pref = [c for c in dt_cols if c.lower() in ("date", "datetime", "timestamp", "time")]
        time_col = pref[0] if pref else dt_cols[0]
    else:
        for c in ["date", "datetime", "timestamp", "time"]:
            if c in df.columns:
                time_col = c
                break
        if time_col is None:
            for c in df.columns:
                lc = c.lower()
                if "date" in lc or "time" in lc or lc in {"dt", "ts"}:
                    time_col = c
                    break
    if time_col is None:
        raise ValueError("В visits_long не найдена временная колонка ('date'/'datetime'/'timestamp'/'time').")

    ts = pd.to_datetime(df[time_col], errors="coerce")
    if ts.notna().sum() == 0:
        raise ValueError(f"Колонка времени '{time_col}' не может быть преобразована в datetime.")
    df["_date"] = ts.dt.floor("D")
    df = df.dropna(subset=["_date"])

    # 4) Фильтрация по типу (регистронезависимо)
    mask = df[type_col].astype(str).str.lower() == str(typ).lower()
    df_type = df.loc[mask]

    # 5) Агрегация по дням
    # min_count=1 -> если по дню все NaN, результат NaN, а не 0
    daily = df_type.groupby("_date", as_index=True)[visit_col].sum(min_count=1).sort_index()
    daily.index.name = "date"
    daily.name = visit_col

    return daily



def load_train_df() -> pd.DataFrame:
    path = "train_df.csv"
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="Отсутствует файл train_df.csv в корне проекта.")
    return pd.read_csv(path)

    # ----------------------------
    # Маршруты
    # ----------------------------


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse("static/index.html")


@app.post("/upload")
async def upload_gpkg(file: UploadFile = File(...)):
    """
    Загрузка файла kasimov.gpkg (полигональный слой).
    После загрузки — запускаем pipeline OsmPopulation и сохраняем POI (сырые, до симуляции).
    """
    filename = os.path.join(STATE.tmp_dir, "kasimov.gpkg")
    content = await file.read()
    with open(filename, "wb") as f:
        f.write(content)

    STATE.gpkg_path = filename

    # Запуск pipeline
    try:
        pipeline = OsmPopulation(STATE.gpkg_path)
        G, bld, pop_df, poi_gdf = pipeline.run()
        STATE.G = G
        STATE.bld = bld
        STATE.pop_df = pop_df
        STATE.poi_gdf = poi_gdf

        # Сырые POI — показать серыми точками на карте
        if STATE.poi_gdf is None or STATE.poi_gdf.empty:
            raise HTTPException(status_code=400, detail="pipeline.run() вернул пустой poi_gdf.")

        # Границы/центр по сырым POI
        STATE.bounds, STATE.center = compute_bounds_center_from_gdf(STATE.poi_gdf)
        return JSONResponse({
            "status": "ok",
            "message": "GPKG загружен и обработан.",
            "bounds": STATE.bounds,
            "center": STATE.center,
            "poi_count": int(len(STATE.poi_gdf))
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка в pipeline: {e}")


@app.get("/poi_raw")
def poi_raw():
    """
    Сырые POI (до симуляции), для показа серыми точками.
    """
    if STATE.poi_gdf is None:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    # Оставим только нужные свойства
    gdf = STATE.poi_gdf.copy()
    props = []
    for col in ["type", "name", "category"]:
        if col in gdf.columns:
            props.append(col)
    gdf = gdf[props + ["geometry"]] if props else gdf[["geometry"]]
    return JSONResponse(gdf_to_geojson(gdf))


@app.post("/run-simulation")
async def run_simulation(request: Request):
    """
    Запуск симуляции с параметрами из UI.
    Тело запроса — JSON, где задаются параметры из примера.
    """
    if STATE.poi_gdf is None or STATE.pop_df is None or STATE.G is None:
        raise HTTPException(status_code=400, detail="Сначала загрузите GPKG через /upload.")

    body = await request.json()

    # Значения по умолчанию из постановки задачи
    dist_bins = body.get("dist_bins", [0, 500, 1500, 3000, 5000])
    edges = body.get("edges", [
        ("distance_cat", "visit"),
        ("weather", "visit"),
        ("time_of_day", "visit"),
        ("poi_type", "visit"),
        ("season", "visit"),
    ])
    gravity = body.get("gravity", {"alpha": 1.0, "beta": 1.2, "gamma": 1.0, "d0": 50.0})
    poi_type_weights = body.get("poi_type_weights", {"mall": 1.5, "park": 0.8, "cafe": 1.1})
    precompute_max_distance = float(body.get("precompute_max_distance", 7000))
    sim_cfg = body.get("sim_cfg", {
        "start": "2025-01-01",
        "end": "2025-12-31",
        "freq": "H",
        "base_rate_per_person_per_hour": 0.02,
        "tod_multipliers": {"morning": 0.9, "afternoon": 1.1, "evening": 1.0},
        "weather_multipliers": {"sunny": 1.2, "rainy": 0.3, "windy": 0.8, "snowy": 0.8},
        "season_multipliers": {"spring": 1.0, "summer": 1.5, "autumn": 0.7, "winter": 0.5},
        "cutoff_distance": 7000,
    })

    try:
        train_df = load_train_df()
        sim = POIVisitSimulator(
            population=STATE.pop_df,
            poi=STATE.poi_gdf,
            road_network=STATE.G,
            bayesian_edges=edges,
            distance_bins=dist_bins,
            seed=42,
        )
        sim.fit_bayesian_network(train_df)
        sim.set_gravity_params(
            alpha=float(gravity["alpha"]),
            beta=float(gravity["beta"]),
            gamma=float(gravity["gamma"]),
            d0=float(gravity["d0"]),
        )
        sim.set_poi_type_weights(poi_type_weights)
        sim.precompute_distances(max_distance=precompute_max_distance)

        visits_long = sim.simulate_time_series(
            start=sim_cfg["start"],
            end=sim_cfg["end"],
            freq=sim_cfg["freq"],
            base_rate_per_person_per_hour=float(sim_cfg["base_rate_per_person_per_hour"]),
            tod_multipliers=sim_cfg["tod_multipliers"],
            weather_multipliers=sim_cfg["weather_multipliers"],
            season_multipliers=sim_cfg["season_multipliers"],
            cutoff_distance=float(sim_cfg["cutoff_distance"]),
        )

        final_gdf = sim.build_final_dataset(visits_long, STATE.poi_gdf)
        # Приводим финальный набор к привычным полям
        if "mean_daily_visitors" not in final_gdf.columns and "visits" in final_gdf.columns:
            # На всякий случай
            final_gdf["mean_daily_visitors"] = final_gdf["visits"]

        final_gdf["visits"] = final_gdf.get("mean_daily_visitors", final_gdf.get("visits", 0))

        # Сохраняем состояние
        STATE.sim = sim
        STATE.visits_long = visits_long
        STATE.final_gdf = final_gdf
        STATE.params = {
            "dist_bins": dist_bins,
            "edges": edges,
            "gravity": gravity,
            "poi_type_weights": poi_type_weights,
            "precompute_max_distance": precompute_max_distance,
            "sim_cfg": sim_cfg,
        }
        STATE.bounds, STATE.center = compute_bounds_center_from_gdf(final_gdf)

        return JSONResponse({
            "status": "ok",
            "message": "Симуляция выполнена.",
            "bounds": STATE.bounds,
            "center": STATE.center,
            "poi_count": int(len(final_gdf)),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка симуляции: {e}")


@app.get("/poi_geojson")
def poi_geojson():
    """
    Финальные POI (после симуляции), свойства: type, mean_daily_visitors, mean_monthly_visitors.
    """
    if STATE.final_gdf is None:
        return JSONResponse({"type": "FeatureCollection", "features": []})

    cols = ["type", "mean_daily_visitors", "mean_monthly_visitors"]
    gdf = STATE.final_gdf.copy()
    existing = [c for c in cols if c in gdf.columns]
    if existing:
        gdf = gdf[existing + ["geometry"]]
    else:
        gdf = gdf[["geometry"]]
    return JSONResponse(gdf_to_geojson(gdf))


@app.get("/metadata")
def metadata():
    """
    Возвращает bounds/center текущих данных (raw если нет симуляции, иначе final).
    """
    if STATE.bounds and STATE.center:
        return JSONResponse({"bounds": STATE.bounds, "center": STATE.center})

    gdf = STATE.final_gdf or STATE.poi_gdf
    if gdf is not None and not gdf.empty:
        bounds, center = compute_bounds_center_from_gdf(gdf)
        return JSONResponse({"bounds": bounds, "center": center})
    return JSONResponse({"bounds": None, "center": None})


@app.post("/ab-test")
async def ab_test(request: Request):
    """
    A/B тест: добавляем новую точку (lat, lon, new_type), пересчитываем,
    сравниваем среднедневные суммарные визиты для этого типа (например, 'cafe').
    H0: добавление не изменяет суммарные среднедневные визиты к типу
    H1: добавление увеличивает суммарные среднедневные визиты к типу
    """


    if STATE.poi_gdf is None or STATE.pop_df is None or STATE.G is None or STATE.params == {}:
        raise HTTPException(status_code=400,
                            detail="Нужно загрузить данные и выполнить базовую симуляцию через /run-simulation.")

    body = await request.json()
    lat = float(body.get("lat"))
    lon = float(body.get("lon"))
    new_type = str(body.get("new_type", "cafe"))

    # Вычисляем базовую дневную серию по типу
    try:
        daily_base = aggregate_daily_by_type(STATE.visits_long, new_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось агрегировать базовую серию: {e}")

    # Вариант с новой точкой
    poi_gdf_new = STATE.poi_gdf.copy()
    G_AB = STATE.G.copy()
    new_row = {
        "type": new_type,
        "geometry": Point(lon, lat),
    }
    poi_gdf_new = gpd.GeoDataFrame(
        pd.concat([poi_gdf_new, gpd.GeoDataFrame([new_row], crs=poi_gdf_new.crs or "EPSG:4326")], ignore_index=True),
        crs=poi_gdf_new.crs or "EPSG:4326")

    def ensure_nearest_nodes(poi_gdf, G):
        # гарантируем наличие столбца
        if "nearest_node" not in poi_gdf.columns:
            poi_gdf["nearest_node"] = pd.Series([pd.NA] * len(poi_gdf), index=poi_gdf.index, dtype="Int64")

        mask = poi_gdf["nearest_node"].isna()
        if not mask.any():
            return poi_gdf

        # Определяем CRS графа (osmnx хранит в G.graph['crs'])
        graph_crs = None
        try:
            graph_crs = G.graph.get("crs", None)
        except Exception:
            graph_crs = None

        # Приводим геометрию POI к CRS графа (если известен), иначе оставляем как есть
        gdf_pts = poi_gdf.loc[mask].copy()
        if graph_crs:
            gdf_pts = gdf_pts.to_crs(graph_crs)
        elif gdf_pts.crs is None:
            # как минимум зададим WGS84
            gdf_pts = gdf_pts.set_crs("EPSG:4326")

        # Координаты точек POI
        Xp = gdf_pts.geometry.x.to_numpy()
        Yp = gdf_pts.geometry.y.to_numpy()

        # Координаты узлов графа
        nodes_data = list(G.nodes(data=True))
        node_ids = np.array([n for n, d in nodes_data])

        # Пытаемся взять x/y из атрибутов (osmnx так делает)
        xs = np.array([d.get("x", np.nan) for _, d in nodes_data], dtype=float)
        ys = np.array([d.get("y", np.nan) for _, d in nodes_data], dtype=float)

        # Если x/y отсутствуют, пробуем достать из геометрии узла
        if np.isnan(xs).any() or np.isnan(ys).any():
            geoms = [d.get("geometry") for _, d in nodes_data]
            if all(g is not None for g in geoms):
                xs = np.array([g.x for g in geoms], dtype=float)
                ys = np.array([g.y for g in geoms], dtype=float)

        # Находим ближайшие узлы (osmnx, если есть; иначе KDTree)
        try:
            import osmnx as ox
            nearest = ox.distance.nearest_nodes(G, X=Xp, Y=Yp)
            # nearest_nodes возвращает np.ndarray или список id
            nearest = np.array(nearest)
        except Exception:
            from scipy.spatial import cKDTree
            tree = cKDTree(np.c_[xs, ys])
            _, idx = tree.query(np.c_[Xp, Yp], k=1)
            nearest = node_ids[idx]

        poi_gdf.loc[mask, "nearest_node"] = nearest
        return poi_gdf

    poi_gdf_new= ensure_nearest_nodes(poi_gdf_new, G_AB)

    # Проверяем, что пропусков не осталось, и приводим к int
    poi_gdf_new["nearest_node"] = poi_gdf_new["nearest_node"].astype("Int64")
    if poi_gdf_new["nearest_node"].isna().any():
        missing_n = poi_gdf_new["nearest_node"].isna().sum()
        raise ValueError(
            f"Не удалось присвоить nearest_node для {missing_n} POI — проверьте CRS графа и координаты точки.")

    poi_gdf_new["nearest_node"] = poi_gdf_new["nearest_node"].astype(int)
    # Пересчет симуляции с новой точкой с теми же параметрами
    p = STATE.params
    try:
        train_df = load_train_df()
        simB = POIVisitSimulator(
            population=STATE.pop_df,
            poi=poi_gdf_new,
            road_network=STATE.G,
            bayesian_edges=p["edges"],
            distance_bins=p["dist_bins"],
            seed=42,  # фиксируем seed
        )
        simB.fit_bayesian_network(train_df)
        simB.set_gravity_params(**p["gravity"])
        simB.set_poi_type_weights(p["poi_type_weights"])
        simB.precompute_distances(max_distance=p["precompute_max_distance"])

        cfg = p["sim_cfg"]
        visits_long_B = simB.simulate_time_series(
            start=cfg["start"],
            end=cfg["end"],
            freq=cfg["freq"],
            base_rate_per_person_per_hour=float(cfg["base_rate_per_person_per_hour"]),
            tod_multipliers=cfg["tod_multipliers"],
            weather_multipliers=cfg["weather_multipliers"],
            season_multipliers=cfg["season_multipliers"],
            cutoff_distance=float(cfg["cutoff_distance"]),
        )

        daily_B = aggregate_daily_by_type(visits_long_B, new_type)

        # Совместим даты
        idx = daily_base.index.intersection(daily_B.index)
        base_vals = daily_base.loc[idx].values
        var_vals = daily_B.loc[idx].values

        # Парный t-тест
        t_stat, p_value = ttest_rel(var_vals, base_vals, alternative="greater")
        mean_base = float(base_vals.mean()) if len(base_vals) > 0 else 0.0
        mean_var = float(var_vals.mean()) if len(var_vals) > 0 else 0.0
        diff = mean_var - mean_base
        reject = bool((p_value < 0.05) and (diff > 0))

        # Вернем ещё GeoJSON варианта (для отображения новой точки, опционально)
        final_gdf_B = simB.build_final_dataset(visits_long_B, poi_gdf_new)
        geojson_B = gdf_to_geojson(final_gdf_B[["type", "mean_daily_visitors", "mean_monthly_visitors",
                                                "geometry"]] if "mean_daily_visitors" in final_gdf_B.columns else final_gdf_B)

        return JSONResponse({
            "status": "ok",
            "new_point": {"lat": lat, "lon": lon, "type": new_type},
            "mean_daily_base": mean_base,
            "mean_daily_variant": mean_var,
            "mean_diff": diff,
            "p_value_one_sided": float(p_value),
            "alpha": 0.05,
            "reject_H0": reject,
            "interpretation": "Добавление новой точки статистически значительно увеличивает суммарные среднедневные визиты данного типа." if reject else "Недостаточно доказательств увеличения (не отвергаем H0).",
            "variant_poi_geojson": geojson_B,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка A/B теста: {e}")