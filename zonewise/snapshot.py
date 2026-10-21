"""The zoning map as it stands now, as byop/v1 raster layers (Phase 3b).

Source: the newest archived copy of the City's zoning-district map (`dj47-wfun`,
data/raw/city_zoning_archive/, archive.py). Its archive date is the layers' `as_of` (byop v1.1):
the consumer dates every metric on them to the day the map was downloaded, not to when it ran.
History is not backfilled: each monthly copy is its own reading, so a year-over-year series grows
from 2026-10 on (reverse-applying the rezonings would guess at map edits the ordinances don't show).

The polygons are rasterized once onto a 10 m grid in EPSG:3435 (US feet), cell-centre rule, as an
index into the district codes; every layer is derived from that index, sunscore's GeoTIFF pattern
(float32, NaN = no data, deflate):

  zoned_single_family.tif     1 = RS-1/2/3 (houses only: no 3+ unit buildings), else 0
  zoned_multifamily.tif       1 = RT/RM residential districts (two-flats, townhouses, apartments), else 0
  zoned_business_commercial.tif  1 = B/C districts, else 0
  zoned_manufacturing.tif     1 = M districts and Planned Manufacturing Districts, else 0
  zoned_planned_development.tif  1 = Planned Developments, else 0
  zoned_max_far.tif           the district's maximum floor-area ratio (data/districts.csv);
                              NaN where the district sets none (PD, POS, a PMD without a number)

The class rasters are NaN only off the map (no polygon), so a `mean` × 100 is the share of zoned
land; downtown (D), parks (POS) and transportation (T) districts count in that base as 0 in all five.
The polygons run to street and alley centrelines, so "zoned land" includes half the streets around it.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .archive import DATASET, archived_copies
from .build import load_districts, lookup
from .cache import LAYERS
from .citymap import load_snapshot, map_to_code
from .elms import log

CELL_M = 10.0
CELL_FT = CELL_M / 0.3048006096012192  # EPSG:3435 is in US survey feet
CRS = "EPSG:3435"
MAX_MB = 500  # Penlight's per-asset download cap
CODE_FIXES = {"RM4-.5": "RM-4.5"}  # typos in the map's zone_class
FAMILIES = {  # layer → districts.csv class_family values that are 1
    "zoned_single_family": {"residential_single_unit"},
    "zoned_multifamily": {"residential_two_flat_townhouse", "residential_multi_unit"},
    "zoned_business_commercial": {"business", "commercial"},
    "zoned_manufacturing": {"manufacturing", "planned_manufacturing"},
    "zoned_planned_development": {"planned_development"},
}
FAR_LAYER = "zoned_max_far"
LAYER_NAMES = [*FAMILIES, FAR_LAYER]


def grid_for(bounds: tuple[float, float, float, float], cell: float = CELL_FT):
    """(transform, (height, width)) of the cell-aligned grid covering `bounds` (minx, miny, maxx, maxy)."""
    import math

    from rasterio.transform import from_origin

    minx, miny = math.floor(bounds[0] / cell) * cell, math.floor(bounds[1] / cell) * cell
    maxx, maxy = math.ceil(bounds[2] / cell) * cell, math.ceil(bounds[3] / cell) * cell
    return from_origin(minx, maxy, cell, cell), (round((maxy - miny) / cell), round((maxx - minx) / cell))


def rasterize_classes(polys: list[tuple[str, Any]], cell: float = CELL_FT):
    """[(district code, shapely geometry in EPSG:3435)] → (index grid, codes, transform). index 0 =
    no polygon; index i ≥ 1 = codes[i - 1]. Where polygons overlap, the later one wins."""
    import numpy as np
    from rasterio.features import rasterize
    from shapely import union_all

    codes = sorted({code for code, _ in polys})
    position = {code: i + 1 for i, code in enumerate(codes)}
    bounds = union_all([geom.envelope for _, geom in polys]).bounds
    transform, shape_hw = grid_for(bounds, cell)
    index = rasterize(((geom, position[code]) for code, geom in polys), out_shape=shape_hw, transform=transform,
                      fill=0, dtype="int16")
    return index.astype(np.int16), codes, transform


def derive_layers(index, codes: list[str], table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Each layer's float32 grid from the class index (NaN off the map; unknown codes NaN everywhere)."""
    import numpy as np

    nan = np.float32("nan")
    info = [lookup(code, table) for code in codes]
    out = {}
    for name, families in FAMILIES.items():
        values = np.array([nan] + [nan if d is None else np.float32(d["family"] in families) for d in info], dtype="float32")
        out[name] = values[index]
    far = np.array([nan] + [nan if d is None or d["far"] is None else np.float32(d["far"]) for d in info], dtype="float32")
    out[FAR_LAYER] = far[index]
    return out


