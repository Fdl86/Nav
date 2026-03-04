import streamlit as st
import requests
import pandas as pd
import datetime as dt
import math
import folium
from streamlit_folium import st_folium
import plotly.graph_objects as go
from fpdf import FPDF
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
import time

# ─── CONFIGURATION ───────────────────────────────────────────────────────
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
CACHE_TTL = 86400  # 24 heures
EARTH_RADIUS_NM = 3440.065  # Rayon terrestre en NM

@dataclass
class Waypoint:
    """Structure de données pour un point de navigation"""
    name: str
    lat: float
    lon: float
    alt: int
    elev: int
    arr_type: str = "Direct"
    tc: Optional[int] = None
    dist: Optional[float] = None
    manual_wind: Optional[Dict] = None

@dataclass
class WindData:
    """Données de vent calculées"""
    direction: int
    speed: int
    source: str

# ─── FONCTIONS DE CHARGEMENT AVEC CACHE ─────────────────────────────────
@st.cache_data(ttl=CACHE_TTL, show_spinner="Chargement des aéroports...")
def load_airports() -> Dict[str, Dict]:
    """Charge la liste des aéroports français avec mise en cache"""
    base = {"LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069}}
    
    try:
        df = pd.read_csv(
            "https://ourairports.com/data/airports.csv",
            usecols=['ident', 'name', 'latitude_deg', 'longitude_deg', 'iso_country', 'type']
        )
        fr_airports = df[
            (df['iso_country'] == 'FR') & 
            (df['type'].isin(['large_airport', 'medium_airport', 'small_airport']))
        ]
        
        downloaded = {
            row['ident']: {"name": row['name'], "lat": row['latitude_deg'], "lon": row['longitude_deg']}
            for _, row in fr_airports.iterrows()
        }
        base.update(downloaded)
    except Exception as e:
        st.warning(f"Impossible de charger les aéroports: {e}")
    
    return base

@st.cache_data(ttl=CACHE_TTL)
def get_elevation_ft(lat: float, lon: float) -> int:
    """Récupère l'élévation en pieds avec mise en cache"""
    try:
        response = requests.get(
            ELEVATION_URL,
            params={"latitude": lat, "longitude": lon},
            timeout=5
        )
        data = response.json()
        return round(data.get("elevation", [0])[0] * 3.28084)
    except Exception:
        return 0

@st.cache_data(ttl=3600)  # Cache 1 heure pour les METAR
def get_metar(icao: str) -> str:
    """Récupère le METAR avec mise en cache"""
    if not icao or len(icao) != 4:
        return "Code OACI invalide"
    
    try:
        response = requests.get(
            f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
            timeout=5
        )
        if response.status_code == 200:
            lines = response.text.split('\n')
            return lines[1] if len(lines) > 1 else "METAR indisponible"
        return "METAR indisponible"
    except Exception:
        return "Erreur de connexion METAR"

# ─── FONCTIONS MÉTIER ───────────────────────────────────────────────────
def get_wind_data(lat: float, lon: float, alt_ft: int, time_dt: dt.datetime, 
                  manual_wind: Optional[Dict] = None) -> WindData:
    """Récupère les données de vent avec fallback automatique"""
    if manual_wind:
        return WindData(manual_wind['wd'], manual_wind['ws'], "Manuel")
    
    # Trouve le niveau de pression approprié
    pressure_level = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[pressure_level]
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn",
        "timezone": "UTC"
    }
    
    try:
        response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        data = response.json()
        hourly = data.get("hourly", {})
        
        # Sélection du modèle avec fallback
        model_priority = [
            (f"wind_speed_{lv}hPa_icon_d2", f"wind_direction_{lv}hPa_icon_d2", "ICON-D2"),
            (f"wind_speed_{lv}hPa_meteofrance_arome_france_hd", f"wind_direction_{lv}hPa_meteofrance_arome_france_hd", "AROME"),
            (f"wind_speed_{lv}hPa_gfs_seamless", f"wind_direction_{lv}hPa_gfs_seamless", "GFS")
        ]
        
        for speed_key, dir_key, source in model_priority:
            if hourly.get(speed_key, [None])[0] is not None:
                speeds = hourly[speed_key]
                directions = hourly[dir_key]
                
                # Trouve l'index temporel le plus proche
                times = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc) 
                        for t in hourly["time"]]
                idx = min(range(len(times)), key=lambda k: abs(times[k] - time_dt))
                
                return WindData(directions[idx], speeds[idx], source)
        
        return WindData(0, 0, "Aucune donnée")
    
    except Exception as e:
        st.warning(f"Erreur récupération vent: {e}")
        return WindData(0, 0, "Erreur")

