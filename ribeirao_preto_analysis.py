#!/usr/bin/env python3
"""
ribeirao_preto_analysis.py
==========================
10-year NDVI change detection for Ribeirão Preto, SP, Brazil (2015–2024).

Tracks urban expansion and vegetation loss using Landsat 8/9 imagery
fixed to August (peak dry season) from Microsoft Planetary Computer.
Fixing the acquisition month reduces inter-year seasonality bias.
No authentication required.

Usage
-----
  python3 ribeirao_preto_analysis.py

Outputs  →  ./outputs/
---------
  ndvi_timeseries.png   Side-by-side NDVI maps for each year
  trend_chart.png       Vegetation vs urban area over time
  location_map.png      Ribeirão Preto within São Paulo state + city flag
  summary.txt           Per-year class statistics
"""

from __future__ import annotations

import io
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
import requests
from shapely.geometry import shape, mapping
from shapely import simplify as shp_simplify

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CITY_NAME  = "Ribeirão Preto, São Paulo, Brazil"
STATE_NAME = "Estado de São Paulo, Brazil"
YEARS      = [2015, 2017, 2019, 2021, 2023, 2024]
DRY_START  = "08-01"   # August — peak dry season, fixes month to reduce seasonality bias
DRY_END    = "08-31"
MAX_CLOUD  = 40        # % — slightly lenient: Landsat revisits every 16 days, ~2 scenes/month
OUT        = Path("outputs")

# NDVI class boundaries follow:
#   Tucker, C. J. (1979). Red and photographic infrared linear combinations for
#   monitoring vegetation. Remote Sensing of Environment, 8(2), 127–150.
#   https://doi.org/10.1016/0034-4257(79)90013-0
# and summarised by the NASA Earth Observatory:
#   Weier, J. & Herring, D. (2000). Measuring Vegetation (NDVI & EVI).
#   https://earthobservatory.nasa.gov/features/MeasuringVegetation
CLASSES: List[Tuple] = [
    ("Water / shadow",     -1.00,  0.00, "#1a6aa8"),
    ("Bare soil / urban",   0.00,  0.20, "#c9a227"),
    ("Sparse vegetation",   0.20,  0.40, "#d4e157"),
    ("Moderate vegetation", 0.40,  0.60, "#66bb6a"),
    ("Dense vegetation",    0.60,  1.01, "#1b5e20"),
]

FLAG_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/"
    "8/81/Bandeira_de_Ribeir%C3%A3o_Preto.svg/"
    "500px-Bandeira_de_Ribeir%C3%A3o_Preto.svg.png"
)

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def nominatim_polygon(query: str, simplify_tol: float = 0.01):
    """Fetch a boundary polygon from OpenStreetMap Nominatim."""
    print(f"  Nominatim: '{query}' …")
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "geojson", "polygon_geojson": 1, "limit": 3},
        headers={"User-Agent": "ndvi-ribeirao-preto/1.0"},
        timeout=20,
    )
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        sys.exit(f"  Nominatim returned no results for: {query}")

    poly_feats = [f for f in features if f["geometry"]["type"] in ("Polygon", "MultiPolygon")]
    feat   = poly_feats[0] if poly_feats else features[0]
    geom   = shape(feat["geometry"])
    name   = feat["properties"].get("display_name", query).split(",")[0].strip()
    bbox   = list(geom.bounds)
    simplified = shp_simplify(geom, tolerance=simplify_tol, preserve_topology=True)
    print(f"    → {name}  ({geom.geom_type})  bbox {[round(v,3) for v in bbox]}")
    return simplified, geom, bbox, name   # simplified for plotting, full for clipping


# ---------------------------------------------------------------------------
# STAC / Landsat helpers
# ---------------------------------------------------------------------------

def _select_mosaic_group(items: list, max_cloud: float):
    """Return all tiles from the least-cloudy day in `items`."""
    candidates = [i for i in items if i.properties.get("eo:cloud_cover", 100) <= max_cloud]
    if not candidates:
        candidates = items
    groups: dict = defaultdict(list)
    for item in candidates:
        key = item.datetime.strftime("%Y-%m-%d") if item.datetime else "unknown"
        groups[key].append(item)
    best = min(
        groups,
        key=lambda d: sum(i.properties.get("eo:cloud_cover", 100) for i in groups[d]) / len(groups[d]),
    )
    return groups[best], best