def write_tif(path, grid, transform, tags: dict[str, str]) -> None:
    import rasterio

    height, width = grid.shape
    with rasterio.open(path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32", crs=CRS,
                       transform=transform, nodata=float("nan"), compress="deflate", predictor=3, tiled=True,
                       blockxsize=512, blockysize=512) as dst:
        dst.write(grid, 1)
        dst.update_tags(**tags)


def read_as_of() -> str | None:
    """The `as_of` stamped in the rasters, None when they haven't been built (or disagree)."""
    if not all((LAYERS / f"{name}.tif").exists() for name in LAYER_NAMES):
        return None
    import rasterio

    stamps = set()
    for name in LAYER_NAMES:
        with rasterio.open(LAYERS / f"{name}.tif") as src:
            stamps.add(src.tags().get("AS_OF"))
    return stamps.pop() if len(stamps) == 1 and None not in stamps else None


def run_snapshot() -> str:
    """Rasterize the newest archived map copy into data/layers/*.tif; returns its date (as_of)."""
    import numpy as np

    copies = archived_copies()
    if not copies:
        raise SystemExit("snapshot: no archived copy of dj47-wfun; run `python -m zonewise archive` first")
    copy = copies[-1]
    as_of = copy["date"]
    table = load_districts()
    polys = []
    for p in load_snapshot("current", copies):
        if not p.geom.is_empty:
            code = map_to_code(p.zone_class)
            polys.append((CODE_FIXES.get(code, code), p.geom))
    unknown = Counter(code for code, _ in polys if lookup(code, table) is None)
    if unknown:
        log(f"snapshot: district codes not in districts.csv (NaN in every layer): {unknown.most_common(10)}")
    index, codes, transform = rasterize_classes(polys)
    grids = derive_layers(index, codes, table)
    LAYERS.mkdir(parents=True, exist_ok=True)
    tags = {"AS_OF": as_of, "SOURCE": f"City of Chicago {DATASET} archived {as_of}", "SOURCE_SHA256": copy["sha256"],
            "CELL_M": str(CELL_M)}
    zoned = index > 0
    log(f"snapshot: {len(polys)} polygons from the {as_of} copy → {index.shape[1]}×{index.shape[0]} cells of {CELL_M:g} m; "
        f"zoned {zoned.sum() * CELL_M ** 2 / 1e6:.1f} km²")
    for name, grid in grids.items():
        path = LAYERS / f"{name}.tif"
        write_tif(path, grid, transform, tags)
        mb = path.stat().st_size / 1e6
        if mb > MAX_MB:
            raise SystemExit(f"snapshot: {path.name} is {mb:.0f} MB > {MAX_MB} MB")
        log(f"  {path.name}: citywide mean {float(np.nanmean(grid)):.3f}, valid {np.isfinite(grid).mean():.1%} of cells, {mb:.1f} MB")
    return as_of


