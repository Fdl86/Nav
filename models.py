from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

class Aerodrome:
    icao: str
    name: str
    lat: float
    lon: float
    elev_ft: float


@dataclass
class LegInput:
    leg_type: str
    route_true_deg: float
    distance_nm: float
    altitude_ft: float
    end_type: str
    target_icao: str = ""
    label: str = ""


@dataclass
class NavPoint:
    name: str
    lat: float
    lon: float
    elev_ft: float = 0.0
    icao: str = ""


@dataclass
class LegResult:
    idx: int
    leg_type: str
    start_name: str
    end_name: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    mid_lat: float
    mid_lon: float
    distance_nm: float
    route_true_deg: float
    declination_deg: float
    route_mag_deg: float
    altitude_ft: float
    tas_kt: float
    wind_source: str
    wind_dir_deg: float
    wind_speed_kt: float
    drift_deg: float
    heading_true_deg: float
    heading_mag_deg: float
    gs_kt: float
    ete_min: float
    end_type: str
    arrival_elev_ft: float = 0.0


@dataclass(frozen=True)
class WeatherBundle:
    metar_raw: Optional[str]
    metar_decoded: Optional[dict]
    taf_raw: Optional[str]
