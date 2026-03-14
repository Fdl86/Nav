# app.py
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict

import folium
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_folium import st_folium

try:
    from pygeomag import GeoMag

    try:
        _GEOMAG = GeoMag(coefficients_file="wmm/WMM_2025.COF")
    except Exception:
        _GEOMAG = GeoMag()
    GEOMAG_AVAILABLE = True
except Exception:
    _GEOMAG = None
    GEOMAG_AVAILABLE = False

APP_TITLE = "Prépa VFR Mobile"
UA = {"User-Agent": "vfr-prep-mobile/1.5"}

AIRPORTS_CSV_URL = "https://ourairports.com/data/airports.csv"
AIRPORTS_FALLBACK_CSV_URL = "https://raw.githubusercontent.com/datasets/airport-codes/main/data/airport-codes.csv"
METAR_API_URL = "https://aviationweather.gov/api/data/metar"
TAF_API_URL = "https://aviationweather.gov/api/data/taf"
OPENMETEO_DWD = "https://api.open-meteo.com/v1/dwd-icon"
OPENMETEO_MF = "https://api.open-meteo.com/v1/meteofrance"
OPENMETEO_ELEV = "https://api.open-meteo.com/v1/elevation"

END_TYPES = ["standard", "verticale", "tour_de_piste"]
LEG_TYPES = ["point_tournant", "aerodrome"]

DWD_LEVELS_M = {
    1000: 110, 975: 320, 950: 500, 925: 800, 900: 1000, 850: 1500,
    800: 1900, 700: 3000, 600: 4200, 500: 5600, 400: 7200, 300: 9200,
    250: 10400, 200: 11800
}
MF_LEVELS_M = {
    1000: 110, 950: 500, 925: 800, 900: 1000, 850: 1500, 800: 1900,
    750: 2500, 700: 3000, 650: 3600, 600: 4200, 550: 4900, 500: 5600,
    450: 6300, 400: 7200, 350: 8100, 300: 9200, 250: 10400, 200: 11800
}
_DWD_LEVELS_SORTED = sorted(DWD_LEVELS_M.items(), key=lambda x: x[1])
_MF_LEVELS_SORTED  = sorted(MF_LEVELS_M.items(),  key=lambda x: x[1])

@dataclass
class Aerodrome:
    icao: str
    name: str
    lat: float
    lon: float
    elev_ft: float


@dataclass
class LegInput:
    leg_type: str
    route_true_deg: float
    distance_nm: float
    altitude_ft: float
    end_type: str
    target_icao: str = ""
    label: str = ""


@dataclass
class NavPoint:
    name: str
    lat: float
    lon: float
    elev_ft: float = 0.0
    icao: str = ""


@dataclass
class LegResult:
    idx: int
    leg_type: str
    start_name: str
    end_name: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    mid_lat: float
    mid_lon: float
    distance_nm: float
    route_true_deg: float
    declination_deg: float
    route_mag_deg: float
    altitude_ft: float
    tas_kt: float
    wind_source: str
    wind_dir_deg: float
    wind_speed_kt: float
    drift_deg: float
    heading_true_deg: float
    heading_mag_deg: float
    gs_kt: float
    ete_min: float
    end_type: str
    arrival_elev_ft: float = 0.0


@st.cache_resource
def session():
    s = requests.Session()
    s.headers.update(UA)
    return s


def fetch_json(url: str, params: Optional[dict] = None, timeout: int = 20):
    r = session().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_airports_primary() -> pd.DataFrame:
    df = pd.read_csv(AIRPORTS_CSV_URL, low_memory=False)
    keep = [
        "ident",
        "name",
        "latitude_deg",
        "longitude_deg",
        "elevation_ft",
        "type",
        "iso_country",
    ]
    df = df[keep].copy()
    df["ident"] = df["ident"].astype(str).str.upper()
    df["name"] = df["name"].fillna("").astype(str)
    df["latitude_deg"] = pd.to_numeric(df["latitude_deg"], errors="coerce")
    df["longitude_deg"] = pd.to_numeric(df["longitude_deg"], errors="coerce")
    df["elevation_ft"] = pd.to_numeric(df["elevation_ft"], errors="coerce").fillna(0)
    df = df.dropna(subset=["latitude_deg", "longitude_deg"])
    return df


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_airports_fallback() -> pd.DataFrame:
    df = pd.read_csv(AIRPORTS_FALLBACK_CSV_URL, low_memory=False)
    needed = ["ident", "name", "latitude_deg", "longitude_deg", "elevation_ft", "type", "iso_country"]
    for col in needed:
        if col not in df.columns:
            df[col] = None
    df = df[needed].copy()
    df["ident"] = df["ident"].astype(str).str.upper()
    df["name"] = df["name"].fillna("").astype(str)
    df["latitude_deg"] = pd.to_numeric(df["latitude_deg"], errors="coerce")
    df["longitude_deg"] = pd.to_numeric(df["longitude_deg"], errors="coerce")
    df["elevation_ft"] = pd.to_numeric(df["elevation_ft"], errors="coerce").fillna(0)
    df = df.dropna(subset=["latitude_deg", "longitude_deg"])
    return df


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_airports_index() -> Dict[str, Tuple[str, float, float, float]]:
    index: Dict[str, Tuple[str, float, float, float]] = {}

    try:
        df_primary = load_airports_primary()
        for row in df_primary.itertuples(index=False):
            index[str(row.ident)] = (
                str(row.name),
                float(row.latitude_deg),
                float(row.longitude_deg),
                float(row.elevation_ft),
            )
    except Exception:
        pass

    try:
        df_fallback = load_airports_fallback()
        for row in df_fallback.itertuples(index=False):
            ident = str(row.ident)
            if ident not in index:
                index[ident] = (
                    str(row.name),
                    float(row.latitude_deg),
                    float(row.longitude_deg),
                    float(row.elevation_ft),
                )
    except Exception:
        pass

    return index


def resolve_airport(icao: str) -> Optional[Aerodrome]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    rec = load_airports_index().get(icao)
    if not rec:
        return None

    name, lat, lon, elev_ft = rec
    return Aerodrome(
        icao=icao,
        name=name,
        lat=lat,
        lon=lon,
        elev_ft=elev_ft,
    )


@st.cache_data(ttl=60 * 10, show_spinner=False)
def fetch_metar(icao: str) -> Tuple[Optional[str], Optional[dict]]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None, None

    try:
        r = session().get(
            METAR_API_URL,
            params={"ids": icao, "format": "json"},
            timeout=15,
        )
        if r.status_code == 204:
            return None, None
        r.raise_for_status()
        js = r.json()
        if not js:
            return None, None

        m = js[0]
        raw = m.get("rawOb") or m.get("raw_text") or m.get("raw")
        decoded = {
            "obs_time": m.get("obsTime") or m.get("receiptTime"),
            "wind_dir": m.get("wdir"),
            "wind_speed_kt": m.get("wspd"),
        }
        return raw, decoded
    except Exception:
        return None, None


@st.cache_data(ttl=60 * 10, show_spinner=False)
def fetch_taf(icao: str) -> Optional[str]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None
    try:
        r = session().get(
            TAF_API_URL,
            params={"ids": icao, "format": "json"},
            timeout=15,
        )
        if r.status_code == 204:
            return None
        r.raise_for_status()
        js = r.json()
        if not js:
            return None
        item = js[0]
        return (
            item.get("rawTAF")
            or item.get("raw_text")
            or item.get("raw")
            or item.get("taf")
        )
    except Exception:
        return None


