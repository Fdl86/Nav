import os
import streamlit as st
import streamlit.components.v1 as components
import requests
import pandas as pd
import datetime as dt
import math
import folium
import plotly.graph_objects as go
from fpdf import FPDF
import re
import numpy as np
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
import hashlib, json

# ─── CONFIGURATION ───
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL  = "https://api.open-meteo.com/v1/elevation"
NOAA_DECL_URL  = "https://www.ngdc.noaa.gov/geomag-web/calculators/calculateDeclination"
PRESSURE_MAP   = {1000:975,1500:960,2000:950,2500:925,3000:900,5000:850,7000:750}
HTTP_TIMEOUT   = 8
ARRIVAL_METAR_RADIUS_NM = 15.0
OPENAIP_API_KEY = os.getenv("OPENAIP_API_KEY","")

# ─── PAGE ───
st.set_page_config(page_title="SkyAssistant V58.7", layout="wide")

st.markdown("""
<style>
div[data-testid="stDataFrame"] [data-testid="stElementToolbar"],
div[data-testid="stDataEditor"] [data-testid="stElementToolbar"] { display:none!important; }
.block-container { padding-top:1.1rem; padding-bottom:1.5rem; }
.sa-card {
    border:1px solid rgba(255,255,255,0.08); border-radius:14px;
    padding:14px 16px; background:rgba(255,255,255,0.02); margin-bottom:0.75rem;
}
.sa-card h4 { margin:0 0 0.35rem 0; font-size:0.95rem; }
.sa-card p  { margin:0; opacity:0.95; line-height:1.4; white-space:pre-wrap; word-break:break-word; }
.sa-section {
    margin-top:0.2rem; margin-bottom:0.5rem;
    padding-bottom:0.2rem; border-bottom:1px solid rgba(255,255,255,0.08);
}
</style>
""", unsafe_allow_html=True)

# ─── HTTP SESSION ───
@st.cache_resource
def get_http_session():
    s = requests.Session()
    s.headers.update({"User-Agent":"SkyAssistant/58.7"})
    return s
SESSION = get_http_session()

# ─── STATE ───
for k,v in [("waypoints",[]),("wx_refresh",0),("map_html",""),("map_html_key","")]:
    if k not in st.session_state:
        st.session_state[k] = v

# ─── AIRPORTS ───
@st.cache_data(ttl=86400)
def load_airports():
    base = {"LFBI":{"name":"Poitiers Biard","lat":46.5877,"lon":0.3069}}
    try:
        df = pd.read_csv("https://ourairports.com/data/airports.csv",
            usecols=["ident","name","latitude_deg","longitude_deg","iso_country","type"])
        fr = df[(df["iso_country"]=="FR") &
                (df["type"].isin(["large_airport","medium_airport","small_airport"]))]
        fr = fr[fr["ident"].astype(str).str.match(r"^LF[A-Z0-9]{2}$")]
        base.update({r.ident:{"name":r.name,"lat":float(r.latitude_deg),"lon":float(r.longitude_deg)}
                     for r in fr.itertuples(index=False)})
    except: pass
    return base
AIRPORTS = load_airports()

# ─── HELPERS ───
ICAO_LF_RE = re.compile(r"^LF[A-Z0-9]{2}$")
def is_lf_icao(s): return bool(ICAO_LF_RE.match(str(s).upper().strip()))
def norm360(x):    return (x%360.0+360.0)%360.0
def fmt_hdg3(x):   return f"{int(round(norm360(x))):03d}"

def _pdf_safe(s):
    if s is None: return ""
    s=str(s).replace("➔","->").replace("→","->").replace("—","-").replace("–","-")
    return s.encode("latin-1","ignore").decode("latin-1")

@st.cache_data(ttl=86400)
def airports_df_fr_lf():
    return pd.DataFrame([{"icao":k,"name":v.get("name",""),"lat":v.get("lat"),"lon":v.get("lon")}
                          for k,v in AIRPORTS.items() if is_lf_icao(k)])

