import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import math
import folium
from streamlit_folium import st_folium

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
AVWX_METAR_URL = "https://avwx.rest/api/metar/" # Nécessite parfois une clé, fallback implémenté

PRESSURE_MAP = {1000:975, 1500:960, 2000:950, 2500:925, 3000:900, 5000:850, 7000:750}

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport']))]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except: return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

# ─── FONCTIONS MÉTÉO & AÉRO ───
def get_metar(icao):
    try:
        # Utilisation d'un service gratuit sans clé pour l'exemple
        r = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT")
        if r.status_code == 200:
            return r.text.split('\n')[1]
        return "METAR indisponible"
    except: return "Erreur connexion METAR"

def get_wind_v22(lat, lon, alt_ft, time_dt, manual_wind=None):
    if manual_wind:
        return manual_wind['wd'], manual_wind['ws'], "Manuel", "Utilisateur"
    
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    params = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
              "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless", "wind_speed_unit": "kn", "timezone": "UTC"}
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        h = r.get("hourly", {})
        ws_icon = h.get(f"wind_speed_{lv}hPa_icon_d2", [None])[0]
        if ws_icon and ws_icon > 0.5:
            ws, wd, src = h[f"wind_speed_{lv}hPa_icon_d2"], h[f"wind_direction_{lv}hPa_icon_d2"], "ICON-D2"
        elif h.get(f"wind_speed_{lv}hPa_meteofrance_arome_france_hd", [None])[0]:
            ws, wd, src = h[f"wind_speed_{lv}hPa_meteofrance_arome_france_hd"], h[f"wind_direction_{lv}hPa_meteofrance_arome_france_hd"], "AROME HD"
        else:
            ws, wd, src = h[f"wind_speed_{lv}hPa_gfs_seamless"], h[f"wind_direction_{lv}hPa_gfs_seamless"], "GFS Global"
        idx = min(range(len(h["time"])), key=lambda k: abs(datetime.fromisoformat(h["time"][k]) - time_dt))
        return wd[idx], ws[idx], h["time"][0], src
    except: return 0, 0, "N/A", "Erreur"

def calculate_gs_and_wca(tas, tc, wd, ws):
    wa = math.radians(wd - tc)
    sin_wca = (ws/tas)*math.sin(wa)
    if abs(sin_wca) > 1: return tas, 0
    wca = math.degrees(math.asin(sin_wca))
    gs = (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa))
    return max(20, gs), round(wca)

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V22", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("✈️ SkyAssistant V22")
    
    # Autocomplétion Aéroport
    search_oaci = st.text_input("🔍 Rechercher OACI (ex: LFBI)", "").upper()
    suggestions = [k for k in AIRPORTS.keys() if k.startswith(search_oaci)] if search_oaci else []
    
    if suggestions:
        selected_oaci = suggestions[0]
        st.info(f"Probable : {selected_oaci} - {AIRPORTS[selected_oaci]['name']}")
        if st.button(f"Initialiser {selected_oaci}"):
            ap = AIRPORTS[selected_oaci]
            st.session_state.waypoints = [{"name": selected_oaci, "lat": ap["lat"], "lon": ap["lon"], "alt": 0}]
            st.rerun()

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    conso = st.number_input("Conso (L/h)", 5, 100, 22)
    
    st.subheader("⛰️ Relief & Perf")
    show_relief = st.checkbox("Afficher Relief", False)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 500)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500)

    if st.button("🗑️ Reset"): st.session_state.waypoints = []; st.rerun()

# Affichage METAR si départ sélectionné
if st.session_state.waypoints:
    metar = get_metar(st.session_state.waypoints[0]["name"])
    st.code(f"🕒 METAR {st.session_state.waypoints[0]['name']} : {metar}", language="bash")

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Segments")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt_in = st.number_input("Altitude Croisière (ft)", 1000, 12500, 2500, step=500)
    
    # Option Vent Manuel
    use_auto_wind = st.toggle("Vent Automatique (AROME/ICON)", True)
    man_wd, man_ws = 0, 0
    if not use_auto_wind:
        c_w1, c_w2 = st.columns(2)
        man_wd = c_w1.number_input("Dir. Vent", 0, 359, 0)
        man_ws = c_w2.number_input("Force Vent", 0, 100, 0)

    if st.button("➕ Ajouter Segment") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065
        brng, la1, lo1 = math.radians(tc_in), math.radians(last["lat"]), math.radians(last["lon"])
        la2 = la1 + (dist_in/R) * math.cos(brng)
        lo2 = lo1 + (dist_in/R) * math.sin(brng) / math.cos(la1)
        
        m_wind = None if use_auto_wind else {'wd': man_wd, 'ws': man_ws}
        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}", "lat": math.degrees(la2), "lon": math.degrees(lo2),
            "tc": tc_in, "dist": dist_in, "alt": alt_in, "manual_wind": m_wind
        })
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=8)
        if show_relief:
            # Ajout d'une couche relief simplifiée (OpenTopoMap)
            folium.TileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', name='Relief', attr='OpenTopoMap').add_to(m)
        
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)
        st_folium(m, width="100%", height=400, key="map_v22")

# ─── LOG DE NAV & PERF ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = datetime.utcnow()
    mv = round(-1.2 - (st.session_state.waypoints[0]["lon"] * 0.35) + (st.session_state.waypoints[0]["lat"] * 0.05), 1)
    
    nav_data = []
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws, run, src = get_wind_v22(w2["lat"], w2["lon"], w2["alt"], curr_t, w2.get("manual_wind"))
        gs, wca = calculate_gs_and_wca(tas, w2["tc"], wd, ws)
        
        # Calcul Montée (Top of Climb)
        alt_diff = w2["alt"] - (w1.get("alt", 0))
        time_climb = alt_diff / v_climb if alt_diff > 0 else 0
        dist_climb = round((gs * time_climb / 60), 1)
        
        # Calcul Descente (Top of Descent)
        # On estime la descente pour le dernier segment
        tod_info = ""
        if i == len(st.session_state.waypoints) - 1:
            dist_desc = round((gs * (w2["alt"] / v_descent) / 60), 1)
            tod_info = f"TOD: {dist_desc}NM avant"

        eet = (w2["dist"]/gs)*60
        nav_data.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Alt": f"{w2['alt']}ft",
            "Vent": f"{int(wd)}/{int(ws)} ({src})",
            "Cm": f"{int((w2['tc']-wca-mv)%360):03d}°",
            "GS": f"{int(gs)}kt",
            "EET": f"{int(eet):02d}:{int((eet%1)*60):02d}",
            "Détails": f"TOC: {dist_climb}NM | {tod_info}"
        })

    st.subheader("📋 Log de Navigation & Profil de Vol")
    st.table(pd.DataFrame(nav_data))
