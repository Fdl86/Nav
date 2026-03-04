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
NOAA_DECL_URL = "https://www.ngdc.noaa.gov/geomag-web/calculators/calculateDeclination"
PRESSURE_MAP = {1000: 975, 1500: 960, 2000: 950, 2500: 925, 3000: 900, 5000: 850, 7000: 750}
HTTP_TIMEOUT = 6

st.set_page_config(page_title="SkyAssistant V48", layout="wide")

# ─── HTTP SESSION (réutilisation TCP = plus rapide) ───
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "SkyAssistant/48"})
    return s

SESSION = get_http_session()

# ─── WAYPOINT HELPERS (support dict OU objet Waypoint) ───
WAYPOINT_KEYS = ["name", "lat", "lon", "alt", "elev", "arr_type", "tc", "dist", "manual_wind"]

def wp_get(wp, key, default=None):
    if isinstance(wp, dict):
        return wp.get(key, default)
    return getattr(wp, key, default)

def wp_to_dict(wp) -> dict:
    if isinstance(wp, dict):
        return wp
    d = {}
    for k in WAYPOINT_KEYS:
        v = getattr(wp, k, None)
        if v is not None:
            d[k] = v
    d.setdefault("arr_type", "Direct")
    return d

# ─── STATE ───
if "waypoints" not in st.session_state:
    st.session_state.waypoints = []
try:
    st.session_state.waypoints = [wp_to_dict(w) for w in st.session_state.waypoints]
except Exception:
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

