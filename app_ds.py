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
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
HTTP_TIMEOUT = 6

st.set_page_config(page_title="SkyAssistant V47", layout="wide")


# ─── HTTP SESSION (réutilisation TCP = plus rapide) ───
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "SkyAssistant/47"})
    return s


SESSION = get_http_session()


# ─── WAYPOINT HELPERS (support dict OU objet Waypoint) ───
WAYPOINT_KEYS = ["name", "lat", "lon", "alt", "elev", "arr_type", "tc", "dist", "manual_wind"]


def wp_get(wp, key, default=None):
    if isinstance(wp, dict):
        return wp.get(key, default)
    return getattr(wp, key, default)


def wp_set(wp, key, value):
    if isinstance(wp, dict):
        wp[key] = value
    else:
        setattr(wp, key, value)


def wp_to_dict(wp) -> dict:
    """Normalise en dict pour éviter les erreurs 'object is not subscriptable' et simplifier le code."""
    if isinstance(wp, dict):
        return wp
    d = {}
    for k in WAYPOINT_KEYS:
        v = getattr(wp, k, None)
        if v is not None:
            d[k] = v
    # valeurs par défaut minimales
    d.setdefault("arr_type", "Direct")
    return d


# ─── STATE ───
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []

# Normalisation (si tu avais déjà des objets Waypoint en session_state)
try:
    st.session_state.waypoints = [wp_to_dict(w) for w in st.session_state.waypoints]
except Exception:
    st.session_state.waypoints = []

if "wx_refresh" not in st.session_state:
    st.session_state.wx_refresh = 0


# ─── DATA (AIRPORTS) ───
@st.cache_data(ttl=86400)
def load_airports():
    base = {"LFBI": {"name": "Poitiers Biard", "lat": 46.5877, "lon": 0.3069}}
    try:
        df = pd.read_csv(
            "https://ourairports.com/data/airports.csv",
            usecols=["ident", "name", "latitude_deg", "longitude_deg", "iso_country", "type"],
        )
        fr = df[(df["iso_country"] == "FR") & (df["type"].isin(["large_airport", "medium_airport", "small_airport"]))]
        downloaded = {
            row["ident"]: {"name": row["name"], "lat": float(row["latitude_deg"]), "lon": float(row["longitude_deg"])}
            for _, row in fr.iterrows()
        }
        base.update(downloaded)
        return base
    except Exception:
        return base


AIRPORTS = load_airports()


# ─── ELEVATION ───
@st.cache_data(ttl=86400)
def get_elevation_ft(lat: float, lon: float) -> int:
    try:
        r = SESSION.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}, timeout=HTTP_TIMEOUT)
        j = r.json()
        return round(j.get("elevation", [0])[0] * 3.28084)
    except Exception:
        return 0


# ─── METAR ───
@st.cache_data(ttl=600)  # 10 min
def get_metar_cached(icao: str, wx_refresh: int) -> str:
    # wx_refresh dans la signature => "force refresh" sans attendre TTL
    try:
        r = SESSION.get(
            f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            lines = r.text.splitlines()
            return lines[1] if len(lines) > 1 else "METAR indisponible"
        return "METAR indisponible"
    except Exception:
        return "Erreur METAR"


# ─── WIND ───
@st.cache_data(ttl=900)  # 15 min
def get_wind_openmeteo_cached(lat: float, lon: float, lv: int, wx_refresh: int) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
        "models": "icon_d2,meteofrance_arome_france_hd,gfs_seamless",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
    }
    r = SESSION.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
    return r.json()