# ------------------------------------------------------------------------------------------------
def spec_part(as_of: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(layers, metrics) for aggregation_spec.json."""
    source = (f"City of Chicago Boundaries - Zoning Districts ({DATASET}) as downloaded {as_of}, rasterized to a "
              "10 m grid (cell centres); the polygons run to street and alley centrelines.")
    share_base = ("Share of the zoned land (every cell inside a zoning district, downtown, parks and transportation "
                  "districts included) whose district is")

    def flag_layer(what: str) -> dict[str, Any]:
        return {"kind": "raster", "as_of": as_of, "value": f"1 where the zoning district is {what}, 0 in any other district, NaN off the zoning map"}

    layers = {
        "zoned_single_family": flag_layer("RS-1, RS-2 or RS-3 (residential single-unit)"),
        "zoned_multifamily": flag_layer("an RT or RM residential district"),
        "zoned_business_commercial": flag_layer("a B (business) or C (commercial) district"),
        "zoned_manufacturing": flag_layer("an M (manufacturing) district or a Planned Manufacturing District"),
        "zoned_planned_development": flag_layer("a Planned Development"),
        FAR_LAYER: {"kind": "raster", "as_of": as_of,
                    "value": "the district's maximum floor-area ratio, NaN off the zoning map and where the district sets none (PD, POS)"},
    }
    for name in layers:
        layers[name] = {"file": f"{name}.tif", **layers[name]}

    def pct(layer: str, **fields: Any) -> dict[str, Any]:
        return {"layer": layer, "combine": "mean", "scale": 100, "unit": "percent", **fields}

    metrics = {
        "zoned_single_family_only_pct": pct(
            "zoned_single_family", direction="lower", category="material_wellbeing",
            label="Single-Family Zoning",
            description="Share of the area's zoned land where only houses may be built, no apartment buildings.",
            methodology=(f"{share_base} RS-1, RS-2 or RS-3: detached houses only (RS-3 also allows two-flats), with "
                         f"multi-unit (3+) residential prohibited (17-2-0207). {source}")),
        "zoned_multifamily_pct": pct(
            "zoned_multifamily", direction="higher", category="material_wellbeing",
            label="Multifamily Zoning",
            description="Share of the area's zoned land in residential districts that allow two-flats, townhouses and apartment buildings.",
            methodology=(f"{share_base} RT-3.5 through RM-6.5, the residential districts that permit multi-unit (3+) "
                         f"residential by right (17-2-0207). Business, commercial and downtown districts that also "
                         f"allow apartments are not counted here. {source}")),
        "zoned_business_commercial_pct": pct(
            "zoned_business_commercial", direction="higher", category="community_vitality", default_weight=0,
            label="Business Zoning",
            description="Share of the area's zoned land in business or commercial districts, where shops and offices may go.",
            methodology=f"{share_base} a B1–B3 business or C1–C3 commercial district (17-3). Downtown districts are not counted. {source}"),
        "zoned_manufacturing_pct": pct(
            "zoned_manufacturing", direction="lower", category="physical_environment", default_weight=0,
            label="Manufacturing Zoning",
            description="Share of the area's zoned land set aside for industry.",
            methodology=f"{share_base} an M1–M3 manufacturing district (17-5) or a Planned Manufacturing District (17-6-0400). {source}"),
        "zoned_planned_development_pct": pct(
            "zoned_planned_development", direction="higher", category="good_governance", default_weight=0,
            label="Planned Developments",
            description="Share of the area's zoned land under a Planned Development, rules negotiated for one large project.",
            methodology=(f"{share_base} a Planned Development (17-8), whose uses and bulk are set by its own ordinance "
                         f"rather than a base district. {source}")),
        "zoned_far_mean": {
            "layer": FAR_LAYER, "combine": "mean", "unit": "ratio", "direction": "higher", "category": "material_wellbeing",
            "label": "Zoned FAR",
            "description": "How much building the zoning allows, on average: floor area per square foot of land.",
            "methodology": ("Area-weighted mean of each district's maximum floor-area ratio over the zoned land "
                            "(data/districts.csv, from Chicago Zoning Ordinance 17-2-0304, 17-3-0403, 17-4-0405, 17-5-0404, "
                            "17-6-0405; bonuses not included). Planned Developments, parks (POS) and PMDs without a "
                            f"set FAR are left out. {source}")},
    }
    return layers, metrics


def merge_into_spec(spec: dict[str, Any], as_of: str | None) -> dict[str, Any]:
    """Replace the snapshot layers/metrics in `spec` with the current ones (dropped when not built)."""
    layers, metrics = spec_part(as_of) if as_of else ({}, {})
    spec["layers"] = {k: v for k, v in spec["layers"].items() if k not in LAYER_NAMES} | layers
    spec["metrics"] = {k: v for k, v in spec["metrics"].items() if v.get("layer") not in LAYER_NAMES} | metrics
    return spec


def update_spec(as_of: str) -> None:
    path = LAYERS / "aggregation_spec.json"
    spec = merge_into_spec(json.loads(path.read_text()), as_of)
    path.write_text(json.dumps(spec, indent=2) + "\n")
    log(f"snapshot: {path.name} now has {len(spec['metrics'])} metrics over {len(spec['layers'])} layers")
