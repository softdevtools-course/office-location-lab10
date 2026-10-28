"""Build a teacher preview from OpenStreetMap walking/cycling networks."""

from __future__ import annotations

import csv
import json
from functools import reduce
from pathlib import Path

import folium
import networkx as nx
import osmnx as ox
from shapely import MultiPoint, concave_hull
from shapely.geometry import mapping, shape


ROOT = Path(__file__).parent
BBOX = (47.18, 56.105, 47.29, 56.16)  # left, bottom, right, top
SPEED = {"walk": 75, "bike": 200}  # metres per minute
LIMIT = 30

OFFICES = [
    ("L", "Ленинградская, 36", 56.137972, 47.245608, "основной"),
    ("Y", "Ярославская, 27", 56.142547, 47.254061, "основной"),
    ("M", "Московский проспект, 19к1", 56.143815, 47.216466, "альтернатива"),
    ("S", "Правая набережная Сугутки, 1", 56.131124, 47.225324, "альтернатива"),
    ("G", "Юрия Гагарина, 7", 56.1239128, 47.2539534, "альтернатива"),
]

EMPLOYEES = [
    ("e01", "Анна", "Зои Яковлевой, 44", 56.1458246, 47.2664731, "walk"),
    ("e02", "Максим", "Маршака, 6/1", 56.1289515, 47.2626389, "walk"),
    ("e03", "Дарья", "Свердлова, 51", 56.1513189, 47.2437748, "walk"),
    ("e04", "Илья", "Московский проспект, 12", 56.1466217, 47.2298557, "walk"),
    ("e05", "Елена", "Энгельса, 14", 56.1254546, 47.2550112, "walk"),
    ("e06", "Тимур", "Ярмарочная, 19к4", 56.134808, 47.2641308, "walk"),
    ("e07", "Ольга", "Гузовского, 3", 56.142523, 47.1851657, "bike"),
    ("e08", "Артём", "7-й Мясокомбинатский проезд, 19", 56.1150237, 47.2807672, "bike"),
]


def graph_for(mode: str):
    cache = Path(f"/tmp/office-{mode}.graphml")
    if cache.exists():
        return ox.load_graphml(cache)
    graph = ox.graph.graph_from_bbox(BBOX, network_type=mode)
    ox.save_graphml(graph, cache)
    return graph


def polygon_for(graph, source, cutoff):
    """Approximate a reachable-road footprint; route times remain authoritative."""
    lengths = nx.single_source_dijkstra_path_length(graph, source, cutoff=cutoff, weight="length")
    points = [graph.nodes[n] for n in lengths]
    coords = [(p["x"], p["y"]) for p in points]
    # Concave hull is a deliberately approximate display layer, not a route test.
    geom = concave_hull(MultiPoint(coords), ratio=0.22).buffer(0.00055)
    return mapping(geom.simplify(0.00012, preserve_topology=True))