# ─── DECLINAISON MAGNÉTIQUE (NOAA Geomag) ───
@st.cache_data(ttl=86400 * 30)  # 30 jours
def get_declination_deg(lat: float, lon: float, date_utc: dt.datetime) -> float:
    """
    Renvoie la déclinaison en degrés (East positive) à partir du service NOAA.
    Formule ensuite:
      Cap_mag = Cap_vrai - declinaison (E = least, W = best)
    """
    try:
        y, m, d = date_utc.year, date_utc.month, date_utc.day
        params = {
            "lat1": lat,
            "lon1": lon,
            "model": "WMM",
            "startYear": y,
            "startMonth": m,
            "startDay": d,
            "resultFormat": "json",
        }
        r = SESSION.get(NOAA_DECL_URL, params=params, timeout=HTTP_TIMEOUT)
        j = r.json()
        res0 = j.get("result", [{}])[0]
        dec = res0.get("declination", None)
        return float(dec) if dec is not None else 0.0
    except Exception:
        return 0.0

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
def create_pdf(df_nav_pdf: pd.DataFrame, metar_text: str):
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()

    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "LOG DE NAVIGATION - SKYASSISTANT", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(0, 8, "METAR DE DEPART :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=9)
    pdf.multi_cell(0, 6, str(metar_text).encode("ascii", "ignore").decode("ascii"), border=1)
    pdf.ln(4)

    # PDF compact: Rv + Cap (mag) + reste
    cols = ["Branche", "Rv", "Cap", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]
    w =   [30,       12,   12,    30,     12,   14,    14,     38,       26]

    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    for i, c in enumerate(cols):
        pdf.cell(w[i], 7, c, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("helvetica", size=8)
    for _, row in df_nav_pdf.iterrows():
        def cell(i, text, align="L"):
            pdf.cell(w[i], 7, str(text).encode("ascii", "ignore").decode("ascii"), border=1, align=align)

        cell(0, row.get("Branche", "").replace("➔", "->"))
        cell(1, row.get("Rv", ""), align="C")
        cell(2, row.get("Cap", ""), align="C")
        cell(3, row.get("Vent", ""))
        cell(4, row.get("GS", ""), align="C")
        cell(5, row.get("EET", ""), align="C")
        cell(6, row.get("Fuel", ""), align="C")
        cell(7, row.get("TOC/TOD", ""))
        cell(8, row.get("Arrivée", ""))
        pdf.ln()

    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return bytes(out)
    
# ─── HELPERS NAV ───
def norm360(x: float) -> float:
    return (x % 360.0 + 360.0) % 360.0

def fmt_deg(x: float) -> str:
    return f"{int(round(norm360(x))):03d}°"

# ─────────────────────────── UI ───────────────────────────
with st.sidebar:
    st.title("✈️ SkyAssistant V48")

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
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840, step=10)     # 10 fpm
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500, step=10) # 10 fpm
    fuel_flow = st.number_input("Conso (L/h)", 1, 200, 25, step=1)            # 1L

    st.markdown("---")
    # Optionnel mais utile pour ETA, pas obligatoire pour la nav: tu peux laisser à 00:00
    dep_time = st.time_input("Heure départ (UTC)", value=dt.time(0, 0))

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
    rv_in = st.number_input("Route Vraie (Rv) °", 0, 359, 0, step=1)
    dist_in = st.number_input("Distance (NM)", 0.1, 300.0, 15.0, step=0.1)
    alt_in = st.number_input("Alt Croisière (ft)", 1000, 12500, 2500, step=500)

    use_auto = st.toggle("Vent Auto", True)
    m_wind = None
    if not use_auto:
        m_wind = {
            "wd": st.number_input("Dir", 0, 359, 0, step=1),
            "ws": st.number_input("Force", 0, 100, 0, step=1),
        }

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last = st.session_state.waypoints[-1]
        R = 3440.065
        brng = math.radians(rv_in)
        la1, lo1 = math.radians(wp_get(last, "lat")), math.radians(wp_get(last, "lon"))
        la2 = math.degrees(la1 + (dist_in / R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in / R) * math.sin(brng) / max(1e-9, math.cos(la1)))

        st.session_state.waypoints.append({
            "name": f"WP{len(st.session_state.waypoints)}",
            "lat": la2,
            "lon": lo2,
            "tc": int(rv_in),            # on garde le champ "tc" mais c'est bien Rv
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

        folium.PolyLine(
            [[wp_get(w, "lat"), wp_get(w, "lon")] for w in st.session_state.waypoints],
            color="red",
            weight=3,
        ).add_to(m)

        num_w = len(st.session_state.waypoints)
        for i, w in enumerate(st.session_state.waypoints):
            if i == 0:
                icon_c, icon_t = "blue", "plane"
            elif i == num_w - 1:
                icon_c, icon_t = "red", "flag"
            else:
                icon_c, icon_t = "orange", "circle"  # point tournant

            folium.Marker(
                [wp_get(w, "lat"), wp_get(w, "lon")],
                popup=f"{wp_get(w, 'name')}",
                icon=folium.Icon(color=icon_c, icon=icon_t, prefix="fa"),
            ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=300, key="map_v48", returned_objects=[])

# ─── LOG DE NAVIGATION & PROFIL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")

    now_utc = dt.datetime.now(dt.timezone.utc)
    # heure départ (UTC) -> datetime aujourd'hui
    dep_dt = dt.datetime.combine(now_utc.date(), dep_time, tzinfo=dt.timezone.utc)

    nav_data = []
    dist_p = [0.0]
    alt_p = [float(wp_get(st.session_state.waypoints[0], "elev", 0))]
    terr_p = [float(wp_get(st.session_state.waypoints[0], "elev", 0))]
    d_total = 0.0
    fig = go.Figure()
    current_alt = float(wp_get(st.session_state.waypoints[0], "elev", 0))

    wind_local_cache = {}
    decl_local_cache = {}

    cum_sec = 0.0
    cum_fuel = 0.0

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i - 1], st.session_state.waypoints[i]

        rv = float(wp_get(w2, "tc", 0))          # Rv
        dist_nm = float(wp_get(w2, "dist", 0))
        alt_ft = float(wp_get(w2, "alt", 0))
        elev2 = float(wp_get(w2, "elev", 0))
        manual = wp_get(w2, "manual_wind", None)

        # ── VENT ──
        target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
        lv = PRESSURE_MAP[target]
        wkey = (round(float(wp_get(w2, "lat")), 3), round(float(wp_get(w2, "lon")), 3), lv, st.session_state.wx_refresh)

        if manual:
            wd, ws, src = float(manual["wd"]), float(manual["ws"]), "Manuel"
        else:
            if wkey in wind_local_cache:
                wd, ws, src = wind_local_cache[wkey]
            else:
                wd, ws, src = get_wind_v27_final(
                    float(wp_get(w2, "lat")),
                    float(wp_get(w2, "lon")),
                    alt_ft,
                    now_utc,
                    manual_wind=None,
                    wx_refresh=st.session_state.wx_refresh,
                )
                wind_local_cache[wkey] = (wd, ws, src)

        # ── WCA / GS ──
        wa = math.radians(wd - rv)
        sin_wca = (ws / max(1e-9, float(tas))) * math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0.0
        cap_vrai = norm360(rv + wca)

        gs = max(20.0, (float(tas) * math.cos(math.radians(wca))) - (ws * math.cos(wa)))

        # ── DECLINAISON (au point w2) ──
        dkey = (round(float(wp_get(w2, "lat")), 2), round(float(wp_get(w2, "lon")), 2), dep_dt.date().isoformat())
        if dkey in decl_local_cache:
            decl = decl_local_cache[dkey]
        else:
            decl = get_declination_deg(float(wp_get(w2, "lat")), float(wp_get(w2, "lon")), dep_dt)
            decl_local_cache[dkey] = decl

        cap_mag = norm360(cap_vrai - decl)  # E positive => subtract

        # ── Temps / Fuel ──
        hours = dist_nm / max(1e-9, gs)
        seg_sec = hours * 3600.0
        fuel_branch = round(hours * float(fuel_flow), 1)

        cum_sec += seg_sec
        cum_fuel += fuel_branch
        eta_dt = dep_dt + dt.timedelta(seconds=cum_sec)

        # ── TOC/TOD + profil ──
        alt_crois = alt_ft
        tt_str = ""

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

        # ── TABLE (compact) ──
        # Cap affiché = cap magnétique (le plus utile en vol). On met la dérive en petit.
        drift_txt = f"{wca:+.0f}°"  # dérive (WCA)
        cap_txt = f"{fmt_deg(cap_mag)} ({drift_txt})"

        nav_data.append({
            "Branche": f"{wp_get(w1,'name')}➔{wp_get(w2,'name')}",
            "Rv": f"{int(round(norm360(rv))):03d}",
            "Cap": cap_txt,
            "Vent": f"{int(wd)}/{int(ws)}kt ({src})",
            "GS": f"{int(gs)}",
            "EET": f"{int(seg_sec//60):02d}:{int(seg_sec%60):02d}",
            "Fuel": f"{fuel_branch:.1f}L",
            "ETA": eta_dt.strftime("%H:%M"),
            "TOC/TOD": tt_str.strip(),
            "Arrivée": at,
            "❌": False,
            "_idx": i,
        })

    st.subheader("📋 Log de Navigation")

    df_nav = pd.DataFrame(nav_data)

    # Affichage écran: compact (on évite de tout mettre)
    df_screen = df_nav[["Branche", "Cap", "Vent", "GS", "EET", "Fuel", "ETA", "TOC/TOD", "Arrivée", "❌", "_idx"]].copy()

    edited_log = st.data_editor(
        df_screen,
        column_config={
            "Branche": st.column_config.TextColumn("Branche", width="small"),
            "Cap": st.column_config.TextColumn("Cap (mag) (+dérive)", width="small", disabled=True),
            "Vent": st.column_config.TextColumn("Vent", width="medium", disabled=True),
            "GS": st.column_config.TextColumn("GS", width="small", disabled=True),
            "EET": st.column_config.TextColumn("EET", width="small", disabled=True),
            "Fuel": st.column_config.TextColumn("Fuel", width="small", disabled=True),
            "ETA": st.column_config.TextColumn("ETA", width="small", disabled=True),
            "TOC/TOD": st.column_config.TextColumn("TOC/TOD", width="small", disabled=True),
            "Arrivée": st.column_config.SelectboxColumn("Arrivée", options=["Direct", "TDP (1000ft)", "VT (1500ft)"], width="small"),
            "❌": st.column_config.CheckboxColumn("❌", width="small"),
            "_idx": None,
        },
        hide_index=True,
    )

    if not edited_log.equals(df_screen):
        new_wps = [st.session_state.waypoints[0]]
        for _, row in edited_log.iterrows():
            if not row["❌"]:
                wp = st.session_state.waypoints[int(row["_idx"])].copy()
                # on garde le nom (modifiable via delete/reorder), et on applique l'arrivée
                wp["arr_type"] = row["Arrivée"]
                new_wps.append(wp)
        st.session_state.waypoints = new_wps
        st.rerun()

    # PDF: un peu plus complet (Rv + Cap + etc), sans colonnes internes
    df_pdf = df_nav[["Branche", "Rv", "Cap", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]].copy()

    st.download_button(
        label="📥 Log PDF",
        data=create_pdf(df_pdf, metar_val),
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
    st.plotly_chart(
    fig,
    use_container_width=False,
    config={
        "displayModeBar": False,
        "staticPlot": True
    }
)
    st.markdown("</div>", unsafe_allow_html=True)
