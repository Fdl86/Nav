import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

PRESSURE_MAP = {1000:975, 1500:960, 2000:950, 2500:925, 3000:900, 5000:850, 7000:750}

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport']))]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except: return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

def get_elevation(lat, lon):
    try:
        r = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}).json()
        return r.get("elevation", [0])[0]
    except: return 0

def get_metar(icao):
    try:
        r = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT")
        return r.text.split('\n')[1] if r.status_code == 200 else "METAR indisponible"
    except: return "Erreur METAR"

def get_wind_v23(lat, lon, alt_ft, time_dt, manual_wind=None):
    if manual_wind: return manual_wind['wd'], manual_wind['ws'], "Manuel", "User"
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    params = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
              "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless", "wind_speed_unit": "kn", "timezone": "UTC"}
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        h = r.get("hourly", {})
        if h.get(f"wind_speed_{lv}hPa_icon_d2", [None])[0]: ws, wd, src = h[f"wind_speed_{lv}hPa_icon_d2"], h[f"wind_direction_{lv}hPa_icon_d2"], "ICON-D2"
        elif h.get(f"wind_speed_{lv}hPa_meteofrance_arome_france_hd", [None])[0]: ws, wd, src = h[f"wind_speed_{lv}hPa_meteofrance_arome_france_hd"], h[f"wind_direction_{lv}hPa_meteofrance_arome_france_hd"], "AROME HD"
        else: ws, wd, src = h[f"wind_speed_{lv}hPa_gfs_seamless"], h[f"wind_direction_{lv}hPa_gfs_seamless"], "GFS"
        idx = min(range(len(h["time"])), key=lambda k: abs(datetime.fromisoformat(h["time"][k]) - time_dt))
        return wd[idx], ws[idx], h["time"][0], src
    except: return 0, 0, "N/A", "Err"

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V23", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("✈️ SkyAssistant V23")
    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []
    if sugg:
        st.info(f"Probable : {sugg[0]} - {AIRPORTS[sugg[0]]['name']}")
        if st.button(f"Initialiser {sugg[0]}"):
            ap = AIRPORTS[sugg[0]]
            elev = get_elevation(ap['lat'], ap['lon'])
            st.session_state.waypoints = [{"name": sugg[0], "lat": ap['lat'], "lon": ap['lon'], "alt": elev, "elev": elev}]
            st.rerun()

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    conso = st.number_input("Conso (L/h)", 5, 100, 22)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 500)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500)
    show_profile = st.toggle("Afficher Profil Vertical (Coupe)", True)
    if st.button("🗑️ Reset"): st.session_state.waypoints = []; st.rerun()

# ─── NAVIGATION ───
if st.session_state.waypoints:
    st.code(f"🕒 METAR {st.session_state.waypoints[0]['name']} : {get_metar(st.session_state.waypoints[0]['name'])}", language="bash")

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Segments")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt_in = st.number_input("Altitude Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Automatique", True)
    m_wind = None if use_auto else {'wd': st.number_input("Dir", 0, 359), 'ws': st.number_input("Force", 0, 100)}

    if st.button("➕ Ajouter Segment") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065
        brng, la1, lo1 = math.radians(tc_in), math.radians(last["lat"]), math.radians(last["lon"])
        la2 = math.degrees(la1 + (dist_in/R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in/R) * math.sin(brng) / math.cos(la1))
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": la2, "lon": lo2, "tc": tc_in, "dist": dist_in, "alt": alt_in, "manual_wind": m_wind, "elev": get_elevation(la2, lo2)})
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)
        st_folium(m, width="100%", height=350, key="map_v23")

# ─── CALCULS & PROFIL VERTICAL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    mv = round(-1.2 - (st.session_state.waypoints[0]["lon"] * 0.35) + (st.session_state.waypoints[0]["lat"] * 0.05), 1)
    
    nav_data, dist_cumul, altitudes, terrains = [], [0], [], []
    altitudes.append(st.session_state.waypoints[0]["elev"])
    terrains.append(st.session_state.waypoints[0]["elev"])

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws, run, src = get_wind_v23(w2["lat"], w2["lon"], w2["alt"], curr_t, w2.get("manual_wind"))
        
        # Calcul GS & WCA
        wa = math.radians(wd - w2["tc"])
        sin_wca = (ws/tas)*math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0
        gs = max(20, (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa)))
        
        # TOC (Top of Climb) : Depuis l'élévation du point précédent
        alt_diff_climb = w2["alt"] - w1["elev"]
        dist_climb = round((gs * (alt_diff_climb / v_climb) / 60), 1) if alt_diff_climb > 0 else 0
        
        # TOD (Top of Descent) : Pour arriver à elev + 1000ft
        alt_diff_desc = w2["alt"] - (w2["elev"] + 1000)
        dist_desc = round((gs * (alt_diff_desc / v_descent) / 60), 1) if alt_diff_desc > 0 else 0

        eet = (w2["dist"]/gs)*60
        nav_data.append({"Branche": f"{w1['name']}➔{w2['name']}", "Alt": f"{w2['alt']}ft", "Vent": f"{int(wd)}/{int(ws)} ({src})", "GS": f"{int(gs)}kt", "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}", "TOC/TOD": f"TOC: {dist_climb}NM | TOD: {dist_desc}NM avant"})
        
        dist_cumul.append(dist_cumul[-1] + w2["dist"])
        altitudes.append(w2["alt"])
        terrains.append(w2["elev"])

    st.subheader("📋 Log de Navigation & Profil de Vol")
    st.table(pd.DataFrame(nav_data))

    if show_profile:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=dist_cumul, y=terrains, fill='tozeroy', name='Terrain', line_color='sienna'))
        fig.add_trace(go.Scatter(x=dist_cumul, y=altitudes, name='Altitude de vol', line=dict(color='royalblue', width=4, dash='dash')))
        fig.update_layout(title="Profil Vertical de la Navigation (Coupe)", xaxis_title="Distance (NM)", yaxis_title="Altitude (ft)", height=300)
        st.plotly_chart(fig, use_container_width=True)
