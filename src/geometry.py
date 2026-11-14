"""Repair and validate plot geometry against the EUDR Information System GeoJSON rules.

Rules implemented (source: EUDR IS 'GeoJSON File Description', updated 17 Aug 2026, and the
Operator API v4 reference / validation rules):
  * WGS84 lon/lat, longitude first, in range; 6 decimals (system truncates to 6)
  * Point / MultiPoint / Polygon / MultiPolygon only; no LineString
  * polygons closed, at least 4 positions, no self-intersections, no holes
  * no duplicate vertices after 6-decimal rounding, no zero-area 'straight line' shapes
  * points: Area (ha) numeric, >= 0.0001 ha, and > 4 ha needs a polygon (except cattle)
  * ProducerCountry ISO 3166-1 alpha-2
Every change is written to the plot's fix log; anything that cannot be fixed safely becomes an error.
"""
from __future__ import annotations

import math
from typing import Any

import shapely
from pyproj import Geod, Transformer
from shapely.geometry import LineString, MultiPoint, MultiPolygon, Point, Polygon, mapping
from shapely.geometry.polygon import orient
from shapely.ops import nearest_points
from shapely.validation import explain_validity, make_valid

from .countries import in_bbox, to_iso2
from .parse import decimals_of

GEOD = Geod(ellps='WGS84')
GRID = 1e-6
POINT_MAX_HA = 4.0
MIN_AREA_HA = 0.0001
CHANNEL_M = 0.5


def geodesic_area_ha(g) -> float:
    if g is None or g.is_empty or g.geom_type not in ('Polygon', 'MultiPolygon'):
        return 0.0
    a, _ = GEOD.geometry_area_perimeter(g)
    return abs(a) / 10_000


def _walk(coords: Any):
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (int, float)):
        yield coords
    elif isinstance(coords, (list, tuple)):
        for c in coords:
            yield from _walk(c)


def _map(coords: Any, fn):
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_map(c, fn) for c in coords]


def _positions(geom: dict) -> list:
    if geom.get('type') == 'GeometryCollection':
        return [p for g in geom.get('geometries') or [] for p in _positions(g)]
    return list(_walk(geom.get('coordinates') or []))


def _map_geom(geom: dict, fn) -> dict:
    if geom.get('type') == 'GeometryCollection':
        return {'type': 'GeometryCollection', 'geometries': [_map_geom(g, fn) for g in geom.get('geometries') or []]}
    return {'type': geom['type'], 'coordinates': _map(geom.get('coordinates') or [], fn)}


def _polys(g) -> list[Polygon]:
    if g is None or g.is_empty:
        return []
    if g.geom_type == 'Polygon':
        return [g]
    if g.geom_type == 'MultiPolygon':
        return list(g.geoms)
    if g.geom_type == 'GeometryCollection':
        return [p for x in g.geoms for p in _polys(x)]
    return []


def _as_poly(parts: list[Polygon]):
    parts = [p for p in parts if not p.is_empty and p.area > 0]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _ring(coords: list, fixes: list[str], errors: list[str], label: str) -> list | None:
    pts = [(float(c[0]), float(c[1])) for c in coords if len(c) >= 2]
    if len(pts) >= 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
        fixes.append(f'closed the open {label} (first point repeated at the end)')
    if len(pts) < 4 or len(set(pts)) < 3:
        errors.append(f'{label} has fewer than 3 distinct corners; it cannot describe an area')
        return None
    return pts


def _open_holes(poly: Polygon, fixes: list[str], lat: float) -> list[Polygon]:
    """EU IS rejects holes. Open each hole to the outer edge with a narrow channel (hole stays excluded)."""
    out: list[Polygon] = []
    queue = [poly]
    guard = 0
    w = CHANNEL_M / (111_320 * max(math.cos(math.radians(lat)), 0.05))
    while queue and guard < 200:
        guard += 1
        p = queue.pop()
        if not p.interiors:
            out.append(p)
            continue
        hole = p.interiors[0]
        a, b = nearest_points(hole, p.exterior)
        dx, dy = b.x - a.x, b.y - a.y
        d = math.hypot(dx, dy) or 1e-9
        ext = LineString([(a.x - dx / d * w, a.y - dy / d * w), (b.x + dx / d * w, b.y + dy / d * w)])
        cut = p.difference(ext.buffer(w / 2, cap_style='flat'))
        queue.extend(_polys(make_valid(cut)))
    if guard >= 200:
        out.extend(queue)
    if out != [poly]:
        fixes.append(f'opened {len(poly.interiors)} hole(s) to the outer edge with a {CHANNEL_M} m channel '
                     '(the EU system rejects polygons with holes; the hole stays excluded)')
    return out


