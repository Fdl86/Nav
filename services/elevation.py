import logging
from typing import Optional, Tuple, List

import requests
import streamlit as st

from services.http import fetch_json

OPENMETEO_ELEV = "https://api.open-meteo.com/v1/elevation"
LOGGER = logging.getLogger(__name__)


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def fetch_elevations(
    lats: Tuple[float, ...], lons: Tuple[float, ...]
) -> Optional[List[float]]:
    try:
        if not lats or not lons or len(lats) != len(lons):
            return None
        all_vals: List[float] = []
        chunk_size = 80
        for i in range(0, len(lats), chunk_size):
            sub_lats = lats[i:i + chunk_size]
            sub_lons = lons[i:i + chunk_size]
            js = fetch_json(
                OPENMETEO_ELEV,
                params={
                    "latitude":  ",".join(f"{x:.6f}" for x in sub_lats),
                    "longitude": ",".join(f"{x:.6f}" for x in sub_lons),
                },
                timeout=25,
            )
            vals = js.get("elevation", [])
            if not vals:
                return None
            all_vals.extend(vals)
        return all_vals if all_vals else None
    except requests.RequestException as exc:
        LOGGER.warning("Erreur réseau élévations: %s", exc)
        return None
    except Exception:
        LOGGER.exception("Erreur inattendue élévations")
        return None