def get_wind_v27_final(lat, lon, alt_ft, time_dt, manual_wind=None, wx_refresh: int = 0):
    if manual_wind:
        return float(manual_wind["wd"]), float(manual_wind["ws"]), "Manuel"

    target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
    lv = PRESSURE_MAP[target]

    try:
        r = get_wind_openmeteo_cached(lat, lon, lv, wx_refresh)
        h = r.get("hourly", {})
        times = h.get("time", [])
        if not times:
            return 0.0, 0.0, "Err"

        def pick(prefix: str):
            ws = h.get(f"wind_speed_{lv}hPa_{prefix}")
            wd = h.get(f"wind_direction_{lv}hPa_{prefix}")
            if ws and wd and ws[0] is not None and wd[0] is not None:
                return wd, ws
            return None

        picked = pick("icon_d2")
        if picked:
            wd_arr, ws_arr, src = picked[0], picked[1], "ICON-D2"
        else:
            picked = pick("meteofrance_arome_france_hd")
            if picked:
                wd_arr, ws_arr, src = picked[0], picked[1], "AROME"
            else:
                wd_arr = h.get(f"wind_direction_{lv}hPa_gfs_seamless", [])
                ws_arr = h.get(f"wind_speed_{lv}hPa_gfs_seamless", [])
                src = "GFS"

        # nearest time (timestamp compare)
        t_target = time_dt.timestamp()
        best_i, best_d = 0, float("inf")
        for i, t in enumerate(times):
            ts = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp()
            d = abs(ts - t_target)
            if d < best_d:
                best_d, best_i = d, i

        return float(wd_arr[best_i]), float(ws_arr[best_i]), src
    except Exception:
        return 0.0, 0.0, "Err"


# ─── PDF ───
def create_pdf(df_nav, metar_text):
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()

    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(0, 8, "METAR DE DEPART :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=9)
    pdf.multi_cell(0, 6, str(metar_text).encode("ascii", "ignore").decode("ascii"), border=1)
    pdf.ln(5)

    w = [30, 35, 15, 20, 15, 45, 30]
    cols = ["Branche", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]

    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for i in range(len(cols)):
        pdf.cell(w[i], 8, cols[i], border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for _, row in df_nav.iterrows():
        pdf.cell(w[0], 8, str(row["Branche"]).replace("➔", "->").encode("ascii", "ignore").decode("ascii"), border=1)
        pdf.cell(w[1], 8, str(row["Vent"]), border=1)
        pdf.cell(w[2], 8, str(row["GS"]), border=1, align="C")
        pdf.cell(w[3], 8, str(row["EET"]), border=1, align="C")
        pdf.cell(w[4], 8, str(row["Fuel"]), border=1, align="C")
        pdf.cell(w[5], 8, str(row["TOC/TOD"]), border=1)
        pdf.cell(w[6], 8, str(row["Arrivée"]), border=1)
        pdf.ln()

    # robust bytes output
    try:
        return pdf.output(dest="S").encode("latin-1")
    except Exception:
        return bytes(pdf.output())


# ─── INTERFACE ───
with st.sidebar:
    st.title("✈️ SkyAssistant V47")

    # bouton demandé
    if st.button("🔄 Rafraîchir météo", use_container_width=True):
        st.session_state.wx_refresh += 1
        st.rerun()

    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []
    if sugg and st.button(f"Départ : {sugg[0]}", use_container_width=True):
        ap = AIRPORTS[sugg[0]]
        elev = get_elevation_ft(ap["lat"], ap["lon"])
        st.session_state.waypoints = [{
            "name": sugg[0],
            "lat": ap["lat"],
            "lon": ap["lon"],
            "alt": elev,
            "elev": elev,
            "arr_type": "Direct",
        }]
        st.rerun()

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100, step=1)

    # demandé: tranches de 10 fpm
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840, step=10)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500, step=10)

    # demandé: cran de 1L
    fuel_flow = st.number_input("Conso (L/h)", 1, 200, 25, step=1)

    if st.button("🗑️ Reset", use_container_width=True):
        st.session_state.waypoints = []
        st.rerun()


# ─── METAR ───
metar_val = ""
if st.session_state.waypoints:
    dep_icao = wp_get(st.session_state.waypoints[0], "name")
    metar_val = get_metar_cached(dep_icao, st.session_state.wx_refresh)
    st.code(f"🕒 METAR {dep_icao} : {metar_val}", language="bash")


