"""Read supplier plot files (CSV, KML/KMZ, GeoJSON) into raw plot records.

A raw plot is a plain dict:
    plotId, sourceFormat, sourceRow, geometry (GeoJSON-like dict, raw coordinates),
    props (normalised EUDR property names), fixes, warnings, errors, minDecimals
Nothing is repaired here except property names and number formats; geometry repair lives in geometry.py.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

EUDR_PROPS = ('ProducerName', 'ProducerCountry', 'ProductionPlace', 'Area')

_KEYMAP = {
    'producername': 'ProducerName', 'producer': 'ProducerName', 'farmer': 'ProducerName', 'farmername': 'ProducerName',
    'grower': 'ProducerName', 'owner': 'ProducerName', 'supplier': 'ProducerName', 'suppliername': 'ProducerName',
    'producercountry': 'ProducerCountry', 'country': 'ProducerCountry', 'countrycode': 'ProducerCountry', 'iso2': 'ProducerCountry',
    'countryofproduction': 'ProducerCountry', 'pais': 'ProducerCountry', 'pays': 'ProducerCountry',
    'productionplace': 'ProductionPlace', 'place': 'ProductionPlace', 'farm': 'ProductionPlace', 'farmname': 'ProductionPlace',
    'plotname': 'ProductionPlace', 'village': 'ProductionPlace', 'location': 'ProductionPlace', 'site': 'ProductionPlace', 'name': 'ProductionPlace',
    'area': 'Area', 'areaha': 'Area', 'hectares': 'Area', 'hectare': 'Area', 'ha': 'Area', 'sizeha': 'Area', 'surface': 'Area', 'superficie': 'Area',
    'aream2': 'Area:m2', 'areasqm': 'Area:m2', 'sqm': 'Area:m2', 'm2': 'Area:m2',
    'acres': 'Area:acre', 'areaac': 'Area:acre', 'areaacres': 'Area:acre',
    'plotid': 'plotId', 'id': 'plotId', 'plot': 'plotId', 'parcelid': 'plotId', 'farmid': 'plotId', 'fid': 'plotId', 'plotcode': 'plotId',
    'commodity': 'commodity',
}
_LAT = {'lat', 'latitude', 'y', 'latdd', 'gpslat', 'latitud', 'gpslatitude', 'centroidlat'}
_LON = {'lon', 'lng', 'long', 'longitude', 'x', 'londd', 'gpslon', 'gpslng', 'longitud', 'gpslongitude', 'centroidlon'}
_WKT = {'wkt', 'geometry', 'geom', 'thegeom', 'polygon', 'shape', 'geometrywkt', 'geojson'}


def _k(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(s).lower())


class DFloat(float):
    """float that remembers how many decimals it was written with."""
    dec: int = 0


def _dfloat(text: str) -> DFloat:
    t = text.strip()
    v = DFloat(float(t))
    m = re.search(r'\.(\d+)', t.lower().split('e')[0])
    v.dec = len(m.group(1)) if m else 0
    return v


def decimals_of(v: Any) -> int:
    if isinstance(v, DFloat):
        return v.dec
    if isinstance(v, int):
        return 0
    r = repr(float(v))
    if 'e' in r:
        return 10
    return len(r.split('.')[1].rstrip('0')) if '.' in r else 0


def parse_number(text: Any) -> float | None:
    """'2,5' -> 2.5, '1.234,5' -> 1234.5, '1,234.5' -> 1234.5, ' 3 ' -> 3."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip().replace('\u00a0', '').replace(' ', '')
    if not t:
        return None
    if ',' in t and '.' in t:
        t = t.replace('.', '').replace(',', '.') if t.rfind(',') > t.rfind('.') else t.replace(',', '')
    elif ',' in t:
        t = t.replace(',', '.') if t.count(',') == 1 else t.replace(',', '')
    try:
        return float(t)
    except ValueError:
        return None


