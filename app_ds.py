```python
import streamlit as st
import requests
import pandas as pd
import datetime as dt
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go
from fpdf import FPDF

# ───────────────── CONFIGURATION ─────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESSURE_MAP = {1000:975, 1500:960, 2000:950, 2500:925, 3000:900, 5000:850, 7000:750}

HTTP_TIMEOUT = 6

st.set_page_config(page_title="SkyAssistant V47", layout="wide")

# ───────────────── SESSION HTTP ─────────────────
@st.cache_resource
def get_http_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "SkyAssistant/47"})
    return s

SESSION = get_http_session()

# ───────────────── ETAT SESSION ─────────────────
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []

if "wx_refresh" not in st.session_state:
    st.session_state.wx_refresh = 0

# ───────────────── AIRPORTS ─────────────────
@st.cache_data(ttl=86400)
def load_airports():
    base = {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}
    try:
        df = pd.read_csv(
            "https://ourairports.com/data/airports.csv",
            usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type']
        )
        fr = df[(df['iso_country']=='FR') &
                (df['type'].isin(['large_airport','medium_airport','small_airport']))]

        downloaded = {
            row['ident']: {
                "name":row['name'],
                "lat":row['latitude_deg'],
                "lon":row['longitude_deg']
            }
            for _,row in fr.iterrows()
        }

        base.update(downloaded)
        return base
    except:
        return base

AIRPORTS = load_airports()

# ───────────────── ELEVATION ─────────────────
@st.cache_data(ttl=86400)
def get_elevation_ft(lat, lon):
    try:
        r = SESSION.get(ELEVATION_URL,
            params={"latitude": lat, "longitude": lon},
            timeout=HTTP_TIMEOUT)
        j = r.json()
        return round(j.get("elevation", [0])[0] * 3.28084)
    except:
        return 0

# ───────────────── METAR ─────────────────
@st.cache_data(ttl=600)
def get_metar_cached(icao, wx_refresh):
    try:
        r = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
            timeout=HTTP_TIMEOUT
        )

        if r.status_code == 200:
            lines = r.text.splitlines()
            if len(lines) > 1:
                return lines[1]

        return "METAR indisponible"

    except:
        return "Erreur METAR"

# ───────────────── VENT ─────────────────
@st.cache_data(ttl=900)
def get_wind_openmeteo_cached(lat, lon, lv, wx_refresh):

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn",
        "timezone": "UTC"
    }

    r = SESSION.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
    return r.json()

def get_wind_v27_final(lat, lon, alt_ft, time_dt, manual_wind=None, wx_refresh=0):

    if manual_wind:
        return manual_wind['wd'], manual_wind['ws'], "Manuel"

    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]

    try:
        r = get_wind_openmeteo_cached(lat, lon, lv, wx_refresh)

        h = r.get("hourly", {})
        times = h.get("time", [])

        if not times:
            return 0,0,"Err"

        def pick(prefix):
            ws = h.get(f"wind_speed_{lv}hPa_{prefix}")
            wd = h.get(f"wind_direction_{lv}hPa_{prefix}")
            if ws and wd and ws[0] is not None:
                return wd,ws
            return None

        picked = pick("icon_d2")

        if picked:
            wd_arr, ws_arr = picked
            src = "ICON-D2"
        else:
            picked = pick("meteofrance_arome_france_hd")
            if picked:
                wd_arr, ws_arr = picked
                src = "AROME"
            else:
                wd_arr = h.get(f"wind_direction_{lv}hPa_gfs_seamless", [])
                ws_arr = h.get(f"wind_speed_{lv}hPa_gfs_seamless", [])
                src = "GFS"

        t_target = time_dt.timestamp()

        best_i = 0
        best_d = float("inf")

        for i,t in enumerate(times):
            ts = dt.datetime.fromisoformat(t).replace(
                tzinfo=dt.timezone.utc).timestamp()
            d = abs(ts - t_target)

            if d < best_d:
                best_d = d
                best_i = i

        return float(wd_arr[best_i]), float(ws_arr[best_i]), src

    except:
        return 0,0,"Err"

# ───────────────── PDF ─────────────────
def create_pdf(df_nav, metar_text):

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()

    pdf.set_font("helvetica", 'B', 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT",
             new_x="LMARGIN", new_y="NEXT", align='C')

    pdf.ln(5)

    pdf.set_font("helvetica", 'B', 10)
    pdf.cell(0, 8, "METAR DE DEPART :",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("helvetica", size=9)

    pdf.multi_cell(
        0,
        6,
        str(metar_text).encode('ascii','ignore').decode('ascii'),
        border=1
    )

    pdf.ln(5)

    w = [30,35,15,20,15,45,30]

    cols = ["Branche","Vent","GS","EET","Fuel","TOC/TOD","Arrivée"]

    pdf.set_font("helvetica",'B',8)
    pdf.set_fill_color(220,220,220)

    for i in range(len(cols)):
        pdf.cell(w[i],8,cols[i],border=1,fill=True,align='C')

    pdf.ln()

    pdf.set_font("helvetica",size=8)

    for _,row in df_nav.iterrows():

        pdf.cell(w[0],8,str(row['Branche']).replace('➔','->'),border=1)
        pdf.cell(w[1],8,str(row['Vent']),border=1)
        pdf.cell(w[2],8,str(row['GS']),border=1,align='C')
        pdf.cell(w[3],8,str(row['EET']),border=1,align='C')
        pdf.cell(w[4],8,str(row['Fuel']),border=1,align='C')
        pdf.cell(w[5],8,str(row['TOC/TOD']),border=1)
        pdf.cell(w[6],8,str(row['Arrivée']),border=1)

        pdf.ln()

    return bytes(pdf.output())

# ───────────────── SIDEBAR ─────────────────
with st.sidebar:

    st.title("✈️ SkyAssistant V47")

    if st.button("🔄 Rafraîchir météo"):
        st.session_state.wx_refresh += 1
        st.rerun()

    search = st.text_input("🔍 Rechercher OACI","").upper()

    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []

    if sugg and st.button(f"Départ : {sugg[0]}"):

        ap = AIRPORTS[sugg[0]]

        elev = get_elevation_ft(ap['lat'],ap['lon'])

        st.session_state.waypoints = [{
            "name":sugg[0],
            "lat":ap['lat'],
            "lon":ap['lon'],
            "alt":elev,
            "elev":elev,
            "arr_type":"Direct"
        }]

        st.rerun()

    st.markdown("---")

    tas = st.number_input("TAS (kt)",50,250,100)
    v_climb = st.number_input("Montée (ft/min)",100,2000,840)
    v_descent = st.number_input("Descente (ft/min)",100,2000,500)
    fuel_flow = st.number_input("Conso (L/h)",5.0,100.0,25.0)

    if st.button("🗑️ Reset"):
        st.session_state.waypoints=[]
        st.rerun()

# ───────────────── METAR ─────────────────
metar_val=""

if st.session_state.waypoints:

    dep = st.session_state.waypoints[0]["name"]

    metar_val = get_metar_cached(dep, st.session_state.wx_refresh)

    st.code(f"🕒 METAR {dep} : {metar_val}", language="bash")

# ───────────────── CARTE ─────────────────
col_map, col_ctrl = st.columns([2,1])

with col_ctrl:

    st.subheader("📍 Ajouter Segment")

    tc_in = st.number_input("Route Vraie (Rv) °",0,359,0)
    dist_in = st.number_input("Distance (NM)",0.1,100.0,15.0)
    alt_in = st.number_input("Alt Croisière (ft)",1000,12500,2500,step=500)

    use_auto = st.toggle("Vent Auto",True)

    m_wind = None

    if not use_auto:
        m_wind = {
            'wd':st.number_input("Dir",0,359),
            'ws':st.number_input("Force",0,100)
        }

    if st.button("➕ Ajouter") and st.session_state.waypoints:

        last = st.session_state.waypoints[-1]

        R = 3440.065

        brng = math.radians(tc_in)

        la1 = math.radians(last["lat"])
        lo1 = math.radians(last["lon"])

        la2 = math.degrees(la1 + (dist_in/R) * math.cos(brng))

        lo2 = math.degrees(
            lo1 + (dist_in/R) * math.sin(brng) / math.cos(la1)
        )

        st.session_state.waypoints.append({

            "name":f"WP{len(st.session_state.waypoints)}",
            "lat":la2,
            "lon":lo2,
            "tc":tc_in,
            "dist":dist_in,
            "alt":alt_in,
            "manual_wind":m_wind,
            "elev":get_elevation_ft(la2,lo2),
            "arr_type":"Direct"
        })

        st.rerun()

with col_map:

    if st.session_state.waypoints:

        m = folium.Map(
            location=[
                st.session_state.waypoints[0]["lat"],
                st.session_state.waypoints[0]["lon"]
            ],
            zoom_start=9
        )

        folium.PolyLine(
            [[w["lat"],w["lon"]] for w in st.session_state.waypoints],
            color="red",
            weight=3
        ).add_to(m)

        for w in st.session_state.waypoints:

            folium.Marker(
                [w["lat"],w["lon"]],
                popup=w["name"]
            ).add_to(m)

        st_folium(m,width="100%",height=300,key="map_v47")

# ───────────────── NAVIGATION ─────────────────
if len(st.session_state.waypoints)>1:

    curr_t = dt.datetime.now(dt.timezone.utc)

    wind_local_cache = {}

    nav_data=[]

    for i in range(1,len(st.session_state.waypoints)):

        w1 = st.session_state.waypoints[i-1]
        w2 = st.session_state.waypoints[i]

        target = min(PRESSURE_MAP.keys(),
                     key=lambda x: abs(x - w2["alt"]))
        lv = PRESSURE_MAP[target]

        key = (
            round(w2["lat"],3),
            round(w2["lon"],3),
            lv,
            st.session_state.wx_refresh
        )

        if w2.get("manual_wind"):

            wd = w2["manual_wind"]["wd"]
            ws = w2["manual_wind"]["ws"]
            src="Manuel"

        else:

            if key in wind_local_cache:
                wd,ws,src = wind_local_cache[key]
            else:
                wd,ws,src = get_wind_v27_final(
                    w2["lat"],
                    w2["lon"],
                    w2["alt"],
                    curr_t,
                    None,
                    st.session_state.wx_refresh
                )
                wind_local_cache[key]=(wd,ws,src)

        wa = math.radians(wd - w2["tc"])

        sin_wca = (ws/tas)*math.sin(wa)

        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca)<=1 else 0

        gs = max(20,(tas*math.cos(math.radians(wca))) -
                 (ws*math.cos(wa)))

        hours = w2["dist"]/gs

        total_sec = hours*3600

        fuel_branch = round(hours*fuel_flow,1)

        nav_data.append({

            "Branche":f"{w1['name']}➔{w2['name']}",
            "Vent":f"{int(wd)}/{int(ws)}kt ({src})",
            "GS":f"{int(gs)}kt",
            "EET":f"{int(total_sec//60):02d}:{int(total_sec%60):02d}",
            "Fuel":f"{fuel_branch}L",
            "TOC/TOD":"",
            "Arrivée":w2.get("arr_type","Direct")
        })

    st.subheader("📋 Log de Navigation")

    df_nav = pd.DataFrame(nav_data)

    st.dataframe(df_nav,use_container_width=True)

    st.download_button(
        label="📥 Log PDF",
        data=create_pdf(df_nav,metar_val),
        file_name="nav_log.pdf"
    )
```
