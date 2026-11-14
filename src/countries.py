"""Country helpers: ISO2 normalisation and rough bounding boxes for lat/lon-swap detection.

The bounding boxes are deliberately generous approximations (lon_min, lat_min, lon_max, lat_max).
They are only used as a heuristic to decide whether swapping lon/lat puts a plot inside the
declared ProducerCountry. They are never used to judge compliance.
"""
from __future__ import annotations

import re

import pycountry

BBOX: dict[str, tuple[float, float, float, float]] = {
    'AR': (-73.6, -55.1, -53.6, -21.8), 'AU': (112.9, -43.7, 153.7, -10.6), 'BI': (29.0, -4.5, 30.9, -2.3),
    'BJ': (0.7, 6.1, 3.9, 12.5), 'BO': (-69.7, -22.9, -57.4, -9.6), 'BR': (-74.1, -33.8, -34.7, 5.3),
    'CA': (-141.1, 41.6, -52.6, 83.2), 'CD': (12.1, -13.5, 31.4, 5.4), 'CF': (14.4, 2.2, 27.5, 11.0),
    'CG': (11.1, -5.1, 18.7, 3.8), 'CI': (-8.7, 4.3, -2.4, 10.8), 'CL': (-75.8, -56.0, -66.9, -17.4),
    'CM': (8.4, 1.6, 16.2, 13.1), 'CN': (73.4, 18.1, 134.8, 53.6), 'CO': (-81.8, -4.3, -66.8, 13.5),
    'CR': (-86.0, 8.0, -82.5, 11.3), 'DE': (5.8, 47.2, 15.1, 55.1), 'EC': (-92.1, -5.1, -75.1, 1.7),
    'ES': (-18.2, 27.6, 4.4, 43.8), 'ET': (32.9, 3.3, 48.0, 15.0), 'FI': (20.5, 59.7, 31.6, 70.1),
    'FR': (-5.2, 41.3, 9.6, 51.1), 'GA': (8.6, -4.0, 14.6, 2.4), 'GH': (-3.3, 4.7, 1.2, 11.2),
    'GN': (-15.1, 7.1, -7.6, 12.7), 'GT': (-92.3, 13.7, -88.2, 17.9), 'GY': (-61.4, 1.1, -56.5, 8.6),
    'HN': (-89.4, 12.9, -83.1, 17.5), 'ID': (94.9, -11.1, 141.1, 6.1), 'IN': (68.1, 6.7, 97.4, 35.7),
    'KE': (33.9, -4.7, 41.9, 5.1), 'KH': (102.3, 10.4, 107.7, 14.7), 'LA': (100.0, 13.9, 107.7, 22.5),
    'LK': (79.6, 5.9, 81.9, 9.9), 'LR': (-11.5, 4.3, -7.4, 8.6), 'MG': (43.2, -25.7, 50.5, -11.9),
    'MM': (92.1, 9.7, 101.2, 28.6), 'MX': (-118.5, 14.5, -86.7, 32.8), 'MY': (99.6, 0.8, 119.3, 7.4),
    'NG': (2.6, 4.2, 14.7, 13.9), 'NI': (-87.7, 10.7, -82.7, 15.1), 'NZ': (166.4, -47.3, 178.6, -34.4),
    'PE': (-81.4, -18.4, -68.6, 0.0), 'PG': (140.8, -11.7, 156.0, -1.3), 'PH': (116.9, 4.6, 126.6, 21.1),
    'PL': (14.1, 49.0, 24.2, 54.9), 'PY': (-62.7, -27.6, -54.2, -19.3), 'RW': (28.8, -2.9, 30.9, -1.0),
    'SE': (11.1, 55.3, 24.2, 69.1), 'SL': (-13.3, 6.9, -10.3, 10.0), 'SR': (-58.1, 1.8, -53.9, 6.0),
    'SV': (-90.2, 13.1, -87.7, 14.5), 'TG': (-0.2, 6.1, 1.8, 11.2), 'TH': (97.3, 5.6, 105.7, 20.5),
    'TZ': (29.3, -11.8, 40.5, -0.9), 'UG': (29.5, -1.5, 35.1, 4.3), 'US': (-125.0, 24.5, -66.9, 49.4),
    'UY': (-58.5, -35.0, -53.1, -30.1), 'VE': (-73.4, 0.6, -59.8, 12.2), 'VN': (102.1, 8.4, 109.5, 23.4),
}

_ALIASES = {
    "IVORY COAST": 'CI', "COTE D'IVOIRE": 'CI', "CÔTE D'IVOIRE": 'CI', 'DRC': 'CD', 'DR CONGO': 'CD',
    'CONGO DRC': 'CD', 'LAOS': 'LA', 'VIETNAM': 'VN', 'BOLIVIA': 'BO', 'TANZANIA': 'TZ', 'UK': 'GB',
    'USA': 'US', 'BRASIL': 'BR', 'INDONESIE': 'ID', 'COSTA DE MARFIL': 'CI', 'PEROU': 'PE',
}


def to_iso2(value) -> tuple[str | None, str | None]:
    """Return (iso2, note). note is set when the value was converted; iso2 None when unknown."""
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    up = raw.upper()
    if re.fullmatch(r'[A-Z]{2}', up) and pycountry.countries.get(alpha_2=up):
        return up, (None if raw == up else f"upper-cased ProducerCountry '{raw}' to '{up}'")
    if up in _ALIASES:
        return _ALIASES[up], f"converted country '{raw}' to ISO code '{_ALIASES[up]}'"
    if re.fullmatch(r'[A-Z]{3}', up):
        c = pycountry.countries.get(alpha_3=up)
        if c:
            return c.alpha_2, f"converted country '{raw}' to ISO code '{c.alpha_2}'"
    try:
        c = pycountry.countries.lookup(raw)
        return c.alpha_2, f"converted country '{raw}' to ISO code '{c.alpha_2}'"
    except LookupError:
        pass
    try:
        hits = pycountry.countries.search_fuzzy(raw)
        if len(hits) == 1:
            return hits[0].alpha_2, f"converted country '{raw}' to ISO code '{hits[0].alpha_2}' (fuzzy match)"
    except LookupError:
        pass
    return None, None


def in_bbox(iso2: str | None, lon: float, lat: float, pad: float = 0.5) -> bool | None:
    """True/False if we know the country's box, None if we don't."""
    b = BBOX.get(iso2 or '')
    if not b:
        return None
    return b[0] - pad <= lon <= b[2] + pad and b[1] - pad <= lat <= b[3] + pad
