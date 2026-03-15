from typing import List, Tuple, Dict, Optional

import plotly.graph_objects as go

from models import Aerodrome, LegInput, LegResult
from core.geo import interpolate_line
from services.elevation import fetch_elevations

def determine_leg_end_target_alt(leg: LegResult, verticale_ft: float, tdp_ft: float) -> Tuple[bool, float, float]:
    is_arrival_aerodrome = leg.leg_type == "aerodrome" and leg.arrival_elev_ft > 0
    terrain_alt = leg.arrival_elev_ft if is_arrival_aerodrome else 0.0
    cruise_alt = leg.altitude_ft

    if is_arrival_aerodrome and leg.end_type == "verticale":
        end_target_alt = terrain_alt + verticale_ft
    elif is_arrival_aerodrome and leg.end_type == "tour_de_piste":
        end_target_alt = terrain_alt + tdp_ft
    else:
        end_target_alt = cruise_alt

    return is_arrival_aerodrome, terrain_alt, end_target_alt


def compute_leg_vertical_segments(
    current_alt: float,
    cruise_alt: float,
    end_target_alt: float,
    leg_distance_nm: float,
    leg_gs_kt: float,
    climb_rate_fpm: float,
    climb_speed_kt: float,
    descent_rate_fpm: float,
) -> Dict[str, float]:
    delta_climb_ft = cruise_alt - current_alt
    if delta_climb_ft > 1:
        climb_dist_nm = climb_speed_kt * ((delta_climb_ft / max(climb_rate_fpm, 1)) / 60.0)
        climb_time_min = delta_climb_ft / max(climb_rate_fpm, 1)
    else:
        climb_dist_nm = 0.0
        climb_time_min = 0.0

    delta_descent_ft = cruise_alt - end_target_alt
    if delta_descent_ft > 1:
        descent_dist_nm = leg_gs_kt * ((delta_descent_ft / max(descent_rate_fpm, 1)) / 60.0)
        descent_time_min = delta_descent_ft / max(descent_rate_fpm, 1)
    else:
        descent_dist_nm = 0.0
        descent_time_min = 0.0

    total_special_nm = climb_dist_nm + descent_dist_nm
    if total_special_nm > leg_distance_nm and total_special_nm > 1e-6:
        scale = leg_distance_nm / total_special_nm
        climb_dist_nm *= scale
        descent_dist_nm *= scale
        climb_time_min *= scale
        descent_time_min *= scale

    toc_nm_local = climb_dist_nm if climb_dist_nm > 0 else None
    tod_nm_local = leg_distance_nm - descent_dist_nm if descent_dist_nm > 0 else None

    return {
        "climb_dist_nm": climb_dist_nm,
        "climb_time_min": climb_time_min,
        "descent_dist_nm": descent_dist_nm,
        "descent_time_min": descent_time_min,
        "toc_nm_local": toc_nm_local,
        "tod_nm_local": tod_nm_local,
    }


def altitude_at_leg_distance(
    x_local: float,
    current_alt: float,
    cruise_alt: float,
    end_target_alt: float,
    climb_dist_nm: float,
    descent_dist_nm: float,
    tod_nm_local: Optional[float],
) -> float:
    if climb_dist_nm > 1e-6 and x_local <= climb_dist_nm:
        frac = x_local / climb_dist_nm
        return current_alt + (cruise_alt - current_alt) * frac

    if tod_nm_local is not None and x_local >= tod_nm_local and descent_dist_nm > 1e-6:
        frac = (x_local - tod_nm_local) / descent_dist_nm
        return cruise_alt + (end_target_alt - cruise_alt) * frac

    return cruise_alt


