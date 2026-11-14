# EUDR Due-Diligence Package Builder: GeoJSON + Deforestation PDF

**Turn a messy supplier plot file into an EUDR filing package in one run.** Upload the CSV, KML or GeoJSON your cooperative or supplier sent. You get back a geolocation file repaired to the EU Information System rules, every plot screened against the EU's 2020 forest map and post-2020 tree-cover loss, **one evidence PDF per plot**, and a **DDS draft** shaped for the TRACES Operator API.

Built for EU importers, traders, exporters, cooperatives and compliance consultants handling **cattle, cocoa, coffee, oil palm, rubber, soya and wood**. Large and medium operators must comply from **30 December 2026**; micro and small operators from 30 June 2027.

- 🛠️ **Fixes the file:** open rings, self-intersections ("bow-ties"), holes, clockwise rings, swapped lat/lon, altitude values, more than 6 decimals, duplicate points, wrong property names, country names instead of ISO codes, area as text.
- 🌳 **Screens every plot:** % forest on 31 Dec 2020 (EC JRC GFC2020 V4) and hectares of tree-cover loss in 2021-2025 (Hansen/UMD GFC v1.13), with a clear flag: `likely_compliant`, `needs_review` or `high_risk`.
- 📄 **Evidence you can file:** a one-page PDF per plot with a map, the numbers, data sources with versions, read timestamps and the full fix log.
- 📦 **Ready to submit:** `EUDR_CLEAN.geojson` (re-validated, split under 25 MB / 1,000 plots), `DDS_DRAFT.json` with base64 geometry per producer and a list of what you still have to fill in, and everything zipped.
- 🔑 **No API keys, no GIS software:** the maps are read straight from the public JRC and UMD files.
- 💸 **Pay per plot:** $0.03 per plot processed. Plots with no usable geometry are free.

## Quick start (about 1 minute)

**Try it now:** the form is prefilled with one demo plot, an 81 ha cattle plot in Rondonia (Brazil) with an open ring and supplier-style column names (`producer`, `country`, `farm`, `area`). Click **Start**. In under a minute you get:

- 1 dataset row: geometry `fixed`, risk flag `high_risk` (about 2 ha of 2020 forest lost in 2022),
- `plot-BR-DEMO-1.pdf` with the map and fix log,
- `EUDR_CLEAN.geojson`, `DDS_DRAFT.json`, `FIX_LOG.json`, `SUMMARY.json` and `PACKAGE.zip`.

Cost: **$0.03** (one plot).

**With your own file:**

1. Upload the supplier file in **Supplier plot file** (or paste a link, or paste the text).
2. Pick the **commodity** (cattle allows point plots over 4 ha).
3. Optional: fill the shipment fields (activity type, HS code, net weight...) so the DDS draft is more complete.
4. Set **Max plots to process** as a cost cap, then click **Start**.
5. Open the **Output** tab: the plots table, the clean GeoJSON, the DDS draft and the ZIP.

## What goes in

| Format | What is read |
|---|---|
| CSV | `latitude` + `longitude` columns (one row per point, or several rows with the same plot ID for a polygon), or a `wkt` / `geometry` column. Comma, semicolon or tab separated. Decimal commas and degrees-minutes-seconds are understood. |
| KML / KMZ | Placemarks with Point, Polygon (with holes), closed LineString or MultiGeometry. Placemark name becomes `ProductionPlace`; ExtendedData is read. |
| GeoJSON | FeatureCollection, Feature or bare geometry. 3D coordinates are fine. |

Column names are mapped automatically: `farmer` / `producer` / `grower` → `ProducerName`; `country` / `country_code` → `ProducerCountry`; `farm` / `village` / `name` → `ProductionPlace`; `area` / `hectares` / `area_m2` / `acres` → `Area` in hectares; `plot_id` / `id` / `parcel_id` → plot ID. Coordinates in metres (UTM) are converted when you give the **Source coordinate system** (for example `EPSG:32630`).

## What the geometry repair does

Rules from the EUDR Information System *GeoJSON File Description* (updated 17 Aug 2026) and the Operator API V4 validation rules.

| Problem | What happens |
|---|---|
| Ring not closed | First point repeated at the end (logged) |
| Self-intersection / figure eight | Rebuilt as a valid shape; area before/after logged |
| Hole in a polygon (not accepted by the EU system) | Opened to the outer edge with a 0.5 m channel, so the hole stays excluded |
| Clockwise outer ring | Turned counter-clockwise (RFC 7946) |
| Latitude first | Swapped when a value is outside ±90, or when only the swapped version falls inside the declared country |
| More than 6 decimals / points that merge after rounding | Rounded to 6 decimals, duplicates removed, re-validated |
| Fewer than 6 decimals in the source | Warning: precision cannot be invented, ask for a better survey |
| Altitude (3rd value) | Dropped |
| Point with Area over 4 ha | **Error** (needs a polygon, cattle excepted): kept out of the clean file, listed in `NEEDS_ATTENTION.geojson` |
| Point without Area | Warning: the EU system assumes 4 ha |
| Open line (GPS track), 0,0, zero-area shape, bad country code | **Error**, never guessed |
| Duplicate plots | Second copy removed from the clean file (option) |
| Overlapping plots, declared area ≠ mapped area | Warning |
| File over 25 MB or over 1,000 plots | Split into `EUDR_CLEAN_part1.geojson`, `_part2`, ... |

