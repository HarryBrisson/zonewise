"""City Clerk eLMS: every zoning-reclassification matter, its detail record, and the text of its
ordinance PDF; plus every Zoning Committee meeting's agenda/summary text (a second, independent
statement of each "Change Request").

The query is copied from ward-wise-civic-tech apps/ward_wise_explorer/services/alerts_digest.py
(`load_zoning`): `matterCategory eq 'ZONING RECLASSIFICATIONS'`. That exact match only returns the
records created in eLMS itself (late 2022 on). Matters migrated from the old Legistar system carry
the category `ZONING RECLASSIFICATIONS | Map No.  7-G`, so we page the string range
['ZONING RECLASSIFICATIONS', 'ZONING RECLASSIFICATIONT') instead, a superset of the exact match, and
keep the exact-match flag on each record.

Caches (append-only JSONL under data/raw/):
  elms_matter_pages.jsonl    one line per list page: {url, fetched_at, response}
  elms_matter_details.jsonl  one line per matter:    {matter_id, url, fetched_at, response}
  elms_attachment_text.jsonl one line per PDF:       {matter_id, path, file_name, sha256, method, pages[]}
  elms_meetings.jsonl        one line per page of Zoning Committee meetings
  elms_meeting_text.jsonl    one line per agenda/summary PDF
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .cache import RAW, append_jsonl, get_json, http_get, now_iso, read_jsonl, run_lock

CLERK_API = "https://api.chicityclerkelms.chicago.gov"
CLERK_SITE = "https://chicityclerkelms.chicago.gov"
EXACT_FILTER = "matterCategory eq 'ZONING RECLASSIFICATIONS'"  # alerts_digest.load_zoning
RANGE_FILTER = "matterCategory ge 'ZONING RECLASSIFICATIONS' and matterCategory lt 'ZONING RECLASSIFICATIONT'"
ZONING_COMMITTEE = "Committee on Zoning, Landmarks and Building Standards"
PAGE = 500  # the API's maximum `top`

PAGES = RAW / "elms_matter_pages.jsonl"
DETAILS = RAW / "elms_matter_details.jsonl"
ATTACHMENT_TEXT = RAW / "elms_attachment_text.jsonl"
MEETINGS = RAW / "elms_meetings.jsonl"
MEETING_TEXT = RAW / "elms_meeting_text.jsonl"

TEXT_PAGES = 12  # legacy PDFs bundle tracking sheet + application + ordinance up front, EDS after
OCR_PAGES = 3    # image-only PDFs: the ordinance is page 1 (modern "Ordinance.pdf" is one page)
OCR_PAGES_MEETING = 40


def log(*parts: Any) -> None:
    print(*parts, file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------------------------
# Matter list + details
# ------------------------------------------------------------------------------------------------
def fetch_matter_pages(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Every zoning matter (list rows). Cached pages are reused unless refresh."""
    if PAGES.exists() and not refresh:
        return matters_from_pages()
    PAGES.unlink(missing_ok=True)
    skip = 0
    while True:
        url = f"{CLERK_API}/matter?{urlencode({'filter': RANGE_FILTER, 'sort': 'recordNumber asc', 'top': PAGE, 'skip': skip})}"
        response = get_json(url)
        append_jsonl(PAGES, {"url": url, "fetched_at": now_iso(), "response": response})
        rows = response.get("data") or []
        log(f"matters skip={skip} got={len(rows)} total={response.get('meta', {}).get('count')}")
        if len(rows) < PAGE:
            break
        skip += PAGE
    return matters_from_pages()


def matters_from_pages() -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for page in read_jsonl(PAGES):
        for row in page["response"].get("data") or []:
            seen[row["matterId"]] = row
    return list(seen.values())


def cached_details() -> dict[str, dict[str, Any]]:
    """{matterId: detail}. The detail endpoint returns its JSON double-encoded (a JSON string);
    the cache keeps it as received and decodes here."""
    out = {}
    for record in read_jsonl(DETAILS):
        response = record["response"]
        out[record["matter_id"]] = json.loads(response) if isinstance(response, str) else response
    return out


def fetch_details(matter_ids: list[str], *, workers: int = 8) -> None:
    have = set(cached_details())
    todo = [m for m in matter_ids if m not in have]
    log(f"details: {len(have)} cached, {len(todo)} to fetch")

    def one(matter_id: str) -> None:
        url = f"{CLERK_API}/matter/{matter_id}"
        append_jsonl(DETAILS, {"matter_id": matter_id, "url": url, "fetched_at": now_iso(), "response": get_json(url)})

    _run_pool(todo, one, workers, "details")


