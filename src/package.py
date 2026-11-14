"""Core pipeline: parse -> repair -> screen -> evidence -> package. Independent of the Apify SDK."""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import re
import threading
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import dds as D
from .geometry import cross_plot_checks, repair_plot
from .parse import parse_any, parse_geojson
from .report import build_pdf
from .screening import (FALLBACK_NAME, HANSEN_BASE, HANSEN_LAST_YEAR, HANSEN_NAME, HANSEN_VERSION, JRC_NAME, JRC_URL, SRC_FALLBACK,
                        SRC_JRC, ScreenResult, Screener, classify)
from .validate import validate_fc

VERSION = '0.1.0'
EVENT = 'plot-processed'
PLOT_SCREEN_TIMEOUT = 120.0  # secs per plot (normal: 1-5 s); slower means the remote rasters are degraded

Put = Callable[[str, bytes, str], Awaitable[None]]
Push = Callable[[dict], Awaitable[None]]
Charge = Callable[[int], Awaitable[bool]]   # returns False when the user's max charge is reached
Url = Callable[[str], str | None]


async def _in_daemon(fn, *args):
    """Run a blocking call in a daemon thread. Unlike asyncio.to_thread, a remote raster read that hangs cannot
    keep the process alive at exit (the default executor joins its threads), so the run still ends on time."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def done(ok: bool, val) -> None:
        if not fut.done():
            fut.set_result(val) if ok else fut.set_exception(val)

    def run() -> None:
        try:
            res, ok = fn(*args), True
        except BaseException as e:  # noqa: BLE001
            res, ok = e, False
        try:
            loop.call_soon_threadsafe(done, ok, res)
        except RuntimeError:  # loop already closed: nobody is waiting any more
            pass

    threading.Thread(target=run, daemon=True).start()
    return await fut


def key_safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9!\-_.'()]", '-', str(s))[:120] or 'plot'


def thresholds(cfg: dict) -> dict:
    return {'highRiskMinHa': float(cfg.get('highRiskMinHa', 0.5)), 'highRiskPct': float(cfg.get('highRiskPct', 2.0)),
            'reviewForestPct': float(cfg.get('reviewForestPct', 10.0))}


def read_sources(sources: list[tuple[str, bytes]], max_bytes: int) -> tuple[list[dict], list[str]]:
    raws: list[dict] = []
    problems: list[str] = []
    for name, data in sources:
        if len(data) > max_bytes:
            problems.append(f'{name}: {len(data) / 1e6:.1f} MB is over the {max_bytes / 1e6:.0f} MB input cap, skipped')
            continue
        try:
            got = parse_any(data, name)
        except Exception as e:  # noqa: BLE001
            problems.append(f'{name}: could not read file ({type(e).__name__}: {str(e)[:200]})')
            continue
        for r in got:
            r['source'] = name
        raws.extend(got)
    return raws, problems


async def _link(url, key: str):
    u = url(key)
    return (await u) if inspect.isawaitable(u) else u


async def build_package(cfg: dict, sources: list[tuple[str, bytes]], put: Put, push: Push, charge: Charge,
                        url: Url, log=print, screener: Screener | None = None, progress=None) -> dict:
    t_start = time.time()
    generated = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    max_plots = int(cfg.get('maxPlots', 200))
    max_in = int(float(cfg.get('maxInputFileMb', 50)) * 1e6)
    th = thresholds(cfg)
    run_meta = {'generatedAt': generated, 'version': VERSION, 'runId': cfg.get('_runId'), 'thresholds': th}

    files: dict[str, tuple[bytes, str]] = {}
    _put = put

    async def put(key: str, data: bytes, ctype: str) -> None:  # noqa: F811 - keep a copy for PACKAGE.zip
        files[key] = (data, ctype)
        await _put(key, data, ctype)

    raws, problems = read_sources(sources, max_in)
    if cfg.get('geojson'):
        try:
            raws.extend(dict(r, source='input.geojson') for r in parse_geojson(cfg['geojson']))
        except Exception as e:  # noqa: BLE001
            problems.append(f'GeoJSON input field: {e}')
    skipped = max(0, len(raws) - max_plots)
    if skipped:
        problems.append(f'{len(raws)} plots found; only the first {max_plots} processed (maxPlots). {skipped} skipped, not charged')
        raws = raws[:max_plots]
    for p in problems:
        log(f'WARN {p}')
    seen_ids: dict[str, int] = {}
    for r in raws:
        pid = str(r.get('plotId'))
        if pid in seen_ids:
            seen_ids[pid] += 1
            r['plotId'] = f'{pid}-{seen_ids[pid]}'
            r.setdefault('warnings', []).append(f'plot ID {pid} used more than once: renamed to {r["plotId"]}')
        else:
            seen_ids[pid] = 1

    opts = {'defaultCountry': cfg.get('defaultCountry'), 'defaultProducerName': cfg.get('defaultProducerName'),
            'commodity': cfg.get('commodity'), 'sourceCrs': cfg.get('sourceCrs')}
    plots = [repair_plot(r, opts) for r in raws]
    cross_plot_checks(plots, drop_duplicates=cfg.get('removeDuplicates', True))
    log(f'{len(plots)} plots parsed: ' + ', '.join(f"{s}={sum(p['geometryStatus'] == s for p in plots)}" for s in ('ok', 'fixed', 'needs_attention')))

    do_screen = cfg.get('runScreening', True)
    own_screener = screener is None and do_screen
    if own_screener:
        screener = Screener()
    sem = asyncio.Semaphore(int(cfg.get('concurrency', 4)))
    stop = asyncio.Event()
    rows: list[dict] = []
    charged = 0
    pdf_keys: list[str] = []

    deadline = float(cfg.get('_deadline') or 0) or None   # epoch secs: stop new plots here (graceful time budget)
    timed_out = 0

    def time_left() -> float | None:
        return None if deadline is None else deadline - time.time()

    async def one(i: int, plot: dict):
        nonlocal charged, timed_out
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            left = time_left()
            if left is not None and left <= 0:
                # Out of time: keep a free, explanatory row instead of timing out with nothing.
                row = make_row(plot, None, 'not_screened', ['run time budget reached before this plot: not processed and not charged; '
                                                            're-run with a longer timeout or fewer plots'])
                row['screeningStatus'] = 'skipped_time_limit'
                row['charged'] = False
                plot['row'] = row
                rows.append(row)
                timed_out += 1
                return
            sr = None
            if do_screen and plot.get('screenShape') is not None:
                per_plot = PLOT_SCREEN_TIMEOUT if left is None else max(5.0, min(PLOT_SCREEN_TIMEOUT, left))
                try:
                    sr = await asyncio.wait_for(_in_daemon(screener.screen, plot['screenShape']), timeout=per_plot)
                except asyncio.TimeoutError:
                    # The JRC server is this slow: use the Hansen fallback for this and every later plot.
                    screener.mark_jrc_down(f'JRC map too slow (no answer within {per_plot:.0f}s)')
                    left = time_left()
                    retry = PLOT_SCREEN_TIMEOUT if left is None else max(5.0, min(PLOT_SCREEN_TIMEOUT, left))
                    try:
                        sr = await asyncio.wait_for(_in_daemon(screener.screen, plot['screenShape'], 0.25, True), timeout=retry)
                        sr.jrcError = sr.jrcError or screener.jrc_down
                    except asyncio.TimeoutError:
                        # Both map sources are degraded: stop screening the rest (circuit breaker) so the run still
                        # finishes with every plot listed; unscreened plots are not charged.
                        if not screener.down:
                            screener.down = ('forest map sources too slow (JRC and the Hansen fallback); plot not screened '
                                             'and not charged. Re-run later.')
                        sr = ScreenResult(status='error', error=screener.down)
            flag, reasons = classify(sr, not plot['errors'], plot.get('geometryType') in ('Point', 'MultiPoint'), th) \
                if plot.get('shape') is not None else ('not_screened', ['no usable geometry'])
            if not do_screen and plot.get('shape') is not None:
                flag, reasons = 'not_screened', ['screening disabled in input']
            row = make_row(plot, sr, flag, reasons)
            if cfg.get('generatePdf', True) and plot.get('shape') is not None:
                key = f"plot-{key_safe(plot['plotId'])}.pdf"
                pdf = await asyncio.to_thread(build_pdf, row, plot, sr, run_meta)
                await put(key, pdf, 'application/pdf')
                row['pdfKey'] = key
                row['pdfUrl'] = await _link(url, key)
                pdf_keys.append(key)
            billable = plot.get('shape') is not None and (not do_screen or (sr is not None and sr.status in ('ok', 'partial', 'no_coverage')))
            row['charged'] = False
            if billable:
                ok = await charge(1)
                row['charged'] = ok
                if ok:
                    charged += 1
                else:
                    stop.set()
                    log('Max charge reached: stopping. Remaining plots are not processed or charged.')
                    return
            plot['row'] = row
            rows.append(row)
            if len(rows) % 25 == 0:
                log(f'{len(rows)}/{len(plots)} plots done')
                if progress:
                    await progress(f'{len(rows)}/{len(plots)} plots screened')

    try:
        await asyncio.gather(*(one(i, p) for i, p in enumerate(plots)))
    finally:
        if own_screener and screener is not None:
            try:
                await asyncio.wait_for(_in_daemon(screener.close), timeout=10)
            except Exception:  # noqa: BLE001 - never fail the package on cleanup
                pass

    done = [p for p in plots if p.get('row')]
    clean = [p for p in done if p['geometryStatus'] != 'needs_attention' and p.get('geojson')]
    attention = [p for p in done if p['geometryStatus'] == 'needs_attention' and p.get('geojson')]
    max_out = int(float(cfg.get('maxOutputFileMb', 24)) * 1024 * 1024)
    chunks = D.split_by_size(clean, max_out, int(cfg.get('maxPlotsPerFile', 1000))) if clean else []
    clean_keys: list[str] = []
    validation: list[str] = []
    all_cattle = (cfg.get('commodity') or '').lower() == 'cattle'
    for n, ch in enumerate(chunks):
        key = 'EUDR_CLEAN.geojson' if len(chunks) == 1 else f'EUDR_CLEAN_part{n + 1}.geojson'
        fc = D.feature_collection(ch)
        validation += [f'{key}: {x}' for x in validate_fc(fc, cattle=all_cattle, max_bytes=max_out + 1)]
        await put(key, D.dumps(fc).encode(), 'application/geo+json')
        clean_keys.append(key)
        for p in ch:
            p['row']['cleanFileKey'] = key
    if attention:
        fc = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'geometry': p['geojson'], 'properties': dict(p['props'], plotId=p['plotId'], _eudr_errors=p['errors'])}
            for p in attention]}
        await put('NEEDS_ATTENTION.geojson', D.dumps(fc).encode(), 'application/geo+json')
    dds = D.build_dds(clean, cfg) if clean else []
    await put('DDS_DRAFT.json', json.dumps(dds, indent=1, ensure_ascii=False).encode(), 'application/json')
    fix_log = [{k: p[k] for k in ('plotId', 'geometryStatus', 'fixes', 'warnings', 'errors')} for p in plots]
    await put('FIX_LOG.json', json.dumps(fix_log, indent=1, ensure_ascii=False).encode(), 'application/json')

    flags = {f: sum(r['riskFlag'] == f for r in rows) for f in ('likely_compliant', 'needs_review', 'high_risk', 'not_screened')}
    summary = {
        'generatedAt': generated + 'Z', 'version': VERSION, 'plotsFound': len(raws) + skipped, 'plotsProcessed': len(rows),
        'plotsCharged': charged, 'plotsSkippedByCap': skipped, 'plotsSkippedTimeLimit': timed_out,
        'screeningSourceDown': (screener.down if screener is not None else None), 'inputProblems': problems,
        'plotsNotScreenedMapError': sum(r.get('screeningStatus') == 'error' for r in rows),
        'forest2020Sources': ({SRC_JRC: screener.source_counts[SRC_JRC], SRC_FALLBACK: screener.source_counts[SRC_FALLBACK]}
                              if screener is not None else None),
        'jrcUnavailable': (screener.jrc_down if screener is not None else None),
        'geometry': {s: sum(p['geometryStatus'] == s for p in done) for s in ('ok', 'fixed', 'needs_attention')},
        'risk': flags, 'cleanFiles': clean_keys, 'cleanPlots': len(clean), 'needsAttentionPlots': len(attention),
        'cleanFileValidation': validation or 'passed', 'ddsDraftParts': len(dds), 'pdfCount': len(pdf_keys),
        'dataSources': {'forest2020': {'name': JRC_NAME, 'url': JRC_URL},
                        'forest2020Fallback': {'name': FALLBACK_NAME, 'baseUrl': HANSEN_BASE,
                                               'usedWhen': 'the JRC server is slow or unreachable; see forest2020Source per plot'},
                        'lossAfter2020': {'name': HANSEN_NAME, 'version': HANSEN_VERSION, 'baseUrl': HANSEN_BASE, 'lastYear': HANSEN_LAST_YEAR}},
        'thresholds': th, 'seconds': round(time.time() - t_start, 2),
        'disclaimer': 'Screening evidence only, not legal advice. The operator remains responsible for the EUDR due-diligence conclusion.',
    }
    if cfg.get('makeZip', True):
        summary['zipKey'] = 'PACKAGE.zip'
    links = {}
    for k in ([*clean_keys, 'NEEDS_ATTENTION.geojson' if attention else None, 'DDS_DRAFT.json',
               'FIX_LOG.json', 'SUMMARY.json', summary.get('zipKey')]):
        if k and (u := await _link(url, k)):
            links[k] = u
    if links:
        summary['links'] = links
    rows_sorted = [p['row'] for p in plots if p.get('row')]
    for r in rows_sorted:
        await push(r)
    await put('SUMMARY.json', json.dumps(summary, indent=1).encode(), 'application/json')
    if cfg.get('makeZip', True):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for k, (data, _) in files.items():
                z.writestr(('evidence/' if k.endswith('.pdf') else '') + k, data)
            z.writestr('PLOTS.json', json.dumps(rows_sorted, indent=1, ensure_ascii=False))
            z.writestr('README.txt', ZIP_README)
        await _put('PACKAGE.zip', buf.getvalue(), 'application/zip')
    return summary


ZIP_README = '''EUDR due-diligence package (LatentLark EUDR Due-Diligence Package Builder)

EUDR_CLEAN*.geojson   geolocation file(s) repaired to the EUDR Information System GeoJSON rules
NEEDS_ATTENTION.geojson  plots that could not be fixed safely (see _eudr_errors), excluded from the clean file
DDS_DRAFT.json        draft shaped like the EUDR Operator API V4 SubmitDdsRequest (not submitted; see operatorMustFill)
FIX_LOG.json          every change, warning and error per plot
PLOTS.json            one record per plot with the screening numbers and risk flag
evidence/*.pdf        one evidence PDF per plot (map, numbers, sources, fix log)
SUMMARY.json          run totals, data sources and versions

Screening evidence only, not legal advice. The operator remains responsible for the due-diligence conclusion.
'''


def make_row(plot: dict, sr, flag: str, reasons: list[str]) -> dict:
    row: dict[str, Any] = {
        'plotId': plot['plotId'], 'source': plot.get('sourceFormat'), 'sourceRow': plot.get('sourceRow'),
        'geometryStatus': plot['geometryStatus'], 'riskFlag': flag, 'riskReasons': reasons,
        'producerName': plot['props'].get('ProducerName'), 'producerCountry': plot.get('producerCountry'),
        'productionPlace': plot['props'].get('ProductionPlace'), 'geometryType': plot.get('geometryType'),
        'vertexCount': plot.get('vertexCount'), 'areaHa': plot.get('areaHa'), 'centroid': plot.get('centroid'),
        'bbox': plot.get('bbox'), 'sourceDecimals': plot.get('sourceDecimals'),
        'fixes': plot['fixes'], 'warnings': plot['warnings'], 'errors': plot['errors'], 'fixCount': len(plot['fixes']),
        'hansenLastYear': HANSEN_LAST_YEAR,
    }
    if sr is not None:
        row.update({k: getattr(sr, k) for k in ('forest2020Ha', 'forest2020Pct', 'lossPost2020Ha', 'lossPost2020Pct',
                                                'forestLossPost2020Ha', 'forestLossPost2020Pct', 'lossPre2021Ha', 'lossByYear',
                                                'analysedHa', 'gridResolutionM', 'hansenTiles', 'forest2020Source', 'jrcError')})
        row.update({'screeningStatus': sr.status, 'screeningError': sr.error, 'screenedAt': sr.readAt, 'screeningSeconds': sr.seconds})
    else:
        row['screeningStatus'] = 'skipped'
    row['cleanFileKey'] = None
    return row


