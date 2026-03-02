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
    # Approximation linéaire pour la France
    return round(-1.2 - (lon * 0.35) + (lat * 0.05), 1)

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065  # Rayon Terre en NM
    d = dist_nm / R
    brng = math.radians(bearing)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    
    lat2 = lat1 + d * math.cos(brng)
    dlat = lat2 - lat1
    # Approximation Rhumb Line pour courtes distances aéronautiques
    if abs(dlat) < 1e-10:
        q = math.cos(lat1)
    else:
        # Différence de latitude croissante
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

# ─── INTERFACE STRÉAMLIT ──────────────────────────────────────────────────
st.title("✈️ Prépa Vol – Modèle AROME (Corrigé)")

col_cfg1, col_cfg2 = st.columns(2)
with col_cfg1:
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

with col_cfg2:
    tas_kts = st.number_input("TAS (Vitesse Propre) en kt", 60, 250, 110)
    alt_default = st.number_input("Altitude standard (ft)", 1000, 15000, 2500, step=500)
    mag_var = get_magnetic_declination(lat, lon)
    st.caption(f"Déclinaison estimée : {mag_var:+.1f}°")

# ─── GESTION DES WAYPOINTS ───────────────────────────────────────────────
if 'waypoints' not in st.session_state:
    st.session_state.waypoints = [{"name": selected_code, "lat": lat, "lon": lon}]

with st.expander("➕ Ajouter un segment", expanded=True):
    c1, c2, c3 = st.columns(3)
    tc = c1.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist = c2.number_input("Distance (NM)", 1.0, 200.0, 20.0)
    alt_seg = c3.number_input("Altitude (ft)", 500, 15000, alt_default)
    
    if st.button("Ajouter le point"):
        last = st.session_state.waypoints[-1]
        new_lat, new_lon = calculate_destination(last["lat"], last["lon"], tc, dist)
        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": round(new_lat, 4), "lon": round(new_lon, 4),
            "tc": tc, "dist": dist, "alt": alt_seg
        })
        st.rerun()

# ─── RÉCAPITULATIF TRAJET ────────────────────────────────────────────────
if len(st.session_state.waypoints) > 1:
    st.write("**Ma route :**")
    wp_names = " ➔ ".join([w['name'] for w in st.session_state.waypoints])
    st.info(wp_names)
    if st.button("Effacer dernier point"):
        st.session_state.waypoints.pop()
        st.rerun()

# ─── CALCULS AVEC AROME ──────────────────────────────────────────────────
if st.button("🚀 CALCULER LE LOG DE NAV", type="primary") and len(st.session_state.waypoints) > 1:
    results = []
    current_time = datetime.utcnow()
    total_min = 0
    total_dist = 0

    

    for i in range(1, len(st.session_state.waypoints)):
        start = st.session_state.waypoints[i-1]
        end = st.session_state.waypoints[i]
        
        tc = end["tc"]
        dist = end["dist"]
        alt_ft = end["alt"]
        mid_lat = (start["lat"] + end["lat"]) / 2
        mid_lon = (start["lon"] + end["lon"]) / 2
        level = get_nearest_pressure_level(alt_ft)

        # Requête API corrigée (wind_speed_unit=kn)
        params = {
            "latitude": mid_lat,
            "longitude": mid_lon,
            "hourly": f"wind_speed_{level}hPa,wind_direction_{level}hPa",
            "models": "meteofrance_seamless", 
            "wind_speed_unit": "kn",
            "timezone": "UTC",
            "forecast_days": 1
        }
        
        try:
            resp = requests.get(OPEN_METEO_URL, params=params).json()
            times = resp["hourly"]["time"]
            # Trouver l'index temporel le plus proche
            idx = min(range(len(times)), key=lambda k: abs(datetime.fromisoformat(times[k]) - current_time))
            
            wind_kt = resp["hourly"][f"wind_speed_{level}hPa"][idx]
            wind_dir = resp["hourly"][f"wind_direction_{level}hPa"][idx]
            
            # --- CALCUL DU TRIANGLE DES VITESSES ---
            # Angle au vent (Wind Angle)
            wa_rad = math.radians(wind_dir - tc)
            
            # Dérive (WCA) : sin(WCA) = (Vw / TAS) * sin(WA)
            sin_wca = (wind_kt / tas_kts) * math.sin(wa_rad)
            # Sécurité si vent > TAS (rare mais bon...)
            sin_wca = max(-1, min(1, sin_wca))
            wca_rad = math.asin(sin_wca)
            wca_deg = math.degrees(wca_rad)
            
            # Cap Vrai (Cv) et Cap Magnétique (Cm)
            cv = (tc - wca_deg) % 360 # On soustrait la dérive au vent pour contrer
            cm = (cv - mag_var) % 360
            
            # Vitesse Sol (GS) : GS = TAS * cos(WCA) - Vw * cos(WA)
            gs = (tas_kts * math.cos(wca_rad)) - (wind_kt * math.cos(wa_rad))
            gs = max(gs, 10.0) # Sécurité vent de face extrême
            
            t_h = dist / gs
            t_min_total = t_h * 60
        
            # Conversion en minutes:secondes
            mins = int(t_min_total)
            secs = int((t_min_total - mins) * 60)
            time_str = f"{mins:02d}:{secs:02d}" # Format mm:ss
        
            total_min += t_min_total
            total_dist += dist
            current_time += timedelta(minutes=t_min_total)

        results.append({
            "Segment": f"{start['name']}→{end['name']}",
            "Alt": f"{alt_ft}ft",
            "Vent (AROME)": f"{int(wind_dir):03d}° / {wind_kt:.0f}kt",
            "Dérive": f"{wca_deg:+.1f}°",
            "Cm (Cap)": f"{int(cm):03d}°",
            "GS": f"{gs:.0f}kt",
            "Temps": time_str  # Utilisation du nouveau format
            })
        except Exception as e:
            st.error(f"Erreur API sur le segment {i} : {e}")

    # AFFICHAGE FINAL
    st.subheader("📋 Log de Navigation")
    df_res = pd.DataFrame(results)
    st.table(df_res)
    
    st.success(f"**BILAN** : {total_dist:.1f} NM en **{total_min:.0f} min** — Arrivée estimée : **{current_time.strftime('%H:%M')} UTC**")

else:
    st.info("Ajoutez des segments pour générer le calcul.")