def build_shape(geom: dict, fixes: list[str], errors: list[str]):
    t = geom.get('type')
    c = geom.get('coordinates')
    if t == 'Point':
        return Point(float(c[0]), float(c[1]))
    if t == 'MultiPoint':
        pts = [Point(float(p[0]), float(p[1])) for p in c or []]
        if len(pts) == 1:
            fixes.append('MultiPoint with one point written as Point')
            return pts[0]
        return MultiPoint(pts) if pts else None
    if t == 'Polygon':
        rings = [_ring(r, fixes, errors, 'outer ring' if i == 0 else f'inner ring {i}') for i, r in enumerate(c or [])]
        if not rings or rings[0] is None:
            return None
        return Polygon(rings[0], [r for r in rings[1:] if r])
    if t == 'MultiPolygon':
        polys = []
        for j, pc in enumerate(c or []):
            rings = [_ring(r, fixes, errors, f'polygon {j + 1} ring {i + 1}') for i, r in enumerate(pc or [])]
            if rings and rings[0]:
                polys.append(Polygon(rings[0], [r for r in rings[1:] if r]))
        return MultiPolygon(polys) if len(polys) > 1 else (polys[0] if polys else None)
    if t in ('LineString', 'LinearRing'):
        pts = [(float(p[0]), float(p[1])) for p in c or []]
        if len(pts) >= 4 and pts[0] == pts[-1] and len(set(pts)) >= 3:
            fixes.append(f'converted a closed {t} (KML/GPS boundary) into a Polygon')
            return Polygon(pts)
        errors.append(f'{t} is an open line (GPS track?), not a plot boundary: the EU system rejects lines')
        return None
    if t == 'GeometryCollection':
        parts = [build_shape(g, fixes, errors) for g in geom.get('geometries') or []]
        parts = [p for p in parts if p is not None]
        polys = [p for x in parts for p in _polys(x)]
        if polys and len(polys) == len(parts):
            fixes.append('merged a GeometryCollection of polygons into a MultiPolygon')
            return _as_poly(polys)
        pts = [p for p in parts if p.geom_type == 'Point']
        if pts and len(pts) == len(parts):
            fixes.append('merged a GeometryCollection of points into a MultiPoint')
            return MultiPoint(pts)
        errors.append('mixed GeometryCollection (points and polygons): split it into separate plots')
        return None
    errors.append(f"geometry type '{t}' is not accepted (use Point, MultiPoint, Polygon or MultiPolygon)")
    return None


def circle(lon: float, lat: float, area_ha: float, n: int = 48) -> Polygon:
    r = math.sqrt(area_ha * 10_000 / math.pi)
    lons, lats, _ = GEOD.fwd([lon] * n, [lat] * n, [i * 360 / n for i in range(n)], [r] * n)
    return Polygon(zip(lons, lats))


def _round_coords(g):
    return shapely.transform(g, lambda a: (a / GRID).round() * GRID)


def clean_geojson(g) -> dict:
    def r(c):
        return [round(c[0], 6), round(c[1], 6)]
    m = mapping(g)
    return {'type': m['type'], 'coordinates': _map(m['coordinates'], r)}


