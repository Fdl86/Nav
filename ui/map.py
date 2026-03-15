from typing import List, Optional, Tuple

import folium

from models import Aerodrome, LegResult
from config import OPENAIP_TILE_URL
from core.geo import initial_bearing_deg, destination_point, deg_norm
from core.formatting import wind_to_deg

def openaip_tiles(api_key: str):
    return f"https://api.tiles.openaip.net/api/data/openaip/{{z}}/{{x}}/{{y}}.png?apiKey={api_key}"

def wind_to_deg(wind_from_deg: float) -> float:
    return deg_norm(wind_from_deg + 180.0)

def offset_point_perpendicular(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    lat: float,
    lon: float,
    offset_nm: float,
    side_sign: int,
) -> Tuple[float, float]:
    """
    Décale un point perpendiculairement à la branche.
    side_sign = +1 ou -1 pour alterner le côté.
    """
    mid_lat_rad = math.radians(lat)
    dlat = lat2 - lat1
    dlon = lon2 - lon1

    # Conversion locale approximative lat/lon -> NM
    x_nm = dlon * 60.0 * math.cos(mid_lat_rad)
    y_nm = dlat * 60.0

    norm = math.hypot(x_nm, y_nm)
    if norm < 1e-9:
        return lat, lon

    px = side_sign * (-y_nm / norm)
    py = side_sign * (x_nm / norm)

    dx_nm = px * offset_nm
    dy_nm = py * offset_nm

    out_lat = lat + (dy_nm / 60.0)
    out_lon = lon + (dx_nm / (60.0 * max(math.cos(mid_lat_rad), 1e-6)))
    return out_lat, out_lon

def destination_point_nm(lat_deg: float, lon_deg: float, bearing_deg: float, distance_nm: float) -> Tuple[float, float]:
    return destination_point(lat_deg, lon_deg, bearing_deg, distance_nm)


def compute_map_center(nav_points: List[NavPoint]) -> List[float]:
    all_pts = [(p.lat, p.lon) for p in nav_points]
    return [sum(x[0] for x in all_pts) / len(all_pts), sum(x[1] for x in all_pts) / len(all_pts)]


def add_basemap_layer(m: folium.Map, openaip_key: str, basemap: str):
    if basemap == "OpenAIP":
        if openaip_key:
            folium.TileLayer(
                tiles=openaip_tiles(openaip_key),
                attr="openAIP",
                name="openAIP",
                overlay=False,
                control=True,
                max_zoom=14,
            ).add_to(m)
        else:
            folium.TileLayer("OpenStreetMap", name="OSM").add_to(m)
    elif basemap == "OpenStreetMap":
        folium.TileLayer("OpenStreetMap", name="OSM", overlay=False, control=True).add_to(m)
    elif basemap == "OpenTopoMap":
        folium.TileLayer(
            tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            attr="OpenTopoMap",
            name="OpenTopoMap",
            overlay=False,
            control=True,
            max_zoom=17,
        ).add_to(m)
    else:
        folium.TileLayer("OpenStreetMap", name="OSM", overlay=False, control=True).add_to(m)


def add_nav_markers(m: folium.Map, nav_points: List[NavPoint]):
    dep = nav_points[0]
    folium.Marker(
        [dep.lat, dep.lon],
        tooltip=f"Départ {dep.name}",
        icon=folium.Icon(color="green", icon="plane", prefix="fa"),
    ).add_to(m)

    for i, pt in enumerate(nav_points[1:], start=1):
        is_arr = i == len(nav_points) - 1 and pt.icao
        color = "red" if is_arr else "blue"
        icon = "flag-checkered" if is_arr else "map-pin"
        folium.Marker(
            [pt.lat, pt.lon],
            tooltip=pt.name,
            popup=f"<b>{html.escape(pt.name)}</b>",
            icon=folium.Icon(color=color, icon=icon, prefix="fa"),
        ).add_to(m)


def add_end_type_marker(m: folium.Map, leg: LegResult):
    if leg.end_type == "verticale":
        label = "VT"
        font_size = 14
        color = "#f59e0b"
    elif leg.end_type == "tour_de_piste":
        label = "TDP"
        font_size = 12
        color = "#00a6ff"
    else:
        return

    folium.Marker(
        [leg.end_lat, leg.end_lon],
        icon=folium.DivIcon(
            icon_size=(0, 0),
            icon_anchor=(0, 0),
            html=f"""
            <div style="
                font-size:{font_size}px;
                font-weight:700;
                color:{color};
                background:transparent;
                border:none;
                padding:0;
                text-shadow:
                    -1px -1px 0 rgba(255,255,255,0.95),
                     1px -1px 0 rgba(255,255,255,0.95),
                    -1px  1px 0 rgba(255,255,255,0.95),
                     1px  1px 0 rgba(255,255,255,0.95);
            ">
                {label}
            </div>
            """,
        ),
    ).add_to(m)


