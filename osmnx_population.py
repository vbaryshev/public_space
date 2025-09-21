import geopandas as gpd
import osmnx as ox
import pandas as pd
import numpy as np
from pyogrio import list_layers
import geopandas as gpd

class OsmPopulation:
    def __init__(self, gpkg_path: str):
        self.gpkg_path = gpkg_path
        self.G = None
        self.poi_gdf = None
        self.bld = None
        self.pop_df = None

    @staticmethod
    def _parse_height(h):
        try:
            if pd.isna(h):
                return np.nan
            if isinstance(h, str):
                num = "".join(c for c in h if c.isdigit() or c == ".")
                return float(num) if num else np.nan
            return float(h)
        except Exception:
            return np.nan

    def run(self):
        # 1) Загружаем полигон из файла .gpkg
        gdf = gpd.read_file(self.gpkg_path)

        polygon = gdf.geometry.iloc[0]

        # 2) Загружаем граф дорожной сети
        G = ox.graph_from_polygon(polygon, network_type="walk")
        # предварительно уберём «дубли»/многократные рёбра, чтобы ускорить shortest_path
        G = G.to_undirected()

        # 3) Загружаем POI (все amenity)
        tags_poi = {
            "amenity": ["cafe", "restaurant", "clinic", "doctors"],
            "shop": ["hairdresser", "supermarket", "convenience", "bakery"],
            "office": ["hairdresser"],
        }
        poi_gdf = ox.features_from_polygon(polygon, tags_poi)  # features_from_polygon
        # берём только точечные объекты
        poi_gdf = poi_gdf[poi_gdf.geometry.type == "Point"].copy()
        poi_gdf = poi_gdf.reset_index(drop=True)
        # присваиваем nearest_node
        poi_gdf["nearest_node"] = ox.distance.nearest_nodes(
            G,
            X=poi_gdf.geometry.x.values,
            Y=poi_gdf.geometry.y.values,
        )
        # вводим колонку 'type' из amenity
        poi_gdf["type"] = poi_gdf["amenity"].astype(str)

        # 4) Загружаем жилые здания и рассчитываем население
        tags_build = {"building": "apartments"}
        bld = ox.features_from_polygon(polygon, tags_build)
        bld = bld[bld.geometry.area > 0].copy()

        # Парсинг высоты
        bld["lev"] = bld.get("building:levels", np.nan).apply(self._parse_height)
        # Заполняем пропуск медианой
        med_h = bld["lev"].median()
        bld["lev"] = bld["lev"].fillna(med_h)

        # Перепроецирование в EPSG:32637 для площади
        bld = bld.to_crs(epsg=32637)
        bld["area_m2"] = round(bld.geometry.area, 4)
        bld = bld.to_crs(epsg=4326)
        # Расчет населения
        bld["population"] = (bld["area_m2"] * (bld["lev"])) / 50.0
        bld["population"] = bld["population"].round().astype(int)
        bld["population"] = bld["population"].clip(lower=0)

        # assign nearest_node
        bld["nearest_node"] = ox.distance.nearest_nodes(
            G,
            X=bld.geometry.centroid.x.values,
            Y=bld.geometry.centroid.y.values,
        )
        poi_gdf.replace("nan", pd.NA, inplace=True)  # Заменить строки "NaN" на настоящие NaN
        poi_gdf['type'] = poi_gdf['type'].fillna(poi_gdf['shop']).fillna(poi_gdf['amenity'])
        # агрегация населения по узлам
        pop_df = bld.groupby("nearest_node", as_index=False)["population"].sum()

        # сохранить в атрибуты и вернуть
        self.G = G
        self.poi_gdf = poi_gdf
        self.bld = bld
        self.pop_df = pop_df
        return G, bld, pop_df, poi_gdf