def calculate_wind_components(wind_dir: int, wind_speed: int, track: int, tas: int) -> Tuple[float, float]:
    """Calcule le WCA et la GS à partir du vent"""
    if wind_speed == 0 or tas == 0:
        return 0.0, float(tas)
    
    wind_angle = math.radians(wind_dir - track)
    sin_wca = (wind_speed / tas) * math.sin(wind_angle)
    
    wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0
    gs = max(20, (tas * math.cos(math.radians(wca))) - (wind_speed * math.cos(wind_angle)))
    
    return wca, gs

def calculate_new_position(lat: float, lon: float, track: float, distance: float) -> Tuple[float, float]:
    """Calcule une nouvelle position à partir d'un point et d'une route/distance"""
    brng_rad = math.radians(track)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    
    new_lat = lat_rad + (distance / EARTH_RADIUS_NM) * math.cos(brng_rad)
    new_lon = lon_rad + (distance / EARTH_RADIUS_NM) * math.sin(brng_rad) / math.cos(lat_rad)
    
    return math.degrees(new_lat), math.degrees(new_lon)

def format_time(minutes: float) -> str:
    """Formate un temps en minutes en HH:MM"""
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours:02d}:{mins:02d}"

# ─── GÉNÉRATION PDF ─────────────────────────────────────────────────────
def create_pdf(df_nav: pd.DataFrame, metar_text: str) -> bytes:
    """Génère le PDF du log de navigation"""
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    
    # Titre
    pdf.set_font("helvetica", 'B', 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT", align='C', ln=True)
    pdf.ln(5)
    
    # METAR
    pdf.set_font("helvetica", 'B', 10)
    pdf.cell(0, 8, "METAR DE DEPART :", ln=True)
    pdf.set_font("helvetica", size=9)
    pdf.multi_cell(0, 6, str(metar_text).encode('ascii', 'ignore').decode('ascii'), border=1)
    pdf.ln(5)
    
    # En-tête du tableau
    col_widths = [30, 35, 15, 20, 15, 45, 30]
    headers = ["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]
    
    pdf.set_font("helvetica", 'B', 8)
    pdf.set_fill_color(220, 220, 220)
    for i, header in enumerate(headers):
        pdf.cell(col_widths[i], 8, header, border=1, fill=True, align='C')
    pdf.ln()
    
    # Lignes de données
    pdf.set_font("helvetica", size=8)
    for _, row in df_nav.iterrows():
        pdf.cell(col_widths[0], 8, str(row['Branche']).replace('➔', '->'), border=1)
        pdf.cell(col_widths[1], 8, str(row['Vent']), border=1)
        pdf.cell(col_widths[2], 8, str(row['GS']), border=1, align='C')
        pdf.cell(col_widths[3], 8, str(row['EET']), border=1, align='C')
        pdf.cell(col_widths[4], 8, str(row['Fuel']), border=1, align='C')
        pdf.cell(col_widths[5], 8, str(row['TOC/TOD']), border=1)
        pdf.cell(col_widths[6], 8, str(row['Arrivée']), border=1)
        pdf.ln()
    
    return bytes(pdf.output())

# ─── INTERFACE STREAMLIT ─────────────────────────────────────────────────
def init_session_state():
    """Initialise l'état de session"""
    if 'waypoints' not in st.session_state:
        st.session_state.waypoints = []

def render_sidebar():
    """Affiche la barre latérale"""
    with st.sidebar:
        st.title("✈️ SkyAssistant V47")
        
        # Recherche aéroport
        search = st.text_input("🔍 Rechercher OACI", "").upper()
        if search:
            suggestions = [k for k in AIRPORTS.keys() if k.startswith(search)]
            if suggestions and st.button(f"Départ : {suggestions[0]}"):
                airport = AIRPORTS[suggestions[0]]
                elev = get_elevation_ft(airport['lat'], airport['lon'])
                st.session_state.waypoints = [Waypoint(
                    name=suggestions[0],
                    lat=airport['lat'],
                    lon=airport['lon'],
                    alt=elev,
                    elev=elev,
                    arr_type="Direct"
                )]
                st.rerun()
        
        st.markdown("---")
        
        # Paramètres avion
        tas = st.number_input("TAS (kt)", 50, 250, 100)
        v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840)
        v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500)
        fuel_flow = st.number_input("Conso (L/h)", 5.0, 100.0, 25.0)
        
        if st.button("🗑️ Reset"):
            st.session_state.waypoints = []
            st.rerun()
        
        return tas, v_climb, v_descent, fuel_flow

