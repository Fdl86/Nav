import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import math
import folium
from streamlit_folium import st_folium
from fpdf import FPDF
import io

# ─── CONFIG & DATA ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500]

@st.cache_data(ttl=86400)
def load_airports():
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv", usecols=['ident','name','latitude_deg','longitude_deg','iso_country','type'])
        fr = df[(df['iso_country']=='FR') & (df['type'].isin(['large_airport','medium_airport','small_airport'])) & (df['ident'].str.len()==4)]
        return {row['ident']: {"name":row['name'], "lat":row['latitude_deg'], "lon":row['longitude_deg']} for _,row in fr.iterrows()}
    except:
        return {"LFBI": {"name":"Poitiers Biard", "lat":46.5877, "lon":0.3069}}

AIRPORTS = load_airports()

def get_elevation(lat, lon):
    try:
        res = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}).json()
        return res.get("elevation", [0])[0]
    except: return 0

def get_magnetic_declination(lat, lon):
    return round(-1.2 - (lon * 0.35) + (lat * 0.05), 1)

def calculate_destination(lat, lon, bearing, dist_nm):
    R = 3440.065
    brng = math.radians(bearing)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = lat1 + (dist_nm/R) * math.cos(brng)
    dlat = lat2 - lat1
    q = math.cos(lat1) if abs(dlat) < 1e-10 else dlat / math.log(math.tan(lat2/2 + math.pi/4)/math.tan(lat1/2 + math.pi/4))
    lon2 = lon1 + (dist_nm/R) * math.sin(brng) / q
    return math.degrees(lat2), math.degrees(lon2)

def create_pdf(df, total_info):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, "LOG DE NAVIGATION AROME", align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.ln(10)
    cols = ["Branche", "Rv", "Vent", "Cm", "GS", "EET"]
    for col in cols: pdf.cell(31, 10, col, border=1, align='C')
    pdf.ln()
    pdf.set_font("Helvetica", size=9)
    for _, row in df.iterrows():
        for col in cols:
            txt = str(row[col]).replace("➔", "->")
            pdf.cell(31, 10, txt, border=1, align='C')
        pdf.ln()
    pdf.ln(10); pdf.set_font("Helvetica", 'I', 11)
    pdf.multi_cell(0, 10, total_info.replace("**", ""))
    return bytes(
