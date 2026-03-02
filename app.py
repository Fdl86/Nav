import streamlit as st
import requests
from datetime import datetime, timedelta
import math

# ─── CONFIG ───────────────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150, 100]

# Approximation variation magnétique (très basique, Europe Ouest ~ -1° à +3° en 2026)
# En vrai : utiliser une lib ou API (ex NOAA magnetic calculator), mais pour proto :
MAG_VAR_DEFAULT = -2.0  # ° (négatif = ouest, ex France Ouest)

st.title("Prépa Vol – Vent Aloft & Dérive (Cap Vrai → CM)")

# ─── SAISIE GLOBALE ──────────────────────────────────────────────────────
col1, col2 = st.columns(2)
with col1:
    tas_kts = st.number_input("TAS moyenne (knots)", min_value=60.0, max_value=250.0, value=110.0, step=5.0)
with col2:
    alt_ft = st.number_input("Altitude croisière (ft)", min_value=1000, max_value=18000, value=4500, step=500)

depart_time_str = st.text_input("Heure départ UTC (YYYY-MM-DDTHH:MM)", value="2026-03-05T08:00")
try:
    depart_time = datetime.fromisoformat(depart_time_str)
except:
    depart_time = datetime.utcnow()
    st.warning("Format heure invalide → prise UTC now")

# Variation magnétique (à améliorer plus tard)
mag_var = st.number_input("Variation magnétique approx (°)", value=MAG_VAR_DEFAULT, step=0.5, format="%.1f")
st.caption("Négatif = ouest (ex: -2° pour Bretagne), positif = est")

# ─── SEGMENTS ─────────────────────────────────────────────────────────────
if 'segments' not in st.session_state:
    st.session_state.segments = []

st.subheader("Segments (Cap Vrai + Distance NM)")
col_a, col_b, col_c, col_d = st.columns([1,1,1,1])
with col_a: new_true_course = st.number_input("Cap Vrai (°)", min_value=0.0, max_value=360.0, value=0.0, key="new_tc")
with col_b: new_dist_nm = st.number_input("Distance (NM)", min_value=1.0, max_value=500.0, value=50.0, key="new_dist")
with col_c: new_alt_ft = st.number_input("Alt ce segment (ft)", value=alt_ft, step=500, key="new_alt")
if st.button("Ajouter segment"):
    st.session_state.segments.append({
        "true_course": new_true_course,
        "dist_nm": new_dist_nm,
        "alt_ft": new_alt_ft
    })
    st.rerun()

# Affichage liste segments
if st.session_state.segments:
    st.write("Segments ajoutés :")
    for i, seg in enumerate(st.session_state.segments):
        st.write(f"{i+1}: Cap Vrai {seg['true_course']:.0f}° – {seg['dist_nm']:.1f} NM – Alt {seg['alt_ft']} ft")

    if st.button("Supprimer dernier segment"):
        st.session_state.segments.pop()
        st.rerun()

# ─── CALCUL ───────────────────────────────────────────────────────────────
if st.button("Calculer la route") and st.session_state.segments:
    current_time = depart_time
    total_time_min = 0
    cumul_dist = 0

    results = []

    for seg in st.session_state.segments:
        tc = seg["true_course"]
        dist = seg["dist_nm"]
        alt_ft = seg["alt_ft"]

        # Choisir pressure level le plus proche
        # Approximation grossière : 1000 hPa ≈ 0 ft, 850 ≈ 5000 ft, 700 ≈ 10000 ft, etc.
        level_hpa = min(PRESSURE_LEVELS, key=lambda h: abs( (1000 - (alt_ft / 30)) - h ))  # ~30 ft/hPa rough

        # Position approximative : on prend un point milieu (mais pour proto, on utilise un point fixe ou premier WP)
        # Pour vrai usage : il faudrait lat/lon cumulés, mais ici on simplifie (vent moyen global ou saisie lat/lon départ)
        # Pour proto : on demande lat/lon départ une fois
        if 'lat' not in locals():
            lat = st.number_input("Latitude départ approx (°)", value=48.5, key="lat_dep")  # ex LFPO
            lon = st.number_input("Longitude départ approx (°)", value=2.4, key="lon_dep")

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": f"wind_speed_{level_hpa}hPa,wind_direction_{level_hpa}hPa,temperature_{level_hpa}hPa",
            "timezone": "UTC",
            "forecast_days": 7
        }
        resp = requests.get(OPEN_METEO_URL, params=params).json()

        if "hourly" not in resp:
            st.error("Erreur API Open-Meteo")
            break

        # Trouver heure la plus proche (simplifié)
        times = [datetime.fromisoformat(t) for t in resp["hourly"]["time"]]
        idx = min(range(len(times)), key=lambda i: abs(times[i] - current_time))

        wind_spd_kt = resp["hourly"][f"wind_speed_{level_hpa}hPa"][idx] * 1.94384  # m/s → kt
        wind_dir_deg = resp["hourly"][f"wind_direction_{level_hpa}hPa"][idx]
        temp_c = resp["hourly"][f"temperature_{level_hpa}hPa"][idx]

        # Calcul dérive (WCA)
        wind_angle_rel = (wind_dir_deg - tc + 180) % 360 - 180  # angle vent vs axe route
        crosswind = wind_spd_kt * math.sin(math.radians(wind_angle_rel))
        headwind = wind_spd_kt * math.cos(math.radians(wind_angle_rel))

        wca = math.degrees(math.asin(crosswind / tas_kts)) if tas_kts > 0 else 0
        if crosswind < 0:
            wca = -wca  # convention : + = vent droite → heading + (droite)

        heading_true = (tc + wca) % 360
        heading_mag = (heading_true - mag_var) % 360

        gs = tas_kts - headwind  # + tailwind = - headwind négatif
        if gs <= 0:
            gs = 1.0  # évite div0

        time_h = dist / gs
        time_min = time_h * 60
        total_time_min += time_min

        eta = current_time + timedelta(hours=time_h)
        current_time = eta

        results.append({
            "dist": dist,
            "tc": tc,
            "wind": f"{wind_spd_kt:.0f} kt / {wind_dir_deg:.0f}°",
            "wca": f"{wca:+.1f}°",
            "hdg_true": f"{heading_true:.0f}°",
            "hdg_mag": f"{heading_mag:.0f}°",
            "gs": f"{gs:.0f} kt",
            "temps": f"{time_min:.0f} min",
            "eta": eta.strftime("%H:%M UTC")
        })

        cumul_dist += dist

    # Affichage tableau
    st.subheader("Résultats")
    st.table(results)

    st.success(f"**Total** : {cumul_dist:.0f} NM – Temps {total_time_min:.0f} min – ETA {current_time.strftime('%H:%M UTC')}")

else:
    st.info("Ajoute au moins un segment pour calculer.")