@st.cache_data(ttl=60 * 10, show_spinner=False)
def fetch_airport_weather_bundle(icao: str) -> Dict[str, object]:
    metar_raw, metar_decoded = fetch_metar(icao)
    taf_raw = fetch_taf(icao)
    return {
        "metar_raw": metar_raw,
        "metar_decoded": metar_decoded,
        "taf_raw": taf_raw,
    }
def ft_to_m(ft: float) -> float:
    return ft * 0.3048


def m_to_ft(m: float) -> float:
    return m / 0.3048


def nm_to_m(nm: float) -> float:
    return nm * 1852.0


def m_to_nm(m: float) -> float:
    return m / 1852.0


def deg_norm(x: float) -> float:
    return x % 360.0


def shortest_angle_deg(a: float, b: float) -> float:
    return (a - b + 180) % 360 - 180


def route3(v: float) -> str:
    return f"{int(round(v)) % 360:03d}"

def format_minutes_mmss(minutes_value: float) -> str:
    total_seconds = max(0, int(round(minutes_value * 60)))
    mm = total_seconds // 60
    ss = total_seconds % 60
    return f"{mm:02d}:{ss:02d}"

def format_duration(minutes_value: float) -> str:
    """MM:SS si < 60 min, HHhMM au-delà."""
    if minutes_value < 60.0:
        total_seconds = max(0, int(round(minutes_value * 60)))
        mm = total_seconds // 60
        ss = total_seconds % 60
        return f"{mm:02d}:{ss:02d}"
    else:
        total_minutes = max(0, int(round(minutes_value)))
        hh = total_minutes // 60
        mm = total_minutes % 60
        return f"{hh:02d}h{mm:02d}"

def correction_label(wca_deg: float) -> str:
    if abs(wca_deg) < 0.05:
        return "nulle"
    return "droite" if wca_deg > 0 else "gauche"

def haversine_nm(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return m_to_nm(2 * r * math.asin(math.sqrt(a)))


def initial_bearing_deg(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return deg_norm(math.degrees(math.atan2(x, y)))


def destination_point(lat_deg: float, lon_deg: float, bearing_deg: float, distance_nm: float):
    r = 6371000.0
    d = nm_to_m(distance_nm) / r
    brg = math.radians(bearing_deg)
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d)
        + math.cos(lat1) * math.sin(d) * math.cos(brg)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), ((math.degrees(lon2) + 540) % 360) - 180


def interpolate_line(lat1, lon1, lat2, lon2, n=16):
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t))
    return pts


def uv_from_wind_from(speed_kt: float, direction_from_deg: float):
    rad = math.radians(direction_from_deg)
    u = -speed_kt * math.sin(rad)
    v = -speed_kt * math.cos(rad)
    return u, v


def wind_from_uv(u: float, v: float):
    speed = math.hypot(u, v)
    if speed < 1e-6:
        return 0.0, 0.0
    direction_to = math.degrees(math.atan2(u, v)) % 360.0
    direction_from = (direction_to + 180.0) % 360.0
    return direction_from, speed


def metar_surface_wind(decoded: Optional[dict]) -> Optional[Tuple[float, float]]:
    if not decoded:
        return None
    wd = decoded.get("wind_dir")
    ws = decoded.get("wind_speed_kt")
    if wd is None or ws is None:
        return None
    try:
        if str(wd).upper() == "VRB":
            return None
        return float(wd), float(ws)
    except Exception:
        return None


def nearest_hour(dt: datetime):
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def generation_hour_utc() -> datetime:
    return nearest_hour(datetime.now(timezone.utc))

def pick_levels(target_alt_m: float, level_map: Dict[int, float]):
    levels = _DWD_LEVELS_SORTED if level_map is DWD_LEVELS_M else _MF_LEVELS_SORTED
    if target_alt_m <= levels[0][1]:
        return levels[0][0], levels[0][0]
    if target_alt_m >= levels[-1][1]:
        return levels[-1][0], levels[-1][0]
    for (p1, h1), (p2, h2) in zip(levels, levels[1:]):
        if h1 <= target_alt_m <= h2:
            return p1, p2
    return levels[-1][0], levels[-1][0]

@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_openmeteo_hour_block(
    source: str,
    lats: Tuple[float, ...],
    lons: Tuple[float, ...],
    hourly_vars: Tuple[str, ...],
):
    url = OPENMETEO_DWD if source == "ICON-D2" else OPENMETEO_MF
    params = {
        "latitude": ",".join(f"{x:.6f}" for x in lats),
        "longitude": ",".join(f"{x:.6f}" for x in lons),
        "hourly": ",".join(hourly_vars),
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "forecast_days": 2,
        "cell_selection": "nearest",
    }
    js = fetch_json(url, params=params, timeout=25)
    return js if isinstance(js, list) else [js]

def get_hour_index(hourly_time: List[str], target_key: str) -> Optional[int]:
    for i, t in enumerate(hourly_time):
        if t == target_key:
            return i
    return None

def interpolate_pressure_wind_for_item(item: dict, hour_idx: int, target_alt_ft: float, level_map: Dict[int, float]):
    target_alt_m = ft_to_m(target_alt_ft)
    p_low, p_high = pick_levels(target_alt_m, level_map)
    hourly = item.get("hourly", {})

    def at(var_name: str):
        arr = hourly.get(var_name, [])
        if hour_idx is None or hour_idx < 0 or hour_idx >= len(arr):
            return None
        return arr[hour_idx]

    spd_low = at(f"wind_speed_{p_low}hPa")
    dir_low = at(f"wind_direction_{p_low}hPa")
    z_low = at(f"geopotential_height_{p_low}hPa")
    if spd_low is None or dir_low is None or z_low is None:
        return None

    if p_low == p_high:
        return float(dir_low), float(spd_low)

    spd_high = at(f"wind_speed_{p_high}hPa")
    dir_high = at(f"wind_direction_{p_high}hPa")
    z_high = at(f"geopotential_height_{p_high}hPa")
    if spd_high is None or dir_high is None or z_high is None:
        return None

    z1 = float(z_low)
    z2 = float(z_high)
    t = 0.0 if abs(z2 - z1) < 1e-6 else max(0.0, min(1.0, (target_alt_m - z1) / (z2 - z1)))

    u1, v1 = uv_from_wind_from(float(spd_low), float(dir_low))
    u2, v2 = uv_from_wind_from(float(spd_high), float(dir_high))
    u = u1 + (u2 - u1) * t
    v = v1 + (v2 - v1) * t
    wd, ws = wind_from_uv(u, v)
    return wd, ws


def extract_surface_wind_for_item(item: dict, hour_idx: int):
    hourly = item.get("hourly", {})
    spd_arr = hourly.get("wind_speed_10m", [])
    dir_arr = hourly.get("wind_direction_10m", [])
    if hour_idx is None or hour_idx < 0:
        return None
    if hour_idx >= len(spd_arr) or hour_idx >= len(dir_arr):
        return None
    spd = spd_arr[hour_idx]
    wdir = dir_arr[hour_idx]
    if spd is None or wdir is None:
        return None
    return float(wdir), float(spd)


