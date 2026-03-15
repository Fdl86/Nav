import logging
from typing import Optional, List, Tuple, Dict

import requests
import streamlit as st

from models import WeatherBundle, DWD_LEVELS_M
from services.http import session, fetch_json
from core.navigation import (
    generation_hour_utc, union_pressure_vars, build_hour_indices,
    mean_branch_pressure_wind, mean_branch_surface_wind, metar_surface_wind,
)

METAR_API_URL  = "https://aviationweather.gov/api/data/metar"
TAF_API_URL    = "https://aviationweather.gov/api/data/taf"
OPENMETEO_DWD  = "https://api.open-meteo.com/v1/dwd-icon"
OPENMETEO_MF   = "https://api.open-meteo.com/v1/meteofrance"

LOGGER = logging.getLogger(__name__)


@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_openmeteo_hour_block(
    source: str,
    lats: Tuple[float, ...],
    lons: Tuple[float, ...],
    hourly_vars: Tuple[str, ...],
):
    url = OPENMETEO_DWD if source == "ICON-D2" else OPENMETEO_MF
    params = {
        "latitude":        ",".join(f"{x:.6f}" for x in lats),
        "longitude":       ",".join(f"{x:.6f}" for x in lons),
        "hourly":          ",".join(hourly_vars),
        "wind_speed_unit": "kn",
        "timezone":        "UTC",
        "forecast_days":   2,
        "cell_selection":  "nearest",
    }
    js = fetch_json(url, params=params, timeout=25)
    return js if isinstance(js, list) else [js]


@st.cache_data(ttl=60 * 10, show_spinner=False)
def fetch_metar(icao: str) -> Tuple[Optional[str], Optional[dict]]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None, None
    try:
        r = session().get(METAR_API_URL, params={"ids": icao, "format": "json"}, timeout=15)
        if r.status_code == 204:
            return None, None
        r.raise_for_status()
        js = r.json()
        if not js:
            return None, None
        m = js[0]
        raw = m.get("rawOb") or m.get("raw_text") or m.get("raw")
        decoded = {
            "obs_time":     m.get("obsTime") or m.get("receiptTime"),
            "wind_dir":     m.get("wdir"),
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
        r = session().get(TAF_API_URL, params={"ids": icao, "format": "json"}, timeout=15)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        js = r.json()
        if not js:
            return None
        item = js[0]
        return item.get("rawTAF") or item.get("raw_text") or item.get("raw") or item.get("taf")
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
    return WeatherBundle(metar_raw=metar_raw, metar_decoded=metar_decoded, taf_raw=taf_raw)


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
        pts   = geom["sample_points"]
        count = len(pts)
        branch_point_indices[geom["idx"]] = list(range(point_cursor, point_cursor + count))
        point_cursor += count
        point_lats.extend(p[0] for p in pts)
        point_lons.extend(p[1] for p in pts)
        altitudes_ft.append(geom["altitude_ft"])

    lats = tuple(round(x, 6) for x in point_lats)
    lons = tuple(round(x, 6) for x in point_lons)
    wind_by_leg: Dict[int, Tuple[str, float, float]] = {}

    # ── ICON-D2 niveaux de pression ──
    icon_pressure_vars     = union_pressure_vars(altitudes_ft, DWD_LEVELS_M)
    icon_pressure_items    = None
    icon_pressure_indices  = None
    if icon_pressure_vars:
        try:
            icon_pressure_items   = fetch_openmeteo_hour_block("ICON-D2", lats, lons, icon_pressure_vars)
            icon_pressure_indices = build_hour_indices(icon_pressure_items)
        except requests.RequestException as exc:
            LOGGER.warning("Erreur réseau vent niveau ICON-D2: %s", exc)
        except Exception:
            LOGGER.exception("Erreur inattendue vent niveau ICON-D2")

    if icon_pressure_items:
        for geom in geometries:
            avg = mean_branch_pressure_wind(
                icon_pressure_items,
                branch_point_indices[geom["idx"]],
                hour_key,
                geom["altitude_ft"],
                DWD_LEVELS_M,
                hour_indices=icon_pressure_indices,
            )
            if avg:
                wind_by_leg[geom["idx"]] = ("ICON-D2 niveau", avg[0], avg[1])

    # ── Météo-France surface (fallback) ──
    surface_vars          = ("wind_speed_10m", "wind_direction_10m")
    mf_surface_items      = None
    mf_surface_indices    = None
    missing_leg_ids = [geom["idx"] for geom in geometries if geom["idx"] not in wind_by_leg]
    if missing_leg_ids:
        try:
            mf_surface_items   = fetch_openmeteo_hour_block("MF", lats, lons, surface_vars)
            mf_surface_indices = build_hour_indices(mf_surface_items)
        except requests.RequestException as exc:
            LOGGER.warning("Erreur réseau vent surface Météo-France: %s", exc)
        except Exception:
            LOGGER.exception("Erreur inattendue vent surface Météo-France")

    if mf_surface_items:
        for geom in geometries:
            if geom["idx"] in wind_by_leg:
                continue
            avg = mean_branch_surface_wind(
                mf_surface_items,
                branch_point_indices[geom["idx"]],
                hour_key,
                hour_indices=mf_surface_indices,
            )
            if avg:
                wind_by_leg[geom["idx"]] = ("Météo-France surface", avg[0], avg[1])

    # ── METAR départ (dernier recours) ──
    metar_pair = metar_surface_wind(metar_decoded)
    for geom in geometries:
        if geom["idx"] not in wind_by_leg:
            if metar_pair:
                wind_by_leg[geom["idx"]] = ("METAR départ", metar_pair[0], metar_pair[1])
            else:
                wind_by_leg[geom["idx"]] = ("Vent indisponible", 0.0, 0.0)

    return wind_by_leg