# ─── NAVIGATION & CARTE ───
col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, step=1)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0, step=0.1)
    alt_in = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)
    use_auto = st.toggle("Vent Auto", True)

    m_wind = None
    if not use_auto:
        m_wind = {"wd": st.number_input("Dir", 0, 359, 0, step=1), "ws": st.number_input("Force", 0, 100, 0, step=1)}

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065
        brng = math.radians(tc_in)
        la1, lo1 = math.radians(wp_get(last, "lat")), math.radians(wp_get(last, "lon"))

        la2 = math.degrees(la1 + (dist_in / R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in / R) * math.sin(brng) / max(1e-9, math.cos(la1)))

        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": la2,
            "lon": lo2,
            "tc": tc_in,
            "dist": float(dist_in),
            "alt": int(alt_in),
            "manual_wind": m_wind,
            "elev": get_elevation_ft(la2, lo2),
            "arr_type": "Direct",
        })
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        lat0 = wp_get(st.session_state.waypoints[0], "lat")
        lon0 = wp_get(st.session_state.waypoints[0], "lon")

        m = folium.Map(location=[lat0, lon0], zoom_start=9)
        folium.TileLayer(
            tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
            attr="Google Satellite",
            name="Vue Satellite",
            overlay=False,
            control=True,
        ).add_to(m)
        folium.TileLayer("openstreetmap", name="Carte Standard").add_to(m)

        folium.PolyLine([[wp_get(w, "lat"), wp_get(w, "lon")] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)

        num_w = len(st.session_state.waypoints)
        for i, w in enumerate(st.session_state.waypoints):
            if i == 0:
                icon_c, icon_t = "blue", "plane"
            elif i == num_w - 1:
                icon_c, icon_t = "red", "flag"
            else:
                # demandé: point tournant plus raccord
                icon_c, icon_t = "orange", "circle"

            folium.Marker(
                [wp_get(w, "lat"), wp_get(w, "lon")],
                popup=f"{wp_get(w, 'name')}",
                icon=folium.Icon(color=icon_c, icon=icon_t, prefix="fa"),
            ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=300, key="map_v47", returned_objects=[])


# ─── LOG DE NAVIGATION & PROFIL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    curr_t = dt.datetime.now(dt.timezone.utc)

    nav_data = []
    dist_p = [0.0]
    alt_p = [float(wp_get(st.session_state.waypoints[0], "elev", 0))]
    terr_p = [float(wp_get(st.session_state.waypoints[0], "elev", 0))]
    d_total = 0.0
    fig = go.Figure()
    current_alt = float(wp_get(st.session_state.waypoints[0], "elev", 0))

    # cache local vent (évite appels répétés)
    wind_local_cache = {}

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i - 1], st.session_state.waypoints[i]

        tc = float(wp_get(w2, "tc", 0))
        dist_nm = float(wp_get(w2, "dist", 0))
        alt_ft = float(wp_get(w2, "alt", 0))
        elev2 = float(wp_get(w2, "elev", 0))
        manual = wp_get(w2, "manual_wind", None)

        # vent: dédup par (lat/lon arrondis, level, refresh token)
        target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
        lv = PRESSURE_MAP[target]
        key = (round(float(wp_get(w2, "lat")), 3), round(float(wp_get(w2, "lon")), 3), lv, st.session_state.wx_refresh)

        if manual:
            wd, ws, src = float(manual["wd"]), float(manual["ws"]), "Manuel"
        else:
            if key in wind_local_cache:
                wd, ws, src = wind_local_cache[key]
            else:
                wd, ws, src = get_wind_v27_final(
                    float(wp_get(w2, "lat")), float(wp_get(w2, "lon")), alt_ft, curr_t, manual_wind=None, wx_refresh=st.session_state.wx_refresh
                )
                wind_local_cache[key] = (wd, ws, src)

        wa = math.radians(wd - tc)
        sin_wca = (ws / max(1e-9, float(tas))) * math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0.0
        gs = max(20.0, (float(tas) * math.cos(math.radians(wca))) - (ws * math.cos(wa)))

        hours = dist_nm / max(1e-9, gs)
        total_sec = hours * 3600.0
        fuel_branch = round(hours * float(fuel_flow), 1)

        alt_crois = alt_ft
        tt_str = ""

        # CALCUL TOC
        if alt_crois > current_alt:
            t_climb = ((alt_crois - current_alt) / max(1e-9, float(v_climb))) * 60.0
            d_climb = gs * (t_climb / 3600.0)
            if d_climb > 0.1:
                t_cl_str = f"{int(t_climb//60):02d}:{int(t_climb%60):02d}"
                tt_str += f"TOC:{round(d_climb,1)}NM "
                if d_climb < dist_nm:
                    dist_p.append(d_total + d_climb)
                    alt_p.append(alt_crois)
                    terr_p.append(float(wp_get(w1, "elev", 0)))
                    fig.add_annotation(
                        x=d_total + d_climb,
                        y=alt_crois,
                        text=f"TOC {round(d_climb,1)}NM ({t_cl_str})",
                        showarrow=True,
                        ay=45,
                    )

        # GESTION ARRIVÉE (TDP / VT)
        at = wp_get(w2, "arr_type", "Direct")
        if (i == len(st.session_state.waypoints) - 1) and at == "Direct":
            at = "VT (1500ft)"

        if at != "Direct":
            alt_t = elev2 + (1500 if "VT" in at else 1000)
            t_desc = ((alt_crois - alt_t) / max(1e-9, float(v_descent))) * 60.0 if alt_crois > alt_t else 0.0
            d_desc = gs * (t_desc / 3600.0)

            if d_desc > 0.1:
                t_de_str = f"{int(t_desc//60):02d}:{int(t_desc%60):02d}"
                tt_str += f"TOD:{round(d_desc,1)}NM"
                if d_desc < dist_nm:
                    dist_p.append(d_total + (dist_nm - d_desc))
                    alt_p.append(alt_crois)
                    terr_p.append(elev2)
                    fig.add_annotation(
                        x=d_total + (dist_nm - d_desc),
                        y=alt_crois,
                        text=f"TOD {round(d_desc,1)}NM ({t_de_str})",
                        showarrow=True,
                        ay=-45,
                    )

            label_dest = "VT" if "VT" in at else "TDP"
            fig.add_annotation(
                x=d_total + dist_nm,
                y=alt_t,
                text=f"<b>{label_dest} {wp_get(w2,'name')}</b>",
                showarrow=False,
                yshift=15,
                font=dict(color="orange", size=11),
            )

            d_total += dist_nm
            dist_p.append(d_total); alt_p.append(alt_t); terr_p.append(elev2)
            dist_p.append(d_total); alt_p.append(elev2); terr_p.append(elev2)
            fig.add_vline(x=d_total, line_width=2, line_dash="dash", line_color="orange")
            current_alt = elev2
        else:
            d_total += dist_nm
            dist_p.append(d_total); alt_p.append(alt_crois); terr_p.append(elev2)
            current_alt = alt_crois

        nav_data.append({
            "Branche": f"{wp_get(w1,'name')}➔{wp_get(w2,'name')}",
            "Vent": f"{int(wd)}/{int(ws)}kt ({src})",
            "GS": f"{int(gs)}kt",
            "EET": f"{int(total_sec//60):02d}:{int(total_sec%60):02d}",
            "Fuel": f"{fuel_branch}L",
            "TOC/TOD": tt_str.strip(),
            "Arrivée": at,
            "❌": False,
            "_idx": i,
        })

    st.subheader("📋 Log de Navigation")
    df_nav = pd.DataFrame(nav_data)

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
            "_idx": None,
        },
        hide_index=True,
    )

    if not edited_log.equals(df_nav):
        new_wps = [st.session_state.waypoints[0]]
        for _, row in edited_log.iterrows():
            if not row["❌"]:
                wp = st.session_state.waypoints[int(row["_idx"])].copy()
                if "➔" in row["Branche"]:
                    wp["name"] = row["Branche"].split("➔")[1]
                wp["arr_type"] = row["Arrivée"]
                new_wps.append(wp)
        st.session_state.waypoints = new_wps
        st.rerun()

    st.download_button(
        label="📥 Log PDF",
        data=create_pdf(df_nav.drop(columns=["❌", "_idx"]), metar_val),
        file_name="nav_log.pdf",
        use_container_width=True,
    )

    # ─── GRAPHIQUE SCROLLABLE ───
    fig.add_trace(go.Scatter(x=dist_p, y=terr_p, fill="tozeroy", name="Relief", line_color="sienna"))
    fig.add_trace(go.Scatter(x=dist_p, y=alt_p, name="Profil Avion", line=dict(color="royalblue", width=4)))
    fig.update_layout(
        width=1000,
        height=350,
        xaxis=dict(fixedrange=True, tickformat=".1f", title="Distance (NM)"),
        yaxis=dict(fixedrange=True, title="Altitude (ft)"),
        margin=dict(l=40, r=40, t=20, b=40),
        showlegend=False,
    )
    st.markdown('<div style="overflow-x: auto; width: 100%; border: 1px solid #444; border-radius: 10px;">', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=False, config={"staticPlot": True, "displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)
