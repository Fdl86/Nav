# app.py
import math
import gzip
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium

# =========================
# CONFIG / CONSTANTS
# =========================

APP_TITLE = "Prépa VFR Mobile"
UA = {"User-Agent": "vfr-prep-streamlit/1.0"}

AVWX_STATIONS_CACHE = "https://aviationweather.gov/data/cache/stations.cache.json.gz"
AVWX_METAR_API = "https://aviationweather.gov/api/data/metar"

OPENMETEO_DWD = "https://api.open-meteo.com/v1/dwd-icon"
OPENMETEO_MF = "https://api.open-meteo.com/v1/meteofrance"
OPENMETEO_ELEV = "https://api.open-meteo.com/v1/elevation"

# Approximate pressure-level heights in meters AMSL
DWD_LEVELS_M = {
    1000: 110, 975: 320, 950: 500, 925: 800, 900: 1000, 850: 1500,
    800: 1900, 700: 3000, 600: 4200, 500: 5600, 400: 7200, 300: 9200,
    250: 10400, 200: 11800, 150: 13500, 100: 15800, 70: 17700, 50: 19300, 30: 22000
}
MF_LEVELS_M = {
    1000: 110, 950: 500, 925: 800, 900: 1000, 850: 1500, 800: 1900, 750: 2500,
    700: 3000, 650: 3600, 600: 4200, 550: 4900, 500: 5600, 450: 6300, 400: 7200,
    350: 8100, 300: 9200, 275: 9700, 250: 10400, 225: 11000, 200: 11800, 175: 12600,
    150: 13500, 125: 14600, 100: 15800, 70: 17700, 50: 19300, 30: 22000, 20: 23000, 10: 26000
}

END_TYPE_OPTIONS = ["standard", "verticale", "tour_de_piste"]

# =========================
# DATA CLASSES
# =========================

@dataclass
class Aerodrome:
    icao: str
    name: str
    lat: float
    lon: float
    elev_ft: float
    metar_raw: Optional[str] = None
    metar_decoded: Optional[Dict] = None


@dataclass
class Waypoint:
    name: str
    lat: float
    lon: float
    altitude_ft: float
    tas_kt: float
    end_type: str = "standard"
    icao: str = ""


@dataclass
class LegResult:
    idx: int
    from_name: str
    to_name: str
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    distance_nm: float
    route_true_deg: float
    altitude_ft: float
    tas_kt: float
    wind_source: str
    wind_dir_deg: float
    wind_speed_kt: float
    drift_deg: float
    heading_true_deg: float
    gs_kt: float
    ete_min: float
    eta: datetime
    end_type: str


# =========================
# LOW-LEVEL HELPERS
# =========================

@st.cache_resource
def http_session():
    s = requests.Session()
    s.headers.update(UA)
    return s


def fetch_json(url: str, params: dict | None = None, timeout: int = 20):
    r = http_session().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_bytes(url: str, timeout: int = 30):
    r = http_session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def ft_to_m(ft: float) -> float:
    return ft * 0.3048


def m_to_ft(m: float) -> float:
    return m / 0.3048


def nm_to_m(nm: float) -> float:
    return nm * 1852.0


def m_to_nm(m: float) -> float:
    return m / 1852.0


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def parse_iso(ts: str) -> datetime:
    # Open-Meteo hourly returns local-ish ISO strings without zone, we treat them as UTC for deterministic matching
    # because we're only looking up the closest forecast hour.
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def deg_norm(d: float) -> float:
    return d % 360.0


def shortest_angle_deg(a: float, b: float) -> float:
    x = (a - b + 180) % 360 - 180
    return x


# =========================
# GEO
# =========================

def haversine_nm(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return m_to_nm(2 * R * math.asin(math.sqrt(a)))


def initial_bearing_deg(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = math.degrees(math.atan2(x, y))
    return deg_norm(brg)


def interpolate_gc(lat1, lon1, lat2, lon2, n=20) -> List[Tuple[float, float]]:
    # Simple linear interpolation in lat/lon: sufficient for short VFR legs
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t))
    return pts


# =========================
# AVIATION WEATHER / AIRPORTS
# =========================

