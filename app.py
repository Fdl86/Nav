# app.py
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict

import folium
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_folium import st_folium


APP_TITLE = "Prépa VFR Mobile"
UA = {"User-Agent": "vfr-prep-mobile/1.3"}

AIRPORTS_CSV_URL = "https://ourairports.com/data/airports.csv"
AIRPORTS_FALLBACK_CSV_URL = "https://raw.githubusercontent.com/datasets/airport-codes/main/data/airport-codes.csv"
METAR_API_URL = "https://aviationweather.gov/api/data/metar"
OPENMETEO_DWD = "https://api.open-meteo.com/v1/dwd-icon"
OPENMETEO_MF = "https://api.open-meteo.com/v1/meteofrance"
OPENMETEO_ELEV = "https://api.open-meteo.com/v1/elevation"

END_TYPES = ["standard", "verticale", "tour_de_piste"]
LEG_TYPES = ["point_tournant", "aerodrome"]

# Approximation des hauteurs AMSL pour interpolation verticale
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
    eta_utc: datetime
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


def fetch_csv(url: str, timeout: int = 30) -> pd.DataFrame:
    r = session().get(url, timeout=timeout)
    r.raise_for_status()
    return pd.read_csv(pd.io.common.StringIO(r.text), low_memory=False)


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
    # Schéma différent du repo datasets/airport-codes
    cols = {
        "ident": "ident",
        "name": "name",
        "latitude_deg": "latitude_deg",
        "longitude_deg": "longitude_deg",
        "elevation_ft": "elevation_ft",
        "type": "type",
        "iso_country": "iso_country",
    }
    existing = [c for c in cols if c in df.columns]
    df = df[existing].copy()
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[list(cols.keys())]
    df["ident"] = df["ident"].astype(str).str.upper()
    df["name"] = df["name"].fillna("").astype(str)
    df["latitude_deg"] = pd.to_numeric(df["latitude_deg"], errors="coerce")
    df["longitude_deg"] = pd.to_numeric(df["longitude_deg"], errors="coerce")
    df["elevation_ft"] = pd.to_numeric(df["elevation_ft"], errors="coerce").fillna(0)
    df = df.dropna(subset=["latitude_deg", "longitude_deg"])
    return df


def resolve_airport(icao: str) -> Optional[Aerodrome]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    # Source principale
    try:
        df = load_airports_primary()
        hit = df[df["ident"] == icao]
        if not hit.empty:
            row = hit.iloc[0]
            return Aerodrome(
                icao=icao,
                name=str(row["name"]),
                lat=float(row["latitude_deg"]),
                lon=float(row["longitude_deg"]),
                elev_ft=float(row["elevation_ft"]),
            )
    except Exception:
        pass

    # Fallback
    try:
        df = load_airports_fallback()
        hit = df[df["ident"] == icao]
        if not hit.empty:
            row = hit.iloc[0]
            return Aerodrome(
                icao=icao,
                name=str(row["name"]),
                lat=float(row["latitude_deg"]),
                lon=float(row["longitude_deg"]),
                elev_ft=float(row["elevation_ft"]),
            )
    except Exception:
        pass

    return None


@st.cache_data(ttl=60 * 5, show_spinner=False)
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
            "flight_category": m.get("fltCat"),
            "wind_dir": m.get("wdir"),
            "wind_speed_kt": m.get("wspd"),
            "visibility": m.get("visib"),
            "temp_c": m.get("temp"),
            "dewpoint_c": m.get("dewp"),
            "qnh_hpa": m.get("altim"),
            "clouds": m.get("clouds", []),
        }
        return raw, decoded
    except Exception:
        return None, None


