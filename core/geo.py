import math
from typing import List, Tuple

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
