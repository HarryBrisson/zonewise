import gzip
import json

import pytest

shapely = pytest.importorskip("shapely")
from shapely.geometry import Point, box  # noqa: E402

from zonewise import archive  # noqa: E402
from zonewise.citymap import Poly, index, match, snapshot_order  # noqa: E402

COPIES = [{"date": "2026-10-19"}, {"date": "2026-11-02"}, {"date": "2026-12-02"}]
ORDER = [label for label, _ in snapshot_order(COPIES)]
AS_OF = {"current": "2026-12-01", "2026-11-02": "2026-11-02", "2026-10-19": "2026-10-19",
         "2015-12": "2015-12-09", "2014-09": "2014-09-29", "2012-11": "2012-11-21"}
HERE = Point(1000, 1000)


def poly(label, zone="RM-5", app="", doc="", ord_date="2026-10-14", at=(900, 900)):
    return Poly(label, zone, app, doc, ord_date, box(at[0], at[1], at[0] + 200, at[1] + 200))


def rezoning(passed="2026-10-14", record="O2026-0012345", app="22501"):
    return {"passed_date": passed, "record_number": record, "app_no": app}


def run(snaps, r, here=HERE):
    return match(r, here, ORDER, AS_OF, *index(snaps))


def test_order_newest_copy_is_current_then_older_copies_then_portal():
    assert ORDER == ["current", "2026-11-02", "2026-10-19", "2015-12", "2014-09", "2012-11"]
    assert [label for label, _ in snapshot_order([])] == ["current", "2015-12", "2014-09", "2012-11"]


def test_superseded_rezoning_validates_against_the_copy_taken_after_it_passed():
    # passed Oct 14; rezoned again in November, so its tag is gone from the current map
    snaps = {"current": [poly("current", zone="PD", app="22777", ord_date="2026-11-18")],
             "2026-11-02": [poly("2026-11-02", app="22501")],
             "2026-10-19": [poly("2026-10-19", app="22501")]}
    hits = run(snaps, rezoning())
    assert [p.label for p in hits] == ["2026-11-02"] and hits[0].zone_class == "RM-5"


def test_archive_matches_clerk_doc_like_current_and_by_its_own_label():
    snaps = {"current": [], "2026-11-02": [poly("2026-11-02", doc="O2026-0012345")], "2026-10-19": []}
    hits = run(snaps, rezoning(app=""))
    assert [p.label for p in hits] == ["2026-11-02"]
    # a substitute (SO…) record matches the doc tagged without the S
    assert run(snaps, rezoning(record="SO2026-0012345", app=""))[0].label == "2026-11-02"


def test_copy_taken_before_the_vote_is_skipped():
    # a reused app number in a copy that predates the vote is not this rezoning
    snaps = {"current": [], "2026-11-02": [], "2026-10-19": [poly("2026-10-19", app="22501", ord_date="2026-10-14")]}
    assert run(snaps, rezoning(passed="2026-10-28")) == []


def test_archive_guards_date_and_distance():
    snaps = {"current": [], "2026-10-19": [],
             "2026-11-02": [poly("2026-11-02", app="22501", ord_date="2025-03-01"),       # stale tag
                            poly("2026-11-02", app="22501", at=(90000, 90000))]}         # elsewhere in the city
    assert run(snaps, rezoning()) == []
    assert len(run(snaps, rezoning(), here=None)) == 1  # no point: only the date guard applies


def test_current_still_wins_when_the_tag_survives():
    snaps = {"current": [poly("current", app="22501")], "2026-11-02": [poly("2026-11-02", app="22501")], "2026-10-19": []}
    assert run(snaps, rezoning())[0].label == "current"


# ------------------------------------------------------------------------------------------------
GEOJSON = json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"zone_class": "RM-5", "ordinance": "22501"},
     "geometry": {"type": "Point", "coordinates": [-87.6, 41.9]}}]}).encode()


@pytest.fixture
def fake_portal(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path)
    monkeypatch.setattr(archive, "MANIFEST", tmp_path / "manifest.jsonl")
    state = {"updated": 1791900176, "body": GEOJSON, "downloads": 0}
    monkeypatch.setattr(archive, "get_json", lambda url, **_: {"rowsUpdatedAt": state["updated"]})

    def download(url, **_):
        state["downloads"] += 1
        return state["body"]

    monkeypatch.setattr(archive, "http_get", download)
    return state


def test_archive_saves_once_then_skips_while_rows_updated_at_is_unchanged(fake_portal, tmp_path):
    entry = archive.archive_current_map(today="2026-10-19")
    assert entry["path"] == "dj47-wfun_2026-10-19.geojson.gz" and entry["rows"] == 1
    assert gzip.decompress((tmp_path / entry["path"]).read_bytes()) == GEOJSON
    assert archive.archive_current_map(today="2026-11-02") is None
    assert fake_portal["downloads"] == 1
    assert [c["date"] for c in archive.archived_copies()] == ["2026-10-19"]


def test_archive_records_but_does_not_duplicate_identical_content(fake_portal):
    archive.archive_current_map(today="2026-10-19")
    fake_portal["updated"] += 1
    entry = archive.archive_current_map(today="2026-11-02")
    assert entry["path"] is None and entry["rows_updated_at"] == fake_portal["updated"]
    fake_portal["updated"] += 1
    fake_portal["body"] = GEOJSON.replace(b"RM-5", b"RM-6")
    archive.archive_current_map(today="2026-12-02")
    assert [c["date"] for c in archive.archived_copies()] == ["2026-10-19", "2026-12-02"]
    assert archive.read_copy(archive.archived_copies()[-1])["features"][0]["properties"]["zone_class"] == "RM-6"


def test_refresh_refetches_only_new_or_changed_matter_details():
    from zonewise.elms import stale_details

    rows = [{"matterId": "a", "status": "90-Final", "subStatus": "Passed"},
            {"matterId": "b", "status": "90-Final", "subStatus": "Passed", "lastPublicationDate": "2026-11-03"},
            {"matterId": "c", "status": "10-Pending"}]
    details = {"a": {"status": "90-Final", "subStatus": "Passed"},
               "b": {"status": "10-Pending", "subStatus": "In Committee", "lastPublicationDate": "2026-10-01"}}
    assert stale_details(rows, details) == ["b", "c"]
