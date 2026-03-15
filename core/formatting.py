from typing import Optional

def format_minutes_mmss(minutes_value: float) -> str:
    total_seconds = max(0, int(round(minutes_value * 60)))
    mm = total_seconds // 60
    ss = total_seconds % 60
    return f"{mm:02d}:{ss:02d}"

def correction_label(wca_deg: float) -> str:
    if abs(wca_deg) < 0.05:
        return "nulle"
    return "droite" if wca_deg > 0 else "gauche"

def wind_to_deg(wind_from_deg: float) -> float:
    return deg_norm(wind_from_deg + 180.0)