def metar_human(decoded: Optional[dict]) -> str:
    if not decoded:
        return "Pas de METAR disponible"

    parts = []
    wd = decoded.get("wind_dir")
    ws = decoded.get("wind_speed_kt")
    vis = decoded.get("visibility")
    temp = decoded.get("temp_c")
    dew = decoded.get("dewpoint_c")
    qnh = decoded.get("qnh_hpa")
    flt = decoded.get("flight_category")
    clouds = decoded.get("clouds") or []

    if wd is not None and ws is not None:
        if str(wd).upper() == "VRB":
            parts.append(f"Vent VRB/{float(ws):.0f} kt")
        else:
            parts.append(f"Vent {float(wd):03.0f}/{float(ws):.0f} kt")
    if vis is not None:
        parts.append(f"Vis {vis}")
    if temp is not None and dew is not None:
        parts.append(f"T {temp}°C / Td {dew}°C")
    if qnh is not None:
        parts.append(f"QNH {qnh}")
    if flt:
        parts.append(f"Cat {flt}")

    cloud_bits = []
    for c in clouds[:3]:
        cover = c.get("cover", "?")
        base = c.get("base", "?")
        cloud_bits.append(f"{cover} {base} ft")
    if cloud_bits:
        parts.append("Nuages " + ", ".join(cloud_bits))

    return " • ".join(parts) if parts else "METAR disponible"


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
    return deg_norm(math.degrees(math.atan2(x, y)))


def destination_point(lat_deg: float, lon_deg: float, bearing_deg: float, distance_nm: float):
    R = 6371000.0
    d = nm_to_m(distance_nm) / R
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


def pick_levels(target_alt_m: float, level_map: Dict[int, float]):
    levels = sorted(level_map.items(), key=lambda x: x[1])
    if target_alt_m <= levels[0][1]:
        return levels[0][0], levels[0][0]
    if target_alt_m >= levels[-1][1]:
        return levels[-1][0], levels[-1][0]
    for (p1, h1), (p2, h2) in zip(levels, levels[1:]):
        if h1 <= target_alt_m <= h2:
            return p1, p2
    return levels[-1][0], levels[-1][0]


def nearest_hour(dt: datetime):
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


