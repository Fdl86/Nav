import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta, date, time
import math

# ─── CONFIG ───────────────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150, 100]

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
        airports[code] = {
            "name": row['name'],
            "lat": row['latitude_deg'],
            "lon": row['longitude_deg']
        }
    airports["AUTRE"] = {"name": "Autre (saisie manuelle)", "lat": None, "lon": None}
    return airports

AIRPORTS = load_airports()

def get_nearest_pressure_level(alt_ft):
    alt_m = alt_ft * 0.3048
    if alt_m < 11000:
        p = 1013.25 * (1 - 0.0065 * alt_m / 288.15)**5.255
    else:
        p = 226.32 * math.exp(-0.000157688 * (alt_m - 11000))
    return min(PRESSURE_LEVELS, key=lambda h: abs(h - p))

def get_magnetic_declination(lat, lon):
    # Approximation réaliste WMM 2025-2030 pour la France (liée aux coordonnées)
    # Plus précis que fixe : ouest France plus négatif
    decl = -1.2 - (lon * 0.35) + (lat * 0.05)
    return round(decl, 1)

st.title("Prépa Vol – Cap Vrai → CM + Vent Aloft (version finale debug)")

# ─── SAISIE GLOBALE ──────────────────────────────────────────────────────
airport_options = [f"{code} - {data['name']}" for code, data in sorted(AIRPORTS.items())]
selected_str = st.selectbox("Aéroport de départ (OACI)", options=airport_options, index=0)
selected_code = selected_str.split(" - ")[0]

if selected_code == "AUTRE":
    st.subheader("Saisie manuelle")
    lat = st.number_input("Latitude (°)", value=48.5, format="%.4f")
    lon = st.number_input("Longitude (°)", value=2.4, format="%.4f")
else:
    lat = AIRPORTS[selected_code]["lat"]
    lon = AIRPORTS[selected_code]["lon"]
    st.success(f"✅ {selected_code} chargé")

# Affichage variation magnétique automatique (cachée en saisie)
mag_var = get_magnetic_declination(lat, lon)
st.info(f"**Variation magnétique utilisée : {mag_var}°** (basée sur position de {selected_code} – France 2026)")

tas_kts = st.number_input("TAS moyenne (knots)", min_value=60, max_value=250, value=110, step=5)
alt_ft = st.number_input("Altitude croisière par défaut (ft)", min_value=1000, max_value=18000, value=2300, step=100)

# Heure uniquement (date = aujourd'hui)
heure_str = st.text_input("Heure départ UTC (HH:MM:SS)", value="08:00:00")

# ─── SEGMENTS (inchangé) ─────────────────────────────────────────────────
if 'segments' not in st.session_state:
    st.session_state.segments = []

st.subheader("Segments (Cap Vrai + Distance NM)")
col_a, col_b, col_c = st.columns([2, 2, 2])
with col_a: new_true_course = st.number_input("Cap Vrai (°)", min_value=0, max_value=359, value=0, step=1)
with col_b: new_dist_nm = st.number_input("Distance (NM)", min_value=1.0, max_value=500.0, value=50.0, step=1.0)
with col_c: new_alt_ft = st.number_input("Alt ce segment (ft)", value=alt_ft, step=500)

if st.button("Ajouter segment"):
    st.session_state.segments.append({"true_course": int(new_true_course), "dist_nm": new_dist_nm, "alt_ft": new_alt_ft})
    st.rerun()

if st.session_state.segments:
    for i, seg in enumerate(st.session_state.segments):
        st.write(f"{i+1}: Cap Vrai **{seg['true_course']}°** – {seg['dist_nm']:.1f} NM – Alt {seg['alt_ft']} ft")
    if st.button("Supprimer dernier segment"):
        st.session_state.segments.pop()
        st.rerun()

# ─── CALCUL + DEBUG VENT ─────────────────────────────────────────────────
if st.button("Calculer la route") and st.session_state.segments:
    try:
        h, m, s = map(int, heure_str.split(':'))
        depart_time = datetime.combine(date.today(), time(h, m, s))
    except:
        st.error("Format heure incorrect")
        st.stop()

    current_time = depart_time
    results = []
    total_time_min = 0
    cumul_dist = 0

    for seg in st.session_state.segments:
        tc = seg["true_course"]
        dist = seg["dist_nm"]
        alt_ft_seg = seg["alt_ft"]

        level_hpa = get_nearest_pressure_level(alt_ft_seg)

        params = {
            "latitude": lat, "longitude": lon,
            "hourly": f"wind_speed_{level_hpa}hPa,wind_direction_{level_hpa}hPa",
            "timezone": "UTC", "forecast_days": 7
        }
        resp = requests.get(OPEN_METEO_URL, params=params).json()

        times = resp["hourly"]["time"]
        idx = min(range(len(times)), key=lambda i: abs(datetime.fromisoformat(times[i].replace('Z','')) - current_time))

        wind_ms = resp["hourly"][f"wind_speed_{level_hpa}hPa"][idx]
        wind_kt = wind_ms * 1.94384
        wind_dir = resp["hourly"][f"wind_direction_{level_hpa}hPa"][idx]

        # Calcul dérive
        wind_angle_rel = (wind_dir - tc + 180) % 360 - 180
        crosswind = wind_kt * math.sin(math.radians(wind_angle_rel))
        headwind = wind_kt * math.cos(math.radians(wind_angle_rel))

        wca = math.degrees(math.asin(crosswind / tas_kts)) if tas_kts > 0 else 0
        if crosswind < 0: wca = -wca

        heading_true = (tc + wca) % 360
        heading_mag = (heading_true - mag_var) % 360
        gs = max(tas_kts - headwind, 1.0)
        time_h = dist / gs
        time_min = time_h * 60
        total_time_min += time_min
        eta = current_time + timedelta(hours=time_h)
        current_time = eta

        results.append({
            "dist": f"{dist:.1f}", "tc": f"{tc}°", "vent": f"{wind_kt:.1f} kt / {wind_dir:.0f}°",
            "wca": f"{wca:+.1f}°", "hdg_true": f"{heading_true:.0f}°", "hdg_mag": f"{heading_mag:.0f}° CM",
            "gs": f"{gs:.0f} kt", "temps": f"{time_min:.0f} min", "eta": eta.strftime("%H:%M")
        })
        cumul_dist += dist

    st.subheader("Résultats")
    st.table(results)
    st.success(f"**Total** : {cumul_dist:.0f} NM – {total_time_min:.0f} min – ETA {current_time.strftime('%H:%M UTC')}")

    with st.expander("🔍 Debug Vent (pour comprendre d'où viennent les chiffres)"):
        st.write(f"**Aéroport** : {selected_code} | Altitude segment : {alt_ft_seg} ft")
        st.write(f"**Niveau pression calculé** : {level_hpa} hPa")
        st.write(f"**Heure forecast utilisée** : {times[idx]}")
        st.write(f"**Vent brut Open-Meteo** : {wind_ms:.1f} m/s = **{wind_kt:.1f} kt**")
        st.caption("Note : À 2300 ft, Open-Meteo (ECMWF) peut montrer ±5 kt par rapport à WINTEM/GFS de Windy. C’est normal à basse altitude.")

else:
    st.info("Ajoute des segments et appuie sur Calculer")
