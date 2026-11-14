"""Independent re-check of the clean GeoJSON against the EUDR IS GeoJSON rules (used as a self-test)."""
from __future__ import annotations

import json
import re

import pycountry
from shapely.geometry import shape as to_shape

ALLOWED = {'Point', 'MultiPoint', 'Polygon', 'MultiPolygon'}
PROPS = {'ProducerName', 'ProducerCountry', 'ProductionPlace', 'Area'}


def _decimals(v: float) -> int:
    r = repr(float(v))
    return len(r.split('.')[1]) if '.' in r and 'e' not in r else (10 if 'e' in r else 0)


def validate_fc(fc: dict, cattle: bool = False, max_bytes: int = 25 * 1024 * 1024) -> list[str]:
    issues: list[str] = []
    raw = json.dumps(fc, separators=(',', ':'))
    if len(raw.encode()) > max_bytes:
        issues.append(f'file is {len(raw.encode())} bytes, over the {max_bytes} byte limit')
    if fc.get('type') != 'FeatureCollection':
        issues.append('top level is not a FeatureCollection')
    for i, f in enumerate(fc.get('features') or []):
        tag = f'feature {i + 1}'
        g = f.get('geometry') or {}
        t = g.get('type')
        if t not in ALLOWED:
            issues.append(f'{tag}: geometry type {t} not allowed')
            continue
        props = f.get('properties') or {}
        for k in props:
            if k not in PROPS:
                issues.append(f'{tag}: unexpected property {k}')
        if 'Area' in props and not isinstance(props['Area'], (int, float)):
            issues.append(f'{tag}: Area is not a number')
        cc = props.get('ProducerCountry')
        if cc is not None and (not re.fullmatch(r'[A-Z]{2}', str(cc)) or not pycountry.countries.get(alpha_2=cc)):
            issues.append(f'{tag}: ProducerCountry {cc} is not ISO2')

        def walk(c):
            if c and isinstance(c[0], (int, float)):
                yield c
            else:
                for x in c:
                    yield from walk(x)
        for x, y, *rest in walk(g.get('coordinates') or []):
            if rest:
                issues.append(f'{tag}: has a 3rd coordinate')
            if not (-180 <= x <= 180 and -90 <= y <= 90):
                issues.append(f'{tag}: coordinate out of range')
            if _decimals(x) > 6 or _decimals(y) > 6:
                issues.append(f'{tag}: more than 6 decimals')
                break
        polys = []
        if t == 'Polygon':
            polys = [g['coordinates']]
        elif t == 'MultiPolygon':
            polys = g['coordinates']
        for pc in polys:
            if len(pc) != 1:
                issues.append(f'{tag}: polygon has holes')
            ring = pc[0]
            if ring[0] != ring[-1]:
                issues.append(f'{tag}: ring not closed')
            if len(ring) < 4:
                issues.append(f'{tag}: ring has fewer than 4 positions')
            if len({tuple(p) for p in ring[:-1]}) != len(ring) - 1:
                issues.append(f'{tag}: duplicate vertices')
        if polys:
            shp = to_shape(g)
            if not shp.is_valid:
                issues.append(f'{tag}: invalid polygon')
            if shp.area == 0:
                issues.append(f'{tag}: zero area')
        if t in ('Point', 'MultiPoint') and not cattle and isinstance(props.get('Area'), (int, float)) and props['Area'] > 4:
            issues.append(f'{tag}: point with Area > 4 ha')
    return issues
