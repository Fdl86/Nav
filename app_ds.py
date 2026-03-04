import streamlit as st
import requests
import pandas as pd
import datetime as dt
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go
from fpdf import FPDF
from dataclasses import dataclass, replace
from typing import Optional, Dict, List, Tuple

# ─── CONFIGURATION ───────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
CACHE_TTL = 86400  
EARTH_RADIUS_NM = 3440.065  

@dataclass(frozen=True)
class Waypoint:
    name: str
    lat: float
    lon: float
    alt: int
    elev: int
    arr_type: str = "Direct"
    tc: Optional[int] = None
    dist: Optional[float] = None
    manual_wind: Optional[Dict] = None

@dataclass
class WindData:
    direction: int
    speed: int
    source: str

# ─── CHARGEMENT ET CACHE ────────────────────────────────────────────────
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_airports():
    base = {"LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069}}
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", 
                         usecols=['ident', 'name', 'latitude_deg', 'longitude_deg', 'iso_country', 'type'])
        fr = df[(df['iso_country'] == 'FR') & (df['type'].str.contains('airport'))]
        base.update(fr.set_index('ident')[['name', 'latitude_deg', 'longitude_deg']].rename(
            columns={'latitude_deg': 'lat', 'longitude_deg': 'lon'}).to_dict('index'))
    except: pass
    return base

@st.cache_data(ttl=CACHE_TTL)
def get_elevation_ft(lat, lon):
    try:
        r = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}, timeout=3).json()
        return round(r.get("elevation", [0])[0] * 3.28084)
    except: return 0

@st.cache_data(ttl=3600)
def get_metar(icao):
    try:
        r = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT", timeout=3)
        return r.text.split('\n')[1] if r.status_code == 200 else "Indisponible"
    except: return "Erreur"

# ─── LOGIQUE MÉTIER ─────────────────────────────────────────────────────
def get_wind_data(lat, lon, alt_ft, time_dt, manual_wind=None) -> WindData:
    if manual_wind: return WindData(manual_wind['wd'], manual_wind['ws'], "Manuel")
    lv = PRESSURE_MAP[min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))]
    params = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
              "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless", "wind_speed_unit": "kn", "timezone": "UTC"}
    try:
        d = requests.get(OPEN_METEO_URL, params=params, timeout=5).json()
        h = d.get("hourly", {})
        for m, src in [("icon_d2", "ICON-D2"), ("meteofrance_arome_france_hd", "AROME"), ("gfs_seamless", "GFS")]:
            sk = f"wind_speed_{lv}hPa_{m}"
            if sk in h:
                idx = min(range(len(h["time"])), key=lambda k: abs(dt.datetime.fromisoformat(h["time"][k]).replace(tzinfo=dt.timezone.utc) - time_dt))
                return WindData(int(round(h[f"wind_direction_{lv}hPa_{m}"][idx])), int(round(h[sk][idx])), src)
    except: pass
    return WindData(0, 0, "N/A")

def calculate_nav(wind_dir, wind_speed, track, tas):
    if wind_speed == 0 or tas == 0: return 0.0, float(tas)
    wa = math.radians(wind_dir - track)
    sw = (wind_speed / tas) * math.sin(wa)
    wca = math.degrees(math.asin(sw)) if abs(sw) <= 1 else 0
    gs = max(20, (tas * math.cos(math.radians(wca))) - (wind_speed * math.cos(wa)))
    return wca, gs

def format_mm_ss(minutes: float) -> str:
    m = int(minutes)
    s = int((minutes - m) * 60)
    return f"{m:02d}:{s:02d}"

# ─── RENDU PROFIL & LOG ─────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def create_flight_profile(waypoints: Tuple[Waypoint], tas, v_climb, v_descent, fuel_flow):
    if len(waypoints) < 2: return None, None
    curr_t = dt.datetime.now(dt.timezone.utc)
    nav_data, dist_p, alt_p, terr_p = [], [0], [waypoints[0].elev], [waypoints[0].elev]
    total_dist, curr_alt = 0.0, waypoints[0].elev
    fig = go.Figure()

    for i in range(1, len(waypoints)):
        w1, w2 = waypoints[i-1], waypoints[i]
        wind = get_wind_data(w2.lat, w2.lon, w2.alt, curr_t, w2.manual_wind)
        wca, gs = calculate_nav(wind.direction, wind.speed, w2.tc or 0, tas)
        hrs = w2.dist / gs if w2.dist else 0
        txt = ""

        if w2.alt > curr_alt:
            cl_t = ((w2.alt - curr_alt) / v_climb)
            cl_d = (gs * (cl_t / 60))
            if cl_d > 0.1:
                txt += f"TOC:{round(cl_d,1)}NM ({format_mm_ss(cl_t)}) "
                if cl_d < w2.dist:
                    dist_p.append(total_dist + cl_d); alt_p.append(w2.alt); terr_p.append(w1.elev)
                    fig.add_annotation(x=total_dist+cl_d, y=w2.alt, text=f"TOC {round(cl_d,1)}NM", showarrow=True, ay=40)

        arr = w2.arr_type
        if i == len(waypoints) - 1 and arr == "Direct": arr = "VT (1500ft)"
        
        if arr != "Direct":
            tgt = w2.elev + (1500 if "VT" in arr else 1000)
            if w2.alt > tgt:
                de_t = ((w2.alt - tgt) / v_descent)
                de_d = (gs * (de_t / 60))
                if de_d > 0.1 and de_d < w2.dist:
                    dist_p.append(total_dist + (w2.dist - de_d)); alt_p.append(w2.alt); terr_p.append(w2.elev)
                    fig.add_annotation(x=total_dist+(w2.dist-de_d), y=w2.alt, text=f"TOD {round(de_d,1)}NM", showarrow=True, ay=-40)
            
            total_dist += w2.dist
            dist_p.extend([total_dist, total_dist])
            alt_p.extend([tgt, w2.elev])
            terr_p.extend([w2.elev, w2.elev])
            fig.add_vline(x=total_dist, line_width=2, line_dash="dash", line_color="orange")
            curr_alt = w2.elev
        else:
            total_dist += w2.dist
            dist_p.append(total_dist); alt_p.append(w2.alt); terr_p.append(w2.elev)
            curr_alt = w2.alt

        nav_data.append({
            "Branche": f"{w1.name}➔{w2.name}", "Vent": f"{wind.direction}/{wind.speed}kt ({wind.source})",
            "GS": f"{int(gs)}kt", "EET": f"{int(hrs*60//60):02d}:{int(hrs*60%60):02d}",
            "Fuel": f"{round(hrs * fuel_flow, 1)}L", "TOC/TOD": txt.strip(), "Arrivée": arr, "_idx": i
        })

    fig.add_trace(go.Scatter(x=dist_p, y=terr_p, fill='tozeroy', name='Relief', line_color='sienna'))
    fig.add_trace(go.Scatter(x=dist_p, y=alt_p, name='Profil', line=dict(color='royalblue', width=4)))
    fig.update_layout(title="Profil Altimétrique du Vol", height=350, margin=dict(t=50), xaxis_title="Distance (NM)", yaxis_title="Altitude (ft)")
    return pd.DataFrame(nav_data), fig