@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def load_station_index() -> List[dict]:
    raw = fetch_bytes(AVWX_STATIONS_CACHE, timeout=40)
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        data = gz.read().decode("utf-8")
    return pd.read_json(io.StringIO(data)).to_dict(orient="records")


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return default


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def resolve_icao(icao: str) -> Optional[Aerodrome]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    stations = load_station_index()
    candidates = []
    for row in stations:
        row_icao = str(_pick(row, "icaoId", "icao", "icao_id", "ident", "id", default="")).upper()
        if row_icao == icao:
            candidates.append(row)

    if not candidates:
        return None

    row = candidates[0]
    lat = _pick(row, "lat", "latitude", "latDec")
    lon = _pick(row, "lon", "longitude", "lonDec")
    elev_m = _pick(row, "elev", "elevation", "elevation_m", default=0.0)
    if lat is None or lon is None:
        return None

    try:
        elev_ft = float(elev_m) * 3.28084
    except Exception:
        elev_ft = 0.0

    name = _pick(row, "site", "name", "stationName", "station_name", default=icao)
    return Aerodrome(
        icao=icao,
        name=str(name),
        lat=float(lat),
        lon=float(lon),
        elev_ft=float(elev_ft),
    )


@st.cache_data(ttl=60 * 5, show_spinner=False)
def fetch_metar(icao: str) -> Tuple[Optional[str], Optional[dict]]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None, None

    params = {"ids": icao, "format": "json"}
    r = http_session().get(AVWX_METAR_API, params=params, timeout=15)
    if r.status_code == 204:
        return None, None
    r.raise_for_status()
    js = r.json()
    if not js:
        return None, None

    m = js[0]
    raw = _pick(m, "rawOb", "raw_text", "raw")
    decoded = {
        "obs_time": _pick(m, "obsTime", "observation_time", "receiptTime"),
        "flight_category": _pick(m, "fltCat", "flight_category"),
        "wind_dir": _pick(m, "wdir", "wind_dir_degrees"),
        "wind_speed_kt": _pick(m, "wspd", "wind_speed_kt"),
        "visibility": _pick(m, "visib", "visibility_statute_mi"),
        "temp_c": _pick(m, "temp", "temp_c"),
        "dewpoint_c": _pick(m, "dewp", "dewpoint_c"),
        "qnh_hpa": _pick(m, "altim", "altimeter"),
        "clouds": _pick(m, "clouds", default=[]),
    }
    return raw, decoded


def metar_human(decoded: Optional[dict]) -> str:
    if not decoded:
        return "METAR indisponible"

    wind_dir = decoded.get("wind_dir")
    wind_spd = decoded.get("wind_speed_kt")
    vis = decoded.get("visibility")
    temp = decoded.get("temp_c")
    dew = decoded.get("dewpoint_c")
    qnh = decoded.get("qnh_hpa")
    flt = decoded.get("flight_category")
    clouds = decoded.get("clouds") or []

    cloud_txt = []
    for c in clouds[:3]:
        cov = _pick(c, "cover", "amount", default="?")
        base = _pick(c, "base", "base_feet_agl", default="?")
        cloud_txt.append(f"{cov} {base} ft")

    parts = []
    if wind_dir not in (None, "") and wind_spd not in (None, ""):
        parts.append(f"Vent {wind_dir:03.0f}/{float(wind_spd):.0f} kt")
    if vis not in (None, ""):
        parts.append(f"Vis {vis}")
    if cloud_txt:
        parts.append("Nuages " + ", ".join(cloud_txt))
    if temp not in (None, "") and dew not in (None, ""):
        parts.append(f"T {temp}°C / Td {dew}°C")
    if qnh not in (None, ""):
        parts.append(f"QNH {qnh}")
    if flt:
        parts.append(f"Cat {flt}")

    return " • ".join(parts) if parts else "METAR disponible"


# =========================
# WIND / WEATHER MODEL
# =========================

def uv_from_wind_from(speed_kt: float, direction_from_deg: float) -> Tuple[float, float]:
    # Meteorological "from" direction -> vector toward
    rad = math.radians(direction_from_deg)
    u = -speed_kt * math.sin(rad)
    v = -speed_kt * math.cos(rad)
    return u, v


def wind_from_uv(u: float, v: float) -> Tuple[float, float]:
    speed = math.hypot(u, v)
    if speed < 1e-6:
        return 0.0, 0.0
    direction_to = math.degrees(math.atan2(u, v)) % 360.0
    direction_from = (direction_to + 180.0) % 360.0
    return direction_from, speed


def pick_levels(target_alt_m: float, level_map: Dict[int, float]) -> Tuple[int, int]:
    levels = sorted(level_map.items(), key=lambda x: x[1])
    if target_alt_m <= levels[0][1]:
        return levels[0][0], levels[0][0]
    if target_alt_m >= levels[-1][1]:
        return levels[-1][0], levels[-1][0]
    for (p1, h1), (p2, h2) in zip(levels, levels[1:]):
        if h1 <= target_alt_m <= h2:
            return p1, p2
    return levels[-1][0], levels[-1][0]


