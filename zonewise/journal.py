"""City Council Journal of the Proceedings: the passed text of an ordinance, read at the page eLMS
cites for its "Passed" action (`actions[].journalPage`, e.g. "[C.J.p. 118289]").

The Clerk publishes each meeting's Journal as one or more PDFs (Vol. I / Vol. II, ~30 MB, text
layer) listed at chicityclerk.com/taxonomy/term/16. eLMS meetings carry no Journal attachment.
Printed page numbers run on across a council term, and every page's first line carries one:
"5/4/2011   REPORTS OF COMMITTEES   118289" or "118290   JOURNAL--CITY COUNCIL--CHICAGO   5/4/2011".
The cited page is where the item starts (or the committee report it sits in), so we read forward
from it to the page that carries the record number tag ("[SO2011-1472]") and the clause after it.

Caches (append-only JSONL under data/raw/):
  journal_index.jsonl  one line per listing page: {url, fetched_at, html}
  journal_pages.jsonl  one line per (matter, Journal page read): {matter_id, record_number,
                       meeting_date, journal_page, pdf_url, pdf_sha256, pdf_page_index,
                       printed_page, method, text}. The PDF itself is not kept.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .cache import RAW, append_jsonl, http_get, now_iso, read_jsonl
from .elms import _ocr_pdf, _pdftotext, _page_count, log

CLERK = "https://www.chicityclerk.com"
LISTING = f"{CLERK}/taxonomy/term/16"
INDEX = RAW / "journal_index.jsonl"
PAGES = RAW / "journal_pages.jsonl"
READ_AHEAD = 60  # pages past the cited one to look for the record's tag
AFTER_TAG = 2    # pages kept past the tag's page: a long boundary description runs on


# ------------------------------------------------------------------------------------------------
# Listing → {meeting date: [pdf urls]}
# ------------------------------------------------------------------------------------------------
def fetch_index(*, refresh: bool = False) -> None:
    if INDEX.exists() and not refresh:
        return
    INDEX.unlink(missing_ok=True)
    page = 0
    while True:
        url = f"{LISTING}?page={page}"
        body = http_get(url, accept="text/html").decode("utf-8", "replace")
        if 'class="node-title"' not in body:
            break
        append_jsonl(INDEX, {"url": url, "fetched_at": now_iso(), "html": body})
        page += 1
    log(f"journal index: {page} listing pages")


TITLE_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")  # 2018 titles: "04/18/18"


def journal_index() -> dict[str, list[dict[str, Any]]]:
    """{YYYY-MM-DD: [{title, pdf_url, size}]} from the cached listing pages."""
    out: dict[str, list[dict[str, Any]]] = {}
    seen = set()
    for record in read_jsonl(INDEX):
        for article in record["html"].split("<article")[1:]:
            title = re.search(r'class="node-title">\s*<a[^>]*><span>([^<]+)</span>', article)
            if not title:
                continue
            title = htmllib.unescape(title.group(1)).strip()
            m = TITLE_DATE.search(title)
            if not m:
                continue
            year = m.group(3) if len(m.group(3)) == 4 else f"20{m.group(3)}"
            day = f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            for href, size in re.findall(r'<a href="(https://[^"]+\.pdf[^"]*)"[^>]*>[^<]*</a></span>\s*<span>\(([^)]*)\)', article):
                href = htmllib.unescape(href)
                if href in seen:
                    continue
                seen.add(href)
                out.setdefault(day, []).append({"title": title, "pdf_url": href, "size": size})
    return out


# ------------------------------------------------------------------------------------------------
# Journal pages
# ------------------------------------------------------------------------------------------------
RUNNING_DATE = re.compile(r"\d{1,2}\s?\S\s?\d{1,2}\s?\S?\s?(?:19|20)\d\d\b")  # "5/4/2011"; OCR'd: "7t24t2019", "11t17 12021"
HEAD_NUMBER = re.compile(r"^\s*((?:\d+ {1,3})*\d+)\s+\D|\D\s+((?:\d+ {1,3})*\d+)\s*$")  # OCR'd: "1   6302" = 16302


def printed_number(page: str) -> int | None:
    """The printed Journal page number on the page's first non-empty line."""
    for line in page.splitlines():
        if line.strip():
            if not RUNNING_DATE.search(line):
                return None
            m = HEAD_NUMBER.search(line)
            number = int((m.group(1) or m.group(2)).replace(" ", "")) if m else None
            return number if number is not None and number < 250000 else None  # not the date read as one
    return None


