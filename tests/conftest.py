import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
FIX = Path(__file__).parent / 'fixtures'


@pytest.fixture(scope='session')
def fake_rasters(tmp_path_factory):
    """Tiny local stand-ins for the JRC COG and one Hansen tile (10N_010W), covering lon -2.2..-2.0, lat 6.2..6.4.

    Forest = west half (lon < -2.1). Loss year 22 (2022) in the box lon -2.15..-2.10, lat 6.30..6.35.
    """
    d = tmp_path_factory.mktemp('rasters')
    res_j = 1 / 12000
    w = h = int(0.2 / res_j)
    tj = from_origin(-2.2, 6.4, res_j, res_j)
    forest = np.zeros((h, w), dtype=np.uint8)
    forest[:, : w // 2] = 1
    jrc = d / 'jrc.tif'
    with rasterio.open(jrc, 'w', driver='GTiff', width=w, height=h, count=1, dtype='uint8', crs='EPSG:4326', transform=tj) as ds:
        ds.write(forest, 1)
    # Hansen tile 10N_010W: top-left (-10, 10), res 0.00025; only write a window around our area
    res_h = 0.00025
    tile_w = tile_h = int(10 / res_h)
    th = from_origin(-10.0, 10.0, res_h, res_h)
    hdir = d / 'hansen'
    hdir.mkdir()
    path = hdir / 'Hansen_GFC-2025-v1.13_lossyear_10N_010W.tif'
    with rasterio.open(path, 'w', driver='GTiff', width=tile_w, height=tile_h, count=1, dtype='uint8', crs='EPSG:4326',
                       transform=th, tiled=True, blockxsize=256, blockysize=256, compress='deflate', sparse_ok=True) as ds:
        from rasterio.windows import from_bounds
        win = from_bounds(-2.15, 6.30, -2.10, 6.35, th).round_offsets().round_lengths()
        ds.write(np.full((int(win.height), int(win.width)), 22, dtype=np.uint8), 1, window=win)
        win2 = from_bounds(-2.05, 6.25, -2.02, 6.28, th).round_offsets().round_lengths()
        ds.write(np.full((int(win2.height), int(win2.width)), 15, dtype=np.uint8), 1, window=win2)
    return str(jrc), str(hdir) + '/'