The clean file is re-checked by an independent validator before it is saved (`cleanFileValidation` in `SUMMARY.json`).

## How the deforestation screening works

| Layer | Source | Resolution | Used for |
|---|---|---|---|
| Forest on 31 Dec 2020 | EC JRC Global Forest Cover 2020, **V4** (single global Cloud-Optimized GeoTIFF on the JRC open-data server) | ~10 m | % and ha of the plot that was forest at the EUDR cut-off |
| Tree-cover loss 2021-2025 | Hansen/UMD/Google/USGS/NASA **Global Forest Change GFC-2025-v1.13**, `lossyear` | ~30 m | ha of loss after the cut-off, per year |
| Forest on 31 Dec 2020, **backup 1** | The same JRC GFC2020 map (release v2020.3) as hosted by **Global Forest Watch**, web-map tiles at zoom 14 | ~10 m | Plots where the JRC server is slow or unreachable |
| Forest on 31 Dec 2020, **backup 2** | Hansen/UMD GFC-2025-v1.13 `treecover2000` at 10% canopy or more (the FAO forest threshold) minus any loss in 2001-2020 | ~30 m | Only if the JRC server and the GFW copy are both unreachable |

**Backup forest maps.** The JRC open-data server is sometimes slow or unreachable. When that happens, the plot is still screened: first with the Global Forest Watch copy of the JRC map (same method, an earlier release; on a 120-plot test it gave the same flag as JRC V4 for 114 plots), and only if that also fails with the Hansen tree-cover layer. The Hansen layer counts tree crops such as cocoa and coffee as forest, so it flags many more plots for review in agroforestry regions. Every row says which map was used in `forest2020Source` (`jrc_gfc2020_v4`, `jrc_gfc2020_gfw_mirror` or `hansen_treecover2000_minus_loss`), with the reason in `jrcError` / `gfwError`. The PDF and `SUMMARY.json` (`forest2020Sources`) say so too. For filing, re-run backup-screened plots later to get the JRC V4 figure. A plot is `not_screened` and free only if every source fails.

Both are read with HTTP range requests, so only a few hundred kB around each plot are downloaded. No account or key. Points are screened as a circle with the declared area (4 ha if missing), and this is stated in the PDF.

**Flags** (thresholds are inputs):

- `high_risk`: forest in 2020 **and** loss after 2020 on at least 0.5 ha or 2% of the plot.
- `needs_review`: any smaller overlap of 2020 forest and post-2020 loss, any post-2020 loss outside the 2020 forest map, at least 10% of the plot forested in 2020, unresolved geometry errors, or partial data.
- `likely_compliant`: none of the above.
- `not_screened`: no usable geometry, or none of the forest maps could be read (not charged).

## What comes out

**Dataset, one row per plot** (table view "Plots"):

```json
{
  "plotId": "BR-DEMO-1",
  "riskFlag": "high_risk",
  "geometryStatus": "fixed",
  "producerName": "Example Ranch (demo)",
  "producerCountry": "BR",
  "geometryType": "Polygon",
  "areaHa": 81.0424,
  "forest2020Pct": 62.09,
  "forest2020Source": "jrc_gfc2020_v4",
  "lossPost2020Ha": 2.3627,
  "forestLossPost2020Ha": 1.9562,
  "lossByYear": {"2022": 2.3627},
  "fixes": ["renamed property 'producer' to 'ProducerName'", "converted country 'Brazil' to ISO code 'BR'", "closed the open outer ring (first point repeated at the end)"],
  "riskReasons": ["1.96 ha (2.4%) of the plot was forest in 2020 (JRC) and shows tree-cover loss in 2021-2025 (Hansen)"],
  "pdfUrl": "https://api.apify.com/v2/key-value-stores/.../records/plot-BR-DEMO-1.pdf?signature=..."
}
```

**Key-value store files:**

