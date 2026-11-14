import asyncio

import numpy as np
from shapely.geometry import box

from src.screening import Screener, classify, hansen_tile_id, pixel_area_ha
from src.package import build_package


def test_tile_ids():
    assert hansen_tile_id(-63.01, -9.51) == '00N_070W'
    assert hansen_tile_id(-2.1, 6.3) == '10N_010W'
    assert hansen_tile_id(101.5, 0.5) == '10N_100E'
    assert hansen_tile_id(-75.6, 4.8) == '10N_080W'
    assert hansen_tile_id(15.0, -12.3) == '10S_010E'


def test_pixel_area_equator():
    a = pixel_area_ha(np.array([0.0]), 0.00025, 0.00025)[0]
    assert 0.076 < a < 0.078  # ~27.8 m x 27.6 m


def test_screen_with_local_rasters(fake_rasters):
    jrc, hdir = fake_rasters
    s = Screener(jrc_url=jrc, hansen_base=hdir)
    th = {'highRiskMinHa': 0.5, 'highRiskPct': 2, 'reviewForestPct': 10}
    hi = s.screen(box(-2.14, 6.31, -2.12, 6.33))
    assert hi.status == 'ok' and hi.forest2020Pct > 99 and hi.forestLossPost2020Pct > 99
    assert list(hi.lossByYear) == ['2022']
    assert classify(hi, True, False, th)[0] == 'high_risk'
    clean = s.screen(box(-2.08, 6.21, -2.06, 6.23))
    assert clean.forest2020Ha == 0 and clean.lossPost2020Ha == 0
    assert classify(clean, True, False, th)[0] == 'likely_compliant'
    old = s.screen(box(-2.045, 6.255, -2.03, 6.27))
    assert old.lossPre2021Ha > 0 and old.lossPost2020Ha == 0
    assert classify(old, True, False, th)[0] == 'likely_compliant'
    forest = s.screen(box(-2.19, 6.21, -2.17, 6.23))
    assert classify(forest, True, False, th)[0] == 'needs_review'
    area_ha = 0.02 * 111.32 * 0.02 * 110.57 * 100 * np.cos(np.radians(6.32))
    assert abs(hi.analysedHa - area_ha) / area_ha < 0.03
    assert classify(hi, False, False, th)[0] == 'high_risk'
    assert classify(clean, False, False, th)[0] == 'needs_review'
    s.close()


def test_classify_not_screened():
    assert classify(None, True, False, {})[0] == 'not_screened'


def _run(cfg, sources, charge_limit=None, screener=None):
    store, rows, charges = {}, [], []

    async def put(k, d, c):
        store[k] = d

    async def push(r):
        rows.append(r)

    async def charge(n):
        if charge_limit is not None and len(charges) >= charge_limit:
            return False
        charges.append(n)
        return True

    summary = asyncio.run(build_package(cfg, sources, put, push, charge, lambda k: None, log=lambda *_: None, screener=screener))
    return summary, store, rows, charges


def test_package_offline_and_caps():
    from tests.conftest import FIX
    src = [(f, (FIX / f).read_bytes()) for f in ('suppliers_mixed.geojson', 'suppliers.csv', 'suppliers.kml')]
    summary, store, rows, charges = _run({'runScreening': False, 'generatePdf': True}, src)
    assert summary['plotsProcessed'] == 19 and summary['cleanFileValidation'] == 'passed'
    assert store['plot-GH-001.pdf'][:4] == b'%PDF' and 'PACKAGE.zip' in store and 'NEEDS_ATTENTION.geojson' in store
    assert len(charges) == 18  # the open line has no geometry: free
    capped, _, rows2, _ = _run({'runScreening': False, 'generatePdf': False, 'maxPlots': 5}, src)
    assert capped['plotsProcessed'] == 5 and capped['plotsSkippedByCap'] == 14
    limited, _, rows3, ch3 = _run({'runScreening': False, 'generatePdf': False, 'concurrency': 1}, src, charge_limit=3)
    assert len(ch3) == 3 and limited['plotsCharged'] == 3
    big = _run({'runScreening': False, 'maxInputFileMb': 0.001}, src)[0]
    assert big['inputProblems']


