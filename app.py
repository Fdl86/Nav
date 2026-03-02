import streamlit as st
import requests
from datetime import datetime, timedelta, date, time
import math

# ─── CONFIG ───────────────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150, 100]

# Liste des principaux aéroports français (tu peux en ajouter facilement)
AIRPORTS = {
    "LFPG": {"name": "Paris Charles de Gaulle", "lat": 49.0097, "lon": 2.5479},
    "LFPO": {"name": "Paris Orly", "lat": 48.7253, "lon": 2.3594},
    "LFLL": {"name": "Lyon Saint-Exupéry", "lat": 45.7256, "lon": 5.0881},
    "LFBD": {"name": "Bordeaux Mérignac", "lat": 44.8283, "lon": -0.7156},
    "LFBO": {"name": "Toulouse Blagnac", "lat": 43.6293, "lon": 1.3639},
    "LFML": {"name": "Marseille Provence", "lat": 43.4393, "lon": 5.2214},
    "LFRS": {"name": "Nantes Atlantique", "lat": 47.1532, "lon": -1.6107},
    "LFST": {"name": "Strasbourg", "lat": 48.5383, "lon": 7.6282},
    "LFMN": {"name": "Nice Côte d'Azur", "lat": 43.6584, "lon": 7.2159},
    "LFMT": {"name": "Montpellier Méditerranée", "lat": 43.5762, "lon": 3.9630},
    "LFKJ": {"name": "Ajaccio Napoléon Bonaparte", "lat": 41.9236, "lon": 8.8029},
    "LFBH": {"name": "La Rochelle", "lat": 46.1792, "lon": -1.1953},
    "LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069},
    "LFBE": {"name": "Bergerac Roumanière", "lat": 44.8247, "lon": 0.5191},
    "LFMH": {"name": "Saint-Étienne Bouthéon", "lat": 45.5406, "lon": 4.2964},
    "AUTRE": {"name": "Autre (saisie manuelle)", "lat": None, "lon": None},
}

MAG_VAR_DEFAULT = -2.0  # Variation magnétique approx France (négatif = ouest)

st.title("Prépa Vol – Cap Vrai → CM + Vent Aloft")

# ─── SAISIE GLOBALE ──────────────────────────────────────────────────────
# Aéroport de départ
airport_options = [f"{code} - {data['name']}" for code, data in AIRPORTS.items()]
selected_airport_str = st.selectbox("Aéroport de départ (OACI)", options=airport_options, index=0)
selected_code = selected_airport_str.split(" - ")[0]

# Récupération lat/lon automatique
if selected_code == "AUTRE":
    st.subheader("Saisie manuelle des coordonnées")
    lat = st.number_input("Latitude départ (°)", value=48.5, format="%.4f")
    lon = st.number_input("Longitude départ (°)", value=2.4, format="%.4f")
else:
    lat = AIRPORTS[selected_code]["lat"]
    lon = AIRPORTS[selected_code]["lon"]
    st.success(f"✅ Coordonnées de {selected_code} chargées automatiquement")

tas_kts = st.number_input("TAS moyenne (knots)", min_value=60, max_value=250, value=110, step=5)
alt_ft = st.number_input("Altitude croisière par défaut (ft)", min_value=1000, max_value=18000, value=4500, step=500)

# Heure de départ UNIQUEMENT HH:MM:SS + date
col1, col2 = st.columns(2)
with col1:
    date_depart = st.date_input("Date départ UTC", value=date.today())
with col2:
    heure_str = st.text_input("Heure départ UTC (HH:MM:SS)", value="08:00:00")

# Variation magnétique
mag_var = st.number_input("Variation magnétique approx (°)", value=MAG_VAR_DEFAULT, step=0.5, format="%.1f")

# ─── SEGMENTS ─────────────────────────────────────────────────────────────
if 'segments' not in st.session_state:
    st.session_state.segments = []

st.subheader("Segments (Cap Vrai + Distance NM)")
col_a, col_b, col_c = st.columns([2, 2, 2])
with col_a:
    new_true_course = st.number_input("Cap Vrai (°)", min_value=0, max_value=359, value=0, step=1)  # ← incrément 1° seulement
