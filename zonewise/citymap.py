"""The City's own zoning map as a truth set, an area source and a placement source.

DPD's zoning-district polygons carry the ordinance that last changed them: the application number
(`ordinance`), from 2023-ish also the Clerk record number (`clerk_docn`), and the ordinance date.
Joining a passed rezoning to the polygons tagged with it gives the district the City actually
mapped, the exact rezoned area and its location. Four snapshots (all raw files cached in data/raw/):

  current  dj47-wfun  Boundaries - Zoning Districts (current)          tags: app no + clerk doc + date
  2015-12  xfyf-x4kx  zoning_2016_01 (deprecated October 2016)        tags: app no + date
  2014-09  nifi-zqag  Zoning (deprecated January 2016)                tags: ordinance DATE only
  2012-11  p8va-airx  Zoning_nov2012 shapefile (deprecated Oct 2014)  tags: app no + date

A rezoning later rezoned again loses its tag in the current map; the snapshots catch some of those.
From 2026-10 on, `python -m zonewise refresh` also keeps our own monthly copies of dj47-wfun
(archive.py). The newest copy is `current`; every older copy is a snapshot labelled by its archive
date, matched exactly like `current` (Clerk doc, then app no; ordinance date within 45 days). So a
rezoning later superseded still validates against the copy taken after it passed.
The 2014 snapshot has no application number, so it matches on ordinance date AND overlap with the
rezoning's geocoded point (within MATCH_FT).

Snapshots that predate a rezoning also give its FROM district: the zone class at the rezoned
polygon's interior point in the latest earlier snapshot (stale if another change happened between
that snapshot and the vote).
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .cache import PROCESSED, RAW, append_jsonl, get_json, http_get, now_iso, read_jsonl
from .elms import log
from .parse import app_key

PORTAL = "https://data.cityofchicago.org"
SNAPSHOTS = [  # the portal's own copies: label, dataset id, as-of date (latest ordinance/edit date in the file)
    ("current", "dj47-wfun", None),
    ("2015-12", "xfyf-x4kx", "2015-12-09"),
    ("2014-09", "nifi-zqag", "2014-09-29"),
    ("2012-11", "p8va-airx", "2012-11-21"),
]
MATCH_FT = 1500           # max distance from our geocoded point to a tagged polygon
DATE_SLACK_DAYS = 45      # ordinance date on the map vs the Council vote
FROM_MAX_DAYS = 3 * 365   # an earlier snapshot older than this is too stale to state the from-district
PARCELS_URL = "https://datacatalog.cookcountyil.gov/resource/77tz-riq7.json"
PARCEL_POINTS = RAW / "parcel_points_2021.jsonl"
OUT = PROCESSED / "city_map_join.csv"


def snapshot_order(copies: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    """[(label, as_of)] in matching order: current, our archived copies newest first, then the
    portal snapshots. `copies` is archive.archived_copies() (oldest first); the newest is current."""
    return ([("current", None)] + [(c["date"], c["date"]) for c in reversed(copies[:-1])]
            + [(label, as_of) for label, _, as_of in SNAPSHOTS if label != "current"])


def snapshot_path(label: str) -> Path:
    return RAW / (f"city_zoning_{label}.geojson" if label != "2012-11" else "city_zoning_2011.zip")


def fetch_snapshots() -> None:
    for label, dataset, _ in SNAPSHOTS:
        path = snapshot_path(label)
        if path.exists() and path.stat().st_size:
            continue
        url = (f"{PORTAL}/download/{dataset}/application%2Fzip" if label == "2012-11"
               else f"{PORTAL}/resource/{dataset}.geojson?$limit=50000")
        path.write_bytes(http_get(url, timeout=600, accept="*/*"))
        log(f"citymap: cached {label} → {path.name}")


# ------------------------------------------------------------------------------------------------
@dataclass
class Poly:
    label: str
    zone_class: str
    app: str
    doc: str
    ord_date: str
    geom: Any  # shapely geometry in EPSG:3435 (feet)


def _to_3435():
    from pyproj import Transformer

    return Transformer.from_crs(4326, 3435, always_xy=True).transform


def _from_3435():
    from pyproj import Transformer

    return Transformer.from_crs(3435, 4326, always_xy=True).transform


def _clean_class(z: Any) -> str:
    return re.sub(r"\s", "", str(z or "")).upper()


def load_snapshot(label: str, copies: list[dict[str, Any]] | None = None) -> list[Poly]:
    """Polygons of one snapshot. With archived `copies`, `current` is the newest copy and a date
    label is the copy archived that day; otherwise `current` is the portal file cached at first run."""
    from shapely.geometry import shape
    from shapely.ops import transform

    from .archive import read_copy

    out: list[Poly] = []
    if label == "2012-11":
        import shapefile  # pyshp

        reader = shapefile.Reader(str(RAW / "city_zoning_2011" / "Zoning_nov2012"))
        for rec, shp in zip(reader.records(), reader.shapes()):
            d = rec.as_dict()
            out.append(Poly(label, _clean_class(d["ZONE_CLASS"]), app_key(d["ORDINANCE_"]) if d["ORDINANCE_"] else "", "",
                            d["ORDINANCE1"].isoformat() if d["ORDINANCE1"] else "", shape(shp.__geo_interface__).buffer(0)))
        return out
    project = _to_3435()
    by_date = {c["date"]: c for c in copies or []}
    if label == "current" and copies:
        collection = read_copy(copies[-1])
    elif label in by_date:
        collection = read_copy(by_date[label])
    else:
        collection = json.loads(snapshot_path(label).read_text())
    for feature in collection["features"]:
        p = feature["properties"]
        if not feature.get("geometry"):
            continue
        if label == "2014-09":  # `ordinance_` holds the ordinance date here
            app, ord_date = "", (p.get("ordinance_") or "")[:10]
        else:
            app, ord_date = app_key(p["ordinance"]) if p.get("ordinance") else "", (p.get("ordinance_1") or "")[:10]
        out.append(Poly(label, _clean_class(p.get("zone_class")), app, re.sub(r"\s", "", p.get("clerk_docn") or "").upper(),
                        ord_date, transform(project, shape(feature["geometry"])).buffer(0)))
    return out


def norm_class(code: str) -> str:
    """Compare districts across our codes and the map's: dashes dropped, every PD/PMD flavour one."""
    c = _clean_class(code).replace("-", "")
    if c.startswith("PMD"):
        return "PMD"
    if c.startswith("PD") or "PLANNED" in c:
        return "PD"
    return c


