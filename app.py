import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math

# ─── CONFIG ───────────────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300]

@st.cache_data(ttl=86400)
def load_airports():
    url = "https://ourairports.com/data/airports.csv"
    df = pd.read_csv(url, usecols=['ident', 'name', 'latitude_deg', 'longitude_deg', 'iso_country', 'type'])
    fr_df = df[(df['iso_country'] == 'FR') & 
               (df['type'].isin(['large_airport', 'medium_airport', 'small_airport'])) &
               (df['ident'].str.len() == 4)]
    airports = {}
    for _, row in fr_df.iterrows():
        code = row['ident']
        airports[code] = {"name": row['name'], "lat": row['latitude_deg'], "lon": row['longitude_deg']}
    airports["AUTRE"] = {"name": "Autre (saisie manuelle)", "lat": None, "lon": None}
    return airports

AIRPORTS = load_airports()

def get_nearest_pressure_level(alt_ft):
    alt_m = alt_ft * 0.3048
    if alt_m < 11000:
        p = 1013.25 * (1 - 0.0065 * alt_m / 288.15)**5.255
    else:
        p = 226.32 * math.exp(-0.000157688 * (alt_m - 11000))
    nearest = min(PRESSURE_LEVELS, key=lambda h: abs(h - p))
    return nearest

def get_magnetic_declination(lat, lon):
    # Approximation WMM-like pour France 2026
    decl = -1.2 - (lon * 0.35) + (lat * 0.05)
    return round(decl, 1)

st.title("Prépa Vol – Cap Vrai → CM + Vent Aloft")

# ─── SAISIE GLOBALE ──────────────────────────────────────────────────────
airport_options = [f"{code} - {data['name']}" for code, data in sorted(AIRPORTS.items())]
selected_str = st.selectbox("Aéroport de départ (OACI)", options=airport_options, index=airport_options.index("LFBI - Poitiers Biard") if "LFBI - Poitiers Biard" in airport_options else 0)
selected_code = selected_str.split(" - ")[0]

if selected_code == "AUTRE":
    lat = st.number_input("Latitude (°)", value=46.5877, format="%.4f")
    lon = st.number_input("Longitude (°)", value=0.3069, format="%.4f")
else:
    lat = AIRPORTS[selected_code]["lat"]
    lon = AIRPORTS[selected_code]["lon"]
    st.success(f"✅ {selected_code} chargé")

mag_var = get_magnetic_declination(lat, lon)
st.info(f"Variation magnétique auto : **{mag_var:+.1f}°** (position {selected_code})")

tas_kts = st.number_input("TAS moyenne (knots)", min_value=60, max_value=250, value=110, step=5)
alt_ft = st.number_input("Altitude croisière par défaut (ft)", min_value=1000, max_value=18000, value=2300, step=100)

# Heure = UTC now (pas de saisie)
depart_time = datetime.utcnow()
st.info(f"Heure de calcul (UTC actuelle) : **{depart_time.strftime('%Y-%m-%d %H:%M:%S UTC')}**")

# ─── SEGMENTS ─────────────────────────────────────────────────────────────
if 'segments' not in st.session_state:
    st.session_state.segments = []

st.subheader("Segments")
col_a, col_b, col_c = st.columns([2, 2, 2])
with col_a: new_tc = st.number_input("Cap Vrai (°)", 0, 359, 0, step=1)
with col_b: new_dist = st.number_input("Dist (NM)", 1.0, 500.0, 50.0, step=1.0)
with col_c: new_alt = st.number_input("Alt segment (ft)", value=alt_ft, step=500)

if st.button("Ajouter"):
    st.session_state.segments.append({"true_course": int(new_tc), "dist_nm": new_dist, "alt_ft": new_alt})
    st.rerun()

if st.session_state.segments:
    for i, seg in enumerate(st.session_state.segments):
        st.write(f"{i+1}: {seg['true_course']}° – {seg['dist_nm']:.1f} NM – {seg['alt_ft']} ft")
    if st.button("Supprimer dernier"):
        st.session_state.segments.pop()
        st.rerun()

# ─── CALCUL ───────────────────────────────────────────────────────────────
if st.button("Calculer") and st.session_state.segments:
    current_time = depart_time
    results = []
    total_min = 0
    total_dist = 0

    for seg in st.session_state.segments:
        tc = seg["true_course"]
        dist = seg["dist_nm"]
        alt = seg["alt_ft"]

        level = get_nearest_pressure_level(alt)

        params = {
            "latitude": lat, "longitude": lon,
            "hourly": f"wind_speed_{level}hPa,wind_direction_{level}hPa",
            "timezone": "UTC", "forecast_days": 7
        }
        resp = requests.get(OPEN_METEO_URL, params=params).json()

        if "hourly" not in resp or f"wind_speed_{level}hPa" not in resp["hourly"]:
            st.error(f"Erreur API pour {level} hPa – essaie une autre altitude ?")
            continue

        times = resp["hourly"]["time"]
        idx = min(range(len(times)), key=lambda i: abs(datetime.fromisoformat(times[i].replace('Z','+00:00')) - current_time))

        wind_ms = resp["hourly"][f"wind_speed_{level}hPa"][idx]
        wind_kt = wind_ms * 1.94384
        wind_dir = resp["hourly"][f"wind_direction_{level}hPa"][idx]

        wind_angle_rel = (wind_dir - tc + 180) % 360 - 180
        cross = wind_kt * math.sin(math.radians(wind_angle_rel))
        head = wind_kt * math.cos(math.radians(wind_angle_rel))

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
            "Dist": f"{dist:.1f} NM",
            "TC": f"{tc}°",
            "Vent": f"{wind_kt:.1f} kt / {wind_dir:.0f}°",
            "WCA": f"{wca:+.1f}°",
            "Hdg vrai": f"{h_true:.0f}°",
            "CM": f"{h_mag:.0f}°",
            "GS": f"{gs:.0f} kt",
            "Temps": f"{t_min:.0f} min",
            "ETA": eta.strftime("%H:%M")
        })
        total_dist += dist

    st.subheader("Résultats")
    st.table(results)
    st.success(f"Total : {total_dist:.0f} NM – {total_min:.0f} min – ETA {current_time.strftime('%H:%M UTC')}")

    with st.expander("Debug Vent détaillé"):
        st.write(f"**Aéroport** : {selected_code} | Alt segment : {alt} ft")
        st.write(f"Niveau pression : {level} hPa (≈ {alt} ft)")
        st.write(f"Heure forecast prise : {times[idx]}")
        st.write(f"Vent brut : {wind_ms:.1f} m/s → **{wind_kt:.1f} kt**")
        st.caption("Si ça diffère beaucoup de Windy/WINTEM : normal à <3000 ft (modèle global vs local). Essaie 3000–5000 ft pour voir.")

else:
    st.info("Ajoute au moins un segment")
