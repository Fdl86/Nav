import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium

# ─── CONFIGURATION ────────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500]

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        airports = {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
        airports["AUTRE"] = {"name":"Autre (saisie manuelle)", "lat":None, "lon":None}
        return airports
    except:
        return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}, "AUTRE": {"name":"Autre", "lat":None, "lon":None}}

AIRPORTS = load_airports()

def get_magnetic_declination(lat, lon):
    return round(-1.2 - (lon * 0.35) + (lat * 0.05), 1)

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    d = dist_nm / R
    brng = math.radians(bearing)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = lat1 + d * math.cos(brng)
    dlat = lat2 - lat1
    if abs(dlat) < 1e-10:
        q = math.cos(lat1)
    else:
        dphi = math.log(math.tan(lat2/2 + math.pi/4)/math.tan(lat1/2 + math.pi/4))
        q = dlat / dphi
    dlon = d * math.sin(brng) / q
    lon2 = lon1 + dlon
    return math.degrees(lat2), math.degrees(lon2)

def get_nearest_pressure_level(alt_ft):
    alt_m = alt_ft * 0.3048
    if alt_m < 11000:
        p = 1013.25 * (1 - 0.0065 * alt_m / 288.15)**5.255
    else:
        p = 226.32 * math.exp(-0.000157688 * (alt_m - 11000))
    return min(PRESSURE_LEVELS, key=lambda h: abs(h - p))

# ─── INTERFACE ────────────────────────────────────────────────────────────
st.set_page_config(page_title="Nav AROME France", layout="wide")
st.title("✈️ Préparation de Nav (Modèle AROME)")

with st.sidebar:
    st.header("⚙️ Paramètres de l'avion")
    airport_options = [f"{code} - {data['name']}" for code, data in sorted(AIRPORTS.items())]
    default_idx = next((i for i, o in enumerate(airport_options) if o.startswith("LFBI")), 0)
    selected_str = st.selectbox("Aéroport de départ", options=airport_options, index=default_idx)
    selected_code = selected_str.split(" - ")[0]

    if selected_code == "AUTRE":
        lat_dep = st.number_input("Lat départ (°)", value=46.5877, format="%.4f")
        lon_dep = st.number_input("Lon départ (°)", value=0.3069, format="%.4f")
    else:
        lat_dep = AIRPORTS[selected_code]["lat"]
        lon_dep = AIRPORTS[selected_code]["lon"]

    tas_kts = st.number_input("Vitesse Propre (TAS) en kt", 50, 250, 100) # Fixé à 100
    alt_default = st.number_input("Altitude par défaut (ft)", 500, 15000, 2500, step=500)
    mag_var = get_magnetic_declination(lat_dep, lon_dep)
    st.info(f"Variation mag. estimée : {mag_var:+.1f}°")

if 'waypoints' not in st.session_state:
    st.session_state.waypoints = [{"name": selected_code, "lat": lat_dep, "lon": lon_dep}]

# ─── NAVIGATION & CARTE ───────────────────────────────────────────────────
col_map, col_input = st.columns([2, 1])

with col_input:
    st.subheader("📍 Ajouter un segment")
    tc = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist = st.number_input("Distance (NM)", 0.1, 200.0, 15.0)
    alt_seg = st.number_input("Altitude segment (ft)", 500, 15000, alt_default)
    
    if st.button("Ajouter le segment"):
        last = st.session_state.waypoints[-1]
        new_lat, new_lon = calculate_destination(last["lat"], last["lon"], tc, dist)
        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": round(new_lat, 4), "lon": round(new_lon, 4),
            "tc": tc, "dist": dist, "alt": alt_seg
        })
        st.rerun()

    if len(st.session_state.waypoints) > 1:
        if st.button("Supprimer dernier point", help="Retirer la dernière branche"):
            st.session_state.waypoints.pop()
            st.rerun()

