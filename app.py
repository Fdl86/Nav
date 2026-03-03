import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium

# ─── CONFIGURATION & PALIERS DE PRESSION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

# Mapping pour coller aux couches de pression VFR (1 hPa ≈ 28 ft)
PRESSURE_MAP = {
    1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 
    3500: 885, 4500: 865, 5000: 850, 5500: 835, 7500: 775, 9500: 725
}

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except: return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

# ─── MOTEUR MÉTÉO V21 : CASCADE ANTI-ZÉRO ───
def get_wind_v21(lat, lon, alt_ft, time_dt):
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    
    # On interroge les 3 sources majeures en une seule requête
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1
    }
    
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        h = r.get("hourly", {})
        
        # 1. Priorité ICON-D2 (Le plus réactif au Nord/Centre)
        ws_icon = h.get(f"wind_speed_{lv}hPa_icon_d2", [None])[0]
        if ws_icon is not None and ws_icon > 0.5:
            ws, wd, src = h[f"wind_speed_{lv}hPa_icon_d2"], h[f"wind_direction_{lv}hPa_icon_d2"], "ICON-D2"
        
        # 2. Sinon AROME HD (Spécialiste France, couvre Castelnaudary)
        elif h.get(f"wind_speed_{lv}hPa_meteofrance_arome_france_hd", [None])[0] is not None:
            ws, wd, src = h[f"wind_speed_{lv}hPa_meteofrance_arome_france_hd"], h[f"wind_direction_{lv}hPa_meteofrance_arome_france_hd"], "AROME HD"
        
        # 3. Dernier recours : GFS Global (Ne renvoie jamais null)
        else:
            ws, wd, src = h[f"wind_speed_{lv}hPa_gfs_seamless"], h[f"wind_direction_{lv}hPa_gfs_seamless"], "GFS Global"

        run_time = h.get("time", ["N/A"])[0]
        idx = min(range(len(h["time"])), key=lambda k: abs(datetime.fromisoformat(h["time"][k]) - time_dt))
        
        return wd[idx], ws[idx], run_time, src
    except:
        return 0, 0, "Erreur", "N/A"

# ─── CALCULS AÉRONAUTIQUES ───
def calculate_gs_and_wca(tas, tc, wd, ws):
    if wd is None or ws is None: return tas, 0
    wa = math.radians(wd - tc)
    sin_wca = (ws/tas)*math.sin(wa)
    if abs(sin_wca) > 1: return 20, 0
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    brng, la1, lo1 = math.radians(bearing), math.radians(lat), math.radians(lon)
    la2 = la1 + (dist_nm/R) * math.cos(brng)
    q = math.cos(la1) if abs(la2-la1) < 1e-10 else (la2-la1) / math.log(math.tan(la2/2 + math.pi/4)/math.tan(la1/2 + math.pi/4))
    lo2 = lo1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(la2), math.degrees(lo2)

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V21", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("🛠️ Flight Planning")
    oaci = st.text_input("Code OACI Départ", "").upper()
    if oaci in AIRPORTS:
        st.success(f"📍 {AIRPORTS[oaci]['name']}")
        if st.button("🚀 Initialiser le vol"):
            ap = AIRPORTS[oaci]
            st.session_state.waypoints = [{"name": oaci, "lat": ap["lat"], "lon": ap["lon"], "alt": 2500}]
            st.rerun()
    
    st.markdown("---")
    tas = st.number_input("TAS (Vitesse Propre) kt", 50, 250, 100)
    conso = st.number_input("Consommation (L/h)", 5, 100, 22)
    if st.button("🗑️ Tout effacer"):
        st.session_state.waypoints = []; st.rerun()

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt_in = st.number_input("Altitude (ft)", 1000, 12500, 2500, step=500)
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("➕ Ajouter") and st.session_state.waypoints:
            l = st.session_state.waypoints[-1]
            n_la, n_lo = calculate_destination(l["lat"], l["lon"], tc_in, dist_in)
            st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": round(n_la, 4), "lon": round(n_lo, 4), "tc": tc_in, "dist": dist_in, "alt": alt_in})
            st.rerun()
    with c2:
        if len(st.session_state.waypoints) > 1:
            if st.button("⬅️ Supprimer"):
                st.session_state.waypoints.pop(); st.rerun()

with col_map:
    if st.session_state.waypoints:
        start_pt = [st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]]
        m = folium.Map(location=start_pt, zoom_start=9)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)
        for w in st.session_state.waypoints: folium.Marker([w["lat"], w["lon"]], tooltip=w['name']).add_to(m)
        st_folium(m, width="100%", height=350, key="map_v21")
    else: st.info("Entrez un code OACI de départ.")

# ─── LOG DE NAVIGATION FINAL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    # Calcul déclinaison magnétique approximative
    mv = round(-1.2 - (st.session_state.waypoints[0]["lon"] * 0.35) + (st.session_state.waypoints[0]["lat"] * 0.05), 1)
    
    nav_data = []
    total_min, total_dist = 0, 0
    
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws, run, model_name = get_wind_v21(w2["lat"], w2["lon"], w2["alt"], curr_t)
        gs, wca = calculate_gs_and_wca(tas, w2["tc"], wd, ws)
        eet = (w2["dist"]/gs)*60
        total_min += eet; total_dist += w2["dist"]
        
        nav_data.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Alt": f"{w2['alt']}ft",
            "Vent": f"{int(wd)}/{int(ws)}kt",
            "Cm (Cap Mag)": f"{int((w2['tc']-wca-mv)%360):03d}°",
            "GS": f"{int(gs)}kt",
            "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}",
            "Modèle": model_name,
            "Run (UTC)": run
        })

    st.subheader("📋 Log de Navigation Intelligent")
    st.table(pd.DataFrame(nav_data))
    
    fuel_total = round((total_min/60)*conso + 10, 1)
    st.success(f"**BILAN : {total_dist:.1f} NM | {int(total_min)} minutes | Fuel : {fuel_total} L (incl. forfait 10L)**")
