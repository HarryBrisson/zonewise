from zonewise.build import classify, load_districts
from zonewise.lots import address_segments, polygon_area_sqft
from zonewise.parse import app_kind, app_number, codes_in, read_change_request, read_ordinance, zoning_address

MODERN_OCR = """SECTION 1. Title 17 of the Municipal Code of Chicago ("the Chicago Zoning
Ordinance") be amended by changing all the M1-2 Limited Manufacturing / Business
Park District symbols and indications as shown on Zoning Map Index 7-H in the area
bounded by A line 50 feet southeast of and parallel to West Terra Cotta Place; and North Clybour
Avenue, to those of an RM 4.5 Residential Multi-Unit District.
SECTION 2. This Ordinance shall be in force"""

LEGACY_OCR = """SECTION 1. Title 17 of the Municipal Code of Chicago, the Chicago Zoning Ordinance, is
hereby amended by changing all of the Ml-2, Limited Manufacturing/Business Park District
symbols and B3-2, Community Shopping District symbols as shown on Map No. 5-H in the area
bounded by blah; to those of a 83-2, Community Shopping District.
SECTION 2. This ordinance takes effect"""

TWO_STEP = """SECTION 1. Title 17 of the Municipal Code of Chicago, the Chicago Zoning Ordinance, is hereby
amended by changing all the B3-2 Community Shopping District symbols and indications as shown on
Map No. 1-F in the area bounded by X; to those of a DX-7 Downtown Mixed-Use District and a
corresponding use district is hereby established in the area above described.
SECTION 2. Title 17 is hereby amended by changing all the DX-7 Downtown Mixed-Use District symbols
and indications within the area herein above described to the designation of Residential-Business
Planned Development Number ___, which is hereby established"""


def test_ordinance_modern_ocr():
    r = read_ordinance(MODERN_OCR)
    assert r["from"] == ["M1-2"] and r["to"] == "RM-4.5"


def test_ordinance_legacy_ocr_repairs_codes():
    r = read_ordinance(LEGACY_OCR)
    assert r["from"] == ["M1-2", "B3-2"] and r["to"] == "B3-2"


def test_ordinance_two_step_pd_chain():
    r = read_ordinance(TWO_STEP)
    assert r["from"] == ["B3-2"] and r["chain"] == ["DX-7", "PD"] and r["to"] == "PD"


def test_agenda_change_requests_with_ocr_noise():
    assert read_change_request("Cl-2 Neighborhood Commerciol Dislrict 1o B3-2 Communify Shopping")["to"] == "B3-2"
    r = read_change_request("POS-1 Porks ond Open Spoces Dislricl lo RS I Residentiol Single Unif")
    assert r["from"] == ["POS-1"] and r["to"] == "RS-1"


def test_codes_reject_nonexistent_suffixes():
    assert codes_in("RS-7 and B3-4 and RM-5.5 and DX-12") == ["RM-5.5", "DX-12"]


def test_title_parsing():
    title = "Zoning Reclassification Map No. 7-H at 2515 N Clybourn Ave - App No. 22286T1"
    assert zoning_address(title)["shown"] == "2515 N Clybourn Ave"
    assert app_number(title) == "22286T1" and app_kind("22286T1") == "type_1"
    assert app_number("Zoning Reclassification Map No. 20-G at 808-820 W 87th St - A8853") == "A8853"
    assert app_kind("A8853") == "aldermanic"


def test_address_segments_carry_street_forward():
    assert address_segments("x at 2318, 2324 W Schubert Ave - App No. 1") == [
        (2318, 2318, ("W", "SCHUBERT")), (2324, 2324, ("W", "SCHUBERT"))]
    assert address_segments("x at 3126-38 W Armitage Ave") == [(3126, 3138, ("W", "ARMITAGE"))]


def test_lot_area_of_a_standard_chicago_lot():
    # 25 ft x 125 ft at Chicago's latitude
    lon0, lat0 = -87.7, 41.8
    dx, dy = 25 / 3.28084 / (111320.0 * 0.74559), 125 / 3.28084 / 110540.0
    ring = [[lon0, lat0], [lon0 + dx, lat0], [lon0 + dx, lat0 + dy], [lon0, lat0 + dy], [lon0, lat0]]
    assert abs(polygon_area_sqft({"type": "Polygon", "coordinates": [ring]}) - 3125) < 15


def _row(froms, chain):
    return {"from_districts": froms, "district_chain": chain, "to_district": chain.split(">")[-1]}


def test_classify_upzoning_to_multifamily():
    c = classify(_row("RS-3", "RM-5"), load_districts())
    assert c["far_delta"] == 1.1 and c["upzoning"] and c["to_multifamily"]


def test_classify_pd_takes_underlying_far():
    c = classify(_row("B3-2", "DX-7>PD"), load_districts())
    assert c["to_far"] == 7.0 and c["to_far_basis"] == "pd_underlying_district" and c["pd_or_pmd"]


def test_classify_direct_pd_keeps_prior_far():
    c = classify(_row("B3-2", "PD"), load_districts())
    assert c["far_delta"] == 0 and c["to_multifamily"] is None


def test_classify_downzoning():
    c = classify(_row("RT-4", "RS-3"), load_districts())
    assert c["downzoning"] and not c["upzoning"] and c["to_multifamily"] is False


# ------------------------------------------------------------------------------------------------
from zonewise.build import reconcile  # noqa: E402
from zonewise.citymap import map_to_code, norm_class  # noqa: E402


def _rez(froms, chain, method="regex_ordinance"):
    return {"from_districts": froms, "district_chain": chain, "to_district": chain.split(">")[-1] if chain else "",
            "parse_method": method}


def _map(classes, snapshot="current", dominant=None, from_class=""):
    return {"map_snapshot": snapshot, "map_classes": classes, "map_dominant_class": dominant or classes.split("+")[0],
            "map_from_class": from_class}


def test_map_codes_normalize():
    assert map_to_code("RM4.5") == "RM-4.5" and map_to_code("PD 1277") == "PD-1277" and map_to_code("PMD11") == "PMD-11"
    assert norm_class("RM-4.5") == norm_class("RM4.5") and norm_class("PD-29") == norm_class("PD")


def test_reconcile_agrees():
    row, info = reconcile(_rez("RS-3", "RM-4.5"), _map("RM4.5"))
    assert info["map_check"] == "agrees" and row["to_district"] == "RM-4.5"


def test_reconcile_adds_missed_pd_step():
    row, info = reconcile(_rez("B3-2", "B3-5"), _map("PD29"))
    assert info["district_source"] == "city_map_pd_step" and row["district_chain"] == "B3-5>PD-29"


def test_reconcile_keeps_ordinance_text_on_conflict():
    row, info = reconcile(_rez("RS-3", "B2-2"), _map("RS-3"))
    assert info["map_check"] == "disagrees" and info["district_source"] == "parsed" and row["to_district"] == "B2-2"


def test_reconcile_map_beats_agenda_request():
    row, info = reconcile(_rez("B3-1", "B3-2", method="regex_agenda"), _map("B3-3"))
    assert info["district_source"] == "city_map_override" and row["to_district"] == "B3-3"


def test_reconcile_fills_unparsed_but_not_from_weak_2014_match():
    row, info = reconcile(_rez("", ""), _map("RT-4", from_class="RS-3"))
    assert row["to_district"] == "RT-4" and row["from_districts"] == "RS-3"
    row, info = reconcile(_rez("", ""), _map("RT-4", snapshot="2014-09"))
    assert row["to_district"] == "" and info["map_check"] == "unparsed"
