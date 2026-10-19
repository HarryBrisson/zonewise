"""Lot area for each rezoning: its address range → Cook County PINs → parcel polygon areas.

The Parcel Universe (pabr-t5kh) has no land area, so:
  1. Assessor - Parcel Addresses (3723-97qp), every City of Chicago PIN's property address for
     ADDRESS_YEAR, downloaded once → data/raw/parcel_addresses_<year>.jsonl
  2. each rezoning's address segments ("5414-5422 S Kedzie Ave, 3201 W 55th St") expand to every
     house number of the same parity in range, matched on (number, direction, street name)
  3. ccgisdata - Parcel 2021 (77tz-riq7) polygons for those PIN10s → data/raw/parcel_polygons.jsonl;
     lot area = Σ area of the distinct PIN10 polygons (condo units share one PIN10 lot).
It is a proxy: an ordinance's bounded area can include part of a lot, an alley, or a lot whose
address is outside the stated range.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode

from .cache import PROCESSED, RAW, append_jsonl, get_json, now_iso, read_jsonl
from .elms import log

ADDRESSES_URL = "https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"
POLYGONS_URL = "https://datacatalog.cookcountyil.gov/resource/77tz-riq7.json"
ADDRESS_YEAR = 2025
ADDRESSES = RAW / f"parcel_addresses_{ADDRESS_YEAR}.jsonl"
POLYGONS = RAW / "parcel_polygons.jsonl"
LOTS = PROCESSED / "lot_areas.csv"
PAGE = 50000
SQFT_PER_M2 = 10.763910417

SUFFIXES = {"AVENUE": "AVE", "AV": "AVE", "STREET": "ST", "BOULEVARD": "BLVD", "PLACE": "PL", "ROAD": "RD",
            "DRIVE": "DR", "COURT": "CT", "PARKWAY": "PKWY", "TERRACE": "TER", "LANE": "LN", "HIGHWAY": "HWY",
            "SQUARE": "SQ", "WAY": "WAY", "PKY": "PKWY", "CIRCLE": "CIR", "EXPRESSWAY": "EXPY"}
SUFFIX_SET = set(SUFFIXES.values()) | {"AVE", "ST", "BLVD", "PL", "RD", "DR", "CT", "PKWY", "TER", "LN", "HWY", "SQ", "CIR", "EXPY"}
DIRS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "N": "N", "S": "S", "E": "E", "W": "W"}


def street_key(words: list[str]) -> tuple[str, str] | None:
    """['S', 'KEDZIE', 'AVE'] → ('S', 'KEDZIE'); drops the suffix; ordinal '69TH' kept."""
    words = [w.strip(".,").upper() for w in words if w.strip(".,")]
    if not words or words[0] not in DIRS:
        return None
    direction, rest = DIRS[words[0]], words[1:]
    rest = [SUFFIXES.get(w, w) for w in rest]
    cut = next((i for i, w in enumerate(rest) if i > 0 and w in SUFFIX_SET), None)
    if cut is not None:
        rest = rest[:cut]  # "HUMBOLDT BLVD 1E": drop the suffix and any unit after it
    return (direction, " ".join(rest)) if rest else None


def address_segments(title: str) -> list[tuple[int, int, tuple[str, str]]]:
    """Every (low, high, street) range in a rezoning title's address list."""
    m = re.search(r"\bat\s+(.+?)(?:\s*-\s*(?:App|A-?\d{4})|$)", title or "", re.I)
    if not m:
        return []
    text = re.sub(r"a\.k\.a\.?", ",", m.group(1), flags=re.I)
    parts = [p.strip() for p in re.split(r",|;|\band\b|&", text) if p.strip()]
    out: list[tuple[int, int, tuple[str, str]]] = []
    pending: list[tuple[int, int]] = []
    for part in parts:
        mm = re.match(r"(\d+)[A-Z]?(?:\s*-\s*(\d+)[A-Z]?)?\s*(.*)$", part)
        if not mm:
            continue
        low = int(mm.group(1))
        high = int(mm.group(2)) if mm.group(2) else low
        if high < low:  # "3126-38 W Armitage" shorthand
            high = int(str(low)[: len(str(low)) - len(str(high))] + str(high))
        key = street_key(mm.group(3).split())
        if key is None:
            pending.append((low, high))  # "2318, 2324, 2330 W Schubert": street comes later
            continue
        for lo, hi in pending + [(low, high)]:
            if 0 <= hi - lo <= 400:
                out.append((lo, hi, key))
        pending = []
    return out


# ------------------------------------------------------------------------------------------------
def fetch_addresses() -> None:
    if ADDRESSES.exists():
        return
    offset = 0
    while True:
        where = f"year='{ADDRESS_YEAR}' AND prop_address_city_name='CHICAGO'"
        url = f"{ADDRESSES_URL}?{urlencode({'$select': 'pin,pin10,prop_address_full', '$where': where, '$order': 'pin', '$limit': PAGE, '$offset': offset})}"
        rows = get_json(url, timeout=600)
        append_jsonl(ADDRESSES, {"url": url, "fetched_at": now_iso(), "rows": rows})
        log(f"  parcel addresses offset={offset} got={len(rows)}")
        if len(rows) < PAGE:
            break
        offset += PAGE