def map_to_code(zone_class: str) -> str:
    """'RM4.5' / 'RM-4.5' / 'PD 1234' / 'PMD 11' → our canonical code."""
    z = _clean_class(zone_class)
    m = re.match(r"^(PMD)-?(\d+[A-Z]?)?$", z)
    if m:
        return f"PMD-{m.group(2)}" if m.group(2) else "PMD"
    m = re.match(r"^PD-?(\d+)?$", z)
    if m:
        return f"PD-{m.group(1)}" if m.group(1) else "PD"
    m = re.match(r"^(RS|RT|RM|POS|[BCM][123]|D[CXRS])-?(\d+(?:\.\d)?A?)$", z)
    return f"{m.group(1)}-{m.group(2)}" if m else z


# ------------------------------------------------------------------------------------------------
def fetch_parcel_points() -> None:
    """Every City of Chicago PIN10 centroid from the 2021 parcel layer (the same layer lot areas
    come from), cached once."""
    if PARCEL_POINTS.exists():
        return
    offset, page = 0, 100000
    while True:
        url = f"{PARCELS_URL}?{urlencode({'$select': 'pin10,latitude,longitude', '$where': "municipality='Chicago'", '$order': 'pin10', '$limit': page, '$offset': offset})}"
        rows = get_json(url, timeout=900)
        append_jsonl(PARCEL_POINTS, {"url": url, "fetched_at": now_iso(), "rows": rows})
        log(f"  parcel points offset={offset} got={len(rows)}")
        if len(rows) < page:
            break
        offset += page


