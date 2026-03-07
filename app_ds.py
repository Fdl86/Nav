import os
import streamlit as st
import requests
import pandas as pd
import datetime as dt
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go
from fpdf import FPDF
import re
import numpy as np
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
NOAA_DECL_URL = "https://www.ngdc.noaa.gov/geomag-web/calculators/calculateDeclination"
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
HTTP_TIMEOUT = 8
ARRIVAL_METAR_RADIUS_NM = 15.0

# ─── PAGE ───
APP_VERSION = "59.1-openAIP-satellite"
st.set_page_config(page_title=f"SkyAssistant {APP_VERSION}", layout="wide")

# ─── UI / UX V58 ───
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

.sa-section {
    margin-top: 0.2rem;
    margin-bottom: 0.5rem;
    padding-bottom: 0.2rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
}
</style>
""",
    unsafe_allow_html=True,
)

# ─── HTTP SESSION ───
@st.cache_resource
def get_http_session():
    s = requests.Session()
    s.headers.update({"User-Agent": f"SkyAssistant/{APP_VERSION}"})
    return s

SESSION = get_http_session()

def get_openaip_key():
    try:
        key = st.secrets.get("OPENAIP_API_KEY", "")
    except Exception:
        key = os.getenv("OPENAIP_API_KEY", "")
    return key.strip()

OPENAIP_API_KEY = get_openaip_key()

# ─── STATE ───
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []
if "wx_refresh" not in st.session_state:
    st.session_state.wx_refresh = 0
if "map_style" not in st.session_state:
    st.session_state.map_style = "openAIP"
if "map_center" not in st.session_state:
    st.session_state.map_center = None
if "map_zoom" not in st.session_state:
    st.session_state.map_zoom = 9

# ─── AIRPORTS ───
@st.cache_data(ttl=86400)
def load_airports():
    base = {"LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069}}
    try:
        df = pd.read_csv(
            "https://ourairports.com/data/airports.csv",
            usecols=["ident", "name", "latitude_deg", "longitude_deg", "iso_country", "type"],
        )
        fr = df[(df["iso_country"] == "FR") & (df["type"].isin(["large_airport", "medium_airport", "small_airport"]))]
        fr = fr[fr["ident"].astype(str).str.match(r"^LF[A-Z0-9]{2}$")]

        downloaded = {
            row.ident: {"name": row.name, "lat": float(row.latitude_deg), "lon": float(row.longitude_deg)}
            for row in fr.itertuples(index=False)
        }
        base.update(downloaded)
        return base
    except Exception:
        return base

AIRPORTS = load_airports()

# ─── HELPERS ───
ICAO_LF_RE = re.compile(r"^LF[A-Z0-9]{2}$")

def is_lf_icao(s: str) -> bool:
    return bool(ICAO_LF_RE.match(str(s).upper().strip()))

def norm360(x: float) -> float:
    return (x % 360.0 + 360.0) % 360.0

def fmt_deg(x: float) -> str:
    return f"{int(round(norm360(x))):03d}°"

def fmt_hdg3(x: float) -> str:
    return f"{int(round(norm360(x))):03d}"

def _pdf_safe(s: object) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("➔", "->").replace("→", "->").replace("—", "-").replace("–", "-")
    return s.encode("latin-1", "ignore").decode("latin-1")

def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    km = R_km * c
    return km / 1.852

@st.cache_data(ttl=86400)
def airports_df_fr_lf():
    rows = []
    for icao, ap in AIRPORTS.items():
        if is_lf_icao(icao):
            rows.append({"icao": icao, "name": ap.get("name", ""), "lat": ap.get("lat"), "lon": ap.get("lon")})
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

def build_map(waypoints, map_style, center=None, zoom=None):
    if waypoints:
        default_center = [waypoints[-1]["lat"], waypoints[-1]["lon"]]
    else:
        default_center = [46.5877, 0.3069]

    m = folium.Map(
        location=center or default_center,
        zoom_start=zoom or 9,
        control_scale=True,
        tiles=None,
    )

    if map_style == "Satellite":
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery",
            name="Satellite",
            overlay=False,
            control=False,
            show=True,
        ).add_to(m)
    else:
        if OPENAIP_API_KEY:
            folium.TileLayer(
                tiles=f"https://api.tiles.openaip.net/api/data/openaip/{{z}}/{{x}}/{{y}}.png?apiKey={OPENAIP_API_KEY}",
                attr="openAIP",
                name="openAIP",
                overlay=False,
                control=False,
                show=True,
            ).add_to(m)
        else:
            folium.TileLayer(
                tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                attr="OpenStreetMap",
                name="OSM fallback",
                overlay=False,
                control=False,
                show=True,
            ).add_to(m)

    if len(waypoints) >= 2:
        folium.PolyLine([[w["lat"], w["lon"]] for w in waypoints], color="red", weight=3).add_to(m)

    last_idx = len(waypoints) - 1
    for i, w in enumerate(waypoints):
        if i == 0:
            icon_c, icon_t = "blue", "plane"
        elif i == last_idx:
            icon_c, icon_t = "red", "flag"
        else:
            icon_c, icon_t = "orange", "circle"
        folium.Marker(
            [w["lat"], w["lon"]],
            popup=w["name"],
            icon=folium.Icon(color=icon_c, icon=icon_t, prefix="fa"),
        ).add_to(m)

    return m

def summarize_route_names(waypoints, max_items: int = 5) -> str:
    names = [w["name"] for w in waypoints]
    if len(names) <= max_items:
        return " → ".join(names)
    return " → ".join(names[:2] + ["…"] + names[-2:])

def get_arrival_metar_candidate(waypoints, dep_icao: str):
    if not waypoints:
        return None
    last = waypoints[-1]
    nearby = nearest_airfields(last["lat"], last["lon"], radius_nm=ARRIVAL_METAR_RADIUS_NM, k=1, exclude_icao=None)
    if not nearby:
        return None
    candidate = nearby[0]
    if candidate["icao"] == dep_icao:
        return None
    return {"icao": candidate["icao"], "name": candidate["name"], "label": "METAR arrivée"}

# ─── ELEVATION ───
@st.cache_data(ttl=86400)
def _elevation_ft_cached(lat: float, lon: float) -> int:
    r = SESSION.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return round(j.get("elevation", [0])[0] * 3.28084)

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

# ─── METAR / TAF ───
@st.cache_data(ttl=600)
def get_metar_cached(icao: str, wx_refresh: int) -> str:
    try:
        r = SESSION.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT", timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            lines = r.text.splitlines()
            return lines[1] if len(lines) > 1 else "METAR indisponible"
        return "METAR indisponible"
    except Exception:
        return "Erreur METAR"

@st.cache_data(ttl=600)
def get_taf_cached(icao: str, wx_refresh: int) -> str:
    try:
        r = SESSION.get(f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{icao}.TXT", timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            lines = [l.strip() for l in r.text.splitlines() if l.strip()]
            if len(lines) > 1:
                return "\n".join(lines[1:])
            return "TAF indisponible"
        return f"TAF indisponible (HTTP {r.status_code})"
    except Exception as e:
        return f"Erreur TAF: {e}"

# ─── DECLINAISON ───
@st.cache_data(ttl=86400 * 30)
def get_declination_deg(lat: float, lon: float, date_utc: dt.datetime) -> float:
    try:
        y, m, d = date_utc.year, date_utc.month, date_utc.day
        params = {
            "lat1": lat,
            "lon1": lon,
            "model": "WMM",
            "startYear": y,
            "startMonth": m,
            "startDay": d,
            "resultFormat": "json",
        }
        r = SESSION.get(NOAA_DECL_URL, params=params, timeout=HTTP_TIMEOUT)
        j = r.json()
        res0 = j.get("result", [{}])[0]
        dec = res0.get("declination", None)
        return float(dec) if dec is not None else 0.0
    except Exception:
        return 0.0

# ─── WIND ───
@st.cache_data(ttl=900)
def get_wind_openmeteo_cached(lat: float, lon: float, lv: int, wx_refresh: int) -> dict:
    lat_q = round(lat, 2)
    lon_q = round(lon, 2)
    params = {
        "latitude": lat_q,
        "longitude": lon_q,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
    }
    r = SESSION.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
    return r.json()

def get_wind_v27_final(lat, lon, alt_ft, time_dt, manual_wind=None, wx_refresh: int = 0):
    if manual_wind:
        return float(manual_wind["wd"]), float(manual_wind["ws"]), "Manuel"
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    try:
        r = get_wind_openmeteo_cached(lat, lon, lv, wx_refresh)
        h = r.get("hourly", {})
        times = h.get("time", [])
        if not times:
            return 0.0, 0.0, "Err"

        def pick_model(prefix: str):
            ws = h.get(f"wind_speed_{lv}hPa_{prefix}")
            wd = h.get(f"wind_direction_{lv}hPa_{prefix}")
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
                wd_arr = h.get(f"wind_direction_{lv}hPa_gfs_seamless", [])
                ws_arr = h.get(f"wind_speed_{lv}hPa_gfs_seamless", [])
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

# ─── PDF ───
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

    # Tableau principal
    w = [30, 35, 15, 20, 15, 45, 30]
    cols = ["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]

    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for col, width in zip(cols, w):
        pdf.cell(width, 8, col, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for row in df_nav.itertuples(index=False):
        pdf.cell(w[0], 8, _pdf_safe(getattr(row, "Branche", "")).replace("➔", "->"), border=1)
        pdf.cell(w[1], 8, _pdf_safe(getattr(row, "Vent", "")), border=1)
        pdf.cell(w[2], 8, _pdf_safe(getattr(row, "GS", "")), border=1, align="C")
        pdf.cell(w[3], 8, _pdf_safe(getattr(row, "EET", "")), border=1, align="C")
        pdf.cell(w[4], 8, _pdf_safe(getattr(row, "Fuel", "")), border=1, align="C")
        pdf.cell(w[5], 8, _pdf_safe(getattr(row, "TOC/TOD", "")), border=1)
        pdf.cell(w[6], 8, _pdf_safe(getattr(row, "Arrivée", "")), border=1)
        pdf.ln()

    pdf.ln(5)

    # Calculs de navigation
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(0, 8, "CALCULS DE NAVIGATION :", new_x="LMARGIN", new_y="NEXT")

    w2 = [34, 14, 12, 14, 12, 14, 22, 14]
    cols2 = ["Branche", "Rv", "d", "Cv", "dm", "Cm", "Deviation", "Cc"]

    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for col, width in zip(cols2, w2):
        pdf.cell(width, 8, col, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for row in df_nav.itertuples(index=False):
        pdf.cell(w2[0], 8, _pdf_safe(getattr(row, "Branche", "")).replace("➔", "->"), border=1)
        pdf.cell(w2[1], 8, _pdf_safe(getattr(row, "Rv", "")), border=1, align="C")
        pdf.cell(w2[2], 8, _pdf_safe(getattr(row, "d", "")), border=1, align="C")
        pdf.cell(w2[3], 8, _pdf_safe(getattr(row, "Cv", "")), border=1, align="C")
        pdf.cell(w2[4], 8, _pdf_safe(getattr(row, "dm", "")), border=1, align="C")
        pdf.cell(w2[5], 8, _pdf_safe(getattr(row, "Cm", "")), border=1, align="C")
        pdf.cell(w2[6], 8, "", border=1, align="C")
        pdf.cell(w2[7], 8, "", border=1, align="C")
        pdf.ln()

    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return out.encode("latin-1", "ignore")

# ─── SIDEBAR ───
with st.sidebar:
    st.title(f"✈️ SkyAssistant {APP_VERSION}")
    if st.button("🔄 Rafraîchir météo", use_container_width=True):
        st.session_state.wx_refresh += 1
        st.rerun()

    map_style = st.selectbox(
        "🗺️ Fond de carte",
        ["openAIP", "Satellite"],
        index=0 if st.session_state.map_style == "openAIP" else 1,
    )
    st.session_state.map_style = map_style

    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []

    if sugg:
        icao0 = sugg[0]
        ap0 = AIRPORTS[icao0]
        if st.button(f"Départ : {ap0['name']} ({icao0})", use_container_width=True):
            elev = get_elevation_ft(ap0["lat"], ap0["lon"])
            st.session_state.waypoints = [{
                "name": icao0,
                "lat": ap0["lat"],
                "lon": ap0["lon"],
                "alt": elev,
                "elev": elev,
                "arr_type": "Direct",
            }]
            st.session_state.map_center = [ap0["lat"], ap0["lon"]]
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
        st.session_state.map_center = None
        st.session_state.map_zoom = 9
        st.rerun()

mission_placeholder = st.container()
weather_placeholder = st.container()

# ─── MÉTÉO ───
metar_val = ""
taf_val = ""
if st.session_state.waypoints:
    dep_icao = st.session_state.waypoints[0]["name"]
    dep_name = AIRPORTS.get(dep_icao, {}).get("name", dep_icao)
    metar_val = get_metar_cached(dep_icao, st.session_state.wx_refresh)
    taf_val = get_taf_cached(dep_icao, st.session_state.wx_refresh)
    arr_candidate = get_arrival_metar_candidate(st.session_state.waypoints, dep_icao)

    with weather_placeholder.container():
        st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">🌦️ Météo</h3></div>', unsafe_allow_html=True)
        col_wx1, col_wx2 = st.columns(2)
        with col_wx1:
            st.markdown(f'<div class="sa-card"><h4>Départ — {dep_name} ({dep_icao})</h4><p>{metar_val}</p></div>', unsafe_allow_html=True)
        with col_wx2:
            if arr_candidate:
                arr_metar = get_metar_cached(arr_candidate["icao"], st.session_state.wx_refresh)
                st.markdown(f'<div class="sa-card"><h4>{arr_candidate["label"]} — {arr_candidate["name"]} ({arr_candidate["icao"]})</h4><p>{arr_metar}</p></div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="sa-card"><h4>Arrivée</h4><p>Aucun METAR distinct à afficher.</p></div>', unsafe_allow_html=True)
        with st.expander(f"📄 TAF départ — {dep_icao}", expanded=False):
            st.code(taf_val, language="text")

# ─── NAVIGATION & CARTE ───
col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">📍 Ajouter Segment</h3></div>', unsafe_allow_html=True)
    rv_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, step=1)
    st.caption(f"Route affichée : {fmt_hdg3(rv_in)}°")
    dist_in = st.number_input("Distance (NM)", 0.1, 300.0, 15.0, step=0.1)
    alt_in = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Auto", True)
    m_wind = None
    if not use_auto:
        m_wind = {
            "wd": st.number_input("Dir", 0, 359, 0, step=1),
            "ws": st.number_input("Force", 0, 100, 0, step=1),
        }

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065
        brng = math.radians(rv_in)
        la1, lo1 = math.radians(last["lat"]), math.radians(last["lon"])
        la2 = math.degrees(la1 + (dist_in / R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in / R) * math.sin(brng) / max(1e-9, math.cos(la1)))
        elev2 = get_elevation_ft(la2, lo2)

        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": la2,
            "lon": lo2,
            "tc": int(rv_in),
            "dist": float(dist_in),
            "alt": int(alt_in),
            "manual_wind": m_wind,
            "elev": elev2,
            "arr_type": "Direct",
        })

        st.session_state.map_center = [la2, lo2]
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        if st.session_state.map_style == "openAIP" and not OPENAIP_API_KEY:
            st.warning("OPENAIP_API_KEY manquante : openAIP indisponible. Bascule sur Satellite ou configure la clé.")

        m = build_map(
            st.session_state.waypoints,
            st.session_state.map_style,
            center=st.session_state.map_center,
            zoom=st.session_state.map_zoom,
        )

        st_folium(
            m,
            width=None,
            height=420,
            key="map_v59_stateful",
            returned_objects=[],
        )

# ─── LOG + PROFIL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    now_utc = dt.datetime.now(dt.timezone.utc)
    dep_dt = dt.datetime.combine(now_utc.date(), dep_time, tzinfo=dt.timezone.utc)

    nav_data = []
    dist_p = [0.0]
    elev0 = float(st.session_state.waypoints[0].get("elev", 0))
    if elev0 <= 0:
        elev_try = get_elevation_ft(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])
        if elev_try > 0:
            elev0 = float(elev_try)
            st.session_state.waypoints[0]["elev"] = elev0

    alt_p = [elev0]
    terr_p = [elev0]
    d_total = 0.0
    fig = go.Figure()
    current_alt = elev0
    wind_local_cache = {}
    decl_local_cache = {}
    cum_sec = 0.0
    fuel_total = 0.0

    with ThreadPoolExecutor(max_workers=4) as executor:
        for i in range(1, len(st.session_state.waypoints)):
            w1, w2 = st.session_state.waypoints[i - 1], st.session_state.waypoints[i]
            rv = float(w2.get("tc", 0))
            dist_nm = float(w2.get("dist", 0))
            alt_ft = float(w2.get("alt", 0))
            elev2 = float(w2.get("elev", 0))
            manual = w2.get("manual_wind", None)

            target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
            lv = PRESSURE_MAP[target]
            wkey = (round(w2["lat"], 2), round(w2["lon"], 2), lv, st.session_state.wx_refresh)
            dkey = (round(w2["lat"], 2), round(w2["lon"], 2), dep_dt.date().isoformat())

            future_elev = None
            future_wind = None
            future_decl = None

            if elev2 <= 0:
                future_elev = executor.submit(get_elevation_ft, w2["lat"], w2["lon"])
            if manual:
                wd, ws, src = float(manual["wd"]), float(manual["ws"]), "Manuel"
            elif wkey in wind_local_cache:
                wd, ws, src = wind_local_cache[wkey]
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
            if dkey in decl_local_cache:
                decl = decl_local_cache[dkey]
            else:
                future_decl = executor.submit(get_declination_deg, float(w2["lat"]), float(w2["lon"]), dep_dt)

            if future_elev is not None:
                elev_try = future_elev.result()
                if elev_try > 0:
                    elev2 = float(elev_try)
                    w2["elev"] = elev2

            if future_wind is not None:
                wd, ws, src = future_wind.result()
                wind_local_cache[wkey] = (wd, ws, src)

            if future_decl is not None:
                decl = future_decl.result()
                decl_local_cache[dkey] = decl

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
            tt_str = ""

            if alt_ft > current_alt:
                t_climb = ((alt_ft - current_alt) / max(1e-9, float(v_climb))) * 60.0
                d_climb = gs * (t_climb / 3600.0)
                if d_climb > 0.1:
                    t_cl_str = f"{int(t_climb//60):02d}:{int(t_climb%60):02d}"
                    tt_str += f"TOC:{round(d_climb,1)}NM "
                    if d_climb < dist_nm:
                        x_toc = d_total + d_climb
                        dist_p.append(x_toc)
                        alt_p.append(alt_ft)
                        terr_p.append(float(w1.get("elev", 0)))
                        fig.add_annotation(x=x_toc, y=alt_ft, text=f"TOC {round(d_climb,1)}NM ({t_cl_str})", showarrow=True, ay=45)

            at = w2.get("arr_type", "Direct")
            if (i == len(st.session_state.waypoints) - 1) and at == "Direct":
                at = "VT (1500ft)"

            if at != "Direct":
                alt_t = elev2 + (1500 if "VT" in at else 1000)
                t_desc = ((alt_ft - alt_t) / max(1e-9, float(v_descent))) * 60.0 if alt_ft > alt_t else 0.0
                d_desc = gs * (t_desc / 3600.0)
                if d_desc > 0.1:
                    t_de_str = f"{int(t_desc//60):02d}:{int(t_desc%60):02d}"
                    tt_str += f"TOD:{round(d_desc,1)}NM"
                    if d_desc < dist_nm:
                        x_tod = d_total + (dist_nm - d_desc)
                        dist_p.append(x_tod)
                        alt_p.append(alt_ft)
                        terr_p.append(elev2)
                        fig.add_annotation(x=x_tod, y=alt_ft, text=f"TOD {round(d_desc,1)}NM ({t_de_str})", showarrow=True, ay=-45)

                label_dest = "VT" if "VT" in at else "TDP"
                fig.add_annotation(
                    x=d_total + dist_nm,
                    y=alt_t,
                    text=f"<b>{label_dest} {w2['name']}</b>",
                    showarrow=False,
                    yshift=15,
                    font=dict(color="orange", size=11),
                )
                d_total += dist_nm
                dist_p.append(d_total)
                alt_p.append(alt_t)
                terr_p.append(elev2)
                dist_p.append(d_total)
                alt_p.append(elev2)
                terr_p.append(elev2)
                fig.add_vline(x=d_total, line_width=2, line_dash="dash", line_color="orange")
                current_alt = elev2
            else:
                d_total += dist_nm
                dist_p.append(d_total)
                alt_p.append(alt_ft)
                terr_p.append(elev2)
                current_alt = alt_ft

            drift_txt = f"{wca:+.0f}°"
            cap_txt = f"{fmt_hdg3(cap_mag)} ({drift_txt})"

            rv_i = int(round(rv)) % 360
            d_i = int(round(wca))
            cv_i = int(round(cap_vrai)) % 360
            dm_i = int(round(decl))
            cm_i = int(round(cap_mag)) % 360

            nav_data.append({
                "Branche": f"{w1['name']}➔{w2['name']}",
                "Vent": f"{int(wd)}/{int(ws)}kt ({src})",
                "GS": f"{int(gs)}kt",
                "EET": f"{int(seg_sec//60):02d}:{int(seg_sec%60):02d}",
                "Fuel": f"{fuel_branch:.1f}L",
                "TOC/TOD": tt_str.strip(),
                "Arrivée": at,
                "❌": False,
                "_idx": i,
                "ETA": eta_dt.strftime("%H:%M"),
                "Cap": cap_txt,
                "Rv": f"{rv_i:03d}",
                "d": f"{d_i:+d}",
                "Cv": f"{cv_i:03d}",
                "dm": f"{dm_i:+d}",
                "Cm": f"{cm_i:03d}",
                "Déviation": "",
                "Cc": "",
            })

    df_nav = pd.DataFrame(nav_data)

    with mission_placeholder.container():
        st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">🧭 Mission</h3></div>', unsafe_allow_html=True)
        card = st.container(border=True)
        with card:
            st.caption(summarize_route_names(st.session_state.waypoints))
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Distance totale", f"{d_total:.1f} NM")
            m2.metric("Temps total", format_hhmm_from_seconds(cum_sec))
            m3.metric("Carburant total", f"{fuel_total:.1f} L")
            m4.metric("ETA arrivée", df_nav.iloc[-1]["ETA"] if len(df_nav) else "--:--")

    st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">📋 Log de Navigation</h3></div>', unsafe_allow_html=True)
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
            "Arrivée": st.column_config.SelectboxColumn("Arrivée", options=["Direct", "TDP (1000ft)", "VT (1500ft)"], width="small"),
            "❌": st.column_config.CheckboxColumn("❌", width="small"),
            "_idx": None,
        },
        hide_index=True,
    )

    if edited_log.to_dict("records") != df_screen.to_dict("records"):
        new_wps = [st.session_state.waypoints[0]]
        for _, row in edited_log.iterrows():
            if not row["❌"]:
                wp = st.session_state.waypoints[int(row["_idx"])].copy()
                wp["arr_type"] = row["Arrivée"]
                branche_txt = str(row["Branche"])
                if "➔" in branche_txt:
                    wp["name"] = branche_txt.split("➔", 1)[1].strip()
                elif "->" in branche_txt:
                    wp["name"] = branche_txt.split("->", 1)[1].strip()
                new_wps.append(wp)
        st.session_state.waypoints = new_wps
        st.rerun()

    df_pdf = df_nav[
        [
            "Branche",
            "Vent",
            "GS",
            "EET",
            "Fuel",
            "TOC/TOD",
            "Arrivée",
            "Rv",
            "d",
            "Cv",
            "dm",
            "Cm",
            "Déviation",
            "Cc",
        ]
    ].copy()

    st.download_button(
        label="📥 Log PDF",
        data=create_pdf(df_pdf, metar_val),
        file_name="nav_log.pdf",
        use_container_width=True,
    )

    fig.add_trace(go.Scatter(x=dist_p, y=terr_p, fill="tozeroy", name="Relief", line_color="sienna"))
    fig.add_trace(go.Scatter(x=dist_p, y=alt_p, name="Profil Avion", line=dict(color="royalblue", width=4)))
    fig.update_layout(
        width=1000,
        height=350,
        xaxis=dict(fixedrange=True, tickformat=".1f", title="Distance (NM)"),
        yaxis=dict(fixedrange=True, title="Altitude (ft)"),
        margin=dict(l=40, r=40, t=20, b=40),
        showlegend=False,
    )
    st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.4rem;">📈 Profil vertical</h3></div>', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=False, config={"staticPlot": True, "displayModeBar": False})
