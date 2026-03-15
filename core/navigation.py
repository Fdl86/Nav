import math
import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict

import requests

from models import (
    Aerodrome, LegInput, LegResult, NavPoint,
    DWD_LEVELS_M, _DWD_LEVELS_SORTED, _MF_LEVELS_SORTED,
)
from core.formatting import deg_norm, ft_to_m, m_to_nm
from core.geo import (
    haversine_nm, initial_bearing_deg, destination_point,
    interpolate_line,
)

LOGGER = logging.getLogger(__name__)


# ── Vent vectoriel ────────────────────────────────────────────────────────────

def uv_from_wind_from(speed_kt: float, direction_from_deg: float) -> Tuple[float, float]:
    rad = math.radians(direction_from_deg)
    return -speed_kt * math.sin(rad), -speed_kt * math.cos(rad)


def wind_from_uv(u: float, v: float) -> Tuple[float, float]:
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


def wind_correction(
    course_deg: float, tas_kt: float, wind_from_deg: float, wind_speed_kt: float
) -> Tuple[float, float, float]:
    from core.formatting import shortest_angle_deg
    delta = math.radians(shortest_angle_deg(wind_from_deg, course_deg))
    ratio = 0.0 if tas_kt <= 0 else max(
        -0.9999, min(0.9999, (wind_speed_kt / tas_kt) * math.sin(delta))
    )
    wca = math.asin(ratio)
    gs = tas_kt * math.cos(wca) - wind_speed_kt * math.cos(delta)
    gs = max(gs, 20.0)
    drift = math.degrees(wca)
    heading = deg_norm(course_deg + drift)
    return drift, heading, gs


def mean_vector_from_pairs(
    pairs: List[Tuple[float, float]]
) -> Optional[Tuple[float, float]]:
    if not pairs:
        return None
    u_sum = v_sum = 0.0
    for wd, ws in pairs:
        u, v = uv_from_wind_from(ws, wd)
        u_sum += u
        v_sum += v
    return wind_from_uv(u_sum / len(pairs), v_sum / len(pairs))


# ── Indexation temporelle ─────────────────────────────────────────────────────

def build_time_index(hourly_time: List[str]) -> Dict[str, int]:
    return {t: i for i, t in enumerate(hourly_time)}


def get_hour_index(
    hourly_time: List[str],
    target_key: str,
    time_index: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    if time_index is not None:
        return time_index.get(target_key)
    for i, t in enumerate(hourly_time):
        if t == target_key:
            return i
    return None


def build_hour_indices(items: List[dict]) -> List[Dict[str, int]]:
    return [
        build_time_index(item.get("hourly", {}).get("time", []))
        for item in items
    ]


# ── Niveaux de pression ───────────────────────────────────────────────────────

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


def union_pressure_vars(
    altitudes_ft: List[float], level_map: Dict[int, float]
) -> Tuple[str, ...]:
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


def interpolate_pressure_wind_for_item(
    item: dict, hour_idx: int, target_alt_ft: float, level_map: Dict[int, float]
) -> Optional[Tuple[float, float]]:
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
    z_low   = at(f"geopotential_height_{p_low}hPa")
    if spd_low is None or dir_low is None or z_low is None:
        return None
    if p_low == p_high:
        return float(dir_low), float(spd_low)

    spd_high = at(f"wind_speed_{p_high}hPa")
    dir_high = at(f"wind_direction_{p_high}hPa")
    z_high   = at(f"geopotential_height_{p_high}hPa")
    if spd_high is None or dir_high is None or z_high is None:
        return None

    z1 = float(z_low)
    z2 = float(z_high)
    t = 0.0 if abs(z2 - z1) < 1e-6 else max(0.0, min(1.0, (target_alt_m - z1) / (z2 - z1)))
    u1, v1 = uv_from_wind_from(float(spd_low), float(dir_low))
    u2, v2 = uv_from_wind_from(float(spd_high), float(dir_high))
    return wind_from_uv(u1 + (u2 - u1) * t, v1 + (v2 - v1) * t)


def extract_surface_wind_for_item(
    item: dict, hour_idx: int
) -> Optional[Tuple[float, float]]:
    hourly = item.get("hourly", {})
    spd_arr = hourly.get("wind_speed_10m", [])
    dir_arr = hourly.get("wind_direction_10m", [])
    if hour_idx is None or hour_idx < 0:
        return None
    if hour_idx >= len(spd_arr) or hour_idx >= len(dir_arr):
        return None
    spd  = spd_arr[hour_idx]
    wdir = dir_arr[hour_idx]
    if spd is None or wdir is None:
        return None
    return float(wdir), float(spd)


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
        hourly_time = item.get("hourly", {}).get("time", [])
        hour_map = hour_indices[idx] if hour_indices else None
        hour_idx = get_hour_index(hourly_time, hour_key, time_index=hour_map)
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
        hour_map = hour_indices[idx] if hour_indices else build_time_index(
            item.get("hourly", {}).get("time", [])
        )
        hour_idx = hour_map.get(hour_key)
        pair = extract_surface_wind_for_item(item, hour_idx)
        if pair:
            pairs.append(pair)
    return mean_vector_from_pairs(pairs)


def sample_point_count(distance_nm: float) -> int:
    if distance_nm <= 15:
        return 1
    if distance_nm <= 50:
        return 2
    if distance_nm <= 120:
        return 3
    return 4


def true_to_magnetic(true_deg: float, declination_deg: float) -> float:
    return deg_norm(true_deg - declination_deg)


def nearest_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def generation_hour_utc(reference_dt: Optional[datetime] = None) -> datetime:
    return nearest_hour(reference_dt or datetime.now(timezone.utc))


# ── Construction de route ─────────────────────────────────────────────────────

def build_route(
    departure: Aerodrome,
    legs_in: List[LegInput],
    tas_kt: float,
    departure_metar_decoded: Optional[dict] = None,
) -> Tuple[List[LegResult], List[NavPoint]]:
    from services.airports import resolve_airport
    from services.weather import prefetch_winds_for_geometries
    from services.magnetic import magnetic_declination_deg

    geometries: List[dict] = []
    nav_points: List[NavPoint] = [
        NavPoint(departure.icao, departure.lat, departure.lon, departure.elev_ft, departure.icao)
    ]
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
            prev.lat, prev.lon, end_pt.lat, end_pt.lon,
            n=sample_point_count(distance_nm),
        )
        geometries.append({
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
        })
        nav_points.append(end_pt)
        prev = end_pt

    wind_by_leg = prefetch_winds_for_geometries(geometries, departure_metar_decoded)

    legs_out: List[LegResult] = []
    for geom in geometries:
        wind_source, wind_dir, wind_speed = wind_by_leg.get(
            geom["idx"], ("Aucune donnée vent", 0.0, 0.0)
        )
        drift, heading_true, gs = wind_correction(
            geom["route_true_deg"], tas_kt, wind_dir, wind_speed
        )
        ete_min = geom["distance_nm"] / gs * 60.0
        decl = magnetic_declination_deg(geom["mid_lat"], geom["mid_lon"], geom["altitude_ft"])
        route_mag   = true_to_magnetic(geom["route_true_deg"], decl)
        heading_mag = true_to_magnetic(heading_true, decl)

        legs_out.append(LegResult(
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
        ))

    return legs_out, nav_points