def mean_vector_from_pairs(pairs: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    if not pairs:
        return None
    u_sum = 0.0
    v_sum = 0.0
    for wd, ws in pairs:
        u, v = uv_from_wind_from(ws, wd)
        u_sum += u
        v_sum += v
    u_avg = u_sum / len(pairs)
    v_avg = v_sum / len(pairs)
    return wind_from_uv(u_avg, v_avg)


def sample_point_count(distance_nm: float) -> int:
    # n = nombre de segments dans interpolate_line, donc :
    # 1 -> 2 points, 2 -> 3 points
    if distance_nm <= 25:
        return 1   # point milieu + extrémités = 2 points utiles / très léger
    return 2       # 3 points pour les branches longues

def mean_branch_pressure_wind(
    items: List[dict],
    point_indices: List[int],
    hour_key: str,
    altitude_ft: float,
    level_map: Dict[int, float],
) -> Optional[Tuple[float, float]]:
    pairs = []
    for idx in point_indices:
        item = items[idx]
        hour_idx = get_hour_index(item.get("hourly", {}).get("time", []), hour_key)
        pair = interpolate_pressure_wind_for_item(item, hour_idx, altitude_ft, level_map)
        if pair:
            pairs.append(pair)
    return mean_vector_from_pairs(pairs)

def mean_branch_surface_wind(
    items: List[dict],
    point_indices: List[int],
    hour_key: str,
) -> Optional[Tuple[float, float]]:
    pairs = []
    for idx in point_indices:
        item = items[idx]
        hour_idx = get_hour_index(item.get("hourly", {}).get("time", []), hour_key)
        pair = extract_surface_wind_for_item(item, hour_idx)
        if pair:
            pairs.append(pair)
    return mean_vector_from_pairs(pairs)


def union_pressure_vars(altitudes_ft: List[float], level_map: Dict[int, float]) -> Tuple[str, ...]:
    vars_set = set()
    for altitude_ft in altitudes_ft:
        p_low, p_high = pick_levels(ft_to_m(altitude_ft), level_map)
        vars_set.update({
            f"wind_speed_{p_low}hPa",
            f"wind_direction_{p_low}hPa",
            f"geopotential_height_{p_low}hPa",
        })
        if p_high != p_low:
            vars_set.update({
                f"wind_speed_{p_high}hPa",
                f"wind_direction_{p_high}hPa",
                f"geopotential_height_{p_high}hPa",
            })
    return tuple(sorted(vars_set))

def prefetch_winds_for_geometries(
    geometries: List[dict],
    metar_decoded: Optional[dict] = None,
) -> Dict[int, Tuple[str, float, float]]:
    if not geometries:
        return {}

    gen_hour = generation_hour_utc()
    hour_key = gen_hour.strftime("%Y-%m-%dT%H:%M")

    point_lats: List[float] = []
    point_lons: List[float] = []
    branch_point_indices: Dict[int, List[int]] = {}
    altitudes_ft: List[float] = []

    point_cursor = 0
    for geom in geometries:
        pts = geom["sample_points"]
        count = len(pts)
        branch_point_indices[geom["idx"]] = list(range(point_cursor, point_cursor + count))
        point_cursor += count
        point_lats.extend(p[0] for p in pts)
        point_lons.extend(p[1] for p in pts)
        altitudes_ft.append(geom["altitude_ft"])

    lats = tuple(round(x, 6) for x in point_lats)
    lons = tuple(round(x, 6) for x in point_lons)

    icon_pressure_vars = union_pressure_vars(altitudes_ft, DWD_LEVELS_M)
    wind_by_leg: Dict[int, Tuple[str, float, float]] = {}

    icon_pressure_items = None
    if icon_pressure_vars:
        try:
            icon_pressure_items = fetch_openmeteo_hour_block("ICON-D2", lats, lons, icon_pressure_vars)
        except Exception:
            icon_pressure_items = None

    if icon_pressure_items:
        for geom in geometries:
            point_indices = branch_point_indices[geom["idx"]]
            avg = mean_branch_pressure_wind(
                icon_pressure_items,
                point_indices,
                hour_key,
                geom["altitude_ft"],
                DWD_LEVELS_M,
            )
            if avg:
                wind_by_leg[geom["idx"]] = ("ICON-D2 niveau", avg[0], avg[1])

    metar_pair = metar_surface_wind(metar_decoded)

    for geom in geometries:
        if geom["idx"] not in wind_by_leg:
            if metar_pair:
                wind_by_leg[geom["idx"]] = ("METAR départ", metar_pair[0], metar_pair[1])
            else:
                wind_by_leg[geom["idx"]] = ("Vent indisponible", 0.0, 0.0)

    return wind_by_leg
    
def wind_correction(course_deg: float, tas_kt: float, wind_from_deg: float, wind_speed_kt: float):
    delta = math.radians(shortest_angle_deg(wind_from_deg, course_deg))
    ratio = 0.0 if tas_kt <= 0 else max(
        -0.9999,
        min(0.9999, (wind_speed_kt / tas_kt) * math.sin(delta))
    )
    wca = math.asin(ratio)
    gs = tas_kt * math.cos(wca) - wind_speed_kt * math.cos(delta)
    gs = max(gs, 20.0)
    drift = math.degrees(wca)
    heading = deg_norm(course_deg + drift)
    return drift, heading, gs

def magnetic_declination_deg(lat: float, lon: float, alt_ft: float = 0.0) -> float:
    if not GEOMAG_AVAILABLE or _GEOMAG is None:
        return 0.0
    try:
        now_utc = datetime.now(timezone.utc)
        year_fraction = now_utc.year + (now_utc.timetuple().tm_yday / 365.25)
        result = _GEOMAG.calculate(
            glat=lat,
            glon=lon,
            alt=ft_to_m(alt_ft) / 1000.0,
            time=year_fraction,
        )
        return float(result.d)
    except Exception:
        return 0.0

def true_to_magnetic(true_deg: float, declination_deg: float) -> float:
    # variation Est = on soustrait, Ouest = on ajoute
    return deg_norm(true_deg - declination_deg)

def build_route(
    departure: Aerodrome,
    legs_in: List[LegInput],
    tas_kt: float,
    departure_metar_decoded: Optional[dict] = None,
) -> Tuple[List[LegResult], List[NavPoint]]:
    geometries: List[dict] = []
    nav_points: List[NavPoint] = [NavPoint(departure.icao, departure.lat, departure.lon, departure.elev_ft, departure.icao)]

    prev = nav_points[0]

    for idx, leg in enumerate(legs_in, start=1):
        if leg.leg_type == "aerodrome":
            arr = resolve_airport(leg.target_icao)
            if not arr:
                raise ValueError(f"Aérodrome introuvable: {leg.target_icao}")
            end_pt = NavPoint(arr.icao, arr.lat, arr.lon, arr.elev_ft, arr.icao)
            route_true = initial_bearing_deg(prev.lat, prev.lon, end_pt.lat, end_pt.lon)
            distance_nm = haversine_nm(prev.lat, prev.lon, end_pt.lat, end_pt.lon)
        else:
            route_true = deg_norm(leg.route_true_deg)
            distance_nm = leg.distance_nm
            lat2, lon2 = destination_point(prev.lat, prev.lon, route_true, distance_nm)
            label = leg.label.strip() if leg.label.strip() else f"PT {idx}"
            end_pt = NavPoint(label, lat2, lon2, 0.0, "")

        sample_points = interpolate_line(
            prev.lat,
            prev.lon,
            end_pt.lat,
            end_pt.lon,
            n=sample_point_count(distance_nm),
        )

        geometries.append(
            {
                "idx": idx,
                "leg_type": leg.leg_type,
                "start_name": prev.name,
                "end_name": end_pt.name,
                "start_lat": prev.lat,
                "start_lon": prev.lon,
                "end_lat": end_pt.lat,
                "end_lon": end_pt.lon,
                "distance_nm": distance_nm,
                "route_true_deg": route_true,
                "altitude_ft": leg.altitude_ft,
                "end_type": leg.end_type,
                "arrival_elev_ft": end_pt.elev_ft,
                "mid_lat": (prev.lat + end_pt.lat) / 2.0,
                "mid_lon": (prev.lon + end_pt.lon) / 2.0,
                "sample_points": sample_points,
            }
        )

        nav_points.append(end_pt)
        prev = end_pt

    wind_by_leg = prefetch_winds_for_geometries(geometries, departure_metar_decoded)

    legs_out: List[LegResult] = []
    for geom in geometries:
        wind_source, wind_dir, wind_speed = wind_by_leg.get(geom["idx"], ("Aucune donnée vent", 0.0, 0.0))
        drift, heading_true, gs = wind_correction(geom["route_true_deg"], tas_kt, wind_dir, wind_speed)
        ete_min = geom["distance_nm"] / gs * 60.0

        decl = magnetic_declination_deg(geom["mid_lat"], geom["mid_lon"], geom["altitude_ft"])
        route_mag = true_to_magnetic(geom["route_true_deg"], decl)
        heading_mag = true_to_magnetic(heading_true, decl)

        legs_out.append(
            LegResult(
                idx=geom["idx"],
                leg_type=geom["leg_type"],
                start_name=geom["start_name"],
                end_name=geom["end_name"],
                start_lat=geom["start_lat"],
                start_lon=geom["start_lon"],
                end_lat=geom["end_lat"],
                end_lon=geom["end_lon"],
                mid_lat=geom["mid_lat"],
                mid_lon=geom["mid_lon"],
                distance_nm=geom["distance_nm"],
                route_true_deg=geom["route_true_deg"],
                declination_deg=decl,
                route_mag_deg=route_mag,
                altitude_ft=geom["altitude_ft"],
                tas_kt=tas_kt,
                wind_source=wind_source,
                wind_dir_deg=wind_dir,
                wind_speed_kt=wind_speed,
                drift_deg=drift,
                heading_true_deg=heading_true,
                heading_mag_deg=heading_mag,
                gs_kt=gs,
                ete_min=ete_min,
                end_type=geom["end_type"],
                arrival_elev_ft=geom["arrival_elev_ft"],
            )
        )

    return legs_out, nav_points
@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def fetch_elevations(lats: Tuple[float, ...], lons: Tuple[float, ...]):
    try:
        if not lats or not lons or len(lats) != len(lons):
            return None
        all_vals = []
        chunk_size = 80  # évite de surcharger l'API sur les routes longues
        for i in range(0, len(lats), chunk_size):
            sub_lats = lats[i:i + chunk_size]
            sub_lons = lons[i:i + chunk_size]
            js = fetch_json(
                OPENMETEO_ELEV,
                params={
                    "latitude": ",".join(f"{x:.6f}" for x in sub_lats),
                    "longitude": ",".join(f"{x:.6f}" for x in sub_lons),
                },
                timeout=25,
            )
            vals = js.get("elevation", [])
            if not vals:
                return None
            all_vals.extend(vals)
        return all_vals if all_vals else None
    except Exception:
        return None

def build_vertical_profile(
    nav_points: List[NavPoint],
    legs: List[LegResult],
    climb_rate_fpm: float,
    climb_speed_kt: float,
    descent_rate_fpm: float,
    verticale_ft: float,
    tdp_ft: float,
):
    terrain_x: List[float] = []
    terrain_route_pts: List[Tuple[float, float]] = []

    aircraft_x: List[Optional[float]] = []
    aircraft_y: List[Optional[float]] = []

    vt_marks: List[Tuple[float, float, float, str]] = []
    tdp_marks: List[Tuple[float, float, float, str]] = []

    toc_marks: List[Tuple[float, str]] = []
    tod_marks: List[Tuple[float, str]] = []

    cumulative_nm = 0.0
    elapsed_min_total = 0.0
    current_alt = nav_points[0].elev_ft

    for i, leg in enumerate(legs):
        next_leg_exists = i < len(legs) - 1

        n = max(12, min(40, int(round(leg.distance_nm * 1.5))))
        seg_pts = interpolate_line(leg.start_lat, leg.start_lon, leg.end_lat, leg.end_lon, n=n)
        seg_x_local = [round((j / n) * leg.distance_nm, 1) for j in range(n + 1)]

        is_arrival_aerodrome = leg.leg_type == "aerodrome" and leg.arrival_elev_ft > 0
        terrain_alt = leg.arrival_elev_ft if is_arrival_aerodrome else 0.0
        cruise_alt = leg.altitude_ft
        
        if is_arrival_aerodrome and leg.end_type == "verticale":
            end_target_alt = terrain_alt + verticale_ft
        elif is_arrival_aerodrome and leg.end_type == "tour_de_piste":
            end_target_alt = terrain_alt + tdp_ft
        else:
            # En "standard", même si c'est un aérodrome, on l'utilise comme point tournant :
            # pas de descente spécifique, on reste à l'altitude de branche.
            end_target_alt = cruise_alt

        # Montée initiale
        delta_climb_ft = cruise_alt - current_alt
        if delta_climb_ft > 1:
            climb_dist_nm = climb_speed_kt * ((delta_climb_ft / max(climb_rate_fpm, 1)) / 60.0)
            climb_time_min = delta_climb_ft / max(climb_rate_fpm, 1)
        else:
            climb_dist_nm = 0.0
            climb_time_min = 0.0

        # Descente finale
        delta_descent_ft = cruise_alt - end_target_alt
        if delta_descent_ft > 1:
            descent_dist_nm = leg.gs_kt * ((delta_descent_ft / max(descent_rate_fpm, 1)) / 60.0)
            descent_time_min = delta_descent_ft / max(descent_rate_fpm, 1)
        else:
            descent_dist_nm = 0.0
            descent_time_min = 0.0

        total_special_nm = climb_dist_nm + descent_dist_nm
        if total_special_nm > leg.distance_nm and total_special_nm > 1e-6:
            scale = leg.distance_nm / total_special_nm
            climb_dist_nm *= scale
            descent_dist_nm *= scale
            climb_time_min *= scale
            descent_time_min *= scale

        toc_nm_local = climb_dist_nm if climb_dist_nm > 0 else None
        tod_nm_local = leg.distance_nm - descent_dist_nm if descent_dist_nm > 0 else None

        leg_start_elapsed_min = elapsed_min_total
        if toc_nm_local is not None:
            toc_x = round(cumulative_nm + toc_nm_local, 1)
            toc_t = format_minutes_mmss(leg_start_elapsed_min + climb_time_min)
            toc_marks.append((toc_x, toc_t))

        if tod_nm_local is not None:
            cruise_nm_before_descent = max(tod_nm_local - max(climb_dist_nm, 0.0), 0.0)
            cruise_time_min = cruise_nm_before_descent / max(leg.gs_kt, 1e-6) * 60.0
            tod_x = round(cumulative_nm + tod_nm_local, 1)
            tod_t = format_minutes_mmss(leg_start_elapsed_min + climb_time_min + cruise_time_min)
            tod_marks.append((tod_x, tod_t))

        leg_end_x = round(cumulative_nm + leg.distance_nm, 1)

        if is_arrival_aerodrome:
            terrain_label = leg.end_name
            if leg.end_type == "verticale":
                vt_marks.append((leg_end_x, terrain_alt, terrain_alt + verticale_ft, terrain_label))
            elif leg.end_type == "tour_de_piste":
                tdp_marks.append((leg_end_x, terrain_alt, terrain_alt + tdp_ft, terrain_label))
                
        for j, (pt, x_local) in enumerate(zip(seg_pts, seg_x_local)):
            x_global = round(cumulative_nm + x_local, 1)

            if i == 0 and j == 0:
                terrain_x.append(x_global)
                terrain_route_pts.append(pt)
            elif j > 0:
                terrain_x.append(x_global)
                terrain_route_pts.append(pt)

            if climb_dist_nm > 1e-6 and x_local <= climb_dist_nm:
                frac = x_local / climb_dist_nm
                alt = current_alt + (cruise_alt - current_alt) * frac
            elif tod_nm_local is not None and x_local >= tod_nm_local and descent_dist_nm > 1e-6:
                frac = (x_local - tod_nm_local) / descent_dist_nm
                alt = cruise_alt + (end_target_alt - cruise_alt) * frac
            else:
                alt = cruise_alt

            if i == 0 and j == 0:
                aircraft_x.append(x_global)
                aircraft_y.append(round(alt))
            elif j > 0:
                aircraft_x.append(x_global)
                aircraft_y.append(round(alt))

        if is_arrival_aerodrome and leg.end_type in ("verticale", "tour_de_piste") and next_leg_exists:
            aircraft_x.append(None)
            aircraft_y.append(None)
            # La branche suivante doit repartir visuellement du terrain
            current_alt = terrain_alt
            aircraft_x.append(leg_end_x)
            aircraft_y.append(round(terrain_alt))
        else:
            current_alt = end_target_alt

        elapsed_min_total += leg.ete_min
        cumulative_nm = round(cumulative_nm + leg.distance_nm, 1)

    return {
        "terrain_x_nm": terrain_x,
        "terrain_route_pts": terrain_route_pts,
        "aircraft_x_nm": aircraft_x,
        "aircraft_alt_ft": aircraft_y,
        "vt_marks": vt_marks,
        "tdp_marks": tdp_marks,
        "toc_marks": toc_marks,
        "tod_marks": tod_marks,
    }

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

def build_map(nav_points: List[NavPoint],legs: List[LegResult],selected_idx: int,openaip_key: str,basemap: str,):
    all_pts = [(p.lat, p.lon) for p in nav_points]
    center = [sum(x[0] for x in all_pts) / len(all_pts), sum(x[1] for x in all_pts) / len(all_pts)]

    m = folium.Map(location=center, zoom_start=8, control_scale=True, tiles=None)

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
            popup=f"<b>{pt.name}</b>",
            icon=folium.Icon(color=color, icon=icon, prefix="fa"),
        ).add_to(m)

    for leg in legs:
        seg = interpolate_line(leg.start_lat, leg.start_lon, leg.end_lat, leg.end_lon, n=28)
        selected = leg.idx == selected_idx

        folium.PolyLine(
            locations=seg,
            color="#ef4444" if selected else "#0f172a",
            weight=7 if selected else 4,
            opacity=0.95 if selected else 0.70,
            tooltip=f"Branche {leg.idx}: {leg.start_name} → {leg.end_name}",
        ).add_to(m)

        if leg.end_type == "verticale":
            folium.Marker(
                [leg.end_lat, leg.end_lon],
                icon=folium.DivIcon(
                    icon_size=(0, 0),
                    icon_anchor=(0, 0),
                    html="""
                    <div style="
                        font-size:14px;
                        font-weight:700;
                        color:#f59e0b;
                        background:transparent;
                        border:none;
                        padding:0;
                        text-shadow:
                            -1px -1px 0 rgba(255,255,255,0.95),
                             1px -1px 0 rgba(255,255,255,0.95),
                            -1px  1px 0 rgba(255,255,255,0.95),
                             1px  1px 0 rgba(255,255,255,0.95);
                    ">
                        VT
                    </div>
                    """
                )
            ).add_to(m)

        elif leg.end_type == "tour_de_piste":
            folium.Marker(
                [leg.end_lat, leg.end_lon],
                icon=folium.DivIcon(
                    icon_size=(0, 0),
                    icon_anchor=(0, 0),
                    html="""
                    <div style="
                        font-size:12px;
                        font-weight:700;
                        color:#00a6ff;
                        background:transparent;
                        border:none;
                        padding:0;
                        text-shadow:
                            -1px -1px 0 rgba(255,255,255,0.95),
                             1px -1px 0 rgba(255,255,255,0.95),
                            -1px  1px 0 rgba(255,255,255,0.95),
                             1px  1px 0 rgba(255,255,255,0.95);
                    ">
                        TDP
                    </div>
                    """
                )
            ).add_to(m)

        # ===== Vent sur toutes les branches =====
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
                """
            )
        ).add_to(m)

    min_lat = min(p[0] for p in all_pts)
    max_lat = max(p[0] for p in all_pts)
    min_lon = min(p[1] for p in all_pts)
    max_lon = max(p[1] for p in all_pts)
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=(18, 18))
    return m

def metric_card(label: str, value: str):
    st.markdown(
        f"""
        <div style="
            border:1px solid rgba(128,128,128,0.22);
            border-radius:16px;
            padding:10px 12px;
            margin-bottom:8px;
            background:rgba(255,255,255,0.03);
        ">
            <div style="font-size:0.82rem;opacity:0.72;">{label}</div>
            <div style="font-size:1.18rem;font-weight:700;line-height:1.3;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def leg_card(
    leg: LegResult,
    selected: bool = False,
    fuel_leg: Optional[float] = None,
    fuel_remaining_l: Optional[float] = None,
    engine_stop_min: Optional[float] = None,
):
    border = "#ef4444" if selected else "rgba(128,128,128,0.22)"
    bg = "rgba(239,68,68,0.05)" if selected else "rgba(255,255,255,0.03)"

    cv_true = leg.heading_true_deg
    cm_mag = leg.heading_mag_deg
    dm_txt = f"{abs(leg.declination_deg):.1f}°{'E' if leg.declination_deg >= 0 else 'W'}"

    fuel_line = ""
    if fuel_leg is not None and fuel_remaining_l is not None and engine_stop_min is not None:
        _stop_total = int(round(engine_stop_min)) % (24 * 60)
        _stop_hh = _stop_total // 60
        _stop_mm = _stop_total % 60
        _stop_str = f"{_stop_hh:02d}:{_stop_mm:02d}Z"
        fuel_line = (
            f"<br>Fuel {fuel_leg:.1f} L • "
            f"Reste {fuel_remaining_l:.1f} L • "
            f"Arrêt moteur {_stop_str}"
        )

    st.markdown(
        f"""
        <div style="
            border:1px solid {border};
            border-radius:18px;
            padding:12px 14px;
            margin-bottom:10px;
            background:{bg};
        ">
            <div style="font-size:1rem;font-weight:700;margin-bottom:6px;">
                Branche {leg.idx} — {leg.start_name} → {leg.end_name}
            </div>
            <div style="font-size:0.95rem;line-height:1.75;">
                RV {route3(leg.route_true_deg)} •
                CD {abs(leg.drift_deg):.1f}° ({correction_label(leg.drift_deg)}) •
                Cv {route3(cv_true)} •
                Dm {dm_txt} •
                Cm {route3(cm_mag)}<br>
                Dist {leg.distance_nm:.1f} NM •
                Alt {int(round(leg.altitude_ft))} ft •
                GS {leg.gs_kt:.0f} kt •
                ETE {format_minutes_mmss(leg.ete_min)}<br>
                Vent {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f} kt ({leg.wind_source}) •
                Fin {leg.end_type.replace("_", " ")}{fuel_line}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def default_legs():
    return [
        {
            "leg_type": "point_tournant",
            "route_true_deg": 14.0,
            "distance_nm": 18.0,
            "altitude_ft": 3500.0,
            "end_type": "standard",
            "target_icao": "",
            "label": "PT 1",
        }
    ]

def legs_signature(legs_data):
    return tuple(
        (
            l["leg_type"],
            round(float(l["route_true_deg"]), 2),
            round(float(l["distance_nm"]), 2),
            round(float(l["altitude_ft"]), 0),
            l["end_type"],
            (l["target_icao"] or "").strip().upper(),
            (l["label"] or "").strip(),
        )
        for l in legs_data
    )

def ensure_state():
    if "legs_data" not in st.session_state:
        st.session_state.legs_data = default_legs()

    default_map = "OpenAIP" if st.secrets.get("OPENAIP_KEY", "") else "OpenStreetMap"

    if "basemap_choice" not in st.session_state:
        st.session_state.basemap_choice = default_map

    if "basemap_selector" not in st.session_state:
        st.session_state.basemap_selector = st.session_state.basemap_choice

def sync_basemap_choice():
    st.session_state.basemap_choice = st.session_state.basemap_selector

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🛩️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.block-container {padding-top: 0.8rem; padding-bottom: 2rem; max-width: 1100px;}
[data-testid="stHorizontalBlock"] {gap: 0.6rem;}
div[data-testid="stExpander"] details summary p {font-size: 1rem;}
</style>
""", unsafe_allow_html=True)