def render_waypoint_input():
    """Affiche le formulaire d'ajout de point"""
    with col_ctrl:
        st.subheader("📍 Ajouter Segment")
        
        tc = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
        distance = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
        cruise_alt = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)
        
        use_auto = st.toggle("Vent Auto", True)
        manual_wind = None
        if not use_auto:
            manual_wind = {
                'wd': st.number_input("Direction vent", 0, 359, key="wind_dir"),
                'ws': st.number_input("Force vent", 0, 100, key="wind_speed")
            }
        
        if st.button("➕ Ajouter") and st.session_state.waypoints:
            last = st.session_state.waypoints[-1]
            new_lat, new_lon = calculate_new_position(last.lat, last.lon, tc, distance)
            
            new_wp = Waypoint(
                name=f"WP{len(st.session_state.waypoints)}",
                lat=new_lat,
                lon=new_lon,
                alt=cruise_alt,
                elev=get_elevation_ft(new_lat, new_lon),
                tc=tc,
                dist=distance,
                manual_wind=manual_wind,
                arr_type="Direct"
            )
            
            st.session_state.waypoints.append(new_wp)
            st.rerun()

def create_flight_profile(waypoints: List[Waypoint], tas: int, v_climb: int, 
                         v_descent: int, fuel_flow: float):
    """Crée le profil de vol et retourne les données de navigation"""
    if len(waypoints) < 2:
        return None, None
    
    current_time = dt.datetime.now(dt.timezone.utc)
    nav_data = []
    distance_points = [0]
    alt_points = [waypoints[0].elev]
    terrain_points = [waypoints[0].elev]
    total_distance = 0
    current_alt = waypoints[0].elev
    
    fig = go.Figure()
    
    for i in range(1, len(waypoints)):
        wpt1, wpt2 = waypoints[i-1], waypoints[i]
        
        # Calcul du vent
        wind = get_wind_data(wpt2.lat, wpt2.lon, wpt2.alt, current_time, wpt2.manual_wind)
        wca, gs = calculate_wind_components(wind.direction, wind.speed, wpt2.tc or 0, tas)
        
        # Calculs de base
        hours = wpt2.dist / gs if wpt2.dist else 0
        total_sec = hours * 3600
        fuel_branch = round(hours * fuel_flow, 1)
        toc_tod_text = ""
        
        # Calcul TOC (montée)
        if wpt2.alt > current_alt:
            climb_time = ((wpt2.alt - current_alt) / v_climb) * 60
            climb_dist = (gs * (climb_time / 3600))
            
            if climb_dist > 0.1:
                toc_tod_text += f"TOC:{round(climb_dist,1)}NM "
                if climb_dist < wpt2.dist:
                    distance_points.append(total_distance + climb_dist)
                    alt_points.append(wpt2.alt)
                    terrain_points.append(wpt1.elev)
                    
                    fig.add_annotation(
                        x=total_distance + climb_dist,
                        y=wpt2.alt,
                        text=f"TOC {round(climb_dist,1)}NM ({format_time(climb_time)})",
                        showarrow=True,
                        ay=45
                    )
        
        # Gestion arrivée
        arrival_type = wpt2.arr_type
        if i == len(waypoints) - 1 and arrival_type == "Direct":
            arrival_type = "VT (1500ft)"
        
        if arrival_type != "Direct":
            target_alt = wpt2.elev + (1500 if "VT" in arrival_type else 1000)
            
            if wpt2.alt > target_alt:
                descent_time = ((wpt2.alt - target_alt) / v_descent) * 60
                descent_dist = (gs * (descent_time / 3600))
                
                if descent_dist > 0.1:
                    toc_tod_text += f"TOD:{round(descent_dist,1)}NM"
                    if descent_dist < wpt2.dist:
                        distance_points.append(total_distance + (wpt2.dist - descent_dist))
                        alt_points.append(wpt2.alt)
                        terrain_points.append(wpt2.elev)
                        
                        fig.add_annotation(
                            x=total_distance + (wpt2.dist - descent_dist),
                            y=wpt2.alt,
                            text=f"TOD {round(descent_dist,1)}NM ({format_time(descent_time)})",
                            showarrow=True,
                            ay=-45
                        )
            
            # Label destination
            label = "VT" if "VT" in arrival_type else "TDP"
            fig.add_annotation(
                x=total_distance + wpt2.dist,
                y=target_alt,
                text=f"<b>{label} {wpt2.name}</b>",
                showarrow=False,
                yshift=15,
                font=dict(color="orange", size=11)
            )
            
            total_distance += wpt2.dist
            distance_points.extend([total_distance, total_distance])
            alt_points.extend([target_alt, wpt2.elev])
            terrain_points.extend([wpt2.elev, wpt2.elev])
            fig.add_vline(x=total_distance, line_width=2, line_dash="dash", line_color="orange")
            current_alt = wpt2.elev
            
        else:
            total_distance += wpt2.dist
            distance_points.append(total_distance)
            alt_points.append(wpt2.alt)
            terrain_points.append(wpt2.elev)
            current_alt = wpt2.alt
        
        # Ajout au tableau de navigation
        nav_data.append({
            "Branche": f"{wpt1.name}➔{wpt2.name}",
            "Vent": f"{wind.direction}/{wind.speed}kt ({wind.source})",
            "GS": f"{int(gs)}kt",
            "EET": format_time(total_sec/60),
            "Fuel": f"{fuel_branch}L",
            "TOC/TOD": toc_tod_text.strip(),
            "Arrivée": arrival_type,
            "_idx": i
        })
    
    # Configuration du graphique
    fig.add_trace(go.Scatter(
        x=distance_points, y=terrain_points,
        fill='tozeroy', name='Relief', line_color='sienna'
    ))
    fig.add_trace(go.Scatter(
        x=distance_points, y=alt_points,
        name='Profil Avion', line=dict(color='royalblue', width=4)
    ))
    
    fig.update_layout(
        width=1000, height=350,
        xaxis=dict(fixedrange=True, tickformat=".1f", title="Distance (NM)"),
        yaxis=dict(fixedrange=True, title="Altitude (ft)"),
        margin=dict(l=40, r=40, t=20, b=40),
        showlegend=False
    )
    
    return pd.DataFrame(nav_data), fig

