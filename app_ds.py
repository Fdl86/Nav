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
ARRIVAL_METAR_RADIUS_NM = 15.0

# ─── PAGE ───
st.set_page_config(page_title="SkyAssistant V56.1", layout="wide")

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
    s.headers.update({"User-Agent": "SkyAssistant/56.1"})
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
    df["d_nm"] = [haversine_nm(lat, lon, lat2, lon2) for lat2,