def repair_plot(raw: dict, opts: dict) -> dict:
    """Return a processed plot dict. opts: defaultCountry, defaultProducerName, commodity, sourceCrs."""
    fixes: list[str] = list(raw.get('fixes') or [])
    warns: list[str] = list(raw.get('warnings') or [])
    errors: list[str] = list(raw.get('errors') or [])
    props = dict(raw.get('props') or {})
    cattle = (props.get('commodity') or opts.get('commodity') or '').lower() == 'cattle'
    plot: dict[str, Any] = {'plotId': str(raw.get('plotId')), 'sourceFormat': raw.get('sourceFormat'),
                            'sourceRow': raw.get('sourceRow'), 'shape': None, 'screenShape': None}

    # --- properties
    cc_raw = props.get('ProducerCountry') or opts.get('defaultCountry')
    cc, note = to_iso2(cc_raw)
    if cc_raw and not props.get('ProducerCountry'):
        fixes.append(f"ProducerCountry missing: used default '{cc or cc_raw}'")
    if note:
        fixes.append(note)
    if cc_raw and not cc:
        errors.append(f"ProducerCountry '{cc_raw}' is not a valid ISO 3166-1 alpha-2 code")
    if not cc_raw:
        warns.append('ProducerCountry missing: the DDS needs the country of production for every producer')
    if not props.get('ProducerName') and opts.get('defaultProducerName'):
        props['ProducerName'] = opts['defaultProducerName']
        fixes.append('ProducerName missing: used default producer name')
    if not props.get('ProductionPlace'):
        props['ProductionPlace'] = plot['plotId']
        fixes.append('ProductionPlace missing: set to the plot ID (EU recommends naming the production place)')
    for k in ('ProducerName', 'ProductionPlace'):
        if props.get(k) and len(str(props[k])) > 500:
            props[k] = str(props[k])[:500]
            fixes.append(f'{k} cut to 500 characters')

    geom = raw.get('geometry')
    if not isinstance(geom, dict) or not geom.get('type'):
        errors.append('no readable geometry')
        return _finish(plot, props, cc, None, fixes, warns, errors, None, cattle)

    # --- coordinate-level checks
    try:
        pos = _positions(geom)
    except (TypeError, ValueError):
        pos = []
    if not pos or any(len(p) < 2 or not all(isinstance(v, (int, float)) for v in p[:2]) for p in pos):
        errors.append('coordinates missing or not numbers')
        return _finish(plot, props, cc, None, fixes, warns, errors, None, cattle)
    min_dec = max(max(decimals_of(p[0]), decimals_of(p[1])) for p in pos)  # best precision seen (trailing zeros vanish)
    if any(len(p) > 2 for p in pos):
        geom = _map_geom(geom, lambda c: [c[0], c[1]])
        fixes.append('dropped altitude (3rd coordinate)')
    out_of_range = any(abs(p[0]) > 180 or abs(p[1]) > 90 for p in pos)
    if out_of_range:
        if opts.get('sourceCrs') and any(abs(p[0]) > 360 or abs(p[1]) > 360 for p in pos):
            try:
                tr = Transformer.from_crs(opts['sourceCrs'], 'EPSG:4326', always_xy=True)
                geom = _map_geom(geom, lambda c: list(tr.transform(c[0], c[1])))
                fixes.append(f"converted coordinates from {opts['sourceCrs']} to WGS84 (EPSG:4326)")
                min_dec = 7
            except Exception as e:  # noqa: BLE001
                errors.append(f"could not convert from {opts['sourceCrs']}: {e}")
        elif all(abs(p[1]) <= 180 and abs(p[0]) <= 90 for p in pos):
            geom = _map_geom(geom, lambda c: [c[1], c[0]])
            fixes.append('swapped latitude/longitude: a latitude value was outside ±90, so the file was latitude-first')
        else:
            errors.append('coordinates outside WGS84 range (metres/UTM?). Set "Source coordinate system" (e.g. EPSG:32630)')
            return _finish(plot, props, cc, None, fixes, warns, errors, min_dec, cattle)
    pos = _positions(geom)
    mx = sum(p[0] for p in pos) / len(pos)
    my = sum(p[1] for p in pos) / len(pos)
    if abs(mx) < 1e-9 and abs(my) < 1e-9:
        errors.append('plot is at 0,0 (missing coordinates)')
        return _finish(plot, props, cc, None, fixes, warns, errors, min_dec, cattle)
    inside = in_bbox(cc, mx, my)
    if inside is False and not out_of_range:
        if in_bbox(cc, my, mx):
            geom = _map_geom(geom, lambda c: [c[1], c[0]])
            fixes.append(f'swapped latitude/longitude: as written the plot falls outside {cc}, swapped it falls inside {cc}')
        else:
            warns.append(f'plot centre ({my:.4f}, {mx:.4f}) is outside the expected area of {cc}: check ProducerCountry or coordinates')
    if min_dec < 6:
        warns.append(f'source coordinates have only {min_dec} decimals (EUDR asks for at least 6, ~0.1 m); '
                     'precision cannot be added afterwards: ask the supplier for a more precise survey if possible')

    # --- shape
    shape = build_shape(geom, fixes, errors)
    if shape is None or shape.is_empty:
        return _finish(plot, props, cc, None, fixes, warns, errors, min_dec, cattle)
    lat = shape.centroid.y
    if shape.geom_type in ('Polygon', 'MultiPolygon'):
        before = geodesic_area_ha(shape)
        was_cw = any(not p.exterior.is_ccw for p in _polys(shape))
        if not shape.is_valid:
            why = explain_validity(shape)
            shape = _as_poly(_polys(make_valid(shape)))
            if shape is None:
                errors.append(f'polygon is not valid and has no area ({why}): a straight line or collapsed shape')
                return _finish(plot, props, cc, None, fixes, warns, errors, min_dec, cattle)
            after = geodesic_area_ha(shape)
            how = f'area {before:.4f} -> {after:.4f} ha' if before > 0.0001 else f'mapped area now {after:.4f} ha'
            fixes.append(f'repaired invalid polygon ({why.split("[")[0].strip()}); {how}')
        polys = []
        for p in _polys(shape):
            polys.extend(_open_holes(p, fixes, lat) if p.interiors else [p])
        shape = _as_poly(polys)
        n0 = shapely.get_num_coordinates(shape)
        snapped = shapely.set_precision(shape, GRID)
        snapped = _as_poly(_polys(snapped)) if snapped is not None else None
        if snapped is None:
            errors.append('polygon collapses to nothing at 6-decimal precision (too small or a line)')
            return _finish(plot, props, cc, None, fixes, warns, errors, min_dec, cattle)
        simp = shapely.simplify(snapped, 0, preserve_topology=True)
        simp = _as_poly(_polys(simp)) or snapped
        n1 = shapely.get_num_coordinates(simp)
        if n1 < n0:
            fixes.append(f'removed {n0 - n1} duplicate or straight-line points (6-decimal rounding / collinear)')
        shape = simp
        shape = _as_poly([orient(p, 1.0) for p in _polys(shape)])
        if was_cw:
            fixes.append('turned the outer ring counter-clockwise (RFC 7946 winding)')
        if not shape.is_valid:
            shape = _as_poly(_polys(make_valid(shape)))
            fixes.append('re-validated after rounding to 6 decimals')
            if shape is None:
                errors.append('polygon becomes invalid at 6-decimal precision')
                return _finish(plot, props, cc, None, fixes, warns, errors, min_dec, cattle)
        area = geodesic_area_ha(shape)
        if area < MIN_AREA_HA:
            errors.append(f'polygon area {area:.6f} ha is below the EU minimum of 0.0001 ha (straight line?)')
        decl = props.get('Area')
        if isinstance(decl, (int, float)) and area > 0 and abs(decl - area) / area > 0.10:
            warns.append(f'declared Area {decl} ha differs from the mapped area {area:.4f} ha by more than 10%')
        if decl is not None and decl != round(area, 4):
            fixes.append(f'Area set to the mapped geodesic area {area:.4f} ha (was {decl})')
        props['Area'] = round(area, 4)
        if area > 10_000:
            warns.append(f'very large plot ({area:.0f} ha): check this is one production place')
        screen = shape
    else:
        shape = _round_coords(shape)
        decl = props.get('Area')
        pts = [shape] if shape.geom_type == 'Point' else list(shape.geoms)
        if decl is None:
            warns.append('point without Area: the EU system will assume 4 ha')
            per = POINT_MAX_HA
        else:
            per = float(decl) / len(pts)
            if decl < MIN_AREA_HA:
                errors.append(f'Area {decl} ha is below the EU minimum of 0.0001 ha')
            if decl > POINT_MAX_HA and not cattle:
                errors.append(f'point plot with Area {decl} ha: plots over 4 ha must be mapped as a polygon '
                              '(EUDR Art. 2(28); cattle excepted). Ask the supplier for the boundary')
        area = float(decl) if decl is not None else POINT_MAX_HA * len(pts)
        screen = _as_poly([circle(p.x, p.y, max(per, MIN_AREA_HA)) for p in pts])
        warns.append(f'screened as a circle of {per:.2f} ha around each point (approximation, not the real boundary)')
    plot['areaHa'] = round(area, 4)
    return _finish(plot, props, cc, shape, fixes, warns, errors, min_dec, cattle, screen)


