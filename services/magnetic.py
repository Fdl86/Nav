import logging
from datetime import datetime, timezone

import streamlit as st

from core.formatting import ft_to_m

LOGGER = logging.getLogger(__name__)

try:
    from pygeomag import GeoMag
    try:
        _GEOMAG = GeoMag(coefficients_file="wmm/WMM_2025.COF")
    except Exception:
        _GEOMAG = GeoMag()
    GEOMAG_AVAILABLE = True
except Exception:
    _GEOMAG = None
    GEOMAG_AVAILABLE = False


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def magnetic_declination_deg(lat: float, lon: float, alt_ft: float = 0.0) -> float:
    if not GEOMAG_AVAILABLE or _GEOMAG is None:
        return 0.0
    try:
        lat_key = round(float(lat), 3)
        lon_key = round(float(lon), 3)
        alt_key = round(float(alt_ft), -2)
        now_utc = datetime.now(timezone.utc)
        year_fraction = now_utc.year + (now_utc.timetuple().tm_yday / 365.25)
        result = _GEOMAG.calculate(
            glat=lat_key,
            glon=lon_key,
            alt=ft_to_m(alt_key) / 1000.0,
            time=year_fraction,
        )
        return float(result.d)
    except Exception:
        LOGGER.exception("Erreur calcul déclinaison magnétique")
        return 0.0