ensure_state()

st.title("🛩️ Prépa VFR mobile")
st.caption("Départ OACI, METAR/TAF, branches simples, carte openAIP, cap magnétique, profil vertical.")

openaip_key = st.secrets.get("OPENAIP_KEY", "")

with st.expander("Vol", expanded=True):
    # Ligne 1 : OACI, heure départ, TAS
    _now_utc = datetime.now(timezone.utc)
    _default_time = _now_utc.strftime("%H:%M")
    r1c1, r1c2, r1c3 = st.columns(3)
    with r1c1:
        dep_icao = st.text_input("Départ OACI", value="LFBI").strip().upper()
    with r1c2:
        dep_time_str = st.text_input("Heure départ UTC (HH:MM)", value=_default_time, max_chars=5)
    with r1c3:
        tas_kt = st.number_input("TAS (kt)", min_value=40, max_value=220, value=100, step=1)

    # Ligne 2 : taux de montée / descente
    r2c1, r2c2, r2c3 = st.columns(3)
    with r2c1:
        climb_rate_fpm = st.number_input("Taux montée (ft/min)", min_value=100, max_value=3000, value=840, step=10)
    with r2c2:
        climb_speed_kt = st.number_input("Vitesse montée (kt)", min_value=40, max_value=200, value=65, step=1)
    with r2c3:
        descent_rate_fpm = st.number_input("Taux descente (ft/min)", min_value=100, max_value=3000, value=500, step=50)

    # Parse heure départ
    try:
        _h, _m = dep_time_str.strip().split(":")
        dep_time_min = int(_h) * 60 + int(_m)
    except Exception:
        dep_time_min = _now_utc.hour * 60 + _now_utc.minute

