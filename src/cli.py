"""Local runner (no Apify needed): python -m src.cli FILE [FILE...] --out output/run1 [--no-screening]"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .package import build_package


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('--out', default='output/local')
    ap.add_argument('--no-screening', action='store_true')
    ap.add_argument('--no-pdf', action='store_true')
    ap.add_argument('--config', help='JSON file with extra input fields')
    a = ap.parse_args()
    out = Path(a.out)
    (out / 'kv').mkdir(parents=True, exist_ok=True)
    cfg = json.loads(Path(a.config).read_text()) if a.config else {}
    cfg.setdefault('runScreening', not a.no_screening)
    cfg.setdefault('generatePdf', not a.no_pdf)
    sources = [(Path(f).name, Path(f).read_bytes()) for f in a.files]
    rows: list[dict] = []

    async def put(key, data, ctype):
        (out / 'kv' / key).write_bytes(data)

    async def push(row):
        rows.append(row)

    async def charge(n):
        return True

    summary = asyncio.run(build_package(cfg, sources, put, push, charge, lambda k: str((out / 'kv' / k).resolve())))
    (out / 'dataset.json').write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()
