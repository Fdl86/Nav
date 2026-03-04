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

# ─── HTTP SESSION (plus rapide que requests.get répétés) ───
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "SkyAssistant/47"})
    return s

SESSION = get_http_session()

# ─── STATE ───
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []
if "wx_refresh" not in st.session_state:
    st.session_state.wx_refresh = 0

# ─── AIRPORTS ───
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
    # wx_refresh dans la signature => permet "force refresh" sans attendre TTL
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

# ─── WIND (Open-Meteo) ───
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

    return bytes(pdf.output())

# ─────────────────────────── INTERFACE ───────────────────────────
with st.sidebar:
    st.title("✈️ SkyAssistant V47")

    # Bouton demandé: force refresh METAR + VENT
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

    tas = st.number_input("TAS (kt)", min_value=50, max_value=250, value=100, step=1)
    v_climb = st.number_input("Montée (ft/min)", min_value=100, max_value=2000, value=840, step=10)   # tranches de 10
    v_descent = st.number_input("Descente (ft/min)", min_value=100, max_value=2000, value=500, step=10) # tranches de 10
    fuel_flow = st.number_input("Conso (L/h)", min_value=1, max_value=200, value=25, step=1)           # cran de 1L

    if st.button("🗑️ Reset", use_container_width=True):
        st.session_state.waypoints = []
        st.rerun()

# ─── NAVIGATION & CARTE ───
metar_val = ""
if st.session_state.waypoints:
    dep_icao = st.session_state.waypoints[0]["name"]
    metar_val = get_metar_cached(dep_icao, st.session_state.wx_refresh)
    st.code(f"🕒 METAR {dep_icao} : {metar_val}", language="bash")

col_map, col_ctrl = st.columns([2, 1])

with col_ctrl:
    st.subheader("📍 Ajouter Segment")
    tc_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, step=1)
    dist_in = st.number_input("Distance (NM)", 0.1, 100.0, 15.0, step=0.1)
    alt_in = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)

    use_auto = st.toggle("Vent Auto", True)
    m_wind = None if use_auto else {
        "wd": st.number_input("Dir", 0, 359, 0, step=1),
        "ws": st.number_input("Force", 0, 100, 0, step=1),
    }

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065

        brng = math.radians(tc_in)
        la1, lo1 = math.radians(last["lat"]), math.radians(last["lon"])
        la2 = math.degrees(la1 + (dist_in / R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in / R) * math.sin(brng) / max(1e-9, math.cos(la1)))

        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": la2,
            "lon": lo2,
            "tc": tc_in,
            "dist": dist_in,
            "alt": alt_in,
            "manual_wind": m_wind,
            "elev": get_elevation_ft(la2, lo2),
            "arr_type": "Direct",
        })
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(
            location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]],
            zoom_start=9
        )
        folium.TileLayer(
            tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
            attr="Google Satellite",
            name="Vue Satellite",
            overlay=False,
            control=True
        ).add_to(m)
        folium.TileLayer("openstreetmap", name="Carte Standard").add_to(m)

        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)

        num_w = len(st.session_state.waypoints)
        for i, w in enumerate(st.session_state.waypoints):
            # Icônes + cohérence visuelle (point tournant plus raccord)
            if i == 0:
                icon_c, icon_t = "blue", "plane"
            elif i == num_w - 1:
                icon_c, icon_t = "red", "flag"
            else:
                # Avant: dot-circle-o. Maintenant: "circle" (plus proche des autres icônes)
                icon_c, icon_t = "orange", "circle"

            folium.Marker(
                [w["lat"], w["lon"]],
                popup=f"{w['name']}",
                icon=folium.Icon(color=icon_c, icon=icon_t, prefix="fa"),
            ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=300, key="map_v47", returned_objects=[])

