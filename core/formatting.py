def ft_to_m(ft: float) -> float:
    return ft * 0.3048


def m_to_ft(m: float) -> float:
    return m / 0.3048


def nm_to_m(nm: float) -> float:
    return nm * 1852.0


def m_to_nm(m: float) -> float:
    return m / 1852.0


def deg_norm(x: float) -> float:
    return x % 360.0


def shortest_angle_deg(a: float, b: float) -> float:
    return (a - b + 180) % 360 - 180


def route3(v: float) -> str:
    return f"{int(round(v)) % 360:03d}"


def format_minutes_mmss(minutes_value: float) -> str:
    total_seconds = max(0, int(round(minutes_value * 60)))
    mm = total_seconds // 60
    ss = total_seconds % 60
    return f"{mm:02d}:{ss:02d}"


def correction_label(wca_deg: float) -> str:
    if abs(wca_deg) < 0.05:
        return "nulle"
    return "droite" if wca_deg > 0 else "gauche"