def main():
    graphs = {mode: graph_for(mode) for mode in SPEED}
    matrix = []
    features = []
    home_features = []

    for emp_id, name, address, lat, lon, mode in EMPLOYEES:
        graph = graphs[mode]
        start = ox.distance.nearest_nodes(graph, lon, lat)
        distances = nx.single_source_dijkstra_path_length(graph, start, weight="length")
        row = {"employee_id": emp_id, "name": name, "address": address, "mode": mode}
        for office_id, _, office_lat, office_lon, _ in OFFICES:
            end = ox.distance.nearest_nodes(graph, office_lon, office_lat)
            row[office_id] = round(distances.get(end, float("inf")) / SPEED[mode], 1)
        matrix.append(row)
        home_features.append({"type": "Feature", "properties": {"id": emp_id, "name": name, "address": address, "mode": mode}, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
        features.append({"type": "Feature", "properties": {"employee_id": emp_id, "name": name, "mode": mode, "minutes": LIMIT, "method": "concave hull of reachable network nodes; visual approximation"}, "geometry": polygon_for(graph, start, SPEED[mode] * LIMIT)})

    assert all(row["L"] <= LIMIT and row["Y"] <= LIMIT for row in matrix)
    assert all(any(row[office] > LIMIT for row in matrix) for office in ("M", "S", "G"))
    common_zone = reduce(lambda left, right: left.intersection(right), (shape(feature["geometry"]) for feature in features))
    assert not common_zone.is_empty
    common_feature = {"type": "Feature", "properties": {"name": "Общая зона для всех 8 сотрудников", "minutes": LIMIT, "method": "intersection of approximate individual zones"}, "geometry": mapping(common_zone)}

    with (ROOT / "employees.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "address", "latitude", "longitude", "mode"])
        writer.writeheader()
        writer.writerows(dict(zip(writer.fieldnames, row)) for row in EMPLOYEES)
    with (ROOT / "teacher-matrix.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["employee_id", "name", "address", "mode"] + [o[0] for o in OFFICES])
        writer.writeheader()
        writer.writerows(matrix)
    office_features = [{"type": "Feature", "properties": {"id": oid, "address": address, "role": role}, "geometry": {"type": "Point", "coordinates": [lon, lat]}} for oid, address, lat, lon, role in OFFICES]
    for filename, data in [
        ("offices.geojson", office_features),
        ("employees.geojson", home_features),
        ("zones.geojson", features),
        ("common-zone.geojson", [common_feature]),
    ]:
        (ROOT / filename).write_text(json.dumps({"type": "FeatureCollection", "features": data}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    colors = ["#2563eb", "#0891b2", "#7c3aed", "#16a34a", "#ea580c", "#dc2626", "#a21caf", "#4f46e5"]
    map_ = folium.Map(location=[56.136, 47.246], zoom_start=13, tiles=None, control_scale=True)
    common_layer = folium.FeatureGroup(name="Общая зона · все 8 сотрудников", show=True)
    folium.GeoJson(common_feature, style_function=lambda _: {"color": "#166534", "weight": 3, "fillColor": "url(#common-zone-hatch)", "fillOpacity": 1}).add_to(common_layer)
    common_layer.add_to(map_)
    for (emp_id, name, address, lat, lon, mode), feature, color in zip(EMPLOYEES, features, colors):
        layer = folium.FeatureGroup(name=f"{name} ({emp_id}): 30 мин · {'пешком' if mode == 'walk' else 'велосипед'}", show=False)
        folium.GeoJson(feature, style_function=lambda _, c=color: {"color": c, "fillColor": c, "weight": 2, "fillOpacity": 0.12}).add_to(layer)
        folium.CircleMarker([lat, lon], radius=7, color=color, fill=True, fill_color=color, fill_opacity=1,
                            tooltip=f"{name} ({emp_id}) · {address} · {'пешком' if mode == 'walk' else 'велосипед'}").add_to(map_)
        layer.add_to(map_)
    for oid, address, lat, lon, role in OFFICES:
        valid = all(row[oid] <= LIMIT for row in matrix)
        popup = f"<b>{address}</b><br>{'Подходит всем восьми' if valid else 'Не подходит всем восьми'}<br>Точка {oid}"
        folium.Marker([lat, lon], popup=popup, tooltip=address,
                      icon=folium.Icon(color="green" if valid else "gray", icon="briefcase", prefix="fa")).add_to(map_)
    folium.LayerControl(collapsed=False).add_to(map_)
    panel = """<div style='position:fixed;top:14px;left:55px;z-index:9999;background:white;padding:12px 16px;border-radius:9px;box-shadow:0 2px 12px #0003;font:14px/1.45 sans-serif;max-width:330px'>
    <b>Пилот: расположение офиса · Чебоксары</b><br>
    8 условных сотрудников, 5 офисов, порог 30 минут.<br>
    <span style='color:#15803d'>●</span> Подходят всем: Ленинградская, 36; Ярославская, 27.<br>
    Штриховка — общая область восьми приближённых зон. Отдельные зоны можно включить справа. Время до офисов считается по кратчайшему пути в сети OSM.</div>"""
    map_.get_root().html.add_child(folium.Element(panel))
    map_.save(str(ROOT / "preview.html"))
    preview = ROOT / "preview.html"
    html = preview.read_text(encoding="utf-8")
    # Folium's root script children render before its map constructor. Insert
    # the vector basemap after Leaflet in <head> and after the map in <body>.
    head = """<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.css">
<script src="https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@maplibre/maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js"></script>
"""
    layer = f"""<script>
    {map_.get_name()}.attributionControl.setPrefix(false);
    const overlaySvg = {map_.get_name()}.getPanes().overlayPane.querySelector('svg');
    if (overlaySvg) {{
      const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
      defs.innerHTML = '<pattern id="common-zone-hatch" patternUnits="userSpaceOnUse" width="9" height="9"><path d="M0 9L9 0" stroke="#15803d" stroke-width="2" opacity="0.7"/></pattern>';
      overlaySvg.prepend(defs);
    }}
    L.maplibreGL({{
      style: 'https://tiles.openfreemap.org/styles/liberty',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, <a href="https://openfreemap.org/">OpenFreeMap</a>'
    }}).addTo({map_.get_name()});</script>
"""
    html = html.replace("</head>", head + "</head>", 1).replace("</html>", layer + "</html>", 1)
    preview.write_text(html, encoding="utf-8")
    print("Office counts:", {oid: sum(row[oid] <= LIMIT for row in matrix) for oid, *_ in OFFICES})


if __name__ == "__main__":
    main()
