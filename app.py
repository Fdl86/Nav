import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium
from fpdf import FPDF

# ─── CONFIG & DATA ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

# Mapping pour coller aux couches de pression
PRESSURE_MAP = {1000: 975, 2000: 950, 2500: 925, 3000: 900, 4000: 875, 5000: 850, 7000: 750, 10000: 700}

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except: return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

# ─── FONCTIONS TECHNIQUES ───
def get_elevation(lat, lon):
    try:
        res = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}).json()
        return res.get("elevation", [0])[0]
    except: return 0

def get_magnetic_declination(lat, lon):
    return round(-1.2 - (lon * 0.35) + (lat * 0.05), 1)

def calculate_gs_and_wca(tas, tc, wd, ws):
    if wd is None or ws is None: return tas, 0
    wa = math.radians(wd - tc)
    sin_wca = (ws/tas)*math.sin(wa)
    if abs(sin_wca) > 1: return 20, 0
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

def get_wind(lat, lon, alt_ft, time_dt):
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    
    # On tente AROME HD en premier
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "meteofrance_arome_france_hd",
        "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1
    }
    
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        
        # Si AROME HD renvoie du vide ou une erreur, on bascule sur le modèle "Seamless"
        if "hourly" not in r or r["hourly"][f"wind_speed_{lv}hPa"][0] is None:
            params["models"] = "meteofrance_seamless"
            r = requests.get(OPEN_METEO_URL, params=params).json()
            
        idx = min(range(len(r["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(r["hourly"]["time"][k]) - time_dt))
        wd = r["hourly"][f"wind_direction_{lv}hPa"][idx]
        ws = r["hourly"][f"wind_speed_{lv}hPa"][idx]
        
        return (wd, ws) if wd is not None else (0, 0)
    except:
        return 0, 0

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    brng, la1, lo1 = math.radians(bearing), math.radians(lat), math.radians(lon)
    la2 = la1 + (dist_nm/R) * math.cos(brng)
    q = math.cos(la1) if abs(la2-la1) < 1e-10 else (la2-la1) / math.log(math.tan(la2/2 + math.pi/4)/math.tan(la1/2 + math.pi/4))
    lo2 = lo1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(la2), math.degrees(lo2)

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V14", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("🛠️ Paramètres")
    oaci = st.text_input("🔍 Code OACI Départ", "", key="start_oaci").upper()
    if oaci in AIRPORTS:
        st.success(f"📍 {AIRPORTS[oaci]['name']}")
        if st.button("🚀 Initialiser le départ", key="btn_init"):
            ap = AIRPORTS[oaci]
            st.session_state.waypoints = [{"name": oaci, "lat": ap["lat"], "lon": ap["lon"], "elev": get_elevation(ap["lat"], ap["lon"]), "alt": 2500}]
            st.rerun()
    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100, key="cfg_tas")
    conso = st.number_input("Conso (L/h)", 5, 100, 22, key="cfg_conso")
    optimize_global = st.checkbox("💡 Optimiseur d'Altitude", True, key="chk_opt")
    if st.button("🗑️ Reset", key="btn_reset"):
        st.session_state.waypoints = []; st.rerun()

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Segments")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, key="in_tc")
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0, key="in_dist")
    alt_in = st.number_input("Altitude (ft)", 1000, 12500, 2500, step=500, key="in_alt")
    if st.button("➕ Ajouter Branche", key="btn_add") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        n_lat, n_lon = calculate_destination(last["lat"], last["lon"], tc_in, dist_in)
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": round(n_lat, 4), "lon": round(n_lon, 4), "tc": tc_in, "dist": dist_in, "alt": alt_in, "elev": get_elevation(n_lat, n_lon)})
        st.rerun()
    if len(st.session_state.waypoints) > 1:
        if st.button("⬅️ Supprimer dernier", key="del_last"):
            st.session_state.waypoints.pop(); st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)
        for w in st.session_state.waypoints: folium.Marker([w["lat"], w["lon"]], tooltip=w['name']).add_to(m)
        st_folium(m, width="100%", height=350, key="map_v14", returned_objects=[])

# CALCULS
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    mv = get_magnetic_declination(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])

    # LOG DE NAV
    res_final, t_min, t_dist = [], 0, 0
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws = get_wind(w2["lat"], w2["lon"], w2["alt"], curr_t)
        gs, wca = calculate_gs_and_wca(tas, w2["tc"], wd, ws)
        eet = (w2["dist"]/gs)*60
        t_min += eet; t_dist += w2["dist"]; curr_t += timedelta(minutes=eet)
        res_final.append({
            "Branche": f"{w1['name']}➔{w2['name']}", 
            "Alt": f"{w2['alt']}ft", 
            "Vent": f"{int(wd)}/{int(ws)}", 
            "Cm": f"{int((w2['tc']-wca-mv)%360):03d}°", 
            "GS": f"{int(gs)}kt", 
            "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}"
        })

    st.subheader("📋 Log de Navigation")
    st.table(pd.DataFrame(res_final))
    
    # Message de debug si vent nul suspect
    if any(row["Vent"] == "0/0" for row in res_final):
        st.warning("⚠️ L'API renvoie un vent nul. Si c'est anormal (ex: LFMW), c'est que le modèle AROME est temporairement indisponible pour cette coordonnée.")

    fuel = round((t_min/60)*conso + 10, 1)
    st.success(f"**Distance : {t_dist:.1f} NM | Temps : {int(t_min)} min | Fuel : {fuel} L**")
