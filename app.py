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
# Mapping des altitudes vers niveaux de pression pour AROME
PRESSURE_MAP = {2000: 950, 3000: 900, 4000: 850, 5000: 850, 6000: 800, 7000: 800, 8000: 750, 9000: 700, 10000: 700}

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
    wa = math.radians(wd - tc)
    sin_wca = (ws/tas)*math.sin(wa)
    if abs(sin_wca) > 1: return 20, 0
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

def get_wind(lat, lon, alt_ft, time_dt):
    # On arrondit l'altitude pour coller au mapping de pression
    rounded_alt = int(round(alt_ft, -3))
    lv = PRESSURE_MAP.get(rounded_alt, 850)
    p = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa", "models": "meteofrance_seamless", "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1}
    try:
        r = requests.get(OPEN_METEO_URL, params=p).json()
        idx = min(range(len(r["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(r["hourly"]["time"][k]) - time_dt))
        return r["hourly"][f"wind_direction_{lv}hPa"][idx], r["hourly"][f"wind_speed_{lv}hPa"][idx]
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

# ─── INTERFACE STREAMLIT ───
st.set_page_config(page_title="SkyAssistant Pro", layout="wide")

if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("🛠️ Paramètres de Vol")
    oaci = st.text_input("🔍 Code OACI Départ", "", key="start_oaci").upper()
    if oaci in AIRPORTS and not st.session_state.waypoints:
        if st.button(f"Initialiser au départ de {oaci}", key="btn_init"):
            ap = AIRPORTS[oaci]
            st.session_state.waypoints = [{"name": oaci, "lat": ap["lat"], "lon": ap["lon"], "elev": get_elevation(ap["lat"], ap["lon"]), "alt": 2500}]
            st.rerun()
    
    st.markdown("---")
    tas = st.number_input("Vitesse Propre (TAS) kt", 50, 250, 100, key="cfg_tas")
    conso = st.number_input("Consommation (L/h)", 5, 100, 22, key="cfg_conso")
    
    st.markdown("---")
    show_relief = st.checkbox("📊 Afficher le Relief", False, key="chk_relief")
    optimize_global = st.checkbox("💡 Optimiseur d'Altitude", True, key="chk_opt")

    if st.button("🗑️ Tout effacer", key="btn_reset"):
        st.session_state.waypoints = []; st.rerun()

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter un segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, key="in_tc")
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0, key="in_dist")
    alt_in = st.number_input("Altitude prévue (ft)", 1000, 12500, 2500, step=500, key="in_alt")
    
    if st.button("➕ Ajouter la branche", key="btn_add") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        n_lat, n_lon = calculate_destination(last["lat"], last["lon"], tc_in, dist_in)
        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}", 
            "lat": round(n_lat, 4), "lon": round(n_lon, 4), 
            "tc": tc_in, "dist": dist_in, "alt": alt_in, 
            "elev": get_elevation(n_lat, n_lon)
        })
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=4).add_to(m)
        for w in st.session_state.waypoints: 
            folium.Marker([w["lat"], w["lon"]], tooltip=w['name']).add_to(m)
        
        # VERSION STABLE DE LA CARTE (Ne recharge pas au déplacement)
        st_folium(m, width="100%", height=350, key="map_v10", returned_objects=[])
    else: 
        st.info("Veuillez entrer un code OACI dans la barre latérale pour commencer.")

# AFFICHAGE RELIEF (Optionnel)
if show_relief and len(st.session_state.waypoints) > 1:
    st.markdown("### 🏔️ Profil de relief")
    prof = [{"Point": w["name"], "Sol (ft)": round(w["elev"]*3.28), "Avion (ft)": w["alt"]} for w in st.session_state.waypoints]
    st.area_chart(pd.DataFrame(prof).set_index("Point"), color=["#8B4513", "#0000FF"])