# ------------------------------------------------------------------------------------------------
# PDF text (text layer first, macOS Vision OCR when the PDF is a bare scan)
# ------------------------------------------------------------------------------------------------
def _pdftotext(pdf: Path, last_page: int) -> list[str]:
    out = subprocess.run(["pdftotext", "-layout", "-l", str(last_page), str(pdf), "-"],
                         capture_output=True, text=True, errors="replace").stdout
    return out.split("\f")[:last_page]


def _page_count(pdf: Path) -> int:
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, errors="replace").stdout
    match = re.search(r"^Pages:\s+(\d+)", info, re.M)
    return int(match.group(1)) if match else 0


def _ocr_png(path: Path) -> str:
    import objc  # pyobjc; macOS only
    import Vision
    from Foundation import NSURL

    with objc.autorelease_pool():
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)  # it "corrects" RM-5 and B3-2 into words
        handler.performRequests_error_([request], None)
        rows = []
        for observation in request.results() or []:
            box = observation.boundingBox()
            rows.append((round(1 - box.origin.y - box.size.height, 3), round(box.origin.x, 3),
                         str(observation.topCandidates_(1)[0].string())))
    rows.sort()
    return "\n".join(row[2] for row in rows)


def _ocr_pdf(pdf: Path, pages: int) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", "-l", str(pages), str(pdf), f"{tmp}/p"],
                       check=True, capture_output=True)
        return [_ocr_png(png) for png in sorted(Path(tmp).glob("p-*.png"))]


def _usable(pages: list[str]) -> bool:
    return sum(len(re.findall(r"[A-Za-z]", p)) for p in pages) > 300


PREFIX_BYTES = 256 * 1024
ANCHOR = re.compile(r"chang\w*\s+a\S{1,2}\s|to\s+those\s+of|Present\s+Zoning|Change\s+Request", re.I)


def _extract(body: bytes, *, text_pages: int, ocr_pages: int, allow_ocr: bool) -> tuple[str, list[str], int]:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "doc.pdf"
        pdf.write_bytes(body)
        pages = _pdftotext(pdf, text_pages)
        if _usable(pages) or not allow_ocr:
            return "pdftotext", pages, _page_count(pdf)
        return "vision_ocr", _ocr_pdf(pdf, ocr_pages), _page_count(pdf)


def pdf_text(url: str, *, text_pages: int, ocr_pages: int, prefix: bool = True) -> dict[str, Any]:
    """{sha256, bytes, total_bytes, page_count, method, pages[]} for one PDF; the PDF is not kept.

    Legacy PDFs bundle the ordinance and application up front, so the first 256 KB (an HTTP Range
    request; poppler rebuilds the missing xref) usually holds them: if that prefix has a text layer
    with an ordinance/application anchor we stop there (method "pdftotext_prefix", sha256 of the
    prefix). Otherwise the whole file is fetched, and OCR'd when it has no text layer."""
    if prefix:
        meta: dict[str, Any] = {}
        body = http_get(url, timeout=180, accept="application/pdf", byte_range=PREFIX_BYTES, meta=meta)
        if len(body) < meta.get("total_bytes", 0):
            method, pages, count = _extract(body, text_pages=text_pages, ocr_pages=ocr_pages, allow_ocr=False)
            if _usable(pages) and ANCHOR.search(" ".join(pages)):
                return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "total_bytes": meta["total_bytes"],
                        "page_count": count, "method": "pdftotext_prefix", "pages": pages}
        elif body:
            method, pages, count = _extract(body, text_pages=text_pages, ocr_pages=ocr_pages, allow_ocr=True)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "total_bytes": len(body),
                    "page_count": count, "method": method, "pages": pages}
    body = http_get(url, timeout=300, accept="application/pdf")
    method, pages, count = _extract(body, text_pages=text_pages, ocr_pages=ocr_pages, allow_ocr=True)
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "total_bytes": len(body),
            "page_count": count, "method": method, "pages": pages}


