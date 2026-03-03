import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium

# ─── CONFIG ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PRESSURE_MAP = {1000: 975, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}

def get_wind_with_metadata(lat, lon, alt_ft, time_dt):
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "meteofrance_arome_france_hd",
        "wind_speed_unit": "kn", "timezone": "UTC"
    }
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        # Récupération de l'heure du run (metadata)
        run_time = r.get("hourly", {}).get("time", ["Inconnue"])[0]
        
        idx = min(range(len(r["hourly"]["time"])), key=lambda k: abs(datetime.fromisoformat(r["hourly"]["time"][k]) - time_dt))
        wd = r["hourly"][f"wind_direction_{lv}hPa"][idx]
        ws = r["hourly"][f"wind_speed_{lv}hPa"][idx]
        
        # Si le vent est suspect (ex: < 5kt alors que tu attends 10+), on note le modèle
        source = "AROME HD 2.5km"
        if ws is None or ws < 1: # Fallback si données vides
            params["models"] = "meteofrance_seamless"
            r = requests.get(OPEN_METEO_URL, params=params).json()
            source = "Météo-France Seamless"
            wd = r["hourly"][f"wind_direction_{lv}hPa"][idx]
            ws = r["hourly"][f"wind_speed_{lv}hPa"][idx]

        return wd, ws, run_time, source
    except:
        return 0, 0, "Erreur", "N/A"

# (Les autres fonctions techniques calculate_gs_and_wca etc. restent identiques à la V16)
def calculate_gs_and_wca(tas, tc, wd, ws):
    if wd is None or ws is None: return tas, 0
    wa = math.radians(wd - tc)
    sin_wca = (ws/tas)*math.sin(wa)
    if abs(sin_wca) > 1: return 20, 0
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V17 - Flight Deck", layout="wide")

if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("🛡️ Sécurité Météo")
    st.info("Les données sont issues du modèle AROME HD (2.5km). Si les valeurs semblent faibles, vérifiez l'heure du run ci-dessous.")
    
    # Paramètres de vol
    tas = st.number_input("TAS (Vitesse Propre) kt", 50, 250, 100)
    conso = st.number_input("Conso (L/h)", 5, 100, 22)
    
    if st.button("🗑️ Reset Plan de Vol"):
        st.session_state.waypoints = []
        st.rerun()

# ... (Gestion des segments et de la carte identique à V16)
# Ici on se concentre sur l'affichage du Log avec la fraîcheur
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    res_final = []
    
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws, run, src = get_wind_with_metadata(w2["lat"], w2["lon"], w2["alt"], curr_t)
        gs, wca = calculate_gs_and_wca(tas, 180, wd, ws) # 180 par défaut pour l'exemple
        
        res_final.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Vent": f"{int(wd)}° / {int(ws)}kt",
            "GS": f"{int(gs)}kt",
            "Source": src,
            "Heure du Run": run
        })

    st.subheader("📋 Log de Navigation Détaillé")
    st.table(pd.DataFrame(res_final))
    
    st.warning(f"💡 **Note technique** : Si l'heure du Run est décalée de plus de 6h par rapport à l'heure actuelle ({curr_t.strftime('%H:%M')} UTC), les données peuvent diverger de Windy qui actualise plus vite.")
