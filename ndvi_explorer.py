#!/usr/bin/env python3
"""
ndvi_explorer.py — NDVI Storytelling from Satellite Imagery
============================================================

Fetch, compute, and narrate NDVI (Normalized Difference Vegetation Index)
for any region on Earth using freely available satellite imagery.
No authentication required.

  sentinel2  Sentinel-2 L2A via Microsoft Planetary Computer  (~60 m, 2017–present)
  landsat    Landsat 8/9 C2L2 via Microsoft Planetary Computer (~120 m, 2013–present)

Area of interest — choose one:
  --city "São Paulo, Brazil"     city polygon from OpenStreetMap (recommended)
  --bbox LON_MIN LAT_MIN LON_MAX LAT_MAX   manual bounding box

What is NDVI?
-------------
NDVI = (NIR - Red) / (NIR + Red)

  < 0.0   Water, clouds, shadows
  0.0–0.2 Bare soil, rock, urban
  0.2–0.4 Sparse or stressed vegetation
  0.4–0.6 Moderate vegetation (crops, grasslands)
  0.6–1.0 Dense, healthy vegetation (forests, tropical canopy)

Quick start
-----------
  # By city name (clips to actual city boundary)
  python3 ndvi_explorer.py \\
      --source sentinel2 \\
      --city "São Paulo, Brazil" \\
      --start 2024-06-01 --end 2024-08-31

  # By bounding box
  python3 ndvi_explorer.py \\
      --source landsat \\
      --bbox -55.0 -12.5 -54.5 -12.0 \\
      --start 2023-07-01 --end 2023-09-30
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _require(module: str, pip_name: str | None = None):
    """Import a module, exiting with a helpful message if it is missing."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        pkg = pip_name or module
        sys.exit(
            f"\nMissing dependency: {module}\n"
            f"Install it with:  pip3 install {pkg}\n"
        )


