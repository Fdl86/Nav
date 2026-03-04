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
from typing import Optional, Dict, List

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

PRESSURE_LEVELS = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
EARTH_RADIUS_NM = 3440.065


@dataclass
class Waypoint:
    name: str
    lat: float
    lon: float
    alt: int
    elev: int
    tc: Optional[int] = None
    dist: Optional[float] = None
    manual_wind: Optional[Dict] = None
    arr_type: str = "Direct"


# ─── CACHED DATA ───
@st.cache_data(ttl=86400 * 2)
def load_airports() -> Dict[str, Dict]:
    base = {"LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069}}
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv",
                         usecols=['ident', 'name', 'latitude_deg', 'longitude_deg', 'iso_country', 'type'])
        fr = df[(df['iso_country'] == 'FR') & (df['type'].isin(['large_airport', 'medium_airport', 'small_airport']))]
        downloaded = {row['ident']: {"name": row['name'], "lat": row['latitude_deg'], "lon": row['longitude_deg']}
                      for _, row in fr.iterrows()}
        base.update(downloaded)
    except Exception:
        pass
    return base


AIRPORTS = load_airports()


# ─── API HELPERS ───
@st.cache_data(ttl=1800)
def get_elevation_ft(lat: float, lon: float) -> int:
    try:
        r = requests.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}, timeout=6).json()
        return round(r.get("elevation", [0])[0] * 3.28084)
    except Exception:
        return 0


def get_metar(icao: str) -> str:
    try:
        r = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT", timeout=8)
        return r.text.splitlines()[1] if r.status_code == 200 and len(r.text.splitlines()) > 1 else "METAR indisponible"
    except Exception:
        return "Erreur METAR"


@st.cache_data(ttl=300)
def get_wind_v27_final(lat: float, lon: float, alt_ft: int, target_time: dt.datetime, manual_wind: Optional[Dict] = None):
    if manual_wind:
        return manual_wind['wd'], manual_wind['ws'], "Manuel"

    level_ft = min(PRESSURE_LEVELS.keys(), key=lambda x: abs(x - alt_ft))
    level_mb = PRESSURE_LEVELS[level_ft]

    params = {
        "latitude": lat, "longitude": lon,
        "hourly": f"wind_speed_{level_mb}hPa,wind_direction_{level_mb}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn", "timezone": "UTC"
    }

    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=10).json()
        h = r.get("hourly", {})
        for model, src_name in [("icon_d2", "ICON-D2"), ("meteofrance_arome_france_hd", "AROME"), ("gfs_seamless", "GFS")]:
            ws_key = f"wind_speed_{level_mb}hPa_{model}"
            wd_key = f"wind_direction_{level_mb}hPa_{model}"
            if ws_key in h and h[ws_key][0] is not None:
                times = h["time"]
                idx = min(range(len(times)), key=lambda k: abs(
                    dt.datetime.fromisoformat(times[k]).replace(tzinfo=dt.timezone.utc) - target_time
                ))
                return int(round(h[wd_key][idx])), int(round(h[ws_key][idx])), src_name
        return 0, 0, "No data"
    except Exception:
        return 0, 0, "Err"


