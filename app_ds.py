import streamlit as st
import requests
import pandas as pd
import datetime as dt
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go
from fpdf import FPDF

# ───────────────── CONFIG ─────────────────

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESSURE_MAP = {1000:975, 1500:960, 2000:950, 2500:925, 3000:900, 5000:850, 7000:750}

st.set_page_config(page_title="SkyAssistant V48", layout="wide")

# ───────────────── SESSION HTTP ─────────────────

@st.cache_resource
def get_session():
    return requests.Session()

SESSION = get_session()

# ───────────────── DATA ─────────────────

@st.cache_data(ttl=86400)
def load_airports():
    base = {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}
    try:
        df = pd.read_csv(
            "https://ourairports.com/data/airports.csv",
            usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type']
        )
        fr = df[
            (df['iso_country']=='FR') &
            (df['type'].isin(['large_airport','medium_airport','small_airport']))
        ]
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

# ───────────────── API CACHÉES ─────────────────

@st.cache_data(ttl=604800)
def get_elevation_ft(lat, lon):
    try:
        r = SESSION.get(ELEVATION_URL, params={
            "latitude": round(lat,4),
            "longitude": round(lon,4)
        }).json()
        return round(r.get("elevation",[0])[0] * 3.28084)
    except:
        return 0

@st.cache_data(ttl=1800)
def get_metar(icao):
    try:
        r = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
        )
        return r.text.split('\n')[1] if r.status_code==200 else "METAR indisponible"
    except:
        return "Erreur METAR"

@st.cache_data(ttl=900)
def preload_wind(lat_list, lon_list, alt_list):
    results = {}
    for lat, lon, alt in zip(lat_list, lon_list, alt_list):
        target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x-alt))
        lv = PRESSURE_MAP[target]

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
            "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
            "wind_speed_unit": "kn",
            "timezone": "UTC"
        }

        try:
            r = SESSION.get(OPEN_METEO_URL, params=params).json()
            h = r.get("hourly", {})
            ws = h.get(f"wind_speed_{lv}hPa_icon_d2",[0])[0]
            wd = h.get(f"wind_direction_{lv}hPa_icon_d2",[0])[0]
            results[(lat,lon,alt)] = (wd,ws,"AUTO")
        except:
            results[(lat,lon,alt)] = (0,0,"Err")

    return results

# ───────────────── PDF ─────────────────

@st.cache_data
def create_pdf_cached(df_nav, metar_text):
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_font("helvetica",'B',14)
    pdf.cell(0,10,"LOG DE NAVIGATION - SKYASSISTANT",align='C')
    pdf.ln(10)

    pdf.set_font("helvetica",'B',10)
    pdf.cell(0,8,"METAR DE DEPART:")
    pdf.ln()
    pdf.set_font("helvetica",size=9)
    pdf.multi_cell(0,6,str(metar_text).encode('ascii','ignore').decode('ascii'),border=1)
    pdf.ln(5)

    cols=["Branche","Vent","GS","EET","Fuel","TOC/TOD","Arrivée"]
    w=[30,35,15,20,15,45,30]

    pdf.set_font("helvetica",'B',8)
    pdf.set_fill_color(220,220,220)
    for i,c in enumerate(cols):
        pdf.cell(w[i],8,c,border=1,fill=True,align='C')
    pdf.ln()

    pdf.set_font("helvetica",size=8)
    for _,row in df_nav.iterrows():
        for i,c in enumerate(cols):
            val=str(row[c]).replace("➔","->").encode('ascii','ignore').decode('ascii')
            pdf.cell(w[i],8,val,border=1)
        pdf.ln()

    return bytes(pdf.output())

# ───────────────── SESSION STATE ─────────────────

if 'waypoints' not in st.session_state:
    st.session_state.waypoints=[]

# ───────────────── SIDEBAR ─────────────────

with st.sidebar:
    st.title("✈️ SkyAssistant V48")

    search = st.text_input("🔍 Rechercher OACI","").upper()
    sugg = [k for k in AIRPORTS if k.startswith(search)] if search else []

    if sugg and st.button(f"Départ : {sugg[0]}"):
        ap=AIRPORTS[sugg[0]]
        elev=get_elevation_ft(ap['lat'],ap['lon'])
        st.session_state.waypoints=[{
            "name":sugg[0],
            "lat":ap['lat'],
            "lon":ap['lon'],
            "alt":elev,
            "elev":elev,
            "arr_type":"Direct"
        }]
        st.rerun()

    st.markdown("---")

    tas=st.number_input("TAS (kt)",50,250,100)
    v_climb=st.number_input("Montée (ft/min)",100,2000,840)
    v_descent=st.number_input("Descente (ft/min)",100,2000,500)
    fuel_flow=st.number_input("Conso (L/h)",5,100,25,step=1)

    if st.button("🗑️ Reset"):
        st.session_state.waypoints=[]
        st.rerun()

