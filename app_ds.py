import os
import math
import re
import datetime as dt
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from fpdf import FPDF
from streamlit_folium import st_folium

# =========================================================
# CONFIGURATION
# =========================================================
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
NOAA_DECL_URL = "https://www.ngdc.noaa.gov/geomag-web/calculators/calculateDeclination"
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
HTTP_TIMEOUT = 8
ARRIVAL_METAR_RADIUS_NM = 15.0
OPENAIP_API_KEY = os.getenv("OPENAIP_API_KEY", "")

st.set_page_config(page_title="SkyAssistant V58.6", layout="wide")

# =========================================================
# CSS
# =========================================================
st.markdown(
    """
<style>
div[data-testid="stDataFrame"] [data-testid="stElementToolbar"],
div[data-testid="stDataEditor"] [data-testid="stElementToolbar"] {
    display: none !important;
}
.sa-card {
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 14px 16px;
    background: rgba(255,255,255,0.02);
    margin-bottom: 0.75rem;
}
.sa-card h4 {
    margin: 0 0 0.35rem 0;
    font-size: 0.95rem;
}
.sa-card p {
    margin: 0;
    opacity: 0.95;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
}
.sa-divider {
    margin-top: 0;
    margin-bottom: 0.6rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
}
</style>
""",
    unsafe_allow_html=True,
)

# =========================================================
# SESSION HTTP
# =========================================================
@st.cache_resource
def get_http_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "SkyAssistant/58.6"})
    return session


SESSION = get_http_session()

# =========================================================
# SESSION STATE
# =========================================================
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []
if "wx_refresh" not in st.session_state:
    st.session_state.wx_refresh = 0


