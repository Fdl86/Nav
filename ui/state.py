import streamlit as st

def default_legs():
    return [
        {
            "leg_type": "point_tournant",
            "route_true_deg": 14.0,
            "distance_nm": 18.0,
            "altitude_ft": 3500.0,
            "end_type": "standard",
            "target_icao": "",
            "label": "PT 1",
        }
    ]

def legs_signature(legs_data):
    return tuple(
        (
            l["leg_type"],
            round(float(l["route_true_deg"]), 2),
            round(float(l["distance_nm"]), 2),
            round(float(l["altitude_ft"]), 0),
            l["end_type"],
            (l["target_icao"] or "").strip().upper(),
            (l["label"] or "").strip(),
        )
        for l in legs_data
    )

def ensure_state():
    if "legs_data" not in st.session_state:
        st.session_state.legs_data = default_legs()

    default_map = "OpenAIP" if st.secrets.get("OPENAIP_KEY", "") else "OpenStreetMap"

    if "basemap_choice" not in st.session_state:
        st.session_state.basemap_choice = default_map

    if "basemap_selector" not in st.session_state:
        st.session_state.basemap_selector = st.session_state.basemap_choice

def sync_basemap_choice():
    st.session_state.basemap_choice = st.session_state.basemap_selector