def _normalize_rgb(
    r: np.ndarray, g: np.ndarray, b: np.ndarray, percentile: float = 2.0
) -> np.ndarray:
    """Percentile-stretch three bands into an [H, W, 3] float array in [0, 1]."""
    def stretch(arr: np.ndarray) -> np.ndarray:
        valid = arr[np.isfinite(arr) & (arr > 0)]
        if valid.size == 0:
            return np.zeros_like(arr, dtype=float)
        lo, hi = np.percentile(valid, [percentile, 100 - percentile])
        return np.clip((arr.astype(float) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)

    return np.dstack([stretch(r), stretch(g), stretch(b)])


# ---------------------------------------------------------------------------
# City polygon lookup — OpenStreetMap Nominatim
# ---------------------------------------------------------------------------

def city_to_polygon(city_name: str):
    """
    Query OpenStreetMap Nominatim for the boundary polygon of a city or place.
    Returns (shapely_polygon, bbox_list, short_display_name).

    No API key required. Rate limit: 1 req/s (we make exactly one request).
    Data: © OpenStreetMap contributors, ODbL licence.
    """
    import requests
    from shapely.geometry import shape

    print(f"  Looking up '{city_name}' on OpenStreetMap Nominatim …")
    params = {
        "q": city_name,
        "format": "geojson",
        "polygon_geojson": 1,   # return full boundary polygon
        "limit": 5,
    }
    headers = {"User-Agent": "ndvi-explorer/1.0"}

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        sys.exit(f"  Nominatim request failed: {exc}")

    features = resp.json().get("features", [])
    if not features:
        sys.exit(
            f"  No results found for '{city_name}'.\n"
            "  Try a more specific name, e.g. 'Paris, France' or 'Nairobi, Kenya'."
        )

    # Prefer results that carry a real polygon (not just a Point centroid)
    poly_features = [
        f for f in features
        if f["geometry"]["type"] in ("Polygon", "MultiPolygon")
    ]
    feat = poly_features[0] if poly_features else features[0]

    geom         = shape(feat["geometry"])
    display_name = feat["properties"].get("display_name", city_name)
    short_name   = display_name.split(",")[0].strip()
    bbox         = list(geom.bounds)   # [lon_min, lat_min, lon_max, lat_max]

    print(f"  Found    : {display_name}")
    print(f"  Type     : {geom.geom_type}")
    print(f"  BBox     : {[round(v, 4) for v in bbox]}")

    return geom, bbox, short_name


# ---------------------------------------------------------------------------
# Polygon clipping helper
# ---------------------------------------------------------------------------

def _clip_to_polygon(data, polygon) -> object:
    """
    Clip a stackstac xarray DataArray to a shapely polygon.
    The data must be in EPSG:4326 (which stackstac sets when epsg=4326 is passed).
    """
    import rioxarray  # noqa: F401 — registers the .rio accessor on xarray
    from shapely.geometry import mapping

    if data.rio.crs is None:
        data = data.rio.write_crs("EPSG:4326")

    return data.rio.clip(
        [mapping(polygon)],
        crs="EPSG:4326",
        drop=True,          # shrink spatial extent to polygon bounds
        all_touched=True,   # include edge pixels
    )


# ---------------------------------------------------------------------------
# NDVI core
# ---------------------------------------------------------------------------

def compute_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """NDVI = (NIR − Red) / (NIR + Red).  Returns values in [−1, 1]."""
    nir = nir.astype(float)
    red = red.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
    ndvi[(nir + red) == 0] = np.nan
    return ndvi


# NDVI class boundaries, labels, colours
_CLASSES = [
    ("Water / no data",    -1.00,  0.00, "#1a6aa8"),
    ("Bare soil / urban",   0.00,  0.20, "#c9a227"),
    ("Sparse vegetation",   0.20,  0.40, "#d4e157"),
    ("Moderate vegetation", 0.40,  0.60, "#66bb6a"),
    ("Dense vegetation",    0.60,  1.01, "#1b5e20"),
]


def classify_ndvi(ndvi: np.ndarray) -> Tuple[np.ndarray, list]:
    """Return an integer classification array and a list of matplotlib Patch objects."""
    import matplotlib.patches as mpatches

    classified  = np.full(ndvi.shape, np.nan)
    valid_count = np.isfinite(ndvi).sum()
    patches     = []

    for idx, (label, lo, hi, color) in enumerate(_CLASSES):
        mask = np.isfinite(ndvi) & (ndvi >= lo) & (ndvi < hi)
        classified[mask] = idx
        pct = 100.0 * mask.sum() / max(valid_count, 1)
        patches.append(mpatches.Patch(facecolor=color, label=f"{label}  ({pct:.1f} %)"))

    return classified, patches


# ---------------------------------------------------------------------------
# Narrative summary
# ---------------------------------------------------------------------------

def narrative(
    ndvi: np.ndarray,
    date: str,
    source: str,
    bbox: list,
    location_label: str | None = None,
) -> str:
    valid = ndvi[np.isfinite(ndvi)]
    if valid.size == 0:
        return "  No valid pixels found — try a wider date range or higher --max-cloud.\n"

    median     = float(np.median(valid))
    p10, p90   = float(np.percentile(valid, 10)), float(np.percentile(valid, 90))
    dense_pct  = 100.0 * (valid > 0.60).sum()                         / valid.size
    mod_pct    = 100.0 * ((valid > 0.40) & (valid <= 0.60)).sum()     / valid.size
    sparse_pct = 100.0 * ((valid > 0.20) & (valid <= 0.40)).sum()     / valid.size
    bare_pct   = 100.0 * ((valid >= 0.00) & (valid <= 0.20)).sum()    / valid.size
    water_pct  = 100.0 * (valid < 0.00).sum()                         / valid.size

    lon_min, lat_min, lon_max, lat_max = bbox
    c_lat = (lat_min + lat_max) / 2
    c_lon = (lon_min + lon_max) / 2

    if location_label:
        loc_line = f"  Location      : {location_label}"
    else:
        loc_line = f"  Centre point  : {c_lat:+.3f}° lat,  {c_lon:+.3f}° lon"

    if median > 0.60:
        interpretation = "Predominantly dense, healthy vegetation (e.g. closed forest or lush farmland)."
    elif median > 0.40:
        interpretation = "Mixed landscape — active vegetation covers most of the scene."
    elif median > 0.20:
        interpretation = "Sparse or stressed vegetation; notable bare ground or urban cover."
    else:
        interpretation = "Mostly non-vegetated: water bodies, bare soil, or built-up surfaces dominate."

    body = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NDVI Story  ·  {source.upper()}  ·  {date}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{loc_line}
  Valid pixels  : {valid.size:,}

  NDVI statistics
  ─────────────────────────────────────────────────
  Median        :  {median:+.3f}
  10th pct      :  {p10:+.3f}
  90th pct      :  {p90:+.3f}

  Land-cover breakdown (NDVI-based estimate)
  ─────────────────────────────────────────────────
  Dense vegetation      {dense_pct:>6.1f} %
  Moderate vegetation   {mod_pct:>6.1f} %
  Sparse vegetation     {sparse_pct:>6.1f} %
  Bare soil / urban     {bare_pct:>6.1f} %
  Water / shadows       {water_pct:>6.1f} %

  Interpretation
  ─────────────────────────────────────────────────
  {interpretation}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return textwrap.dedent(body)


# ---------------------------------------------------------------------------
# Visualisation
# ---------------------------------------------------------------------------

def plot_story(
    ndvi: np.ndarray,
    rgb: np.ndarray | None,
    date: str,
    source: str,
    bbox: list,
    out_path: Path,
    location_label: str | None = None,
) -> None:
    """Produce a 4-panel storytelling figure and save it."""
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    from matplotlib.gridspec import GridSpec

    classified, legend_patches = classify_ndvi(ndvi)

    ndvi_cmap = mcolors.LinearSegmentedColormap.from_list(
        "ndvi_story",
        [
            (0.00, "#d73027"),
            (0.30, "#fc8d59"),
            (0.45, "#fee08b"),
            (0.55, "#d9ef8b"),
            (0.70, "#91cf60"),
            (1.00, "#1a9850"),
        ],
    )
    class_cmap = mcolors.ListedColormap([color for *_, color in _CLASSES])

    n_panels = 4 if rgb is not None else 3
    fig = plt.figure(figsize=(5.2 * n_panels, 5.4), dpi=150)
    fig.patch.set_facecolor("#0d0d0d")
    gs = GridSpec(1, n_panels, figure=fig, wspace=0.06, left=0.02, right=0.98)

    col = 0

    # ── Panel 1: True-colour RGB ──────────────────────────────────────────────
    if rgb is not None:
        ax = fig.add_subplot(gs[0, col])
        ax.imshow(rgb)
        ax.set_title("True Colour (RGB)", color="white", fontsize=9, pad=6)
        ax.axis("off")
        col += 1

    # ── Panel 2: NDVI heatmap ─────────────────────────────────────────────────
    ax = fig.add_subplot(gs[0, col])
    im = ax.imshow(ndvi, cmap=ndvi_cmap, vmin=-0.2, vmax=0.9)
    ax.set_title("NDVI", color="white", fontsize=9, pad=6)
    ax.axis("off")
    cb = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04, orientation="vertical")
    cb.set_label("NDVI value", color="white", fontsize=7)
    cb.ax.yaxis.set_tick_params(color="white")
    plt.setp(cb.ax.yaxis.get_ticklabels(), color="white", fontsize=7)
    col += 1

    # ── Panel 3: Classification map ───────────────────────────────────────────
    ax = fig.add_subplot(gs[0, col])
    ax.imshow(classified, cmap=class_cmap, vmin=0, vmax=len(_CLASSES) - 1,
              interpolation="nearest")
    ax.set_title("NDVI Classification", color="white", fontsize=9, pad=6)
    ax.axis("off")
    ax.legend(handles=legend_patches, loc="lower left", fontsize=6,
              framealpha=0.65, labelcolor="white", facecolor="#1a1a1a", edgecolor="#555")
    col += 1

    # ── Panel 4: Histogram ────────────────────────────────────────────────────
    ax = fig.add_subplot(gs[0, col])
    valid = ndvi[np.isfinite(ndvi)].ravel()
    ax.hist(valid, bins=90, color="#66bb6a", edgecolor="none", alpha=0.85)
    med = float(np.median(valid))
    ax.axvline(med, color="white", linewidth=1.3, label=f"Median  {med:+.3f}")
    ax.set_facecolor("#1a1a1a")
    ax.tick_params(colors="white", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#555")
    ax.set_xlabel("NDVI", color="white", fontsize=8)
    ax.set_ylabel("Pixel count", color="white", fontsize=8)
    ax.set_title("Distribution", color="white", fontsize=9, pad=6)
    ax.legend(fontsize=7, labelcolor="white", facecolor="#1a1a1a",
              edgecolor="#555", framealpha=0.65)

    lon_min, lat_min, lon_max, lat_max = bbox
    loc_part = (
        location_label
        if location_label
        else f"bbox [{lon_min:.2f}, {lat_min:.2f}, {lon_max:.2f}, {lat_max:.2f}]"
    )
    fig.suptitle(
        f"NDVI Explorer  ·  {source.upper()}  ·  {date}  ·  {loc_part}",
        color="white", fontsize=11, y=1.02,
    )

    plt.savefig(out_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Figure saved → {out_path}")


# ---------------------------------------------------------------------------
# Data backends
# ---------------------------------------------------------------------------

def _select_mosaic_group(items: list, max_cloud: float):
    """
    Group STAC items by acquisition date and return all tiles from the best day.

    A city often spans several satellite tiles acquired on the same pass.
    Returning all tiles for one date lets stackstac mosaic them into a
    seamless, full-coverage scene.
    """
    from collections import defaultdict

    candidates = [i for i in items if i.properties.get("eo:cloud_cover", 100) <= max_cloud]
    if not candidates:
        print(f"  Warning: no items within cloud threshold; using best available.")
        candidates = items

    groups: dict = defaultdict(list)
    for item in candidates:
        date_key = item.datetime.strftime("%Y-%m-%d") if item.datetime else "unknown"
        groups[date_key].append(item)

    # Pick the date whose tiles have the lowest average cloud cover
    best_date = min(
        groups,
        key=lambda d: sum(
            i.properties.get("eo:cloud_cover", 100) for i in groups[d]
        ) / len(groups[d]),
    )
    return groups[best_date], best_date


def _mosaic(data) -> object:
    """
    Collapse the time dimension by taking the median of valid pixels.
    For non-overlapping tiles this is identical to 'first valid pixel';
    for overlapping tiles it averages the overlap gracefully.
    """
    if data.sizes.get("time", 1) > 1:
        return data.median("time", skipna=True)
    return data.squeeze("time", drop=True)


def _stack_to_arrays(
    data, band_nir: str, band_red: str, band_g: str, band_b: str, polygon=None
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Shared post-processing for both backends:
    optionally clip to a polygon, then extract numpy arrays.
    """
    if polygon is not None:
        print("  Clipping to city polygon …")
        data = _clip_to_polygon(data, polygon)

    nir = data.sel(band=band_nir).values.squeeze().astype(float)
    red = data.sel(band=band_red).values.squeeze().astype(float)
    r   = data.sel(band=band_red).values.squeeze().astype(float)
    g   = data.sel(band=band_g).values.squeeze().astype(float)
    b   = data.sel(band=band_b).values.squeeze().astype(float)

    nodata = (nir == 0) | (red == 0)
    nir[nodata] = np.nan
    red[nodata] = np.nan

    return nir, red, _normalize_rgb(r, g, b)


def fetch_sentinel2(
    bbox: list,
    start_date: str,
    end_date: str,
    max_cloud: float = 20.0,
    polygon=None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """
    Fetch the least-cloudy Sentinel-2 L2A scene from Microsoft Planetary Computer.
    If `polygon` is provided (shapely geometry), the result is clipped to it.
    No authentication required.
    """
    pystac_client = _require("pystac_client", "pystac-client")
    pc            = _require("planetary_computer", "planetary-computer")
    stackstac     = _require("stackstac", "stackstac")

    print("  Searching Sentinel-2 L2A on Microsoft Planetary Computer …")
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox,
        datetime=f"{start_date}/{end_date}",
        max_items=100,
    )
    items = list(search.items())
    if not items:
        sys.exit(
            "No Sentinel-2 scenes found for the given area/date range.\n"
            "Try widening the date range or increasing --max-cloud."
        )

    tiles, date_str = _select_mosaic_group(items, max_cloud)
    avg_cloud = sum(i.properties.get("eo:cloud_cover", 0) for i in tiles) / len(tiles)
    print(f"  Date     : {date_str}  |  Tiles: {len(tiles)}  |  Avg cloud: {avg_cloud:.1f} %")
    for t in tiles:
        print(f"    {t.id}")

    print(f"  Stacking {len(tiles)} tile(s) at 60 m resolution …")
    stack = stackstac.stack(
        tiles,
        assets=["B08", "B04", "B03", "B02"],
        epsg=4326,
        resolution=0.0006,   # ~60 m in degrees
        bounds_latlon=bbox,
        dtype="float64",
        fill_value=np.nan,
    )
    data = _mosaic(stack.compute())

    nir, red, rgb = _stack_to_arrays(data, "B08", "B04", "B03", "B02", polygon)
    return nir, red, rgb, date_str


def fetch_landsat(
    bbox: list,
    start_date: str,
    end_date: str,
    max_cloud: float = 20.0,
    polygon=None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """
    Fetch the least-cloudy Landsat 8/9 Collection 2 Level-2 scene from
    Microsoft Planetary Computer.
    If `polygon` is provided (shapely geometry), the result is clipped to it.
    No authentication required.
    """
    pystac_client = _require("pystac_client", "pystac-client")
    pc            = _require("planetary_computer", "planetary-computer")
    stackstac     = _require("stackstac", "stackstac")

    print("  Searching Landsat C2L2 on Microsoft Planetary Computer …")
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )
    search = catalog.search(
        collections=["landsat-c2-l2"],
        bbox=bbox,
        datetime=f"{start_date}/{end_date}",
        max_items=100,
    )
    items = list(search.items())
    if not items:
        sys.exit(
            "No Landsat scenes found for the given area/date range.\n"
            "Try widening the date range or increasing --max-cloud."
        )

    tiles, date_str = _select_mosaic_group(items, max_cloud)
    avg_cloud = sum(i.properties.get("eo:cloud_cover", 0) for i in tiles) / len(tiles)
    print(f"  Date     : {date_str}  |  Tiles: {len(tiles)}  |  Avg cloud: {avg_cloud:.1f} %")
    for t in tiles:
        print(f"    {t.id}")

    # Auto-detect band naming from first tile (modern vs legacy)
    asset_keys = list(tiles[0].assets.keys())
    if "nir08" in asset_keys:
        nir_key, red_key, g_key, b_key = "nir08", "red", "green", "blue"
    else:
        nir_key, red_key, g_key, b_key = "B5", "B4", "B3", "B2"

    print(f"  Stacking {len(tiles)} tile(s) at 120 m resolution …")
    stack = stackstac.stack(
        tiles,
        assets=[nir_key, red_key, g_key, b_key],
        epsg=4326,
        resolution=0.0011,   # ~120 m in degrees
        bounds_latlon=bbox,
        dtype="float64",
        fill_value=np.nan,
    )
    data = _mosaic(stack.compute())

    nir, red, rgb = _stack_to_arrays(data, nir_key, red_key, g_key, b_key, polygon)
    return nir, red, rgb, date_str


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ndvi_explorer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--source",
        choices=["sentinel2", "landsat"],
        default="sentinel2",
        help="Satellite data backend (default: sentinel2)",
    )

    aoi = p.add_mutually_exclusive_group()
    aoi.add_argument(
        "--city",
        default=None,
        metavar="NAME",
        help="City or place name — boundary polygon fetched from OpenStreetMap "
             "(e.g. 'São Paulo, Brazil', 'Nairobi, Kenya')",
    )
    aoi.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
        default=None,
        help="Manual bounding box in WGS84 degrees",
    )

    p.add_argument("--start", default="2024-06-01", metavar="YYYY-MM-DD",
                   help="Start date (default: 2024-06-01)")
    p.add_argument("--end",   default="2024-08-31", metavar="YYYY-MM-DD",
                   help="End date   (default: 2024-08-31)")
    p.add_argument(
        "--max-cloud",
        type=float,
        default=20.0,
        metavar="PCT",
        help="Maximum cloud-cover %% accepted (default: 20)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="FILE",
        help="Output figure path (default: ndvi_<source>_<date>.png)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"\n  ╔══════════════════════════════════════╗")
    print(f"  ║        NDVI Explorer  🛰              ║")
    print(f"  ╚══════════════════════════════════════╝")
    print(f"  Source  : {args.source.upper()}")
    print(f"  Period  : {args.start} → {args.end}")
    print(f"  Cloud ≤ : {args.max_cloud} %\n")

    # ── Resolve area of interest ──────────────────────────────────────────────
    polygon        = None
    location_label = None

    if args.city:
        polygon, bbox, location_label = city_to_polygon(args.city)
        print()
    elif args.bbox:
        bbox = args.bbox
    else:
        # Default to São Paulo when neither flag is given
        bbox = [-46.70, -23.65, -46.45, -23.45]
        print("  No --city or --bbox given; using default São Paulo bbox.\n")

    # ── Fetch + compute NDVI ──────────────────────────────────────────────────
    if args.source == "sentinel2":
        nir, red, rgb, date_str = fetch_sentinel2(
            bbox, args.start, args.end, args.max_cloud, polygon=polygon
        )
    else:
        nir, red, rgb, date_str = fetch_landsat(
            bbox, args.start, args.end, args.max_cloud, polygon=polygon
        )

    ndvi = compute_ndvi(nir, red)

    print(narrative(ndvi, date_str, args.source, bbox, location_label=location_label))

    # Build output filename: include city slug when --city is used
    if args.out:
        out_path = args.out
    elif location_label:
        slug = location_label.lower().replace(" ", "_").replace(",", "")
        out_path = Path(f"ndvi_{args.source}_{date_str}_{slug}.png")
    else:
        out_path = Path(f"ndvi_{args.source}_{date_str}.png")

    plot_story(ndvi, rgb, date_str, args.source, bbox, out_path,
               location_label=location_label)

    print("  Done.\n")


if __name__ == "__main__":
    main()