def parcel_tree():
    from shapely import STRtree
    from shapely.geometry import Point

    project = _to_3435()
    pins, points = [], []
    for page in read_jsonl(PARCEL_POINTS):
        for row in page["rows"]:
            if not row.get("pin10"):
                continue
            try:
                x, y = project(float(row["longitude"]), float(row["latitude"]))
            except (KeyError, TypeError, ValueError):
                continue
            pins.append(row["pin10"])
            points.append(Point(x, y))
    return pins, points, STRtree(points)


# ------------------------------------------------------------------------------------------------
def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def index(snaps: dict[str, list[Poly]]) -> tuple[dict, dict, dict]:
    """by_doc[(label, clerk doc)], by_app[(label, app no)], by_date[2014-09 ordinance date]."""
    by_doc: dict[tuple[str, str], list[Poly]] = defaultdict(list)
    by_app: dict[tuple[str, str], list[Poly]] = defaultdict(list)
    by_date: dict[str, list[Poly]] = defaultdict(list)
    for label, polys in snaps.items():
        for p in polys:
            if p.doc:
                by_doc[(label, p.doc)].append(p)
            if p.app:
                by_app[(label, p.app)].append(p)
            if label == "2014-09" and p.ord_date:
                by_date[p.ord_date].append(p)
    return by_doc, by_app, by_date


def match(r: dict[str, str], here: Any, order: list[str], as_of: dict[str, str], by_doc: dict, by_app: dict,
          by_date: dict) -> list[Poly]:
    """The tagged polygons of one passed rezoning in the first snapshot (in `order`) that has them.
    A snapshot older than the vote can't hold its tag and is skipped (`current` never is). Every
    snapshot but 2014-09 matches on Clerk doc, then app no; 2014-09 has only ordinance dates, so it
    matches on date AND proximity. In all of them the map's ordinance date must be within
    DATE_SLACK_DAYS of the vote and, when we have a point, the polygon within MATCH_FT of it."""
    record = r["record_number"].upper()
    for label in order:
        if label != "current" and as_of[label] < r["passed_date"]:
            continue  # the snapshot predates the vote
        if label == "2014-09":
            cands = [p for p in by_date.get(r["passed_date"], []) if here is not None and p.geom.distance(here) <= MATCH_FT]
        else:
            cands = by_doc.get((label, record)) or by_doc.get((label, re.sub(r"^S", "", record))) or []
            cands = cands or (by_app.get((label, r["app_no"]), []) if r["app_no"] else [])
        cands = [p for p in cands if not p.ord_date or _days(p.ord_date, r["passed_date"]) <= DATE_SLACK_DAYS]
        if here is not None and cands:
            cands = [p for p in cands if p.geom.distance(here) <= MATCH_FT]  # an app number reused/misread elsewhere is not this rezoning
        if cands:
            return cands
    return []