def nearest_hour(dt: datetime) -> datetime:
    dt = to_utc(dt)
    return dt.replace(minute=0, second=0, microsecond=0)


def _openmeteo_multi(url: str, lats: List[float], lons: List[float], params: dict) -> List[dict]:
    full_params = {
        **params,
        "latitude": ",".join(f"{x:.6f}" for x in lats),
        "longitude": ",".join(f"{x:.6f}" for x in lons),
    }
    js = fetch_json(url, full_params, timeout=25)
    if isinstance(js, list):
        return js
    return [js]


@st.cache_data(ttl=60 * 15, show_spinner=False)
def fetch_wind_batch(
    source: str,
    lats: Tuple[float, ...],
    lons: Tuple[float, ...],
    valid_hour_iso: str,
    target_alt_ft: float,
) -> Optional[List[dict]]:
    dt_hour = datetime.fromisoformat(valid_hour_iso)
    target_alt_m = ft_to_m(target_alt_ft)

    if source == "ICON-D2":
        url = OPENMETEO_DWD
        level_map = DWD_LEVELS_M
    else:
        url = OPENMETEO_MF
        level_map = MF_LEVELS_M

    p_low, p_high = pick_levels(target_alt_m, level_map)
    variables = [
        f"wind_speed_{p_low}hPa",
        f"wind_direction_{p_low}hPa",
        f"geopotential_height_{p_low}hPa",
    ]
    if p_high != p_low:
        variables += [
            f"wind_speed_{p_high}hPa",
            f"wind_direction_{p_high}hPa",
            f"geopotential_height_{p_high}hPa",
        ]

    params = {
        "hourly": ",".join(variables),
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "start_hour": dt_hour.strftime("%Y-%m-%dT%H:%M"),
        "end_hour": (dt_hour + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "cell_selection": "nearest",
    }

    try:
        out = _openmeteo_multi(url, list(lats), list(lons), params)
    except Exception:
        return None

    results = []
    for item in out:
        hourly = item.get("hourly", {})
        h = {}

        def first_val(name):
            arr = hourly.get(name, [])
            return arr[0] if arr else None

        spd_low = first_val(f"wind_speed_{p_low}hPa")
        dir_low = first_val(f"wind_direction_{p_low}hPa")
        z_low = first_val(f"geopotential_height_{p_low}hPa")

        if spd_low is None or dir_low is None or z_low is None:
            return None

        if p_high == p_low:
            wind_dir = float(dir_low)
            wind_speed = float(spd_low)
        else:
            spd_high = first_val(f"wind_speed_{p_high}hPa")
            dir_high = first_val(f"wind_direction_{p_high}hPa")
            z_high = first_val(f"geopotential_height_{p_high}hPa")
            if spd_high is None or dir_high is None or z_high is None:
                return None

            z1, z2 = float(z_low), float(z_high)
            if abs(z2 - z1) < 1e-6:
                t = 0.0
            else:
                t = clamp((target_alt_m - z1) / (z2 - z1), 0.0, 1.0)

            u1, v1 = uv_from_wind_from(float(spd_low), float(dir_low))
            u2, v2 = uv_from_wind_from(float(spd_high), float(dir_high))
            u = u1 + (u2 - u1) * t
            v = v1 + (v2 - v1) * t
            wind_dir, wind_speed = wind_from_uv(u, v)

        h["wind_dir_deg"] = float(wind_dir)
        h["wind_speed_kt"] = float(wind_speed)
        h["grid_elev_m"] = float(item.get("elevation", 0.0))
        results.append(h)

    return results


def leg_mean_wind(
    leg_points: List[Tuple[float, float]],
    valid_dt: datetime,
    altitude_ft: float
) -> Tuple[str, float, float]:
    lats = tuple(p[0] for p in leg_points)
    lons = tuple(p[1] for p in leg_points)
    valid_hour_iso = nearest_hour(valid_dt).replace(tzinfo=None).isoformat(timespec="minutes")

    primary = fetch_wind_batch("ICON-D2", lats, lons, valid_hour_iso, altitude_ft)
    source = "ICON-D2"
    batch = primary

    if not batch:
        fallback = fetch_wind_batch("AROME", lats, lons, valid_hour_iso, altitude_ft)
        source = "AROME"
        batch = fallback

    if not batch:
        return "MANUEL", 0.0, 0.0

    u_sum = 0.0
    v_sum = 0.0
    for x in batch:
        u, v = uv_from_wind_from(x["wind_speed_kt"], x["wind_dir_deg"])
        u_sum += u
        v_sum += v

    u_avg = u_sum / len(batch)
    v_avg = v_sum / len(batch)
    wind_dir, wind_speed = wind_from_uv(u_avg, v_avg)
    return source, wind_dir, wind_speed


# =========================
# NAVIGATION CALCS
# =========================

def wind_correction(course_deg: float, tas_kt: float, wind_from_deg: float, wind_speed_kt: float):
    delta = math.radians(shortest_angle_deg(wind_from_deg, course_deg))
    ratio = 0.0 if tas_kt <= 0 else clamp((wind_speed_kt / tas_kt) * math.sin(delta), -0.9999, 0.9999)
    wca_rad = math.asin(ratio)
    gs = tas_kt * math.cos(wca_rad) - wind_speed_kt * math.cos(delta)
    gs = max(gs, 20.0)
    hdg = deg_norm(course_deg + math.degrees(wca_rad))
    drift = math.degrees(wca_rad)
    return drift, hdg, gs


def compute_legs(
    departure: Aerodrome,
    waypoints: List[Waypoint],
    offblock_utc: datetime
) -> List[LegResult]:
    results = []
    prev_name, prev_lat, prev_lon = departure.icao, departure.lat, departure.lon
    elapsed_min = 0.0

    for i, wp in enumerate(waypoints, start=1):
        dist_nm = haversine_nm(prev_lat, prev_lon, wp.lat, wp.lon)
        route_true = initial_bearing_deg(prev_lat, prev_lon, wp.lat, wp.lon)

        # Use midpoint ETA estimate for weather sampling (first pass with TAS only)
        est_mid = offblock_utc + timedelta(minutes=elapsed_min + (dist_nm / max(wp.tas_kt, 30.0) * 30.0))
        sample_pts = interpolate_gc(prev_lat, prev_lon, wp.lat, wp.lon, n=4)
        source, wind_dir, wind_speed = leg_mean_wind(sample_pts, est_mid, wp.altitude_ft)
        drift, hdg, gs = wind_correction(route_true, wp.tas_kt, wind_dir, wind_speed)
        ete_min = dist_nm / gs * 60.0
        eta = offblock_utc + timedelta(minutes=elapsed_min + ete_min)

        results.append(
            LegResult(
                idx=i,
                from_name=prev_name,
                to_name=wp.name,
                from_lat=prev_lat,
                from_lon=prev_lon,
                to_lat=wp.lat,
                to_lon=wp.lon,
                distance_nm=dist_nm,
                route_true_deg=route_true,
                altitude_ft=wp.altitude_ft,
                tas_kt=wp.tas_kt,
                wind_source=source,
                wind_dir_deg=wind_dir,
                wind_speed_kt=wind_speed,
                drift_deg=drift,
                heading_true_deg=hdg,
                gs_kt=gs,
                ete_min=ete_min,
                eta=eta,
                end_type=wp.end_type,
            )
        )

        elapsed_min += ete_min
        prev_name, prev_lat, prev_lon = wp.name, wp.lat, wp.lon

    return results


# =========================
# TERRAIN PROFILE
# =========================

@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def fetch_elevations(lats: Tuple[float, ...], lons: Tuple[float, ...]) -> List[float]:
    params = {
        "latitude": ",".join(f"{x:.6f}" for x in lats),
        "longitude": ",".join(f"{x:.6f}" for x in lons),
    }
    js = fetch_json(OPENMETEO_ELEV, params=params, timeout=25)
    return js.get("elevation", [])


def build_route_points(departure: Aerodrome, waypoints: List[Waypoint], n_per_leg=24):
    pts = [(departure.lat, departure.lon)]
    prev_lat, prev_lon = departure.lat, departure.lon
    for wp in waypoints:
        seg = interpolate_gc(prev_lat, prev_lon, wp.lat, wp.lon, n=n_per_leg)
        pts.extend(seg[1:])
        prev_lat, prev_lon = wp.lat, wp.lon
    return pts


def route_distances_nm(route_pts: List[Tuple[float, float]]) -> List[float]:
    d = [0.0]
    for i in range(1, len(route_pts)):
        step = haversine_nm(route_pts[i-1][0], route_pts[i-1][1], route_pts[i][0], route_pts[i][1])
        d.append(d[-1] + step)
    return d


def arrival_target_alt_ft(arr_field_elev_ft: float, last_end_type: str, verticale_height_ft: float, tdp_height_ft: float):
    if last_end_type == "verticale":
        return arr_field_elev_ft + verticale_height_ft
    if last_end_type == "tour_de_piste":
        return arr_field_elev_ft + tdp_height_ft
    return arr_field_elev_ft + 300.0


def build_vertical_profile(
    departure: Aerodrome,
    arrival_elev_ft: float,
    last_end_type: str,
    route_d_nm: List[float],
    cruise_alt_ft: float,
    climb_rate_fpm: float,
    climb_speed_kt: float,
    descent_rate_fpm: float,
    descent_speed_kt: float,
    verticale_height_ft: float,
    tdp_height_ft: float,
):
    dep_alt = departure.elev_ft
    arr_target = arrival_target_alt_ft(arrival_elev_ft, last_end_type, verticale_height_ft, tdp_height_ft)

    climb_ft = max(cruise_alt_ft - dep_alt, 0.0)
    desc_ft = max(cruise_alt_ft - arr_target, 0.0)

    climb_time_min = 0.0 if climb_rate_fpm <= 0 else climb_ft / climb_rate_fpm
    desc_time_min = 0.0 if descent_rate_fpm <= 0 else desc_ft / descent_rate_fpm

    toc_nm = climb_speed_kt * (climb_time_min / 60.0)
    tod_nm = max(route_d_nm[-1] - descent_speed_kt * (desc_time_min / 60.0), 0.0)

    if toc_nm > tod_nm:
        # No real cruise segment
        midpoint = route_d_nm[-1] / 2.0
        toc_nm = min(toc_nm, midpoint)
        tod_nm = max(tod_nm, midpoint)

    alt_profile = []
    for d in route_d_nm:
        if d <= toc_nm and toc_nm > 0:
            alt = dep_alt + (cruise_alt_ft - dep_alt) * (d / toc_nm)
        elif d >= tod_nm and route_d_nm[-1] > tod_nm:
            frac = (d - tod_nm) / max(route_d_nm[-1] - tod_nm, 1e-6)
            alt = cruise_alt_ft + (arr_target - cruise_alt_ft) * frac
        else:
            alt = cruise_alt_ft
        alt_profile.append(alt)

    return {
        "toc_nm": toc_nm,
        "tod_nm": tod_nm,
        "arr_target_ft": arr_target,
        "alt_profile_ft": alt_profile,
    }


# =========================
# MAP
# =========================

def openaip_tile_url(api_key: str) -> str:
    # Common TMS pattern used by clients
    return f"https://api.tiles.openaip.net/api/data/openaip/{'{z}'}/{'{x}'}/{'{y}'}.png?apiKey={api_key}"


def build_map(
    departure: Aerodrome,
    waypoints: List[Waypoint],
    legs: List[LegResult],
    selected_leg_idx: int,
    openaip_key: str,
):
    points = [(departure.lat, departure.lon)]
    points.extend((wp.lat, wp.lon) for wp in waypoints)

    center_lat = sum(p[0] for p in points) / len(points)
    center_lon = sum(p[1] for p in points) / len(points)

    m = folium.Map(location=[center_lat, center_lon], zoom_start=8, control_scale=True, tiles=None)

    if openaip_key:
        folium.TileLayer(
            tiles=openaip_tile_url(openaip_key),
            attr='openAIP',
            name='openAIP',
            overlay=False,
            control=True,
            max_zoom=14,
        ).add_to(m)
    else:
        folium.TileLayer("OpenStreetMap", name="OSM").add_to(m)

    # Departure marker
    dep_popup = f"<b>{departure.icao}</b><br>{departure.name}"
    if departure.metar_raw:
        dep_popup += f"<br><br><code>{departure.metar_raw}</code>"
    folium.Marker(
        location=[departure.lat, departure.lon],
        popup=dep_popup,
        tooltip=f"Départ {departure.icao}",
        icon=folium.Icon(color="green", icon="plane", prefix="fa"),
    ).add_to(m)

    # Waypoint / arrival markers
    for i, wp in enumerate(waypoints, start=1):
        color = "red" if i == len(waypoints) else "blue"
        icon = "flag-checkered" if i == len(waypoints) else "circle"
        label = f"{i}. {wp.name}"
        extra = ""
        if wp.icao:
            extra += f"<br>OACI: {wp.icao}"
        if wp.end_type != "standard":
            extra += f"<br>Fin: {wp.end_type}"
        folium.Marker(
            location=[wp.lat, wp.lon],
            popup=f"<b>{label}</b>{extra}",
            tooltip=label,
            icon=folium.Icon(color=color, icon=icon, prefix="fa"),
        ).add_to(m)

        if wp.end_type == "verticale":
            folium.Marker(
                location=[wp.lat, wp.lon],
                icon=folium.DivIcon(html="""
                    <div style="font-size:16px;font-weight:700;color:#f59e0b;background:white;border:1px solid #f59e0b;border-radius:999px;padding:2px 6px;">
                        V
                    </div>
                """)
            ).add_to(m)
        elif wp.end_type == "tour_de_piste":
            folium.Marker(
                location=[wp.lat, wp.lon],
                icon=folium.DivIcon(html="""
                    <div style="font-size:12px;font-weight:700;color:#2563eb;background:white;border:1px solid #2563eb;border-radius:999px;padding:2px 6px;">
                        TDP
                    </div>
                """)
            ).add_to(m)

    # Legs
    for leg in legs:
        seg = interpolate_gc(leg.from_lat, leg.from_lon, leg.to_lat, leg.to_lon, n=28)
        selected = leg.idx == selected_leg_idx
        folium.PolyLine(
            locations=seg,
            color="#ef4444" if selected else "#0f172a",
            weight=6 if selected else 4,
            opacity=0.95 if selected else 0.70,
            tooltip=(
                f"Branche {leg.idx}: {leg.from_name} → {leg.to_name} | "
                f"RM {leg.route_true_deg:.0f}° | HDG {leg.heading_true_deg:.0f}° | GS {leg.gs_kt:.0f} kt"
            ),
        ).add_to(m)

    # fit bounds
    min_lat = min(p[0] for p in points)
    max_lat = max(p[0] for p in points)
    min_lon = min(p[1] for p in points)
    max_lon = max(p[1] for p in points)
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=(20, 20))

    folium.LayerControl(collapsed=True).add_to(m)
    return m


