import os
import requests
import sys
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
from shapely.geometry import Point, Polygon, LineString
from shapely.ops import unary_union, linemerge, polygonize
import folium

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

load_dotenv()
MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    logging.error("Не задана переменная MONGODB_URI (создайте файл .env по образцу .env.example)")
    sys.exit(1)
DB_NAME = "learn"
COLLECTION_NAME = "unicorns"

# Основной сервер Overpass и зеркала на случай перегрузки (504/429)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "MoscowParksUnicornFinder/1.0 (edu; stankin)"


def ensure_ccw(polygon_coords):
    """Рекомендация RFC 7946: обход внешнего кольца против часовой стрелки"""
    edge_sum = 0.0
    for i in range(len(polygon_coords) - 1):
        x1, y1 = polygon_coords[i]
        x2, y2 = polygon_coords[i + 1]
        edge_sum += (x2 - x1) * (y2 + y1)
    if edge_sum > 0:
        return polygon_coords[::-1]
    return polygon_coords.copy()


def build_geometry_from_osm(element):
    """Преобразует объекты OSM (way, relation) в полигоны Shapely с лечением топологии"""
    polygons = []
    try:
        if element.get("type") == "way" and "geometry" in element:
            coords = [[pt["lon"], pt["lat"]] for pt in element["geometry"]]
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            coords = ensure_ccw(coords)
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)  # Автоматическое исправление микропетлей
            polygons.append(poly)
        elif element.get("type") == "relation" and "members" in element:
            # Внешний контур крупного парка в OSM часто состоит из нескольких
            # незамкнутых кусков (way). Их нельзя замыкать по отдельности:
            # сначала склеиваем куски в линии (linemerge), затем собираем
            # из них замкнутые кольца-полигоны (polygonize)
            outer_lines = []
            for member in element["members"]:
                if member.get("type") == "way" and "geometry" in member:
                    role = member.get("role", "")
                    if role in ("outer", "") and len(member["geometry"]) >= 2:
                        coords = [(pt["lon"], pt["lat"]) for pt in member["geometry"]]
                        outer_lines.append(LineString(coords))
            if outer_lines:
                for poly in polygonize(linemerge(outer_lines)):
                    poly = Polygon(ensure_ccw(list(poly.exterior.coords)))
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    polygons.append(poly)
    except (ValueError, TypeError) as e:
        logging.warning(f"Ошибка обработки геометрии объекта OSM {element.get('id')}: {e}")
    return polygons


def main():
    # Выборка только площадных объектов (исключаем точечные node для оптимизации трафика)
    # Вместо области RU-MOW (вместе с Новой Москвой) берем прямоугольник
    # старой Москвы (юг, запад, север, восток): запрос легче и не падает по таймауту
    overpass_query = """
    [out:json][timeout:120];
    (
      way["leisure"="park"](55.55,37.35,55.92,37.88);
      relation["leisure"="park"](55.55,37.35,55.92,37.88);
    );
    out geom qt;
    """
    logging.info("Запрос данных о парках Москвы из Overpass API...")
    osm_data = None
    for url in OVERPASS_URLS:
        try:
            logging.info(f"Сервер: {url}")
            response = requests.post(url, data={'data': overpass_query},
                                     headers={"User-Agent": USER_AGENT}, timeout=150)
            response.raise_for_status()
            osm_data = response.json()
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.warning(f"Ошибка сети: {e}. Пробуем следующий сервер...")
    if osm_data is None:
        logging.error("Ни один сервер Overpass API не ответил.")
        sys.exit(1)

    elements = osm_data.get("elements", [])
    if not elements:
        logging.error("Overpass API не вернул данных.")
        sys.exit(1)

    moscow_parks_shapely = []
    for elem in elements:
        moscow_parks_shapely.extend(build_geometry_from_osm(elem))

    if not moscow_parks_shapely:
        logging.error("Не удалось построить валидные полигоны.")
        sys.exit(1)

    # Пространственное слияние в RAM во избежание N+1 сетевых запросов к БД
    moscow_parks_union = unary_union(moscow_parks_shapely)
    logging.info(f"Успешно объединено парков в оперативной памяти: {len(moscow_parks_shapely)}")

    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=30000)
        db = client[DB_NAME]
        collection = db[COLLECTION_NAME]
        client.server_info()
    except ServerSelectionTimeoutError as e:
        logging.error(f"Ошибка подключения к MongoDB Atlas: {e}")
        sys.exit(1)

    logging.info("Выгрузка координат единорогов...")
    # Выборка по вложенному массиву координат GeoJSON Point
    unicorns_list = list(collection.find(
        {"loc.coordinates": {"$exists": True, "$type": "array"}},
        {"name": 1, "loc": 1}))
    logging.info(f"Получено единорогов с координатами: {len(unicorns_list)}")

    matches = []
    for uni in unicorns_list:
        coords = uni.get("loc", {}).get("coordinates", [])
        if len(coords) == 2:
            p = Point(coords[0], coords[1])
            if moscow_parks_union.contains(p):
                matches.append({"name": uni.get("name", "Безымянный"), "coords": coords})

    print("\n" + "=" * 60)
    logging.info(f"Найдено совпадений: {len(matches)}")
    for m in matches:
        print(f" - Единорог: {m['name']} Координаты: {m['coords']}")
    print("=" * 60)
    return matches, moscow_parks_shapely


