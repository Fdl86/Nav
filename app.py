import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

PRESSURE_MAP = {1000:975, 1500:960, 2000:950, 2500:925, 3000:900, 5000:850, 7000:750}

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport']))]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except: return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

def get_elevation_ft(lat, lon):
    # Correction manuelle pour LFBI si l'API dévie
    if round(lat,2) == 46.59 and round(lon,2) == 0.31: return 423
    try:
        r = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}).json()
        return round(r.get("elevation", [0])[0] * 3.28084)
    except: return 0

def get_metar(icao):
    try:
        r = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT")
        return r.text.split('\n')[1] if r.status_code == 200 else "METAR indisponible"
    except: return "Erreur METAR"

def get_wind_v26(lat, lon, alt_ft, time_dt, manual_wind=None):
    if manual_wind: return manual_wind['wd'], manual_wind['ws'], "Manuel", "User"
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    params = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
              "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless", "wind_speed_unit": "kn", "timezone": "UTC"}
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        h = r.get("hourly", {})
        if h.get(f"wind_speed_{lv}hPa_icon_d2", [None])[0]: ws, wd, src = h[f"wind_speed_{lv}hPa_icon_d2"], h[f"wind_direction_{lv}hPa_icon_d2"], "ICON-D2"
        elif h.get(f"wind_speed_{lv}hPa_meteofrance_arome_france_hd", [None])[0]: ws, wd, src = h[f"wind_speed_{lv}hPa_meteofrance_arome_france_hd"], h[f"wind_direction_{lv}hPa_meteofrance_arome_france_hd"], "AROME HD"
        else: ws, wd, src = h[f"wind_speed_{lv}hPa_gfs_seamless"], h[f"wind_direction_{lv}hPa_gfs_seamless"], "GFS"
        idx = min(range(len(h["time"])), key=lambda k: abs(datetime.fromisoformat(h["time"][k]) - time_dt))
        return wd[idx], ws[idx], h["time"][0], src
    except: return 0, 0, "N/A", "Err"

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V26", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("✈️ SkyAssistant V26")
    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []
    if sugg:
        if st.button(f"Initialiser Départ : {sugg[0]}"):
            ap = AIRPORTS[sugg[0]]
            elev = get_elevation_ft(ap['lat'], ap['lon'])
            st.session_state.waypoints = [{"name": sugg[0], "lat": ap['lat'], "lon": ap['lon'], "alt": elev, "elev": elev}]
            st.rerun()

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    conso = st.number_input("Conso (L/h)", 5, 100, 22)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500)
    if st.button("🗑️ Reset"): st.session_state.waypoints = []; st.rerun()

# ─── NAVIGATION & CARTE ───
if st.session_state.waypoints:
    st.code(f"🕒 METAR {st.session_state.waypoints[0]['name']} : {get_metar(st.session_state.waypoints[0]['name'])}", language="bash")

col_map, col_ctrl = st.columns([2, 1])
with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt_in = st.number_input("Altitude Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Auto", True)
    m_wind = None if use_auto else {'wd': st.number_input("Dir", 0, 359), 'ws': st.number_input("Force", 0, 100)}

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065
        brng, la1, lo1 = math.radians(tc_in), math.radians(last["lat"]), math.radians(last["lon"])
        la2 = math.degrees(la1 + (dist_in/R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in/R) * math.sin(brng) / math.cos(la1))
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": la2, "lon": lo2, "tc": tc_in, "dist": dist_in, "alt": alt_in, "manual_wind": m_wind, "elev": get_elevation_ft(la2, lo2)})
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=9)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)
        st_folium(m, width="100%", height=350, key="map_v26")

# ─── LOG & PROFIL AVEC PALIERS ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t, mv = datetime.utcnow(), -1.2
    nav_data, dist_p, alt_p, terr_p = [], [0], [], [st.session_state.waypoints[0]["elev"]]
    alt_p.append(st.session_state.waypoints[0]["elev"]) # Sol départ

    d_total = 0
    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws, run, src = get_wind_v26(w2["lat"], w2["lon"], w2["alt"], curr_t, w2.get("manual_wind"))
        
        # GS & WCA
        wa = math.radians(wd - w2["tc"])
        sin_wca = (ws/tas)*math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0
        gs = max(20, (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa)))
        
        # Points TOC / TOD pour le graphique
        alt_croisiere = w2["alt"]
        alt_depart = w1["elev"] if i == 1 else w1["alt"]
        
        # TOC
        dist_climb = (gs * ((alt_croisiere - alt_depart) / v_climb) / 60) if alt_croisiere > alt_depart else 0
        if 0 < dist_climb < w2["dist"]:
            dist_p.append(d_total + dist_climb)
            alt_p.append(alt_croisiere)
            terr_p.append(get_elevation_ft(w1["lat"], w1["lon"])) # Simplifié
            
        # TOD (Dernier segment)
        dist_desc = 0
        if i == len(st.session_state.waypoints) - 1:
            alt_fin = w2["elev"] + 1000
            dist_desc = (gs * ((alt_croisiere - alt_fin) / v_descent) / 60)
            if 0 < dist_desc < w2["dist"]:
                dist_p.append(d_total + (w2["dist"] - dist_desc))
                alt_p.append(alt_croisiere)
                terr_p.append(w2["elev"])

        d_total += w2["dist"]
        dist_p.append(d_total)
        alt_p.append(w2["elev"] if i == len(st.session_state.waypoints)-1 else alt_croisiere)
        terr_p.append(w2["elev"])

        nav_data.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Vent": f"{int(wd)}/{int(ws)}kt",
            "Source": src,
            "GS": f"{int(gs)}kt",
            "EET": f"{int((w2['dist']/gs)*60):02d} min",
            "TOC/TOD": f"TOC: {round(dist_climb,1)}NM | TOD: {round(dist_desc,1)}NM avant"
        })

    st.table(pd.DataFrame(nav_data))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dist_p, y=terr_p, fill='tozeroy', name='Relief', line_color='sienna'))
    fig.add_trace(go.Scatter(x=dist_p, y=alt_p, name='Profil Avion', line=dict(color='royalblue', width=4)))
    st.plotly_chart(fig, use_container_width=True)