# ───────────────── AJOUT SEGMENT ─────────────────

col_map,col_ctrl=st.columns([2,1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")

    tc_in=st.number_input("Route Vraie (Rv) °",0,359,0)
    dist_in=st.number_input("Distance (NM)",0.1,100.0,15.0)
    alt_in=st.number_input("Alt Croisière (ft)",1000,12500,2500,step=500)

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last=st.session_state.waypoints[-1]
        R=3440.065

        brng=math.radians(tc_in)
        la1=math.radians(last["lat"])
        lo1=math.radians(last["lon"])

        la2=math.degrees(la1+(dist_in/R)*math.cos(brng))
        lo2=math.degrees(lo1+(dist_in/R)*math.sin(brng)/math.cos(la1))

        st.session_state.waypoints.append({
            "name":f"WP{len(st.session_state.waypoints)}",
            "lat":la2,
            "lon":lo2,
            "tc":tc_in,
            "dist":dist_in,
            "alt":alt_in,
            "elev":get_elevation_ft(la2,lo2),
            "arr_type":"Direct"
        })
        st.rerun()

# ───────────────── CARTE ─────────────────

with col_map:
    if (
        st.session_state.waypoints
        and isinstance(st.session_state.waypoints, list)
        and "lat" in st.session_state.waypoints[0]
        and "lon" in st.session_state.waypoints[0]
    ):

        start_wp = st.session_state.waypoints[0]

        m = folium.Map(
            location=[start_wp["lat"], start_wp["lon"]],
            zoom_start=9,
            prefer_canvas=True
        )

        folium.PolyLine(
            [[w["lat"], w["lon"]] for w in st.session_state.waypoints if "lat" in w],
            color="red",
            weight=3
        ).add_to(m)

        num_w = len(st.session_state.waypoints)

        for i, w in enumerate(st.session_state.waypoints):

            if "lat" not in w or "lon" not in w:
                continue

            icon_c = "blue" if i == 0 else ("red" if i == num_w-1 else "green")
            icon_t = "plane" if i == 0 else ("flag-checkered" if i == num_w-1 else "location-arrow")

            folium.Marker(
                [w["lat"], w["lon"]],
                popup=w.get("name", f"WP{i}"),
                icon=folium.Icon(color=icon_c, icon=icon_t, prefix="fa")
            ).add_to(m)

        st_folium(m, width="100%", height=350, returned_objects=[])
        
# ───────────────── LOG NAV ─────────────────

if len(st.session_state.waypoints)>1:

    curr_t=dt.datetime.now(dt.timezone.utc)

    lat_list=[w["lat"] for w in st.session_state.waypoints[1:]]
    lon_list=[w["lon"] for w in st.session_state.waypoints[1:]]
    alt_list=[w["alt"] for w in st.session_state.waypoints[1:]]

    wind_cache=preload_wind(lat_list,lon_list,alt_list)

    nav_data=[]
    d_total=0
    current_alt=st.session_state.waypoints[0]["elev"]

    for i in range(1,len(st.session_state.waypoints)):
        w1=st.session_state.waypoints[i-1]
        w2=st.session_state.waypoints[i]

        wd,ws,src=wind_cache.get(
            (w2["lat"],w2["lon"],w2["alt"]),
            (0,0,"Err")
        )

        wa=math.radians(wd-w2["tc"])
        sin_wca=(ws/tas)*math.sin(wa)
        wca=math.degrees(math.asin(sin_wca)) if abs(sin_wca)<=1 else 0
        gs=max(20,(tas*math.cos(math.radians(wca)))-(ws*math.cos(wa)))

        hours=w2["dist"]/gs
        total_sec=hours*3600
        fuel_branch=round(hours*fuel_flow,1)

        d_total+=w2["dist"]
        current_alt=w2["alt"]

        nav_data.append({
            "Branche":f"{w1['name']}➔{w2['name']}",
            "Vent":f"{int(wd)}/{int(ws)}kt ({src})",
            "GS":f"{int(gs)}kt",
            "EET":f"{int(total_sec//60):02d}:{int(total_sec%60):02d}",
            "Fuel":f"{fuel_branch}L",
            "TOC/TOD":"",
            "Arrivée":"Direct"
        })

    st.subheader("📋 Log de Navigation")
    df_nav=pd.DataFrame(nav_data)
    st.dataframe(df_nav,use_container_width=True)

    st.download_button(
        "📥 Log PDF",
        create_pdf_cached(df_nav,get_metar(st.session_state.waypoints[0]["name"])),
        "nav_log.pdf"
    )