with st.expander("Devis carburant", expanded=True):
    c1, c2, c3 = st.columns(3)

    with c1:
        fuel_burn_lph = st.number_input("Consommation (L/h)", min_value=1, max_value=100, value=20, step=1)
        taxi_departure_min = st.number_input("Forfait roulage/arrivée (min)", min_value=0, max_value=60, value=20, step=1)

    with c2:
        diversion_min = st.number_input("Déroutement (min)", min_value=0, max_value=120, value=15, step=1)
        diversion_arrival_min = st.number_input("Arrivée déroutement (min)", min_value=0, max_value=60, value=12, step=1)

    with c3:
        unusable_fuel_l = st.number_input("Carburant non utilisable (L)", min_value=0.0, max_value=50.0, value=2.0, step=0.5)
        final_reserve_min = st.number_input("Réserve finale (min)", min_value=0, max_value=120, value=30, step=1)

    # Calcul emport minimum exposé hors fragment pour usage dans tabs[1]
    _emport_min_total_min = (
        taxi_departure_min
        + diversion_min
        + diversion_arrival_min
        + final_reserve_min
    )
    emport_min_l = _emport_min_total_min / 60.0 * fuel_burn_lph + unusable_fuel_l

    @st.fragment
    def emport_carburant(emport_min_l, emport_min_total_min, emport_total_ref_l):
        st.info(
            f"**Emport minimum** (hors trajet) : **{emport_min_l:.1f} L** "
            f"— {format_duration(emport_min_total_min)} moteur [{unusable_fuel_l:.1f} L non utilisable déduits]"
        )
        st.divider()
        boarded_l = st.number_input(
            "Carburant embarqué (L)",
            min_value=0.0,
            max_value=500.0,
            value=0.0,
            step=0.5,
            key="boarded_fuel_l",
        )
        if boarded_l > 0:
            marge = boarded_l - emport_total_ref_l
            if marge < 0:
                st.error(
                    f"⚠️ Insuffisant : {boarded_l:.1f} L embarqués < {emport_total_ref_l:.1f} L requis "
                    f"({marge:.1f} L)"
                )
            else:
                extra_min = (marge / fuel_burn_lph) * 60.0 if fuel_burn_lph > 0 else 0.0
                st.success(
                    f"✓ Marge : +{marge:.1f} L — +{format_duration(extra_min)} d'autonomie supplémentaire"
                )

    # emport_total_l est calculé dans tabs[1] après build_route
    # On le récupère depuis session_state s'il existe
    _emport_total_ref = st.session_state.get("emport_total_l", emport_min_l)
    emport_carburant(emport_min_l, _emport_min_total_min, _emport_total_ref)