# ─── LOG DE NAVIGATION & PROFIL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")

    curr_t = dt.datetime.now(dt.timezone.utc)

    nav_data = []
    dist_p = [0]
    alt_p = [st.session_state.waypoints[0]["elev"]]
    terr_p = [st.session_state.waypoints[0]["elev"]]

    d_total = 0.0
    fig = go.Figure()
    current_alt = st.session_state.waypoints[0]["elev"]

    # cache local vent (évite recalcul si segments identiques)
    wind_local_cache = {}

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i - 1], st.session_state.waypoints[i]

        # Dédup clé vent : (lat/lon arrondis, pressure level, refresh token)
        target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - w2["alt"]))
        lv = PRESSURE_MAP[target]
        key = (round(w2["lat"], 3), round(w2["lon"], 3), lv, st.session_state.wx_refresh)

        if w2.get("manual_wind"):
            wd, ws, src = float(w2["manual_wind"]["wd"]), float(w2["manual_wind"]["ws"]), "Manuel"
        else:
            if key in wind_local_cache:
                wd, ws, src = wind_local_cache[key]
            else:
                wd, ws, src = get_wind_v27_final(
                    w2["lat"], w2["lon"], w2["alt"], curr_t,
                    manual_wind=None,
                    wx_refresh=st.session_state.wx_refresh
                )
                wind_local_cache[key] = (wd, ws, src)

        wa = math.radians(wd - w2["tc"])
        sin_wca = (ws / max(1e-9, tas)) * math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0.0
        gs = max(20.0, (tas * math.cos(math.radians(wca))) - (ws * math.cos(wa)))

        hours = (w2["dist"] / max(1e-9, gs))
        total_sec = hours * 3600.0
        fuel_branch = round(hours * float(fuel_flow), 1)

        alt_crois = w2["alt"]
        tt_str = ""

        # CALCUL TOC
        if alt_crois > current_alt:
            t_climb = ((alt_crois - current_alt) / max(1e-9, v_climb)) * 60.0
            d_climb = (gs * (t_climb / 3600.0))
            if d_climb > 0.1:
                t_cl_str = f"{int(t_climb // 60):02d}:{int(t_climb % 60):02d}"
                tt_str += f"TOC:{round(d_climb, 1)}NM "
                if d_climb < w2["dist"]:
                    dist_p.append(d_total + d_climb)
                    alt_p.append(alt_crois)
                    terr_p.append(w1["elev"])
                    fig.add_annotation(
                        x=d_total + d_climb,
                        y=alt_crois,
                        text=f"TOC {round(d_climb,1)}NM ({t_cl_str})",
                        showarrow=True,
                        ay=45,
                    )

        # GESTION ARRIVÉE (TDP / VT)
        at = w2.get("arr_type", "Direct")
        if (i == len(st.session_state.waypoints) - 1) and at == "Direct":
            at = "VT (1500ft)"

        if at != "Direct":
            alt_t = w2["elev"] + (1500 if "VT" in at else 1000)
            t_desc = ((alt_crois - alt_t) / max(1e-9, v_descent)) * 60.0 if alt_crois > alt_t else 0.0
            d_desc = (gs * (t_desc / 3600.0))
            if d_desc > 0.1:
                t_de_str = f"{int(t_desc // 60):02d}:{int(t_desc % 60):02d}"
                tt_str += f"TOD:{round(d_desc,1)}NM"
                if d_desc < w2["dist"]:
                    dist_p.append(d_total + (w2["dist"] - d_desc))
                    alt_p.append(alt_crois)
                    terr_p.append(w2["elev"])
                    fig.add_annotation(
                        x=d_total + (w2["dist"] - d_desc),
                        y=alt_crois,
                        text=f"TOD {round(d_desc,1)}NM ({t_de_str})",
                        showarrow=True,
                        ay=-45,
                    )

            label_dest = "VT" if "VT" in at else "TDP"
            fig.add_annotation(
                x=d_total + w2["dist"],
                y=alt_t,
                text=f"<b>{label_dest} {w2['name']}</b>",
                showarrow=False,
                yshift=15,
                font=dict(color="orange", size=11),
            )

            d_total += w2["dist"]
            dist_p.append(d_total); alt_p.append(alt_t); terr_p.append(w2["elev"])
            dist_p.append(d_total); alt_p.append(w2["elev"]); terr_p.append(w2["elev"])
            fig.add_vline(x=d_total, line_width=2, line_dash="dash", line_color="orange")
            current_alt = w2["elev"]

        else:
            d_total += w2["dist"]
            dist_p.append(d_total); alt_p.append(alt_crois); terr_p.append(w2["elev"])
            current_alt = alt_crois

        nav_data.append({
            "Branche": f"{w1['name']}➔{w2['name']}",
            "Vent": f"{int(wd)}/{int(ws)}kt ({src})",
            "GS": f"{int(gs)}kt",
            "EET": f"{int(total_sec // 60):02d}:{int(total_sec % 60):02d}",
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
            "Arrivée": st.column_config.SelectboxColumn(
                "Arrivée",
                options=["Direct", "TDP (1000ft)", "VT (1500ft)"],
                width="small",
            ),
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

    st.markdown(
        '<div style="overflow-x: auto; width: 100%; border: 1px solid #444; border-radius: 10px;">',
        unsafe_allow_html=True,
    )
    st.plotly_chart(fig, use_container_width=False, config={"staticPlot": True, "displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)
