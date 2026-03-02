import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium
from fpdf import FPDF
import base64

# ─── CONFIGURATION & DATA ──────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500]

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except:
        return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

# ─── FONCTIONS TECHNIQUES ──────────────────────────────────────────────────
def get_elevation(lat, lon):
    try:
        res = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}).json()
        return res.get("elevation", [0])[0]
    except: return 0

def get_magnetic_declination(lat, lon):
    return round(-1.2 - (lon * 0.35) + (lat * 0.05), 1)

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    brng = math.radians(bearing)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = lat1 + (dist_nm/R) * math.cos(brng)
    dlat = lat2 - lat1
    q = math.cos(lat1) if abs(dlat) < 1e-10 else dlat / math.log(math.tan(lat2/2 + math.pi/4)/math.tan(lat1/2 + math.pi/4))
    lon2 = lon1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(lat2), math.degrees(lon2)

def get_wind_at_alt(lat, lon, alt_ft, time_dt):
    # Interpolation simple entre niveaux de pression
    level = min(PRESSURE_LEVELS, key=lambda h: abs(h - (1013.25 * (1 - 0.0065 * (alt_ft*0.3048) / 288.15)**5.255)))
    params = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{level}hPa,wind_direction_{level}hPa",
              "models": "meteofrance_seamless", "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1}
    resp = requests.get(OPEN_METEO_URL, params=params).json()
    idx = min(range(len(resp["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(resp["hourly"]["time"][k]) - time_dt))
    return resp["hourly"][f"wind_direction_{level}hPa"][idx], resp["hourly"][f"wind_speed_{level}hPa"][idx]

# ─── GENERATION PDF ────────────────────────────────────────────────────────
from fpdf import FPDF # L'import reste le même mais utilise fpdf2 derrière

def create_pdf(df, total_info):
    pdf = FPDF()
    pdf.add_page()
    # On utilise une police standard qui supporte mieux les symboles
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, "LOG DE NAVIGATION AROME", new_x="LMARGIN", new_y="NEXT", align='C')
    
    pdf.set_font("Helvetica", size=10)
    pdf.ln(10)
    
    # Header tableau
    cols = ["Branche", "Rv", "Vent", "Cm", "GS", "EET"]
    for col in cols:
        pdf.cell(31, 10, col, border=1)
    pdf.ln()
    
    # Data - On nettoie les caractères flèche pour le PDF
    for _, row in df.iterrows():
        for col in cols:
            # On remplace la flèche ➔ par -> pour être sûr du rendu PDF
            txt = str(row[col]).replace("➔", "->")
            pdf.cell(31, 10, txt, border=1)
        pdf.ln()
        
    pdf.ln(10)
    pdf.set_font("Helvetica", 'I', 12)
    # Nettoyage des ** du markdown pour le PDF
    clean_info = total_info.replace("**", "")
    pdf.multi_cell(0, 10, clean_info)
    
    # Avec fpdf2, on récupère les bytes directement
    return pdf.output()

# ─── INTERFACE STREAMLIT ───────────────────────────────────────────────────
st.set_page_config(page_title="SkyAssistant AROME", layout="wide")

# Mode Nuit / Style personnalisé
st.markdown("""<style> .main { background-color: #121212; color: white; } </style>""", unsafe_allow_html=True)

if 'waypoints' not in st.session_state: st.session_state.waypoints = []
if 'results' not in st.session_state: st.session_state.results = None

with st.sidebar:
    st.title("🛠️ Paramètres Vol")
    oaci_search = st.text_input("🔍 Recherche OACI (ex: LFBZ)", "").upper()
    
    if oaci_search in AIRPORTS and not st.session_state.waypoints:
        if st.button(f"Partir de {oaci_search}"):
            ap = AIRPORTS[oaci_search]
            st.session_state.waypoints = [{"name": oaci_search, "lat": ap["lat"], "lon": ap["lon"], "elev": get_elevation(ap["lat"], ap["lon"])}]
            st.rerun()

    st.markdown("---")
    tas_kts = st.number_input("Vitesse Propre (TAS) kt", 50, 250, 100)
    conso_h = st.number_input("Conso (L/h)", 5, 100, 22)
    forfait_fuel = st.number_input("Forfait Fuel (Roulage/Rés) L", 0, 50, 10)
    
    if st.button("🗑️ Reset Complet"):
        st.session_state.waypoints = []; st.session_state.results = None; st.rerun()

col_left, col_right = st.columns([2, 1])

with col_right:
    st.subheader("📍 Segments")
    tc = st.number_input("Route Vraie °", 0, 359, 0)
    dist = st.number_input("Distance NM", 0.1, 200.0, 15.0)
    alt_seg = st.number_input("Altitude ft", 500, 15000, 2500, step=500)
    
    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        n_lat, n_lon = calculate_destination(last["lat"], last["lon"], tc, dist)
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": round(n_lat, 4), "lon": round(n_lon, 4), 
                                           "tc": tc, "dist": dist, "alt": alt_seg, "elev": get_elevation(n_lat, n_lon)})
        st.session_state.results = None; st.rerun()

with col_left:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8, tiles="CartoDB DarkMatter")
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="orange", weight=5).add_to(m)
        for w in st.session_state.waypoints: folium.Marker([w["lat"], w["lon"]], tooltip=w['name']).add_to(m)
        st_folium(m, width="100%", height=400, key="main_map")
        
        # Petit profil de relief simplifié
        elevs = [w["elev"] for w in st.session_state.waypoints]
        if len(elevs) > 1:
            st.area_chart(pd.DataFrame({"Altitude Terrain (m)": elevs}))

# ─── CALCUL LOG DE NAV ─────────────────────────────────────────────────────
if st.button("🚀 CALCULER LOG & CARBURANT", type="primary") and len(st.session_state.waypoints) > 1:
    res_list = []
    curr_t = datetime.utcnow()
    t_min_total, t_dist_total = 0, 0
    mv = get_magnetic_declination(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws = get_wind_at_alt(w2["lat"], w2["lon"], w2["alt"], curr_t)
        
        wa = math.radians(wd - w2["tc"])
        wca = math.degrees(math.asin(max(-1, min(1, (ws/tas_kts)*math.sin(wa)))))
        gs = max(20, (tas_kts * math.cos(math.radians(wca))) - (ws * math.cos(wa)))
        
        eet_min = (w2["dist"]/gs)*60
        t_min_total += eet_min; t_dist_total += w2["dist"]; curr_t += timedelta(minutes=eet_min)
        
        res_list.append({"Branche": f"{w1['name']}➔{w2['name']}", "Rv": f"{int(w2['tc']):03d}°", 
                         "Vent": f"{int(wd):03d}/{int(ws)}kt", "Cm": f"{int((w2['tc']-wca-mv)%360):03d}°",
                         "GS": f"{int(gs)}kt", "EET": f"{int(eet_min):02d}:{int((eet_min%1)*60):02d}"})

    st.session_state.results = pd.DataFrame(res_list)
    fuel_total = round((t_min_total/60)*conso_h + forfait_fuel, 1)
    st.session_state.total_info = f"**TOTAL : {t_dist_total:.1f} NM | {int(t_min_total)} min | Carburant estimé : {fuel_total} L**"

if st.session_state.results is not None:
    st.table(st.session_state.results)
    st.success(st.session_state.total_info)
    
    # BOUTON PDF
    pdf_data = create_pdf(st.session_state.results, st.session_state.total_info)
    st.download_button(label="📥 Télécharger le Log en PDF", data=pdf_data, file_name="log_nav.pdf", mime="application/pdf")
