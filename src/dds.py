"""DDS (Due Diligence Statement) draft shaped like the EUDR Operator API V4 'SubmitDdsRequest'.

Field names follow the V4 API reference (EUDRDueDiligenceStatementServiceV4). The API itself is SOAP with
WS-Security; this JSON is a mapping aid for the operator's own submit client or for manual entry in TRACES.
We never submit anything. Fields we cannot know are left as null and listed in 'operatorMustFill'.
"""
from __future__ import annotations

import base64
import json
from typing import Any

MAX_PRODUCERS_PER_COMMODITY = 1000
MAX_PRODUCERS_PER_DDS = 10_000
MAX_GEO_BYTES = 25 * 1024 * 1024

HS_DEFAULTS = {'cattle': '0102', 'cocoa': '1801', 'coffee': '0901', 'oil_palm': '1511', 'rubber': '4001', 'soya': '1201', 'wood': '4403'}


def feature_collection(plots: list[dict]) -> dict:
    return {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': p['props'], 'geometry': p['geojson']} for p in plots]}


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


def split_by_size(plots: list[dict], max_bytes: int, max_features: int) -> list[list[dict]]:
    """Split plots into chunks whose FeatureCollection stays under max_bytes and max_features."""
    chunks: list[list[dict]] = [[]]
    size = len(dumps(feature_collection([])))
    for p in plots:
        f = len(dumps({'type': 'Feature', 'properties': p['props'], 'geometry': p['geojson']})) + 1
        if chunks[-1] and (size + f > max_bytes or len(chunks[-1]) >= max_features):
            chunks.append([])
            size = len(dumps(feature_collection([])))
        chunks[-1].append(p)
        size += f
    return [c for c in chunks if c]


def build_dds(plots: list[dict], cfg: dict) -> list[dict]:
    """One DDS draft per chunk of <= 1000 producers-with-geometry and <= 25 MB geolocation."""
    commodity = (cfg.get('commodity') or '').lower() or None
    hs = cfg.get('hsHeading') or HS_DEFAULTS.get(commodity or '', None)
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in plots:
        key = (p['props'].get('ProducerName') or '', p.get('producerCountry') or '')
        groups.setdefault(key, []).append(p)
    producers = []
    for (name, cc), ps in groups.items():
        fc = feature_collection(ps)
        producers.append({
            'country': cc or None,
            'name': name or None,
            'geometryGeojson': base64.b64encode(dumps(fc).encode()).decode(),
            '_plots': [p['plotId'] for p in ps],
            '_geojsonBytes': len(dumps(fc).encode()),
        })
    drafts = []
    chunk: list[dict] = []
    size = 0
    for pr in producers:
        if chunk and (len(chunk) >= MAX_PRODUCERS_PER_COMMODITY or size + pr['_geojsonBytes'] > MAX_GEO_BYTES):
            drafts.append(chunk)
            chunk, size = [], 0
        chunk.append(pr)
        size += pr['_geojsonBytes']
    if chunk:
        drafts.append(chunk)
    out = []
    must = ['operatorRole (OPERATOR or REPRESENTATIVE_OPERATOR)', 'statement.activityType (IMPORT, EXPORT or DOMESTIC)',
            'statement.geoLocationConfidential (true/false)', 'commodities.descriptors.descriptionOfGoods',
            'commodities.descriptors.goodsMeasure.netWeight (kg; mandatory for IMPORT/EXPORT)',
            'commodities.hsHeading (HS code, 2-10 digits)', 'operator EORI (for IMPORT/EXPORT) and Information System credentials',
            'speciesInfo scientificName + commonName (wood products only)', 'risk assessment and risk mitigation conclusion (Art. 10-11)',
            'legality of production evidence (Art. 3(b), Art. 9(1)(h))']
    for i, prods in enumerate(drafts):
        missing = [m for m in must if not _filled(m, cfg, hs)]
        if hs and not cfg.get('hsHeading'):
            missing.append(f'confirm hsHeading: {hs} is only a default suggested from the commodity')
        for pr in prods:
            if not pr['country']:
                missing.append(f"producers.country for producer '{pr['name'] or '(no name)'}'")
        out.append({
            '_about': 'Draft shaped like EUDR Operator API V4 SubmitDdsRequest. Not submitted. Check every field before use.',
            '_part': i + 1, '_parts': len(drafts),
            'operatorRole': cfg.get('operatorRole') or None,
            'statement': {
                'internalReferenceNumber': (f"{cfg['internalReference']}-{i + 1}" if len(drafts) > 1 else cfg['internalReference'])
                if cfg.get('internalReference') else None,
                'activityType': cfg.get('activityType') or None,
                'countryOfActivity': cfg.get('countryOfActivity') or None,
                'borderCrossCountry': cfg.get('borderCrossCountry') or None,
                'comment': None,
                'commodities': [{
                    'position': 1,
                    'descriptors': {'descriptionOfGoods': cfg.get('descriptionOfGoods') or None,
                                    'goodsMeasure': {'netWeight': cfg.get('netWeightKg'), 'supplementaryUnit': None,
                                                     'supplementaryUnitQualifier': None}},
                    'hsHeading': hs,
                    'speciesInfo': None if commodity != 'wood' else {'scientificName': None, 'commonName': None},
                    'producers': [{'position': j + 1, 'country': pr['country'], 'name': pr['name'],
                                   'geometryGeojson': pr['geometryGeojson']} for j, pr in enumerate(prods)],
                }],
                'geoLocationConfidential': cfg.get('geoLocationConfidential'),
            },
            'plotsInThisPart': [pid for pr in prods for pid in pr['_plots']],
            'operatorMustFill': missing,
        })
    return out


def _filled(item: str, cfg: dict, hs: str | None) -> bool:
    if item.startswith('operatorRole'):
        return bool(cfg.get('operatorRole'))
    if item.startswith('statement.activityType'):
        return bool(cfg.get('activityType'))
    if item.startswith('statement.geoLocationConfidential'):
        return cfg.get('geoLocationConfidential') is not None
    if 'descriptionOfGoods' in item:
        return bool(cfg.get('descriptionOfGoods'))
    if 'netWeight' in item:
        return cfg.get('netWeightKg') is not None or cfg.get('activityType') == 'DOMESTIC'
    if 'hsHeading' in item:
        return bool(hs)
    if item.startswith('speciesInfo'):
        return (cfg.get('commodity') or '').lower() != 'wood'
    return False