# --- CALCULS ET LOG DE NAVIGATION ---
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    # On calcule la déclinaison ici pour qu'elle soit dispo partout
    mv = get_magnetic_declination(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])

    # 1. OPTIMISATION GLOBALE (+/- 2000ft)
    if optimize_global:
        alt_prevue = st.session_state.waypoints[1]["alt"]
        avg_tc = sum(w.get("tc", 0) for w in st.session_state.waypoints[1:]) / (len(st.session_state.waypoints)-1)
        avg_rm = (avg_tc - mv) % 360
        
        # Niveaux théoriques (Est/Ouest)
        niveaux_vfr = [3500, 5500, 7500, 9500] if 0 <= avg_rm < 180 else [2500, 4500, 6500, 8500]
        # Filtre de proximité
        levels_to_scan = [l for l in niveaux_vfr if abs(l - alt_prevue) <= 2000]
        if not levels_to_scan: levels_to_scan = [niveaux_vfr[0]]

        # Performance à l'altitude prévue
        gs_prev_list = []
        for w in st.session_state.waypoints[1:]:
            wd_p, ws_p = get_wind(w["lat"], w["lon"], alt_prevue, curr_t)
            gs_p, _ = calculate_gs_and_wca(tas, w["tc"], wd_p, ws_p)
            gs_prev_list.append(gs_p)
        avg_gs_prevue = sum(gs_prev_list) / len(gs_prev_list)

        analysis_data, best_time, best_alt = [], 9999, 0
        
        for alt_t in levels_to_scan:
            total_time, gs_list = 0, []
            for i in range(1, len(st.session_state.waypoints)):
                w = st.session_state.waypoints[i]
                wd_t, ws_t = get_wind(w["lat"], w["lon"], alt_t, curr_t)
                gs_t, _ = calculate_gs_and_wca(tas, w["tc"], wd_t, ws_t)
                total_time += (w["dist"] / gs_t) * 60
                gs_list.append(gs_t)
            
            avg_gs_t = sum(gs_list) / len(gs_list)
            diff = int(avg_gs_t - avg_gs_prevue)
            analysis_data.append({
                "Altitude": f"{alt_t} ft", 
                "GS Moy": f"{int(avg_gs_t)} kt", 
                "vs Prévue": f"{'+' if diff >= 0 else ''}{diff} kt",
                "Temps": f"{int(total_time)} min"
            })
            if total_time < best_time:
                best_time, best_alt = total_time, alt_t
        
        if best_alt == alt_prevue:
            st.success(f"✅ Altitude de {alt_prevue} ft optimale.")
        else:
            st.info(f"💡 **Conseil : L'altitude de {best_alt} ft est plus performante.**")
        
        with st.expander("🧐 Justification de l'analyse (Vents & GS)"):
            st.write(f"Comparaison par rapport à votre choix de {alt_prevue} ft :")
            st.table(pd.DataFrame(analysis_data))
            st.write(f"**Vents détaillés au niveau recommandé ({best_alt} ft) :**")
            for i in range(1, len(st.session_state.waypoints)):
                w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
                wd_o, ws_o = get_wind(w2["lat"], w2["lon"], best_alt, curr_t)
                gs_o, _ = calculate_gs_and_wca(tas, w2["tc"], wd_o, ws_o)
                st.write(f"- Branche {i} : {int(wd_o)}°/{int(ws_o)}kt (GS {int(gs_o)}kt)")

    # 2. GÉNÉRATION DU LOG
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

    df_res = pd.DataFrame(res_final)
    st.subheader("📋 Log de Navigation")
    st.table(df_res)
    
    fuel = round((t_min/60)*conso + 10, 1)
    info_text = f"**Distance : {t_dist:.1f} NM | Temps estimé : {int(t_min)} min | Carburant : {fuel} L**"
    st.success(info_text)
    
    if st.button("📥 Télécharger le PDF", key="btn_pdf"):
        pdf_bytes = create_pdf_simple(df_res, info_text)
        st.download_button("Confirmer le téléchargement", bytes(pdf_bytes), "navigation.pdf", "application/pdf")
