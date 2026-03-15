import html
import logging
import math
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict

import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import METAR_API_URL, TAF_API_URL, OPENMETEO_DWD, METEOFRANCE_AROME, UA
from models import WeatherBundle, LegResult
from core.geo import (
    interpolate_line,
    uv_from_wind_from,
    wind_from_uv,
    metar_surface_wind,
)
from core.formatting import wind_to_deg

LOGGER = logging.getLogger(__name__)

def session():
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update(UA)

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_json(url: str, params: Optional[dict] = None, timeout: int = 20):
    r = session().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

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
    except requests.RequestException as exc:
        LOGGER.warning("Erreur réseau METAR pour %s: %s", icao, exc)
        return None, None
    except Exception:
        LOGGER.exception("Erreur inattendue METAR pour %s", icao)
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
    except requests.RequestException as exc:
        LOGGER.warning("Erreur réseau TAF pour %s: %s", icao, exc)
        return None
    except Exception:
        LOGGER.exception("Erreur inattendue TAF pour %s", icao)
        return None


@st.cache_data(ttl=60 * 10, show_spinner=False)
def fetch_airport_weather_bundle(icao: str) -> WeatherBundle:
    metar_raw, metar_decoded = fetch_metar(icao)
    taf_raw = fetch_taf(icao)
    return WeatherBundle(
        metar_raw=metar_raw,
        metar_decoded=metar_decoded,
        taf_raw=taf_raw,
    )

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


def generation_hour_utc(reference_dt: Optional[datetime] = None) -> datetime:
    return nearest_hour(reference_dt or datetime.now(timezone.utc))

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

def build_time_index(hourly_time: List[str]) -> Dict[str, int]:
    return {t: i for i, t in enumerate(hourly_time)}


def get_hour_index(hourly_time: List[str], target_key: str) -> Optional[int]:
    return build_time_index(hourly_time).get(target_key)


def build_hour_indices(items: List[dict]) -> List[Dict[str, int]]:
    return [build_time_index(item.get("hourly", {}).get("time", [])) for item in items]

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
    # n = nombre de segments dans interpolate_line
    if distance_nm <= 15:
        return 1
    if distance_nm <= 50:
        return 2
    if distance_nm <= 120:
        return 3
    return 4

def mean_branch_pressure_wind(
    items: List[dict],
    point_indices: List[int],
    hour_key: str,
    altitude_ft: float,
    level_map: Dict[int, float],
    hour_indices: Optional[List[Dict[str, int]]] = None,
) -> Optional[Tuple[float, float]]:
    pairs = []
    for idx in point_indices:
        item = items[idx]
        hour_map = hour_indices[idx] if hour_indices else build_time_index(item.get("hourly", {}).get("time", []))
        hour_idx = hour_map.get(hour_key)
        pair = interpolate_pressure_wind_for_item(item, hour_idx, altitude_ft, level_map)
        if pair:
            pairs.append(pair)
    return mean_vector_from_pairs(pairs)


def mean_branch_surface_wind(
    items: List[dict],
    point_indices: List[int],
    hour_key: str,
    hour_indices: Optional[List[Dict[str, int]]] = None,
) -> Optional[Tuple[float, float]]:
    pairs = []
    for idx in point_indices:
        item = items[idx]
        hour_map = hour_indices[idx] if hour_indices else build_time_index(item.get("hourly", {}).get("time", []))
        hour_idx = hour_map.get(hour_key)
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

    wind_by_leg: Dict[int, Tuple[str, float, float]] = {}

    icon_pressure_vars = union_pressure_vars(altitudes_ft, DWD_LEVELS_M)
    icon_pressure_items = None
    icon_pressure_indices = None
    if icon_pressure_vars:
        try:
            icon_pressure_items = fetch_openmeteo_hour_block("ICON-D2", lats, lons, icon_pressure_vars)
            icon_pressure_indices = build_hour_indices(icon_pressure_items)
        except requests.RequestException as exc:
            LOGGER.warning("Erreur réseau vent niveau ICON-D2: %s", exc)
            icon_pressure_items = None
        except Exception:
            LOGGER.exception("Erreur inattendue vent niveau ICON-D2")
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
                hour_indices=icon_pressure_indices,
            )
            if avg:
                wind_by_leg[geom["idx"]] = ("ICON-D2 niveau", avg[0], avg[1])

    surface_vars = ("wind_speed_10m", "wind_direction_10m")
    mf_surface_items = None
    mf_surface_indices = None
    missing_leg_ids = [geom["idx"] for geom in geometries if geom["idx"] not in wind_by_leg]
    if missing_leg_ids:
        try:
            mf_surface_items = fetch_openmeteo_hour_block("MF", lats, lons, surface_vars)
            mf_surface_indices = build_hour_indices(mf_surface_items)
        except requests.RequestException as exc:
            LOGGER.warning("Erreur réseau vent surface Météo-France: %s", exc)
            mf_surface_items = None
        except Exception:
            LOGGER.exception("Erreur inattendue vent surface Météo-France")
            mf_surface_items = None

    if mf_surface_items:
        for geom in geometries:
            if geom["idx"] in wind_by_leg:
                continue
            point_indices = branch_point_indices[geom["idx"]]
            avg = mean_branch_surface_wind(
                mf_surface_items,
                point_indices,
                hour_key,
                hour_indices=mf_surface_indices,
            )
            if avg:
                wind_by_leg[geom["idx"]] = ("Météo-France surface", avg[0], avg[1])

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
