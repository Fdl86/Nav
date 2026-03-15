import html

import streamlit as st
from streamlit_folium import st_folium

from config import APP_TITLE
from models import LegInput
from services.airports import resolve_airport
from services.weather import fetch_airport_weather_bundle
from services.magnetic import GEOMAG_AVAILABLE
from core.navigation import build_route
from core.profile import build_vertical_profile
from ui.map import build_map
from ui.components import metric_card, leg_card
from ui.state import ensure_state, sync_basemap_choice


st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🛩️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.block-container {padding-top: 0.8rem; padding-bottom: 2rem; max-width: 1100px;}
[data-testid="stHorizontalBlock"] {gap: 0.6rem;}
div[data-testid="stExpander"] details summary p {font-size: 1rem;}
</style>
""", unsafe_allow_html=True)

ensure_state()

st.title("🛩️ Prépa VFR mobile")
st.caption("Départ OACI, METAR/TAF, branches simples, carte openAIP, cap magnétique, profil vertical.")

openaip_key = st.secrets.get("OPENAIP_KEY", "")

with st.expander("Vol", expanded=True):
    c1, c2, c3 = st.columns(3)

    with c1:
        dep_icao = st.text_input("Départ OACI", value="LFBI").strip().upper()

    with c2:
        tas_kt = st.number_input("TAS (kt)", min_value=40, max_value=220, value=100, step=1)
        fuel_burn_lph = st.number_input("Conso (L/h)", min_value=1, max_value=200, value=20, step=1)
        reserve_min = st.number_input("Réserve (min)", min_value=0, max_value=180, value=45, step=5)

    with c3:
        climb_rate_fpm = st.number_input("Taux montée (ft/min)", min_value=100, max_value=3000, value=840, step=10)
        climb_speed_kt = st.number_input("Vitesse montée (kt)", min_value=40, max_value=200, value=65, step=1)
        descent_rate_fpm = st.number_input("Taux descente (ft/min)", min_value=100, max_value=3000, value=500, step=50)

departure = resolve_airport(dep_icao)
if not departure:
    st.error("Aérodrome de départ introuvable.")
    st.stop()

weather_bundle = fetch_airport_weather_bundle(dep_icao)
metar_raw = weather_bundle.metar_raw
metar_decoded = weather_bundle.metar_decoded
taf_raw = weather_bundle.taf_raw

if not GEOMAG_AVAILABLE:
    st.warning("`pygeomag` n'est pas installé : le cap magnétique sera temporairement égal au cap vrai.")

with st.expander("Terrain de départ", expanded=True):
    c1, c2 = st.columns([1, 3])

    with c1:
        metric_card("OACI", departure.icao)

    with c2:
        metric_card("Nom", departure.name)

    st.markdown("### Météo")

    weather_block = f"""
    <div style="
        background-color: rgba(255,255,255,0.03);
        padding:14px;
        border-radius:10px;
        border:1px solid rgba(255,255,255,0.08);
        font-family: monospace;
        white-space: pre-wrap;
        line-height:1.4;
    ">
    <b>METAR</b>
    {html.escape(metar_raw) if metar_raw else "METAR indisponible."}
    
    <br><b>TAF</b>
    {html.escape(taf_raw) if taf_raw else "TAF indisponible."}
        </div>
        """

    st.markdown(weather_block, unsafe_allow_html=True)

with st.expander("Branches", expanded=True):
    st.caption("Ordre chronologique conservé. Ajout en bas pour garder un flux naturel départ → arrivée.")

    delete_idx = None

    for i, leg in enumerate(st.session_state.legs_data):
        st.markdown(f"### Branche {i + 1}")
        t1, t2 = st.columns([1, 1])

        with t1:
            leg["leg_type"] = st.selectbox(
                "Type",
                LEG_TYPES,
                index=LEG_TYPES.index(leg["leg_type"]),
                key=f"leg_type_{i}",
            )

            if leg["leg_type"] == "point_tournant":
                leg["route_true_deg"] = st.number_input(
                    "Route vraie (°)",
                    min_value=0,
                    max_value=359,
                    value=int(round(leg["route_true_deg"])) % 360,
                    step=1,
                    key=f"route_{i}",
                )
                leg["distance_nm"] = st.number_input(
                    "Distance (NM)",
                    min_value=0.1,
                    max_value=500.0,
                    value=float(leg["distance_nm"]),
                    step=1.0,
                    key=f"dist_{i}",
                )
                leg["label"] = st.text_input(
                    "Label",
                    value=leg["label"],
                    key=f"label_{i}",
                )
                st.caption(f"RV affichée : {route3(leg['route_true_deg'])}")
            else:
                leg["target_icao"] = st.text_input(
                    "OACI arrivée",
                    value=leg["target_icao"],
                    key=f"icao_{i}",
                ).strip().upper()

        with t2:
            leg["altitude_ft"] = st.number_input(
                "Altitude branche (ft)",
                min_value=500,
                max_value=18000,
                value=int(leg["altitude_ft"]),
                step=100,
                key=f"alt_{i}",
            )
            leg["end_type"] = st.selectbox(
                "Fin de branche",
                END_TYPES,
                index=END_TYPES.index(leg["end_type"]),
                key=f"end_{i}",
            )

            if st.button(f"🗑️ Supprimer branche {i + 1}", key=f"del_{i}", width="stretch"):
                delete_idx = i

        st.divider()

    if delete_idx is not None:
        st.session_state.legs_data.pop(delete_idx)
        if not st.session_state.legs_data:
            st.session_state.legs_data = default_legs()
        st.rerun()

    if st.button("➕ Ajouter une branche", width="stretch"):
        st.session_state.legs_data.append(
            {
                "leg_type": "point_tournant",
                "route_true_deg": 0.0,
                "distance_nm": 10.0,
                "altitude_ft": 3500.0,
                "end_type": "standard",
                "target_icao": "",
                "label": f"PT {len(st.session_state.legs_data) + 1}",
            }
        )
        st.rerun()

legs_in = []
for raw in st.session_state.legs_data:
    legs_in.append(
        LegInput(
            leg_type=raw["leg_type"],
            route_true_deg=float(raw["route_true_deg"]),
            distance_nm=float(raw["distance_nm"]),
            altitude_ft=float(raw["altitude_ft"]),
            end_type=raw["end_type"],
            target_icao=raw["target_icao"],
            label=raw["label"],
        )
    )

metar_sig = None
if metar_decoded:
    metar_sig = (
        metar_decoded.get("wind_dir"),
        metar_decoded.get("wind_speed_kt"),
        metar_decoded.get("obs_time"),
    )

route_key = (
    dep_icao,
    round(float(tas_kt), 1),
    metar_sig,
    legs_signature(st.session_state.legs_data),
)

if st.session_state.get("route_key") == route_key:
    legs = st.session_state["route_legs"]
    nav_points = st.session_state["route_nav_points"]
else:
    try:
        legs, nav_points = build_route(
            departure,
            legs_in,
            tas_kt,
            departure_metar_decoded=metar_decoded,
        )
    except ValueError as e:
        st.error(str(e))
        st.stop()

    st.session_state["route_key"] = route_key
    st.session_state["route_legs"] = legs
    st.session_state["route_nav_points"] = nav_points

selected_leg_idx = st.selectbox(
    "Branche sélectionnée",
    options=[leg.idx for leg in legs],
    format_func=lambda i: f"Branche {i}: {legs[i - 1].start_name} → {legs[i - 1].end_name}",
)

tabs = st.tabs(["Carte", "Navigation", "Profil vertical", "Météo"])

with tabs[0]:
    basemap_options = ["OpenAIP", "OpenStreetMap", "OpenTopoMap"]

    if st.session_state.basemap_choice not in basemap_options:
        st.session_state.basemap_choice = "OpenStreetMap"
    if st.session_state.basemap_selector not in basemap_options:
        st.session_state.basemap_selector = st.session_state.basemap_choice

    st.selectbox(
        "Fond de carte",
        basemap_options,
        index=basemap_options.index(st.session_state.basemap_choice),
        key="basemap_selector",
        on_change=sync_basemap_choice,
        width="stretch",
    )

    basemap = st.session_state.basemap_choice
    map_key = (st.session_state["route_key"], selected_leg_idx, basemap)
    if st.session_state.get("map_key") == map_key:
        fmap = st.session_state["map_cache"]
    else:
        fmap = build_map(nav_points, legs, selected_leg_idx, openaip_key, basemap)
        st.session_state["map_key"]   = map_key
        st.session_state["map_cache"] = fmap
    st_folium(fmap, width="stretch", height=560, key="main_map", returned_objects=[],)

    sel = legs[selected_leg_idx - 1]
    c1, c2 = st.columns(2)
    with c1:
        dm_txt = f"{abs(sel.declination_deg):.1f}°{'E' if sel.declination_deg >= 0 else 'W'}"
        metric_card("Branche", f"{sel.start_name} → {sel.end_name}")
        metric_card("RV", route3(sel.route_true_deg))
        metric_card("CD", f"{abs(sel.drift_deg):.1f}° ({correction_label(sel.drift_deg)})")
        metric_card("Cv", route3(sel.heading_true_deg))
    with c2:
        metric_card("Dm", dm_txt)
        metric_card("Cm", route3(sel.heading_mag_deg))
        metric_card("Vent", f"{route3(sel.wind_dir_deg)}/{sel.wind_speed_kt:.0f} kt ({sel.wind_source})")
        metric_card("Altitude", f"{int(round(sel.altitude_ft))} ft")

with tabs[1]:
    total_nm = sum(l.distance_nm for l in legs)
    total_min = sum(l.ete_min for l in legs)
    trip_fuel_l = total_min / 60.0 * fuel_burn_lph
    total_fuel_l = trip_fuel_l + reserve_min / 60.0 * fuel_burn_lph

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Distance totale", f"{total_nm:.1f} NM")
    with c2:
        metric_card("Temps total", f"{total_min:.1f} min")
    with c3:
        metric_card("Trip fuel", f"{trip_fuel_l:.1f} L")
    with c4:
        metric_card("Fuel + réserve", f"{total_fuel_l:.1f} L")

    st.markdown("### Log de navigation")
    for leg in legs:
        leg_card(leg, selected=(leg.idx == selected_leg_idx))

with tabs[2]:
    verticale_ft = 1500
    tdp_ft = 1000

    profile_key = (
        st.session_state["route_key"],
        climb_rate_fpm, climb_speed_kt, descent_rate_fpm,
        verticale_ft, tdp_ft,
    )
    if st.session_state.get("profile_key") == profile_key:
        profile = st.session_state["profile_cache"]
        elev_m  = st.session_state["profile_elev"]
    else:
        profile = build_vertical_profile(
            nav_points=nav_points,
            legs=legs,
            climb_rate_fpm=climb_rate_fpm,
            climb_speed_kt=climb_speed_kt,
            descent_rate_fpm=descent_rate_fpm,
            verticale_ft=verticale_ft,
            tdp_ft=tdp_ft,
        )
        elev_m = fetch_elevations(
            tuple(p[0] for p in profile["terrain_route_pts"]),
            tuple(p[1] for p in profile["terrain_route_pts"]),
        )
        st.session_state["profile_key"]   = profile_key
        st.session_state["profile_cache"] = profile
        st.session_state["profile_elev"]  = elev_m
        
    if elev_m is None:
        terrain_ft = [0] * len(profile["terrain_route_pts"])
        st.warning("Relief indisponible en ligne, profil affiché sans terrain.")
    else:
        terrain_ft = [int(round(m_to_ft(x))) for x in elev_m]

    x_terrain = [round(x, 1) for x in profile["terrain_x_nm"]]
    x_air = profile["aircraft_x_nm"]
    y_air = profile["aircraft_alt_ft"]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=x_terrain,
        y=terrain_ft,
        mode="lines",
        name="Sol",
        fill="tozeroy",
        line=dict(color="#8B5A2B", width=2),
        fillcolor="rgba(139, 90, 43, 0.45)",
        hovertemplate="Dist %{x:.1f} NM<br>Sol %{y:.0f} ft<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=x_air,
        y=y_air,
        mode="lines",
        name="Avion",
        line=dict(width=3),
        connectgaps=False,
        hovertemplate="Dist %{x:.1f} NM<br>Avion %{y:.0f} ft<extra></extra>",
    ))

    for x, t_txt in profile["toc_marks"]:
        fig.add_annotation(
            x=round(x, 1),
            y=max(y for y in y_air if y is not None),
            text=f"TOC {t_txt}",
            showarrow=False,
            yshift=10,
            font=dict(color="green"),
        )

    for x, t_txt in profile["tod_marks"]:
        fig.add_annotation(
            x=round(x, 1),
            y=max(y for y in y_air if y is not None),
            text=f"TOD {t_txt}",
            showarrow=False,
            yshift=10,
            font=dict(color="purple"),
        )
    # VT / TDP : marqueurs à la distance exacte du terrain, bornés entre sol et altitude d'intégration
    for x, y0, y1, terrain_name in profile["vt_marks"]:
        fig.add_shape(
            type="line",
            x0=round(x, 1),
            x1=round(x, 1),
            y0=round(y0),
            y1=round(y1),
            line=dict(color="orange", width=2, dash="dot"),
        )
        fig.add_annotation(
            x=round(x, 1),
            y=round(y1),
            text=f"VT<br>{terrain_name} {round(y0):.0f} ft",
            showarrow=False,
            yshift=10,
            font=dict(color="orange"),
            align="center",
        )

    for x, y0, y1, terrain_name in profile["tdp_marks"]:
        fig.add_shape(
            type="line",
            x0=round(x, 1),
            x1=round(x, 1),
            y0=round(y0),
            y1=round(y1),
            line=dict(color="deepskyblue", width=2, dash="dot"),
        )
        fig.add_annotation(
            x=round(x, 1),
            y=round(y1),
            text=f"TDP<br>{terrain_name} {round(y0):.0f} ft",
            showarrow=False,
            yshift=10,
            font=dict(color="deepskyblue"),
            align="center",
        )

    fig.update_layout(
        height=430,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Distance cumulée (NM)",
        yaxis_title="Altitude (ft)",
        legend=dict(orientation="h"),
    )

    st.plotly_chart(
        fig,
        width="stretch",
        config={
            "displayModeBar": False,
            "scrollZoom": False,
            "doubleClick": False,
            "staticPlot": True,
        },
    )

    if x_terrain:
        c1, c2, c3 = st.columns(3)
        with c1:
            metric_card("Distance totale", f"{x_terrain[-1]:.1f} NM")
        with c2:
            metric_card("Alt min avion", f"{min(y for y in y_air if y is not None):.0f} ft")
        with c3:
            metric_card("Alt max avion", f"{max(y for y in y_air if y is not None):.0f} ft")

    if terrain_ft and len(x_terrain) == len(terrain_ft):
        air_pairs = [(x, y) for x, y in zip(x_air, y_air) if x is not None and y is not None]
        terrain_map = {round(x, 1): t for x, t in zip(x_terrain, terrain_ft)}

        margins = []
        for x, y in air_pairs:
            key = round(x, 1)
            if key in terrain_map:
                margins.append(y - terrain_map[key])

        if margins:
            min_margin = min(margins)
            if min_margin < 500:
                st.error(f"Marge verticale minimale faible : {min_margin:.0f} ft")
            else:
                st.success(f"Marge verticale minimale : {min_margin:.0f} ft")

with tabs[3]:
    st.subheader(f"Départ {departure.icao}")
    st.markdown(f"**{departure.name}**")

    st.markdown("**METAR**")
    if metar_raw:
        st.code(metar_raw, language="text")
    else:
        st.warning("METAR indisponible.")

    st.markdown("**TAF**")
    if taf_raw:
        st.code(taf_raw, language="text")
    else:
        st.warning("TAF indisponible.")

    st.markdown("### Vent par branche")
    hour_txt = generation_hour_utc().strftime("%Y-%m-%d %H:%M UTC")
    for leg in legs:
        st.markdown(
            f"**Vent branche {leg.idx}** : {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f} kt "
            f"({leg.wind_source}) — {hour_txt}"
        )