def run_citymap(*, workers: int = 4) -> None:
    from shapely import STRtree
    from shapely.geometry import Point
    from shapely.ops import transform, unary_union

    from .archive import archived_copies
    from .build import in_chicago
    from .geocode import cached_points, clean_lookup
    from .lots import fetch_polygons

    fetch_snapshots()
    copies = archived_copies()
    ordered = snapshot_order(copies)
    order = [label for label, _ in ordered]
    snaps = {label: load_snapshot(label, copies) for label in order}
    as_of = {label: (d or max(p.ord_date for p in snaps[label] if p.ord_date)) for label, d in ordered}
    current_from = f"archived copy {copies[-1]['date']}" if copies else snapshot_path("current").name
    log(f"citymap: current = {current_from}; " + ", ".join(f"{k} {len(v)} polygons (as of {as_of[k]})" for k, v in snaps.items()))
    by_doc, by_app, by_date = index(snaps)
    trees = {label: STRtree([p.geom for p in polys]) for label, polys in snaps.items()}

    project, unproject = _to_3435(), _from_3435()
    rows = [r for r in csv.DictReader((PROCESSED / "rezonings.csv").open())
            if r["status"] == "passed" and r["map_amendment"] == "True" and r["passed_date"]]
    geo = cached_points()
    joined: list[dict[str, Any]] = []
    for r in rows:
        pt = (geo.get(clean_lookup(r["address_lookup"])) or {}).get("point")
        here = Point(*project(*pt)) if in_chicago(pt) else None
        hits = match(r, here, order, as_of, by_doc, by_app, by_date)
        if not hits:
            joined.append({"matter_id": r["matter_id"]})
            continue
        label = hits[0].label
        area_by_class: dict[str, float] = defaultdict(float)
        for p in hits:
            area_by_class[p.zone_class] += p.geom.area
        dominant = max(area_by_class, key=area_by_class.get)
        shape_all = unary_union([p.geom for p in hits])
        inner = shape_all.representative_point()
        lon, lat = unproject(inner.x, inner.y)
        # FROM: the latest snapshot strictly before the vote, at the rezoned area's interior point
        prior = [(as_of[l], l) for l in order if l != "current" and as_of[l] < r["passed_date"]]
        from_city, from_label = "", ""
        prior = [(d, l) for d, l in prior if _days(d, r["passed_date"]) <= FROM_MAX_DAYS]
        if prior:
            _, from_label = max(prior)
            idx = trees[from_label].query(inner, predicate="intersects")
            if len(idx):
                from_city = snaps[from_label][int(idx[0])].zone_class
        joined.append({
            "matter_id": r["matter_id"], "map_snapshot": label, "map_classes": "+".join(sorted(area_by_class)),
            "map_dominant_class": dominant, "map_area_sqft": round(sum(area_by_class.values())),
            "map_polygons": len(hits), "map_lon": round(lon, 7), "map_lat": round(lat, 7),
            "map_from_class": from_city, "map_from_snapshot": from_label,
            "_geom_by_class": {c: unary_union([p.geom for p in hits if p.zone_class == c]) for c in area_by_class},
        })
    log(f"citymap: {sum(1 for j in joined if 'map_snapshot' in j)}/{len(rows)} passed rezonings matched to mapped polygons "
        f"(by snapshot: {dict(sorted(_count(joined).items()))})")

    # lot area = Σ 2021 parcels whose centroid falls inside the rezoned polygons of the final class
    fetch_parcel_points()
    pins, points, tree = parcel_tree()
    log(f"citymap: {len(pins)} parcel centroids indexed")
    pins_in: dict[str, dict[str, set[str]]] = {}
    for j in joined:
        if "_geom_by_class" not in j:
            continue
        pins_in[j["matter_id"]] = {c: {pins[int(i)] for i in tree.query(g, predicate="contains")} for c, g in j["_geom_by_class"].items()}
    areas = fetch_polygons({p for d in pins_in.values() for s in d.values() for p in s}, workers)
    with OUT.open("w", newline="") as handle:
        fields = ["matter_id", "map_snapshot", "map_classes", "map_dominant_class", "map_area_sqft", "map_polygons",
                  "map_lon", "map_lat", "map_from_class", "map_from_snapshot", "map_lot_area_by_class"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for j in joined:
            by_class = {c: round(sum(areas.get(p, 0) for p in s)) for c, s in pins_in.get(j["matter_id"], {}).items()}
            j["map_lot_area_by_class"] = json.dumps(by_class) if by_class else ""
            writer.writerow(j)
    log(f"citymap: wrote {OUT}")


def _count(joined: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for j in joined:
        if "map_snapshot" in j:
            out[j["map_snapshot"]] += 1
    return out
