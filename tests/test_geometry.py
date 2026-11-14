from shapely.geometry import shape

from src.geometry import cross_plot_checks, repair_plot
from src.parse import parse_any
from src.validate import validate_fc
from src import dds as D
from tests.conftest import FIX


def _plots(name, **opts):
    raws = parse_any((FIX / name).read_bytes(), name)
    plots = [repair_plot(r, opts) for r in raws]
    cross_plot_checks(plots)
    return {p['plotId']: p for p in plots}


def test_unclosed_ring_is_closed():
    p = _plots('suppliers_mixed.geojson')['GH-002']
    ring = p['geojson']['coordinates'][0]
    assert ring[0] == ring[-1] and any('closed the open' in f for f in p['fixes'])
    assert p['props']['ProducerCountry'] == 'GH' and isinstance(p['props']['Area'], float)


def test_point_over_4ha_flagged_unless_cattle():
    p = _plots('suppliers_mixed.geojson')['GH-003']
    assert p['geometryStatus'] == 'needs_attention' and any('over 4 ha' in e for e in p['errors'])
    c = _plots('suppliers_mixed.geojson', commodity='cattle')['GH-003']
    assert not any('over 4 ha' in e for e in c['errors'])


def test_latlon_swap_by_country_box_and_by_range():
    p = _plots('suppliers_mixed.geojson')['CI-004']
    assert p['geojson']['coordinates'] == [-5.4321, 6.123456]
    assert p['producerCountry'] == 'CI'
    q = _plots('suppliers.csv')['CO-002']
    assert q['geojson']['coordinates'][0] < -70 and any('swapped' in f for f in q['fixes'])


def test_self_intersection_repaired_valid():
    p = _plots('suppliers_mixed.geojson')['BR-005']
    g = shape(p['geojson'])
    assert g.is_valid and g.area > 0 and any('Self-intersection' in f for f in p['fixes'])


def test_holes_opened_and_area_kept():
    p = _plots('suppliers_mixed.geojson')['BR-006']
    g = shape(p['geojson'])
    assert g.is_valid and all(len(poly) == 1 for poly in ([p['geojson']['coordinates']] if p['geojson']['type'] == 'Polygon' else p['geojson']['coordinates']))
    assert 76 < p['areaHa'] < 78  # 81 ha outer - 4 ha hole


def test_ccw_z_drop_and_six_decimals():
    p = _plots('suppliers_mixed.geojson')['ID-007']
    g = shape(p['geojson'])
    assert g.exterior.is_ccw
    for x, y in p['geojson']['coordinates'][0]:
        assert len(repr(x).split('.')[1]) <= 6 and len(repr(y).split('.')[1]) <= 6
    assert any('altitude' in f for f in p['fixes'])


def test_open_line_and_duplicate():
    ps = _plots('suppliers_mixed.geojson')
    assert ps['ID-008']['geometryStatus'] == 'needs_attention' and ps['ID-008']['shape'] is None
    assert any('duplicate of plot GH-001' in e for e in ps['GH-009']['errors'])


def test_kml_hole_linestring_unclosed():
    ps = _plots('suppliers.kml')
    assert ps['CIV-K2']['geometryType'] == 'Polygon' and not shape(ps['CIV-K2']['geojson']).interiors
    assert ps['CIV-K3']['geometryType'] == 'Polygon'
    assert ps['CIV-K4']['geometryStatus'] == 'needs_attention'
    assert ps['CIV-K5']['geometryStatus'] == 'fixed'


def test_zero_zero_and_projected_coordinates():
    p = repair_plot({'plotId': 'z', 'geometry': {'type': 'Point', 'coordinates': [0, 0]}, 'props': {}}, {})
    assert p['errors'] and 'at 0,0' in p['errors'][0]
    utm = repair_plot({'plotId': 'u', 'geometry': {'type': 'Point', 'coordinates': [622000.0, 700000.0]}, 'props': {'Area': 1}}, {})
    assert utm['geometryStatus'] == 'needs_attention' and 'UTM' in utm['errors'][0]
    conv = repair_plot({'plotId': 'u', 'geometry': {'type': 'Point', 'coordinates': [622000.0, 700000.0]}, 'props': {'Area': 1}},
                       {'sourceCrs': 'EPSG:32630'})
    assert conv['geometryStatus'] != 'needs_attention' and -2 < conv['geojson']['coordinates'][0] < -1


def test_tiny_and_bad_country():
    tiny = repair_plot({'plotId': 't', 'geometry': {'type': 'Polygon', 'coordinates': [[[1, 1], [1.000001, 1], [1.000001, 1.000001], [1, 1]]]},
                        'props': {}}, {})
    assert tiny['errors']
    bad = repair_plot({'plotId': 'b', 'geometry': {'type': 'Point', 'coordinates': [-2.1, 6.3]}, 'props': {'ProducerCountry': 'Atlantis'}}, {})
    assert any('not a valid ISO' in e for e in bad['errors'])


def test_clean_file_passes_validator_and_dds_shape():
    ps = list(_plots('suppliers_mixed.geojson').values()) + list(_plots('suppliers.kml').values()) + list(_plots('suppliers.csv').values())
    clean = [p for p in ps if p['geometryStatus'] != 'needs_attention' and p['geojson']]
    fc = D.feature_collection(clean)
    assert validate_fc(fc) == []
    bad = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'Area': '3'},
           'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1]], [[0.2, 0.2], [0.3, 0.2], [0.3, 0.3], [0.2, 0.2]]]}}]}
    issues = ' '.join(validate_fc(bad))
    assert 'holes' in issues and 'not closed' in issues and 'not a number' in issues
    drafts = D.build_dds(clean, {'commodity': 'cocoa', 'activityType': 'IMPORT'})
    assert len(drafts) == 1
    st = drafts[0]['statement']
    assert st['activityType'] == 'IMPORT' and st['commodities'][0]['hsHeading'] == '1801'
    import base64, json
    prod = st['commodities'][0]['producers'][0]
    assert json.loads(base64.b64decode(prod['geometryGeojson']))['type'] == 'FeatureCollection'
    must = ' '.join(drafts[0]['operatorMustFill'])
    assert 'operatorRole' in must and 'netWeight' in must and 'confirm hsHeading' in must


def test_split_by_size_and_producer_cap():
    ps = [p for p in _plots('suppliers_mixed.geojson').values() if p['geojson']]
    chunks = D.split_by_size(ps, 600, 1000)
    assert len(chunks) > 1 and sum(len(c) for c in chunks) == len(ps)
    many = [dict(p, props=dict(p['props'], ProducerName=f'P{i}')) for i, p in enumerate(ps * 300)]
    drafts = D.build_dds(many, {})
    assert all(len(d['statement']['commodities'][0]['producers']) <= 1000 for d in drafts) and len(drafts) >= 2