# ─── PDF ───
def create_pdf(df_nav: pd.DataFrame, metar_text: str) -> bytes:
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_font("helvetica", 'B', 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT", new_x="LMARGIN", new_y="NEXT", align='C')
    pdf.ln(5)
    pdf.set_font("helvetica", 'B', 10)
    pdf.cell(0, 8, "METAR DE DEPART :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=9)
    pdf.multi_cell(0, 6, str(metar_text).encode('ascii', 'ignore').decode('ascii'), border=1)
    pdf.ln(5)

    w = [30, 35, 15, 20, 15, 45, 30]
    cols = ["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]
    pdf.set_font("helvetica", 'B', 8)
    pdf.set_fill_color(220, 220, 220)
    for width, col in zip(w, cols):
        pdf.cell(width, 8, col, border=1, fill=True, align='C')
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for _, row in df_nav.iterrows():
        pdf.cell(w[0], 8, str(row['Branche']).replace('➔', '->'), border=1)
        pdf.cell(w[1], 8, str(row['Vent']), border=1)
        pdf.cell(w[2], 8, str(row['GS']), border=1, align='C')
        pdf.cell(w[3], 8, str(row['EET']), border=1, align='C')
        pdf.cell(w[4], 8, str(row['Fuel']), border=1, align='C')
        pdf.cell(w[5], 8, str(row['TOC/TOD']), border=1)
        pdf.cell(w[6], 8, str(row['Arrivée']), border=1)
        pdf.ln()
    return bytes(pdf.output())


# ─── INTERFACE ───
st.set_page_config(page_title="SkyAssistant V49", layout="wide")

if 'waypoints' not in st.session_state:
    st.session_state.waypoints: List[Waypoint] = []
if 'calc_time' not in st.session_state:
    st.session_state.calc_time = dt.datetime.now(dt.timezone.utc)

with st.sidebar:
    st.title("✈️ SkyAssistant V49")
    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []

    if sugg and st.button(f"Départ : {sugg[0]}"):
        ap = AIRPORTS[sugg[0]]
        elev = get_elevation_ft(ap['lat'], ap['lon'])
        st.session_state.waypoints = [Waypoint(name=sugg[0], lat=ap['lat'], lon=ap['lon'], alt=elev, elev=elev, arr_type="Direct")]
        st.rerun()

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500)
    fuel_flow = st.number_input("Conso (L/h)", 5, 100, 25, step=1)   # ← tranches de 1 L

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗑️ Reset"):
            st.session_state.waypoints = []
            st.rerun()
    with col2:
        if st.button("🔄 Rafraîchir vents"):
            st.session_state.calc_time = dt.datetime.now(dt.timezone.utc)
            st.rerun()

# ─── METAR ───
metar_val = ""
if st.session_state.waypoints:
    metar_val = get_metar(st.session_state.waypoints[0].name)
    st.code(f"🕒 METAR {st.session_state.waypoints[0].name} : {metar_val}", language="bash")
    st.caption(f"🌬️ Météo calculée à {st.session_state.calc_time.strftime('%H:%M:%S UTC')}")

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0)
    alt_in = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Auto", True)
    m_wind = None if use_auto else {'wd': st.number_input("Dir", 0, 359), 'ws': st.number_input("Force", 0, 100)}

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        brng, la1, lo1 = math.radians(tc_in), math.radians(last.lat), math.radians(last.lon)
        la2 = math.degrees(la1 + (dist_in / EARTH_RADIUS_NM) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in / EARTH_RADIUS_NM) * math.sin(brng) / math.cos(la1))

        new_wp = Waypoint(name=f"WP{len(st.session_state.waypoints)}", lat=la2, lon=lo2, tc=tc_in,
                          dist=dist_in, alt=alt_in, manual_wind=m_wind,
                          elev=get_elevation_ft(la2, lo2), arr_type="Direct")
        st.session_state.waypoints.append(new_wp)
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(location=[st.session_state.waypoints[0].lat, st.session_state.waypoints[0].lon], zoom_start=9)
        folium.TileLayer(tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', attr='Google Satellite', name='Vue Satellite').add_to(m)
        folium.TileLayer('openstreetmap', name='Carte Standard').add_to(m)
        folium.PolyLine([[w.lat, w.lon] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)

        for i, w in enumerate(st.session_state.waypoints):
            color = "blue" if i == 0 else ("red" if i == len(st.session_state.waypoints)-1 else "orange")
            icon = "plane" if i == 0 else ("flag" if i == len(st.session_state.waypoints)-1 else "dot-circle-o")
            folium.Marker([w.lat, w.lon], popup=w.name, icon=folium.Icon(color=color, icon=icon, prefix="fa")).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=300, key="map_v49", returned_objects=[])

# ─── LOG DE NAVIGATION ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    nav_data = []
    dist_p = [0]
    alt_p = [st.session_state.waypoints[0].elev]
    terr_p = [st.session_state.waypoints[0].elev]
    d_total = 0.0
    fig = go.Figure()
    current_alt = st.session_state.waypoints[0].elev

    for i in range(1, len(st.session_state.waypoints)):
        w1 = st.session_state.waypoints[i-1]
        w2 = st.session_state.waypoints[i]
        wd, ws, src = get_wind_v27_final(w2.lat, w2.lon, w2.alt, st.session_state.calc_time, w2.manual_wind)

        wa = math.radians(wd - w2.tc)
        sin_wca = (ws / tas) * math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0
        gs = max(20, tas * math.cos(math.radians(wca)) - ws * math.cos(wa))

        hours = w2.dist / gs
        total_sec = hours * 3600
        fuel_branch = round(hours * fuel_flow, 1)
        alt_crois = w2.alt
        tt_str = ""

        # TOC & TOD (identique à avant)
        if alt_crois > current_alt:
            t_climb = ((alt_crois - current_alt) / v_climb) * 60
            d_climb = gs * (t_climb / 3600)
            if d_climb > 0.1:
                t_cl_str = f"{int(t_climb//60):02d}:{int(t_climb%60):02d}"
                tt_str += f"TOC:{round(d_climb,1)}NM "
                if d_climb < w2.dist:
                    dist_p.append(d_total + d_climb)
                    alt_p.append(alt_crois)
                    terr_p.append(w1.elev)
                    fig.add_annotation(x=d_total + d_climb, y=alt_crois, text=f"TOC {round(d_climb,1)}NM ({t_cl_str})", showarrow=True, ay=45)

        at = w2.arr_type
        if i == len(st.session_state.waypoints)-1 and at == "Direct":
            at = "VT (1500ft)"

        if at != "Direct":
            alt_t = w2.elev + (1500 if "VT" in at else 1000)
            t_desc = ((alt_crois - alt_t) / v_descent) * 60 if alt_crois > alt_t else 0
            d_desc = gs * (t_desc / 3600)
            if d_desc > 0.1:
                t_de_str = f"{int(t_desc//60):02d}:{int(t_desc%60):02d}"
                tt_str += f"TOD:{round(d_desc,1)}NM"
                if d_desc < w2.dist:
                    dist_p.append(d_total + (w2.dist - d_desc))
                    alt_p.append(alt_crois)
                    terr_p.append(w2.elev)
                    fig.add_annotation(x=d_total + (w2.dist - d_desc), y=alt_crois, text=f"TOD {round(d_desc,1)}NM ({t_de_str})", showarrow=True, ay=-45)

            label_dest = "VT" if "VT" in at else "TDP"
            fig.add_annotation(x=d_total + w2.dist, y=alt_t, text=f"<b>{label_dest} {w2.name}</b>", showarrow=False, yshift=15, font=dict(color="orange", size=11))

            d_total += w2.dist
            dist_p.extend([d_total, d_total])
            alt_p.extend([alt_t, w2.elev])
            terr_p.extend([w2.elev, w2.elev])
            fig.add_vline(x=d_total, line_width=2, line_dash="dash", line_color="orange")
            current_alt = w2.elev
        else:
            d_total += w2.dist
            dist_p.append(d_total)
            alt_p.append(alt_crois)
            terr_p.append(w2.elev)
            current_alt = alt_crois

        nav_data.append({
            "Branche": f"{w1.name}➔{w2.name}",
            "Vent": f"{int(wd)}/{int(ws)}kt ({src})",
            "GS": f"{int(gs)}kt",
            "EET": f"{int(total_sec//60):02d}:{int(total_sec%60):02d}",
            "Fuel": f"{fuel_branch}L",
            "TOC/TOD": tt_str.strip(),
            "Arrivée": at,
            "❌": False,
            "_idx": i
        })

    df_nav = pd.DataFrame(nav_data)

    st.subheader("📋 Log de Navigation")
    st.caption("Modifiez les noms, l’arrivée ou cochez ❌ puis cliquez sur **Appliquer**")

    with st.form("edit_log_form"):
        edited_log = st.data_editor(
            df_nav,
            column_config={
                "Branche": st.column_config.TextColumn("Branche", width="small"),
                "Vent": st.column_config.TextColumn("Vent", width="medium", disabled=True),
                "GS": st.column_config.TextColumn("GS", width="small", disabled=True),
                "EET": st.column_config.TextColumn("EET", width="small", disabled=True),
                "Fuel": st.column_config.TextColumn("Fuel", width="small", disabled=True),
                "TOC/TOD": st.column_config.TextColumn("TOC/TOD", width="small", disabled=True),
                "Arrivée": st.column_config.SelectboxColumn("Arrivée", options=["Direct", "TDP (1000ft)", "VT (1500ft)"], width="small"),
                "❌": st.column_config.CheckboxColumn("❌", width="small"),
                "_idx": None
            },
            hide_index=True
        )
        submitted = st.form_submit_button("✅ Appliquer modifications")

    if submitted and not edited_log.equals(df_nav):
        new_wps = [st.session_state.waypoints[0]]
        for _, row in edited_log.iterrows():
            if not row['❌']:
                wp_dict = st.session_state.waypoints[row['_idx']].__dict__.copy()
                if "➔" in row['Branche']:
                    wp_dict['name'] = row['Branche'].split("➔")[1]
                wp_dict['arr_type'] = row['Arrivée']
                new_wps.append(Waypoint(**wp_dict))
        st.session_state.waypoints = new_wps
        st.rerun()

    st.download_button("📥 Log PDF", data=create_pdf(df_nav.drop(columns=['❌', '_idx']), metar_val), file_name="nav_log.pdf")

    # Graphique SANS zoom ni modebar
    fig.add_trace(go.Scatter(x=dist_p, y=terr_p, fill='tozeroy', name='Relief', line_color='sienna'))
    fig.add_trace(go.Scatter(x=dist_p, y=alt_p, name='Profil Avion', line=dict(color='royalblue', width=4)))

    fig.update_layout(width=1000, height=350,
                      xaxis=dict(fixedrange=True, tickformat=".1f", title="Distance (NM)"),
                      yaxis=dict(fixedrange=True, title="Altitude (ft)"),
                      margin=dict(l=40, r=40, t=20, b=40), showlegend=False)

    st.markdown('<div style="overflow-x: auto; width: 100%; border: 1px solid #444; border-radius: 10px;">', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=False, config={'displayModeBar': False})
    st.markdown('</div>', unsafe_allow_html=True)
