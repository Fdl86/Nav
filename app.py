import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium

# ─── CONFIG & DATA ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
# Niveaux de pression disponibles pour le scan
PRESSURE_MAP = {2000: 950, 3000: 900, 4000: 850, 5000: 850, 6000: 800, 7000: 800, 8000: 750, 9000: 700, 10000: 700}

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except:
        return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

# ─── LOGIQUE AÉRO ───
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
    if abs(sin_wca) > 1: return 20, 0 # Vent trop fort
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

def get_wind(lat, lon, alt_ft, time_dt):
    lv = PRESSURE_MAP.get(int(round(alt_ft, -3)), 850)
    p = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa", "models": "meteofrance_seamless", "wind_speed_unit": "kn", "timezone": "UTC", "forecast_days": 1}
    r = requests.get(OPEN_METEO_URL, params=p).json()
    idx = min(range(len(r["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(r["hourly"]["time"][k]) - time_dt))
    return r["hourly"][f"wind_direction_{lv}hPa"][idx], r["hourly"][f"wind_speed_{lv}hPa"][idx]

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    brng, la1, lo1 = math.radians(bearing), math.radians(lat), math.radians(lon)
    la2 = la1 + (dist_nm/R) * math.cos(brng)
    q = math.cos(la1) if abs(la2-la1) < 1e-10 else (la2-la1) / math.log(math.tan(la2/2 + math.pi/4)/math.tan(la1/2 + math.pi/4))
    lo2 = lo1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(la2), math.degrees(lo2)

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant Pro", layout="wide")

if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("✈️ Navigation Pro")
    oaci = st.text_input("🔍 Départ OACI", "").upper()
    if oaci in AIRPORTS and not st.session_state.waypoints:
        if st.button(f"Initialiser {oaci}"):
            ap = AIRPORTS[oaci]
            st.session_state.waypoints = [{"name": oaci, "lat": ap["lat"], "lon": ap["lon"], "elev": get_elevation(ap["lat"], ap["lon"]), "alt": 2500}]
            st.rerun()
    
    st.markdown("---")
    tas = st.number_input("Vitesse Propre (TAS) kt", 50, 250, 100)
    vz = st.number_input("Vz moyenne (ft/min)", 0, 2000, 500)
    
    st.markdown("---")
    show_relief = st.checkbox("📊 Afficher Profil Relief", value=False)
    optimize_alt = st.checkbox("💡 Optimiseur d'Altitude", value=True)

    if st.button("🗑️ Reset"):
        st.session_state.waypoints = []; st.rerun()

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc = st.number_input("Route Vraie °", 0, 359, 0)
    dist = st.number_input("Distance NM", 0.1, 100.0, 15.0)
    alt_sel = st.number_input("Altitude ft", 1500, 12500, 2500, step=500)
    
    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        n_lat, n_lon = calculate_destination(last["lat"], last["lon"], tc, dist)
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": round(n_lat, 4), "lon": round(n_lon, 4), "tc": tc, "dist": dist, "alt": alt_sel, "elev": get_elevation(n_lat, n_lon)})
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="blue", weight=3).add_to(m)
        for w in st.session_state.waypoints: folium.Marker([w["lat"], w["lon"]], tooltip=w['name']).add_to(m)
        st_folium(m, width="100%", height=350, key="v7_map")
    else:
        st.info("Entrez un point de départ.")

# OPTION RELIEF
if show_relief and len(st.session_state.waypoints) > 1:
    st.markdown("### 🏔️ Relief de la Navigation")
    prof = [{"Point": w["name"], "Sol (ft)": round(w["elev"]*3.28), "Avion (ft)": w["alt"]} for w in st.session_state.waypoints]
    st.area_chart(pd.DataFrame(prof).set_index("Point"), color=["#8B4513", "#0000FF"])

# CALCULS ET OPTIMISATION
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    st.subheader("📝 Log de Navigation")
    
    res = []
    curr_t = datetime.utcnow()
    mv = get_magnetic_declination(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws = get_wind(w2["lat"], w2["lon"], w2["alt"], curr_t)
        gs, wca = calculate_gs_and_wca(tas, w2["tc"], wd, ws)
        eet = (w2["dist"]/gs)*60
        curr_t += timedelta(minutes=eet)
        
        # Logique Optimiseur
        best_alt_info = ""
        if optimize_alt:
            # Règle semi-circulaire : RM (Route Magnétique) = TC - MV
            rm = (w2["tc"] - mv) % 360
            niveaux_vfr = []
            if 0 <= rm < 180: # Impair + 500
                niveaux_vfr = [3500, 5500, 7500, 9500]
            else: # Pair + 500
                niveaux_vfr = [2500, 4500, 6500, 8500]
            
            best_gs = 0
            best_alt = w2["alt"]
            for a in niveaux_vfr:
                wd_test, ws_test = get_wind(w2["lat"], w2["lon"], a, curr_t)
                gs_test, _ = calculate_gs_and_wca(tas, w2["tc"], wd_test, ws_test)
                if gs_test > best_gs:
                    best_gs = gs_test
                    best_alt = a
            
            if best_alt != w2["alt"]:
                gain = int(best_gs - gs)
                if gain > 2: # On ne propose que si le gain est significatif
                    best_alt_info = f"💡 Conseil : Montez à {best_alt}ft (Gain +{gain}kt GS)"

        res.append({
            "Branche": f"{w1['name']}->{w2['name']}",
            "Altitude": f"{w2['alt']} ft",
            "Vent": f"{int(wd):03d}/{int(ws)}kt",
            "Cm": f"{int((w2['tc']-wca-mv)%360):03d}°",
            "GS": f"{int(gs)} kt",
            "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}",
            "Optimisation": best_alt_info
        })

    df_res = pd.DataFrame(res)
    st.table(df_res)
