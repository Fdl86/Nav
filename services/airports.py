import logging
from typing import Optional, Dict, Tuple

import pandas as pd
import streamlit as st

from models import Aerodrome
from services.http import fetch_json

AIRPORTS_CSV_URL          = "https://ourairports.com/data/airports.csv"
AIRPORTS_FALLBACK_CSV_URL = "https://raw.githubusercontent.com/datasets/airport-codes/main/data/airport-codes.csv"

LOGGER = logging.getLogger(__name__)


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_airports_primary() -> pd.DataFrame:
    df = pd.read_csv(AIRPORTS_CSV_URL, low_memory=False)
    keep = ["ident", "name", "latitude_deg", "longitude_deg", "elevation_ft", "type", "iso_country"]
    df = df[keep].copy()
    df["ident"]        = df["ident"].astype(str).str.upper()
    df["name"]         = df["name"].fillna("").astype(str)
    df["latitude_deg"] = pd.to_numeric(df["latitude_deg"], errors="coerce")
    df["longitude_deg"]= pd.to_numeric(df["longitude_deg"], errors="coerce")
    df["elevation_ft"] = pd.to_numeric(df["elevation_ft"], errors="coerce").fillna(0)
    return df.dropna(subset=["latitude_deg", "longitude_deg"])


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_airports_fallback() -> pd.DataFrame:
    df = pd.read_csv(AIRPORTS_FALLBACK_CSV_URL, low_memory=False)
    needed = ["ident", "name", "latitude_deg", "longitude_deg", "elevation_ft", "type", "iso_country"]
    for col in needed:
        if col not in df.columns:
            df[col] = None
    df = df[needed].copy()
    df["ident"]        = df["ident"].astype(str).str.upper()
    df["name"]         = df["name"].fillna("").astype(str)
    df["latitude_deg"] = pd.to_numeric(df["latitude_deg"], errors="coerce")
    df["longitude_deg"]= pd.to_numeric(df["longitude_deg"], errors="coerce")
    df["elevation_ft"] = pd.to_numeric(df["elevation_ft"], errors="coerce").fillna(0)
    return df.dropna(subset=["latitude_deg", "longitude_deg"])


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_airports_index() -> Dict[str, Tuple[str, float, float, float]]:
    index: Dict[str, Tuple[str, float, float, float]] = {}
    try:
        for row in load_airports_primary().itertuples(index=False):
            index[str(row.ident)] = (str(row.name), float(row.latitude_deg), float(row.longitude_deg), float(row.elevation_ft))
    except Exception:
        pass
    try:
        for row in load_airports_fallback().itertuples(index=False):
            ident = str(row.ident)
            if ident not in index:
                index[ident] = (str(row.name), float(row.latitude_deg), float(row.longitude_deg), float(row.elevation_ft))
    except Exception:
        pass
    return index


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def resolve_airport(icao: str) -> Optional[Aerodrome]:
    icao = (icao or "").strip().upper()
    if not icao:
        return None
    rec = load_airports_index().get(icao)
    if not rec:
        return None
    name, lat, lon, elev_ft = rec
    return Aerodrome(icao=icao, name=name, lat=lat, lon=lon, elev_ft=elev_ft)