@st.cache_data(ttl=60 * 15, show_spinner=False)
def fetch_wind_pressure_batch(
    source: str,
    lats: Tuple[float, ...],
    lons: Tuple[float, ...],
    valid_hour_iso: str,
    target_alt_ft: float,
):
    target_alt_m = ft_to_m(target_alt_ft)
    valid_dt = datetime.fromisoformat(valid_hour_iso)

    if source == "ICON-D2":
        url = OPENMETEO_DWD
        level_map = DWD_LEVELS_M
    else:
        url = OPENMETEO_MF
        level_map = MF_LEVELS_M

    p_low, p_high = pick_levels(target_alt_m, level_map)

    hourly = [
        f"wind_speed_{p_low}hPa",
        f"wind_direction_{p_low}hPa",
        f"geopotential_height_{p_low}hPa",
    ]
    if p_high != p_low:
        hourly += [
            f"wind_speed_{p_high}hPa",
            f"wind_direction_{p_high}hPa",
            f"geopotential_height_{p_high}hPa",
        ]

    params = {
        "latitude": ",".join(f"{x:.6f}" for x in lats),
        "longitude": ",".join(f"{x:.6f}" for x in lons),
        "hourly": ",".join(hourly),
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "start_hour": valid_dt.strftime("%Y-%m-%dT%H:%M"),
        "end_hour": (valid_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "cell_selection": "nearest",
    }

    try:
        js = fetch_json(url, params=params, timeout=25)
    except Exception:
        return None

    items = js if isinstance(js, list) else [js]
    out = []

    for item in items:
        h = item.get("hourly", {})

        def first(name):
            arr = h.get(name, [])
            return arr[0] if arr else None

        spd_low = first(f"wind_speed_{p_low}hPa")
        dir_low = first(f"wind_direction_{p_low}hPa")
        z_low = first(f"geopotential_height_{p_low}hPa")

        if spd_low is None or dir_low is None or z_low is None:
            return None

        if p_low == p_high:
            out.append({"wind_dir_deg": float(dir_low), "wind_speed_kt": float(spd_low)})
            continue

        spd_high = first(f"wind_speed_{p_high}hPa")
        dir_high = first(f"wind_direction_{p_high}hPa")
        z_high = first(f"geopotential_height_{p_high}hPa")
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
        out.append({"wind_dir_deg": wd, "wind_speed_kt": ws})

    return out


@st.cache_data(ttl=60 * 15, show_spinner=False)
def fetch_wind_surface_batch(
    source: str,
    lats: Tuple[float, ...],
    lons: Tuple[float, ...],
    valid_hour_iso: str,
):
    valid_dt = datetime.fromisoformat(valid_hour_iso)
    url = OPENMETEO_DWD if source == "ICON-D2" else OPENMETEO_MF
    params = {
        "latitude": ",".join(f"{x:.6f}" for x in lats),
        "longitude": ",".join(f"{x:.6f}" for x in lons),
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "start_hour": valid_dt.strftime("%Y-%m-%dT%H:%M"),
        "end_hour": (valid_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "cell_selection": "nearest",
    }
    try:
        js = fetch_json(url, params=params, timeout=25)
    except Exception:
        return None

    items = js if isinstance(js, list) else [js]
    out = []
    for item in items:
        h = item.get("hourly", {})
        spd = (h.get("wind_speed_10m") or [None])[0]
        wdir = (h.get("wind_direction_10m") or [None])[0]
        if spd is None or wdir is None:
            return None
        out.append({"wind_dir_deg": float(wdir), "wind_speed_kt": float(spd)})
    return out


def mean_vector_wind(batch: List[dict]) -> Tuple[float, float]:
    u_sum, v_sum = 0.0, 0.0
    for b in batch:
        u, v = uv_from_wind_from(b["wind_speed_kt"], b["wind_dir_deg"])
        u_sum += u
        v_sum += v
    u_avg = u_sum / len(batch)
    v_avg = v_sum / len(batch)
    return wind_from_uv(u_avg, v_avg)


def leg_mean_wind(points: List[Tuple[float, float]], valid_dt: datetime, altitude_ft: float):
    lats = tuple(p[0] for p in points)
    lons = tuple(p[1] for p in points)
    valid_hour = nearest_hour(valid_dt).replace(tzinfo=None).isoformat(timespec="minutes")

    # 1) Essai pression/niveau ICON
    batch = fetch_wind_pressure_batch("ICON-D2", lats, lons, valid_hour, altitude_ft)
    if batch:
        wd, ws = mean_vector_wind(batch)
        return "ICON-D2 niveau", wd, ws

    # 2) Essai pression/niveau AROME
    batch = fetch_wind_pressure_batch("AROME", lats, lons, valid_hour, altitude_ft)
    if batch:
        wd, ws = mean_vector_wind(batch)
        return "AROME niveau", wd, ws

    # 3) Fallback surface ICON
    batch = fetch_wind_surface_batch("ICON-D2", lats, lons, valid_hour)
    if batch:
        wd, ws = mean_vector_wind(batch)
        return "ICON-D2 10m", wd, ws

    # 4) Fallback surface AROME
    batch = fetch_wind_surface_batch("AROME", lats, lons, valid_hour)
    if batch:
        wd, ws = mean_vector_wind(batch)
        return "AROME 10m", wd, ws

    return "Aucune donnée vent", 0.0, 0.0


def wind_correction(course_deg: float, tas_kt: float, wind_from_deg: float, wind_speed_kt: float):
    delta = math.radians(shortest_angle_deg(wind_from_deg, course_deg))
    ratio = 0.0 if tas_kt <= 0 else max(-0.9999, min(0.9999, (wind_speed_kt / tas_kt) * math.sin(delta)))
    wca = math.asin(ratio)
    gs = tas_kt * math.cos(wca) - wind_speed_kt * math.cos(delta)
    gs = max(gs, 20.0)
    drift = math.degrees(wca)
    heading = deg_norm(course_deg + drift)
    return drift, heading, gs


def build_route(
    departure: Aerodrome,
    legs_in: List[LegInput],
    offblock_utc: datetime,
    tas_kt: float,
) -> Tuple[List[LegResult], List[NavPoint]]:
    legs_out: List[LegResult] = []
    nav_points: List[NavPoint] = [NavPoint(departure.icao, departure.lat, departure.lon, departure.elev_ft, departure.icao)]

    prev = nav_points[0]
    elapsed_min = 0.0

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

        est_mid = offblock_utc + timedelta(minutes=elapsed_min + (distance_nm / max(tas_kt, 40.0) * 30.0))
        sample = interpolate_line(prev.lat, prev.lon, end_pt.lat, end_pt.lon, n=4)
        wind_source, wind_dir, wind_speed = leg_mean_wind(sample, est_mid, leg.altitude_ft)
        drift, heading, gs = wind_correction(route_true, tas_kt, wind_dir, wind_speed)
        ete_min = distance_nm / gs * 60.0
        eta_utc = offblock_utc + timedelta(minutes=elapsed_min + ete_min)

        legs_out.append(
            LegResult(
                idx=idx,
                leg_type=leg.leg_type,
                start_name=prev.name,
                end_name=end_pt.name,
                start_lat=prev.lat,
                start_lon=prev.lon,
                end_lat=end_pt.lat,
                end_lon=end_pt.lon,
                distance_nm=distance_nm,
                route_true_deg=route_true,
                altitude_ft=leg.altitude_ft,
                tas_kt=tas_kt,
                wind_source=wind_source,
                wind_dir_deg=wind_dir,
                wind_speed_kt=wind_speed,
                drift_deg=drift,
                heading_true_deg=heading,
                gs_kt=gs,
                ete_min=ete_min,
                eta_utc=eta_utc,
                end_type=leg.end_type,
                arrival_elev_ft=end_pt.elev_ft,
            )
        )

        nav_points.append(end_pt)
        prev = end_pt
        elapsed_min += ete_min

    return legs_out, nav_points


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def fetch_elevations(lats: Tuple[float, ...], lons: Tuple[float, ...]):
    try:
        js = fetch_json(
            OPENMETEO_ELEV,
            params={
                "latitude": ",".join(f"{x:.6f}" for x in lats),
                "longitude": ",".join(f"{x:.6f}" for x in lons),
            },
            timeout=25,
        )
        vals = js.get("elevation", [])
        return vals if vals else None
    except Exception:
        return None


def build_profile_points(nav_points: List[NavPoint], n_per_leg: int = 18):
    pts = [(nav_points[0].lat, nav_points[0].lon)]
    for a, b in zip(nav_points, nav_points[1:]):
        seg = interpolate_line(a.lat, a.lon, b.lat, b.lon, n=n_per_leg)
        pts.extend(seg[1:])
    return pts


def cumulative_distances_nm(points: List[Tuple[float, float]]):
    d = [0.0]
    for i in range(1, len(points)):
        d.append(d[-1] + haversine_nm(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]))
    return d


def arrival_target_alt_ft(arr_elev_ft: float, end_type: str, verticale_ft: float, tdp_ft: float):
    if end_type == "verticale":
        return arr_elev_ft + verticale_ft
    if end_type == "tour_de_piste":
        return arr_elev_ft + tdp_ft
    return arr_elev_ft + 300.0


def build_vertical_profile(
    dep_elev_ft: float,
    arr_elev_ft: float,
    end_type: str,
    cruise_alt_ft: float,
    route_d_nm: List[float],
    climb_rate_fpm: float,
    climb_speed_kt: float,
    descent_rate_fpm: float,
    descent_speed_kt: float,
    verticale_ft: float,
    tdp_ft: float,
):
    arr_target = arrival_target_alt_ft(arr_elev_ft, end_type, verticale_ft, tdp_ft)
    climb_ft = max(0.0, cruise_alt_ft - dep_elev_ft)
    descent_ft = max(0.0, cruise_alt_ft - arr_target)

    climb_time_min = 0.0 if climb_rate_fpm <= 0 else climb_ft / climb_rate_fpm
    descent_time_min = 0.0 if descent_rate_fpm <= 0 else descent_ft / descent_rate_fpm

    toc_nm = climb_speed_kt * (climb_time_min / 60.0)
    tod_nm = max(0.0, route_d_nm[-1] - descent_speed_kt * (descent_time_min / 60.0))

    if toc_nm > tod_nm:
        mid = route_d_nm[-1] / 2.0
        toc_nm = min(toc_nm, mid)
        tod_nm = max(tod_nm, mid)

    alt_profile = []
    for d in route_d_nm:
        if d <= toc_nm and toc_nm > 0:
            alt = dep_elev_ft + (cruise_alt_ft - dep_elev_ft) * (d / toc_nm)
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


def openaip_tiles(api_key: str):
    return f"https://api.tiles.openaip.net/api/data/openaip/{{z}}/{{x}}/{{y}}.png?apiKey={api_key}"


def build_map(nav_points: List[NavPoint], legs: List[LegResult], selected_idx: int, openaip_key: str):
    all_pts = [(p.lat, p.lon) for p in nav_points]
    center = [sum(x[0] for x in all_pts) / len(all_pts), sum(x[1] for x in all_pts) / len(all_pts)]

    m = folium.Map(location=center, zoom_start=8, control_scale=True, tiles=None)

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
                icon=folium.DivIcon(html="""
                    <div style="font-size:14px;font-weight:700;color:#f59e0b;background:white;border:1px solid #f59e0b;border-radius:999px;padding:2px 6px;">
                        V
                    </div>
                """)
            ).add_to(m)
        elif leg.end_type == "tour_de_piste":
            folium.Marker(
                [leg.end_lat, leg.end_lon],
                icon=folium.DivIcon(html="""
                    <div style="font-size:12px;font-weight:700;color:#2563eb;background:white;border:1px solid #2563eb;border-radius:999px;padding:2px 6px;">
                        TDP
                    </div>
                """)
            ).add_to(m)

    min_lat = min(p[0] for p in all_pts)
    max_lat = max(p[0] for p in all_pts)
    min_lon = min(p[1] for p in all_pts)
    max_lon = max(p[1] for p in all_pts)
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=(18, 18))
    folium.LayerControl(collapsed=True).add_to(m)
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


