"""rezonings.csv + geocodes + lot areas + districts.csv → byop/v1 layers dir.

Per passed map amendment:
  from_far     mean max FAR of the from-district(s) (split lots weigh districts equally)
  to_far       max FAR of the final district. A Planned Development has no FAR of its own: under
               17-8-0901 it must comply with the FAR of the district in force just before PD
               approval, so a PD's FAR is the underlying district's — the intermediate district
               of a two-step ordinance (B3-2 → B3-5 → PD), or the from-district when the change
               goes straight to PD (FAR unchanged).
  far_delta    to_far − from_far; upzoning / downzoning = its sign (null when either FAR is unknown)
  to_multifamily  the final district permits 3+ unit housing and at least one from-district didn't
  capacity_sqft_delta  far_delta × lot_area_sqft (null when either is unknown)
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from datetime import date
from typing import Any

from .cache import DATA, LAYERS, PROCESSED
from .elms import log
from .geocode import cached_points, clean_lookup

DISTRICTS = DATA / "districts.csv"
SOURCE_URL = "https://github.com/HarryBrisson/zonewise"


def load_districts() -> dict[str, dict[str, Any]]:
    out = {}
    for row in csv.DictReader(DISTRICTS.open()):
        out[row["district"]] = {
            "far": float(row["max_far"]) if row["max_far"] else None,
            "multifamily": {"yes": True, "no": False}.get(row["allows_multifamily"]),
            "family": row["class_family"], "certainty": row["certainty"],
        }
    return out


def lookup(code: str, table: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if code in table:
        return table[code]
    if code.startswith("PMD-"):
        return table.get(code.rstrip("ABCDEFGH")) or table.get("PMD")
    if code.startswith("PD"):
        return table.get("PD")
    return None


def is_pd(code: str) -> bool:
    return code.startswith("PD")


def classify(row: dict[str, str], table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    froms = [c for c in row["from_districts"].split("+") if c]
    chain = [c for c in row["district_chain"].split(">") if c]
    to = row["to_district"]
    out: dict[str, Any] = {"from_far": None, "to_far": None, "far_delta": None, "upzoning": None, "downzoning": None,
                           "to_multifamily": None, "pd_or_pmd": None, "to_far_basis": "", "uncertain_far": False}
    if not froms or not to:
        return out
    out["pd_or_pmd"] = any(c.startswith(("PD", "PMD")) for c in froms + chain)
    from_info = [lookup(c, table) for c in froms]
    if all(i and i["far"] is not None for i in from_info):
        out["from_far"] = round(sum(i["far"] for i in from_info) / len(from_info), 3)
    effective = to
    if is_pd(to):
        underlying = [c for c in chain if not is_pd(c)]
        if underlying:
            effective, out["to_far_basis"] = underlying[-1], "pd_underlying_district"
        elif not any(is_pd(c) for c in froms):
            effective, out["to_far_basis"] = "", "pd_inherits_prior_far"
    else:
        out["to_far_basis"] = "district_table"
    to_info = lookup(effective, table) if effective else None
    if out["to_far_basis"] == "pd_inherits_prior_far":
        out["to_far"] = out["from_far"]
    elif to_info and to_info["far"] is not None:
        out["to_far"] = to_info["far"]
    out["uncertain_far"] = any(i and i["certainty"] != "verified" for i in from_info + [to_info])
    if out["from_far"] is not None and out["to_far"] is not None:
        delta = round(out["to_far"] - out["from_far"], 3)
        out.update(far_delta=delta, upzoning=delta > 0, downzoning=delta < 0)
    to_mf = to_info["multifamily"] if to_info else None
    from_mf = [i["multifamily"] if i else None for i in from_info]
    if out["to_far_basis"] == "pd_inherits_prior_far":
        to_mf = None  # a PD's permitted uses are set by its own statements, not inherited
    if to_mf is False:
        out["to_multifamily"] = False
    elif to_mf is True and all(m is not None for m in from_mf):
        out["to_multifamily"] = not all(from_mf)
    return out


CHICAGO_BBOX = (-87.95, 41.64, -87.52, 42.03)  # lon/lat envelope of the city incl. O'Hare


def in_chicago(point: list[float] | None) -> bool:
    if not point:
        return False
    west, south, east, north = CHICAGO_BBOX
    return west <= point[0] <= east and south <= point[1] <= north


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reconcile(row: dict[str, str], m: dict[str, str]) -> tuple[dict[str, str], dict[str, Any]]:
    """Check our parsed districts against the City zoning map's polygons for this ordinance.
    Where we parsed nothing, or read only a request (agenda, application, the as-introduced text of
    an ordinance that passed as a substitute), or missed a PD step, the map's dominant
    (largest-area) class becomes the to-district; the parsed value is kept in parsed_to_district.
    Where the passed text disagrees (the ordinance, or for a substitute whose PDF didn't read, its
    printing in the Journal of Proceedings, parse_method "journal"), the text stands (the Journal
    backs it in 37 of 38 adjudicated conflicts) and map_check flags it."""
    from .citymap import map_to_code, norm_class

    text_source = "journal" if row["parse_method"] == "journal" else "parsed"  # the Journal: the passed text
    info: dict[str, Any] = {"map_check": None, "district_source": text_source if row["to_district"] else None,
                            "parsed_to_district": row["to_district"] or None, "map_from_check": None}
    if not m.get("map_snapshot"):
        return row, info
    classes = {norm_class(c) for c in m["map_classes"].split("+") if c}
    chain = [c for c in row["district_chain"].split(">") if c]
    dominant = map_to_code(m["map_dominant_class"])
    weak_match = m["map_snapshot"] == "2014-09"  # matched on date + proximity only: check, never fill
    row = dict(row)
    if not row["to_district"]:
        info["map_check"] = "unparsed"
        if not weak_match:
            info["district_source"] = "city_map"
            row.update(to_district=dominant, district_chain=dominant)
            if m.get("map_from_class"):
                row["from_districts"] = map_to_code(m["map_from_class"])
                info["district_source"] = "city_map+prior_snapshot"
    elif norm_class(row["to_district"]) in classes:
        info["map_check"] = "agrees"
    elif any(norm_class(c) in classes for c in chain):
        info["map_check"] = "agrees_chain_step"  # e.g. we read B3-5>PD, the map shows B3-5
    else:
        info["map_check"] = "disagrees"
        if weak_match:
            pass
        elif norm_class(dominant) == "PD" and not row["to_district"].startswith("PD"):
            # a two-step ordinance whose PD step we missed: keep ours as the underlying district
            info["district_source"] = "city_map_pd_step"
            row.update(to_district=dominant, district_chain=f"{row['to_district']}>{dominant}")
        elif row["parse_method"] not in ("regex_ordinance", "journal"):
            # a committee "Change Request" or application form is the request, the map is the result
            info["district_source"] = "city_map_override"
            row.update(to_district=dominant, district_chain=dominant)
        elif row["parse_method"] == "regex_ordinance" and row.get("substitute_unread") == "True":
            # the matter passed as a substitute we couldn't read, so our reading is the ordinance as
            # introduced: a request again (parse reads the Journal instead where it can). The Journal of Proceedings sided with the map in 13 of 13
            # such conflicts (data/review/journal_adjudication.csv)
            info["district_source"] = "city_map_substitute"
            row.update(to_district=dominant, district_chain=dominant)
        # else: the passed ordinance text (or its Journal printing) and the map conflict. The Journal sided with the text in
        # 37 of 38 (the map's tag is stale or a later change kept it): keep it and leave the flag
    if m.get("map_from_class") and row["from_districts"] and info["district_source"] != "city_map+prior_snapshot":
        info["map_from_check"] = norm_class(m["map_from_class"]) in {norm_class(c) for c in row["from_districts"].split("+")}
    return row, info


def assemble() -> list[dict[str, Any]]:
    from .citymap import OUT as MAP_JOIN, norm_class
    from .lots import LOTS, parcel_centroids

    table = load_districts()
    rows = [r for r in csv.DictReader((PROCESSED / "rezonings.csv").open())
            if r["status"] == "passed" and r["map_amendment"] == "True" and r["passed_date"]]
    points = cached_points()
    lots = {r["matter_id"]: r for r in csv.DictReader(LOTS.open())} if LOTS.exists() else {}
    maps = {r["matter_id"]: r for r in csv.DictReader(MAP_JOIN.open())} if MAP_JOIN.exists() else {}
    need_centroid = {p for r in rows if not (points.get(clean_lookup(r["address_lookup"])) or {}).get("point")
                     for p in (lots.get(r["matter_id"], {}).get("pin10s") or "").split()}
    centroids = parcel_centroids(need_centroid) if need_centroid else {}
    out = []
    for row in rows:
        m = maps.get(row["matter_id"], {})
        row, info = reconcile(row, m)
        strong = m.get("map_snapshot") not in (None, "", "2014-09")  # 2014-09 matched on date + proximity only
        if strong and m.get("map_lon"):  # the rezoned polygon's interior point beats the first address of the range
            point, source = [float(m["map_lon"]), float(m["map_lat"])], f"city_zoning_map_{m['map_snapshot']}"
        else:
            geo = points.get(clean_lookup(row["address_lookup"])) or {}
            point = geo.get("point") if in_chicago(geo.get("point")) else None  # "Chicago, IL" can still match a namesake street elsewhere
            source = "census_batch" if point else ""
        lot = lots.get(row["matter_id"], {})
        if not point:
            pts = [centroids[p] for p in (lot.get("pin10s") or "").split() if p in centroids]
            if pts and in_chicago(pts[0]):
                point = [round(sum(p[0] for p in pts) / len(pts), 7), round(sum(p[1] for p in pts) / len(pts), 7)]
                source = "parcel_universe_centroid"
        c = classify(row, table)
        by_class = json.loads(m["map_lot_area_by_class"]) if strong and m.get("map_lot_area_by_class") else {}
        map_lot = sum(v for k, v in by_class.items() if norm_class(k) == norm_class(row["to_district"])) if row["to_district"] else 0
        if map_lot:
            lot_area, lot_source = float(map_lot), "parcels_in_city_zoning_polygon"
        else:
            lot_area = _num(lot.get("lot_area_sqft", ""))
            lot_source = "parcels_at_address_range" if lot_area else None
        capacity = round(c["far_delta"] * lot_area) if c["far_delta"] is not None and lot_area else None
        out.append({"row": row, "point": point, "geocode_source": source, "lot_area_sqft": lot_area, "lot_area_source": lot_source,
                    "capacity_sqft_delta": capacity, "map": m, **info, **c})
    return out


def feature(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": item["point"]}, "properties": {
        "passed_date": row["passed_date"],
        "from_district": row["from_districts"] or None,
        "to_district": row["to_district"] or None,
        "district_chain": row["district_chain"] or None,
        "upzoning": item["upzoning"],
        "downzoning": item["downzoning"],
        "to_multifamily": item["to_multifamily"],
        "pd_or_pmd": item["pd_or_pmd"],
        "far_delta": item["far_delta"],
        "from_far": item["from_far"],
        "to_far": item["to_far"],
        "to_far_basis": item["to_far_basis"] or None,
        "capacity_sqft_delta": item["capacity_sqft_delta"],
        "lot_area_sqft": item["lot_area_sqft"],
        "lot_area_source": item["lot_area_source"],
        "district_source": item["district_source"],
        "parsed_to_district": item["parsed_to_district"],
        "map_check": item["map_check"],
        "map_from_check": item["map_from_check"],
        "map_snapshot": item["map"].get("map_snapshot") or None,
        "map_zone_classes": item["map"].get("map_classes") or None,
        "map_area_sqft": _num(item["map"].get("map_area_sqft", "")),
        "address": row["address"] or None,
        "matter_id": row["matter_id"],
        "record_number": row["record_number"],
        "app_no": row["app_no"] or None,
        "app_kind": row["app_kind"],
        "type_1": row["type_1"] == "True",
        "introduced_date": row["introduced_date"] or None,
        "parse_method": row["parse_method"],
        "journal_page": int(row["journal_page"]) if row.get("journal_page") else None,
        "geocode_source": item["geocode_source"],
        "url": row["url"],
    }}


def write_geojson(path, features: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")) + "\n")


def coverage_years(items: list[dict[str, Any]]) -> tuple[int, int]:
    """First year with a full year of passed rezonings in eLMS (≥ 60% of the median year)."""
    years = Counter(int(i["row"]["passed_date"][:4]) for i in items)
    complete = [y for y in years if y < date.today().year]
    median = sorted(years[y] for y in complete)[len(complete) // 2]
    first_full = min(y for y in complete if years[y] >= 0.6 * median and all(years.get(z, 0) >= 0.6 * median for z in range(y, max(complete) + 1)))
    return min(years), first_full


def spec(first_full: int, as_of: str) -> dict[str, Any]:
    years = [first_full, date.today().year]
    method_tail = ("Ordinances come from the City Clerk's eLMS (matterCategory ZONING RECLASSIFICATIONS); from/to "
                   "districts are read from each ordinance PDF (OCR where scanned) or the Zoning Committee agenda, "
                   "checked against (and, for missed PD steps or where only a request was readable, completed from) the City's "
                   "zoning-district map, placed at the "
                   "rezoned map polygon (else the Census geocoder), and counted in the calendar year the City Council passed them.")
    common = {"time_field": "passed_date", "years": years}
    governance, housing = {"category": "good_governance"}, {"category": "material_wellbeing"}
    return {
        "contract": "byop/v1",
        "source": "zonewise",
        "source_url": SOURCE_URL,
        "caveat": ("Pre-2023 ordinances were migrated from Legistar; their district codes come from OCR of scanned "
                   "PDFs. Districts are the change as written in the ordinance or committee agenda; where only the "
                   "as-introduced text of a substitute ordinance was readable, the City map's district is used. Ward values use "
                   "today's ward map, not the map in force when it passed. Of the rezonings matched to the City zoning map, "
                   "97.7% agree with it; conflicts with the passed ordinance text are kept as the text reads (the Council's "
                   "Journal of Proceedings backs the text in 37 of 38 checked) and flagged (map_check)."),
        "coverage": {"first_full_year": first_full, "as_of": as_of},
        "layers": {
            "rezonings": {"file": "rezonings.geojson", "kind": "points",
                          "value": "one point per zoning map amendment the City Council passed; properties.passed_date, from/to district, upzoning, downzoning, to_multifamily, pd_or_pmd, far_delta, capacity_sqft_delta"},
            "rezonings_far_known": {"file": "rezonings_far_known.geojson", "kind": "points",
                                    "value": "the passed rezonings whose before and after floor-area ratio are both known (the base for the upzoning share)"},
            "rezonings_use_known": {"file": "rezonings_use_known.geojson", "kind": "points",
                                    "value": "the passed rezonings whose before and after residential use permissions are both known (the base for the multifamily share)"},
        },
        "metrics": {
            "rezonings_passed_per_km2": {
                "layer": "rezonings", "combine": "count", "per": "area_km2", "unit": "rate", "direction": "higher",
                "label": "Rezonings Passed",
                "description": "Zoning map changes the City Council passed per square kilometer in the year.",
                "methodology": f"Count of passed zoning map amendments (Type 1, standard and aldermanic) per km², by year passed. {method_tail}",
                **common, **governance},
            "rezonings_upzoning_share": {
                "layer": "rezonings_far_known", "combine": "share", "flag": "upzoning", "scale": 100, "unit": "percent", "direction": "higher",
                "label": "Upzoning Share",
                "description": "Of the year's rezonings, the share that raised how much building the lot allows.",
                "methodology": ("Share of passed rezonings whose new district has a higher maximum floor-area ratio than the old one "
                                "(Chicago Zoning Ordinance FAR tables, 17-2-0304, 17-3-0403, 17-4-0405, 17-5-0404; a Planned Development "
                                f"takes its underlying district's FAR per 17-8-0901). Rezonings with an unknown FAR on either side are left out. {method_tail}"),
                **common, **housing},
            "rezonings_to_multifamily_share": {
                "layer": "rezonings_use_known", "combine": "share", "flag": "to_multifamily", "scale": 100, "unit": "percent", "direction": "higher",
                "label": "Multifamily Rezonings",
                "description": "Of the year's rezonings, the share that newly allowed apartment buildings (3+ units).",
                "methodology": ("Share of passed rezonings to a district that permits multi-unit (3+) residential from one that did not "
                                f"(use tables 17-2-0207, 17-3-0207, 17-4-0207). {method_tail}"),
                **common, **housing},
            "zoned_capacity_added_sqft_per_km2": {
                "layer": "rezonings", "combine": "sum", "value": "capacity_sqft_delta", "per": "area_km2", "unit": "rate", "direction": "higher",
                "label": "Zoned Capacity Added",
                "description": "Net floor area that the year's rezonings added to what the zoning allows, in square feet per km².",
                "methodology": ("Σ (new max FAR − old max FAR) × lot area over passed rezonings, per km². Lot area = Cook County 2021 "
                                "parcels whose centroid lies in the rezoned City zoning-map polygon (else the parcels at the "
                                "ordinance's address range); "
                                f"rezonings with unknown FAR or lot area add nothing, so this is a floor. {method_tail}"),
                **common, **housing},
        },
        "fixed_geography_metrics": {},
    }


def write_conflicts(items: list[dict[str, Any]]) -> None:
    """Ordinance text vs City map conflicts, kept as parsed, for a human to adjudicate."""
    path = DATA / "review" / "map_conflicts.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["record_number", "passed_date", "address", "parsed_from", "parsed_to", "map_zone_classes",
                         "map_snapshot", "parse_method", "url", "ordinance_snippet"])
        for i in sorted(items, key=lambda i: i["row"]["passed_date"]):
            r = i["row"]
            writer.writerow([r["record_number"], r["passed_date"], r["address"], r["from_districts"], r["to_district"],
                             i["map"].get("map_classes"), i["map"].get("map_snapshot"), r["parse_method"], r["url"],
                             (r.get("ordinance_snippet") or "")[:300]])
    log(f"  {len(items)} ordinance-vs-map conflicts → {path}")


def run_build() -> None:
    items = assemble()
    placed = [i for i in items if i["point"]]
    LAYERS.mkdir(parents=True, exist_ok=True)
    features = [feature(i) for i in sorted(placed, key=lambda i: (i["row"]["passed_date"], i["row"]["record_number"]))]
    write_geojson(LAYERS / "rezonings.geojson", features)
    write_geojson(LAYERS / "rezonings_far_known.geojson", [f for f in features if f["properties"]["far_delta"] is not None])
    write_geojson(LAYERS / "rezonings_use_known.geojson", [f for f in features if f["properties"]["to_multifamily"] is not None])
    write_conflicts([i for i in placed if i["map_check"] == "disagrees" and i["district_source"] in ("parsed", "journal")
                     and i["map"].get("map_snapshot") != "2014-09"])
    earliest, first_full = coverage_years(items)
    (LAYERS / "aggregation_spec.json").write_text(json.dumps(spec(first_full, date.today().isoformat()), indent=2) + "\n")

    n = len(items)
    log(f"build: {n} passed map amendments; {len(placed)} placed ({len(placed) / max(1, n):.1%}) by {dict(Counter(i['geocode_source'] for i in placed))}")
    log(f"  districts parsed: {sum(1 for i in items if i['row']['to_district'])}; far_delta known: {sum(i['far_delta'] is not None for i in items)}; "
        f"to_multifamily known: {sum(i['to_multifamily'] is not None for i in items)}; capacity known: {sum(i['capacity_sqft_delta'] is not None for i in items)}")
    log(f"  upzoning {sum(bool(i['upzoning']) for i in items)}, downzoning {sum(bool(i['downzoning']) for i in items)}, "
        f"to_multifamily {sum(bool(i['to_multifamily']) for i in items)}, pd_or_pmd {sum(bool(i['pd_or_pmd']) for i in items)}")
    log(f"  passed by year: {sorted(Counter(i['row']['passed_date'][:4] for i in items).items())}")
    log(f"  city map check: {dict(Counter(i['map_check'] or 'no_map_polygon' for i in items))}")
    log(f"  city map check by snapshot: {dict(Counter((i['map'].get('map_snapshot') or '-', i['map_check'] or '-') for i in items))}")
    log(f"  from-district vs prior snapshot: {dict(Counter(i['map_from_check'] for i in items if i['map_from_check'] is not None))}")
    log(f"  district source: {dict(Counter(i['district_source'] for i in items))}; lot area source: {dict(Counter(i['lot_area_source'] for i in items))}")
    checked = Counter((i['row']['passed_date'][:4], bool(i['map_check'])) for i in items)
    log(f"  share checked against a City map, by year: {[(y, round(checked[(y, True)] / (checked[(y, True)] + checked[(y, False)]), 2)) for y in sorted({k[0] for k in checked})]}")
    log(f"  earliest passed year {earliest}; first full year {first_full} → spec years [{first_full}, {date.today().year}]")
    unknown = Counter(c for i in items for c in (i["row"]["from_districts"].split("+") + [i["row"]["to_district"]])
                      if c and lookup(c, load_districts()) is None)
    if unknown:
        log(f"  district codes not in districts.csv: {unknown.most_common(15)}")


def print_sample(*, n: int = 20, seed: int = 20261019) -> None:
    items = [i for i in assemble() if i["point"]]
    rng = random.Random(seed)
    sample = sorted(rng.sample(items, min(n, len(items))), key=lambda i: i["row"]["passed_date"])
    print(f"\nSpot-check sample: {len(sample)} of {len(items)} placed passed rezonings (seed {seed})\n")
    for k, i in enumerate(sample, 1):
        r = i["row"]
        sign = {True: "+", False: "-"}.get(i["upzoning"], "?") if not (i["far_delta"] == 0) else "0"
        print(f"{k:2}. {r['passed_date']} {r['record_number']:<15} {r['app_kind']:<13} {r['address'][:42]:<42}")
        print(f"    {r['district_chain'] and r['from_districts'] + ' → ' + r['district_chain'] or '(unparsed)':<40} "
              f"FAR {i['from_far']} → {i['to_far']} ({i['to_far_basis'] or '-'})  Δ {i['far_delta']} [{sign}]  "
              f"lot {i['lot_area_sqft']}  cap {i['capacity_sqft_delta']}  mf {i['to_multifamily']}  via {r['parse_method']}")
        print(f"    {r['url']}")
