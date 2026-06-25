# NDVI Explorer

Fetch, compute, and narrate **NDVI** (Normalized Difference Vegetation Index)
for any region on Earth. No authentication required.

```
NDVI = (NIR − Red) / (NIR + Red)
```

| NDVI range | Land cover |
|---|---|
| < 0.0 | Water, clouds, shadows |
| 0.0 – 0.2 | Bare soil, rock, urban areas |
| 0.2 – 0.4 | Sparse or stressed vegetation |
| 0.4 – 0.6 | Moderate vegetation (crops, grassland) |
| 0.6 – 1.0 | Dense, healthy vegetation (forest, tropical canopy) |

---

## Area of interest

Two ways to define where to look — pick one:

| Flag | How it works |
|---|---|
| `--city "São Paulo, Brazil"` | Fetches the **real city boundary polygon** from OpenStreetMap and clips the satellite data to that shape. No square edges. |
| `--bbox LON_MIN LAT_MIN LON_MAX LAT_MAX` | Uses a manual bounding box. Pixels outside the rectangle are not masked. |

City polygons come from **OpenStreetMap via the Nominatim API** (free, no key
required, data © OpenStreetMap contributors under ODbL).

---

## Supported satellite backends

| `--source` | Dataset | Approx. resolution | Auth required |
|---|---|---|---|
| `sentinel2` | Sentinel-2 L2A via [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) | ~60 m | No |
| `landsat` | Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer | ~120 m | No |

Both backends stream Cloud-Optimized GeoTIFFs (COGs) — only the pixels inside
your area of interest are downloaded.

---

## Installation

```bash
pip3 install -r requirements.txt
```

> The script is also directly executable: `chmod +x ndvi_explorer.py` then
> `./ndvi_explorer.py --city "Nairobi, Kenya" ...`

**Dependencies:**

| Package | Version | Role |
|---|---|---|
| `numpy` | ≥ 1.24 | Array maths |
| `matplotlib` | ≥ 3.8 | Figures |
| `requests` | ≥ 2.28 | Nominatim HTTP requests |
| `shapely` | ≥ 2.0 | City polygon geometry |
| `pystac-client` | ≥ 0.7 | STAC catalog search |
| `planetary-computer` | ≥ 1.0 | Signs Planetary Computer asset URLs |
| `stackstac` | ≥ 0.5 | Reprojects + clips COG bands to bbox |
| `rioxarray` | ≥ 0.15 | Raster clipping to polygon |

---

## Usage

### By city name (recommended)

```bash
python3 ndvi_explorer.py \
    --source sentinel2 \
    --city "São Paulo, Brazil" \
    --start 2024-06-01 --end 2024-08-31
```

```bash
python3 ndvi_explorer.py \
    --source landsat \
    --city "Nairobi, Kenya" \
    --start 2023-07-01 --end 2023-09-30
```

```bash
python3 ndvi_explorer.py \
    --source sentinel2 \
    --city "Amsterdam, Netherlands" \
    --start 2024-05-01 --end 2024-07-31
```

### By bounding box

```bash
python3 ndvi_explorer.py \
    --source landsat \
    --bbox -55.0 -12.5 -54.5 -12.0 \
    --start 2023-07-01 --end 2023-09-30
```

### Save figure to a specific path

```bash
python3 ndvi_explorer.py \
    --source sentinel2 \
    --city "Tokyo, Japan" \
    --start 2024-04-01 --end 2024-06-30 \
    --out tokyo_spring.png
```

### All options

```
--source      sentinel2 | landsat              (default: sentinel2)

Area of interest (mutually exclusive):
--city        city/place name                  (fetches OSM polygon)
--bbox        LON_MIN LAT_MIN LON_MAX LAT_MAX  (manual bounding box)

--start       YYYY-MM-DD                       (default: 2024-06-01)
--end         YYYY-MM-DD                       (default: 2024-08-31)
--max-cloud   maximum cloud cover % to accept  (default: 20)
--out         output figure path               (default: ndvi_<source>_<date>[_<city>].png)
```

---

## Output

Every run produces two outputs.

### 1. Terminal narrative

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NDVI Story  ·  SENTINEL2  ·  2024-08-03
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Location      : São Paulo
  Valid pixels  : 61,204

  NDVI statistics
  ─────────────────────────────────────────────────
  Median        :  +0.074
  10th pct      :  +0.017
  90th pct      :  +0.331

  Land-cover breakdown (NDVI-based estimate)
  ─────────────────────────────────────────────────
  Dense vegetation         0.1 %
  Moderate vegetation      5.7 %
  Sparse vegetation       14.3 %
  Bare soil / urban       75.5 %
  Water / shadows          4.4 %

  Interpretation
  ─────────────────────────────────────────────────
  Mostly non-vegetated: water bodies, bare soil, or built-up surfaces dominate.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 2. Four-panel figure (PNG)

| Panel | Content |
|---|---|
| **True Colour** | Percentile-stretched RGB composite |
| **NDVI** | Continuous heatmap (red → yellow → green) with colorbar |
| **Classification** | 5-class land-cover map with per-class percentages |
| **Distribution** | Pixel histogram with median line |

When `--city` is used, areas outside the city boundary are masked (transparent /
NaN), so the panels show the city shape rather than a rectangle.

The figure is saved as `ndvi_<source>_<date>_<city>.png` (city mode) or
`ndvi_<source>_<date>.png` (bbox mode) unless `--out` is set.

---

## How it works

```
--city / --bbox
      │
      ▼ (--city only)
  Nominatim API → OSM city polygon
  polygon.bounds → bbox for STAC search
      │
      ▼
  Planetary Computer STAC search
  (sentinel-2-l2a or landsat-c2-l2)
      │
      ▼
  Least-cloudy scene selected
  (respects --max-cloud)
      │
      ▼
  NIR + Red + RGB bands fetched
  (COG windows clipped to bbox, EPSG:4326)
      │
      ▼ (--city only)
  rioxarray.clip(city_polygon)
  → pixels outside city → NaN
      │
      ▼
  NDVI = (NIR − Red) / (NIR + Red)
      │
      ├──▶ Narrative printed to stdout
      └──▶ 4-panel PNG saved to disk
```

---

## Tips

**City not found?**
- Be more specific: `"Paris, France"` instead of `"Paris"`.
- Include the country to avoid ambiguity: `"Cambridge, UK"` vs `"Cambridge, MA, USA"`.
- Some small towns may only return a point centroid — use `--bbox` in that case.

**No scenes found?**
- Widen the date range (`--start` / `--end`).
- Raise the cloud threshold (`--max-cloud 50`).
- For `--bbox`: check order is `LON_MIN LAT_MIN LON_MAX LAT_MAX`.

**Slow download?**
- Only pixels inside the bbox are streamed; smaller areas are faster.
- Sentinel-2 is slower than Landsat (higher resolution, more data).

---

## Data sources

- **Sentinel-2**: ESA Copernicus Programme — [sentinel.esa.int](https://sentinel.esa.int)
- **Landsat 8/9**: USGS / NASA — [landsat.gsfc.nasa.gov](https://landsat.gsfc.nasa.gov)
- **Microsoft Planetary Computer**: [planetarycomputer.microsoft.com](https://planetarycomputer.microsoft.com)
- **OpenStreetMap / Nominatim**: [nominatim.openstreetmap.org](https://nominatim.openstreetmap.org) — © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/)

---

## License

MIT