# ─── INTERFACE STREAMLIT ────────────────────────────────────────────────
st.set_page_config(page_title="SkyAssistant V47", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []
AIRPORTS = load_airports()

with st.sidebar:
    st.title("✈️ SkyAssistant")
    search = st.text_input("🔍 Rechercher OACI").upper()
    if search in AIRPORTS and st.button(f"Définir Départ : {search}"):
        a = AIRPORTS[search]
        e = get_elevation_ft(a['lat'], a['lon'])
        st.session_state.waypoints = [Waypoint(search, a['lat'], a['lon'], e, e)]
        st.rerun()
    
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 800, step=10)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500, step=10)
    fuel_flow = st.number_input("Conso (L/h)", 5.0, 100.0, 25.0, step=1.0)
    
    if st.button("🗑️ Reset Complet"):
        st.session_state.waypoints = []
        st.rerun()

col_map, col_ctrl = st.columns([2, 1])

with col_map:
    if st.session_state.waypoints:
        wps = st.session_state.waypoints
        m = folium.Map(location=[wps[0].lat, wps[0].lon], zoom_start=9)
        folium.TileLayer('openstreetmap', name='Standard').add_to(m)
        folium.TileLayer('https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', attr='Google', name='Satellite').add_to(m)
        folium.PolyLine([[w.lat, w.lon] for w in wps], color="red", weight=3).add_to(m)
        for i, w in enumerate(wps):
            ic, it = ("blue", "plane") if i==0 else (("red", "flag") if i==len(wps)-1 else ("orange", "dot-circle-o"))
            folium.Marker([w.lat, w.lon], popup=w.name, icon=folium.Icon(color=ic, icon=it, prefix="fa")).add_to(m)
        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=400, key="map", returned_objects=[])

with col_ctrl:
    st.subheader("📍 Nouveau Segment")
    tc = st.number_input("Route Vraie (Rv)°", 0, 359, 0)
    dist = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt = st.number_input("Alt Croisière (ft)", 1000, 12000, 3000, 500)
    if st.button("➕ Ajouter Segment") and st.session_state.waypoints:
        l = st.session_state.waypoints[-1]
        from math import radians, degrees, sin, cos
        nl = degrees(radians(l.lat) + (dist/EARTH_RADIUS_NM) * cos(radians(tc)))
        nlo = degrees(radians(l.lon) + (dist/EARTH_RADIUS_NM) * sin(radians(tc)) / cos(radians(l.lat)))
        st.session_state.waypoints.append(Waypoint(f"WP{len(wps)}", nl, nlo, alt, get_elevation_ft(nl, nlo), "Direct", tc, dist))
        st.rerun()
    
    if len(st.session_state.waypoints) > 1:
        if st.button("❌ Supprimer dernier WP"):
            st.session_state.waypoints.pop()
            st.rerun()

if len(st.session_state.waypoints) > 1:
    st.markdown("### 📋 Log de Navigation")
    df_nav, fig = create_flight_profile(tuple(st.session_state.waypoints), tas, v_climb, v_descent, fuel_flow)
    
    if df_nav is not None:
        ed_log = st.data_editor(df_nav, hide_index=True, use_container_width=True,
            column_config={
                "Branche": st.column_config.TextColumn("Branche"),
                "Arrivée": st.column_config.SelectboxColumn("Arrivée", options=["Direct", "TDP (1000ft)", "VT (1500ft)"]),
                "Vent": st.column_config.TextColumn(disabled=True),
                "GS": st.column_config.TextColumn(disabled=True),
                "EET": st.column_config.TextColumn(disabled=True),
                "Fuel": st.column_config.TextColumn(disabled=True),
                "TOC/TOD": st.column_config.TextColumn(disabled=True),
                "_idx": None
            })
        
        if not ed_log.equals(df_nav):
            new_wps = [st.session_state.waypoints[0]]
            for _, row in ed_log.iterrows():
                orig = st.session_state.waypoints[row['_idx']]
                new_wps.append(replace(orig, arr_type=row['Arrivée'], name=row['Branche'].split('➔')[-1]))
            st.session_state.waypoints = new_wps
            st.rerun()

        st.plotly_chart(fig, use_container_width=True)