departure = resolve_airport(dep_icao)
if not departure:
    st.error("Aérodrome de départ introuvable.")
    st.stop()

weather_bundle = fetch_airport_weather_bundle(dep_icao)
metar_raw = weather_bundle["metar_raw"]
metar_decoded = weather_bundle["metar_decoded"]
taf_raw = weather_bundle["taf_raw"]

if not GEOMAG_AVAILABLE:
    st.warning("`pygeomag` n'est pas installé : le cap magnétique sera temporairement égal au cap vrai.")

with st.expander("Terrain de départ", expanded=True):
    c1, c2 = st.columns([1, 3])

    with c1:
        metric_card("OACI", departure.icao)

    with c2:
        metric_card("Nom", departure.name)

    st.markdown("### Météo")

    weather_block = f"""
    <div style="
        background-color: rgba(255,255,255,0.03);
        padding:14px;
        border-radius:10px;
        border:1px solid rgba(255,255,255,0.08);
        font-family: monospace;
        white-space: pre-wrap;
        line-height:1.4;
    ">
    <b>METAR</b>
    {metar_raw if metar_raw else "METAR indisponible."}
    
    <br><b>TAF</b>
    {taf_raw if taf_raw else "TAF indisponible."}
        </div>
        """

    st.markdown(weather_block, unsafe_allow_html=True)