# =========================
# UI HELPERS
# =========================

def metric_card(label: str, value: str):
    st.markdown(
        f"""
        <div style="
            border:1px solid rgba(128,128,128,0.25);
            border-radius:16px;
            padding:12px 14px;
            margin-bottom:8px;
            background:rgba(255,255,255,0.03);
        ">
            <div style="font-size:0.82rem;opacity:0.7;">{label}</div>
            <div style="font-size:1.25rem;font-weight:700;line-height:1.25;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def init_waypoints_df():
    if "wp_df" not in st.session_state:
        st.session_state.wp_df = pd.DataFrame([
            {
                "name": "LFXX",
                "icao": "",
                "lat": 43.6000,
                "lon": 3.9000,
                "altitude_ft": 3500,
                "tas_kt": 105,
                "end_type": "standard",
            },
            {
                "name": "LFYY",
                "icao": "",
                "lat": 43.4500,
                "lon": 5.2000,
                "altitude_ft": 3500,
                "tas_kt": 105,
                "end_type": "tour_de_piste",
            },
        ])


def resolve_waypoints(df: pd.DataFrame) -> Tuple[List[Waypoint], List[str], Optional[float]]:
    warnings = []
    arrival_elev_ft = None
    out = []

    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip() or "PT"
        icao = str(row.get("icao", "")).strip().upper()
        lat = row.get("lat")
        lon = row.get("lon")
        alt_ft = float(row.get("altitude_ft", 3500) or 3500)
        tas_kt = float(row.get("tas_kt", 105) or 105)
        end_type = str(row.get("end_type", "standard")).strip()
        if end_type not in END_TYPE_OPTIONS:
            end_type = "standard"

        if icao and (pd.isna(lat) or pd.isna(lon) or lat == "" or lon == ""):
            ad = resolve_icao(icao)
            if ad:
                lat = ad.lat
                lon = ad.lon
                name = ad.icao
                arrival_elev_ft = ad.elev_ft
            else:
                warnings.append(f"OACI introuvable: {icao}")

        if pd.isna(lat) or pd.isna(lon):
            warnings.append(f"Point ignoré faute de coordonnées: {name}")
            continue

        out.append(
            Waypoint(
                name=name,
                lat=float(lat),
                lon=float(lon),
                altitude_ft=alt_ft,
                tas_kt=tas_kt,
                end_type=end_type,
                icao=icao,
            )
        )

    if out and out[-1].icao:
        ad = resolve_icao(out[-1].icao)
        if ad:
            arrival_elev_ft = ad.elev_ft

    return out, warnings, arrival_elev_ft


# =========================
# APP
# =========================

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🛩️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.block-container {padding-top: 0.8rem; padding-bottom: 2rem;}
[data-testid="stHorizontalBlock"] {gap: 0.5rem;}
div[data-testid="stDataFrameResizable"] {font-size: 0.92rem;}
</style>
""", unsafe_allow_html=True)

