"""Deforestation screening per plot with free, key-less HTTP range reads of public rasters.

  * Forest in 2020: EC JRC Global Forest Cover 2020 (GFC2020) V4, single global Cloud-Optimized GeoTIFF
    https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/FOREST/GFC2020/LATEST/single-cog/JRC_GFC2020_V4_COG.tif
    (~10 m, value 1 = forest). Free, no key, Copernicus licence (attribution).
  * Tree-cover loss after 2020: Hansen/UMD Global Forest Change GFC-2025-v1.13 'lossyear' 10x10 degree tiles
    https://storage.googleapis.com/earthenginepartners-hansen/GFC-2025-v1.13/ (~30 m, value 21..25 = loss in 2021..2025).
    Free, no key, CC BY 4.0.
Both are read with GDAL /vsicurl/ windowed reads, so only the bytes around each plot are downloaded.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.windows import Window

JRC_URL = 'https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/FOREST/GFC2020/LATEST/single-cog/JRC_GFC2020_V4_COG.tif'
JRC_NAME = 'EC JRC Global Forest Cover 2020 (GFC2020) V4'
HANSEN_VERSION = 'GFC-2025-v1.13'
HANSEN_BASE = f'https://storage.googleapis.com/earthenginepartners-hansen/{HANSEN_VERSION}/'
HANSEN_NAME = f'Hansen/UMD/Google/USGS/NASA Global Forest Change {HANSEN_VERSION} (lossyear)'
HANSEN_RES = 0.00025
HANSEN_LAST_YEAR = 2025
CUTOFF_CODE = 20          # lossyear 20 = 2020; >20 is after the EUDR cut-off (31 Dec 2020)
_MASK_LOCK = threading.Lock()
MAX_GRID_PX = 2400        # cap per side of the analysis grid (~22 km at 10 m); larger plots are read decimated

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif',
    GDAL_HTTP_MULTIRANGE='YES', GDAL_HTTP_MERGE_CONSECUTIVE_RANGES='YES', VSI_CACHE='TRUE',
    VSI_CACHE_SIZE=str(32 * 1024 * 1024), GDAL_CACHEMAX='64', GDAL_HTTP_MAX_RETRY='2',
    GDAL_HTTP_RETRY_DELAY='1', GDAL_HTTP_TIMEOUT='20', GDAL_HTTP_CONNECTTIMEOUT='10', CPL_VSIL_CURL_CHUNK_SIZE=str(64 * 1024),
)


def _vsi(path: str) -> str:
    return '/vsicurl/' + path if path.startswith(('http://', 'https://')) else path


def hansen_tile_id(lon: float, lat: float) -> str:
    tlat = int(math.ceil(lat / 10.0) * 10)
    tlon = int(math.floor(lon / 10.0) * 10)
    return f"{abs(tlat):02d}{'N' if tlat >= 0 else 'S'}_{abs(tlon):03d}{'E' if tlon >= 0 else 'W'}"


def pixel_area_ha(lats: np.ndarray, dlat: float, dlon: float) -> np.ndarray:
    """Area (ha) of a lat/lon pixel centred at each latitude (spherical, authalic radius)."""
    r = 6_371_007.2
    lat1 = np.radians(lats + dlat / 2)
    lat2 = np.radians(lats - dlat / 2)
    return (r * r * math.radians(dlon) * np.abs(np.sin(lat1) - np.sin(lat2))) / 10_000


@dataclass
class ScreenResult:
    status: str = 'ok'
    error: str | None = None
    forest2020Ha: float = 0.0
    forest2020Pct: float = 0.0
    lossPost2020Ha: float = 0.0
    lossPost2020Pct: float = 0.0
    forestLossPost2020Ha: float = 0.0
    forestLossPost2020Pct: float = 0.0
    lossPre2021Ha: float = 0.0
    lossByYear: dict[str, float] = field(default_factory=dict)
    analysedHa: float = 0.0
    gridResolutionM: float = 0.0
    decimated: bool = False
    hansenTiles: list[str] = field(default_factory=list)
    bytesHint: int = 0
    seconds: float = 0.0
    readAt: str | None = None
    # arrays for the thumbnail (not serialised)
    forest: Any = None
    loss: Any = None
    mask: Any = None
    transform: Any = None
    _grid: Any = None


class Screener:
    """Holds ONE set of open raster handles shared by all threads (reads are serialised with a lock).

    One handle matters: GDAL allocates the global COG's full tile-offset index (~100 MB) per open handle,
    so per-thread handles would multiply memory. Stats and PDFs still run in parallel outside the lock.
    """

    def __init__(self, jrc_url: str = JRC_URL, hansen_base: str = HANSEN_BASE):
        self.jrc_url = jrc_url
        self.hansen_base = hansen_base
        self._lock = threading.Lock()
        self._jrc_ds = None
        self._h: dict[str, Any] = {}
        self._missing_tiles: set[str] = set()
        self._fails = 0             # consecutive read errors (circuit breaker)
        self.down: str | None = None  # set when the map sources look unreachable: later plots skip fast (not charged)
        for k, v in GDAL_ENV.items():  # process-wide, so worker threads see them too
            os.environ.setdefault(k, v)

    def close(self) -> None:
        if not self._lock.acquire(timeout=5):  # a hung remote read still holds it: let process exit clean up
            return
        try:
            for ds in list(self._h.values()) + [self._jrc_ds]:
                if ds is not None:
                    ds.close()
            self._h.clear()
            self._jrc_ds = None
        finally:
            self._lock.release()

    def _jrc(self):
        if self._jrc_ds is None:
            self._jrc_ds = rasterio.open(_vsi(self.jrc_url))
        return self._jrc_ds

    def _hansen(self, tile: str):
        if tile in self._missing_tiles:
            return None
        if tile not in self._h:
            url = f'{self.hansen_base}Hansen_{HANSEN_VERSION}_lossyear_{tile}.tif'
            try:
                self._h[tile] = rasterio.open(_vsi(url))
            except rasterio.errors.RasterioIOError:
                self._missing_tiles.add(tile)
                return None
            if len(self._h) > 6:  # keep a few tiles open; close the oldest
                old = next(iter(self._h))
                self._h.pop(old).close()
        return self._h[tile]

    def screen(self, shape, margin: float = 0.25) -> ScreenResult:
        t0 = time.time()
        res = ScreenResult(readAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        if self.down:
            res.status = 'error'
            res.error = self.down
            res.seconds = 0.0
            return res
        try:
            self._screen(shape, margin, res)
            self._fails = 0
        except Exception as e:  # noqa: BLE001
            res.status = 'error'
            res.error = f'{type(e).__name__}: {e}'[:300]
            if isinstance(e, rasterio.errors.RasterioIOError) or 'HTTP' in str(e) or 'timed out' in str(e).lower():
                self._fails += 1
                if self._fails >= 3 and not self.down:
                    self.down = ('forest map source unreachable after 3 failed reads (' + res.error[:120] +
                                 '); plot not screened and not charged. Re-run later.')
        res.seconds = round(time.time() - t0, 3)
        return res

    def _screen(self, shape, margin: float, res: ScreenResult) -> None:
        with self._lock:
            if self.down:
                res.status, res.error = 'error', self.down
                return
            self._read(shape, margin, res)
        if res.forest is None:
            return
        self._stats(shape, res)

    def _read(self, shape, margin: float, res: ScreenResult) -> None:
        jrc = self._jrc()
        minx, miny, maxx, maxy = shape.bounds
        w, h = maxx - minx, maxy - miny
        pad = max(w, h) * margin + 0.0005
        minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
        if miny < -60 or maxy > 80:
            res.status = 'no_coverage'
            res.error = 'outside JRC GFC2020 coverage (60S-80N)'
            return
        t = jrc.transform
        rx, ry = t.a, -t.e
        c0 = max(int(math.floor((minx - t.c) / rx)), 0)
        c1 = min(int(math.ceil((maxx - t.c) / rx)), jrc.width)
        r0 = max(int(math.floor((t.f - maxy) / ry)), 0)
        r1 = min(int(math.ceil((t.f - miny) / ry)), jrc.height)
        win = Window(c0, r0, c1 - c0, r1 - r0)
        scale = max(1, math.ceil(max(win.width, win.height) / MAX_GRID_PX))
        out_shape = (max(1, int(win.height // scale)), max(1, int(win.width // scale)))
        forest = jrc.read(1, window=win, out_shape=out_shape) == 1
        gx = win.width * rx / out_shape[1]
        gy = win.height * ry / out_shape[0]
        left, top = t.c + c0 * rx, t.f - r0 * ry
        gt = from_origin(left, top, gx, gy)
        res.decimated = scale > 1
        res.gridResolutionM = round(gy * 111_320, 1)
        lons = left + (np.arange(out_shape[1]) + 0.5) * gx
        lats = top - (np.arange(out_shape[0]) + 0.5) * gy

        # Hansen lossyear sampled at the analysis-grid pixel centres (nearest neighbour)
        loss = np.zeros(out_shape, dtype=np.uint8)
        tiles_lat = np.ceil(lats / 10.0) * 10
        tiles_lon = np.floor(lons / 10.0) * 10
        for tl in np.unique(tiles_lat):
            rows = np.where(tiles_lat == tl)[0]
            for tn in np.unique(tiles_lon):
                cols = np.where(tiles_lon == tn)[0]
                tile = hansen_tile_id(float(lons[cols[0]]), float(lats[rows[0]]))
                ds = self._hansen(tile)
                res.hansenTiles.append(tile)
                if ds is None:
                    continue
                ht = ds.transform
                hc = np.floor((lons[cols] - ht.c) / ht.a).astype(int).clip(0, ds.width - 1)
                hr = np.floor((ht.f - lats[rows]) / -ht.e).astype(int).clip(0, ds.height - 1)
                hwin = Window(int(hc.min()), int(hr.min()), int(hc.max() - hc.min() + 1), int(hr.max() - hr.min() + 1))
                block = ds.read(1, window=hwin)
                loss[np.ix_(rows, cols)] = block[np.ix_(hr - hwin.row_off, hc - hwin.col_off)]
        res.hansenTiles = sorted(set(res.hansenTiles))
        if all(t_ in self._missing_tiles for t_ in res.hansenTiles):
            res.status = 'partial'
            res.error = 'no Hansen loss tile for this area (ocean/out of coverage): loss not screened'
        res.forest, res.loss, res.transform = forest, loss, gt
        res._grid = (out_shape, lats, gx, gy)

    def _stats(self, shape, res: ScreenResult) -> None:
        forest, loss, gt = res.forest, res.loss, res.transform
        out_shape, lats, gx, gy = res._grid

        with _MASK_LOCK:  # rasterize via GDAL MEM datasets; serialise to avoid cross-thread geotransform races
            inside = ~geometry_mask([shape], out_shape=out_shape, transform=gt, all_touched=False)
            if inside.sum() == 0:
                inside = ~geometry_mask([shape], out_shape=out_shape, transform=gt, all_touched=True)
        pa = pixel_area_ha(lats, gy, gx)[:, None] * np.ones((1, out_shape[1]))
        tot = float((pa * inside).sum())
        post = inside & (loss > CUTOFF_CODE)
        res.analysedHa = round(tot, 4)
        res.forest2020Ha = round(float((pa * (inside & forest)).sum()), 4)
        res.lossPost2020Ha = round(float((pa * post).sum()), 4)
        res.forestLossPost2020Ha = round(float((pa * (post & forest)).sum()), 4)
        res.lossPre2021Ha = round(float((pa * (inside & (loss > 0) & (loss <= CUTOFF_CODE))).sum()), 4)
        if tot > 0:
            res.forest2020Pct = round(100 * res.forest2020Ha / tot, 2)
            res.lossPost2020Pct = round(100 * res.lossPost2020Ha / tot, 2)
            res.forestLossPost2020Pct = round(100 * res.forestLossPost2020Ha / tot, 2)
        for code in range(CUTOFF_CODE + 1, HANSEN_LAST_YEAR - 2000 + 1):
            ha = float((pa * (inside & (loss == code))).sum())
            if ha > 0:
                res.lossByYear[str(2000 + code)] = round(ha, 4)
        res.mask = inside


def classify(sr: ScreenResult | None, geometry_ok: bool, is_point: bool, th: dict) -> tuple[str, list[str]]:
    """Return (riskFlag, reasons). Flags: likely_compliant | needs_review | high_risk | not_screened."""
    reasons: list[str] = []
    if sr is None or sr.status in ('error', 'no_coverage'):
        return 'not_screened', [f"screening not possible: {sr.error if sr else 'screening disabled'}"]
    hi_ha, hi_pct = th.get('highRiskMinHa', 0.5), th.get('highRiskPct', 2.0)
    rv_forest = th.get('reviewForestPct', 10.0)
    flag = 'likely_compliant'
    if sr.forestLossPost2020Ha >= hi_ha or sr.forestLossPost2020Pct >= hi_pct:
        flag = 'high_risk'
        reasons.append(f'{sr.forestLossPost2020Ha:.2f} ha ({sr.forestLossPost2020Pct:.1f}%) of the plot was forest in 2020 '
                       f'(JRC) and shows tree-cover loss in 2021-{HANSEN_LAST_YEAR} (Hansen)')
    elif sr.forestLossPost2020Ha > 0:
        flag = 'needs_review'
        reasons.append(f'small overlap of 2020 forest and post-2020 tree-cover loss: {sr.forestLossPost2020Ha:.3f} ha')
    if sr.lossPost2020Ha > 0 and sr.forestLossPost2020Ha == 0:
        flag = 'needs_review' if flag == 'likely_compliant' else flag
        reasons.append(f'post-2020 tree-cover loss of {sr.lossPost2020Ha:.3f} ha outside JRC 2020 forest (map disagreement or plantation harvest)')
    if sr.forest2020Pct >= rv_forest:
        flag = 'needs_review' if flag == 'likely_compliant' else flag
        reasons.append(f'{sr.forest2020Pct:.1f}% of the plot was mapped as forest on 31 Dec 2020: confirm the commodity was '
                       'not produced on land converted after the cut-off (agroforestry and tree crops can map as forest)')
    if sr.status == 'partial':
        flag = 'needs_review' if flag == 'likely_compliant' else flag
        reasons.append(sr.error or 'partial screening')
    if not geometry_ok:
        flag = 'needs_review' if flag == 'likely_compliant' else flag
        reasons.append('geometry has unresolved errors: fix before filing')
    if is_point:
        reasons.append('point plot screened as an approximate circle: a real boundary gives stronger evidence')
    if flag == 'likely_compliant':
        reasons.insert(0, f'no post-2020 tree-cover loss detected and {sr.forest2020Pct:.1f}% forest in 2020 '
                          f'(below the {rv_forest:.0f}% review threshold)')
    return flag, reasons