def build_vertical_profile(
    nav_points: List[NavPoint],
    legs: List[LegResult],
    climb_rate_fpm: float,
    climb_speed_kt: float,
    descent_rate_fpm: float,
    verticale_ft: float,
    tdp_ft: float,
):
    terrain_x: List[float] = []
    terrain_route_pts: List[Tuple[float, float]] = []

    aircraft_x: List[Optional[float]] = []
    aircraft_y: List[Optional[float]] = []

    vt_marks: List[Tuple[float, float, float, str]] = []
    tdp_marks: List[Tuple[float, float, float, str]] = []

    toc_marks: List[Tuple[float, str]] = []
    tod_marks: List[Tuple[float, str]] = []

    cumulative_nm = 0.0
    elapsed_min_total = 0.0
    current_alt = nav_points[0].elev_ft

    for i, leg in enumerate(legs):
        next_leg_exists = i < len(legs) - 1

        n = max(12, min(40, int(round(leg.distance_nm * 1.5))))
        seg_pts = interpolate_line(leg.start_lat, leg.start_lon, leg.end_lat, leg.end_lon, n=n)
        seg_x_local = [round((j / n) * leg.distance_nm, 1) for j in range(n + 1)]

        is_arrival_aerodrome, terrain_alt, end_target_alt = determine_leg_end_target_alt(
            leg,
            verticale_ft,
            tdp_ft,
        )
        vertical_segments = compute_leg_vertical_segments(
            current_alt=current_alt,
            cruise_alt=leg.altitude_ft,
            end_target_alt=end_target_alt,
            leg_distance_nm=leg.distance_nm,
            leg_gs_kt=leg.gs_kt,
            climb_rate_fpm=climb_rate_fpm,
            climb_speed_kt=climb_speed_kt,
            descent_rate_fpm=descent_rate_fpm,
        )

        toc_nm_local = vertical_segments["toc_nm_local"]
        tod_nm_local = vertical_segments["tod_nm_local"]

        leg_start_elapsed_min = elapsed_min_total
        if toc_nm_local is not None:
            toc_x = round(cumulative_nm + toc_nm_local, 1)
            toc_t = format_minutes_mmss(leg_start_elapsed_min + vertical_segments["climb_time_min"])
            toc_marks.append((toc_x, toc_t))

        if tod_nm_local is not None:
            cruise_nm_before_descent = max(tod_nm_local - max(vertical_segments["climb_dist_nm"], 0.0), 0.0)
            cruise_time_min = cruise_nm_before_descent / max(leg.gs_kt, 1e-6) * 60.0
            tod_x = round(cumulative_nm + tod_nm_local, 1)
            tod_t = format_minutes_mmss(leg_start_elapsed_min + vertical_segments["climb_time_min"] + cruise_time_min)
            tod_marks.append((tod_x, tod_t))

        leg_end_x = round(cumulative_nm + leg.distance_nm, 1)

        if is_arrival_aerodrome:
            terrain_label = leg.end_name
            if leg.end_type == "verticale":
                vt_marks.append((leg_end_x, terrain_alt, terrain_alt + verticale_ft, terrain_label))
            elif leg.end_type == "tour_de_piste":
                tdp_marks.append((leg_end_x, terrain_alt, terrain_alt + tdp_ft, terrain_label))

        for j, (pt, x_local) in enumerate(zip(seg_pts, seg_x_local)):
            x_global = round(cumulative_nm + x_local, 1)

            if i == 0 and j == 0:
                terrain_x.append(x_global)
                terrain_route_pts.append(pt)
            elif j > 0:
                terrain_x.append(x_global)
                terrain_route_pts.append(pt)

            alt = altitude_at_leg_distance(
                x_local=x_local,
                current_alt=current_alt,
                cruise_alt=leg.altitude_ft,
                end_target_alt=end_target_alt,
                climb_dist_nm=vertical_segments["climb_dist_nm"],
                descent_dist_nm=vertical_segments["descent_dist_nm"],
                tod_nm_local=tod_nm_local,
            )

            if i == 0 and j == 0:
                aircraft_x.append(x_global)
                aircraft_y.append(round(alt))
            elif j > 0:
                aircraft_x.append(x_global)
                aircraft_y.append(round(alt))

        if is_arrival_aerodrome and leg.end_type in ("verticale", "tour_de_piste") and next_leg_exists:
            aircraft_x.append(None)
            aircraft_y.append(None)
            current_alt = terrain_alt
            aircraft_x.append(leg_end_x)
            aircraft_y.append(round(terrain_alt))
        else:
            current_alt = end_target_alt

        elapsed_min_total += leg.ete_min
        cumulative_nm = round(cumulative_nm + leg.distance_nm, 1)

    return {
        "terrain_x_nm": terrain_x,
        "terrain_route_pts": terrain_route_pts,
        "aircraft_x_nm": aircraft_x,
        "aircraft_alt_ft": aircraft_y,
        "vt_marks": vt_marks,
        "tdp_marks": tdp_marks,
        "toc_marks": toc_marks,
        "tod_marks": tod_marks,
    }