def parse_coord(text: Any) -> DFloat | None:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return text if isinstance(text, DFloat) else DFloat(text)
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    dms = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*[°d:\s]\s*(\d+(?:\.\d+)?)?\s*['m:\s]?\s*(\d+(?:\.\d+)?)?\s*[\"s]?\s*([NSEWnsew])?", t)
    if dms and (dms.group(2) or dms.group(4)) and not re.fullmatch(r'-?\d+([.,]\d+)?', t):
        deg, mi, se, hemi = float(dms.group(1)), float(dms.group(2) or 0), float(dms.group(3) or 0), (dms.group(4) or '').upper()
        v = abs(deg) + mi / 60 + se / 3600
        if deg < 0 or hemi in ('S', 'W'):
            v = -v
        out = DFloat(round(v, 7))
        out.dec = 7 if se else (5 if mi else 0)
        return out
    t = t.replace(',', '.') if t.count(',') == 1 and '.' not in t else t
    try:
        return _dfloat(t)
    except ValueError:
        return None


def normalise_props(src: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Map supplier column/property names to EUDR names. Returns (props, fixes, warnings)."""
    props: dict[str, Any] = {}
    fixes: list[str] = []
    warns: list[str] = []
    for key, val in (src or {}).items():
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        target = key if key in EUDR_PROPS or key in ('plotId', 'commodity') else _KEYMAP.get(_k(key))
        if not target:
            continue
        if target.startswith('Area'):
            unit = target.split(':')[1] if ':' in target else 'ha'
            sval = str(val).lower()
            if unit == 'ha' and re.search(r'm2|m²|sqm', sval):
                unit = 'm2'
            elif unit == 'ha' and 'acre' in sval:
                unit = 'acre'
            num = parse_number(re.sub(r'[a-z²]+', '', sval)) if not isinstance(val, (int, float)) else float(val)
            if num is None:
                warns.append(f"could not read area '{val}' from '{key}'")
                continue
            ha = num / 10000 if unit == 'm2' else num * 0.40468564 if unit == 'acre' else num
            if 'Area' in props:
                continue
            props['Area'] = round(ha, 4)
            if key != 'Area' or not isinstance(val, (int, float)) or unit != 'ha':
                fixes.append(f"converted '{key}'='{val}' to Area {props['Area']} (hectares, number)")
            continue
        if target in props:
            continue
        props[target] = str(val).strip() if target != 'plotId' else str(val).strip()
        if target in EUDR_PROPS and key != target:
            fixes.append(f"renamed property '{key}' to '{target}'")
    return props, fixes, warns


# ---------------------------------------------------------------- GeoJSON

def _strip_obj(o: Any) -> Any:
    return o


def parse_geojson(text_or_obj: Any) -> list[dict[str, Any]]:
    if isinstance(text_or_obj, (str, bytes)):
        obj = json.loads(text_or_obj, parse_float=_dfloat)
    else:
        obj = text_or_obj
    feats: list[dict[str, Any]] = []
    t = (obj or {}).get('type') if isinstance(obj, dict) else None
    if t == 'FeatureCollection':
        feats = list(obj.get('features') or [])
    elif t == 'Feature':
        feats = [obj]
    elif t in ('Point', 'MultiPoint', 'Polygon', 'MultiPolygon', 'LineString', 'MultiLineString', 'GeometryCollection'):
        feats = [{'type': 'Feature', 'geometry': obj, 'properties': {}}]
    elif isinstance(obj, list):
        feats = [f for f in obj if isinstance(f, dict)]
    else:
        raise ValueError('not a GeoJSON FeatureCollection, Feature or geometry')
    out = []
    for i, f in enumerate(feats):
        p, fixes, warns = normalise_props(f.get('properties') or {})
        pid = p.pop('plotId', None) or (str(f['id']) if f.get('id') is not None else None)
        out.append({'plotId': pid or f'plot-{i + 1}', 'sourceFormat': 'geojson', 'sourceRow': i + 1,
                    'geometry': f.get('geometry'), 'props': p, 'fixes': fixes, 'warnings': warns, 'errors': []})
    return out


# ---------------------------------------------------------------- KML / KMZ

def _coords_kml(text: str) -> list[list[DFloat]]:
    pts = []
    for tok in (text or '').split():
        parts = tok.split(',')
        if len(parts) >= 2:
            x, y = parse_coord(parts[0]), parse_coord(parts[1])
            if x is not None and y is not None:
                pts.append([x, y] + ([float(parts[2])] if len(parts) > 2 and parts[2] else []))
    return pts


def _kml_geom(el: ET.Element, ns: str) -> dict[str, Any] | None:
    tag = el.tag.replace(ns, '')
    if tag == 'Point':
        c = _coords_kml(el.findtext(f'{ns}coordinates') or '')
        return {'type': 'Point', 'coordinates': c[0]} if c else None
    if tag == 'Polygon':
        outer = el.find(f'{ns}outerBoundaryIs/{ns}LinearRing/{ns}coordinates')
        rings = [_coords_kml(outer.text if outer is not None else '')]
        for inner in el.findall(f'{ns}innerBoundaryIs/{ns}LinearRing/{ns}coordinates'):
            rings.append(_coords_kml(inner.text or ''))
        return {'type': 'Polygon', 'coordinates': rings}
    if tag in ('LineString', 'LinearRing'):
        return {'type': tag if tag == 'LineString' else 'LinearRing', 'coordinates': _coords_kml(el.findtext(f'{ns}coordinates') or '')}
    if tag == 'MultiGeometry':
        geoms = [g for g in (_kml_geom(c, ns) for c in el) if g]
        if geoms and all(g['type'] == 'Polygon' for g in geoms):
            return {'type': 'MultiPolygon', 'coordinates': [g['coordinates'] for g in geoms]}
        if geoms and all(g['type'] == 'Point' for g in geoms):
            return {'type': 'MultiPoint', 'coordinates': [g['coordinates'] for g in geoms]}
        return {'type': 'GeometryCollection', 'geometries': geoms}
    return None


def parse_kml(data: bytes | str) -> list[dict[str, Any]]:
    if isinstance(data, bytes) and data[:2] == b'PK':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith('.kml')), None)
            if not name:
                raise ValueError('KMZ has no .kml inside')
            if z.getinfo(name).file_size > 60_000_000:
                raise ValueError('KML inside KMZ is too large')
            data = z.read(name)
    root = ET.fromstring(data if isinstance(data, bytes) else data.encode())
    m = re.match(r'(\{.*\})', root.tag)
    ns = m.group(1) if m else ''
    out = []
    for i, pm in enumerate(root.iter(f'{ns}Placemark')):
        src: dict[str, Any] = {}
        nm = pm.findtext(f'{ns}name')
        for d in pm.iter(f'{ns}Data'):
            src[d.get('name')] = d.findtext(f'{ns}value')
        for d in pm.iter(f'{ns}SimpleData'):
            src[d.get('name')] = d.text
        p, fixes, warns = normalise_props(src)
        if nm and 'ProductionPlace' not in p:
            p['ProductionPlace'] = nm.strip()
            fixes.append('used KML placemark name as ProductionPlace')
        geom = None
        for child in pm:
            geom = _kml_geom(child, ns)
            if geom:
                break
        out.append({'plotId': p.pop('plotId', None) or (pm.get('id') or nm or f'plot-{i + 1}'), 'sourceFormat': 'kml',
                    'sourceRow': i + 1, 'geometry': geom, 'props': p, 'fixes': fixes, 'warnings': warns, 'errors': []})
    return out


# ---------------------------------------------------------------- CSV

def _parse_wkt(text: str) -> dict[str, Any] | None:
    t = (text or '').strip()
    if t.startswith('{'):
        try:
            return json.loads(t, parse_float=_dfloat)
        except ValueError:
            return None
    m = re.match(r'^\s*(?:SRID=\d+;)?\s*([A-Za-z]+)\s*(?:Z|M|ZM)?\s*(\(.*\))\s*$', t, re.S)
    if not m:
        return None
    kind, body = m.group(1).upper(), m.group(2)

    def pts(s: str) -> list[list[DFloat]]:
        res = []
        for pair in s.split(','):
            nums = pair.strip().strip('()').split()
            if len(nums) >= 2:
                res.append([_dfloat(nums[0]), _dfloat(nums[1])])
        return res

    def rings(s: str) -> list[list[list[DFloat]]]:
        return [pts(r) for r in re.findall(r'\(([^()]*)\)', s)]

    try:
        if kind == 'POINT':
            return {'type': 'Point', 'coordinates': pts(body)[0]}
        if kind == 'POLYGON':
            return {'type': 'Polygon', 'coordinates': rings(body)}
        if kind == 'MULTIPOLYGON':
            polys = re.findall(r'\(\s*(\([^()]*\)(?:\s*,\s*\([^()]*\))*)\s*\)', body[1:-1])
            return {'type': 'MultiPolygon', 'coordinates': [rings(p) for p in polys]}
        if kind == 'MULTIPOINT':
            return {'type': 'MultiPoint', 'coordinates': pts(body.replace('(', '').replace(')', ''))}
        if kind == 'LINESTRING':
            return {'type': 'LineString', 'coordinates': pts(body)}
    except (ValueError, IndexError):
        return None
    return None


def parse_csv(text: str) -> list[dict[str, Any]]:
    text = text.lstrip('\ufeff')
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        delim = dialect.delimiter
    except csv.Error:
        delim = ';' if sample.count(';') > sample.count(',') else ','
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    if not rows:
        return []
    cols = {_k(c): c for c in rows[0].keys() if c is not None}
    lat_c = next((cols[k] for k in cols if k in _LAT), None)
    lon_c = next((cols[k] for k in cols if k in _LON), None)
    wkt_c = next((cols[k] for k in cols if k in _WKT), None)
    pid_c = next((cols[k] for k in cols if _KEYMAP.get(k) == 'plotId'), None)
    if not ((lat_c and lon_c) or wkt_c):
        raise ValueError(f'CSV needs latitude+longitude columns or a WKT column; found {list(rows[0].keys())}')
    skip = {lat_c, lon_c, wkt_c}
    groups: dict[str, list[tuple[int, dict]]] = {}
    order: list[str] = []
    for i, r in enumerate(rows):
        pid = (r.get(pid_c) or '').strip() if pid_c else ''
        pid = pid or f'row-{i + 2}'
        if pid not in groups:
            groups[pid] = []
            order.append(pid)
        groups[pid].append((i + 2, r))
    out = []
    for pid in order:
        grp = groups[pid]
        line, first = grp[0]
        p, fixes, warns = normalise_props({k: v for k, v in first.items() if k not in skip and k})
        p.pop('plotId', None)
        errors: list[str] = []
        geom = None
        if wkt_c and (first.get(wkt_c) or '').strip():
            geom = _parse_wkt(first[wkt_c])
            if geom is None:
                errors.append(f"could not read geometry text in column '{wkt_c}'")
        elif lat_c and lon_c:
            pts = []
            for ln, r in grp:
                la, lo = parse_coord(r.get(lat_c)), parse_coord(r.get(lon_c))
                if la is None or lo is None:
                    errors.append(f'line {ln}: missing or unreadable latitude/longitude')
                    continue
                pts.append([lo, la])
            if len(pts) == 1:
                geom = {'type': 'Point', 'coordinates': pts[0]}
            elif len(pts) >= 2:
                geom = {'type': 'Polygon', 'coordinates': [pts]}
                fixes.append(f'built a polygon from {len(pts)} rows sharing plot ID {pid}')
        out.append({'plotId': pid, 'sourceFormat': 'csv', 'sourceRow': line, 'geometry': geom, 'props': p,
                    'fixes': fixes, 'warnings': warns, 'errors': errors})
    return out


# ---------------------------------------------------------------- dispatcher

def sniff_format(data: bytes, name: str = '') -> str:
    n = name.lower()
    if n.endswith('.kmz') or data[:2] == b'PK':
        return 'kml'
    head = data[:2000].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    if n.endswith(('.geojson', '.json')) or head[:1] in (b'{', b'['):
        return 'geojson'
    if n.endswith('.kml') or head.startswith(b'<?xml') or b'<kml' in head:
        return 'kml'
    return 'csv'


def parse_any(data: bytes, name: str = '') -> list[dict[str, Any]]:
    fmt = sniff_format(data, name)
    if fmt == 'kml':
        return parse_kml(data)
    text = data.decode('utf-8-sig', errors='replace')
    if fmt == 'geojson':
        return parse_geojson(text)
    return parse_csv(text)
