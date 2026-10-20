from zonewise.journal import app_line, normalize, passed_text, printed_number, read_passed, record_tag, verdict
from zonewise.parse import codes_in


def test_running_head_page_numbers_including_ocr():
    assert printed_number("5/4/2011                      REPORTS OF COMMITTEES                     118289\n") == 118289
    assert printed_number("118290                   JOURNAL-CITY COUNCIL-CHICAGO                     5/4/2011") == 118290
    assert printed_number("7t24t2019                    REPORTS OF COMMITTEES            3357") == 3357
    assert printed_number("1   6302              JOU RNAL.-CITY COU NC I L.-CH I CAGO           4t24t2020") == 16302
    assert printed_number("41526   JOU RNAL--CITY COU NCI L..CH I CAGO      11t17 t2021") == 41526
    assert printed_number("Reclassification Of Area Shown On Map No. 10-E.") is None  # not a running head


def test_record_tag_survives_ocr():
    assert record_tag("SO2011-1472").search("[SO2011-1472]")
    assert record_tag("O2011-1474").search("[02011-1474]")
    assert record_tag("SO2013-2741").search("[S02013-27 41]")
    assert record_tag("O2019-5470").search("lo201e-54701")
    assert record_tag("SO2021-3976").search("lso2021-3e76I")
    assert record_tag("SO2016-5617").search("[02016-5617]")  # a substitute printed without its S
    assert not record_tag("O2011-1474").search("[02011-14745]")
    assert app_line("18633").search("(Application No. 1a633)") and app_line("A-8243").search("(Application No. A-8243)")


def test_journal_normalize():
    assert codes_in(normalize("to those of a B 1-2 Neighborhood")) == ["B1-2"]
    assert codes_in(normalize("an RM4 .5 Residential")) == ["RM-4.5"]
    assert codes_in(normalize("an RIMS Residential")) == ["RM-5"]
    assert "to those of an M2-2" in normalize("to those an M2-2")


PAGE_1 = """5/4/2011                      REPORTS OF COMMITTEES                                    118289

                  Reclassification Of Area Shown On Map No. 10-E.
                                     (As Amended)
                                 (Application No. 17215)
                                                                               [SO2011-1472]

 Be It Ordained by the City Council of the City of Chicago:

   SECTION 1. Title 17 of the Municipal Code of Chicago, the Chicago Zoning Ordinance, is
 hereby amended by changing all of the RM5 Residential Multi-Unit District symbols and
 indications as shown on Map Number 10-E in the area bounded by:

    East Pershing Road; the alley next east of and parallel to South Dr. Martin Luther"""
PAGE_2 = """118290                   JOURNAL-CITY COUNCIL-CHICAGO                            5/4/2011

    King, Jr. Drive,

 to those of a B3-1 Community Shopping District and a corresponding use district is hereby
 established in the area above described.

                  Reclassification Of Area Shown On Map No. 10-1.
                                 (Application No. 17224)
                                                                                 [02011-1474]
 Be It Ordained by the City Council of the City of Chicago:
   SECTION 1. ... changing all of the B3-1 Community Shopping District ... to those of a C2-1 Motor Vehicle"""


def test_passed_text_spans_the_page_break_and_stops_at_the_next_item():
    pages = [{"text": PAGE_1, "pdf_page_index": 0}, {"text": PAGE_2, "pdf_page_index": 1}]
    text = passed_text("SO2011-1472", pages)
    assert "118290" not in text and "C2-1" not in text
    read = read_passed("SO2011-1472", pages)
    assert read["from"] == ["RM-5"] and read["to"] == "B3-1"


def test_verdict():
    assert verdict("B3-1", ["B3-1"], "C1-1", ["B3-1"]) == "city_map"
    assert verdict("B2-5", ["B2-5"], "B2-5", ["C1-2"]) == "ordinance_pdf"
    assert verdict("RS-3", ["RS-3"], "B2-2", ["B1-1"]) == "neither"
    assert verdict(None, [], "B2-2", ["B1-1"]) == "unread"
    assert verdict("PD-43", ["PD-43"], "PD-43", ["PD 43"]) == "both"