with st.expander("Branches", expanded=True):
    st.caption("Ordre chronologique conservé. Ajout en bas pour garder un flux naturel départ → arrivée.")

    delete_idx = None

    for i, leg in enumerate(st.session_state.legs_data):
        st.markdown(f"### Branche {i + 1}")
        t1, t2 = st.columns([1, 1])

        with t1:
            leg["leg_type"] = st.selectbox(
                "Type",
                LEG_TYPES,
                index=LEG_TYPES.index(leg["leg_type"]),
                key=f"leg_type_{i}",
            )

            if leg["leg_type"] == "point_tournant":
                leg["route_true_deg"] = st.number_input(
                    "Route vraie (°)",
                    min_value=0,
                    max_value=359,
                    value=int(round(leg["route_true_deg"])) % 360,
                    step=1,
                    key=f"route_{i}",
                )
                leg["distance_nm"] = st.number_input(
                    "Distance (NM)",
                    min_value=0.1,
                    max_value=500.0,
                    value=float(leg["distance_nm"]),
                    step=1.0,
                    key=f"dist_{i}",
                )
                leg["label"] = st.text_input(
                    "Label",
                    value=leg["label"],
                    key=f"label_{i}",
                )
                st.caption(f"RV affichée : {route3(leg['route_true_deg'])}")
            else:
                leg["target_icao"] = st.text_input(
                    "OACI arrivée",
                    value=leg["target_icao"],
                    key=f"icao_{i}",
                ).strip().upper()

        with t2:
            leg["altitude_ft"] = st.number_input(
                "Altitude branche (ft)",
                min_value=500,
                max_value=18000,
                value=int(leg["altitude_ft"]),
                step=100,
                key=f"alt_{i}",
            )
            leg["end_type"] = st.selectbox(
                "Fin de branche",
                END_TYPES,
                index=END_TYPES.index(leg["end_type"]),
                key=f"end_{i}",
            )

            if st.button(f"🗑️ Supprimer branche {i + 1}", key=f"del_{i}", width="stretch"):
                delete_idx = i

        st.divider()

    if delete_idx is not None:
        st.session_state.legs_data.pop(delete_idx)
        if not st.session_state.legs_data:
            st.session_state.legs_data = default_legs()
        st.rerun()

    if st.button("➕ Ajouter une branche", width="stretch"):
        st.session_state.legs_data.append(
            {
                "leg_type": "point_tournant",
                "route_true_deg": 0.0,
                "distance_nm": 10.0,
                "altitude_ft": 3500.0,
                "end_type": "standard",
                "target_icao": "",
                "label": f"PT {len(st.session_state.legs_data) + 1}",
            }
        )
        st.rerun()

legs_in = []
for raw in st.session_state.legs_data:
    legs_in.append(
        LegInput(
            leg_type=raw["leg_type"],
            route_true_deg=float(raw["route_true_deg"]),
            distance_nm=float(raw["distance_nm"]),
            altitude_ft=float(raw["altitude_ft"]),
            end_type=raw["end_type"],
            target_icao=raw["target_icao"],
            label=raw["label"],
        )
    )

metar_sig = None
if metar_decoded:
    metar_sig = (
        metar_decoded.get("wind_dir"),
        metar_decoded.get("wind_speed_kt"),
        metar_decoded.get("obs_time"),
    )

route_key = (
    dep_icao,
    round(float(tas_kt), 1),
    metar_sig,
    legs_signature(st.session_state.legs_data),
)

if st.session_state.get("route_key") == route_key:
    legs = st.session_state["route_legs"]
    nav_points = st.session_state["route_nav_points"]
else:
    try:
        legs, nav_points = build_route(
            departure,
            legs_in,
            tas_kt,
            departure_metar_decoded=metar_decoded,
        )
    except ValueError as e:
        st.error(str(e))
        st.stop()

    st.session_state["route_key"] = route_key
    st.session_state["route_legs"] = legs
    st.session_state["route_nav_points"] = nav_points

selected_leg_idx = st.selectbox(
    "Branche sélectionnée",
    options=[leg.idx for leg in legs],
    format_func=lambda i: f"Branche {i}: {legs[i - 1].start_name} → {legs[i - 1].end_name}",
)

tabs = st.tabs(["Carte", "Navigation", "Profil vertical", "Météo"])

with tabs[0]:
    basemap_options = ["OpenAIP", "OpenStreetMap", "OpenTopoMap"]

    if st.session_state.basemap_choice not in basemap_options:
        st.session_state.basemap_choice = "OpenStreetMap"
    if st.session_state.basemap_selector not in basemap_options:
        st.session_state.basemap_selector = st.session_state.basemap_choice

    st.selectbox(
        "Fond de carte",
        basemap_options,
        index=basemap_options.index(st.session_state.basemap_choice),
        key="basemap_selector",
        on_change=sync_basemap_choice,
        width="stretch",
    )

    basemap = st.session_state.basemap_choice
    map_key = (st.session_state["route_key"], selected_leg_idx, basemap)
    if st.session_state.get("map_key") == map_key:
        fmap = st.session_state["map_cache"]
    else:
        fmap = build_map(nav_points, legs, selected_leg_idx, openaip_key, basemap)
        st.session_state["map_key"]   = map_key
        st.session_state["map_cache"] = fmap
    st_folium(fmap, width="stretch", height=560, key="main_map", returned_objects=[],)

    sel = legs[selected_leg_idx - 1]
    c1, c2 = st.columns(2)
    with c1:
        dm_txt = f"{abs(sel.declination_deg):.1f}°{'E' if sel.declination_deg >= 0 else 'W'}"
        metric_card("Branche", f"{sel.start_name} → {sel.end_name}")
        metric_card("RV", route3(sel.route_true_deg))
        metric_card("CD", f"{abs(sel.drift_deg):.1f}° ({correction_label(sel.drift_deg)})")
        metric_card("Cv", route3(sel.heading_true_deg))
    with c2:
        metric_card("Dm", dm_txt)
        metric_card("Cm", route3(sel.heading_mag_deg))
        metric_card("Vent", f"{route3(sel.wind_dir_deg)}/{sel.wind_speed_kt:.0f} kt ({sel.wind_source})")
        metric_card("Altitude", f"{int(round(sel.altitude_ft))} ft")