# =========================================================
# AIRPORTS
# =========================================================
@st.cache_data(ttl=86400)
def load_airports():
    base = {"LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069}}
    try:
        df = pd.read_csv(
            "https://ourairports.com/data/airports.csv",
            usecols=["ident", "name", "latitude_deg", "longitude_deg", "iso_country", "type"],
        )
        fr = df[
            (df["iso_country"] == "FR")
            & (df["type"].isin(["large_airport", "medium_airport", "small_airport"]))
        ]
        fr = fr[fr["ident"].astype(str).str.match(r"^LF[A-Z0-9]{2}$")]
        downloaded = {
            row.ident: {
                "name": row.name,
                "lat": float(row.latitude_deg),
                "lon": float(row.longitude_deg),
            }
            for row in fr.itertuples(index=False)
        }
        base.update(downloaded)
        return base
    except Exception:
        return base


AIRPORTS = load_airports()
ICAO_LF_RE = re.compile(r"^LF[A-Z0-9]{2}$")


# =========================================================
# HELPERS
# =========================================================
def is_lf_icao(value: str) -> bool:
    return bool(ICAO_LF_RE.match(str(value).upper().strip()))


def norm360(value: float) -> float:
    return (value % 360.0 + 360.0) % 360.0


def fmt_hdg3(value: float) -> str:
    return f"{int(round(norm360(value))):03d}"


def _pdf_safe(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("➔", "->").replace("→", "->").replace("—", "-").replace("–", "-")
    return text.encode("latin-1", "ignore").decode("latin-1")


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    r_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return (r_km * c) / 1.852


@st.cache_data(ttl=86400)
def airports_df_fr_lf():
    rows = []
    for icao, airport in AIRPORTS.items():
        if is_lf_icao(icao):
            rows.append({
                "icao": icao,
                "name": airport.get("name", ""),
                "lat": airport.get("lat"),
                "lon": airport.get("lon"),
            })
    return pd.DataFrame(rows)


def nearest_airfields(lat, lon, radius_nm=15.0, k=5, exclude_icao=None):
    df = airports_df_fr_lf().copy()
    if exclude_icao and is_lf_icao(exclude_icao):
        df = df[df["icao"] != exclude_icao]

    lat1 = np.radians(lat)
    lon1 = np.radians(lon)
    lat2 = np.radians(df["lat"].to_numpy(dtype=float))
    lon2 = np.radians(df["lon"].to_numpy(dtype=float))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
    km = 6371.0 * c
    df["d_nm"] = km / 1.852

    df = df[df["d_nm"] <= radius_nm].sort_values("d_nm").head(k)
    return df[["icao", "name", "d_nm"]].to_dict("records")


def format_hhmm_from_seconds(total_seconds: float) -> str:
    total_seconds = int(round(total_seconds))
    hh = total_seconds // 3600
    mm = (total_seconds % 3600) // 60
    return f"{hh:02d}:{mm:02d}"


def summarize_route_names(waypoints, max_items: int = 5) -> str:
    names = [w["name"] for w in waypoints]
    if len(names) <= max_items:
        return " → ".join(names)
    return " → ".join(names[:2] + ["…"] + names[-2:])


def get_arrival_metar_candidate(waypoints, dep_icao: str):
    if not waypoints:
        return None
    last = waypoints[-1]
    nearby = nearest_airfields(last["lat"], last["lon"], radius_nm=ARRIVAL_METAR_RADIUS_NM, k=1)
    if not nearby:
        return None
    candidate = nearby[0]
    if candidate["icao"] == dep_icao:
        return None
    return {"icao": candidate["icao"], "name": candidate["name"], "label": "METAR arrivée"}


# =========================================================
# DATA FETCHERS
# =========================================================
@st.cache_data(ttl=86400)
def _elevation_ft_cached(lat: float, lon: float) -> int:
    response = SESSION.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return round(payload.get("elevation", [0])[0] * 3.28084)


_elev_local_cache = {}


def get_elevation_ft(lat: float, lon: float) -> int:
    try:
        key = (round(lat, 3), round(lon, 3))
        if key in _elev_local_cache:
            return _elev_local_cache[key]
        elev = _elevation_ft_cached(lat, lon)
        _elev_local_cache[key] = elev
        return elev
    except Exception:
        return 0


@st.cache_data(ttl=600)
def get_metar_cached(icao: str, wx_refresh: int) -> str:
    try:
        response = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code == 200:
            lines = response.text.splitlines()
            return lines[1] if len(lines) > 1 else "METAR indisponible"
        return "METAR indisponible"
    except Exception:
        return "Erreur METAR"


@st.cache_data(ttl=600)
def get_taf_cached(icao: str, wx_refresh: int) -> str:
    try:
        response = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{icao}.TXT",
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code == 200:
            lines = [line.strip() for line in response.text.splitlines() if line.strip()]
            if len(lines) > 1:
                return "\n".join(lines[1:])
            return "TAF indisponible"
        return f"TAF indisponible (HTTP {response.status_code})"
    except Exception as exc:
        return f"Erreur TAF: {exc}"


@st.cache_data(ttl=86400 * 30)
def get_declination_deg(lat: float, lon: float, date_utc: dt.datetime) -> float:
    try:
        params = {
            "lat1": lat,
            "lon1": lon,
            "model": "WMM",
            "startYear": date_utc.year,
            "startMonth": date_utc.month,
            "startDay": date_utc.day,
            "resultFormat": "json",
        }
        response = SESSION.get(NOAA_DECL_URL, params=params, timeout=HTTP_TIMEOUT)
        payload = response.json()
        result = payload.get("result", [{}])[0]
        decl = result.get("declination", None)
        return float(decl) if decl is not None else 0.0
    except Exception:
        return 0.0


@st.cache_data(ttl=900)
def get_wind_openmeteo_cached(lat: float, lon: float, level_hpa: int, wx_refresh: int) -> dict:
    params = {
        "latitude": round(lat, 2),
        "longitude": round(lon, 2),
        "hourly": f"wind_speed_{level_hpa}hPa,wind_direction_{level_hpa}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
    }
    response = SESSION.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
    return response.json()


def get_wind_v27_final(lat, lon, alt_ft, time_dt, manual_wind=None, wx_refresh: int = 0):
    if manual_wind:
        return float(manual_wind["wd"]), float(manual_wind["ws"]), "Manuel"

    target_alt = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    level_hpa = PRESSURE_MAP[target_alt]
    try:
        payload = get_wind_openmeteo_cached(lat, lon, level_hpa, wx_refresh)
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return 0.0, 0.0, "Err"

        def pick_model(prefix: str):
            ws = hourly.get(f"wind_speed_{level_hpa}hPa_{prefix}")
            wd = hourly.get(f"wind_direction_{level_hpa}hPa_{prefix}")
            if ws and wd and ws[0] is not None and wd[0] is not None:
                return wd, ws
            return None

        picked = pick_model("icon_d2")
        if picked:
            wd_arr, ws_arr, src = picked[0], picked[1], "ICON-D2"
        else:
            picked = pick_model("meteofrance_arome_france_hd")
            if picked:
                wd_arr, ws_arr, src = picked[0], picked[1], "AROME"
            else:
                wd_arr = hourly.get(f"wind_direction_{level_hpa}hPa_gfs_seamless", [])
                ws_arr = hourly.get(f"wind_speed_{level_hpa}hPa_gfs_seamless", [])
                src = "GFS"

        if not wd_arr or not ws_arr:
            return 0.0, 0.0, "Err"

        t_target = time_dt.timestamp()
        timestamps = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp() for t in times]
        pos = bisect_left(timestamps, t_target)
        if pos <= 0:
            best_i = 0
        elif pos >= len(timestamps):
            best_i = len(timestamps) - 1
        else:
            prev_i = pos - 1
            next_i = pos
            best_i = prev_i if abs(timestamps[prev_i] - t_target) <= abs(timestamps[next_i] - t_target) else next_i

        return float(wd_arr[best_i]), float(ws_arr[best_i]), src
    except Exception:
        return 0.0, 0.0, "Err"


# =========================================================
# PDF
# =========================================================
def create_pdf(df_nav, metar_text):
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(0, 8, "METAR DE DEPART :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=9)
    pdf.multi_cell(0, 6, _pdf_safe(metar_text), border=1)
    pdf.ln(5)

    widths = [30, 35, 15, 20, 15, 45, 30]
    columns = ["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]
    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for col, width in zip(columns, widths):
        pdf.cell(width, 8, col, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for _, row in df_nav.iterrows():
        pdf.cell(widths[0], 8, _pdf_safe(row.get("Branche", "")).replace("➔", "->"), border=1)
        pdf.cell(widths[1], 8, _pdf_safe(row.get("Vent", "")), border=1)
        pdf.cell(widths[2], 8, _pdf_safe(row.get("GS", "")), border=1, align="C")
        pdf.cell(widths[3], 8, _pdf_safe(row.get("EET", "")), border=1, align="C")
        pdf.cell(widths[4], 8, _pdf_safe(row.get("Fuel", "")), border=1, align="C")
        pdf.cell(widths[5], 8, _pdf_safe(row.get("TOC/TOD", "")), border=1)
        pdf.cell(widths[6], 8, _pdf_safe(row.get("Arrivée", "")), border=1)
        pdf.ln()

    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return out.encode("latin-1", "ignore")


# =========================================================
# MAP BUILDER
# =========================================================
def build_map(waypoints: list) -> folium.Map:
    center = [waypoints[0]["lat"], waypoints[0]["lon"]]

    if OPENAIP_API_KEY:
        # OSM comme fond, OpenAIP en overlay par-dessus
        m = folium.Map(location=center, zoom_start=9, control_scale=True, tiles="openstreetmap")
        folium.TileLayer(
            tiles=f"https://api.tiles.openaip.net/api/data/openaip/{{z}}/{{x}}/{{y}}.png?apiKey={OPENAIP_API_KEY}",
            attr='<a href="https://www.openaip.net/">openAIP</a>',
            name="Données aviation (openAIP)",
            overlay=True,
            control=True,
            opacity=1.0,
        ).add_to(m)
        folium.LayerControl(collapsed=False).add_to(m)
    else:
        m = folium.Map(location=center, zoom_start=9, control_scale=True, tiles="openstreetmap")

    # Tracé de la route
    folium.PolyLine(
        [[w["lat"], w["lon"]] for w in waypoints],
        color="red",
        weight=3,
    ).add_to(m)

    # Marqueurs
    num_waypoints = len(waypoints)
    for i, waypoint in enumerate(waypoints):
        if i == 0:
            icon_color, icon_name = "blue", "plane"
        elif i == num_waypoints - 1:
            icon_color, icon_name = "red", "flag"
        else:
            icon_color, icon_name = "orange", "circle"

        folium.Marker(
            [waypoint["lat"], waypoint["lon"]],
            popup=waypoint["name"],
            icon=folium.Icon(color=icon_color, icon=icon_name, prefix="fa"),
        ).add_to(m)

    return m


# =========================================================
# SIDEBAR
# =========================================================
with st.sidebar:
    st.title("✈️ SkyAssistant V58.6")

    if st.button("🔄 Rafraîchir météo", use_container_width=True):
        st.session_state.wx_refresh += 1
        st.rerun()

    search = st.text_input("🔍 Rechercher OACI", "").upper()
    suggestions = [icao for icao in AIRPORTS.keys() if icao.startswith(search)] if search else []

    if suggestions:
        icao0 = suggestions[0]
        airport0 = AIRPORTS[icao0]
        if st.button(f"Départ : {airport0['name']} ({icao0})", use_container_width=True):
            elev = get_elevation_ft(airport0["lat"], airport0["lon"])
            st.session_state.waypoints = [{
                "name": icao0,
                "lat": airport0["lat"],
                "lon": airport0["lon"],
                "alt": elev,
                "elev": elev,
                "arr_type": "Direct",
            }]
            st.rerun()

    if st.session_state.waypoints:
        dep_icao = st.session_state.waypoints[0]["name"]
        dep_name = AIRPORTS.get(dep_icao, {}).get("name", dep_icao)
        st.success(f"Départ sélectionné : {dep_name} ({dep_icao})")

    with st.expander("🧾 Briefing", expanded=False):
        st.link_button("📌 SOFIA Briefing (NOTAM)", "https://sofia-briefing.aviation-civile.gouv.fr/sofia/pages/homepage.html")
        st.link_button("📚 SIA / Visualisateur AIP", "https://www.sia.aviation-civile.gouv.fr/vaip")

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100, step=1)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840, step=10)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500, step=10)
    fuel_flow = st.number_input("Conso (L/h)", 1, 200, 20, step=1)
    dep_time = st.time_input("Heure départ (UTC)", value=dt.time(0, 0))

    if st.button("🗑️ Reset", use_container_width=True):
        st.session_state.waypoints = []
        st.rerun()


# =========================================================
# TOP CONTAINERS
# =========================================================
mission_placeholder = st.container()
weather_placeholder = st.container()

# =========================================================
# WEATHER
# =========================================================
metar_val = ""
taf_val = ""
if st.session_state.waypoints:
    dep_icao = st.session_state.waypoints[0]["name"]
    dep_name = AIRPORTS.get(dep_icao, {}).get("name", dep_icao)
    metar_val = get_metar_cached(dep_icao, st.session_state.wx_refresh)
    taf_val = get_taf_cached(dep_icao, st.session_state.wx_refresh)
    arr_candidate = get_arrival_metar_candidate(st.session_state.waypoints, dep_icao)

    with weather_placeholder.container():
        st.subheader("🌦️ Météo")
        st.markdown('<div class="sa-divider"></div>', unsafe_allow_html=True)
        wx_col1, wx_col2 = st.columns(2)
        with wx_col1:
            st.markdown(
                f'<div class="sa-card"><h4>Départ — {dep_name} ({dep_icao})</h4><p>{metar_val}</p></div>',
                unsafe_allow_html=True,
            )
        with wx_col2:
            if arr_candidate:
                arr_metar = get_metar_cached(arr_candidate["icao"], st.session_state.wx_refresh)
                st.markdown(
                    f'<div class="sa-card"><h4>{arr_candidate["label"]} — {arr_candidate["name"]} ({arr_candidate["icao"]})</h4><p>{arr_metar}</p></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="sa-card"><h4>Arrivée</h4><p>Aucun METAR distinct à afficher.</p></div>',
                    unsafe_allow_html=True,
                )
        with st.expander(f"📄 TAF départ — {dep_icao}", expanded=False):
            st.code(taf_val, language="text")


# =========================================================
# MAP + CONTROLS
# =========================================================
col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    st.markdown('<div class="sa-divider"></div>', unsafe_allow_html=True)
    rv_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, step=1)
    st.caption(f"Route affichée : {fmt_hdg3(rv_in)}°")
    dist_in = st.number_input("Distance (NM)", 0.1, 300.0, 15.0, step=0.1)
    alt_in = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Auto", True)

    manual_wind = None
    if not use_auto:
        manual_wind = {
            "wd": st.number_input("Dir", 0, 359, 0, step=1),
            "ws": st.number_input("Force", 0, 100, 0, step=1),
        }

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        r_nm = 3440.065
        bearing = math.radians(rv_in)
        lat1 = math.radians(last["lat"])
        lon1 = math.radians(last["lon"])
        lat2 = math.degrees(lat1 + (dist_in / r_nm) * math.cos(bearing))
        lon2 = math.degrees(lon1 + (dist_in / r_nm) * math.sin(bearing) / max(1e-9, math.cos(lat1)))
        elev2 = get_elevation_ft(lat2, lon2)

        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": lat2,
            "lon": lon2,
            "tc": int(rv_in),
            "dist": float(dist_in),
            "alt": int(alt_in),
            "manual_wind": manual_wind,
            "elev": elev2,
            "arr_type": "Direct",
        })
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = build_map(st.session_state.waypoints)
        st_folium(m, width="100%", height=380, key="map_main", returned_objects=[])