def _mosaic(data):
    """Collapse tiles on the time axis (median of valid pixels)."""
    if data.sizes.get("time", 1) > 1:
        return data.median("time", skipna=True)
    return data.squeeze("time", drop=True)


def _clip(data, polygon):
    """Clip an xarray DataArray to a shapely polygon (EPSG:4326)."""
    import rioxarray  # noqa: F401 — registers .rio accessor
    if data.rio.crs is None:
        data = data.rio.write_crs("EPSG:4326")
    return data.rio.clip([mapping(polygon)], crs="EPSG:4326", drop=True, all_touched=True)


def _normalize_rgb(r, g, b, pct=2.0):
    def stretch(a):
        v = a[np.isfinite(a) & (a > 0)]
        if not v.size:
            return np.zeros_like(a, dtype=float)
        lo, hi = np.percentile(v, [pct, 100 - pct])
        return np.clip((a.astype(float) - lo) / max(hi - lo, 1e-9), 0, 1)
    return np.dstack([stretch(r), stretch(g), stretch(b)])


# ---------------------------------------------------------------------------
# Fetch one year
# ---------------------------------------------------------------------------

def fetch_year(
    year: int,
    bbox: list,
    polygon,
    max_cloud: float = 30.0,
) -> Tuple[np.ndarray | None, np.ndarray | None, str]:
    """
    Fetch the best Landsat dry-season mosaic for `year`.
    Returns (ndvi_array, rgb_array, date_str).
    """
    import pystac_client
    import planetary_computer
    import stackstac

    start = f"{year}-{DRY_START}"
    end   = f"{year}-{DRY_END}"

    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(
        collections=["landsat-c2-l2"],
        bbox=bbox,
        datetime=f"{start}/{end}",
        max_items=100,
    )
    items = list(search.items())
    if not items:
        print(f"  {year}: no scenes found — skipping.")
        return None, None, str(year)

    tiles, date_str = _select_mosaic_group(items, max_cloud)
    avg_cloud = sum(i.properties.get("eo:cloud_cover", 0) for i in tiles) / len(tiles)
    print(f"  {year}: {len(tiles)} tile(s)  date={date_str}  avg_cloud={avg_cloud:.0f}%")

    # Detect band naming
    asset_keys = list(tiles[0].assets.keys())
    if "nir08" in asset_keys:
        nir_k, red_k, g_k, b_k = "nir08", "red", "green", "blue"
    else:
        nir_k, red_k, g_k, b_k = "B5", "B4", "B3", "B2"

    stack = stackstac.stack(
        tiles,
        assets=[nir_k, red_k, g_k, b_k],
        epsg=4326,
        resolution=0.0011,   # ~120 m
        bounds_latlon=bbox,
        dtype="float64",
        fill_value=np.nan,
    )
    data = _mosaic(stack.compute())
    data = _clip(data, polygon)

    nir = data.sel(band=nir_k).values.squeeze().astype(float)
    red = data.sel(band=red_k).values.squeeze().astype(float)
    r   = data.sel(band=red_k).values.squeeze().astype(float)
    g   = data.sel(band=g_k).values.squeeze().astype(float)
    b   = data.sel(band=b_k).values.squeeze().astype(float)

    nodata = (nir == 0) | (red == 0)
    nir[nodata] = np.nan
    red[nodata] = np.nan

    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
    ndvi[(nir + red) == 0] = np.nan

    rgb = _normalize_rgb(r, g, b)
    return ndvi, rgb, date_str


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def class_stats(ndvi: np.ndarray) -> Dict[str, float]:
    """Return percentage of valid pixels in each NDVI class."""
    valid = ndvi[np.isfinite(ndvi)]
    n = max(valid.size, 1)
    stats: Dict[str, float] = {}
    for label, lo, hi, _ in CLASSES:
        mask = (valid >= lo) & (valid < hi)
        stats[label] = 100.0 * mask.sum() / n
    stats["_median"] = float(np.median(valid)) if valid.size else np.nan
    stats["_valid_px"] = int(valid.size)
    return stats


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def _dark_fig(w, h, dpi=150):
    fig = plt.figure(figsize=(w, h), dpi=dpi)
    fig.patch.set_facecolor("#0d0d0d")
    return fig


