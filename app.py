import streamlit as st
import requests
import pandas as pd
import datetime as dt
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go
from fpdf import FPDF

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

def get_wind_v27_final(lat, lon, alt_ft, time_dt, manual_wind=None):
    if manual_wind: return manual_wind['wd'], manual_wind['ws'], "Manuel", "User"
    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]
    params = {"latitude": lat, "longitude": lon, "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa", "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless", "wind_speed_unit": "kn", "timezone": "UTC"}
    try:
        r = requests.get(OPEN_METEO_URL, params=params).json()
        h = r.get("hourly", {})
        if h.get(f"wind_speed_{lv}hPa_icon_d2", [None])[0] is not None: ws, wd, src = h[f"wind_speed_{lv}hPa_icon_d2"], h[f"wind_direction_{lv}hPa_icon_d2"], "ICON-D2"
        elif h.get(f"wind_speed_{lv}hPa_meteofrance_arome_france_hd", [None])[0] is not None: ws, wd, src = h[f"wind_speed_{lv}hPa_meteofrance_arome_france_hd"], h[f"wind_direction_{lv}hPa_meteofrance_arome_france_hd"], "AROME HD"
        else: ws, wd, src = h[f"wind_speed_{lv}hPa_gfs_seamless"], h[f"wind_direction_{lv}hPa_gfs_seamless"], "GFS"
        idx = min(range(len(h["time"])), key=lambda k: abs(dt.datetime.fromisoformat(h["time"][k]).replace(tzinfo=dt.timezone.utc) - time_dt))
        return wd[idx], ws[idx], h["time"][0], src
    except: return 0, 0, "N/A", "Err"

def create_pdf(df_nav, metar_text):
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_font("helvetica", 'B', 16)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT", new_x="LMARGIN", new_y="NEXT", align='C')
    pdf.ln(5)
    pdf.set_font("helvetica", 'B', 10)
    pdf.cell(0, 8, "METAR DE DEPART :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=9)
    clean_metar = str(metar_text).encode('ascii', 'ignore').decode('ascii')
    pdf.multi_cell(0, 6, clean_metar, border=1)
    pdf.ln(10)
    w = [45, 35, 20, 25, 65] 
    cols = ["Branche", "Vent", "GS", "EET", "TOC/TOD"]
    pdf.set_font("helvetica", 'B', 9)
    pdf.set_fill_color(220, 220, 220)
    for i in range(len(cols)): pdf.cell(w[i], 10, cols[i], border=1, fill=True, align='C')
    pdf.ln()
    pdf.set_font("helvetica", size=9)
    for _, row in df_nav.iterrows():
        txt_br = str(row['Branche']).replace('➔', '->').encode('ascii', 'ignore').decode('ascii')
        pdf.cell(w[0], 10, txt_br, border=1)
        pdf.cell(w[1], 10, str(row.get('Vent','')), border=1)
        pdf.cell(w[2], 10, str(row.get('GS','')), border=1, align='C')
        pdf.cell(w[3], 10, str(row.get('EET','')), border=1, align='C')
        pdf.cell(w[4], 10, str(row.get('TOC/TOD','')), border=1)
        pdf.ln()
    return bytes(pdf.output())

# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V30", layout="wide")
if 'waypoints' not in st.session_state: st.session_state.waypoints = []

with st.sidebar:
    st.title("✈️ SkyAssistant V30")
    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []
    if sugg and st.button(f"Initialiser Départ : {sugg[0]}"):
        ap = AIRPORTS[sugg[0]]; elev = get_elevation_ft(ap['lat'], ap['lon'])
        st.session_state.waypoints = [{"name": sugg[0], "lat": ap['lat'], "lon": ap['lon'], "alt": elev, "elev": elev}]
        st.rerun()
    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    conso = st.number_input("Conso (L/h)", 5, 100, 22)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500)
    if st.button("🗑️ Reset"): st.session_state.waypoints = []; st.rerun()

# ─── NAVIGATION & CARTE ───
metar_val = ""
if st.session_state.waypoints:
    metar_val = get_metar(st.session_state.waypoints[0]["name"])
    st.code(f"🕒 METAR {st.session_state.waypoints[0]['name']} : {metar_val}", language="bash")

col_map, col_ctrl = st.columns([2, 1])
with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt_in = st.number_input("Altitude Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Auto", True)
    m_wind = None if use_auto else {'wd': st.number_input("Dir", 0, 359), 'ws': st.number_input("Force", 0, 100)}
    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]; R = 3440.065
        brng, la1, lo1 = math.radians(tc_in), math.radians(last["lat"]), math.radians(last["lon"])
        la2 = math.degrees(la1 + (dist_in/R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in/R) * math.sin(brng) / math.cos(la1))
        st.session_state.waypoints.append({"name": f"WP{len(st.session_state.waypoints)}", "lat": la2, "lon": lo2, "tc": tc_in, "dist": dist_in, "alt": alt_in, "manual_wind": m_wind, "elev": get_elevation_ft(la2, lo2)})
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]], zoom_start=9)
        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)
        st_folium(m, width="100%", height=350, key="map_v30")

