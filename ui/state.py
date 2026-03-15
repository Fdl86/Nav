import streamlit as st

from ui.panels import default_legs


def ensure_state() -> None:
    if "legs_data" not in st.session_state:
        st.session_state.legs_data = default_legs()

    default_map = "OpenAIP" if st.secrets.get("OPENAIP_KEY", "") else "OpenStreetMap"

    if "basemap_choice" not in st.session_state:
        st.session_state.basemap_choice = default_map

    if "basemap_selector" not in st.session_state:
        st.session_state.basemap_selector = st.session_state.basemap_choice


def sync_basemap_choice() -> None:
    st.session_state.basemap_choice = st.session_state.basemap_selector
