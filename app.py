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

# Mapping plus fin pour coller aux couches AROME (925hPa = ~2500ft)
PRESSURE_MAP = {
    1000: 975, 2000: 950, 2500: 925, 3000: 900, 
    4000: 875, 5000: 850, 6000: 800, 7000: 750, 8000: 700
}

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
    # Sécurité : si le vent est invalide, on renvoie les valeurs par défaut
    if wd is None or ws is None: return tas, 0
    wa = math.radians(wd - tc)
    sin_wca = (ws/tas)*math.sin(wa)
    if abs(sin_wca) > 1: return 20, 0
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

def get_wind(lat, lon, alt_ft, time_dt):
    # Trouve la pression la plus proche dans notre map
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    
    p = {
        "latitude": lat, "longitude": lon, 
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa", 
        "models": "meteofrance_arome_france_hd", # SOURCE WINDY
        "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1
    }
    try:
        r = requests.get(OPEN_METEO_URL, params=p).json()
        # Fallback si AROME HD n'est pas dispo (ex: bordure de zone)
        if "hourly" not in r:
            p["models"] = "meteofrance_seamless"
            r = requests.get(OPEN_METEO_URL, params=p).json()
        
        idx = min(range(len(r["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(r["hourly"]["time"][k]) - time_dt))
        wd = r["hourly"][f"wind_direction_{lv}hPa"][idx]
        ws = r["hourly"][f"wind_speed_{lv}hPa"][idx]
        return (wd, ws) if wd is not None else (0, 0)
    except: return 0, 0

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    brng, la1, lo1 = math.radians(bearing), math.radians(lat), math.radians(lon)
    la2 = la1 + (dist_nm/R) * math.cos(brng)
    q = math.cos(la1) if abs(la2-la1) < 1e-10 else (la2-la1) / math.log(math.tan(la2/2 + math.pi/4)/math.tan(la1/2 + math.pi/4))
    lo2 = lo1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(la2), math.degrees(lo2)

def create_pdf_simple(df, total_info):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION", ln=True, align='C')
    pdf.set_font("Helvetica", size=10)
    for _, row in df.iterrows():
        pdf.cell(0, 8, f"{row['Branche']} | Alt:{row['Alt']} | Cm:{row['Cm']} | GS:{row['GS']} | EET:{row['EET']}", ln=True, border=1)
    pdf.ln(5)
    pdf.multi_cell(0, 10, total_info.replace("**", ""))
    return pdf.output()

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V13", layout="wide")
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
    tas = st.number_input("Vitesse Propre (TAS) kt", 50, 250, 100, key="cfg_tas")
    conso = st.number_input("Consommation (L/h)", 5, 100, 22, key="cfg_conso")
    show_relief = st.checkbox("📊 Relief", False, key="chk_relief")
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
        st_folium(m, width="100%", height=350, key="map_v13", returned_objects=[])
    else: st.info("Définissez un départ.")

# CALCULS & LOG
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    mv = get_magnetic_declination(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])

    # Optimiseur
    if optimize_global:
        alt_p = st.session_state.waypoints[1]["alt"]
        avg_tc = sum(w.get("tc", 0) for w in st.session_state.waypoints[1:]) / (len(st.session_state.waypoints)-1)
        avg_rm = (avg_tc - mv) % 360
        niveaux_vfr = [3500, 5500, 7500, 9500] if 0 <= avg_rm < 180 else [2500, 4500, 6500, 8500]
        levels_to_scan = [l for l in niveaux_vfr if abs(l - alt_p) <= 2000]
        
        # GS Prévue
        gs_prev_list = []
        for w in st.session_state.waypoints[1:]:
            wd_p, ws_p = get_wind(w["lat"], w["lon"], alt_p, curr_t)
            gs_p, _ = calculate_gs_and_wca(tas, w["tc"], wd_p, ws_p)
            gs_prev_list.append(gs_p)
        avg_gs_p = sum(gs_prev_list) / len(gs_prev_list)

        best_alt, best_gain = alt_p, 0
        analysis_data = []
        for alt_t in levels_to_scan:
            gs_l = []
            for i in range(1, len(st.session_state.waypoints)):
                w = st.session_state.waypoints[i]
                wd_t, ws_t = get_wind(w["lat"], w["lon"], alt_t, curr_t)
                gs_t, _ = calculate_gs_and_wca(tas, w["tc"], wd_t, ws_t)
                gs_l.append(gs_t)
            avg_gs_t = sum(gs_l) / len(gs_l)
            gain = int(avg_gs_t - avg_gs_p)
            analysis_data.append({"Altitude": f"{alt_t} ft", "GS Moy": f"{int(avg_gs_t)} kt", "Gain": f"{gain} kt"})
            if gain > best_gain:
                best_alt, best_gain = alt_t, gain

        if best_gain > 0:
            st.info(f"💡 **Altitude recommandée : {best_alt} ft** (Gain : +{best_gain} kt par rapport à {alt_p} ft)")
            with st.expander("🧐 Détails AROME HD"):
                st.table(pd.DataFrame(analysis_data))

    # Tableau final
    res_final, t_min, t_dist = [], 0, 0
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws = get_wind(w2["lat"], w2["lon"], w2["alt"], curr_t)
        gs, wca = calculate_gs_and_wca(tas, w2["tc"], wd, ws)
        eet = (w2["dist"]/gs)*60
        t_min += eet; t_dist += w2["dist"]; curr_t += timedelta(minutes=eet)
        res_final.append({"Branche": f"{w1['name']}➔{w2['name']}", "Alt": f"{w2['alt']}ft", "Vent": f"{int(wd)}/{int(ws)}", "Cm": f"{int((w2['tc']-wca-mv)%360):03d}°", "GS": f"{int(gs)}kt", "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}"})

    st.subheader("📋 Log de Navigation")
    df_res = pd.DataFrame(res_final)
    st.table(df_res)
    fuel = round((t_min/60)*conso + 10, 1)
    info_text = f"**Distance : {t_dist:.1f} NM | Temps : {int(t_min)} min | Fuel : {fuel} L**"
    st.success(info_text)
    if st.button("📥 Générer PDF", key="btn_pdf"):
        pdf_bytes = create_pdf_simple(df_res, info_text)
        st.download_button("Télécharger", bytes(pdf_bytes), "navigation.pdf", "application/pdf")
