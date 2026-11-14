from __future__ import annotations

import time

from apify import Actor

from .fetch import fetch
from .package import EVENT, build_package


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}
        cfg['_runId'] = Actor.configuration.actor_run_id
        max_in = int(float(cfg.get('maxInputFileMb', 50)) * 1e6)
        sources: list[tuple[str, bytes]] = []
        urls = [u for u in (cfg.get('plotFileUrls') or []) if isinstance(u, str) and u.strip()]
        if cfg.get('plotFile'):
            urls.insert(0, cfg['plotFile'])
        for u in urls:
            try:
                sources.append(await fetch(u, max_in))
                Actor.log.info(f'Downloaded {sources[-1][0]} ({len(sources[-1][1]) / 1e3:.0f} kB)')
            except Exception as e:  # noqa: BLE001
                Actor.log.warning(f'Could not download {u[:120]}: {e}')
        if (cfg.get('plotsText') or '').strip():
            sources.append(('pasted-text', cfg['plotsText'].encode()))
        if not sources and not cfg.get('geojson'):
            msg = 'No plots given: add a file, a URL, pasted CSV/KML/GeoJSON text or a GeoJSON object.'
            await Actor.push_data({'status': 'input_error', 'error': msg, 'charged': False})  # free explanatory row
            await Actor.set_status_message(msg, is_terminal=True)
            return
        # Graceful time budget: stop starting new plots with enough margin left to write the package files.
        t_at = Actor.configuration.timeout_at
        if t_at is not None:
            total = max(1.0, t_at.timestamp() - time.time())
            cfg['_deadline'] = t_at.timestamp() - max(45.0, min(180.0, total * 0.15))

        kvs = await Actor.open_key_value_store()

        async def put(key: str, data: bytes, ctype: str) -> None:
            await kvs.set_value(key, data, content_type=ctype)

        async def push(row: dict) -> None:
            await Actor.push_data(row)

        async def charge(n: int) -> bool:
            res = await Actor.charge(EVENT, count=n)
            return not res.event_charge_limit_reached or res.charged_count >= n

        async def url(key: str) -> str | None:
            # Signed public URL on the platform (works for accounts with restricted storage access); None locally.
            if not Actor.is_at_home():
                return None
            try:
                return await kvs.get_public_url(key)
            except Exception:  # noqa: BLE001
                return None

        async def progress(msg: str) -> None:
            await Actor.set_status_message(msg)

        summary = await build_package(cfg, sources, put, push, charge, url, log=Actor.log.info, progress=progress)
        if not summary.get('plotsProcessed'):
            # Never finish with an empty dataset: explain why nothing was processed (free row).
            await Actor.push_data({'status': 'no_plots', 'charged': False, 'inputProblems': summary.get('inputProblems'),
                                   'error': 'No usable plots found in the input. Check the file format (GeoJSON, KML, CSV with '
                                            'lat/lon or WKT) and see inputProblems / FIX_LOG.json.'})
        if not summary.get('plotsProcessed'):
            await Actor.set_status_message('No usable plots found in the input (nothing charged): check the file format '
                                           '(GeoJSON, KML, CSV with lat/lon or WKT) and see the dataset row.', is_terminal=True)
            return
        await Actor.set_status_message(
            f"{summary['plotsProcessed']} plots: {summary['risk']['high_risk']} high risk, {summary['risk']['needs_review']} review, "
            f"{summary['risk']['likely_compliant']} likely compliant; {summary['cleanPlots']} in the clean GeoJSON"
            + (f"; {summary['plotsSkippedTimeLimit']} not processed (time budget, not charged)" if summary.get('plotsSkippedTimeLimit') else '')
            + (f"; {summary['plotsNotScreenedMapError']} not screened (forest maps unreachable, not charged)"
               if summary.get('plotsNotScreenedMapError') else '')
            + (f"; {(summary.get('forest2020Sources') or {}).get('jrc_gfc2020_gfw_mirror')} screened with the GFW copy of the JRC map "
               "(JRC server unavailable)" if (summary.get('forest2020Sources') or {}).get('jrc_gfc2020_gfw_mirror') else '')
            + (f"; {(summary.get('forest2020Sources') or {}).get('hansen_treecover2000_minus_loss')} screened with the Hansen "
               "fallback forest map (JRC maps unavailable)" if (summary.get('forest2020Sources') or {}).get('hansen_treecover2000_minus_loss') else ''),
            is_terminal=True)