def leg_card(leg: LegResult, selected: bool = False):
    border = "#ef4444" if selected else "rgba(128,128,128,0.22)"
    bg = "rgba(239,68,68,0.05)" if selected else "rgba(255,255,255,0.03)"
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
            <div style="font-size:0.95rem;line-height:1.65;">
                RM {route3(leg.route_true_deg)} • HDG {route3(leg.heading_true_deg)} • Dérive {leg.drift_deg:+.1f}°<br>
                Dist {leg.distance_nm:.1f} NM • Alt {int(leg.altitude_ft)} ft • GS {leg.gs_kt:.0f} kt<br>
                Vent {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f} kt ({leg.wind_source})<br>
                ETE {leg.ete_min:.1f} min • ETA {leg.eta_utc.strftime("%H:%M")} UTC • Fin {leg.end_type.replace("_", " ")}
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


def ensure_state():
    if "legs_data" not in st.session_state:
        st.session_state.legs_data = default_legs()


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
st.caption("Départ OACI, METAR, branches simples, carte openAIP, profil vertical.")

openaip_key = st.secrets.get("OPENAIP_KEY", "")

with st.expander("Vol", expanded=True):
    c1, c2, c3 = st.columns(3)

    with c1:
        dep_icao = st.text_input("Départ OACI", value="LFMT").strip().upper()
        off_str = st.text_input(
            "Heure OFF UTC (YYYY-MM-DD HH:MM)",
            value=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        )

    with c2:
        tas_kt = st.number_input("TAS (kt)", min_value=40, max_value=220, value=105, step=1)
        fuel_burn_lph = st.number_input("Conso (L/h)", min_value=1.0, max_value=200.0, value=28.0, step=0.5)
        reserve_min = st.number_input("Réserve (min)", min_value=0, max_value=180, value=45, step=5)

    with c3:
        cruise_alt_ft = st.number_input("Altitude croisière (ft)", min_value=500, max_value=18000, value=3500, step=100)
        climb_rate_fpm = st.number_input("Taux montée (ft/min)", min_value=100, max_value=3000, value=500, step=50)
        climb_speed_kt = st.number_input("Vitesse montée (kt)", min_value=40, max_value=200, value=75, step=1)
        descent_rate_fpm = st.number_input("Taux descente (ft/min)", min_value=100, max_value=3000, value=500, step=50)
        descent_speed_kt = st.number_input("Vitesse descente (kt)", min_value=40, max_value=250, value=100, step=1)

