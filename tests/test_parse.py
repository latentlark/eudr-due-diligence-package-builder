from src.parse import parse_any, parse_coord, parse_csv, parse_geojson, parse_kml, parse_number, sniff_format, _parse_wkt, normalise_props
from tests.conftest import FIX


def test_parse_number_formats():
    assert parse_number('2,5') == 2.5
    assert parse_number('1.234,5') == 1234.5
    assert parse_number('1,234.5') == 1234.5
    assert parse_number(' 3 ') == 3.0
    assert parse_number('abc') is None


def test_parse_coord_decimals_and_dms():
    c = parse_coord('4,812345')
    assert abs(c - 4.812345) < 1e-12 and c.dec == 6
    d = parse_coord("6°41'30\"N")
    assert abs(d - 6.691667) < 1e-5
    w = parse_coord("1°30'0\"W")
    assert w < 0


def test_property_mapping_units():
    p, fixes, _ = normalise_props({'farmer': 'A', 'country': 'Ghana', 'area_m2': '25000', 'plot_id': 'X'})
    assert p['ProducerName'] == 'A' and p['ProducerCountry'] == 'Ghana' and p['Area'] == 2.5 and p['plotId'] == 'X'
    p2, _, _ = normalise_props({'acres': '10'})
    assert abs(p2['Area'] - 4.0469) < 1e-3
    p3, _, _ = normalise_props({'Area': '3 ha'})
    assert p3['Area'] == 3.0


def test_csv_semicolon_decimal_commas_and_multirow_polygon():
    raws = parse_csv((FIX / 'suppliers.csv').read_text())
    ids = [r['plotId'] for r in raws]
    assert ids == ['CO-001', 'CO-002', 'CO-003', 'CO-004', 'CO-010']
    poly = raws[-1]
    assert poly['geometry']['type'] == 'Polygon' and len(poly['geometry']['coordinates'][0]) == 4
    assert raws[0]['geometry']['coordinates'][0] < -75  # lon first


def test_kml_types_and_names():
    raws = parse_kml((FIX / 'suppliers.kml').read_bytes())
    assert [r['geometry']['type'] for r in raws] == ['Polygon', 'Polygon', 'LineString', 'Point', 'Polygon']
    assert len(raws[1]['geometry']['coordinates']) == 2  # outer + hole
    assert raws[0]['props']['ProductionPlace'] == 'CIV-K1'
    assert raws[3]['props']['Area'] == 6.5


def test_geojson_and_sniff():
    data = (FIX / 'suppliers_mixed.geojson').read_bytes()
    assert sniff_format(data, 'x.json') == 'geojson'
    assert sniff_format((FIX / 'suppliers.kml').read_bytes()) == 'kml'
    assert sniff_format(b'a;b\n1;2') == 'csv'
    raws = parse_any(data, 'suppliers_mixed.geojson')
    assert len(raws) == 9 and raws[0]['plotId'] == 'GH-001'
    single = parse_geojson({'type': 'Point', 'coordinates': [1.5, 2.5]})
    assert single[0]['geometry']['type'] == 'Point'


def test_wkt():
    g = _parse_wkt('POLYGON ((-2.1 6.3, -2.0 6.3, -2.0 6.4, -2.1 6.3))')
    assert g['type'] == 'Polygon' and len(g['coordinates'][0]) == 4
    m = _parse_wkt('MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)), ((2 2, 3 2, 3 3, 2 2)))')
    assert m['type'] == 'MultiPolygon' and len(m['coordinates']) == 2
    raws = parse_csv('id,wkt\nA,"POINT (-2.123456 6.123456)"\n')
    assert raws[0]['geometry']['type'] == 'Point'


def test_n8n_style_csv_mixed_points_polygons_wkt():
    from src.geometry import repair_plot
    t = ('plot_id,farmer,country,area_ha,latitude,longitude,wkt\n'
         'A,"X, Y",GH,"1,2",6.123456,-2.123456,\n'
         'B,Z,GH,,6.2,-2.2,\nB,Z,GH,,6.2,-2.199,\nB,Z,GH,,6.201,-2.199,\n'
         'C,W,GH,,,,"POLYGON ((-2.1 6.3, -2.09 6.3, -2.09 6.31, -2.1 6.3))"\n')
    plots = [repair_plot(r, {}) for r in parse_csv(t)]
    assert [(p['plotId'], p['geometryType']) for p in plots] == [('A', 'Point'), ('B', 'Polygon'), ('C', 'Polygon')]
    assert plots[0]['props']['ProducerName'] == 'X, Y' and plots[0]['props']['Area'] == 1.2