# ─── MAIN ───────────────────────────────────────────────────────────────
# Configuration page
st.set_page_config(page_title="SkyAssistant V47", layout="wide")

# Initialisation
init_session_state()
AIRPORTS = load_airports()

# Sidebar
tas, v_climb, v_descent, fuel_flow = render_sidebar()

# Affichage METAR
if st.session_state.waypoints:
    metar = get_metar(st.session_state.waypoints[0].name)
    st.code(f"🕒 METAR {st.session_state.waypoints[0].name} : {metar}", language="bash")

# Colonnes principales
col_map, col_ctrl = st.columns([2, 1])

# Carte
with col_map:
    if st.session_state.waypoints:
        center_lat = st.session_state.waypoints[0].lat
        center_lon = st.session_state.waypoints[0].lon
        
        m = folium.Map(location=[center_lat, center_lon], zoom_start=9)
        
        # Tuiles
        folium.TileLayer(
            tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
            attr='Google Satellite',
            name='Vue Satellite'
        ).add_to(m)
        folium.TileLayer('openstreetmap', name='Carte Standard').add_to(m)
        
        # Ligne de route
        folium.PolyLine(
            [[w.lat, w.lon] for w in st.session_state.waypoints],
            color="red",
            weight=3
        ).add_to(m)
        
        # Marqueurs
        num_wp = len(st.session_state.waypoints)
        for i, wp in enumerate(st.session_state.waypoints):
            icon_color = "blue" if i == 0 else ("red" if i == num_wp-1 else "orange")
            icon_type = "plane" if i == 0 else ("flag" if i == num_wp-1 else "dot-circle-o")
            
            folium.Marker(
                [wp.lat, wp.lon],
                popup=wp.name,
                icon=folium.Icon(color=icon_color, icon=icon_type, prefix="fa")
            ).add_to(m)
        
        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=300, key="map_v47", returned_objects=[])

