import logging
from typing import List

import requests
import streamlit as st

from config import UA

LOGGER = logging.getLogger(__name__)

def fetch_elevations(lats: Tuple[float, ...], lons: Tuple[float, ...]):
    try:
        if not lats or not lons or len(lats) != len(lons):
            return None
        all_vals = []
        chunk_size = 80  # évite de surcharger l'API sur les routes longues
        for i in range(0, len(lats), chunk_size):
            sub_lats = lats[i:i + chunk_size]
            sub_lons = lons[i:i + chunk_size]
            js = fetch_json(
                OPENMETEO_ELEV,
                params={
                    "latitude": ",".join(f"{x:.6f}" for x in sub_lats),
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
