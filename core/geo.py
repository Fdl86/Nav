import math
from typing import List, Tuple

from core.formatting import deg_norm, nm_to_m, m_to_nm


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return m_to_nm(2 * r * math.asin(math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return deg_norm(math.degrees(math.atan2(x, y)))


def destination_point(
    lat_deg: float, lon_deg: float, bearing_deg: float, distance_nm: float
) -> Tuple[float, float]:
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


def interpolate_line(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int = 16
) -> List[Tuple[float, float]]:
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t))
    return pts


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
    mid_lat_rad = math.radians(lat)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
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