# ─── LOG DE NAVIGATION INTERACTIF ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = dt.datetime.now(dt.timezone.utc)
    nav_data = []
    dist_p, alt_p, terr_p = [0], [st.session_state.waypoints[0]["elev"]], [st.session_state.waypoints[0]["elev"]]
    d_total = 0
    fig = go.Figure()

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i-1], st.session_state.waypoints[i]
        wd, ws, _, src = get_wind_v27_final(w2["lat"], w2["lon"], w2["alt"], curr_t, w2.get("manual_wind"))
        
        wa = math.radians(wd - w2["tc"]); sin_wca = (ws/tas)*math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0
        gs = max(20, (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa)))
        
        # Temps mm:ss
        total_seconds = (w2["dist"] / gs) * 3600
        eet_str = f"{int(total_seconds//60):02d}:{int(total_seconds%60):02d}"

        # TOC/TOD
        alt_croisiere = w2["alt"]; alt_depart = w1["elev"] if i == 1 else w1["alt"]
        t_climb_min = (alt_croisiere - alt_depart) / v_climb if alt_croisiere > alt_depart else 0
        dist_climb = (gs * t_climb_min / 60)
        
        dist_desc = 0; t_desc_min = 0
        if i == len(st.session_state.waypoints) - 1:
            t_desc_min = (alt_croisiere - (w2["elev"] + 1000)) / v_descent
            dist_desc = (gs * t_desc_min / 60)

        # Annotations Profil
        if 0 < dist_climb < w2["dist"]:
            x_toc = d_total + dist_climb
            dist_p.append(x_toc); alt_p.append(alt_croisiere); terr_p.append(w1["elev"])
            fig.add_annotation(x=x_toc, y=alt_croisiere, text=f"TOC ({round(t_climb_min,1)}min)", showarrow=True)

        if i == len(st.session_state.waypoints) - 1 and 0 < dist_desc < w2["dist"]:
            x_tod = d_total + (w2["dist"] - dist_desc)
            dist_p.append(x_tod); alt_p.append(alt_croisiere); terr_p.append(w2["elev"])
            fig.add_annotation(x=x_tod, y=alt_croisiere, text=f"TOD ({round(t_desc_min,1)}min)", showarrow=True)

        d_total += w2["dist"]
        dist_p.append(d_total); alt_p.append(w2["elev"] if i == len(st.session_state.waypoints)-1 else alt_croisiere); terr_p.append(w2["elev"])
        
        # Données pour le tableau éditable
        nav_data.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Vent": f"{int(wd)}/{int(ws)}kt",
            "GS": f"{int(gs)}kt",
            "EET": eet_str,
            "TOC/TOD": f"TOC:{round(dist_climb,1)} TOD:{round(dist_desc,1)}",
            "Suppr": False,
            "_orig_idx": i # Index pour retrouver le waypoint
        })

    st.subheader("📋 Log de Navigation Interactif")
    df_nav = pd.DataFrame(nav_data)
    
    # ÉDITEUR DE LOG
    edited_log = st.data_editor(
        df_nav,
        column_config={
            "Branche": st.column_config.TextColumn("Branche (Renommer)", width="medium"),
            "Suppr": st.column_config.CheckboxColumn("❌", width="small"),
            "Vent": st.column_config.TextColumn("Vent", disabled=True),
            "GS": st.column_config.TextColumn("GS", disabled=True),
            "EET": st.column_config.TextColumn("EET (mm:ss)", disabled=True),
            "TOC/TOD": st.column_config.TextColumn("TOC/TOD", disabled=True),
        },
        hide_index=True,
        key="nav_editor"
    )

    # TRAITEMENT DES MODIFICATIONS (Renommer ou Supprimer)
    if not edited_log.equals(df_nav):
        new_wps = [st.session_state.waypoints[0]] # On garde le départ
        for idx, row in edited_log.iterrows():
            if not row['Suppr']:
                wp = st.session_state.waypoints[row['_orig_idx']].copy()
                # On extrait le nouveau nom de la branche (partie après la flèche)
                if "➔" in row['Branche']:
                    wp['name'] = row['Branche'].split("➔")[1]
                new_wps.append(wp)
        st.session_state.waypoints = new_wps
        st.rerun()

    # BOUTON PDF & GRAPHIQUE
    st.download_button(label="📥 Télécharger Log PDF", data=create_pdf(df_nav.drop(columns=['Suppr', '_orig_idx']), metar_val), file_name="nav_log.pdf")

    fig.add_trace(go.Scatter(x=dist_p, y=terr_p, fill='tozeroy', name='Relief', line_color='sienna'))
    fig.add_trace(go.Scatter(x=dist_p, y=alt_p, name='Profil Avion', line=dict(color='royalblue', width=4)))
    fig.update_layout(xaxis_title="Distance (NM)", yaxis_title="Altitude (ft)", xaxis=dict(tickformat=".1f"))
    st.plotly_chart(fig, use_container_width=True)
