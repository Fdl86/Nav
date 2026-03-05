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
HTTP_TIMEOUT = 8

st.set_page_config(page_title="SkyAssistant V53.1", layout="wide")

# ─── HIDE STREAMLIT DATAFRAME TOOLBAR ───
st.markdown(
    """
<style>
div[data-testid="stDataFrame"] [data-testid="stElementToolbar"],
div[data-testid="stDataEditor"] [data-testid="stElementToolbar"] {
    display: none !important;
}
</style>
""",
    unsafe_allow_html=True,
)

# ─── HTTP SESSION ───
@st.cache_resource
def get_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "SkyAssistant/53.1"})
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

# ─── ELEVATION (ne pas cacher un 0 en cas d'échec) ───
@st.cache_data(ttl=86400)
def _elevation_ft_cached(lat: float, lon: float) -> int:
    r = SESSION.get(ELEVATION_URL, params={"latitude": lat, "longitude": lon}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return round(j.get("elevation", [0])[0] * 3.28084)


def get_elevation_ft(lat: float, lon: float) -> int:
    try:
        return _elevation_ft_cached(lat, lon)
    except Exception:
        return 0


# ─── METAR ───
@st.cache_data(ttl=600)
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


# ─── DECLINAISON ───
@st.cache_data(ttl=86400 * 30)
def get_declination_deg(lat: float, lon: float, date_utc: dt.datetime) -> float:
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
@st.cache_data(ttl=900)
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


# ─── NAV HELPERS ───
def norm360(x: float) -> float:
    return (x % 360.0 + 360.0) % 360.0


def fmt_deg(x: float) -> str:
    return f"{int(round(norm360(x))):03d}°"


# ─── PDF ───

def _pdf_safe(s: object) -> str:
    """Compat core fonts fpdf2 (latin-1)."""
    if s is None:
        return ""
    s = str(s)
    s = s.replace("➔", "->").replace("→", "->").replace("—", "-").replace("–", "-")
    return s.encode("latin-1", "ignore").decode("latin-1")

def create_pdf(df_nav, metar_text: str):
    """
    Génère la fiche NAV A4 paysage en utilisant lognava5.png comme fond.
    IMPORTANT: lognava5.png doit être accessible (même dossier ou chemin correct).
    """

    # --- Template PNG connu ---
    # Ton lognava5.png est ~2048 x 1448 px.
    # On l'étire en A4 paysage: 297 x 210 mm.
    TEMPLATE_W_PX = 2048
    TEMPLATE_H_PX = 1448
    PAGE_W_MM = 297.0
    PAGE_H_MM = 210.0

    sx = PAGE_W_MM / TEMPLATE_W_PX
    sy = PAGE_H_MM / TEMPLATE_H_PX

    def px(x: float) -> float:  # px -> mm (x)
        return x * sx

    def py(y: float) -> float:  # px -> mm (y)
        return y * sy

    def put_in_box(x0_px, y0_px, x1_px, y1_px, text, align="C", size=7, bold=False, pad_px=3):
        """Écrit un texte centré/aligné dans une 'case' définie en pixels (du template)."""
        txt = _pdf_safe(text)
        if not txt:
            return
        x0 = px(x0_px + pad_px)
        y0 = py(y0_px + 1)  # léger offset vertical
        w = px((x1_px - x0_px) - 2 * pad_px)
        h = py(y1_px - y0_px) - py(1)

        pdf.set_xy(x0, y0)
        pdf.set_font("helvetica", "B" if bold else "", size)
        pdf.cell(w, h, txt, border=0, ln=0, align=align)

    # ─── PDF ───
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.add_page()
    pdf.image("lognava5.png", x=0, y=0, w=PAGE_W_MM, h=PAGE_H_MM)
    pdf.set_text_color(0, 0, 0)

    # ─────────────────────────────────────────────
    # 1) Champs haut de page (AD départ / arrivée)
    # ─────────────────────────────────────────────
    # Cases repérées visuellement sur ton template
    # (tu pourras ajuster 2-3 px si jamais ton fichier change)
    #
    # AD Départ : zone (env.)
    AD_DEP_BOX = (10, 10, 280, 40)      # on écrira plutôt à gauche dans la case
    AD_ARR_BOX = (10, 70, 280, 100)

    # Déduction simple depuis les branches si possible
    dep_icao = ""
    arr_icao = ""
    try:
        if len(df_nav) > 0 and "Branche" in df_nav.columns:
            first_b = str(df_nav.iloc[0]["Branche"])
            last_b = str(df_nav.iloc[-1]["Branche"])
            if "->" in first_b:
                dep_icao = first_b.split("->")[0].strip()
            elif "➔" in first_b:
                dep_icao = first_b.split("➔")[0].strip()
            if "->" in last_b:
                arr_icao = last_b.split("->")[-1].strip()
            elif "➔" in last_b:
                arr_icao = last_b.split("➔")[-1].strip()
    except Exception:
        pass

    # Écriture dans les cases (align gauche)
    put_in_box(AD_DEP_BOX[0], AD_DEP_BOX[1], AD_DEP_BOX[2], AD_DEP_BOX[3], dep_icao, align="L", size=9, bold=True, pad_px=18)
    put_in_box(AD_ARR_BOX[0], AD_ARR_BOX[1], AD_ARR_BOX[2], AD_ARR_BOX[3], arr_icao, align="L", size=9, bold=True, pad_px=18)

    # METAR : on le met sous AD départ (ligne libre)
    put_in_box(10, 42, 720, 65, metar_text, align="L", size=7, bold=False, pad_px=18)

    # ─────────────────────────────────────────────
    # 2) Tableau NAV — coordonnées des colonnes
    # ─────────────────────────────────────────────
    # On s'appuie sur les lignes/colonnes réelles du template (détectées).
    # Verticales principales (px) : 96,234,289,344,399,564,618,673,728,783,1140
    # Carburant (bleu) : 892,948,1029 (+ bord table à 1140)
    #
    # Lignes horizontales des premières rangées : y=310,337,364,392,419 ...
    # Donc une hauteur de ligne ~27 px.
    #
    ROW_TOP0 = 310
    ROW_H = 27
    MAX_ROWS = 10  # nombre de lignes visibles sur ta fiche (ajuste si tu veux)

    # Colonnes (x0,x1) en pixels
    COL_ZMINI   = (96, 234)
    COL_RM      = (234, 289)
    COL_X       = (289, 344)
    COL_CM      = (344, 399)
    COL_XMAX    = (399, 564)   # on y met le VENT (large)
    COL_REPERE  = (564, 618)   # on y met la BRANCHE (repère)
    COL_DIST    = (673, 728)   # distance
    COL_TSV     = (728, 783)   # TAS
    # Zone 783-892 non séparée en noir dans l'image => on découpe en sous-colonnes "logiques"
    COL_TAV     = (783, 820)   # GS
    COL_HE      = (820, 856)   # ETA
    COL_HR      = (856, 892)   # (optionnel) EET ou vide
    # Carburant bleu
    COL_CONSO   = (892, 948)   # Conso
    COL_RESTE   = (948, 1029)  # Reste (si tu l'as)
    # le reste du tableau (1029-1140) on laisse (zone vide / extension)

    # Cumul fuel pour éventuellement remplir "reste" si tu veux (ici on le laisse vide si pas dispo)
    # Si tu as une colonne "Fuel" genre "3.8L", on peut faire un cumul.
    fuel_cum = 0.0

    for i in range(min(len(df_nav), MAX_ROWS)):
        y0 = ROW_TOP0 + i * ROW_H
        y1 = y0 + ROW_H

        r = df_nav.iloc[i]

        branche = r.get("Branche", "")
        cap = r.get("Cap", "")              # ex "193° (-7°)" -> on en extrait le cap
        vent = r.get("Vent", "")
        gs = r.get("GS", "")
        eet = r.get("EET", "")
        eta = r.get("ETA", "")
        dist = r.get("Dist", "") if "Dist" in df_nav.columns else r.get("Distance", "")
        tsv = r.get("TSV", "") if "TSV" in df_nav.columns else ""  # sinon vide
        fuel = r.get("Fuel", "")            # "3.8L"

        # --- Extraction de valeurs plus "cases" ---
        # Cm = cap mag (les 3 chiffres au début)
        cm_val = ""
        try:
            s = str(cap).strip()
            # récup "193" dans "193° (-7°)"
            cm_val = "".join([c for c in s[:4] if c.isdigit()])
            if len(cm_val) == 2:  # rare
                cm_val = "0" + cm_val
        except Exception:
            cm_val = ""

        # X (dérive) : on récupère ce qu'il y a entre parenthèses si possible
        x_val = ""
        try:
            s = str(cap)
            if "(" in s and ")" in s:
                inside = s.split("(", 1)[1].split(")", 1)[0].strip()
                # inside ex: "-7°" ou "+5°"
                inside = inside.replace("°", "")
                x_val = inside
        except Exception:
            x_val = ""

        # Fuel conso numérique
        fuel_num = None
        try:
            fs = str(fuel).replace(",", ".").strip().upper()
            if fs.endswith("L"):
                fuel_num = float(fs[:-1])
        except Exception:
            fuel_num = None

        if fuel_num is not None:
            fuel_cum += fuel_num

        # --- Placement dans les cases ---
        # Zmini : on laisse vide (tu pourras y mettre une alt mini plus tard)
        # Rm : si tu as une colonne "Rm" on la met, sinon vide
        rm_val = r.get("Rm", "") if "Rm" in df_nav.columns else ""
        put_in_box(*COL_RM, y0, y1, rm_val, align="C", size=7)

        # X : dérive
        put_in_box(*COL_X, y0, y1, x_val, align="C", size=7)

        # Cm : cap mag
        put_in_box(*COL_CM, y0, y1, cm_val, align="C", size=7)

        # Vent (colonne large)
        put_in_box(*COL_XMAX, y0, y1, vent, align="L", size=7)

        # Repère : branche (on met le segment ici, c'est le plus logique sur ce template)
        put_in_box(*COL_REPERE, y0, y1, branche, align="L", size=7)

        # Dist : si dispo (sinon on laisse)
        put_in_box(*COL_DIST, y0, y1, dist, align="C", size=7)

        # TSV : TAS si dispo
        put_in_box(*COL_TSV, y0, y1, tsv, align="C", size=7)

        # TAV : GS
        put_in_box(*COL_TAV, y0, y1, gs, align="C", size=7)

        # HE : ETA
        put_in_box(*COL_HE, y0, y1, eta, align="C", size=7)

        # HR : EET (ça fait sens d’avoir le temps branche ici, plutôt que rien)
        put_in_box(*COL_HR, y0, y1, eet, align="C", size=7)

        # Carburant : Conso (branche)
        put_in_box(*COL_CONSO, y0, y1, fuel, align="C", size=7)

        # Reste : si tu as "Reste" dans df_nav, sinon vide
        reste_val = r.get("Reste", "") if "Reste" in df_nav.columns else ""
        put_in_box(*COL_RESTE, y0, y1, reste_val, align="C", size=7)

    # ─────────────────────────────────────────────
    # 3) Totaux en bas (TOTAL / ETA)
    # ─────────────────────────────────────────────
    # Zones bas de page: on place TOTAL EET et ETA finale si dispo
    try:
        # TOTAL EET
        if "EET" in df_nav.columns:
            # addition simple HH:MM
            tot_sec = 0
            for v in df_nav["EET"].tolist():
                s = str(v)
                if ":" in s:
                    hh, mm = s.split(":")[0], s.split(":")[1]
                    tot_sec += int(hh) * 3600 + int(mm) * 60
            tot_h = int(tot_sec // 3600)
            tot_m = int((tot_sec % 3600) // 60)
            total_eet = f"{tot_h:02d}:{tot_m:02d}"
        else:
            total_eet = ""

        eta_final = ""
        if "ETA" in df_nav.columns and len(df_nav) > 0:
            eta_final = str(df_nav.iloc[-1]["ETA"])

        # Cases (px) bas : TOTAL (à gauche du bloc) et ETA (milieu)
        put_in_box(210, 1120, 450, 1165, total_eet, align="C", size=9, bold=True)
        put_in_box(640, 1120, 900, 1165, eta_final, align="C", size=9, bold=True)
    except Exception:
        pass

    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return out.encode("latin-1", "ignore")

# ─────────────────────────── UI ───────────────────────────
with st.sidebar:
    st.title("✈️ SkyAssistant V53.1")

    if st.button("🔄 Rafraîchir météo", use_container_width=True):
        st.session_state.wx_refresh += 1
        st.rerun()

    search = st.text_input("🔍 Rechercher OACI", "").upper()
    sugg = [k for k in AIRPORTS.keys() if k.startswith(search)] if search else []
    if sugg and st.button(f"Départ : {sugg[0]}", use_container_width=True):
        ap = AIRPORTS[sugg[0]]
        elev = get_elevation_ft(ap["lat"], ap["lon"])
        st.session_state.waypoints = [
            {
                "name": sugg[0],
                "lat": ap["lat"],
                "lon": ap["lon"],
                "alt": elev,
                "elev": elev,
                "arr_type": "Direct",
            }
        ]
        st.rerun()

    st.markdown("---")
    tas = st.number_input("TAS (kt)", 50, 250, 100, step=1)
    v_climb = st.number_input("Montée (ft/min)", 100, 2000, 840, step=10)
    v_descent = st.number_input("Descente (ft/min)", 100, 2000, 500, step=10)
    fuel_flow = st.number_input("Conso (L/h)", 1, 200, 25, step=1)

    dep_time = st.time_input("Heure départ (UTC)", value=dt.time(0, 0))

    if st.button("🗑️ Reset", use_container_width=True):
        st.session_state.waypoints = []
        st.rerun()

# ─── METAR ───
metar_val = ""
if st.session_state.waypoints:
    dep_icao = st.session_state.waypoints[0]["name"]
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
        la1, lo1 = math.radians(last["lat"]), math.radians(last["lon"])
        la2 = math.degrees(la1 + (dist_in / R) * math.cos(brng))
        lo2 = math.degrees(lo1 + (dist_in / R) * math.sin(brng) / max(1e-9, math.cos(la1)))

        elev2 = get_elevation_ft(la2, lo2)

        st.session_state.waypoints.append(
            {
                "name": f"WP{len(st.session_state.waypoints)}",
                "lat": la2,
                "lon": lo2,
                "tc": int(rv_in),
                "dist": float(dist_in),
                "alt": int(alt_in),
                "manual_wind": m_wind,
                "elev": elev2,
                "arr_type": "Direct",
            }
        )
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        m = folium.Map(
            location=[st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"]],
            zoom_start=9,
        )
        folium.TileLayer(
            tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
            attr="Google Satellite",
            name="Vue Satellite",
            overlay=False,
            control=True,
        ).add_to(m)
        folium.TileLayer("openstreetmap", name="Carte Standard").add_to(m)

        folium.PolyLine([[w["lat"], w["lon"]] for w in st.session_state.waypoints], color="red", weight=3).add_to(m)

        num_w = len(st.session_state.waypoints)
        for i, w in enumerate(st.session_state.waypoints):
            if i == 0:
                icon_c, icon_t = "blue", "plane"
            elif i == num_w - 1:
                icon_c, icon_t = "red", "flag"
            else:
                icon_c, icon_t = "orange", "circle"  # point tournant plus raccord

            folium.Marker(
                [w["lat"], w["lon"]],
                popup=f"{w['name']}",
                icon=folium.Icon(color=icon_c, icon=icon_t, prefix="fa"),
            ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width="100%", height=300, key="map_v53_1", returned_objects=[])

# ─── LOG DE NAVIGATION & PROFIL ───
if len(st.session_state.waypoints) > 1:
    st.markdown("---")
    now_utc = dt.datetime.now(dt.timezone.utc)
    dep_dt = dt.datetime.combine(now_utc.date(), dep_time, tzinfo=dt.timezone.utc)

    nav_data = []
    dist_p = [0.0]

    elev0 = float(st.session_state.waypoints[0].get("elev", 0))
    if elev0 <= 0:
        elev_try = get_elevation_ft(st.session_state.waypoints[0]["lat"], st.session_state.waypoints[0]["lon"])
        if elev_try > 0:
            elev0 = float(elev_try)
            st.session_state.waypoints[0]["elev"] = elev0

    alt_p = [elev0]
    terr_p = [elev0]
    d_total = 0.0
    fig = go.Figure()
    current_alt = elev0

    wind_local_cache = {}
    decl_local_cache = {}
    cum_sec = 0.0

    for i in range(1, len(st.session_state.waypoints)):
        w1, w2 = st.session_state.waypoints[i - 1], st.session_state.waypoints[i]

        rv = float(w2.get("tc", 0))
        dist_nm = float(w2.get("dist", 0))
        alt_ft = float(w2.get("alt", 0))
        elev2 = float(w2.get("elev", 0))
        manual = w2.get("manual_wind", None)

        if elev2 <= 0:
            elev_try = get_elevation_ft(w2["lat"], w2["lon"])
            if elev_try > 0:
                elev2 = float(elev_try)
                w2["elev"] = elev2

        # wind
        target = min(PRESSURE_MAP.keys(), key=lambda x: abs(x - alt_ft))
        lv = PRESSURE_MAP[target]
        wkey = (round(w2["lat"], 3), round(w2["lon"], 3), lv, st.session_state.wx_refresh)

        if manual:
            wd, ws, src = float(manual["wd"]), float(manual["ws"]), "Manuel"
        else:
            if wkey in wind_local_cache:
                wd, ws, src = wind_local_cache[wkey]
            else:
                wd, ws, src = get_wind_v27_final(
                    w2["lat"], w2["lon"], alt_ft, now_utc, manual_wind=None, wx_refresh=st.session_state.wx_refresh
                )
                wind_local_cache[wkey] = (wd, ws, src)

        wa = math.radians(wd - rv)
        sin_wca = (ws / max(1e-9, float(tas))) * math.sin(wa)
        wca = math.degrees(math.asin(sin_wca)) if abs(sin_wca) <= 1 else 0.0
        cap_vrai = norm360(rv + wca)
        gs = max(20.0, (float(tas) * math.cos(math.radians(wca))) - (ws * math.cos(wa)))

        # declinaison & cap mag
        dkey = (round(w2["lat"], 2), round(w2["lon"], 2), dep_dt.date().isoformat())
        if dkey in decl_local_cache:
            decl = decl_local_cache[dkey]
        else:
            decl = get_declination_deg(float(w2["lat"]), float(w2["lon"]), dep_dt)
            decl_local_cache[dkey] = decl
        cap_mag = norm360(cap_vrai - decl)

        hours = dist_nm / max(1e-9, gs)
        seg_sec = hours * 3600.0
        fuel_branch = round(hours * float(fuel_flow), 1)

        cum_sec += seg_sec
        eta_dt = dep_dt + dt.timedelta(seconds=cum_sec)

        tt_str = ""

        # ── TOC (avec annotation graphique) ──
        if alt_ft > current_alt:
            t_climb = ((alt_ft - current_alt) / max(1e-9, float(v_climb))) * 60.0
            d_climb = gs * (t_climb / 3600.0)
            if d_climb > 0.1:
                t_cl_str = f"{int(t_climb//60):02d}:{int(t_climb%60):02d}"
                tt_str += f"TOC:{round(d_climb,1)}NM "
                if d_climb < dist_nm:
                    x_toc = d_total + d_climb
                    dist_p.append(x_toc)
                    alt_p.append(alt_ft)
                    terr_p.append(float(w1.get("elev", 0)))
                    fig.add_annotation(
                        x=x_toc, y=alt_ft, text=f"TOC {round(d_climb,1)}NM ({t_cl_str})", showarrow=True, ay=45
                    )

        at = w2.get("arr_type", "Direct")
        if (i == len(st.session_state.waypoints) - 1) and at == "Direct":
            at = "VT (1500ft)"

        if at != "Direct":
            alt_t = elev2 + (1500 if "VT" in at else 1000)
            t_desc = ((alt_ft - alt_t) / max(1e-9, float(v_descent))) * 60.0 if alt_ft > alt_t else 0.0
            d_desc = gs * (t_desc / 3600.0)

            # ── TOD (avec annotation graphique) ──
            if d_desc > 0.1:
                t_de_str = f"{int(t_desc//60):02d}:{int(t_desc%60):02d}"
                tt_str += f"TOD:{round(d_desc,1)}NM"
                if d_desc < dist_nm:
                    x_tod = d_total + (dist_nm - d_desc)
                    dist_p.append(x_tod)
                    alt_p.append(alt_ft)
                    terr_p.append(elev2)
                    fig.add_annotation(
                        x=x_tod, y=alt_ft, text=f"TOD {round(d_desc,1)}NM ({t_de_str})", showarrow=True, ay=-45
                    )

            # ── Label VT/TDP + vline ──
            label_dest = "VT" if "VT" in at else "TDP"
            fig.add_annotation(
                x=d_total + dist_nm,
                y=alt_t,
                text=f"<b>{label_dest} {w2['name']}</b>",
                showarrow=False,
                yshift=15,
                font=dict(color="orange", size=11),
            )

            d_total += dist_nm
            dist_p.append(d_total)
            alt_p.append(alt_t)
            terr_p.append(elev2)

            dist_p.append(d_total)
            alt_p.append(elev2)
            terr_p.append(elev2)

            fig.add_vline(x=d_total, line_width=2, line_dash="dash", line_color="orange")
            current_alt = elev2
        else:
            d_total += dist_nm
            dist_p.append(d_total)
            alt_p.append(alt_ft)
            terr_p.append(elev2)
            current_alt = alt_ft

        drift_txt = f"{wca:+.0f}°"
        cap_txt = f"{fmt_deg(cap_mag)} ({drift_txt})"

        nav_data.append(
            {
                "Branche": f"{w1['name']}➔{w2['name']}",
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
            }
        )

    st.subheader("📋 Log de Navigation")
    df_nav = pd.DataFrame(nav_data)

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
            "Arrivée": st.column_config.SelectboxColumn(
                "Arrivée", options=["Direct", "TDP (1000ft)", "VT (1500ft)"], width="small"
            ),
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
                wp["arr_type"] = row["Arrivée"]
                new_wps.append(wp)
        st.session_state.waypoints = new_wps
        st.rerun()

    df_pdf = df_nav[["Branche", "Rv", "Cap", "Vent", "GS", "EET", "Fuel", "TOC/TOD", "Arrivée"]].copy()
    st.download_button(
        label="📥 Log PDF",
        data=create_pdf(df_pdf, metar_val),
        file_name="nav_log.pdf",
        use_container_width=True,
    )

    # ─── GRAPHIQUE (avec éléments terrain + profil + annotations) ───
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