with tabs[1]:
    total_nm = sum(l.distance_nm for l in legs)
    trip_minutes = sum(l.ete_min for l in legs)

    usable_total_min = (
        trip_minutes
        + taxi_departure_min
        + diversion_min
        + diversion_arrival_min
        + final_reserve_min
    )
    usable_fuel_l = usable_total_min / 60.0 * fuel_burn_lph
    total_fuel_l  = usable_fuel_l + unusable_fuel_l

    emport_nav_l  = trip_minutes / 60.0 * fuel_burn_lph   # carburant branches seules
    emport_total_l = emport_nav_l + emport_min_l           # + forfaits/réserves
    st.session_state["emport_total_l"] = emport_total_l    # disponible pour le fragment devis

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Distance totale", f"{total_nm:.1f} NM")
    with c2:
        metric_card("Temps de route", format_duration(trip_minutes))
    with c3:
        metric_card("Emport navigation", f"{emport_nav_l:.1f} L")
    with c4:
        metric_card("Emport total", f"{emport_total_l:.1f} L")

    # Carburant de référence pour le log : embarqué si saisi, sinon emport total calculé
    boarded_l = st.session_state.get("boarded_fuel_l", 0.0)
    ref_fuel_l = boarded_l if boarded_l > 0 else emport_total_l

    st.markdown("### Log de navigation")
    fuel_remaining_l = ref_fuel_l
    elapsed_min = 0.0
    for leg in legs:
        fuel_leg = leg.ete_min / 60.0 * fuel_burn_lph
        fuel_remaining_l -= fuel_leg
        elapsed_min += leg.ete_min
        autonomy_min = (fuel_remaining_l / fuel_burn_lph * 60.0) if fuel_burn_lph > 0 else 0.0
        engine_stop_min = dep_time_min + elapsed_min + autonomy_min
        leg_card(leg, selected=(leg.idx == selected_leg_idx),
                 fuel_leg=fuel_leg, fuel_remaining_l=fuel_remaining_l,
                 engine_stop_min=engine_stop_min)

with tabs[2]:
    verticale_ft = 1500
    tdp_ft = 1000

    profile_key = (
        st.session_state["route_key"],
        climb_rate_fpm, climb_speed_kt, descent_rate_fpm,
        verticale_ft, tdp_ft,
    )
    if st.session_state.get("profile_key") == profile_key:
        profile = st.session_state["profile_cache"]
        elev_m  = st.session_state["profile_elev"]
    else:
        profile = build_vertical_profile(
            nav_points=nav_points,
            legs=legs,
            climb_rate_fpm=climb_rate_fpm,
            climb_speed_kt=climb_speed_kt,
            descent_rate_fpm=descent_rate_fpm,
            verticale_ft=verticale_ft,
            tdp_ft=tdp_ft,
        )
        elev_m = fetch_elevations(
            tuple(p[0] for p in profile["terrain_route_pts"]),
            tuple(p[1] for p in profile["terrain_route_pts"]),
        )
        st.session_state["profile_key"]   = profile_key
        st.session_state["profile_cache"] = profile
        st.session_state["profile_elev"]  = elev_m
        
    if elev_m is None:
        terrain_ft = [0] * len(profile["terrain_route_pts"])
        st.warning("Relief indisponible en ligne, profil affiché sans terrain.")
    else:
        terrain_ft = [int(round(m_to_ft(x))) for x in elev_m]

    x_terrain = [round(x, 1) for x in profile["terrain_x_nm"]]
    x_air = profile["aircraft_x_nm"]
    y_air = profile["aircraft_alt_ft"]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=x_terrain,
        y=terrain_ft,
        mode="lines",
        name="Sol",
        fill="tozeroy",
        line=dict(color="#8B5A2B", width=2),
        fillcolor="rgba(139, 90, 43, 0.45)",
        hovertemplate="Dist %{x:.1f} NM<br>Sol %{y:.0f} ft<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=x_air,
        y=y_air,
        mode="lines",
        name="Avion",
        line=dict(width=3),
        connectgaps=False,
        hovertemplate="Dist %{x:.1f} NM<br>Avion %{y:.0f} ft<extra></extra>",
    ))

    for x, t_txt in profile["toc_marks"]:
        fig.add_annotation(
            x=round(x, 1),
            y=max(y for y in y_air if y is not None),
            text=f"TOC {t_txt}",
            showarrow=False,
            yshift=10,
            font=dict(color="green"),
        )

    for x, t_txt in profile["tod_marks"]:
        fig.add_annotation(
            x=round(x, 1),
            y=max(y for y in y_air if y is not None),
            text=f"TOD {t_txt}",
            showarrow=False,
            yshift=10,
            font=dict(color="purple"),
        )
    # VT / TDP : marqueurs à la distance exacte du terrain, bornés entre sol et altitude d'intégration
    for x, y0, y1, terrain_name in profile["vt_marks"]:
        fig.add_shape(
            type="line",
            x0=round(x, 1),
            x1=round(x, 1),
            y0=round(y0),
            y1=round(y1),
            line=dict(color="orange", width=2, dash="dot"),
        )
        fig.add_annotation(
            x=round(x, 1),
            y=round(y1),
            text=f"VT<br>{terrain_name} {round(y0):.0f} ft",
            showarrow=False,
            yshift=10,
            font=dict(color="orange"),
            align="center",
        )

    for x, y0, y1, terrain_name in profile["tdp_marks"]:
        fig.add_shape(
            type="line",
            x0=round(x, 1),
            x1=round(x, 1),
            y0=round(y0),
            y1=round(y1),
            line=dict(color="deepskyblue", width=2, dash="dot"),
        )
        fig.add_annotation(
            x=round(x, 1),
            y=round(y1),
            text=f"TDP<br>{terrain_name} {round(y0):.0f} ft",
            showarrow=False,
            yshift=10,
            font=dict(color="deepskyblue"),
            align="center",
        )

    fig.update_layout(
        height=430,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Distance cumulée (NM)",
        yaxis_title="Altitude (ft)",
        legend_orientation="h",
    )

    st.plotly_chart(
        fig,
        width="stretch",
        config={
            "displayModeBar": False,
            "scrollZoom": False,
            "doubleClick": False,
            "staticPlot": True,
        },
    )

    if x_terrain:
        c1, c2, c3 = st.columns(3)
        with c1:
            metric_card("Distance totale", f"{x_terrain[-1]:.1f} NM")
        with c2:
            metric_card("Alt min avion", f"{min(y for y in y_air if y is not None):.0f} ft")
        with c3:
            metric_card("Alt max avion", f"{max(y for y in y_air if y is not None):.0f} ft")

    if terrain_ft and len(x_terrain) == len(terrain_ft):
        air_pairs = [(x, y) for x, y in zip(x_air, y_air) if x is not None and y is not None]
        terrain_map = {round(x, 1): t for x, t in zip(x_terrain, terrain_ft)}

        margins = []
        for x, y in air_pairs:
            key = round(x, 1)
            if key in terrain_map:
                margins.append(y - terrain_map[key])

        if margins:
            min_margin = min(margins)
            if min_margin < 500:
                st.error(f"Marge verticale minimale faible : {min_margin:.0f} ft")
            else:
                st.success(f"Marge verticale minimale : {min_margin:.0f} ft")

with tabs[3]:
    st.subheader(f"Départ {departure.icao}")
    st.markdown(f"**{departure.name}**")

    st.markdown("**METAR**")
    if metar_raw:
        st.code(metar_raw, language="text")
    else:
        st.warning("METAR indisponible.")

    st.markdown("**TAF**")
    if taf_raw:
        st.code(taf_raw, language="text")
    else:
        st.warning("TAF indisponible.")

    st.markdown("### Vent par branche")
    hour_txt = generation_hour_utc().strftime("%Y-%m-%d %H:%M UTC")
    for leg in legs:
        st.markdown(
            f"**Vent branche {leg.idx}** : {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f} kt "
            f"({leg.wind_source}) — {hour_txt}"
        )
