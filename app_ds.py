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

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
NOAA_DECL_URL = "https://www.ngdc.noaa.gov/geomag-web/calculators/calculateDeclination"
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
HTTP_TIMEOUT = 8
DIVERT_RADIUS_NM = 20.0
ARRIVAL_METAR_RADIUS_NM = 15.0

# ─── PAGE ───
st.set_page_config(page_title="SkyAssistant V56", layout="wide")

# ─── HIDE STREAMLIT DATAFRAME TOOLBAR ───
st.markdown(
    """
<style>
div[data-testid="stDataFrame"] [data-testid="stElementToolbar"],
div[data-testid="stDataEditor"] [data-testid="stElementToolbar"] {
    display: none !important;
}
</style>
""",
    unsafe_allow_html=True,
)

# ─── HTTP SESSION ───
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "SkyAssistant/56"})
    return s

SESSION = get_http_session()

# ─── STATE ───
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []
if "wx_refresh" not in st.session_state:
    st.session_state.wx_refresh = 0

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
            row["ident"]: {"name": row["name"], "lat": float(row["latitude_deg"]), "lon": float(row["longitude_deg"])}
            for _, row in fr.iterrows()
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

    lat_arr = df["lat"].values
    lon_arr = df["lon"].values

    df["d_nm"] = [
        haversine_nm(lat, lon, lat2, lon2)
        for lat2, lon2 in zip(lat_arr, lon_arr)
    ]

    df = df[df["d_nm"] <= radius_nm].sort_values("d_nm").head(k)
    return df[["icao", "name", "d_nm"]].to_dict("records")

def unique_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

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
    last_lat = last["lat"]
    last_lon = last["lon"]

    # On prend toujours le terrain LFxx le plus proche du dernier point réel de la route,
    # même si le waypoint a été renommé manuellement.
    nearby = nearest_airfields(last_lat, last_lon, radius_nm=ARRIVAL_METAR_RADIUS_NM, k=1, exclude_icao=None)
    if not nearby:
        return None

    candidate = nearby[0]
    candidate_icao = candidate["icao"]

    # Si on retombe sur le départ, inutile d'afficher un 2e METAR identique.
    if candidate_icao == dep_icao:
        return None

    return {
        "icao": candidate_icao,
        "name": candidate["name"],
        "label": "METAR arrivée",
    }

    last = waypoints[-1]
    last_name = str(last["name"]).upper().strip()
    if is_lf_icao(last_name) and last_name != dep_icao:
        return {"icao": last_name, "name": AIRPORTS.get(last_name, {}).get("name", last_name), "label": "METAR arrivée"}

    nearby = nearest_airfields(last["lat"], last["lon"], radius_nm=ARRIVAL_METAR_RADIUS_NM, k=1, exclude_icao=dep_icao)
    if nearby:
        return {"icao": nearby[0]["icao"], "name": nearby[0]["name"], "label": "METAR arrivée probable"}

    return None

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
        r = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            lines = r.text.splitlines()
            return lines[1] if len(lines) > 1 else "METAR indisponible"
        return "METAR indisponible"
    except Exception:
        return "Erreur METAR"

@st.cache_data(ttl=600)
def get_taf_cached(icao: str, wx_refresh: int) -> str:
    try:
        r = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{icao}.TXT",
            timeout=HTTP_TIMEOUT,
        )
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
        best_i = 0
        best_d = float("inf")
        for i, t in enumerate(times):
            ts = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp()
            d = abs(ts - t_target)
            if d < best_d:
                best_d = d
                best_i = i

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

    w = [30, 35, 15, 20, 15, 45, 30]
    cols = ["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]

    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for i in range(len(cols)):
        pdf.cell(w[i], 8, cols[i], border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for _, row in df_nav.iterrows():
        pdf.cell(w[0], 8, _pdf_safe(row.get("Branche", "")).replace("➔", "->"), border=1)
        pdf.cell(w[1], 8, _pdf_safe(row.get("Vent", "")), border=1)
        pdf.cell(w[2], 8, _pdf_safe(row.get("GS", "")), border=1, align="C")
        pdf.cell(w[3], 8, _pdf_safe(row.get("EET", "")), border=1, align="C")
        pdf.cell(w[4], 8, _pdf_safe(row.get("Fuel", "")), border=1, align="C")
        pdf.cell(w[5], 8, _pdf_safe(row.get("TOC/TOD", "")), border=1)
        pdf.cell(w[6], 8, _pdf_safe(row.get("Arrivée", "")), border=1)
        pdf.ln()

    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return out.encode("latin-1", "ignore")

# ─── SIDEBAR ───
with st.sidebar:
    st.title("✈️ SkyAssistant V56")

    if st.button("🔄 Rafraîchir météo", use_container_width=True):
        st.session_state.wx_refresh += 1
        st.rerun()

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
            st.rerun()

    if st.session_state.waypoints:
        with st.expander("🧾 Briefing", expanded=False):
            st.link_button("📌 SOFIA Briefing (NOTAM)", "https://sofia-briefing.aviation-civile.gouv.fr/sofia/pages/homepage.html")
            st.link_button("📚 SIA / Visualisateur AIP", "https://www.sia.aviation-civile.gouv.fr/vaip")
    
            st.markdown("---")
            st.caption("Briefing volontairement léger : météo et liens officiels, sans suggérer de déroutements.")