OCR_DIGIT = {"0": "[0OoQD]", "1": "[1lIi|!]", "2": "[2Z]", "3": "3", "4": "4", "5": "[5Ss]", "6": "[6b]",
             "7": "7", "8": "[8B&a]", "9": "[9eg]"}  # OCR'd 2019+ tags: "lo201e-54701" = [O2019-5470]


def record_tag(record_number: str) -> re.Pattern[str]:
    """'[SO2011-1472]' as it comes out of the text layer ('[02011-1474]', '[S02019-270]')."""
    m = re.match(r"(S?)O(\d{4})-0*(\d+)", record_number)
    prefix, year, serial = m.groups()
    s = "[S5s]?" if prefix else ""  # a substitute is sometimes printed without its S
    year = "".join(OCR_DIGIT[c] for c in year)
    digits = r"\s?".join(OCR_DIGIT[c] for c in serial)  # "[S02013-27 41]"
    return re.compile(rf"[\[(|l1]\s*{s}[O0Qo]\s?{year}\s?[-–,;.:]{{1,3}}\s?[0Oo]*{digits}\s*[\])|l1IJ]")  # "[SO2021,;.5150]", "lso2021-3e76I"


def _pages_of(pdf: Path) -> tuple[str, list[str]]:
    count = _page_count(pdf)
    pages = _pdftotext(pdf, count)
    if sum(len(re.findall(r"[A-Za-z]", p)) for p in pages[:20]) > 2000:
        return "pdftotext", pages
    return "vision_ocr", _ocr_pdf(pdf, count)  # not hit so far: every 2011+ Journal has a text layer


class Journal:
    """One downloaded Journal PDF, its pages' text, and its printed page range."""

    def __init__(self, url: str):
        body = http_get(quote(url, safe=":/?&=%~"), timeout=600, accept="application/pdf")  # "2024_7_17 Journal of …pdf"
        self.url, self.sha256 = url, hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "journal.pdf"
            pdf.write_bytes(body)
            self.method, self.pages = _pages_of(pdf)
        self.numbers = [printed_number(p) for p in self.pages]

    def index_of(self, printed: int) -> int | None:
        hits = [i for i, n in enumerate(self.numbers) if n == printed]
        if hits:
            return hits[0]
        # the cited page's running head didn't read (OCR): count on from the nearest numbered page
        # before it, starting two pages early since OCR'd heads also misread neighbours' numbers
        below = [(printed - n, i) for i, n in enumerate(self.numbers) if n is not None and 0 < printed - n <= 20]
        if not below:
            return None
        gap, i = min(below)
        return max(0, i + gap - 2)


def app_line(app_no: str | None) -> re.Pattern[str] | None:
    """'(Application No. 17398)' / '(Application No. A-7689)': the fallback anchor when the
    Journal mistypes the record tag ([SO2012-9729] for SO2011-9729)."""
    if not app_no:
        return None
    m = re.match(r"([A-Z]?)-?(\d+)", app_no)
    letter, digits = m.groups()
    digits = "".join(OCR_DIGIT[c] for c in digits)  # "(Application No. 1a633)"
    return re.compile(rf"Application\s+No\.?\s*{letter + r'\s?-?\s?' if letter else ''}{digits}(?![0-9])", re.I)


def _anchor(text: str, record_number: str, app_no: str | None) -> re.Match[str] | None:
    m = record_tag(record_number).search(text)
    if not m and app_line(app_no):
        m = app_line(app_no).search(text)
    return m


def read_cited(journal: Journal, record_number: str, journal_page: int | None, app_no: str | None = None) -> list[int] | None:
    """PDF page indexes from the cited page through the one after the record's tag (the clause
    can run onto the next page); None when the cited page isn't in this PDF. With no cited page
    (eLMS left journalPage blank) the tag is looked for anywhere in the PDF."""
    tag = record_tag(record_number)
    if journal_page is None:
        hits = [i for i, p in enumerate(journal.pages) if tag.search(p) and re.search(r"SECTION\s+1", p[tag.search(p).end():])]
        return [i for i in range(hits[0], hits[0] + 1 + AFTER_TAG) if i < len(journal.pages)] if hits else None
    start = journal.index_of(journal_page)
    if start is None:
        return None
    for i in range(start, min(len(journal.pages), start + READ_AHEAD)):
        if _anchor(journal.pages[i], record_number, app_no):
            return list(range(start, min(len(journal.pages), i + 1 + AFTER_TAG)))
    return [start]  # the cited page, kept as read, though the tag isn't on it or after it


