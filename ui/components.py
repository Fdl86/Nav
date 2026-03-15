import html
from typing import List

import streamlit as st

from models import Aerodrome, LegResult

def metric_card(label: str, value: str):
    st.markdown(
        f"""
        <div style="
            border:1px solid rgba(128,128,128,0.22);
            border-radius:16px;
            padding:10px 12px;
            margin-bottom:8px;
            background:rgba(255,255,255,0.03);
        ">
            <div style="font-size:0.82rem;opacity:0.72;">{label}</div>
            <div style="font-size:1.18rem;font-weight:700;line-height:1.3;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def leg_card(leg: LegResult, selected: bool = False):
    border = "#ef4444" if selected else "rgba(128,128,128,0.22)"
    bg = "rgba(239,68,68,0.05)" if selected else "rgba(255,255,255,0.03)"

    cv_true = leg.heading_true_deg
    cm_mag = leg.heading_mag_deg
    dm_txt = f"{abs(leg.declination_deg):.1f}°{'E' if leg.declination_deg >= 0 else 'W'}"

    st.markdown(
        f"""
        <div style="
            border:1px solid {border};
            border-radius:18px;
            padding:12px 14px;
            margin-bottom:10px;
            background:{bg};
        ">
            <div style="font-size:1rem;font-weight:700;margin-bottom:6px;">
                Branche {leg.idx} — {leg.start_name} → {leg.end_name}
            </div>
            <div style="font-size:0.95rem;line-height:1.75;">
                RV {route3(leg.route_true_deg)} •
                CD {abs(leg.drift_deg):.1f}° ({correction_label(leg.drift_deg)}) •
                Cv {route3(cv_true)} •
                Dm {dm_txt} •
                Cm {route3(cm_mag)}<br>
                Dist {leg.distance_nm:.1f} NM •
                Alt {int(round(leg.altitude_ft))} ft •
                GS {leg.gs_kt:.0f} kt •
                ETE {format_minutes_mmss(leg.ete_min)}<br>
                Vent {route3(leg.wind_dir_deg)}/{leg.wind_speed_kt:.0f} kt ({leg.wind_source}) •
                Fin {leg.end_type.replace("_", " ")}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