def nearest_airfields(lat,lon,radius_nm=15.0,k=5,exclude_icao=None):
    df=airports_df_fr_lf().copy()
    if exclude_icao and is_lf_icao(exclude_icao): df=df[df["icao"]!=exclude_icao]
    la1,lo1=np.radians(lat),np.radians(lon)
    la2=np.radians(df["lat"].to_numpy(dtype=float)); lo2=np.radians(df["lon"].to_numpy(dtype=float))
    a=np.sin((la2-la1)/2)**2+np.cos(la1)*np.cos(la2)*np.sin((lo2-lo1)/2)**2
    df["d_nm"]=6371.0*2*np.arctan2(np.sqrt(a),np.sqrt(1-a))/1.852
    return df[df["d_nm"]<=radius_nm].sort_values("d_nm").head(k)[["icao","name","d_nm"]].to_dict("records")

def format_hhmm(sec):
    sec=int(round(sec)); return f"{sec//3600:02d}:{(sec%3600)//60:02d}"

def summarize_route(wps,n=5):
    names=[w["name"] for w in wps]
    return " → ".join(names) if len(names)<=n else " → ".join(names[:2]+["…"]+names[-2:])

def get_arrival_metar_candidate(wps,dep_icao):
    if not wps: return None
    nb=nearest_airfields(wps[-1]["lat"],wps[-1]["lon"],radius_nm=ARRIVAL_METAR_RADIUS_NM,k=1)
    if not nb or nb[0]["icao"]==dep_icao: return None
    return {"icao":nb[0]["icao"],"name":nb[0]["name"],"label":"METAR arrivée"}