def target(matter_id: str, detail: dict[str, Any], passed_date: str) -> dict[str, Any]:
    """What to read for one passed matter: its Passed action's meeting and cited page (blank in
    eLMS for a few recent matters: then the meeting is the passed date and the tag is searched)."""
    action = passed_action(detail) or {}
    from .parse import app_number

    return {"matter_id": matter_id, "record_number": detail["recordNumber"], "app_no": app_number(detail.get("title") or ""),
            "meeting_date": meeting_date(action) if action.get("actionDate") else passed_date,
            "journal_page": (action.get("journalPage") or "").strip() or None}


def passed_action(detail: dict[str, Any]) -> dict[str, Any] | None:
    for action in detail.get("actions") or []:
        if re.match(r"passed", action.get("actionName") or "", re.I) and "council" in (action.get("actionByName") or "").lower():
            return action
    return None


def _complete(pages: list[dict[str, Any]], t: dict[str, Any]) -> bool:
    """A cached read is complete once the Journal's to-clause reads from it; one that stopped
    short (an earlier, narrower tag pattern or page window) is read again."""
    return bool(pages) and bool(read_passed(t["record_number"], _dedup(pages), t.get("app_no")))


def fetch_journal_pages(targets: list[dict[str, Any]]) -> None:
    """targets: [{matter_id, record_number, meeting_date, journal_page}]. Downloads each meeting's
    Journal PDF(s) once, caches the cited page(s) per matter, and logs misses."""
    fetch_index()
    index = journal_index()
    cached = cached_pages()
    # a matter counts as read once its tag (or application line) is on a cached page; a read that
    # stopped at the cited page is retried after the tag pattern improves
    have = {t["matter_id"] for t in targets if _complete(cached.get(t["matter_id"], []), t)}
    todo = [t for t in targets if t["matter_id"] not in have]
    by_date: dict[str, list[dict[str, Any]]] = {}
    for t in todo:
        by_date.setdefault(t["meeting_date"], []).append(t)
    log(f"journal pages: {len(have)} matters cached, {len(todo)} to read over {len(by_date)} meetings")
    for day, items in sorted(by_date.items()):
        pdfs = index.get(day) or []
        if not pdfs:  # an action stamped near midnight UTC can land a day off the meeting
            pdfs = [e for d in (_shift(day, -1), _shift(day, 1)) for e in index.get(d) or []]
        if not pdfs:
            log(f"  {day}: no Journal listed for this meeting ({[t['record_number'] for t in items]})")
            continue
        left = list(items)
        for entry in pdfs:
            if not left:
                break
            try:
                journal = Journal(entry["pdf_url"])
            except Exception as error:  # noqa: BLE001
                log(f"  {day} {entry['pdf_url']}: {type(error).__name__}: {error}")
                continue
            numbered = [n for n in journal.numbers if n is not None]
            log(f"  {day} {entry['title']}: {len(journal.pages)} pages, printed {min(numbered, default=None)}–{max(numbered, default=None)}")
            for t in list(left):
                indexes = read_cited(journal, t["record_number"], int(t["journal_page"]) if t.get("journal_page") else None, t.get("app_no"))
                if indexes is None:
                    continue
                left.remove(t)
                for i in indexes:
                    append_jsonl(PAGES, {"matter_id": t["matter_id"], "record_number": t["record_number"],
                                         "meeting_date": day, "journal_page": int(t["journal_page"]) if t.get("journal_page") else None,
                                         "pdf_url": journal.url, "pdf_title": entry["title"], "pdf_sha256": journal.sha256,
                                         "pdf_page_index": i, "printed_page": journal.numbers[i],
                                         "method": journal.method, "fetched_at": now_iso(), "text": journal.pages[i]})
        for t in left:
            log(f"  {day}: cited page {t['journal_page']} for {t['record_number']} not found in {len(pdfs)} PDF(s)")


def cached_pages() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in read_jsonl(PAGES):
        out.setdefault(r["matter_id"], []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["pdf_page_index"])
    return out


def _strip_header(page: str) -> str:
    """Drop the running head ("118290  JOURNAL--CITY COUNCIL--CHICAGO  5/4/2011") so a clause
    that runs across a page break reads as one."""
    lines = page.splitlines()
    for i, line in enumerate(lines):
        if line.strip():
            if printed_number(line) is not None:
                del lines[i]
            break
    return "\n".join(lines)