with col_map:
    # Création de la carte Folium
    m = folium.Map(location=[lat_dep, lon_dep], zoom_start=8, tiles="CartoDB positron")
    
    # Dessin du trajet
    path = [[wp["lat"], wp["lon"]] for wp in st.session_state.waypoints]
    folium.PolyLine(path, color="red", weight=4, opacity=0.7).add_to(m)
    
    # Icônes pour les points
    for i, wp in enumerate(st.session_state.waypoints):
        icon_color = "green" if i == 0 else "blue"
        folium.Marker(
            [wp["lat"], wp["lon"]], 
            tooltip=wp['name'],
            icon=folium.Icon(color=icon_color, icon="info-sign")
        ).add_to(m)
    
    st_folium(m, width="100%", height=450)

# ─── CALCULS DU LOG DE NAV ────────────────────────────────────────────────
if st.button("🚀 CALCULER LE LOG DE NAV", type="primary"):
    results = []
    current_time = datetime.utcnow()
    total_min_dec = 0
    total_dist = 0

    

    for i in range(1, len(st.session_state.waypoints)):
        start = st.session_state.waypoints[i-1]
        end = st.session_state.waypoints[i]
        
        tc, dist_seg, alt_ft = end["tc"], end["dist"], end["alt"]
        mid_lat, mid_lon = (start["lat"] + end["lat"]) / 2, (start["lon"] + end["lon"]) / 2
        level = get_nearest_pressure_level(alt_ft)

        params = {
            "latitude": mid_lat, "longitude": mid_lon,
            "hourly": f"wind_speed_{level}hPa,wind_direction_{level}hPa",
            "models": "meteofrance_seamless", 
            "wind_speed_unit": "kn",
            "timezone": "UTC", "forecast_days": 1
        }
        
        try:
            resp = requests.get(OPEN_METEO_URL, params=params).json()
            idx = min(range(len(resp["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(resp["hourly"]["time"][k]) - current_time))
            
            wind_kt = resp["hourly"][f"wind_speed_{level}hPa"][idx]
            wind_dir = resp["hourly"][f"wind_direction_{level}hPa"][idx]
            
            # Triangle des vitesses
            wa_rad = math.radians(wind_dir - tc)
            sin_wca = (wind_kt / tas_kts) * math.sin(wa_rad)
            sin_wca = max(-1, min(1, sin_wca))
            wca_deg = math.degrees(math.asin(sin_wca))
            
            cv = (tc - wca_deg) % 360
            cm = (cv - mag_var) % 360
            gs = (tas_kts * math.cos(math.asin(sin_wca))) - (wind_kt * math.cos(wa_rad))
            gs = max(gs, 20) # Évite GS nulle ou négative
            
            # Temps mm:ss
            t_h = dist_seg / gs
            t_min_total = t_h * 60
            m_part = int(t_min_total)
            s_part = int((t_min_total - m_part) * 60)
            
            total_min_dec += t_min_total
            total_dist += dist_seg
            current_time += timedelta(minutes=t_min_total)

            results.append({
                "Branche": f"{start['name']} ➔ {end['name']}",
                "Rv": f"{int(tc):03d}°",
                "Vent (AROME)": f"{int(wind_dir):03d}°/{int(wind_kt)}kt",
                "Cm (Cap)": f"{int(cm):03d}°",
                "GS": f"{int(gs)}kt",
                "Temps": f"{m_part:02d}:{s_part:02d}"
            })
        except:
            st.error(f"Erreur météo sur le segment {i}")

    st.subheader("📋 Log de Navigation")
    st.table(pd.DataFrame(results))
    
    # Bilan total
    t_h_total = total_min_dec / 60
    h_tot = int(t_h_total)
    m_tot = int(total_min_dec % 60)
    s_tot = int((total_min_dec * 60) % 60)
    
    st.success(f"**BILAN FINAL** : {total_dist:.1f} NM | Temps total : **{h_tot}h {m_tot}min {s_tot}s** | Arrivée : **{current_time.strftime('%H:%M')} UTC**")