def _finish(plot, props, cc, shape, fixes, warns, errors, min_dec, cattle, screen=None) -> dict:
    clean = {k: props[k] for k in ('ProducerName', 'ProducerCountry', 'ProductionPlace', 'Area') if props.get(k) not in (None, '')}
    if cc:
        clean['ProducerCountry'] = cc
    if shape is not None and shape.geom_type in ('Point', 'MultiPoint') and 'Area' not in props:
        clean.pop('Area', None)
    plot.update({
        'shape': shape, 'screenShape': screen, 'props': clean, 'producerCountry': cc,
        'geometryType': shape.geom_type if shape is not None else None,
        'vertexCount': int(shapely.get_num_coordinates(shape)) if shape is not None else 0,
        'sourceDecimals': min_dec, 'isCattle': cattle,
        'fixes': list(dict.fromkeys(fixes)), 'warnings': list(dict.fromkeys(warns)), 'errors': list(dict.fromkeys(errors)),
    })
    plot.setdefault('areaHa', None)
    if shape is not None:
        c = shape.centroid
        plot['centroid'] = [round(c.x, 6), round(c.y, 6)]
        plot['bbox'] = [round(v, 6) for v in shape.bounds]
        plot['geojson'] = clean_geojson(shape)
    else:
        plot['centroid'] = plot['bbox'] = plot['geojson'] = None
    plot['geometryStatus'] = 'needs_attention' if plot['errors'] else ('fixed' if plot['fixes'] else 'ok')
    return plot