with col_b:
    new_dist_nm = st.number_input("Distance (NM)", min_value=1.0, max_value=500.0, value=50.0, step=1.0)
with col_c:
    new_alt_ft = st.number_input("Alt ce segment (ft)", value=alt_ft, step=500)

if st.button("Ajouter segment"):
    st.session_state.segments.append({
        "true_course": int(new_true_course),
        "dist_nm": new_dist_nm,
        "alt_ft": new_alt_ft
    })
    st.rerun()

# Affichage + suppression
if st.session_state.segments:
    st.write("Segments ajoutés :")
    for i, seg in enumerate(st.session_state.segments):
        st.write(f"{i+1}: Cap Vrai **{seg['true_course']}°** – {seg['dist_nm']:.1f} NM – Alt {seg['alt_ft']} ft")

    if st.button("Supprimer dernier segment"):
        st.session_state.segments.pop()
        st.rerun()

# ─── CALCUL ───────────────────────────────────────────────────────────────
if st.button("Calculer la route") and st.session_state.segments:
    try:
        h, m, s = map(int, heure_str.split(':'))
        depart_time = datetime.combine(date_depart, time(h, m, s))
    except:
        st.error("Format heure incorrect → utilise HH:MM:SS")
        st.stop()

    current_time = depart_time
    total_time_min = 0
    cumul_dist = 0
    results = []

    for seg in st.session_state.segments:
        tc = seg["true_course"]
        dist = seg["dist_nm"]
        alt_ft = seg["alt_ft"]

        # Choix du niveau de pression le plus proche
        level_hpa = min(PRESSURE_LEVELS, key=lambda h: abs((1000 - (alt_ft / 30)) - h))

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": f"wind_speed_{level_hpa}hPa,wind_direction_{level_hpa}hPa",
            "timezone": "UTC",
            "forecast_days": 7
        }
        resp = requests.get(OPEN_METEO_URL, params=params).json()

        times = [datetime.fromisoformat(t) for t in resp["hourly"]["time"]]
        idx = min(range(len(times)), key=lambda i: abs(times[i] - current_time))

        wind_spd_kt = resp["hourly"][f"wind_speed_{level_hpa}hPa"][idx] * 1.94384
        wind_dir_deg = resp["hourly"][f"wind_direction_{level_hpa}hPa"][idx]

        # Calcul dérive (WCA)
        wind_angle_rel = (wind_dir_deg - tc + 180) % 360 - 180
        crosswind = wind_spd_kt * math.sin(math.radians(wind_angle_rel))
        headwind = wind_spd_kt * math.cos(math.radians(wind_angle_rel))

        wca = math.degrees(math.asin(crosswind / tas_kts)) if tas_kts > 0 else 0
        if crosswind < 0:
            wca = -wca

        heading_true = (tc + wca) % 360
        heading_mag = (heading_true - mag_var) % 360

        gs = max(tas_kts - headwind, 1.0)
        time_h = dist / gs
        time_min = time_h * 60
        total_time_min += time_min

        eta = current_time + timedelta(hours=time_h)
        current_time = eta

        results.append({
            "dist": f"{dist:.1f}",
            "tc": f"{tc}°",
            "wind": f"{wind_spd_kt:.0f} kt / {wind_dir_deg:.0f}°",
            "wca": f"{wca:+.1f}°",
            "hdg_true": f"{heading_true:.0f}°",
            "hdg_mag": f"{heading_mag:.0f}° CM",
            "gs": f"{gs:.0f} kt",
            "temps": f"{time_min:.0f} min",
            "eta": eta.strftime("%H:%M")
        })

        cumul_dist += dist

    st.subheader("Résultats")
    st.table(results)

    st.success(f"**Total** : {cumul_dist:.0f} NM – Temps {total_time_min:.0f} min – ETA {current_time.strftime('%H:%M UTC')}")

else:
    st.info("Ajoute au moins un segment et appuie sur Calculer")
