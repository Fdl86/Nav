import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math

# ─── CONFIG ───────────────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500]

@st.cache_data(ttl=86400)
def load_airports():
    df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
    fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
    airports = {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    airports["AUTRE"] = {"name":"Autre (saisie manuelle)", "lat":None, "lon":None}
    return airports

AIRPORTS = load_airports()

def get_magnetic_declination(lat, lon):
    return round(-1.2 - (lon * 0.35) + (lat * 0.05), 1)

def calculate_destination(lat, lon, bearing, dist_nm):
    """Calcule la position suivante (formule rhumb line)"""
    R = 3440.065  # rayon Terre en NM
    d = dist_nm / R
    bearing = math.radians(bearing)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    
    lat2 = lat1 + d * math.cos(bearing)
    dlon = math.atan2(math.sin(bearing) * math.sin(d) * math.cos(lat1),
                      math.cos(d) - math.sin(lat1) * math.sin(lat2))
    lon2 = lon1 + dlon
    return math.degrees(lat2), math.degrees(lon2) % 360

def get_nearest_pressure_level(alt_ft):
    alt_m = alt_ft * 0.3048
    if alt_m < 11000:
        p = 1013.25 * (1 - 0.0065 * alt_m / 288.15)**5.255
    else:
        p = 226.32 * math.exp(-0.000157688 * (alt_m - 11000))
    return min(PRESSURE_LEVELS, key=lambda h: abs(h - p))

st.title("Prépa Vol – AROME France (vent par point tournant)")

# SAISIE GLOBALE
airport_options = [f"{code} - {data['name']}" for code, data in sorted(AIRPORTS.items())]
default_idx = next((i for i, o in enumerate(airport_options) if o.startswith("LFBI")), 0)
selected_str = st.selectbox("Aéroport de départ", options=airport_options, index=default_idx)
selected_code = selected_str.split(" - ")[0]

if selected_code == "AUTRE":
    lat = st.number_input("Latitude départ (°)", value=46.5877, format="%.4f")
    lon = st.number_input("Longitude départ (°)", value=0.3069, format="%.4f")
else:
    lat = AIRPORTS[selected_code]["lat"]
    lon = AIRPORTS[selected_code]["lon"]

st.success(f"✅ {selected_code} chargé – Modèle **AROME France** activé")

mag_var = get_magnetic_declination(lat, lon)
st.info(f"Variation magnétique auto : **{mag_var:+.1f}°**")

tas_kts = st.number_input("TAS moyenne (knots)", 60, 250, 110, step=5)
alt_default = st.number_input("Altitude par défaut (ft)", 1000, 18000, 2300, step=100)

depart_time = datetime.utcnow()
st.info(f"Heure de calcul (UTC now) : **{depart_time.strftime('%Y-%m-%d %H:%M:%S')}**")

# GESTION DES SEGMENTS + POSITIONS
if 'waypoints' not in st.session_state:
    st.session_state.waypoints = [{"name": selected_code, "lat": lat, "lon": lon}]

st.subheader("Ajout segments (Cap Vrai + Distance)")
col1, col2, col3 = st.columns([2,2,2])
with col1: tc = st.number_input("Cap Vrai (°)", 0, 359, 0, step=1)
with col2: dist = st.number_input("Distance (NM)", 1.0, 500.0, 50.0, step=1.0)
with col3: alt = st.number_input("Alt segment (ft)", value=alt_default, step=500)

if st.button("Ajouter segment"):
    last = st.session_state.waypoints[-1]
    new_lat, new_lon = calculate_destination(last["lat"], last["lon"], tc, dist)
    st.session_state.waypoints.append({
        "name": f"WP{len(st.session_state.waypoints)}",
        "lat": round(new_lat, 4),
        "lon": round(new_lon, 4),
        "tc": tc,
        "dist": dist,
        "alt": alt
    })
    st.rerun()

# Affichage waypoints
st.write("**Waypoints calculés** (position réelle) :")
for i, wp in enumerate(st.session_state.waypoints):
    st.write(f"{i}: **{wp['name']}** → {wp['lat']:.4f}°, {wp['lon']:.4f}°")

if len(st.session_state.waypoints) > 1 and st.button("Supprimer dernier segment"):
    st.session_state.waypoints.pop()
    st.rerun()

# CALCUL
if st.button("Calculer la route") and len(st.session_state.waypoints) > 1:
    results = []
    current_time = depart_time
    total_min = 0
    total_dist = 0

    for i in range(1, len(st.session_state.waypoints)):
        start = st.session_state.waypoints[i-1]
        end = st.session_state.waypoints[i]
        tc = end.get("tc", 0)
        dist = end.get("dist", 0)
        alt_ft = end.get("alt", alt_default)

        # Midpoint du segment pour vent AROME
        mid_lat = (start["lat"] + end["lat"]) / 2
        mid_lon = (start["lon"] + end["lon"]) / 2

        level = get_nearest_pressure_level(alt_ft)

        params = {
            "latitude": mid_lat,
            "longitude": mid_lon,
            "hourly": f"wind_speed_{level}hPa,wind_direction_{level}hPa",
            "models": "meteofrance_seamless",   # ← AROME France activé !
            "timezone": "UTC",
            "forecast_days": 2
        }
        resp = requests.get(OPEN_METEO_URL, params=params).json()

        times = resp["hourly"]["time"]
        idx = min(range(len(times)), key=lambda k: abs(datetime.fromisoformat(times[k].replace('Z','+00:00')) - current_time))

        wind_kt = resp["hourly"][f"wind_speed_{level}hPa"][idx] * 1.94384
        wind_dir = resp["hourly"][f"wind_direction_{level}hPa"][idx]

        # Calcul dérive classique
        wind_angle = (wind_dir - tc + 180) % 360 - 180
        cross = wind_kt * math.sin(math.radians(wind_angle))
        head = wind_kt * math.cos(math.radians(wind_angle))
        wca = math.degrees(math.asin(cross / tas_kts)) if tas_kts > 0 else 0
        if cross < 0: wca = -wca

        h_true = (tc + wca) % 360
        h_mag = (h_true - mag_var) % 360
        gs = max(tas_kts - head, 1.0)
        t_h = dist / gs
        t_min = t_h * 60
        total_min += t_min
        eta = current_time + timedelta(hours=t_h)
        current_time = eta

        results.append({
            "Segment": f"{start['name']} → {end['name']}",
            "Vent AROME": f"{wind_kt:.1f} kt / {wind_dir:.0f}°",
            "WCA": f"{wca:+.1f}°",
            "CM": f"{h_mag:.0f}°",
            "GS": f"{gs:.0f} kt",
            "Temps": f"{t_min:.0f} min",
            "ETA": eta.strftime("%H:%M")
        })
        total_dist += dist

    st.subheader("Résultats AROME")
    st.table(results)
    st.success(f"**Total** : {total_dist:.0f} NM – {total_min:.0f} min – ETA {current_time.strftime('%H:%M UTC')}")

    with st.expander("Debug (pour vérifier AROME)"):
        st.write(f"Midpoint utilisé pour dernier segment : {mid_lat:.4f}°, {mid_lon:.4f}°")
        st.write(f"Niveau pression : {level} hPa")
        st.caption("Modèle forcé : meteofrance_seamless (AROME 1.5-2.5 km) → devrait coller à Windy AROME")
else:
    st.info("Ajoute au moins un segment pour voir le calcul avec vent par zone")