def address_index() -> dict[tuple[str, str], dict[int, set[str]]]:
    index: dict[tuple[str, str], dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for page in read_jsonl(ADDRESSES):
        for row in page["rows"]:
            words = (row.get("prop_address_full") or "").split()
            if not words or not words[0].isdigit():
                continue
            key = street_key(words[1:])
            if key:
                index[key][int(words[0])].add(row.get("pin10") or row["pin"][:10])
    return index


def ring_area_m2(ring: list[list[float]]) -> float:
    """Planar area of a lon/lat ring in a local equirectangular projection (fine at lot scale)."""
    if len(ring) < 3:
        return 0.0
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    pts = [(p[0] * kx, p[1] * ky) for p in ring]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2


def polygon_area_sqft(geom: dict[str, Any]) -> float:
    polys = geom["coordinates"] if geom.get("type") == "MultiPolygon" else [geom.get("coordinates") or []]
    m2 = sum(ring_area_m2(poly[0]) - sum(ring_area_m2(h) for h in poly[1:]) for poly in polys if poly)
    return m2 * SQFT_PER_M2


def fetch_polygons(pin10s: set[str], workers: int) -> dict[str, float]:
    have: dict[str, float] = {}
    for record in read_jsonl(POLYGONS):
        have.update(record["areas"])
        for pin in record.get("missing", []):
            have.setdefault(pin, float("nan"))
    todo = sorted(p for p in pin10s if p not in have)
    log(f"lots: {len(have)} polygon areas cached, {len(todo)} PIN10s to fetch")
    chunks = [todo[i:i + 80] for i in range(0, len(todo), 80)]

    def one(chunk: list[str]) -> None:
        where = "pin10 in (" + ",".join(f"'{p}'" for p in chunk) + ")"
        url = f"{POLYGONS_URL}?{urlencode({'$select': 'pin10,the_geom', '$where': where, '$limit': 5000})}"
        rows = get_json(url, timeout=300)
        areas: dict[str, float] = defaultdict(float)
        for row in rows:
            if row.get("the_geom"):
                areas[row["pin10"]] = max(areas[row["pin10"]], polygon_area_sqft(row["the_geom"]))
        append_jsonl(POLYGONS, {"url": url, "fetched_at": now_iso(), "areas": dict(areas),
                                "missing": [p for p in chunk if p not in areas]})

    with ThreadPoolExecutor(max_workers=min(4, workers)) as pool:
        list(pool.map(one, chunks))
    out: dict[str, float] = {}
    for record in read_jsonl(POLYGONS):
        out.update(record["areas"])
    return out


def run_lots(*, workers: int = 4) -> None:
    rows = [r for r in csv.DictReader((PROCESSED / "rezonings.csv").open()) if r["status"] == "passed"]
    fetch_addresses()
    index = address_index()
    log(f"lots: {sum(len(v) for v in index.values())} Chicago address numbers indexed")
    pins_by_matter: dict[str, set[str]] = {}
    for row in rows:
        pins: set[str] = set()
        for low, high, key in address_segments(row["title"]) or address_segments("at " + row["address"]):
            numbers = index.get(key, {})
            for n in range(low, high + 1, 2 if high > low else 1):
                pins |= numbers.get(n, set())
            if low == high and not pins:  # a lot is often addressed a number or two away
                for n in sorted((m for m in range(low - 4, low + 5, 2) if m in numbers), key=lambda m: abs(m - low))[:1]:
                    pins |= numbers[n]
        pins_by_matter[row["matter_id"]] = pins
    areas = fetch_polygons(set().union(*pins_by_matter.values()), workers)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    matched = 0
    with LOTS.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["matter_id", "pin10s", "lot_area_sqft"])
        for matter_id, pins in pins_by_matter.items():
            found = [areas[p] for p in pins if p in areas and areas[p] == areas[p]]
            area = round(sum(found)) if found else ""
            matched += bool(found)
            writer.writerow([matter_id, " ".join(sorted(pins)), area])
    log(f"lots: lot area for {matched}/{len(rows)} passed rezonings ({matched / max(1, len(rows)):.1%}) → {LOTS}")


# ------------------------------------------------------------------------------------------------
UNIVERSE_URL = "https://datacatalog.cookcountyil.gov/resource/pabr-t5kh.json"
CENTROIDS = RAW / "parcel_centroids.jsonl"


def parcel_centroids(pin10s: set[str]) -> dict[str, list[float]]:
    """{pin10: [lon, lat]} from the Assessor Parcel Universe (pabr-t5kh), the fallback point when
    the Census geocoder misses but the address matched parcels."""
    have: dict[str, list[float]] = {}
    for record in read_jsonl(CENTROIDS):
        have.update(record["points"])
    todo = sorted(p for p in pin10s if p not in have)
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        where = "pin10 in (" + ",".join(f"'{p}'" for p in chunk) + ") AND lat IS NOT NULL"
        url = f"{UNIVERSE_URL}?{urlencode({'$select': 'pin10,lon,lat', '$where': where, '$limit': 5000})}"
        rows = get_json(url, timeout=300)
        points = {r["pin10"]: [float(r["lon"]), float(r["lat"])] for r in rows}
        append_jsonl(CENTROIDS, {"url": url, "fetched_at": now_iso(), "points": points})
        have.update(points)
    return have
