from typing import Optional, Dict, List, Tuple, Any, Union
import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx

from pgmpy.models import BayesianModel
from pgmpy.models import BayesianNetwork
from typing import Optional, Dict, List, Tuple, Any, Union
import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx

from pgmpy.models import BayesianModel
from pgmpy.estimators import MaximumLikelihoodEstimator
from pgmpy.inference import VariableElimination

import plotly.express as px
import plotly.graph_objects as go
import folium
from folium.plugins import MarkerCluster

try:
    from tqdm.auto import tqdm
    _HAS_TQDM = True
except Exception:
    _HAS_TQDM = False


class POIVisitSimulator:
    """
    Гибридная модель посещений POI:
      - Байесовская сеть даёт P(visit | distance_cat, poi_type, weather, time_of_day, season, ...)
      - Гравитационная (Huff) модель распределяет спрос населения по POI с убыванием по расстоянию.

    Обязательные поля:
      - population: DataFrame с колонками ['nearest_node', 'population']
      - poi: GeoDataFrame с колонками ['nearest_node', 'type', 'geometry', ...]
      - road_network: nx.Graph с весом рёбер 'length' (в метрах по умолчанию)
    """

    def __init__(
        self,
        population: pd.DataFrame,
        poi: gpd.GeoDataFrame,
        road_network: nx.Graph,
        bayesian_edges: List[Tuple[str, str]],
        distance_bins: List[float],
        target_var: str = "visit",
        positive_state: Any = 1,
        seed: Optional[int] = None,
        edge_weight_attr: str = "length",
    ):
        # Валидации
        for col in ("nearest_node", "population"):
            if col not in population.columns:
                raise ValueError(f"population is missing required column '{col}'")
        for col in ("nearest_node", "type"):
            if col not in poi.columns:
                raise ValueError(f"poi is missing required column '{col}'")

        self.pop_df = population.copy()
        self.poi = poi.copy()
        self.G = road_network
        self.edge_weight_attr = edge_weight_attr

        self.edges = list(bayesian_edges)
        self.target_var = target_var
        self.positive_state = positive_state

        self.pop_df["nearest_node"] = self.pop_df["nearest_node"].astype(int)
        self.poi["nearest_node"] = self.poi["nearest_node"].astype(int)
        self.poi["type"] = self.poi["type"].astype(str).fillna("unknown")

        if not distance_bins:
            raise ValueError("distance_bins must be a non-empty list of bin edges")
        self.distance_bins = np.asarray(distance_bins, dtype=float)

        self.model = BayesianNetwork(self.edges)
        self.infer: Optional[VariableElimination] = None

        # Домены контекстов (по умолчанию)
        self.context_domains: Dict[str, List[Any]] = {
            "weather": ["sunny", "rainy", "windy", "snowy"],
            "time_of_day": ["morning", "afternoon", "evening"],
            "season": ["winter", "spring", "summer", "autumn"],
        }
        self.context_distributions: Dict[str, List[float]] = {}  # если заданы — используются
        # Параметры Huff/Gravity
        self.alpha = 1.0     # степень при привлекательности A_j
        self.beta = 1.2      # степень при расстоянии
        self.gamma = 1.0     # степень при P_visit_ij
        self.d0 = 50.0       # метры, сглаживание нуля

        if "attractiveness" not in self.poi.columns:
            self.poi["attractiveness"] = 1.0

        # Типовые веса типов (необязательно)
        self.poi_type_weights: Dict[str, float] = {t: 1.0 for t in self.poi["type"].unique()}

        # RNG
        self.rng = np.random.default_rng(seed)

        # Предвычисления
        self._pop_nodes = self.pop_df["nearest_node"].to_numpy()
        self._pop_sizes = self.pop_df["population"].astype(float).to_numpy()
        self._poi_nodes = self.poi["nearest_node"].to_numpy()
        self._poi_types = self.poi["type"].to_numpy()

        self._D: Optional[np.ndarray] = None        # матрица расстояний (I x J)
        self._dist_cat: Optional[np.ndarray] = None # категории расстояний (I x J), dtype=str

        # Кэш для p_lookup по контексту
        self._last_ctx_key: Optional[Tuple[Tuple[str, Any], ...]] = None
        self._last_p_lookup: Optional[Dict[Tuple[str, str], float]] = None

    # -------------------- Настройки модели --------------------

    def set_context_domain(self, name: str, values: List[Any]) -> None:
        self.context_domains[name] = list(values)

    def set_context_distribution(self, name: str, probs: List[float]) -> None:
        if name not in self.context_domains:
            raise ValueError(f"Context '{name}' not in domains. Set domain first.")
        if len(probs) != len(self.context_domains[name]):
            raise ValueError(f"len(probs) must match len(domain) for context '{name}'")
        if not np.isclose(sum(probs), 1.0):
            raise ValueError("Context probabilities must sum to 1")
        self.context_distributions[name] = list(probs)

    def set_poi_type_weights(self, weights: Dict[str, float]) -> None:
        self.poi_type_weights.update(weights)

    def set_gravity_params(self, alpha: float = 1.0, beta: float = 1.2, gamma: float = 1.0, d0: float = 50.0) -> None:
        self.alpha, self.beta, self.gamma, self.d0 = float(alpha), float(beta), float(gamma), float(d0)

    # -------------------- Обучение BN --------------------

    def fit_bayesian_network(self, data: pd.DataFrame) -> None:
        """
        data должен содержать все узлы, фигурирующие в self.model, включая self.target_var.
        """
        model_nodes = set([n for e in self.edges for n in e])
        model_nodes.add(self.target_var)
        missing = model_nodes - set(data.columns)
        if missing:
            raise ValueError(f"Training data is missing required columns: {sorted(missing)}")
        self.model.fit(data, estimator=MaximumLikelihoodEstimator)
        self.infer = VariableElimination(self.model)

    # -------------------- Вспомогательное --------------------

    @staticmethod
    def month_to_season(month: int) -> str:
        if month in (12, 1, 2):
            return "winter"
        if month in (3, 4, 5):
            return "spring"
        if month in (6, 7, 8):
            return "summer"
        if month in (9, 10, 11):
            return "autumn"
        raise ValueError("month must be in 1..12")

    @staticmethod
    def hour_to_time_of_day(hour: int) -> str:
        if 6 <= hour <= 11:
            return "morning"
        if 12 <= hour <= 17:
            return "afternoon"
        return "evening"

    def _prob_positive_state(self, q, var: str) -> float:
        try:
            names = list(q.state_names[var])
            vals = np.asarray(q.values, dtype=float).ravel()
        except Exception:
            return 0.0
        if self.positive_state in names:
            return float(vals[names.index(self.positive_state)])
        for cand in [1, "1", "yes", "true", True]:
            if cand in names:
                return float(vals[names.index(cand)])
        if len(vals) == 2:
            return float(vals[1])
        return 0.0

    @staticmethod
    def _context_key(ctx: Dict[str, Any]) -> Tuple[Tuple[str, Any], ...]:
        return tuple(sorted(ctx.items(), key=lambda kv: kv[0]))

    # -------------------- Предвычисление расстояний --------------------

    def precompute_distances(
        self,
        max_distance: Optional[float] = None,
        show_progress: Optional[bool] = None
    ) -> None:
        """
        Строит матрицу расстояний D (I x J) от узлов населения до узлов POI по графу.
        max_distance — необязательный обрез по дистанции (метры), чтобы ускорить (остальные = inf).
        """
        I = len(self._pop_nodes)
        J = len(self._poi_nodes)
        D = np.full((I, J), np.inf, dtype=float)

        it = enumerate(self._pop_nodes)
        if show_progress is None:
            show_progress = _HAS_TQDM
        if show_progress and _HAS_TQDM:
            it = tqdm(it, total=I, desc="Precomputing distances")

        for i, src in it:
            try:
                lengths = nx.single_source_dijkstra_path_length(
                    self.G, int(src), cutoff=max_distance, weight=self.edge_weight_attr
                )
            except Exception:
                lengths = {}
            for j, tgt in enumerate(self._poi_nodes):
                val = lengths.get(int(tgt))
                if val is not None:
                    D[i, j] = float(val)

        self._D = D

        # Предварительно дискретизируем в категории для BN
        dist_cat = np.full((I, J), "bin_overflow", dtype=object)
        finite_mask = np.isfinite(D)
        if np.any(finite_mask):
            idx = np.digitize(D[finite_mask], self.distance_bins, right=False)
            cats = np.array([f"bin_{k}" for k in idx], dtype=object)
            dist_cat[finite_mask] = cats
        self._dist_cat = dist_cat

    # -------------------- Вспомогательные для симуляции --------------------

    def _sample_context(self, ts: pd.Timestamp, fixed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fixed = fixed or {}
        ctx = {}
        # season от времени
        ctx["season"] = fixed.get("season", self.month_to_season(ts.month))
        # time_of_day от часа
        ctx["time_of_day"] = fixed.get("time_of_day", self.hour_to_time_of_day(ts.hour))
        # weather — из распределения, если задано
        if "weather" in fixed:
            ctx["weather"] = fixed["weather"]
        else:
            domain = self.context_domains.get("weather", ["sunny", "rainy"])
            probs = self.context_distributions.get("weather")
            ctx["weather"] = self.rng.choice(domain, p=probs if probs is not None else None)
        return ctx

    def _p_visit_lookup(self, context: Dict[str, Any]) -> Dict[Tuple[str, str], float]:
        """
        Возвращает словарь {(distance_cat, poi_type) -> P(visit=positive)} для текущего контекста.
        Кэширует результат, если контекст не меняется.
        """
        if self.infer is None:
            raise RuntimeError("Bayesian network is not fitted.")
        assert self._dist_cat is not None, "Call precompute_distances() first"

        ctx_key = self._context_key(context)
        if self._last_ctx_key == ctx_key and self._last_p_lookup is not None:
            return self._last_p_lookup

        unique_dist = np.unique(self._dist_cat.ravel())
        unique_types = np.unique(self._poi_types)

        model_vars = set(self.model.nodes())
        lookup: Dict[Tuple[str, str], float] = {}
        for dcat in unique_dist:
            for t in unique_types:
                evidence = {
                    "distance_cat": str(dcat),
                    "poi_type": str(t),
                    **context
                }
                # Используем только те ключи, которые есть в модели
                ev = {k: v for k, v in evidence.items() if k in model_vars}
                try:
                    q = self.infer.query(variables=[self.target_var], evidence=ev)
                    p = self._prob_positive_state(q, self.target_var)
                except Exception:
                    p = 0.0
                lookup[(str(dcat), str(t))] = float(max(0.0, min(1.0, p)))

        # Обновляем кэш
        self._last_ctx_key = ctx_key
        self._last_p_lookup = lookup
        return lookup

    @staticmethod
    def _safe_divide(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """
        Безопасное деление матриц с броадкастом denominator (b) и маской where.
        a: (I, J)
        b: (I, 1) или (I,) — будет растянут до (I, J)
        """
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)
        if b.ndim == 1:
            b2 = b[:, None]
        else:
            b2 = b
        out = np.zeros_like(a, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            mask = np.isfinite(b2) & (b2 > 0)
            mask_full = np.broadcast_to(mask, a.shape)
            np.divide(a, b2, out=out, where=mask_full)
        out[~np.isfinite(out)] = 0.0
        return out

    # -------------------- Основная симуляция: временной ряд --------------------

    def simulate_time_series(
        self,
        start: Union[str, pd.Timestamp],
        end: Union[str, pd.Timestamp],
        freq: str = "H",
        fixed_context: Optional[Dict[str, Any]] = None,
        base_rate_per_person_per_hour: Union[float, Any] = 0.02,
        tod_multipliers: Optional[Dict[str, float]] = None,
        weather_multipliers: Optional[Dict[str, float]] = None,
        season_multipliers: Optional[Dict[str, float]] = None,
        cutoff_distance: Optional[float] = None,
        show_progress: Optional[bool] = None,
    ) -> pd.DataFrame:
        """
        Возвращает длинный DataFrame с ожидаемыми (не накопленными) посещениями по POI на каждом временном шаге:
        колонки: ['timestamp','poi_idx','poi_node','poi_type','weather','time_of_day','season','expected_visitors'].

        base_rate_per_person_per_hour может быть числом или функцией f(ts, ctx) -> float.
        """
        if self._D is None or self._dist_cat is None:
            raise RuntimeError("Call precompute_distances() before simulate_time_series().")
        if self.infer is None:
            raise RuntimeError("Bayesian network is not fitted.")

        I, J = self._D.shape
        ts_index = pd.date_range(start=start, end=end, freq=freq, inclusive="left")
        if len(ts_index) == 0:
            raise ValueError("Empty time range")

        # Маска по отсечке расстояний
        if cutoff_distance is not None:
            valid_mask = self._D <= cutoff_distance
        else:
            valid_mask = np.isfinite(self._D)

        poi_attr = self.poi["attractiveness"].astype(float).to_numpy()
        type_weights = np.array([self.poi_type_weights.get(t, 1.0) for t in self._poi_types], dtype=float)
        A = (poi_attr * type_weights) ** self.alpha  # (J,)

        rows = []

        it = ts_index
        if show_progress is None:
            show_progress = _HAS_TQDM
        if show_progress and _HAS_TQDM:
            it = tqdm(ts_index, desc="Simulating time series")

        # Мультипликаторы
        tod_multipliers = tod_multipliers or {"morning": 1.0, "afternoon": 1.0, "evening": 1.0}
        weather_multipliers = weather_multipliers or {"sunny": 1.0, "rainy": 0.9, "windy": 0.95, "snowy": 0.8}
        season_multipliers = season_multipliers or {"winter": 0.9, "spring": 1.0, "summer": 1.1, "autumn": 1.0}

        for ts in it:
            ctx = self._sample_context(ts, fixed=fixed_context)
            p_lookup = self._p_visit_lookup(ctx)

            # Матрица P_visit (I x J)
            P_visit = np.zeros((I, J), dtype=float)
            for j in range(J):
                P_visit[:, j] = np.array(
                    [p_lookup[(str(dcat), str(self._poi_types[j]))] for dcat in self._dist_cat[:, j]],
                    dtype=float
                )

            # Huff веса W_ij
            D_eff = (self._D + self.d0) ** self.beta
            with np.errstate(divide="ignore", invalid="ignore"):
                W = np.zeros((I, J), dtype=float)
                W[valid_mask] = ((A[np.newaxis, :] * (P_visit ** self.gamma))[valid_mask]) / D_eff[valid_mask]

            # Нормируем по i (для каждого узла населения) -> матрица долей P_ij
            denom = W.sum(axis=1, keepdims=True)  # (I,1)
            P_ij = self._safe_divide(W, denom)

            # Базовый спрос за шаг
            if callable(base_rate_per_person_per_hour):
                rate = float(base_rate_per_person_per_hour(ts, ctx))
            else:
                rate = float(base_rate_per_person_per_hour)
            rate *= tod_multipliers.get(ctx["time_of_day"], 1.0)
            rate *= weather_multipliers.get(ctx["weather"], 1.0)
            rate *= season_multipliers.get(ctx["season"], 1.0)

            demand_i = self._pop_sizes * rate  # (I,)
            visitors_j = (demand_i[:, None] * P_ij).sum(axis=0)  # (J,)

            for j in range(J):
                rows.append({
                    "timestamp": ts,
                    "poi_idx": j,
                    "poi_node": int(self._poi_nodes[j]),
                    "poi_type": str(self._poi_types[j]),
                    "weather": ctx["weather"],
                    "time_of_day": ctx["time_of_day"],
                    "season": ctx["season"],
                    "expected_visitors": float(visitors_j[j]),
                })

        result = pd.DataFrame(rows)
        return result

    # -------------------- Агрегации и итоговые наборы --------------------

    @staticmethod
    def aggregate_by_timescale(visits_long: pd.DataFrame, timescale: str = "H") -> pd.DataFrame:
        """
        timescale: 'H' (час), 'D' (день), 'M' (месяц), '6MS' (полугодие, начало), 'Y' (год)
        Возвращает агрегацию по POI и временным интервалам.
        """
        df = visits_long.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        out = df.groupby(["poi_idx", pd.Grouper(freq=timescale)])["expected_visitors"].sum().reset_index()
        out = out.rename(columns={"timestamp": "period"})
        return out

    @staticmethod
    def aggregate_by_context(visits_long: pd.DataFrame, context: str) -> pd.DataFrame:
        """
        context: 'season' | 'time_of_day' | 'weather'
        """
        if context not in {"season", "time_of_day", "weather"}:
            raise ValueError("context must be one of: season, time_of_day, weather")
        return (visits_long
                .groupby(["poi_type", context], as_index=False)["expected_visitors"].sum())

    @staticmethod
    def mean_daily_monthly(visits_long: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Возвращает:
          - средние дневные по POI
          - средние месячные по POI
        """
        df = visits_long.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        daily = (df.groupby("poi_idx")["expected_visitors"]
                 .resample("D").sum().groupby("poi_idx").mean()
                 .reset_index().rename(columns={"expected_visitors": "mean_daily_visitors"}))
        monthly = (df.groupby("poi_idx")["expected_visitors"]
                   .resample("M").sum().groupby("poi_idx").mean()
                   .reset_index().rename(columns={"expected_visitors": "mean_monthly_visitors"}))
        return daily, monthly

    @staticmethod
    def build_final_dataset(
        visits_long: pd.DataFrame,
        poi_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """
        Формирует итоговый набор: средние дневные и месячные, тип, координаты и т.п.
        """
        daily, monthly = POIVisitSimulator.mean_daily_monthly(visits_long)
        poi_meta = poi_gdf.reset_index(drop=True)
        poi_meta = poi_meta.reset_index().rename(columns={"index": "poi_idx"})
        out = poi_meta.merge(daily, on="poi_idx", how="left").merge(monthly, on="poi_idx", how="left")
        for col in ("mean_daily_visitors", "mean_monthly_visitors"):
            if col not in out.columns:
                out[col] = 0.0
        return gpd.GeoDataFrame(out, geometry="geometry", crs=poi_gdf.crs)

    # -------------------- Визуализации Plotly --------------------

    @staticmethod
    def plot_context_distributions(visits_long: pd.DataFrame) -> Dict[str, go.Figure]:
        figs = {}
        for ctx in ["season", "time_of_day", "weather"]:
            agg = (visits_long
                   .groupby([ctx, "poi_type"], as_index=False)["expected_visitors"].sum())
            figs[ctx] = px.bar(
                agg, x=ctx, y="expected_visitors", color="poi_type",
                title=f"Распределение ожидаемых посещений по {ctx}"
            )
        return figs

    @staticmethod
    def plot_type_timescale(visits_long: pd.DataFrame, timescale: str = "D") -> go.Figure:
        df = visits_long.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        ts = (df.groupby(["poi_type"])
              .resample(timescale)["expected_visitors"].sum()
              .reset_index())
        fig = px.line(
            ts, x="timestamp", y="expected_visitors", color="poi_type",
            title=f"Ожидаемые посещения по типам POI (интервал {timescale})"
        )
        return fig

    # -------------------- Веб‑карта Folium --------------------

    @staticmethod
    def make_folium_map(
        poi_stats: gpd.GeoDataFrame,
        value_col: str = "mean_daily_visitors",
        popup_cols: Optional[List[str]] = None
    ) -> folium.Map:
        """
        poi_stats: GeoDataFrame c колонками ['geometry', value_col, 'type', ...]
        """
        popup_cols = popup_cols or ["type", value_col]

        gdf = poi_stats.copy()
        # Проецируем в WGS84 для корректных координат
        try:
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
        except Exception:
            pass

        # Центр карты
        centroid = gdf.geometry.unary_union.centroid
        center = (centroid.y, centroid.x)

        m = folium.Map(location=center, zoom_start=12, tiles="cartodbpositron")
        mc = MarkerCluster().add_to(m)

        v = gdf[value_col].fillna(0.0).to_numpy()
        vmax = max(1.0, np.percentile(v, 95))
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            lat, lon = geom.y, geom.x
            size = 5 + 20 * min(1.0, float(row[value_col]) / vmax)
            popup_text = "<br>".join([f"{c}: {row.get(c)}" for c in popup_cols])
            folium.CircleMarker(
                location=(lat, lon),
                radius=size,
                fill=True,
                fill_opacity=0.6,
                color="#2a9d8f",
                fill_color="#2a9d8f",
                popup=popup_text
            ).add_to(mc)
        folium.LayerControl().add_to(m)
        return m

    # -------------------- Визуализация BN: структура --------------------

    def _node_roles(self) -> Dict[str, str]:
        """
        Возвращает роль узла:
         - 'target' для целевой переменной
         - 'distance' для distance_cat
         - 'type' для poi_type
         - 'context' для контекстов (weather, time_of_day, season и т.п.)
         - 'other' для остальных
        """
        roles = {}
        for n in self.model.nodes():
            if n == self.target_var:
                roles[n] = "target"
            elif n == "distance_cat":
                roles[n] = "distance"
            elif n == "poi_type":
                roles[n] = "type"
            elif n in self.context_domains.keys():
                roles[n] = "context"
            else:
                roles[n] = "other"
        return roles

    @staticmethod
    def _hierarchical_pos_dag(G: nx.DiGraph) -> Optional[Dict[Any, Tuple[float, float]]]:
        """
        Вычисляет слойную иерархическую раскладку для DAG.
        Возвращает словарь позиций или None, если не удалось.
        """
        try:
            # Для DAG можно получить поколения (слои)
            gens = list(nx.algorithms.dag.topological_generations(G))
            pos = {}
            y_step = 1.0 / max(1, len(gens) - 1)
            for layer_idx, layer in enumerate(gens):
                x_step = 1.0 / max(1, len(layer) + 1)
                for k, node in enumerate(layer, start=1):
                    pos[node] = (k * x_step, 1.0 - layer_idx * y_step)
            return pos
        except Exception:
            return None

    def plot_bayesian_network(self) -> go.Figure:
        """
        Интерактивная визуализация структуры BN (Plotly).
        Узлы раскрашены по ролям, ориентация слоёв соответствует топологии DAG.
        """
        G = nx.DiGraph()
        G.add_nodes_from(self.model.nodes())
        G.add_edges_from(self.model.edges())

        pos = self._hierarchical_pos_dag(G)
        if pos is None:
            pos = nx.spring_layout(G, seed=42)

        roles = self._node_roles()
        role_colors = {
            "target": "#e76f51",
            "distance": "#2a9d8f",
            "type": "#264653",
            "context": "#457b9d",
            "other": "#6c757d",
        }

        # Рёбра
        edge_x, edge_y = [], []
        for u, v in G.edges():
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            edge_x += [x0, x1, None]
            edge_y += [y0, y1, None]
        edge_trace = go.Scatter(
            x=edge_x, y=edge_y,
            line=dict(width=1.5, color="#999"),
            hoverinfo="none",
            mode="lines"
        )

        # Узлы по ролям
        node_traces = []
        for role, color in role_colors.items():
            xs, ys, texts = [], [], []
            for n, (x, y) in pos.items():
                if roles.get(n, "other") != role:
                    continue
                xs.append(x); ys.append(y)
                texts.append(n)
            if xs:
                node_traces.append(go.Scatter(
                    x=xs, y=ys,
                    mode="markers+text",
                    text=texts,
                    textposition="bottom center",
                    marker=dict(size=18 if role == "target" else 14, color=color, line=dict(color="#fff", width=1)),
                    name=role
                ))

        fig = go.Figure(data=[edge_trace] + node_traces)
        fig.update_layout(
            title="Структура байесовской сети",
            showlegend=True,
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            plot_bgcolor="white",
            margin=dict(l=20, r=20, t=50, b=20),
            height=500
        )
        return fig

    # -------------------- Визуализация BN: тепловая карта P(visit|distance,type) --------------------

    def _distance_labels(self, categories: List[str]) -> Dict[str, str]:
        """
        Преобразует 'bin_k' в человекочитаемые интервалы, согласно self.distance_bins.
        """
        bins = self.distance_bins
        labels: Dict[str, str] = {}
        for c in categories:
            if c == "bin_overflow":
                labels[c] = "∞"
                continue
            if not c.startswith("bin_"):
                labels[c] = str(c)
                continue
            try:
                k = int(c.split("_")[1])
            except Exception:
                labels[c] = c
                continue
            if k <= 0:
                labels[c] = f"[0, {bins[0]:.0f})"
            elif k < len(bins):
                labels[c] = f"[{bins[k-1]:.0f}, {bins[k]:.0f})"
            else:
                labels[c] = f"[{bins[-1]:.0f}, +∞)"
        return labels

    def plot_visit_heatmap(
        self,
        context: Optional[Dict[str, Any]] = None,
        distance_order: Optional[List[str]] = None,
        type_order: Optional[List[str]] = None
    ) -> go.Figure:
        """
        Тепловая карта P(visit=positive | distance_cat, poi_type) при фиксированном контексте.
        """
        if self._D is None or self._dist_cat is None:
            raise RuntimeError("Call precompute_distances() first.")
        if self.infer is None:
            raise RuntimeError("Bayesian network is not fitted.")

        ctx = context or {}
        p_lookup = self._p_visit_lookup(ctx)

        # Упорядочение категорий
        if distance_order is None:
            uniq_d = sorted(set(self._dist_cat.ravel()), key=lambda s: (s != "bin_overflow", int(s.split("_")[1]) if s.startswith("bin_") and s.split("_")[1].isdigit() else 10**9))
        else:
            uniq_d = distance_order
        if type_order is None:
            uniq_t = sorted(set(map(str, self._poi_types)))
        else:
            uniq_t = type_order

        Z = np.zeros((len(uniq_t), len(uniq_d)), dtype=float)
        for i_t, t in enumerate(uniq_t):
            for j_d, d in enumerate(uniq_d):
                Z[i_t, j_d] = p_lookup.get((str(d), str(t)), np.nan)

        d_labels = self._distance_labels(uniq_d)
        x = [d_labels.get(d, d) for d in uniq_d]
        y = uniq_t

        fig = go.Figure(data=go.Heatmap(
            z=Z,
            x=x,
            y=y,
            colorscale="Viridis",
            colorbar=dict(title="P(visit)")
        ))
        fig.update_layout(
            title=f"P({self.target_var}=positive | distance_cat, poi_type) при контексте: {ctx}",
            xaxis_title="Категория расстояния",
            yaxis_title="Тип POI",
            height=500
        )
        return fig

    # -------------------- Диагностика BN --------------------

    def summarize_bn(self) -> pd.DataFrame:
        """
        Возвращает таблицу: узел, родители, дети.
        """
        rows = []
        for n in self.model.nodes():
            rows.append({
                "node": n,
                "parents": ", ".join(self.model.get_parents(n)),
                "children": ", ".join(self.model.get_children(n)),
            })
        return pd.DataFrame(rows).sort_values("node").reset_index(drop=True)

    def validate_bn_states(self) -> Dict[str, List[str]]:
        """
        Возвращает словарь {var: [states]} по доступным состояниям после fit.
        Полезно, чтобы убедиться, что значения evidence существуют.
        """
        if self.infer is None:
            raise RuntimeError("Bayesian network is not fitted.")
        state_map: Dict[str, List[str]] = {}
        try:
            # Попытка через CPD
            for cpd in self.model.get_cpds():
                if hasattr(cpd, "state_names") and cpd.variable in cpd.state_names:
                    states = list(cpd.state_names[cpd.variable])
                    state_map[cpd.variable] = [str(s) for s in states]
        except Exception:
            pass
        # Дополнительно через infer.state_names (часто богаче)
        try:
            for var, states in getattr(self.infer, "state_names", {}).items():
                state_map[var] = [str(s) for s in states]
        except Exception:
            pass
        return state_map
