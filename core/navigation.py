from typing import List, Optional, Tuple, Dict

from models import Aerodrome, LegInput, NavPoint, LegResult
from core.geo import (
    haversine_nm,
    initial_bearing_deg,
    shortest_angle_deg,
)
from core.formatting import route3, format_minutes_mmss, correction_label
from services.weather import prefetch_winds_for_geometries, mean_branch_pressure_wind, mean_branch_surface_wind
from services.magnetic import magnetic_declination_deg, true_to_magnetic

def build_route(
    departure: Aerodrome,
    legs_in: List[LegInput],
    tas_kt: float,
    departure_metar_decoded: Optional[dict] = None,
) -> Tuple[List[LegResult], List[NavPoint]]:
    geometries: List[dict] = []
    nav_points: List[NavPoint] = [NavPoint(departure.icao, departure.lat, departure.lon, departure.elev_ft, departure.icao)]

    prev = nav_points[0]

    for idx, leg in enumerate(legs_in, start=1):
        if leg.leg_type == "aerodrome":
            arr = resolve_airport(leg.target_icao)
            if not arr:
                raise ValueError(f"Aérodrome introuvable: {leg.target_icao}")
            end_pt = NavPoint(arr.icao, arr.lat, arr.lon, arr.elev_ft, arr.icao)
            route_true = initial_bearing_deg(prev.lat, prev.lon, end_pt.lat, end_pt.lon)
            distance_nm = haversine_nm(prev.lat, prev.lon, end_pt.lat, end_pt.lon)
        else:
            route_true = deg_norm(leg.route_true_deg)
            distance_nm = leg.distance_nm
            lat2, lon2 = destination_point(prev.lat, prev.lon, route_true, distance_nm)
            label = leg.label.strip() if leg.label.strip() else f"PT {idx}"
            end_pt = NavPoint(label, lat2, lon2, 0.0, "")

        sample_points = interpolate_line(
            prev.lat,
            prev.lon,
            end_pt.lat,
            end_pt.lon,
            n=sample_point_count(distance_nm),
        )

        geometries.append(
            {
                "idx": idx,
                "leg_type": leg.leg_type,
                "start_name": prev.name,
                "end_name": end_pt.name,
                "start_lat": prev.lat,
                "start_lon": prev.lon,
                "end_lat": end_pt.lat,
                "end_lon": end_pt.lon,
                "distance_nm": distance_nm,
                "route_true_deg": route_true,
                "altitude_ft": leg.altitude_ft,
                "end_type": leg.end_type,
                "arrival_elev_ft": end_pt.elev_ft,
                "mid_lat": (prev.lat + end_pt.lat) / 2.0,
                "mid_lon": (prev.lon + end_pt.lon) / 2.0,
                "sample_points": sample_points,
            }
        )

        nav_points.append(end_pt)
        prev = end_pt

    wind_by_leg = prefetch_winds_for_geometries(geometries, departure_metar_decoded)

    legs_out: List[LegResult] = []
    for geom in geometries:
        wind_source, wind_dir, wind_speed = wind_by_leg.get(geom["idx"], ("Aucune donnée vent", 0.0, 0.0))
        drift, heading_true, gs = wind_correction(geom["route_true_deg"], tas_kt, wind_dir, wind_speed)
        ete_min = geom["distance_nm"] / gs * 60.0

        decl = magnetic_declination_deg(geom["mid_lat"], geom["mid_lon"], geom["altitude_ft"])
        route_mag = true_to_magnetic(geom["route_true_deg"], decl)
        heading_mag = true_to_magnetic(heading_true, decl)

        legs_out.append(
            LegResult(
                idx=geom["idx"],
                leg_type=geom["leg_type"],
                start_name=geom["start_name"],
                end_name=geom["end_name"],
                start_lat=geom["start_lat"],
                start_lon=geom["start_lon"],
                end_lat=geom["end_lat"],
                end_lon=geom["end_lon"],
                mid_lat=geom["mid_lat"],
                mid_lon=geom["mid_lon"],
                distance_nm=geom["distance_nm"],
                route_true_deg=geom["route_true_deg"],
                declination_deg=decl,
                route_mag_deg=route_mag,
                altitude_ft=geom["altitude_ft"],
                tas_kt=tas_kt,
                wind_source=wind_source,
                wind_dir_deg=wind_dir,
                wind_speed_kt=wind_speed,
                drift_deg=drift,
                heading_true_deg=heading_true,
                heading_mag_deg=heading_mag,
                gs_kt=gs,
                ete_min=ete_min,
                end_type=geom["end_type"],
                arrival_elev_ft=geom["arrival_elev_ft"],
            )
        )

    return legs_out, nav_points