def passed_text(record_number: str, pages: list[dict[str, Any]], app_no: str | None = None) -> str | None:
    """The record's own item from the cached pages: from its tag (else its application line) to
    the next item's heading."""
    text = "\n".join(_strip_header(p["text"]) for p in pages)
    m = _anchor(text, record_number, app_no)
    if not m:
        return None
    rest = text[m.end():]
    body = re.search(r"Ordained|SECTION\s*1", rest)
    skip = body.start() if body and body.start() < 1500 else 0
    end = re.search(r"\n[^\n]*Reclassification\s+Of\s+Area|\n[^\n]*\(Application\s+No\.|\[\s*[S5]?[O0Q]\s?\d{4}\s?-\s?\d+\s*\]", rest[skip:])
    return rest[: skip + end.start()] if end else rest[: skip + 20000]  # a PD's boundary runs pages


def _shift(day: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def meeting_date(action: dict[str, Any]) -> str:
    stamp = datetime.fromisoformat(action["actionDate"].replace("Z", "+00:00"))
    from .parse import CHICAGO

    return stamp.astimezone(CHICAGO).strftime("%Y-%m-%d")


# ------------------------------------------------------------------------------------------------
# Adjudication: which reading does the passed text support?
# ------------------------------------------------------------------------------------------------
def normalize(text: str) -> str:
    """Typesetting quirks of the Journal's text layer that the ordinance reader doesn't expect:
    letter-spaced codes ("R S 3", "B 1-2", "RM4 .5", "aB2-2", "S E C T I O N"), OCR'd "RIMS", 'a"' for "all", a dropped "of" ("to those an M2-2")."""
    text = re.sub(r"\bR\s(S|T|M)\s?(\d)", r"R\1\2", text)
    text = re.sub(r"\bRI[MV](?=[S\d]\b)", "RM", text)  # OCR'd "RIMS" = RM5
    text = re.sub(r"\b([BCM])\s([123]\s?-)", r"\1\2", text)  # "B 1-2"
    text = re.sub(r"S\s?E\s?C\s?T\s?I\s?O\s?N", "SECTION", text)
    text = re.sub(r"\bchanging\s+a\"\s", "changing all ", text)  # 'a"' for "all"
    text = re.sub(r"\b(of\s+an?)(?=[A-Z]{1,2}\d)", r"\1 ", text)  # "to those of aB2-2"
    text = re.sub(r"(\d)\s+\.(\d)", r"\1.\2", text)  # "RM4 .5"
    return re.sub(r"\bto\s+those\s+(?=an?\b)", "to those of ", text)


def read_passed(record_number: str, pages: list[dict[str, Any]], app_no: str | None = None) -> dict[str, Any] | None:
    from .parse import read_ordinance

    text = passed_text(record_number, pages, app_no)
    if not text:
        return None
    read = read_ordinance(normalize(text))
    if read:
        return read
    # the from-district can be OCR noise ("tU1-2 Limited lt/anufacturing"); the to-clause is what
    # the adjudication needs, so take it alone when the full read fails
    from .parse import ORD_TO, _flat, codes_in, repair_by_name

    tos = [codes_in(m.group("to")) for m in ORD_TO.finditer(repair_by_name(_flat(normalize(text))))]
    first = next((c[0] for c in tos if c), None)
    return {"from": [], "chain": [first], "to": first, "snippet": ""} if first else None


def verdict(journal_to: str | None, journal_chain: list[str], parsed_to: str, map_classes: list[str]) -> str:
    """ordinance_pdf | city_map | both | neither | unread, comparing on the City map's class scheme
    (every PD flavour one class) and letting any step of a two-step chain match the map."""
    from .citymap import norm_class

    if not journal_to:
        return "unread"
    classes = {norm_class(c) for c in map_classes if c}
    pdf = bool(parsed_to) and norm_class(journal_to) == norm_class(parsed_to)
    on_map = bool(classes) and (norm_class(journal_to) in classes or any(norm_class(c) in classes for c in journal_chain))
    return {(True, True): "both", (True, False): "ordinance_pdf", (False, True): "city_map"}.get((pdf, on_map), "neither")


ADJUDICATION = RAW.parent / "review" / "journal_adjudication.csv"
SPOT_SEED = 20261019


def is_conflict(item: dict[str, Any]) -> bool:
    """An ordinance-text reading of the to-district that the City map contradicts, before any
    Journal-based policy: map_check 'disagrees' on a solid map match, read from the ordinance PDF."""
    return (item["map_check"] == "disagrees" and item["row"]["parse_method"] == "regex_ordinance"
            and item["map"].get("map_snapshot") not in (None, "", "2014-09")
            and item["district_source"] in ("parsed", "city_map_substitute"))


def ledger() -> dict[str, dict[str, str]]:
    """The adjudication file as last written: {record_number: row}. A conflict once adjudicated
    stays in it (with the reading that was flagged) after a re-parse or a policy resolves it."""
    import csv

    if not ADJUDICATION.exists():
        return {}
    return {r["record_number"]: r for r in csv.DictReader(ADJUDICATION.open())}


def adjudication_sets(items: list[dict[str, Any]], *, spot_n: int = 30) -> list[tuple[str, dict[str, Any]]]:
    import random

    kept = {k for k, r in ledger().items() if r["set"] == "map_conflict"}
    conflicts = [("map_conflict", i) for i in items if is_conflict(i) or i["row"]["record_number"] in kept]
    unchecked = sorted((i for i in items if i["map_check"] is None), key=lambda i: i["row"]["matter_id"])
    spot = random.Random(SPOT_SEED).sample(unchecked, min(spot_n, len(unchecked)))
    return conflicts + [("no_map_spot_check", i) for i in spot]


def run_journal(*, spot_n: int = 30) -> None:
    """Read the Journal for every ordinance-vs-map conflict (+ a random sample of rezonings the map
    can't check) and write data/review/journal_adjudication.csv."""
    import csv
    from collections import Counter

    from .build import assemble
    from .elms import cached_details, legislation_attachments

    details = cached_details()
    before = ledger()
    chosen = adjudication_sets(assemble(), spot_n=spot_n)
    fetch_journal_pages([target(i["row"]["matter_id"], details[i["row"]["matter_id"]], i["row"]["passed_date"]) for _, i in chosen])
    pages = cached_pages()
    rows = []
    for kind, item in chosen:
        r, detail = item["row"], details[item["row"]["matter_id"]]
        mine = _dedup(pages.get(r["matter_id"], []))
        app = target(r["matter_id"], detail, r["passed_date"])["app_no"]
        parsed_to = item["parsed_to_district"] or ""
        prev = before.get(r["record_number"]) or {}
        flagged_to = prev.get("flagged_to") or prev.get("parsed_to") or parsed_to  # the reading the map contradicted
        read = read_passed(r["record_number"], mine, app) if mine else None
        text = passed_text(r["record_number"], mine, app) if mine else None
        map_classes = [c for c in (item["map"].get("map_classes") or "").split("+") if c]
        if kind == "map_conflict":
            supports = verdict(read and read["to"], read["chain"] if read else [], flagged_to, map_classes)
        else:
            from .citymap import norm_class

            supports = ("unread" if not read else "unparsed" if not parsed_to
                        else "agrees" if norm_class(read["to"]) == norm_class(parsed_to) else "disagrees")
        action = passed_action(detail) or {}
        rows.append({
            "set": kind, "record_number": r["record_number"], "passed_date": r["passed_date"], "address": r["address"],
            "passed_action": action.get("actionName") or "", "parsed_from": r["from_districts"],
            "flagged_to": flagged_to if kind == "map_conflict" else "", "parsed_to": parsed_to,
            "parse_method": r["parse_method"], "ordinance_file": r.get("ordinance_file", ""),
            "substitute_unread": r.get("substitute_unread", ""),
            "ordinance_pdfs": " | ".join(a.get("fileName") or "" for a in legislation_attachments(detail)),
            "map_zone_classes": "+".join(map_classes), "map_snapshot": item["map"].get("map_snapshot") or "",
            "journal_from": "+".join(read["from"]) if read else "", "journal_chain": ">".join(read["chain"]) if read else "",
            "journal_to": read["to"] if read else "", "journal_supports": supports,
            "journal_cited_page": mine[0]["journal_page"] if mine else "",
            "journal_pages_read": " ".join(str(p["printed_page"]) for p in mine),
            "journal_pdf": mine[0]["pdf_url"] if mine else "",
            "journal_clause": re.sub(r"\s+", " ", normalize(text or ""))[:400],
            "published_to": r["to_district"], "district_source": item["district_source"] or "",
            "url": r["url"],
        })
    ADJUDICATION.parent.mkdir(parents=True, exist_ok=True)
    with ADJUDICATION.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for kind in ("map_conflict", "no_map_spot_check"):
        log(f"  {kind}: {dict(Counter(r['journal_supports'] for r in rows if r['set'] == kind))}")
    log(f"  → {ADJUDICATION}")


def _dedup(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per PDF page (a rerun that re-reads a matter appends; the latest read wins)."""
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for p in pages:
        latest[(p["pdf_url"], p["pdf_page_index"])] = p
    return sorted(latest.values(), key=lambda p: p["pdf_page_index"])