# =========================================================
# NAV LOG + PROFILE
# =========================================================
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    now_utc = dt.datetime.now(dt.timezone.utc)
    dep_dt = dt.datetime.combine(now_utc.date(), dep_time, tzinfo=dt.timezone.utc)

    nav_rows = []
    dist_profile = [0.0]
    elev0 = float(st.session_state.waypoints[0].get("elev", 0))
    if elev0 <= 0:
        elev_try = get_elevation_ft(
            st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]
        )
        if elev_try > 0:
            elev0 = float(elev_try)
            st.session_state.waypoints[0]["elev"] = elev0

    alt_profile = [elev0]
    terr_profile = [elev0]
    total_distance = 0.0
    fig = go.Figure()
    current_alt = elev0
    wind_local_cache = {}
    decl_local_cache = {}
    cum_sec = 0.0
    fuel_total = 0.0

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i - 1], st.session_state.waypoints[i]
        rv = float(w2.get("tc", 0))
        dist_nm = float(w2.get("dist", 0))
        alt_ft = float(w2.get("alt", 0))
        elev2 = float(w2.get("elev", 0))
        manual = w2.get("manual_wind", None)

        target_alt = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
        level_hpa = PRESSURE_MAP[target_alt]
        wind_key = (round(w2["lat"], 2), round(w2["lon"], 2), level_hpa, st.session_state.wx_refresh)
        decl_key = (round(w2["lat"], 2), round(w2["lon"], 2), dep_dt.date().isoformat())

        future_elev = None
        future_wind = None
        future_decl = None

        with ThreadPoolExecutor(max_workers=3) as executor:
            if elev2 <= 0:
                future_elev = executor.submit(get_elevation_ft, w2["lat"], w2["lon"])

            if manual:
                wd, ws, src = float(manual["wd"]), float(manual["ws"]), "Manuel"
            elif wind_key in wind_local_cache:
                wd, ws, src = wind_local_cache[wind_key]
            else:
                future_wind = executor.submit(
                    get_wind_v27_final,
                    w2["lat"],
                    w2["lon"],
                    alt_ft,
                    now_utc,
                    None,
                    st.session_state.wx_refresh,
                )

            if decl_key in decl_local_cache:
                decl = decl_local_cache[decl_key]
            else:
                future_decl = executor.submit(
                    get_declination_deg, float(w2["lat"]), float(w2["lon"]), dep_dt
                )

            if future_elev is not None:
                elev_try = future_elev.result()
                if elev_try > 0:
                    elev2 = float(elev_try)
                    w2["elev"] = elev2

            if future_wind is not None:
                wd, ws, src = future_wind.result()
                wind_local_cache[wind_key] = (wd, ws, src)

            if future_decl is not None:
                decl = future_decl.result()
                decl_local_cache[decl_key] = decl

        wa = math.radians(wd - rv)
        sin_wca = (ws / max(1e-9, float(tas))) * math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0.0
        cap_vrai = norm360(rv + wca)
        gs = max(20.0, (float(tas) * math.cos(math.radians(wca))) - (ws * math.cos(wa)))
        cap_mag = norm360(cap_vrai - decl)

        hours = dist_nm / max(1e-9, gs)
        seg_sec = hours * 3600.0
        fuel_branch = round(hours * float(fuel_flow), 1)
        fuel_total += fuel_branch
        cum_sec += seg_sec
        eta_dt = dep_dt + dt.timedelta(seconds=cum_sec)
        toc_tod_text = ""

        if alt_ft > current_alt:
            t_climb = ((alt_ft - current_alt) / max(1e-9, float(v_climb))) * 60.0
            d_climb = gs * (t_climb / 3600.0)
            if d_climb > 0.1:
                t_cl_str = f"{int(t_climb//60):02d}:{int(t_climb%60):02d}"
                toc_tod_text += f"TOC:{round(d_climb,1)}NM "
                if d_climb < dist_nm:
                    x_toc = total_distance + d_climb
                    dist_profile.append(x_toc)
                    alt_profile.append(alt_ft)
                    terr_profile.append(float(w1.get("elev", 0)))
                    fig.add_annotation(
                        x=x_toc,
                        y=alt_ft,
                        text=f"TOC {round(d_climb,1)}NM ({t_cl_str})",
                        showarrow=True,
                        ay=45,
                    )

        arrival_type = w2.get("arr_type", "Direct")
        if (i == len(st.session_state.waypoints) - 1) and arrival_type == "Direct":
            arrival_type = "VT (1500ft)"

        if arrival_type != "Direct":
            alt_target = elev2 + (1500 if "VT" in arrival_type else 1000)
            t_desc = (
                ((alt_ft - alt_target) / max(1e-9, float(v_descent))) * 60.0
                if alt_ft > alt_target
                else 0.0
            )
            d_desc = gs * (t_desc / 3600.0)
            if d_desc > 0.1:
                t_de_str = f"{int(t_desc//60):02d}:{int(t_desc%60):02d}"
                toc_tod_text += f"TOD:{round(d_desc,1)}NM"
                if d_desc < dist_nm:
                    x_tod = total_distance + (dist_nm - d_desc)
                    dist_profile.append(x_tod)
                    alt_profile.append(alt_ft)
                    terr_profile.append(elev2)
                    fig.add_annotation(
                        x=x_tod,
                        y=alt_ft,
                        text=f"TOD {round(d_desc,1)}NM ({t_de_str})",
                        showarrow=True,
                        ay=-45,
                    )

            label_dest = "VT" if "VT" in arrival_type else "TDP"
            fig.add_annotation(
                x=total_distance + dist_nm,
                y=alt_target,
                text=f"<b>{label_dest} {w2['name']}</b>",
                showarrow=False,
                yshift=15,
                font=dict(color="orange", size=11),
            )
            total_distance += dist_nm
            dist_profile.append(total_distance)
            alt_profile.append(alt_target)
            terr_profile.append(elev2)
            dist_profile.append(total_distance)
            alt_profile.append(elev2)
            terr_profile.append(elev2)
            fig.add_vline(x=total_distance, line_width=2, line_dash="dash", line_color="orange")
            current_alt = elev2
        else:
            total_distance += dist_nm
            dist_profile.append(total_distance)
            alt_profile.append(alt_ft)
            terr_profile.append(elev2)
            current_alt = alt_ft

        drift_txt = f"{wca:+.0f}°"
        cap_txt = f"{fmt_hdg3(cap_mag)} ({drift_txt})"
        nav_rows.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Vent": f"{int(wd)}/{int(ws)}kt ({src})",
            "GS": f"{int(gs)}kt",
            "EET": f"{int(seg_sec//60):02d}:{int(seg_sec%60):02d}",
            "Fuel": f"{fuel_branch:.1f}L",
            "TOC/TOD": toc_tod_text.strip(),
            "Arrivée": arrival_type,
            "❌": False,
            "_idx": i,
            "ETA": eta_dt.strftime("%H:%M"),
            "Cap": cap_txt,
        })

    df_nav = pd.DataFrame(nav_rows)

    with mission_placeholder.container():
        st.subheader("🧭 Mission")
        st.markdown('<div class="sa-divider"></div>', unsafe_allow_html=True)
        card = st.container(border=True)
        with card:
            st.caption(summarize_route_names(st.session_state.waypoints))
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Distance totale", f"{total_distance:.1f} NM")
            m2.metric("Temps total", format_hhmm_from_seconds(cum_sec))
            m3.metric("Carburant total", f"{fuel_total:.1f} L")
            m4.metric("ETA arrivée", df_nav.iloc[-1]["ETA"] if len(df_nav) else "--:--")

    st.subheader("📋 Log de Navigation")
    st.markdown('<div class="sa-divider"></div>', unsafe_allow_html=True)
    df_screen = df_nav[["Branche", "Cap", "Vent", "GS", "EET", "Fuel", "ETA", "TOC/TOD", "Arrivée", "❌", "_idx"]].copy()
    edited_log = st.data_editor(
        df_screen,
        column_config={
            "Branche": st.column_config.TextColumn("Branche", width="small"),
            "Cap": st.column_config.TextColumn("Cap (mag) (+dérive)", width="small", disabled=True),
            "Vent": st.column_config.TextColumn("Vent", width="small", disabled=True),
            "GS": st.column_config.TextColumn("GS", width="small", disabled=True),
            "EET": st.column_config.TextColumn("EET", width="small", disabled=True),
            "Fuel": st.column_config.TextColumn("Fuel", width="small", disabled=True),
            "ETA": st.column_config.TextColumn("ETA", width="small", disabled=True),
            "TOC/TOD": st.column_config.TextColumn("TOC/TOD", width="medium", disabled=True),
            "Arrivée": st.column_config.SelectboxColumn(
                "Arrivée", options=["Direct", "TDP (1000ft)", "VT (1500ft)"], width="small"
            ),
            "❌": st.column_config.CheckboxColumn("❌", width="small"),
            "_idx": None,
        },
        hide_index=True,
    )

    if edited_log.to_dict("records") != df_screen.to_dict("records"):
        new_waypoints = [st.session_state.waypoints[0]]
        for _, row in edited_log.iterrows():
            if not row["❌"]:
                wp = st.session_state.waypoints[int(row["_idx"])].copy()
                wp["arr_type"] = row["Arrivée"]
                branch_txt = str(row["Branche"])
                if "➔" in branch_txt:
                    wp["name"] = branch_txt.split("➔", 1)[1].strip()
                elif "->" in branch_txt:
                    wp["name"] = branch_txt.split("->", 1)[1].strip()
                new_waypoints.append(wp)
        st.session_state.waypoints = new_waypoints
        st.rerun()

    df_pdf = df_nav[["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]].copy()
    st.download_button(
        label="📥 Log PDF",
        data=create_pdf(df_pdf, metar_val),
        file_name="nav_log.pdf",
        use_container_width=True,
    )

    fig.add_trace(go.Scatter(x=dist_profile, y=terr_profile, fill="tozeroy", name="Relief", line_color="sienna"))
    fig.add_trace(
        go.Scatter(x=dist_profile, y=alt_profile, name="Profil Avion", line=dict(color="royalblue", width=4))
    )
    fig.update_layout(
        width=1000,
        height=350,
        xaxis=dict(fixedrange=True, tickformat=".1f", title="Distance (NM)"),
        yaxis=dict(fixedrange=True, title="Altitude (ft)"),
        margin=dict(l=40, r=40, t=20, b=40),
        showlegend=False,
    )

    st.subheader("📈 Profil vertical")
    st.markdown('<div class="sa-divider"></div>', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=False, config={"staticPlot": True, "displayModeBar": False})
