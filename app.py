import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium
from fpdf import FPDF
import io

# ─── CONFIG & DATA ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except:
        return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

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
    if abs(dlat) < 1e-10:
        q = math.cos(lat1)
    else:
        dphi = math.log(math.tan(lat2/2 + math.pi/4)/math.tan(lat1/2 + math.pi/4))
        q = dlat / dphi
    lon2 = lon1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(lat2), math.degrees(lon2)

def create_pdf(df, total_info):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, "LOG DE NAVIGATION AROME", align='C', ln=True)
    pdf.ln(5)
    pdf.set_font("Helvetica", size=10)
    cols = ["Branche", "Rv", "Vent", "Cm", "GS", "EET"]
    for col in cols: pdf.cell(31, 10, col, border=1, align='C')
    pdf.ln()
    pdf.set_font("Helvetica", size=9)
    for _, row in df.iterrows():
        for col in cols:
            txt = str(row[col]).replace("➔", "->")
            pdf.cell(31, 10, txt, border=1, align='C')
        pdf.ln()
    pdf.ln(10)
    pdf.set_font("Helvetica", 'I', 11)
    pdf.multi_cell(0, 10, total_info.replace("**", ""))
    return pdf.output()

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant", layout="wide")

if 'waypoints' not in st.session_state: st.session_state.waypoints = []
if 'results' not in st.session_state: st.session_state.results = None
if 'total_info' not in st.session_state: st.session_state.total_info = ""

with st.sidebar:
    st.title("🛠️ Paramètres")
    oaci = st.text_input("🔍 Code OACI", "").upper()
    if oaci in AIRPORTS and not st.session_state.waypoints:
        if st.button(f"Départ de {oaci}"):
            ap = AIRPORTS[oaci]
            st.session_state.waypoints = [{"name": oaci, "lat": ap["lat"], "lon": ap["lon"], "elev": get_elevation(ap["lat"], ap["lon"]), "alt": 0}]
            st.rerun()
    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    conso = st.number_input("Conso (L/h)", 5, 100, 22)
    if st.button("🗑️ Reset"):
        st.session_state.waypoints = []; st.session_state.results = None; st.rerun()

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Segments")
    tc = st.number_input("Rv °", 0, 359, 0)
    dist = st.number_input("Dist NM", 0.1, 100.0, 15.0)
    alt = st.number_input("Alt ft", 500, 15000, 2500, step=500)
    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        n_lat, n_lon = calculate_destination(last["lat"], last["lon"], tc, dist)
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": round(n_lat, 4), "lon": round(n_lon, 4), "tc": tc, "dist": dist, "alt": alt, "elev": get_elevation(n_lat, n_lon)})
        st.session_state.results = None
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red").add_to(m)
        for w in st.session_state.waypoints: folium.Marker([w["lat"], w["lon"]], tooltip=w['name']).add_to(m)
        st_folium(m, width="100%", height=350, key="v5_map", returning_objects=[])
    else:
        st.info("Entrez un code OACI à gauche.")

if len(st.session_state.waypoints) > 1:
    st.markdown("### 🏔️ Relief")
    prof = [{"Point": w["name"], "Sol (ft)": round(w["elev"]*3.28), "Avion (ft)": w.get("alt", 0)} for w in st.session_state.waypoints]
    st.area_chart(pd.DataFrame(prof).set_index("Point"), color=["#8B4513", "#0000FF"])

if st.button("🚀 CALCULER", type="primary") and len(st.session_state.waypoints) > 1:
    res = []
    t_min, t_dist = 0, 0
    curr_t = datetime.utcnow()
    mv = get_magnetic_declination(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])
    
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        lv = 950 if w2["alt"] < 3000 else 850
        p = {"latitude": w2["lat"], "longitude": w2["lon"], "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa", "models": "meteofrance_seamless", "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1}
        r = requests.get(OPEN_METEO_URL, params=p).json()
        idx = min(range(len(r["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(r["hourly"]["time"][k]) - curr_t))
        wd, ws = r["hourly"][f"wind_direction_{lv}hPa"][idx], r["hourly"][f"wind_speed_{lv}hPa"][idx]
        
        wa = math.radians(wd - w2["tc"])
        swca = (ws/tas)*math.sin(wa)
        wca = math.degrees(math.asin(max(-1, min(1, swca))))
        gs = max(20, (tas * math.cos(math.asin(max(-1, min(1, swca))))) - (ws * math.cos(wa)))
        eet = (w2["dist"]/gs)*60
        t_min += eet; t_dist += w2["dist"]; curr_t += timedelta(minutes=eet)
        res.append({"Branche": f"{w1['name']}->{w2['name']}", "Rv": f"{int(w2['tc']):03d}°", "Vent": f"{int(wd):03d}/{int(ws)}", "Cm": f"{int((w2['tc']-wca-mv)%360):03d}°", "GS": f"{int(gs)}kt", "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}"})
    
    st.session_state.results = pd.DataFrame(res)
    st.session_state.total_info = f"**{t_dist:.1f} NM | {int(t_min)} min | Fuel : {round((t_min/60)*conso + 10, 1)} L**"

if st.session_state.results is not None:
    st.table(st.session_state.results)
    st.success(st.session_state.total_info)
    pdf_out = create_pdf(st.session_state.results, st.session_state.total_info)
    st.download_button("📥 PDF", bytes(pdf_out), "log.pdf", "application/pdf")