def add_leg_polyline(m: folium.Map, leg: LegResult, selected: bool):
    segment_count = max(18, min(40, int(round(leg.distance_nm * 0.8))))
    seg = interpolate_line(leg.start_lat, leg.start_lon, leg.end_lat, leg.end_lon, n=segment_count)
    folium.PolyLine(
        locations=seg,
        color="#ef4444" if selected else "#0f172a",
        weight=7 if selected else 4,
        opacity=0.95 if selected else 0.70,
        tooltip=f"Branche {leg.idx}: {leg.start_name} → {leg.end_name}",
    ).add_to(m)


def add_wind_overlay(m: folium.Map, leg: LegResult, selected: bool):
    side_sign = 1 if (leg.idx % 2 == 1) else -1
    offset_nm = 1.15 if selected else 0.85

    anchor_lat, anchor_lon = offset_point_perpendicular(
        leg.start_lat,
        leg.start_lon,
        leg.end_lat,
        leg.end_lon,
        leg.mid_lat,
        leg.mid_lon,
        offset_nm=offset_nm,
        side_sign=side_sign,
    )

    arrow_bearing = wind_to_deg(leg.wind_dir_deg)
    arrow_len_nm = min(1.0, 0.45 + 0.03 * leg.wind_speed_kt)
    tip_lat, tip_lon = destination_point_nm(anchor_lat, anchor_lon, arrow_bearing, arrow_len_nm)

    arrow_color = "#1d4ed8" if selected else "#60a5fa"
    label_color = "#0f3b82" if selected else "#2563eb"

    folium.PolyLine(
        locations=[(anchor_lat, anchor_lon), (tip_lat, tip_lon)],
        color=arrow_color,
        weight=3,
        opacity=0.9,
    ).add_to(m)

    head_left_lat, head_left_lon = destination_point_nm(tip_lat, tip_lon, arrow_bearing + 150, 0.18)
    head_right_lat, head_right_lon = destination_point_nm(tip_lat, tip_lon, arrow_bearing - 150, 0.18)

    folium.PolyLine(
        locations=[(head_left_lat, head_left_lon), (tip_lat, tip_lon), (head_right_lat, head_right_lon)],
        color=arrow_color,
        weight=3,
        opacity=0.9,
    ).add_to(m)

    label_lat, label_lon = offset_point_perpendicular(
        leg.start_lat,
        leg.start_lon,
        leg.end_lat,
        leg.end_lon,
        anchor_lat,
        anchor_lon,
        offset_nm=0.45,
        side_sign=side_sign,
    )

    folium.Marker(
        [label_lat, label_lon],
        tooltip=f"Vent {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f} kt",
        icon=folium.DivIcon(
            icon_size=(0, 0),
            icon_anchor=(0, 0),
            html=f"""
            <div style="
                font-size:11px;
                font-weight:700;
                color:{label_color};
                background:transparent;
                border:none;
                padding:0;
                white-space:nowrap;
                text-shadow:
                    -1px -1px 0 rgba(255,255,255,0.95),
                     1px -1px 0 rgba(255,255,255,0.95),
                    -1px  1px 0 rgba(255,255,255,0.95),
                     1px  1px 0 rgba(255,255,255,0.95);
            ">
                {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f}
            </div>
            """,
        ),
    ).add_to(m)


def fit_map_to_bounds(m: folium.Map, nav_points: List[NavPoint]):
    all_pts = [(p.lat, p.lon) for p in nav_points]
    min_lat = min(p[0] for p in all_pts)
    max_lat = max(p[0] for p in all_pts)
    min_lon = min(p[1] for p in all_pts)
    max_lon = max(p[1] for p in all_pts)
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=(18, 18))


def build_map(nav_points: List[NavPoint], legs: List[LegResult], selected_idx: int, openaip_key: str, basemap: str):
    m = folium.Map(location=compute_map_center(nav_points), zoom_start=8, control_scale=True, tiles=None)
    add_basemap_layer(m, openaip_key, basemap)
    add_nav_markers(m, nav_points)

    for leg in legs:
        selected = leg.idx == selected_idx
        add_leg_polyline(m, leg, selected)
        add_end_type_marker(m, leg)
        add_wind_overlay(m, leg, selected)

    fit_map_to_bounds(m, nav_points)
    return m