def test_package_with_local_screening(fake_rasters):
    jrc, hdir = fake_rasters
    gj = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'plotId': 'HI', 'ProducerCountry': 'GH'}, 'geometry': box(-2.14, 6.31, -2.12, 6.33).__geo_interface__},
        {'type': 'Feature', 'properties': {'plotId': 'OK', 'ProducerCountry': 'GH'}, 'geometry': box(-2.08, 6.21, -2.06, 6.23).__geo_interface__}]}
    s = Screener(jrc_url=jrc, hansen_base=hdir)
    summary, store, rows, charges = _run({'geojson': gj, 'commodity': 'cocoa'}, [], screener=s)
    flags = {r['plotId']: r['riskFlag'] for r in rows}
    assert flags == {'HI': 'high_risk', 'OK': 'likely_compliant'}
    assert len(charges) == 2 and store['plot-HI.pdf'][:4] == b'%PDF'
    s.close()


def _fallback_rasters(fake_rasters, tmp_path):
    """Add a Hansen treecover2000 tile matching the fake JRC forest (west half 80% canopy, east half 0%)."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.windows import from_bounds
    jrc, hdir = fake_rasters
    res_h = 0.00025
    th = from_origin(-10.0, 10.0, res_h, res_h)
    path = f'{hdir}Hansen_GFC-2025-v1.13_treecover2000_10N_010W.tif'
    import os
    if not os.path.exists(path):
        with rasterio.open(path, 'w', driver='GTiff', width=40000, height=40000, count=1, dtype='uint8', crs='EPSG:4326',
                           transform=th, tiled=True, blockxsize=256, blockysize=256, compress='deflate', sparse_ok=True) as ds:
            win = from_bounds(-2.2, 6.2, -2.1, 6.4, th).round_offsets().round_lengths()
            ds.write(np.full((int(win.height), int(win.width)), 80, dtype=np.uint8), 1, window=win)
    return jrc, hdir


def test_fallback_when_jrc_unreachable(fake_rasters, tmp_path):
    from src.screening import SRC_FALLBACK, SRC_JRC
    jrc, hdir = _fallback_rasters(fake_rasters, tmp_path)
    th = {'highRiskMinHa': 0.5, 'highRiskPct': 2, 'reviewForestPct': 10}
    s = Screener(jrc_url=str(tmp_path / 'missing-jrc.tif'), hansen_base=hdir, gfw_tiles=None)
    hi = s.screen(box(-2.14, 6.31, -2.12, 6.33))
    assert hi.status == 'ok' and hi.forest2020Source == SRC_FALLBACK and hi.jrcError
    assert hi.forest2020Pct > 99 and hi.forestLossPost2020Pct > 99
    flag, reasons = classify(hi, True, False, th)
    assert flag == 'high_risk' and 'JRC map was unavailable' in reasons[0]
    clean = s.screen(box(-2.08, 6.21, -2.06, 6.23))
    assert clean.forest2020Source == SRC_FALLBACK and clean.forest2020Ha == 0
    assert classify(clean, True, False, th)[0] == 'likely_compliant'
    # old loss (2015) removes forest from the 2020 fallback map
    old = s.screen(box(-2.045, 6.255, -2.03, 6.27))
    assert old.forest2020Ha == 0 and old.lossPre2021Ha > 0
    assert s.jrc_down and s.source_counts[SRC_FALLBACK] == 3 and s.source_counts[SRC_JRC] == 0 and not s.down
    s.close()


def test_fallback_only_and_charges(fake_rasters, tmp_path):
    jrc, hdir = _fallback_rasters(fake_rasters, tmp_path)
    gj = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'plotId': 'HI', 'ProducerCountry': 'GH'}, 'geometry': box(-2.14, 6.31, -2.12, 6.33).__geo_interface__},
        {'type': 'Feature', 'properties': {'plotId': 'OK', 'ProducerCountry': 'GH'}, 'geometry': box(-2.08, 6.21, -2.06, 6.23).__geo_interface__}]}
    s = Screener(jrc_url=str(tmp_path / 'missing-jrc.tif'), hansen_base=hdir, gfw_tiles=None)
    summary, store, rows, charges = _run({'geojson': gj, 'commodity': 'cocoa'}, [], screener=s)
    assert {r['plotId']: r['riskFlag'] for r in rows} == {'HI': 'high_risk', 'OK': 'likely_compliant'}
    assert all(r['forest2020Source'] == 'hansen_treecover2000_minus_loss' for r in rows)
    assert len(charges) == 2 and summary['forest2020Sources']['hansen_treecover2000_minus_loss'] == 2
    assert summary['plotsNotScreenedMapError'] == 0 and store['plot-HI.pdf'][:4] == b'%PDF'
    s.close()


def test_both_sources_down_not_charged(tmp_path):
    gj = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'plotId': f'P{i}', 'ProducerCountry': 'GH'},
         'geometry': box(-2.14 + i * 0.01, 6.31, -2.135 + i * 0.01, 6.315).__geo_interface__} for i in range(5)]}
    s = Screener(jrc_url=str(tmp_path / 'no-jrc.tif'), hansen_base='https://127.0.0.1:9/', gfw_tiles='http://127.0.0.1:9/{z}/{x}/{y}.png')
    summary, store, rows, charges = _run({'geojson': gj, 'commodity': 'cocoa', 'generatePdf': False, 'concurrency': 1}, [], screener=s)
    assert len(rows) == 5 and all(r['riskFlag'] == 'not_screened' for r in rows)
    assert charges == [] and summary['plotsNotScreenedMapError'] == 5 and summary['plotsCharged'] == 0
    s.close()


def test_hidden_fallback_switch(fake_rasters, tmp_path, monkeypatch):
    jrc, hdir = _fallback_rasters(fake_rasters, tmp_path)
    import src.package as P
    monkeypatch.setattr(P, 'Screener', lambda: Screener(jrc_url=jrc, hansen_base=hdir, gfw_tiles='http://127.0.0.1:9/{z}/{x}/{y}.png'))
    gj = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'plotId': 'HI', 'ProducerCountry': 'GH'}, 'geometry': box(-2.14, 6.31, -2.12, 6.33).__geo_interface__}]}
    summary, store, rows, charges = _run({'geojson': gj, 'commodity': 'cocoa', '_forestSource': 'hansen', 'generatePdf': False}, [])
    assert rows[0]['forest2020Source'] == 'hansen_treecover2000_minus_loss' and rows[0]['riskFlag'] == 'high_risk'
    assert summary['jrcUnavailable'] and len(charges) == 1


def _gfw_server():
    """Local stand-in for the GFW tile server: opaque (forest) where lon < -2.1, like the fake JRC map."""
    import http.server
    import io as _io
    import threading
    from PIL import Image

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            z, x, y = (int(v) for v in self.path.strip('/').split('.')[0].split('/'))
            n = 256 * 2 ** z
            lon = (x * 256 + np.arange(256) + 0.5) / n * 360 - 180
            a = np.zeros((256, 256, 4), dtype=np.uint8)
            a[:, lon < -2.1, 3] = 255
            buf = _io.BytesIO()
            Image.fromarray(a, 'RGBA').save(buf, 'PNG')
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.end_headers()
            self.wfile.write(buf.getvalue())

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_gfw_mirror_when_jrc_down(fake_rasters, tmp_path):
    from src.screening import SRC_GFW
    jrc, hdir = fake_rasters
    srv = _gfw_server()
    try:
        s = Screener(jrc_url=str(tmp_path / 'missing.tif'), hansen_base=hdir,
                     gfw_tiles=f'http://127.0.0.1:{srv.server_address[1]}/{{z}}/{{x}}/{{y}}.png')
        th = {'highRiskMinHa': 0.5, 'highRiskPct': 2, 'reviewForestPct': 10}
        hi = s.screen(box(-2.14, 6.31, -2.12, 6.33))
        assert hi.forest2020Source == SRC_GFW and hi.forest2020Pct > 99 and hi.forestLossPost2020Pct > 99
        flag, reasons = classify(hi, True, False, th)
        assert flag == 'high_risk' and 'Global Forest Watch' in reasons[0]
        clean = s.screen(box(-2.08, 6.21, -2.06, 6.23))
        assert clean.forest2020Source == SRC_GFW and clean.forest2020Ha == 0
        assert s.source_counts[SRC_GFW] == 2 and not s.gfw_down
        s.close()
    finally:
        srv.shutdown()
