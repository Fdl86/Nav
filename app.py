import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import re

# ─── CONFIG ───────────────────────────────────────────────────────────────
NOAA_WINDS_URL = "https://aviationweather.gov/api/data/windtemp?ids={icao}&levels=low&fcst=06&format=text"  # 6h forecast, low levels

@st.cache_data(ttl=1800)  # Cache 30 min
def load_airports():
    url = "https://ourairports.com/data/airports.csv"
    df = pd.read_csv(url, usecols=['ident', 'name', 'latitude_deg', 'longitude_deg', 'iso_country', 'type'])
    fr_df = df[(df['iso_country'] == 'FR') & (df['type'].isin(['large_airport', 'medium_airport', 'small_airport'])) & (df['ident'].str.len() == 4)]
    airports = {}
    for _, row in fr_df.iterrows():
        code = row['ident']
        airports[code] = {"name": row['name'], "lat": row['latitude_deg'], "lon": row['longitude_deg']}
    airports["AUTRE"] = {"name": "Autre (saisie manuelle)", "lat": None, "lon": None}
    return airports

AIRPORTS = load_airports()

def get_magnetic_declination(lat, lon):
    decl = -1.2 - (lon * 0.35) + (lat * 0.05)  # Approx France 2026
    return round(decl, 1)

def get_winds_aloft_noaa(icao, target_alt_ft, current_time):
    url = NOAA_WINDS_URL.format(icao=icao)
    try:
        resp = requests.get(url, timeout=10).text
        # Parser FB Winds text (ex: lignes comme "3000 18015-05" = dir 180° speed 15 kt temp -05°C)
        lines = resp.splitlines()
        for line in lines:
            if re.search(r'\d{3,4}\s+\d{3,5}', line):  # cherche pattern altitude + dir/speed
                parts = line.split()
                if len(parts) >= 2:
                    alt_str = parts[0]
                    wind_code = parts[1]
                    if alt_str.isdigit() and int(alt_str) in [3000, 6000, 9000, 12000, 18000]:
                        alt = int(alt_str)
                        if abs(alt - target_alt_ft) < 4000:  # closest reasonable
                            if wind_code == "9900":
                                return 0, 0, "VRB/00 kt"
                            dir_str = wind_code[:3]
                            spd_str = wind_code[3:5]
                            wind_dir = int(dir_str) if dir_str != '990' else 0
                            wind_kt = int(spd_str)
                            return wind_kt, wind_dir, f"{wind_dir:03d}/{wind_kt:02d} kt"
        return None, None, "Pas trouvé dans FB Winds"
    except Exception as e:
        return None, None, f"Erreur NOAA: {str(e)}"

st.title("Prépa Vol – Cap Vrai → CM (Vent Aloft NOAA)")

# ─── SAISIE GLOBALE ──────────────────────────────────────────────────────
airport_options = [f"{code} - {data['name']}" for code, data in sorted(AIRPORTS.items())]
default_idx = next((i for i, opt in enumerate(airport_options) if opt.startswith("LFBI")), 0)
selected_str = st.selectbox("Aéroport de départ (OACI)", options=airport_options, index=default_idx)
selected_code = selected_str.split(" - ")[0]

if selected_code == "AUTRE":
    lat = st.number_input("Latitude (°)", value=46.5877, format="%.4f")
    lon = st.number_input("Longitude (°)", value=0.3069, format="%.4f")
else:
    lat = AIRPORTS[selected_code]["lat"]
    lon = AIRPORTS[selected_code]["lon"]
    st.success(f"✅ {selected_code} chargé")

mag_var = get_magnetic_declination(lat, lon)
st.info(f"Variation magnétique auto : **{mag_var:+.1f}°**")

tas_kts = st.number_input("TAS moyenne (knots)", 60, 250, 110, step=5)
alt_ft_default = st.number_input("Altitude croisière par défaut (ft)", 1000, 18000, 2300, step=100)

depart_time = datetime.utcnow()
st.info(f"Heure calcul (UTC now) : **{depart_time.strftime('%Y-%m-%d %H:%M:%S UTC')}**")

# ─── SEGMENTS ─────────────────────────────────────────────────────────────
if 'segments' not in st.session_state:
    st.session_state.segments = []

st.subheader("Segments (Cap Vrai + Distance NM)")
col_a, col_b, col_c = st.columns([2, 2, 2])
with col_a: new_tc = st.number_input("Cap Vrai (°)", 0, 359, 0, step=1)
with col_b: new_dist = st.number_input("Dist (NM)", 1.0, 500.0, 50.0, step=1.0)
with col_c: new_alt = st.number_input("Alt segment (ft)", value=alt_ft_default, step=500)

if st.button("Ajouter segment"):
    st.session_state.segments.append({"true_course": int(new_tc), "dist_nm": new_dist, "alt_ft": new_alt})
    st.rerun()

if st.session_state.segments:
    for i, seg in enumerate(st.session_state.segments):
        st.write(f"{i+1}: Cap Vrai **{seg['true_course']}°** – {seg['dist_nm']:.1f} NM – Alt {seg['alt_ft']} ft")
    if st.button("Supprimer dernier segment"):
        st.session_state.segments.pop()
        st.rerun()

# ─── CALCUL ───────────────────────────────────────────────────────────────
if st.button("Calculer la route") and st.session_state.segments:
    current_time = depart_time
    results = []
    total_time_min = 0
    cumul_dist = 0

    for seg in st.session_state.segments:
        tc = seg["true_course"]
        dist = seg["dist_nm"]
        alt_ft_seg = seg["alt_ft"]

        wind_kt, wind_dir, debug_note = get_winds_aloft_noaa(selected_code, alt_ft_seg, current_time)

        if wind_kt is None:
            st.warning(f"Pas de vent aloft NOAA pour {selected_code} à ~{alt_ft_seg} ft → {debug_note}")
            continue  # ou fallback Open-Meteo si tu veux

        wind_angle_rel = (wind_dir - tc + 180) % 360 - 180
        crosswind = wind_kt * math.sin(math.radians(wind_angle_rel))
        headwind = wind_kt * math.cos(math.radians(wind_angle_rel))

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
            "Dist": f"{dist:.1f} NM",
            "TC": f"{tc}°",
            "Vent NOAA": f"{wind_kt:.0f} kt / {wind_dir:03d}° ({debug_note})",
            "WCA": f"{wca:+.1f}°",
            "Hdg vrai": f"{heading_true:.0f}°",
            "CM": f"{heading_mag:.0f}°",
            "GS": f"{gs:.0f} kt",
            "Temps": f"{time_min:.0f} min",
            "ETA": eta.strftime("%H:%M")
        })
        cumul_dist += dist

    st.subheader("Résultats")
    st.table(results)
    st.success(f"**Total** : {cumul_dist:.0f} NM – {total_time_min:.0f} min – ETA {current_time.strftime('%H:%M UTC')}")

    with st.expander("Debug Vent NOAA"):
        st.write(f"Requête : {NOAA_WINDS_URL.format(icao=selected_code)}")
        st.write(f"Pour altitude ~{alt_ft_seg} ft → niveau le plus proche (3000 ft typique)")
        st.caption("Compare avec Windy ECMWF (18 kt SE) ou AROME (16 kt). NOAA est GFS-based, souvent conservateur à basse alt.")

else:
    st.info("Ajoute au moins un segment pour calculer")
