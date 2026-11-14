"""Evidence PDF per plot (reportlab) with a raster map thumbnail or a vector outline fallback."""
from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from reportlab.graphics.shapes import Drawing, Polygon as RLPolygon, Circle, Rect
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .screening import HANSEN_BASE, HANSEN_NAME, HANSEN_VERSION, JRC_NAME, JRC_URL

FLAG_COLORS = {'likely_compliant': '#1a7f37', 'needs_review': '#b35900', 'high_risk': '#c62828', 'not_screened': '#555555'}
FLAG_TEXT = {'likely_compliant': 'LIKELY COMPLIANT (screening)', 'needs_review': 'NEEDS REVIEW',
             'high_risk': 'HIGH RISK', 'not_screened': 'NOT SCREENED'}
DISCLAIMER = ('Screening evidence only, not legal advice and not a compliance decision. The JRC GFC2020 map is a '
              'non-mandatory, non-exclusive and not legally binding source under the EUDR. Hansen tree-cover loss is '
              'stand-replacing loss (it includes plantation harvest and can miss degradation or small clearings). '
              'The operator remains responsible for the due-diligence conclusion under Regulation (EU) 2023/1115.')


def _polys(shape) -> list:
    if shape is None:
        return []
    if shape.geom_type == 'Polygon':
        return [shape]
    if shape.geom_type in ('MultiPolygon', 'GeometryCollection'):
        return [g for g in shape.geoms if g.geom_type == 'Polygon']
    return []


def thumbnail_png(sr, shape, screen_shape, size: int = 480) -> bytes | None:
    if sr is None or sr.forest is None:
        return None
    f, loss = sr.forest, sr.loss
    h, w = f.shape
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (242, 239, 230)
    img[f] = (77, 146, 33)
    img[(loss > 0) & (loss <= 20)] = (190, 170, 140)
    img[loss > 20] = (227, 26, 28)
    scale = max(1, size // max(h, w))
    pil = Image.fromarray(img).resize((w * scale, h * scale), Image.NEAREST)
    d = ImageDraw.Draw(pil)
    t = sr.transform

    def px(x, y):
        return ((x - t.c) / t.a * scale, (y - t.f) / t.e * scale)

    outline = _polys(screen_shape) or _polys(shape)
    for p in outline:
        d.line([px(x, y) for x, y in p.exterior.coords], fill=(20, 60, 200), width=max(2, scale))
    if shape is not None and shape.geom_type in ('Point', 'MultiPoint'):
        for pt in ([shape] if shape.geom_type == 'Point' else shape.geoms):
            cx, cy = px(pt.x, pt.y)
            d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(20, 60, 200))
    buf = io.BytesIO()
    pil.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def outline_drawing(shape, screen_shape, size: float = 70 * mm) -> Drawing:
    d = Drawing(size, size)
    d.add(Rect(0, 0, size, size, strokeColor=colors.grey, fillColor=colors.whitesmoke))
    geom = screen_shape if screen_shape is not None else shape
    if geom is None:
        return d
    minx, miny, maxx, maxy = geom.bounds
    span = max(maxx - minx, maxy - miny) or 1e-6
    s = (size - 10) / span

    def tr(x, y):
        return (5 + (x - minx) * s, 5 + (y - miny) * s)

    for p in _polys(geom):
        pts = [c for xy in p.exterior.coords for c in tr(*xy)]
        d.add(RLPolygon(pts, strokeColor=colors.HexColor('#1f3cc8'), fillColor=colors.HexColor('#dfe7ff'), strokeWidth=1.2))
    if shape is not None and shape.geom_type in ('Point', 'MultiPoint'):
        for pt in ([shape] if shape.geom_type == 'Point' else shape.geoms):
            x, y = tr(pt.x, pt.y)
            d.add(Circle(x, y, 2.5, fillColor=colors.HexColor('#1f3cc8'), strokeColor=None))
    return d