NDVI_CMAP = mcolors.LinearSegmentedColormap.from_list(
    "ndvi",
    [(0.00, "#d73027"), (0.30, "#fc8d59"), (0.45, "#fee08b"),
     (0.55, "#d9ef8b"), (0.75, "#91cf60"), (1.00, "#1a9850")],
)
CLASS_CMAP = mcolors.ListedColormap([c for *_, c in CLASSES])


def plot_timeseries(
    ndvi_by_year: Dict[int, np.ndarray],
    rgb_by_year:  Dict[int, np.ndarray],
    dates_by_year: Dict[int, str],
) -> None:
    """
    Two-row figure:
      Row 1 — true-colour RGB composite per year
      Row 2 — NDVI heatmap per year
    """
    years = [y for y in YEARS if ndvi_by_year.get(y) is not None]
    n = len(years)

    fig = _dark_fig(4.0 * n, 8.5)
    gs  = GridSpec(2, n, figure=fig, wspace=0.04, hspace=0.12,
                   left=0.02, right=0.95, top=0.92, bottom=0.04)

    for col, year in enumerate(years):
        ndvi = ndvi_by_year[year]
        rgb  = rgb_by_year.get(year)
        date = dates_by_year.get(year, str(year))

        # Row 0: RGB
        ax = fig.add_subplot(gs[0, col])
        if rgb is not None:
            ax.imshow(rgb)
        else:
            ax.set_facecolor("#1a1a1a")
        ax.set_title(f"{year}\n{date}", color="white", fontsize=8, pad=4)
        ax.axis("off")
        if col == 0:
            ax.set_ylabel("True Colour", color="#aaa", fontsize=7)

        # Row 1: NDVI
        ax = fig.add_subplot(gs[1, col])
        im = ax.imshow(ndvi, cmap=NDVI_CMAP, vmin=-0.1, vmax=0.8)
        ax.axis("off")
        if col == 0:
            ax.set_ylabel("NDVI", color="#aaa", fontsize=7)

    # Shared colorbar
    cbar_ax = fig.add_axes([0.96, 0.05, 0.013, 0.40])
    cb = fig.colorbar(im, cax=cbar_ax)
    cb.set_label("NDVI", color="white", fontsize=8)
    cb.ax.yaxis.set_tick_params(color="white")
    plt.setp(cb.ax.yaxis.get_ticklabels(), color="white", fontsize=7)

    fig.suptitle(
        "Ribeirão Preto, SP  —  NDVI Dry Season (June–August)  2015–2024",
        color="white", fontsize=12, y=0.97,
    )
    out = OUT / "ndvi_timeseries.png"
    plt.savefig(out, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved → {out}")


def plot_trend(stats_by_year: Dict[int, Dict]) -> None:
    """Line chart of each NDVI class percentage over the analysis period."""
    years = sorted(y for y in stats_by_year)

    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#111")

    for label, *_, color in CLASSES:
        vals = [stats_by_year[y].get(label, np.nan) for y in years]
        lw   = 2.5 if "urban" in label.lower() or "dense" in label.lower() else 1.5
        ls   = "-" if "urban" in label.lower() or "dense" in label.lower() else "--"
        ax.plot(years, vals, color=color, linewidth=lw, linestyle=ls,
                marker="o", markersize=5, label=label)

    ax.set_xlabel("Year", color="white", fontsize=10)
    ax.set_ylabel("% of city area", color="white", fontsize=10)
    ax.set_title(
        "Ribeirão Preto — Vegetation vs Urban Cover (2015–2024)",
        color="white", fontsize=12, pad=10,
    )
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_edgecolor("#444")
    ax.set_xticks(years)
    ax.set_xticklabels(years, color="white")
    ax.grid(color="#333", linestyle="--", linewidth=0.5)
    ax.legend(
        fontsize=8, labelcolor="white", facecolor="#1a1a1a",
        edgecolor="#555", framealpha=0.8, loc="center right",
    )

    out = OUT / "trend_chart.png"
    plt.tight_layout()
    plt.savefig(out, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved → {out}")


def plot_location_map(
    sp_simple,   # simplified SP polygon (for drawing)
    rp_poly,     # full RP polygon
) -> None:
    """São Paulo state outline with Ribeirão Preto highlighted + city flag inset."""
    from PIL import Image as PILImage

    # Download city flag
    flag_img = None
    try:
        resp = requests.get(FLAG_URL, headers={"User-Agent": "ndvi-analysis/1.0"}, timeout=15)
        resp.raise_for_status()
        flag_img = PILImage.open(io.BytesIO(resp.content))
        print("  City flag downloaded.")
    except Exception as e:
        print(f"  Could not download flag: {e}")

    fig, ax = plt.subplots(figsize=(9, 8), dpi=150)
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#0d1a2a")

    # SP state fill + outline
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath
    import shapely

    def _poly_to_patch(geom, **kw):
        """Convert a shapely geometry to a matplotlib patch."""
        from matplotlib.patches import PathPatch
        from matplotlib.path import Path as MPath
        import numpy as np

        def ring_to_verts(ring):
            coords = np.array(ring.coords)
            codes = [MPath.MOVETO] + [MPath.LINETO] * (len(coords) - 2) + [MPath.CLOSEPOLY]
            return coords, codes

        verts_all, codes_all = [], []
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            v, c = ring_to_verts(poly.exterior)
            verts_all.extend(v); codes_all.extend(c)
            for hole in poly.interiors:
                v, c = ring_to_verts(hole)
                verts_all.extend(v); codes_all.extend(c)
        path = MPath(verts_all, codes_all)
        return PathPatch(path, **kw)

    # São Paulo state
    sp_patch = _poly_to_patch(sp_simple, facecolor="#1a3a1a", edgecolor="#4a8a4a",
                               linewidth=0.8, zorder=1)
    ax.add_patch(sp_patch)

    # Ribeirão Preto
    rp_patch = _poly_to_patch(rp_poly, facecolor="#e53935", edgecolor="#ff8a80",
                               linewidth=1.5, zorder=2, alpha=0.85)
    ax.add_patch(rp_patch)

    # Label
    cx, cy = rp_poly.centroid.x, rp_poly.centroid.y
    ax.annotate(
        "Ribeirão Preto",
        xy=(cx, cy), xytext=(cx + 1.5, cy + 0.8),
        color="white", fontsize=11, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#ff8a80", lw=1.5),
        zorder=5,
    )

    # Set bounds to SP state extent with padding
    minx, miny, maxx, maxy = sp_simple.bounds
    pad = 0.5
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Location: Ribeirão Preto within the State of São Paulo",
                 color="white", fontsize=11, pad=10)

    # Flag inset (bottom-right)
    if flag_img is not None:
        flag_arr = np.array(flag_img)
        ax_flag = fig.add_axes([0.65, 0.05, 0.28, 0.22])
        ax_flag.imshow(flag_arr)
        ax_flag.axis("off")
        ax_flag.set_title("City flag", color="white", fontsize=8, pad=4)
        ax_flag.patch.set_facecolor("#0d0d0d")

    out = OUT / "location_map.png"
    plt.savefig(out, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved → {out}")


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------

def save_summary(
    stats_by_year: Dict[int, Dict],
    dates_by_year: Dict[int, str],
) -> None:
    lines = [
        "Ribeirão Preto — NDVI Dry-Season Analysis  (2015–2024)",
        "=" * 60,
        f"{'Year':<6} {'Date':<12} {'Water':>6} {'Bare/Urban':>10} "
        f"{'Sparse':>7} {'Moderate':>9} {'Dense':>6} {'Median':>7}",
        "-" * 60,
    ]
    for year in sorted(stats_by_year):
        s = stats_by_year[year]
        d = dates_by_year.get(year, "-")
        lines.append(
            f"{year:<6} {d:<12} "
            f"{s.get('Water / shadow', 0):>6.1f} "
            f"{s.get('Bare soil / urban', 0):>10.1f} "
            f"{s.get('Sparse vegetation', 0):>7.1f} "
            f"{s.get('Moderate vegetation', 0):>9.1f} "
            f"{s.get('Dense vegetation', 0):>6.1f} "
            f"{s.get('_median', 0):>+7.3f}"
        )

    # Delta between first and last year
    years_available = sorted(stats_by_year)
    if len(years_available) >= 2:
        y0, yn = years_available[0], years_available[-1]
        s0, sn = stats_by_year[y0], stats_by_year[yn]
        lines += [
            "",
            f"Change  {y0} → {yn}",
            "-" * 40,
        ]
        for label, *_ in CLASSES:
            delta = sn.get(label, 0) - s0.get(label, 0)
            sign  = "+" if delta >= 0 else ""
            lines.append(f"  {label:<25}  {sign}{delta:.1f} pp")
        delta_med = sn.get("_median", 0) - s0.get("_median", 0)
        lines.append(f"  {'Median NDVI':<25}  {delta_med:+.3f}")

    out = OUT / "summary.txt"
    out.write_text("\n".join(lines) + "\n")
    print(f"  Saved → {out}")
    print("\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    OUT.mkdir(exist_ok=True)

    print("\n  ╔══════════════════════════════════════════════╗")
    print("  ║  Ribeirão Preto — 10-Year NDVI Analysis 🌿   ║")
    print("  ╚══════════════════════════════════════════════╝\n")

    # ── 1. Geometry ───────────────────────────────────────────────────────────
    print("[ 1/4 ] Fetching city and state boundaries …")
    rp_simple, rp_full, rp_bbox, _ = nominatim_polygon(CITY_NAME, simplify_tol=0.002)
    sp_simple, sp_full, _,       _ = nominatim_polygon(STATE_NAME, simplify_tol=0.05)

    # ── 2. Satellite data ─────────────────────────────────────────────────────
    print("\n[ 2/4 ] Fetching Landsat dry-season imagery …")
    ndvi_by_year:  Dict[int, np.ndarray] = {}
    rgb_by_year:   Dict[int, np.ndarray] = {}
    dates_by_year: Dict[int, str]        = {}
    stats_by_year: Dict[int, Dict]       = {}

    cache_dir = OUT / "cache"
    cache_dir.mkdir(exist_ok=True)

    for year in YEARS:
        cache_ndvi = cache_dir / f"ndvi_{year}.npy"
        cache_rgb  = cache_dir / f"rgb_{year}.npy"
        cache_date = cache_dir / f"date_{year}.txt"

        if cache_ndvi.exists() and cache_rgb.exists():
            print(f"  {year}: loading from cache …")
            ndvi = np.load(str(cache_ndvi))
            rgb  = np.load(str(cache_rgb))
            date = cache_date.read_text().strip()
        else:
            ndvi, rgb, date = fetch_year(year, rp_bbox, rp_full, MAX_CLOUD)
            if ndvi is None:
                continue
            np.save(str(cache_ndvi), ndvi)
            np.save(str(cache_rgb),  rgb)
            cache_date.write_text(date)

        ndvi_by_year[year]  = ndvi
        rgb_by_year[year]   = rgb
        dates_by_year[year] = date
        stats_by_year[year] = class_stats(ndvi)

    if not ndvi_by_year:
        sys.exit("No data fetched — check your date range or max-cloud setting.")

    # ── 3. Visualisations ─────────────────────────────────────────────────────
    print("\n[ 3/4 ] Generating figures …")
    plot_timeseries(ndvi_by_year, rgb_by_year, dates_by_year)
    plot_trend(stats_by_year)
    plot_location_map(sp_simple, rp_full)

    # ── 4. Summary ────────────────────────────────────────────────────────────
    print("\n[ 4/4 ] Writing summary …")
    save_summary(stats_by_year, dates_by_year)

    print("\n  All outputs written to ./outputs/\n")


if __name__ == "__main__":
    main()