# Formulaire d'ajout
render_waypoint_input()

# Log de navigation et profil
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    
    df_nav, fig = create_flight_profile(
        st.session_state.waypoints, tas, v_climb, v_descent, fuel_flow
    )
    
    if df_nav is not None:
        st.subheader("📋 Log de Navigation")
        
        edited_log = st.data_editor(
            df_nav,
            column_config={
                "Branche": st.column_config.TextColumn("Branche", width="small"),
                "Vent": st.column_config.TextColumn("Vent", width="medium", disabled=True),
                "GS": st.column_config.TextColumn("GS", width="small", disabled=True),
                "EET": st.column_config.TextColumn("EET", width="small", disabled=True),
                "Fuel": st.column_config.TextColumn("Fuel", width="small", disabled=True),
                "TOC/TOD": st.column_config.TextColumn("TOC/TOD", width="small", disabled=True),
                "Arrivée": st.column_config.SelectboxColumn(
                    "Arrivée",
                    options=["Direct", "TDP (1000ft)", "VT (1500ft)"],
                    width="small"
                ),
                "_idx": None
            },
            hide_index=True
        )
        
        # Mise à jour des waypoints si modification
        if not edited_log.equals(df_nav):
            new_waypoints = [st.session_state.waypoints[0]]
            for _, row in edited_log.iterrows():
                wp = st.session_state.waypoints[row['_idx']].copy()
                if "➔" in row['Branche']:
                    wp.name = row['Branche'].split("➔")[1]
                wp.arr_type = row['Arrivée']
                new_waypoints.append(wp)
            st.session_state.waypoints = new_waypoints
            st.rerun()
        
        # Téléchargement PDF
        metar_text = get_metar(st.session_state.waypoints[0].name)
        st.download_button(
            label="📥 Log PDF",
            data=create_pdf(df_nav.drop(columns=['_idx']), metar_text),
            file_name="nav_log.pdf"
        )
        
        # Graphique
        st.markdown(
            '<div style="overflow-x: auto; width: 100%; border: 1px solid #444; border-radius: 10px;">',
            unsafe_allow_html=True
        )
        st.plotly_chart(
            fig,
            use_container_width=False,
            config={'staticPlot': True, 'displayModeBar': False}
        )
        st.markdown('</div>', unsafe_allow_html=True)