| Key | What it is |
|---|---|
| `EUDR_CLEAN.geojson` (or `_partN`) | Upload this to the EU Information System. Only `ProducerName`, `ProducerCountry`, `ProductionPlace`, `Area`. |
| `NEEDS_ATTENTION.geojson` | Plots that need the supplier, with `_eudr_errors` |
| `DDS_DRAFT.json` | Draft shaped like Operator API V4 `SubmitDdsRequest`: producers grouped by name + country with base64 GeoJSON, plus `operatorMustFill` |
| `plot-<id>.pdf` | Evidence PDF per plot |
| `FIX_LOG.json`, `SUMMARY.json`, `PACKAGE.zip` | Fix log, run totals with data versions (plus signed download `links` for every file), everything zipped |

The `pdfUrl` in each row and the `links` in `SUMMARY.json` are signed links: they open in a browser or in n8n/Zapier without your API token, so you can share them with an auditor.

**What the DDS draft cannot know** (always listed in `operatorMustFill`): operator role and identity (EORI), activity type, goods description, net weight, HS code confirmation, species for wood, whether geolocation is confidential, and your own risk-assessment and legality conclusions. The draft is never submitted; the Operator API needs your own TRACES credentials.

## Pricing

| Event | Price |
|---|---|
| Plot processed (`plot-processed`: repaired, screened, PDF) | **$0.03** |

Platform usage is included in the price. Plots without usable geometry, plots whose map data could not be read, and plots over your **Max plots** cap are not charged. With screening turned off (fix-only), each plot with usable geometry is still one plot processed. Examples: 50-plot cooperative file **$1.50**; 1,000 plots **$30**. Set a maximum cost per run in the run options and the Actor stops cleanly at that limit.

## Use it from n8n, Make, Zapier or code

Send the file link or the pasted text. Python:

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_APIFY_TOKEN>")
run = client.actor("latentlark/eudr-due-diligence-package-builder").call(run_input={
    "plotFile": "https://example.com/supplier-plots.csv",
    "commodity": "cocoa",
    "activityType": "IMPORT",
    "maxPlots": 200,
})
for row in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(row["plotId"], row["riskFlag"], row["forestLossPost2020Ha"])
```

## How does it compare?

| | This Actor | EUDR Plot File Fixer | EUDR Parcel Screener |
|---|---|---|---|
| Repairs the plot file to EU rules | ✅ | ✅ (also zipped Shapefile) | ❌ |
| Deforestation screening | ✅ JRC GFC2020 V4 + Hansen loss 2021-2025 | ❌ | ✅ JRC GFC2020 + JRC TMF |
| Evidence PDF per plot | ✅ | ❌ | ❌ |
| DDS draft for the Operator API | ✅ | ❌ | ❌ |
| Price | $0.03 / plot | $0.01 / plot | $0.05 / result + platform usage |

If you only need the file fixed, the fixer is cheaper. If you need the whole package for the filing (fixed file + screening + PDF evidence + DDS draft), this does it in one run.

## Limitations (please read)

- **Screening evidence, not legal advice.** The JRC map is a non-mandatory, non-exclusive, not legally binding source under the EUDR. A `likely_compliant` flag is not a compliance decision; the operator remains responsible under Regulation (EU) 2023/1115.
- Hansen loss is **stand-replacing tree-cover loss**: it includes plantation harvests and fires, and can miss selective logging, degradation and clearings smaller than about 30 m. Loss in 2026 is not in the data yet.
- 10 m and 30 m pixels make plot edges approximate; very small plots (under about 0.1 ha) are screened from a handful of pixels.
- Point plots are screened as circles, not their real boundary.
- Lat/lon swap detection uses approximate country boxes; it only swaps when the result is unambiguous and always logs it.
- Not covered: legality of production, country benchmarking tier, degradation of primary forest, protected areas, indigenous land.

## FAQ

**The run finished with no rows. Why?** No plots were found in the input: check that the file link opens without a login (Google Drive and Dropbox share links must be direct-download links), that a CSV has `latitude`/`longitude` or `wkt` columns, and look at `inputProblems` in `SUMMARY.json`.

**Why did a plot get no PDF?** It had no usable geometry (for example an open GPS track). See `errors` in its row.

**Why is the area different from my supplier's?** The Actor computes the geodesic area of the repaired polygon and writes that into `Area`; a difference above 10% is flagged.

**Is my data shared?** Files stay in your own Apify storage under your retention settings. Only public map files are read: JRC, UMD (from Google Cloud Storage) and, as a backup, the Global Forest Watch copy of the JRC map. Your plot coordinates are not sent anywhere: the Actor only requests the map tiles around them.

**Can the GeoJSON go straight into TRACES?** That is what it is built for, but always check the result in the EU system's own import screen before filing.

---

**Related LatentLark Actors:** send the plots table to a spreadsheet with [Google Sheets Export & Import (No OAuth)](https://apify.com/latentlark/sheets-export).

Made by **LatentLark**. More tools: [apify.com/latentlark](https://apify.com/latentlark).