def legislation_attachments(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """The ordinance PDF(s): attachmentType 'Legislation'; else any file named like an ordinance."""
    files = detail.get("attachments") or []
    chosen = [a for a in files if (a.get("attachmentType") or "").lower() == "legislation"]
    if not chosen:
        chosen = [a for a in files if re.search(r"ordinance|substitute", a.get("fileName") or "", re.I)]
    return chosen


def is_substitute(file_name: str | None) -> bool:
    return bool(re.match(r"\s*SO\d", file_name or "", re.I) or re.search(r"substitute", file_name or "", re.I))


def fetch_attachment_text(details: dict[str, dict[str, Any]], matter_ids: list[str], *, workers: int = 6) -> None:
    if not shutil.which("pdftotext") or not shutil.which("pdftoppm"):
        raise SystemExit("needs poppler (brew install poppler) for pdftotext/pdftoppm")
    have = {(r["matter_id"], r["path"]) for r in read_jsonl(ATTACHMENT_TEXT)}
    # one preferred PDF per matter first (the substitute ordinance — the text that passed — when
    # there is one), then the rest (duplicates and as-introduced versions)
    first, rest = [], []
    for m in matter_ids:
        files = sorted(legislation_attachments(details.get(m) or {}), key=lambda a: not is_substitute(a.get("fileName")))
        for i, a in enumerate(files):
            if (m, a["path"]) not in have:
                (first if i == 0 else rest).append((m, a))
    jobs = first + rest
    log(f"attachment text: {len(have)} cached, {len(jobs)} to fetch")

    def one(job: tuple[str, dict[str, Any]]) -> None:
        matter_id, attachment = job
        try:
            result = pdf_text(attachment["path"], text_pages=TEXT_PAGES, ocr_pages=OCR_PAGES)
        except Exception as error:  # noqa: BLE001 - record and move on; rerun retries errors
            log(f"  {matter_id} {attachment.get('fileName')}: {type(error).__name__}: {error}")
            return
        append_jsonl(ATTACHMENT_TEXT, {"matter_id": matter_id, "path": attachment["path"],
                                       "file_name": attachment.get("fileName"),
                                       "attachment_type": attachment.get("attachmentType"),
                                       "fetched_at": now_iso(), **result})

    _run_pool(jobs, one, workers, "attachments")


# ------------------------------------------------------------------------------------------------
# Zoning Committee agendas + summaries
# ------------------------------------------------------------------------------------------------
def fetch_meetings(*, refresh: bool = False) -> list[dict[str, Any]]:
    if MEETINGS.exists() and not refresh:
        return [m for page in read_jsonl(MEETINGS) for m in page["response"].get("data") or []]
    MEETINGS.unlink(missing_ok=True)
    meetings, skip = [], 0
    while True:
        url = f"{CLERK_API}/meeting?{urlencode({'search': 'Zoning', 'sort': 'date asc', 'top': PAGE, 'skip': skip})}"
        response = get_json(url)
        append_jsonl(MEETINGS, {"url": url, "fetched_at": now_iso(), "response": response})
        rows = response.get("data") or []
        meetings += rows
        if len(rows) < PAGE:
            break
        skip += PAGE
    return meetings


def fetch_meeting_text(meetings: list[dict[str, Any]], *, workers: int = 4) -> None:
    have = {r["path"] for r in read_jsonl(MEETING_TEXT)}
    jobs = [(m, f) for m in meetings if "zoning" in (m.get("body") or "").lower()
            for f in m.get("files") or [] if f.get("attachmentType") in ("Agenda", "Summary") and f["path"] not in have]
    log(f"meeting text: {len(have)} cached, {len(jobs)} to fetch")

    def one(job: tuple[dict[str, Any], dict[str, Any]]) -> None:
        meeting, attachment = job
        try:
            result = pdf_text(attachment["path"], text_pages=200, ocr_pages=OCR_PAGES_MEETING, prefix=False)
        except Exception as error:  # noqa: BLE001
            log(f"  meeting {meeting['date'][:10]} {attachment.get('attachmentType')}: {type(error).__name__}: {error}")
            return
        append_jsonl(MEETING_TEXT, {"meeting_id": meeting["meetingId"], "date": meeting["date"],
                                    "body": meeting.get("body"), "path": attachment["path"],
                                    "attachment_type": attachment.get("attachmentType"),
                                    "fetched_at": now_iso(), **result})

    _run_pool(jobs, one, workers, "meetings")


def _run_pool(jobs: list[Any], fn, workers: int, label: str) -> None:
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, job) for job in jobs]
        for future in as_completed(futures):
            done += 1
            try:
                future.result()
            except Exception as error:  # noqa: BLE001
                log(f"  {label} error: {type(error).__name__}: {error}")
            if done % 200 == 0:
                log(f"  {label}: {done}/{len(jobs)}")


# ------------------------------------------------------------------------------------------------
def is_passed(row: dict[str, Any], detail: dict[str, Any] | None) -> bool:
    actions = (detail or {}).get("actions") or []
    if any(re.match(r"passed", a.get("actionName") or "", re.I) and "council" in (a.get("actionByName") or "").lower()
           for a in actions):
        return True
    return (row.get("subStatus") or row.get("legacyStatus") or "").lower().startswith("passed")


def fetch_all(*, refresh: bool = False, workers: int = 8) -> None:
    with run_lock("fetch"):
        rows = fetch_matter_pages(refresh=refresh)
        log(f"{len(rows)} zoning matters")
        fetch_details([r["matterId"] for r in rows], workers=workers)
        details = cached_details()
        passed = [r["matterId"] for r in rows if r.get("type") == "Ordinance" and is_passed(r, details.get(r["matterId"]))]
        log(f"{len(passed)} passed ordinances")
        fetch_meeting_text(fetch_meetings(refresh=refresh))
        fetch_attachment_text(details, passed, workers=max(2, workers - 2))