st.title("🛩️ Prépa VFR mobile")
st.caption("Route complète, METAR, dérive, carte openAIP, profil vertical.")

init_waypoints_df()

with st.expander("Configuration", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        dep_icao = st.text_input("Départ OACI", value="LFMT").strip().upper()
        off_local = st.text_input("Heure OFF UTC (YYYY-MM-DD HH:MM)", value=datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
        openaip_key = st.text_input("Clé API openAIP", type="password")
    with c2:
        cruise_alt_ft = st.number_input("Altitude croisière globale (ft)", min_value=500, max_value=18000, value=3500, step=100)
        fuel_burn_lph = st.number_input("Conso (L/h)", min_value=1.0, max_value=200.0, value=28.0, step=0.5)
        reserve_min = st.number_input("Réserve (min)", min_value=0, max_value=180, value=45, step=5)
    with c3:
        climb_rate_fpm = st.number_input("Taux montée (ft/min)", min_value=100, max_value=3000, value=500, step=50)
        climb_speed_kt = st.number_input("Vitesse montée (kt)", min_value=40, max_value=200, value=75, step=1)
        descent_rate_fpm = st.number_input("Taux descente (ft/min)", min_value=100, max_value=3000, value=500, step=50)
        descent_speed_kt = st.number_input("Vitesse descente (kt)", min_value=40, max_value=250, value=100, step=1)

try:
    offblock_utc = datetime.strptime(off_local, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
except Exception:
    st.error("Format heure OFF invalide. Utilise YYYY-MM-DD HH:MM")
    st.stop()

departure = resolve_icao(dep_icao)
if not departure:
    st.error("Aérodrome de départ introuvable.")
    st.stop()

metar_raw, metar_decoded = fetch_metar(dep_icao)
departure.metar_raw = metar_raw
departure.metar_decoded = metar_decoded

with st.expander("Route / points", expanded=True):
    st.caption("Renseigne soit un OACI, soit lat/lon. La dernière ligne peut être l'arrivée.")
    edited_df = st.data_editor(
        st.session_state.wp_df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "name": st.column_config.TextColumn("Nom / label"),
            "icao": st.column_config.TextColumn("OACI"),
            "lat": st.column_config.NumberColumn("Lat", format="%.6f"),
            "lon": st.column_config.NumberColumn("Lon", format="%.6f"),
            "altitude_ft": st.column_config.NumberColumn("Altitude branche (ft)", min_value=500, max_value=18000, step=100),
            "tas_kt": st.column_config.NumberColumn("TAS (kt)", min_value=40, max_value=250, step=1),
            "end_type": st.column_config.SelectboxColumn("Fin branche", options=END_TYPE_OPTIONS),
        },
        hide_index=True,
        key="route_editor",
    )
    st.session_state.wp_df = edited_df

waypoints, route_warnings, arrival_elev_ft = resolve_waypoints(edited_df)
if route_warnings:
    for w in route_warnings:
        st.warning(w)

if not waypoints:
    st.warning("Ajoute au moins un point de route.")
    st.stop()

if arrival_elev_ft is None:
    arrival_elev_ft = 0.0

legs = compute_legs(departure, waypoints, offblock_utc)
selected_leg = st.selectbox("Branche affichée", options=[leg.idx for leg in legs], format_func=lambda i: f"Branche {i}: {legs[i-1].from_name} → {legs[i-1].to_name}")

tabs = st.tabs(["Carte", "Navigation", "Profil vertical", "Météo"])

# =========================
# CARTE
# =========================
with tabs[0]:
    fmap = build_map(departure, waypoints, legs, selected_leg, openaip_key)
    st_folium(fmap, use_container_width=True, height=520)

    leg = legs[selected_leg - 1]
    c1, c2 = st.columns(2)
    with c1:
        metric_card("Branche", f"{leg.from_name} → {leg.to_name}")
        metric_card("Route vraie", f"{leg.route_true_deg:.0f}°")
        metric_card("Altitude", f"{leg.altitude_ft:.0f} ft")
        metric_card("Vent", f"{leg.wind_dir_deg:.0f}/{leg.wind_speed_kt:.0f} kt ({leg.wind_source})")
    with c2:
        metric_card("Dérive", f"{leg.drift_deg:+.1f}°")
        metric_card("Cap vrai", f"{leg.heading_true_deg:.0f}°")
        metric_card("GS", f"{leg.gs_kt:.0f} kt")
        metric_card("Fin de branche", leg.end_type.replace("_", " "))

# =========================
# NAVIGATION
# =========================
with tabs[1]:
    total_nm = sum(l.distance_nm for l in legs)
    total_min = sum(l.ete_min for l in legs)
    fuel_trip = total_min / 60.0 * fuel_burn_lph
    fuel_total = fuel_trip + reserve_min / 60.0 * fuel_burn_lph

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Distance totale", f"{total_nm:.1f} NM")
    with c2:
        metric_card("Temps total", f"{total_min:.0f} min")
    with c3:
        metric_card("Trip fuel", f"{fuel_trip:.1f} L")
    with c4:
        metric_card("Fuel + réserve", f"{fuel_total:.1f} L")

    rows = []
    for l in legs:
        rows.append({
            "Branche": f"{l.idx}",
            "De": l.from_name,
            "Vers": l.to_name,
            "Dist NM": round(l.distance_nm, 1),
            "RM°": round(l.route_true_deg),
            "Vent": f"{l.wind_dir_deg:.0f}/{l.wind_speed_kt:.0f}",
            "Drv°": round(l.drift_deg, 1),
            "HDG°": round(l.heading_true_deg),
            "GS kt": round(l.gs_kt),
            "ETE min": round(l.ete_min, 1),
            "ETA UTC": l.eta.strftime("%H:%M"),
            "Fin": l.end_type,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# =========================
# PROFIL VERTICAL
# =========================
with tabs[2]:
    verticale_height_ft = st.number_input("Hauteur verticale terrain (ft sol)", min_value=500, max_value=3000, value=1500, step=100)
    tdp_height_ft = st.number_input("Hauteur tour de piste (ft sol)", min_value=500, max_value=2000, value=1000, step=100)

    route_pts = build_route_points(departure, waypoints, n_per_leg=18)
    route_d = route_distances_nm(route_pts)
    elev_m = fetch_elevations(tuple(p[0] for p in route_pts), tuple(p[1] for p in route_pts))
    terrain_ft = [m_to_ft(x) for x in elev_m] if elev_m else [0.0] * len(route_pts)

    profile = build_vertical_profile(
        departure=departure,
        arrival_elev_ft=arrival_elev_ft,
        last_end_type=waypoints[-1].end_type,
        route_d_nm=route_d,
        cruise_alt_ft=cruise_alt_ft,
        climb_rate_fpm=climb_rate_fpm,
        climb_speed_kt=climb_speed_kt,
        descent_rate_fpm=descent_rate_fpm,
        descent_speed_kt=descent_speed_kt,
        verticale_height_ft=verticale_height_ft,
        tdp_height_ft=tdp_height_ft,
    )

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=route_d,
        y=terrain_ft,
        mode="lines",
        name="Relief",
        fill="tozeroy",
    ))
    fig.add_trace(go.Scatter(
        x=route_d,
        y=profile["alt_profile_ft"],
        mode="lines",
        name="Altitude avion",
    ))
    fig.add_vline(x=profile["toc_nm"], line_dash="dash", annotation_text="Fin montée")
    fig.add_vline(x=profile["tod_nm"], line_dash="dash", annotation_text="Début descente")
    fig.update_layout(
        height=420,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Distance cumulée (NM)",
        yaxis_title="Altitude (ft)",
        legend_orientation="h",
    )
    st.plotly_chart(fig, use_container_width=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("Fin montée", f"{profile['toc_nm']:.1f} NM")
    with c2:
        metric_card("Début descente", f"{profile['tod_nm']:.1f} NM")
    with c3:
        metric_card("Altitude arrivée cible", f"{profile['arr_target_ft']:.0f} ft")

    max_terrain = max(terrain_ft) if terrain_ft else 0.0
    min_margin = min(a - t for a, t in zip(profile["alt_profile_ft"], terrain_ft)) if terrain_ft else 0.0
    if min_margin < 500:
        st.error(f"Marge verticale minimale faible: {min_margin:.0f} ft")
    else:
        st.success(f"Marge verticale minimale: {min_margin:.0f} ft")

# =========================
# METEO
# =========================
with tabs[3]:
    st.subheader(f"Départ {departure.icao}")
    metric_card("Terrain", f"{departure.name}")
    metric_card("Position", f"{departure.lat:.4f}, {departure.lon:.4f}")
    metric_card("Élévation", f"{departure.elev_ft:.0f} ft")

    if departure.metar_raw:
        st.code(departure.metar_raw, language="text")
        st.info(metar_human(departure.metar_decoded))
    else:
        st.warning("METAR indisponible pour ce terrain.")

    leg = legs[selected_leg - 1]
    st.subheader(f"Vent branche {leg.idx}")
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("Source", leg.wind_source)
    with c2:
        metric_card("Vent retenu", f"{leg.wind_dir_deg:.0f}/{leg.wind_speed_kt:.0f} kt")
    with c3:
        metric_card("Validité approx.", nearest_hour(leg.eta - timedelta(minutes=leg.ete_min/2)).strftime("%Y-%m-%d %H:%M UTC"))

st.caption("Conseil pratique : garde 4 à 6 points max sur mobile pour rester très fluide.")