# ─── ELEVATION ───
@st.cache_data(ttl=86400)
def _elev_cached(lat,lon):
    r=SESSION.get(ELEVATION_URL,params={"latitude":lat,"longitude":lon},timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return round(r.json().get("elevation",[0])[0]*3.28084)

_elev_cache={}
def get_elevation_ft(lat,lon):
    try:
        k=(round(lat,3),round(lon,3))
        if k not in _elev_cache: _elev_cache[k]=_elev_cached(lat,lon)
        return _elev_cache[k]
    except: return 0

# ─── METAR / TAF ───
@st.cache_data(ttl=600)
def get_metar_cached(icao,ref):
    try:
        r=SESSION.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",timeout=HTTP_TIMEOUT)
        if r.status_code==200:
            lines=r.text.splitlines(); return lines[1] if len(lines)>1 else "METAR indisponible"
        return "METAR indisponible"
    except: return "Erreur METAR"

@st.cache_data(ttl=600)
def get_taf_cached(icao,ref):
    try:
        r=SESSION.get(f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{icao}.TXT",timeout=HTTP_TIMEOUT)
        if r.status_code==200:
            lines=[l.strip() for l in r.text.splitlines() if l.strip()]
            return "\n".join(lines[1:]) if len(lines)>1 else "TAF indisponible"
        return f"TAF indisponible (HTTP {r.status_code})"
    except Exception as e: return f"Erreur TAF: {e}"

# ─── DECLINAISON ───
@st.cache_data(ttl=86400*30)
def get_declination_deg(lat,lon,date_utc):
    try:
        p={"lat1":lat,"lon1":lon,"model":"WMM",
           "startYear":date_utc.year,"startMonth":date_utc.month,"startDay":date_utc.day,"resultFormat":"json"}
        r=SESSION.get(NOAA_DECL_URL,params=p,timeout=HTTP_TIMEOUT)
        dec=r.json().get("result",[{}])[0].get("declination",None)
        return float(dec) if dec is not None else 0.0
    except: return 0.0

# ─── WIND ───
@st.cache_data(ttl=900)
def get_wind_openmeteo_cached(lat,lon,lv,ref):
    p={"latitude":round(lat,2),"longitude":round(lon,2),
       "hourly":f"wind_speed_{lv}hPa,wind_direction_{lv}hPa",
       "models":"icon_d2,meteofrance_arome_france_hd,gfs_seamless",
       "wind_speed_unit":"kn","timezone":"UTC"}
    return SESSION.get(OPEN_METEO_URL,params=p,timeout=HTTP_TIMEOUT).json()

def get_wind(lat,lon,alt_ft,time_dt,manual=None,ref=0):
    if manual: return float(manual["wd"]),float(manual["ws"]),"Manuel"
    lv=PRESSURE_MAP[min(PRESSURE_MAP,key=lambda x:abs(x-alt_ft))]
    try:
        h=get_wind_openmeteo_cached(lat,lon,lv,ref).get("hourly",{})
        times=h.get("time",[])
        if not times: return 0.0,0.0,"Err"
        def pick(pfx):
            ws=h.get(f"wind_speed_{lv}hPa_{pfx}"); wd=h.get(f"wind_direction_{lv}hPa_{pfx}")
            return (wd,ws) if ws and wd and ws[0] is not None else None
        p=pick("icon_d2")
        if p: wd_a,ws_a,src=p[0],p[1],"ICON-D2"
        else:
            p=pick("meteofrance_arome_france_hd")
            if p: wd_a,ws_a,src=p[0],p[1],"AROME"
            else: wd_a=h.get(f"wind_direction_{lv}hPa_gfs_seamless",[]); ws_a=h.get(f"wind_speed_{lv}hPa_gfs_seamless",[]); src="GFS"
        if not wd_a or not ws_a: return 0.0,0.0,"Err"
        tt=[dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp() for t in times]
        pos=bisect_left(tt,time_dt.timestamp())
        if pos<=0: i=0
        elif pos>=len(tt): i=len(tt)-1
        else: i=pos-1 if abs(tt[pos-1]-time_dt.timestamp())<=abs(tt[pos]-time_dt.timestamp()) else pos
        return float(wd_a[i]),float(ws_a[i]),src
    except: return 0.0,0.0,"Err"

# ─── PDF ───
def create_pdf(df_nav,metar_text):
    pdf=FPDF(orientation="P",unit="mm",format="A4"); pdf.add_page()
    pdf.set_font("helvetica","B",14)
    pdf.cell(0,10,"LOG DE NAVIGATION - SKYASSISTANT",new_x="LMARGIN",new_y="NEXT",align="C"); pdf.ln(5)
    pdf.set_font("helvetica","B",10); pdf.cell(0,8,"METAR DE DEPART :",new_x="LMARGIN",new_y="NEXT")
    pdf.set_font("helvetica",size=9); pdf.multi_cell(0,6,_pdf_safe(metar_text),border=1); pdf.ln(5)
    ws=[30,35,15,20,15,45,30]; cols=["Branche","Vent","GS","EET","Fuel","TOC/TOD","Arrivée"]
    pdf.set_font("helvetica","B",8); pdf.set_fill_color(220,220,220)
    for c,w in zip(cols,ws): pdf.cell(w,8,c,border=1,fill=True,align="C")
    pdf.ln(); pdf.set_font("helvetica",size=8)
    for _,row in df_nav.iterrows():
        pdf.cell(ws[0],8,_pdf_safe(row.get("Branche","")).replace("➔","->"),border=1)
        for j,k in enumerate(["Vent","GS","EET","Fuel","TOC/TOD","Arrivée"],1):
            pdf.cell(ws[j],8,_pdf_safe(row.get(k,"")),border=1,align="C" if k in("GS","EET","Fuel") else "L")
        pdf.ln()
    out=pdf.output(dest="S")
    return bytes(out) if isinstance(out,(bytes,bytearray)) else out.encode("latin-1","ignore")

# ─── CARTE : génération HTML avec clé de cache basée sur les waypoints ───
def waypoints_hash(wps):
    """Hash stable des waypoints pour détecter un vrai changement de tracé."""
    data=[(round(w["lat"],4),round(w["lon"],4),w.get("name","")) for w in wps]
    return hashlib.md5(json.dumps(data).encode()).hexdigest()

def build_map_html(waypoints):
    center=[waypoints[0]["lat"],waypoints[0]["lon"]]
    m=folium.Map(location=center,zoom_start=9,control_scale=True,tiles=None)

    folium.TileLayer("openstreetmap",name="🗺️ Standard",
                     overlay=False,control=True,show=True).add_to(m)
    folium.TileLayer(
        tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        attr="Google Satellite",name="🛰️ Satellite",
        overlay=False,control=True,show=False).add_to(m)
    if OPENAIP_API_KEY:
        folium.TileLayer(
            tiles=f"https://api.tiles.openaip.net/api/data/openaip/{{z}}/{{x}}/{{y}}.png?apiKey={OPENAIP_API_KEY}",
            attr="openAIP",name="✈️ Aviation (openAIP)",
            overlay=False,control=True,show=False).add_to(m)

    folium.PolyLine([[w["lat"],w["lon"]] for w in waypoints],color="red",weight=3).add_to(m)
    n=len(waypoints)
    for i,w in enumerate(waypoints):
        ic,it=("blue","plane") if i==0 else (("red","flag") if i==n-1 else ("orange","circle"))
        folium.Marker([w["lat"],w["lon"]],popup=w["name"],
                      icon=folium.Icon(color=ic,icon=it,prefix="fa")).add_to(m)

    folium.LayerControl(position="topright",collapsed=False).add_to(m)

    # ── Supprime les IDs aléatoires Folium pour rendre le HTML déterministe ──
    html = m._repr_html_()
    return html

# ══════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════
with st.sidebar:
    st.title("✈️ SkyAssistant V58.7")
    if st.button("🔄 Rafraîchir météo",use_container_width=True):
        st.session_state.wx_refresh+=1; st.rerun()

    search=st.text_input("🔍 Rechercher OACI","").upper()
    sugg=[k for k in AIRPORTS if k.startswith(search)] if search else []
    if sugg:
        ic0=sugg[0]; ap0=AIRPORTS[ic0]
        if st.button(f"Départ : {ap0['name']} ({ic0})",use_container_width=True):
            elev=get_elevation_ft(ap0["lat"],ap0["lon"])
            st.session_state.waypoints=[{"name":ic0,"lat":ap0["lat"],"lon":ap0["lon"],
                                          "alt":elev,"elev":elev,"arr_type":"Direct"}]
            st.session_state.map_html=""   # force régénération carte
            st.rerun()

    if st.session_state.waypoints:
        d0=st.session_state.waypoints[0]["name"]
        st.success(f"Départ : {AIRPORTS.get(d0,{}).get('name',d0)} ({d0})")

    with st.expander("🧾 Briefing",expanded=False):
        st.link_button("📌 SOFIA (NOTAM)","https://sofia-briefing.aviation-civile.gouv.fr/sofia/pages/homepage.html")
        st.link_button("📚 SIA / AIP","https://www.sia.aviation-civile.gouv.fr/vaip")

    st.markdown("---")
    tas       =st.number_input("TAS (kt)",50,250,100,step=1)
    v_climb   =st.number_input("Montée (ft/min)",100,2000,840,step=10)
    v_descent =st.number_input("Descente (ft/min)",100,2000,500,step=10)
    fuel_flow =st.number_input("Conso (L/h)",1,200,20,step=1)
    dep_time  =st.time_input("Heure départ (UTC)",value=dt.time(0,0))
    if st.button("🗑️ Reset",use_container_width=True):
        st.session_state.waypoints=[]; st.session_state.map_html=""; st.rerun()

mission_ph=st.container()
weather_ph=st.container()

# ══════════════════════════════════════════
#  MÉTÉO
# ══════════════════════════════════════════
metar_val=taf_val=""
if st.session_state.waypoints:
    d0=st.session_state.waypoints[0]["name"]
    dn=AIRPORTS.get(d0,{}).get("name",d0)
    metar_val=get_metar_cached(d0,st.session_state.wx_refresh)
    taf_val  =get_taf_cached(d0, st.session_state.wx_refresh)
    arr_cand =get_arrival_metar_candidate(st.session_state.waypoints,d0)

    with weather_ph.container():
        st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">🌦️ Météo</h3></div>',unsafe_allow_html=True)
        c1,c2=st.columns(2)
        with c1:
            st.markdown(f'<div class="sa-card"><h4>Départ — {dn} ({d0})</h4><p>{metar_val}</p></div>',unsafe_allow_html=True)
        with c2:
            if arr_cand:
                am=get_metar_cached(arr_cand["icao"],st.session_state.wx_refresh)
                st.markdown(f'<div class="sa-card"><h4>{arr_cand["label"]} — {arr_cand["name"]} ({arr_cand["icao"]})</h4><p>{am}</p></div>',unsafe_allow_html=True)
            else:
                st.markdown('<div class="sa-card"><h4>Arrivée</h4><p>Aucun METAR distinct.</p></div>',unsafe_allow_html=True)
        with st.expander(f"📄 TAF départ — {d0}",expanded=False):
            st.code(taf_val,language="text")

# ══════════════════════════════════════════
#  NAVIGATION & CARTE
# ══════════════════════════════════════════
st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">🗺️ Navigation & Carte</h3></div>',unsafe_allow_html=True)

col_map,col_ctrl=st.columns([2,1])

with col_ctrl:
    st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">📍 Ajouter Segment</h3></div>',unsafe_allow_html=True)
    rv_in  =st.number_input("Route Vraie (Rv) °",0,359,0,step=1)
    st.caption(f"Route affichée : {fmt_hdg3(rv_in)}°")
    dist_in=st.number_input("Distance (NM)",0.1,300.0,15.0,step=0.1)
    alt_in =st.number_input("Alt Croisière (ft)",1000,12500,2500,step=500)
    use_auto=st.toggle("Vent Auto",True)
    m_wind=None
    if not use_auto:
        m_wind={"wd":st.number_input("Dir",0,359,0,step=1),
                "ws":st.number_input("Force",0,100,0,step=1)}

    if st.button("➕ Ajouter") and st.session_state.waypoints:
        last=st.session_state.waypoints[-1]; R=3440.065
        brng=math.radians(rv_in)
        la1,lo1=math.radians(last["lat"]),math.radians(last["lon"])
        la2=math.degrees(la1+(dist_in/R)*math.cos(brng))
        lo2=math.degrees(lo1+(dist_in/R)*math.sin(brng)/max(1e-9,math.cos(la1)))
        elev2=get_elevation_ft(la2,lo2)
        st.session_state.waypoints.append({
            "name":f"WP{len(st.session_state.waypoints)}",
            "lat":la2,"lon":lo2,"tc":int(rv_in),"dist":float(dist_in),
            "alt":int(alt_in),"manual_wind":m_wind,"elev":elev2,"arr_type":"Direct"})
        st.session_state.map_html=""   # invalide le cache carte → tracé mis à jour
        st.rerun()

with col_map:
    if st.session_state.waypoints:
        # ── Cache HTML carte : régénéré SEULEMENT si les waypoints ont changé ──
        current_hash = waypoints_hash(st.session_state.waypoints)
        if st.session_state.map_html_key != current_hash:
            st.session_state.map_html     = build_map_html(st.session_state.waypoints)
            st.session_state.map_html_key = current_hash

        # srcdoc + sandbox : l'iframe est stable entre reruns car le HTML ne change pas
        components.html(st.session_state.map_html, height=420, scrolling=False)

# ══════════════════════════════════════════
#  LOG DE NAVIGATION + PROFIL VERTICAL
# ══════════════════════════════════════════
if len(st.session_state.waypoints)>1:
    st.markdown("---")
    now_utc=dt.datetime.now(dt.timezone.utc)
    dep_dt=dt.datetime.combine(now_utc.date(),dep_time,tzinfo=dt.timezone.utc)

    nav_data=[]; dist_p=[0.0]
    elev0=float(st.session_state.waypoints[0].get("elev",0))
    if elev0<=0:
        ev=get_elevation_ft(st.session_state.waypoints[0]["lat"],st.session_state.waypoints[0]["lon"])
        if ev>0: elev0=float(ev); st.session_state.waypoints[0]["elev"]=elev0

    alt_p=[elev0]; terr_p=[elev0]; d_total=0.0
    fig=go.Figure(); current_alt=elev0
    wnd_cache={}; dec_cache={}; cum_sec=0.0; fuel_total=0.0

    for i in range(1,len(st.session_state.waypoints)):
        w1,w2=st.session_state.waypoints[i-1],st.session_state.waypoints[i]
        rv=float(w2.get("tc",0)); dist_nm=float(w2.get("dist",0))
        alt_ft=float(w2.get("alt",0)); elev2=float(w2.get("elev",0))
        manual=w2.get("manual_wind",None)
        lv=PRESSURE_MAP[min(PRESSURE_MAP,key=lambda x:abs(x-alt_ft))]
        wk=(round(w2["lat"],2),round(w2["lon"],2),lv,st.session_state.wx_refresh)
        dk=(round(w2["lat"],2),round(w2["lon"],2),dep_dt.date().isoformat())

        fe=fw=fd=None
        with ThreadPoolExecutor(max_workers=3) as ex:
            if elev2<=0: fe=ex.submit(get_elevation_ft,w2["lat"],w2["lon"])
            if manual:   wd,ws,src=float(manual["wd"]),float(manual["ws"]),"Manuel"
            elif wk in wnd_cache: wd,ws,src=wnd_cache[wk]
            else: fw=ex.submit(get_wind,w2["lat"],w2["lon"],alt_ft,now_utc,None,st.session_state.wx_refresh)
            if dk in dec_cache: decl=dec_cache[dk]
            else: fd=ex.submit(get_declination_deg,float(w2["lat"]),float(w2["lon"]),dep_dt)

            if fe is not None:
                ev=fe.result()
                if ev>0: elev2=float(ev); w2["elev"]=elev2
            if fw is not None: wd,ws,src=fw.result(); wnd_cache[wk]=(wd,ws,src)
            if fd is not None: decl=fd.result(); dec_cache[dk]=decl

        wa=math.radians(wd-rv)
        sin_wca=(ws/max(1e-9,float(tas)))*math.sin(wa)
        wca=math.degrees(math.asin(sin_wca)) if abs(sin_wca)<=1 else 0.0
        gs=max(20.0,(float(tas)*math.cos(math.radians(wca)))-(ws*math.cos(wa)))
        cap_mag=norm360(norm360(rv+wca)-decl)

        hours=dist_nm/max(1e-9,gs); seg_sec=hours*3600.0
        fb=round(hours*float(fuel_flow),1); fuel_total+=fb
        cum_sec+=seg_sec; eta_dt=dep_dt+dt.timedelta(seconds=cum_sec); tt_str=""

        if alt_ft>current_alt:
            tc=((alt_ft-current_alt)/max(1e-9,float(v_climb)))*60.0
            dc=gs*(tc/3600.0)
            if dc>0.1:
                tt_str+=f"TOC:{round(dc,1)}NM "
                if dc<dist_nm:
                    xt=d_total+dc; dist_p.append(xt); alt_p.append(alt_ft); terr_p.append(float(w1.get("elev",0)))
                    fig.add_annotation(x=xt,y=alt_ft,text=f"TOC {round(dc,1)}NM ({int(tc//60):02d}:{int(tc%60):02d})",showarrow=True,ay=45)

        at=w2.get("arr_type","Direct")
        if (i==len(st.session_state.waypoints)-1) and at=="Direct": at="VT (1500ft)"

        if at!="Direct":
            alt_t=elev2+(1500 if "VT" in at else 1000)
            td=((alt_ft-alt_t)/max(1e-9,float(v_descent)))*60.0 if alt_ft>alt_t else 0.0
            dd=gs*(td/3600.0)
            if dd>0.1:
                tt_str+=f"TOD:{round(dd,1)}NM"
                if dd<dist_nm:
                    xd=d_total+(dist_nm-dd); dist_p.append(xd); alt_p.append(alt_ft); terr_p.append(elev2)
                    fig.add_annotation(x=xd,y=alt_ft,text=f"TOD {round(dd,1)}NM ({int(td//60):02d}:{int(td%60):02d})",showarrow=True,ay=-45)
            lbl="VT" if "VT" in at else "TDP"
            fig.add_annotation(x=d_total+dist_nm,y=alt_t,text=f"<b>{lbl} {w2['name']}</b>",
                                showarrow=False,yshift=15,font=dict(color="orange",size=11))
            d_total+=dist_nm
            dist_p+=[d_total,d_total]; alt_p+=[alt_t,elev2]; terr_p+=[elev2,elev2]
            fig.add_vline(x=d_total,line_width=2,line_dash="dash",line_color="orange")
            current_alt=elev2
        else:
            d_total+=dist_nm; dist_p.append(d_total); alt_p.append(alt_ft); terr_p.append(elev2)
            current_alt=alt_ft

        nav_data.append({"Branche":f"{w1['name']}➔{w2['name']}",
            "Vent":f"{int(wd)}/{int(ws)}kt ({src})","GS":f"{int(gs)}kt",
            "EET":f"{int(seg_sec//60):02d}:{int(seg_sec%60):02d}","Fuel":f"{fb:.1f}L",
            "TOC/TOD":tt_str.strip(),"Arrivée":at,"❌":False,"_idx":i,
            "ETA":eta_dt.strftime("%H:%M"),"Cap":f"{fmt_hdg3(cap_mag)} ({wca:+.0f}°)"})

    df_nav=pd.DataFrame(nav_data)

    with mission_ph.container():
        st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">🧭 Mission</h3></div>',unsafe_allow_html=True)
        with st.container(border=True):
            st.caption(summarize_route(st.session_state.waypoints))
            m1,m2,m3,m4=st.columns(4)
            m1.metric("Distance totale",f"{d_total:.1f} NM")
            m2.metric("Temps total",format_hhmm(cum_sec))
            m3.metric("Carburant total",f"{fuel_total:.1f} L")
            m4.metric("ETA arrivée",df_nav.iloc[-1]["ETA"] if len(df_nav) else "--:--")

    st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.2rem;">📋 Log de Navigation</h3></div>',unsafe_allow_html=True)
    df_scr=df_nav[["Branche","Cap","Vent","GS","EET","Fuel","ETA","TOC/TOD","Arrivée","❌","_idx"]].copy()
    edited=st.data_editor(df_scr,column_config={
        "Branche":st.column_config.TextColumn("Branche",width="small"),
        "Cap":st.column_config.TextColumn("Cap (mag)",width="small",disabled=True),
        "Vent":st.column_config.TextColumn("Vent",width="small",disabled=True),
        "GS":st.column_config.TextColumn("GS",width="small",disabled=True),
        "EET":st.column_config.TextColumn("EET",width="small",disabled=True),
        "Fuel":st.column_config.TextColumn("Fuel",width="small",disabled=True),
        "ETA":st.column_config.TextColumn("ETA",width="small",disabled=True),
        "TOC/TOD":st.column_config.TextColumn("TOC/TOD",width="medium",disabled=True),
        "Arrivée":st.column_config.SelectboxColumn("Arrivée",options=["Direct","TDP (1000ft)","VT (1500ft)"],width="small"),
        "❌":st.column_config.CheckboxColumn("❌",width="small"),
        "_idx":None},hide_index=True)

    if edited.to_dict("records")!=df_scr.to_dict("records"):
        new_wps=[st.session_state.waypoints[0]]
        for _,row in edited.iterrows():
            if not row["❌"]:
                wp=st.session_state.waypoints[int(row["_idx"])].copy()
                wp["arr_type"]=row["Arrivée"]
                bt=str(row["Branche"])
                wp["name"]=bt.split("➔",1)[1].strip() if "➔" in bt else (bt.split("->",1)[1].strip() if "->" in bt else wp["name"])
                new_wps.append(wp)
        st.session_state.waypoints=new_wps
        st.session_state.map_html=""   # force recalcul carte après suppression segment
        st.rerun()

    df_pdf=df_nav[["Branche","Vent","GS","EET","Fuel","TOC/TOD","Arrivée"]].copy()
    st.download_button("📥 Log PDF",data=create_pdf(df_pdf,metar_val),file_name="nav_log.pdf",use_container_width=True)

    fig.add_trace(go.Scatter(x=dist_p,y=terr_p,fill="tozeroy",name="Relief",line_color="sienna"))
    fig.add_trace(go.Scatter(x=dist_p,y=alt_p,name="Profil Avion",line=dict(color="royalblue",width=4)))
    fig.update_layout(width=1000,height=350,
        xaxis=dict(fixedrange=True,tickformat=".1f",title="Distance (NM)"),
        yaxis=dict(fixedrange=True,title="Altitude (ft)"),
        margin=dict(l=40,r=40,t=20,b=40),showlegend=False)
    st.markdown('<div class="sa-section"><h3 style="margin-bottom:0.4rem;">📈 Profil vertical</h3></div>',unsafe_allow_html=True)
    st.plotly_chart(fig,use_container_width=False,config={"staticPlot":True,"displayModeBar":False})