def cross_plot_checks(plots: list[dict], drop_duplicates: bool = True) -> None:
    """Flag duplicate and overlapping plots (EU rejects duplicates)."""
    geoms = [(i, p['shape']) for i, p in enumerate(plots) if p.get('shape') is not None]
    if len(geoms) < 2:
        return
    tree = shapely.STRtree([g for _, g in geoms])
    seen: set[tuple[int, int]] = set()
    for a_idx, (i, g) in enumerate(geoms):
        for b_idx in tree.query(g):
            b_idx = int(b_idx)
            if b_idx <= a_idx:
                continue
            j, h = geoms[b_idx]
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if shapely.equals_exact(g, h, tolerance=GRID / 2) or g.equals(h):
                msg = f"duplicate of plot {plots[i]['plotId']} (identical geometry)"
                if drop_duplicates:
                    plots[j]['errors'].append(msg + ': removed from the clean file')
                    plots[j]['geometryStatus'] = 'needs_attention'
                else:
                    plots[j]['warnings'].append(msg)
                continue
            if g.geom_type in ('Polygon', 'MultiPolygon') and h.geom_type in ('Polygon', 'MultiPolygon'):
                inter = g.intersection(h)
                if not inter.is_empty and inter.area > 0.01 * min(g.area, h.area):
                    ha = geodesic_area_ha(_as_poly(_polys(inter)))
                    plots[i]['warnings'].append(f"overlaps plot {plots[j]['plotId']} by {ha:.4f} ha")
                    plots[j]['warnings'].append(f"overlaps plot {plots[i]['plotId']} by {ha:.4f} ha")