def generate_interactive_map(matches, moscow_parks_shapely):
    """Генерирует интерактивную HTML-карту с инверсией координатных осей"""
    logging.info("Инициализация Folium карты (Центр Москвы)...")
    # Подложка карты Esri вместо tile.openstreetmap.org: серверы OSM блокируют (403)
    # загрузку карты из локального HTML-файла без заголовка Referer
    m = folium.Map(
        location=[55.7558, 37.6173],
        zoom_start=11,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri"
    )

    # Сначала парки, в которых найдены единороги, затем остальные
    # (ограничение до 50 для оптимизации веса HTML)
    match_points = [Point(mt["coords"][0], mt["coords"][1]) for mt in matches]
    with_unicorns = [p for p in moscow_parks_shapely if any(p.contains(pt) for pt in match_points)]
    others = [p for p in moscow_parks_shapely if p not in with_unicorns]
    parks_to_draw = with_unicorns + others[:max(0, 50 - len(with_unicorns))]

    # Отрисовка полигонов парков
    for sh_geom in parks_to_draw:
        if not sh_geom.is_valid or sh_geom.is_empty:
            continue
        # После buffer(0) может получиться MultiPolygon: рисуем каждую часть
        parts = sh_geom.geoms if sh_geom.geom_type == "MultiPolygon" else [sh_geom]
        for sh_poly in parts:
            # Явная инверсия осей: из (lon, lat) в (lat, lon) для Folium
            ext_coords = [[lat, lon] for lon, lat in sh_poly.exterior.coords]
            folium.Polygon(
                locations=ext_coords,
                color="green",
                fill=True,
                fill_color="lime",
                fill_opacity=0.20,
                popup="Природная парковая зона Москвы"
            ).add_to(m)

    # Отрисовка маркеров найденных единорогов
    for match in matches:
        lon, lat = match["coords"]
        folium.Marker(
            location=[lat, lon],  # Инвертированный формат [широта, долгота]
            popup=f"Обнаружен единорог: {match['name']}",
            icon=folium.Icon(color="purple", icon="cloud")
        ).add_to(m)

    output_filename = "moscow_parks_unicorns.html"
    m.save(output_filename)
    logging.info(f"Интерактивная ГИС-карта успешно экспортирована в файл: {output_filename}")


# Переопределенная точка входа, объединяющая Дополнительные задания 1 и 2
if __name__ == "__main__":
    # Выполняем пространственный поиск
    matches_results, parks_geometry = main()
    # Запускаем генератор интерактивной карты
    generate_interactive_map(matches_results, parks_geometry)