try:
    offblock_utc = datetime.strptime(off_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
except Exception:
    st.error("Format heure OFF invalide. Utilise YYYY-MM-DD HH:MM")
    st.stop()

departure = resolve_airport(dep_icao)
if not departure:
    st.error("Aérodrome de départ introuvable.")
    st.stop()

metar_raw, metar_decoded = fetch_metar(dep_icao)

with st.expander("Terrain de départ", expanded=True):
    c1, c2 = st.columns([1, 2])
    with c1:
        metric_card("OACI", departure.icao)
        metric_card("Nom", departure.name)
        metric_card("Position", f"{departure.lat:.4f}, {departure.lon:.4f}")
        metric_card("Élévation", f"{departure.elev_ft:.0f} ft")
    with c2:
        if metar_raw:
            st.code(metar_raw, language="text")
            st.info(metar_human(metar_decoded))
        else:
            st.warning("Pas de METAR disponible pour ce terrain.")

with st.expander("Branches", expanded=True):
    st.caption("Pour un point tournant : route vraie + distance + altitude. Pour un terrain : OACI arrivée.")

    if st.button("➕ Ajouter une branche", use_container_width=True):
        st.session_state.legs_data.append(
            {
                "leg_type": "point_tournant",
                "route_true_deg": 0.0,
                "distance_nm": 10.0,
                "altitude_ft": cruise_alt_ft,
                "end_type": "standard",
                "target_icao": "",
                "label": f"PT {len(st.session_state.legs_data) + 1}",
            }
        )
        st.rerun()

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
                st.caption(f"Route affichée : {route3(leg['route_true_deg'])}")
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

            if st.button(f"🗑️ Supprimer branche {i + 1}", key=f"del_{i}", use_container_width=True):
                delete_idx = i

        st.divider()

    if delete_idx is not None:
        st.session_state.legs_data.pop(delete_idx)
        if not st.session_state.legs_data:
            st.session_state.legs_data = default_legs()
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

try:
    legs, nav_points = build_route(departure, legs_in, offblock_utc, tas_kt)
except ValueError as e:
    st.error(str(e))
    st.stop()

selected_leg_idx = st.selectbox(
    "Branche sélectionnée",
    options=[leg.idx for leg in legs],
    format_func=lambda i: f"Branche {i}: {legs[i - 1].start_name} → {legs[i - 1].end_name}",
)

tabs = st.tabs(["Carte", "Navigation", "Profil vertical", "Météo"])

with tabs[0]:
    fmap = build_map(nav_points, legs, selected_leg_idx, openaip_key)
    st_folium(fmap, use_container_width=True, height=560)

    sel = legs[selected_leg_idx - 1]
    c1, c2 = st.columns(2)
    with c1:
        metric_card("Branche", f"{sel.start_name} → {sel.end_name}")
        metric_card("Route vraie", route3(sel.route_true_deg))
        metric_card("Cap vrai", route3(sel.heading_true_deg))
        metric_card("Dérive", f"{sel.drift_deg:+.1f}°")
    with c2:
        metric_card("Vent", f"{route3(sel.wind_dir_deg)}/{sel.wind_speed_kt:.0f} kt")
        metric_card("Source vent", sel.wind_source)
        metric_card("Altitude", f"{int(sel.altitude_ft)} ft")
        metric_card("GS", f"{sel.gs_kt:.0f} kt")

with tabs[1]:
    total_nm = sum(l.distance_nm for l in legs)
    total_min = sum(l.ete_min for l in legs)
    trip_fuel_l = total_min / 60.0 * fuel_burn_lph
    total_fuel_l = trip_fuel_l + reserve_min / 60.0 * fuel_burn_lph

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Distance totale", f"{total_nm:.1f} NM")
    with c2:
        metric_card("Temps total", f"{total_min:.1f} min")
    with c3:
        metric_card("Trip fuel", f"{trip_fuel_l:.1f} L")
    with c4:
        metric_card("Fuel + réserve", f"{total_fuel_l:.1f} L")

    st.markdown("### Log de navigation")
    for leg in legs:
        leg_card(leg, selected=(leg.idx == selected_leg_idx))

with tabs[2]:
    verticale_ft = st.number_input("Hauteur verticale terrain (ft sol)", min_value=500, max_value=3000, value=1500, step=100)
    tdp_ft = st.number_input("Hauteur tour de piste (ft sol)", min_value=500, max_value=2000, value=1000, step=100)

    profile_pts = build_profile_points(nav_points, n_per_leg=18)
    route_d = cumulative_distances_nm(profile_pts)

    elev_m = fetch_elevations(tuple(p[0] for p in profile_pts), tuple(p[1] for p in profile_pts))
    if elev_m is None:
        terrain_ft = [0.0] * len(profile_pts)
        st.warning("Relief indisponible en ligne, profil affiché sans terrain.")
    else:
        terrain_ft = [m_to_ft(x) for x in elev_m]

    last_leg = legs[-1]
    profile = build_vertical_profile(
        dep_elev_ft=departure.elev_ft,
        arr_elev_ft=last_leg.arrival_elev_ft,
        end_type=last_leg.end_type,
        cruise_alt_ft=cruise_alt_ft,
        route_d_nm=route_d,
        climb_rate_fpm=climb_rate_fpm,
        climb_speed_kt=climb_speed_kt,
        descent_rate_fpm=descent_rate_fpm,
        descent_speed_kt=descent_speed_kt,
        verticale_ft=verticale_ft,
        tdp_ft=tdp_ft,
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
        height=430,
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

    if terrain_ft:
        min_margin = min(a - t for a, t in zip(profile["alt_profile_ft"], terrain_ft))
        if min_margin < 500:
            st.error(f"Marge verticale minimale faible : {min_margin:.0f} ft")
        else:
            st.success(f"Marge verticale minimale : {min_margin:.0f} ft")

with tabs[3]:
    st.subheader(f"Départ {departure.icao}")
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("Terrain", departure.name)
    with c2:
        metric_card("Position", f"{departure.lat:.4f}, {departure.lon:.4f}")
    with c3:
        metric_card("Élévation", f"{departure.elev_ft:.0f} ft")

    if metar_raw:
        st.code(metar_raw, language="text")
        st.info(metar_human(metar_decoded))
    else:
        st.warning("METAR indisponible pour ce terrain.")

    sel = legs[selected_leg_idx - 1]
    st.subheader(f"Vent branche {sel.idx}")
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("Source", sel.wind_source)
    with c2:
        metric_card("Vent retenu", f"{route3(sel.wind_dir_deg)}/{sel.wind_speed_kt:.0f} kt")
    with c3:
        midpoint_eta = sel.eta_utc - timedelta(minutes=sel.ete_min / 2)
        metric_card("Validité approx.", nearest_hour(midpoint_eta).strftime("%Y-%m-%d %H:%M UTC"))

st.caption("Installation : pip install streamlit streamlit-folium folium requests pandas plotly")