def build_pdf(row: dict[str, Any], plot: dict, sr, run_meta: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"EUDR plot evidence {row['plotId']}", author='LatentLark EUDR Package Builder')
    ss = getSampleStyleSheet()
    small = ParagraphStyle('small', parent=ss['BodyText'], fontSize=8, leading=10)
    body = ParagraphStyle('body', parent=ss['BodyText'], fontSize=9, leading=11.5)
    flag = row.get('riskFlag', 'not_screened')
    el: list = [
        Paragraph(f"EUDR plot screening evidence: <b>{_esc(row['plotId'])}</b>", ss['Title']),
        Paragraph(f"<font color='{FLAG_COLORS[flag]}'><b>{FLAG_TEXT[flag]}</b></font> &nbsp; Geometry: <b>{row['geometryStatus']}</b>"
                  f" &nbsp; Generated {run_meta['generatedAt']} UTC", body),
        Spacer(1, 4 * mm),
    ]
    png = thumbnail_png(sr, plot.get('shape'), plot.get('screenShape'))
    if png:
        img = RLImage(io.BytesIO(png), width=78 * mm, height=78 * mm, kind='proportional')
        legend = Paragraph("Map (analysis grid ~%s m): <font color='#4d9221'><b>green</b></font> = forest 2020 (JRC), "
                           "<font color='#e31a1c'><b>red</b></font> = tree-cover loss 2021-%s (Hansen), "
                           "<font color='#9b8460'><b>tan</b></font> = loss 2001-2020, "
                           "<font color='#1f3cc8'><b>blue</b></font> = plot boundary%s." % (
                               row.get('gridResolutionM') or '?', row.get('hansenLastYear', ''),
                               ' (circle approximation around the point)' if row.get('geometryType') in ('Point', 'MultiPoint') else ''), small)
        map_cell = [img, legend]
    else:
        map_cell = [outline_drawing(plot.get('shape'), plot.get('screenShape')),
                    Paragraph('Vector outline (no raster thumbnail: screening was not available).', small)]
    facts = [
        ['Production place', _esc(row.get('productionPlace') or '')], ['Producer', _esc(row.get('producerName') or '-')],
        ['Country', row.get('producerCountry') or '-'], ['Geometry', f"{row.get('geometryType')} ({row.get('vertexCount')} vertices)"],
        ['Area (ha)', _num(row.get('areaHa'))], ['Centroid (lon, lat)', ', '.join(f'{v:.6f}' for v in row.get('centroid') or []) or '-'],
        ['Forest 2020 (JRC)', f"{_num(row.get('forest2020Ha'))} ha / {_num(row.get('forest2020Pct'))}%"],
        ['Loss 2021+ (Hansen)', f"{_num(row.get('lossPost2020Ha'))} ha / {_num(row.get('lossPost2020Pct'))}%"],
        ['Forest-2020 lost 2021+', f"{_num(row.get('forestLossPost2020Ha'))} ha / {_num(row.get('forestLossPost2020Pct'))}%"],
        ['Loss 2001-2020 (info)', f"{_num(row.get('lossPre2021Ha'))} ha"],
        ['Loss by year', ', '.join(f'{k}: {v} ha' for k, v in (row.get('lossByYear') or {}).items()) or 'none'],
    ]
    ft = Table([[Paragraph(f'<b>{a}</b>', small), Paragraph(str(b), small)] for a, b in facts], colWidths=[34 * mm, 58 * mm])
    ft.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.3, colors.lightgrey), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    top = Table([[map_cell, ft]], colWidths=[84 * mm, 94 * mm])
    top.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    el += [top, Spacer(1, 3 * mm), Paragraph('<b>Screening result</b>', ss['Heading4'])]
    el += [Paragraph('• ' + _esc(r), body) for r in row.get('riskReasons') or []]
    el += [Spacer(1, 2 * mm), Paragraph('<b>Geometry fix log</b>', ss['Heading4'])]
    for label, items in (('Fixed', row.get('fixes')), ('Warning', row.get('warnings')), ('Error', row.get('errors'))):
        for it in items or []:
            el.append(Paragraph(f'<b>{label}:</b> {_esc(it)}', body))
    if not any(row.get(k) for k in ('fixes', 'warnings', 'errors')):
        el.append(Paragraph('No changes needed.', body))
    el += [Spacer(1, 2 * mm), Paragraph('<b>Data sources and method</b>', ss['Heading4'])]
    src = [
        f'{JRC_NAME}. File: {JRC_URL} (read {row.get("screenedAt") or "-"} UTC by HTTP range request). Value 1 = forest on 31 Dec 2020. '
        'Citation: European Commission, Joint Research Centre, Global map of forest cover 2020. Free, Copernicus licence.',
        f'{HANSEN_NAME}. Tiles {", ".join(row.get("hansenTiles") or []) or "-"} from {HANSEN_BASE}. Values 21-{row.get("hansenLastYear", 25) - 2000} = loss in 2021-{row.get("hansenLastYear", "")}. '
        'Citation: Hansen et al. (2013) Science 342:850-853. CC BY 4.0.',
        'Method: both rasters are sampled on one grid (JRC ~10 m pixels; Hansen 30 m pixels sampled nearest-neighbour at the same centres). '
        'Pixel areas are computed on the sphere; plot pixels are those whose centres fall inside the boundary.',
        f"Geometry rules: EUDR Information System GeoJSON File Description (updated 17 Aug 2026). Thresholds: high risk if forest-2020 lost "
        f"after 2020 >= {run_meta['thresholds']['highRiskMinHa']} ha or >= {run_meta['thresholds']['highRiskPct']}% of the plot; "
        f"review if any post-2020 loss or forest 2020 >= {run_meta['thresholds']['reviewForestPct']}%.",
        f"Tool: LatentLark EUDR Due-Diligence Package Builder v{run_meta['version']}, run {run_meta.get('runId') or 'local'}.",
    ]
    el += [Paragraph(_esc(s), small) for s in src]
    el += [Spacer(1, 3 * mm), Paragraph('<b>Important:</b> ' + _esc(DISCLAIMER), small)]
    doc.build(el)
    return buf.getvalue()


def _num(v) -> str:
    if v is None:
        return '-'
    return f'{v:,.4f}'.rstrip('0').rstrip('.') if isinstance(v, float) else str(v)


def _esc(s: Any) -> str:
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
