"""Matter → one structured rezoning row: from/to district, address, dates, status, application type.

Three independent texts can state the change, in order of authority:
  1. the ordinance itself ("…by changing all the RS-3 Residential Single-Unit (Detached House)
     District symbols and indications as shown on Map No. 7-H … to those of an RM-5 Residential
     Multi-Unit District"), from the matter's Legislation PDF (text layer, or Vision OCR);
  2. the Zoning Committee agenda/summary ("Change Request: RS-3 … to RM-5 …"), joined on the
     application number;
  3. the application form ("Present Zoning District … Proposed Zoning District …"), legacy PDFs.
Regex reads each. When none yields a usable pair, `--llm` asks a cheap model to SELECT the from/to
among district codes regex found in those texts (it never invents a code), caching the raw I/O.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .cache import PROCESSED, read_jsonl
from .elms import ATTACHMENT_TEXT, CLERK_SITE, EXACT_FILTER, MEETING_TEXT, cached_details, is_passed, is_substitute, log, matters_from_pages

CHICAGO = ZoneInfo("America/Chicago")
OUT = PROCESSED / "rezonings.csv"


# ------------------------------------------------------------------------------------------------
# Copied from ward-wise-civic-tech apps/ward_wise_explorer/services/alerts_digest.py (zoning_address)
# ------------------------------------------------------------------------------------------------
def zoning_address(title: Any) -> dict[str, str] | None:
    """"Zoning Reclassification Map No. 2-I at 2400 W Grenshaw St a.k.a. … - App No. 23149T1" →
    {"shown": "2400 W Grenshaw St", "lookup": "2400 W Grenshaw St"}."""
    match = re.search(r"(?:\s|-|[A-Z])at (\d[^,;/]*?)(?:\s*-\s*App|,|;|/| a\.k\.a\.| and |$)", str(title))
    if not match:
        return None
    shown = match.group(1).strip()
    return {"shown": shown, "lookup": re.sub(r"^(\d+)-\d+", r"\1", shown)}


# ------------------------------------------------------------------------------------------------
# District codes
# ------------------------------------------------------------------------------------------------
# Descriptive names that follow a code in ordinance text; used to repair OCR'd prefixes (83-2 →
# B3-2) and as a check on the code.
FAMILY_NAMES = [
    ("RS", r"single[\s-]*unit"), ("RT", r"two[\s-]*flat"), ("RM", r"multi[\s-]*unit"),
    ("B1", r"neighborhood\s+shopping"), ("B2", r"neighborhood\s+mixed"), ("B3", r"community\s+shopping"),
    ("C1", r"neighborhood\s+commercial"), ("C2", r"motor\s+vehicle"), ("C3", r"commercial,?\s+manufacturing"),
    ("M1", r"limited\s+manufacturing"), ("M2", r"light\s+industr"), ("M3", r"heavy\s+industr"),
    ("DC", r"downtown\s+core"), ("DX", r"downtown\s+mixed"), ("DR", r"downtown\s+residential"),
    ("DS", r"downtown\s+service"), ("POS", r"parks?\s+and\s+open\s+space"),
]

SUFFIX = r"(?:1\.5|3\.5|4\.5|5\.5|6\.5|4A|\d{1,2})"
CODE = re.compile(
    rf"\b(?P<cls>RS|RT|RM|B[123]|C[123]|D[CXRS]|M[123]|POS)\s?[-–—]?\s?(?P<suf>{SUFFIX})(?![\d.]*\d)"
    r"|\b(?P<pmd>P\.?\s?M\.?\s?D\.?|Planned\s+Manufacturing\s+District)\b(?:\s*(?:No\.?|Number|#)\s*(?P<pmdn>\d+[A-Z]?))?"
    r"|(?P<pd>\bP\S{0,2}an\S{0,3}ed\s+Dev\S{2,10}|\bP\.?\s?D\.?\b)(?:\s*(?:No\.?|Number|#)?\s*(?P<pdn>\d+)\b)?"
    r"|\b(?P<t>T)\s+Transportation\s+District",
    re.I,
)
_VALID = {
    "RS": {"1", "2", "3"}, "RT": {"3.5", "4", "4A"}, "RM": {"4.5", "5", "5.5", "6", "6.5"},
    "B": {"1", "1.5", "2", "3", "5"}, "C": {"1", "1.5", "2", "3", "5"}, "M": {"1", "2", "3"},
    "D": {"3", "5", "7", "10", "12", "16"}, "POS": {"1", "2"},
}


def ocr_fix(text: str) -> str:
    """Undo the OCR misreads that hit district codes: 83-2→B3-2, Ml-2→M1-2, RS-l→RS-1, R5-3→RS-3."""
    t = text.replace("­", "-")
    one = str.maketrans({"l": "1", "I": "1", "L": "1", "|": "1", "!": "1", "i": "1", "Z": "2"})

    def bcm(m: re.Match[str]) -> str:
        cls = ("B" if m.group(1) == "8" else m.group(1)) + m.group(2).translate(one)
        return f"{cls}-{m.group(3).translate(one)}"
    t = re.sub(r"\b([8BCM])([123lIL|!iZ])[\s,.]{0,2}-+\s?(1\.5|[lIL|Z]|\d)(?![\d.]*\d)", bcm, t)
    t = re.sub(r"\bP[O0]S\s?-\s?[lIL|1]\b", "POS-1", t, flags=re.I)
    t = re.sub(r"\bR([STM])\s?-\s?Z\b", r"R\1-2", t)
    t = re.sub(r"\bR([TM])\s?-\s?s\b", r"R\1-5", t)
    t = re.sub(r"\bR[5S]\s?-?\s?[lI|]\b", "RS-1", t)
    t = re.sub(r"\bR5\s?-?\s?(?=[123]\b)", "RS-", t)
    t = re.sub(r"\bRS([123])\b", r"RS-\1", t)
    t = re.sub(r"\bRM\s?S\b", "RM-5", t)
    t = re.sub(r"\b([BCM])-([123])-", r"\1\2-", t)
    return t


def normalize_code(match: re.Match[str]) -> str | None:
    if match.group("pmd"):
        return f"PMD-{match.group('pmdn')}" if match.group("pmdn") else "PMD"
    if match.group("pd"):
        return f"PD-{match.group('pdn')}" if match.group("pdn") else "PD"
    if match.group("t"):
        return "T"
    cls, suf = match.group("cls").upper(), match.group("suf").upper()
    family = cls if cls in ("RS", "RT", "RM", "POS") else cls[0]
    if suf not in _VALID[family]:
        return None
    if family == "D" and cls == "DS" and suf not in {"3", "5"}:
        return None
    return f"{cls}-{suf}"


def codes_in(span: str) -> list[str]:
    """District codes in a span of text, in order, deduplicated; drops a bare PD when a numbered
    PD is present, and a bare "PD" that is only the abbreviation inside "PD No."."""
    out: list[str] = []
    for match in CODE.finditer(ocr_fix(span)):
        code = normalize_code(match)
        if code and code not in out:
            out.append(code)
    if any(c.startswith("PD-") for c in out):
        out = [c for c in out if c != "PD"]
    if any(c.startswith("PMD-") for c in out):
        out = [c for c in out if c != "PMD"]
    return out


def family_of(code: str) -> str:
    if code.startswith("PMD"):
        return "PMD"
    if code.startswith("PD"):
        return "PD"
    head = code.split("-")[0]
    return head if head in ("RS", "RT", "RM", "POS", "T") else head


def repair_by_name(span: str) -> str:
    """'83-2 Community Shopping' is B3-2: when the descriptive name after a code names a family
    unambiguously, rebuild the code from that family + the code's suffix."""
    def fix(match: re.Match[str]) -> str:
        suffix, tail = match.group(2), match.group(3)
        for family, pattern in FAMILY_NAMES:
            if re.match(rf"\s*,?\s*(?:residential\s+)?{pattern}", tail, re.I):
                return f"{family}-{suffix}{tail}"
        return match.group(0)
    return re.sub(r"\b([A-Z0-9]{1,3})\s?[-–—]\s?(\d(?:\.\d)?A?)(\s+[A-Za-z][^\n]{0,40}?(?=\s|$))", fix, span)


# ------------------------------------------------------------------------------------------------
# Clause readers
# ------------------------------------------------------------------------------------------------
def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", ocr_fix(text or ""))


ORD_FROM = re.compile(r"chang(?:ing|e)\s+a[lIHU1]{1,2}\s*(?:of\s*)?(?:the\s+)?(?P<from>.{2,500}?)\s+(?:as\s+shown|shown\s+on|on\s+(?:Zoning\s+)?Map|in\s+the\s+area\s+bounded)", re.I)
ORD_TO = re.compile(r"\bto\s+(?:those\s+of|that\s+of|the\s+designation\s+of)\s*(?:an?\b|the\b)?\s*(?P<to>.{2,220}?)(?:\.\s|;|\bSECTION\b|\bwhich\b|\band\s+a\s+corresponding\b|$)", re.I)


def read_ordinance(text: str) -> dict[str, Any] | None:
    """From/to from the ordinance clause(s). A two-step ordinance (to an underlying district, then
    to a Planned Development) yields a chain; the final district is the last 'to those of'."""
    flat = repair_by_name(_flat(text))
    start = re.search(r"Chicago\s+Zoning\s+Ordinance|Title\s+17|Zoning\s+Ordinance\W+(?:be|is)\s+(?:hereby\s+)?amended", flat, re.I)
    if start:
        flat = flat[max(0, start.start() - 50):]
    m_from = ORD_FROM.search(flat)
    if not m_from:
        return None
    from_codes = codes_in(m_from.group("from"))
    tos = [codes_in(m.group("to")) for m in ORD_TO.finditer(flat, m_from.end())]
    tos_first = [codes[0] for codes in tos if codes]
    # the first "to those of" is the change; a later one counts only when it is the PD step of a
    # two-step ordinance (later non-PD clauses are other areas of a multi-area ordinance, or the
    # same ordinance repeated in the bundle), and a PD always ends the chain
    chain = tos_first[:1]
    if chain and not chain[0].startswith("PD"):
        chain += [c for c in tos_first[1:] if c.startswith("PD")][:1]
    if not from_codes or not chain:
        return None
    numbered = [c for c in from_codes if c.startswith("PD-")]
    if chain[-1] == "PD" and len(numbered) == 1 and len(chain) == 1:
        chain = [numbered[0]]  # "PD No. 37 … to Planned Development No. 37, as amended": a PD amendment
    return {"from": from_codes, "chain": chain, "to": chain[-1], "snippet": flat[m_from.start(): m_from.start() + 600]}


def read_application(text: str) -> dict[str, Any] | None:
    flat = repair_by_name(_flat(text))
    m = re.search(r"Present\s+Zoning\s+District\W{0,5}(?P<from>.{1,80}?)\s+Proposed\s+Zoning\s+District\W{0,5}(?P<to>.{1,80}?)(?:\s+\d{1,2}\.|\s{2,}|$)", flat, re.I)
    if not m:
        return None
    from_codes, to_codes = codes_in(m.group("from")), codes_in(m.group("to"))
    if not from_codes or not to_codes:
        return None
    return {"from": from_codes, "chain": to_codes[:1], "to": to_codes[0], "snippet": m.group(0)[:300]}


def read_change_request(change: str) -> dict[str, Any] | None:
    flat = repair_by_name(_flat(change))
    parts = re.split(r"(?<=[\s,a-z)\]\d])(?:to|ro|lo|t0|io|1o|l0|Io)(?=[\s;:,]|[A-Z0-9])\s*[;:,]?\s*", flat, maxsplit=1)
    if len(parts) != 2:
        return None
    from_codes, to_codes = codes_in(parts[0]), codes_in(parts[1])
    if not from_codes or not to_codes:
        return None
    # "B3-2 to B3-5 and then to a Residential-Business Planned Development"
    chain = [c for c in to_codes if c not in from_codes] or to_codes[:1]
    return {"from": from_codes, "chain": chain, "to": chain[-1], "snippet": flat[:300]}


# ------------------------------------------------------------------------------------------------
# Agenda items (pattern adapted from alerts_digest.parse_agenda_items, loosened for OCR)
# ------------------------------------------------------------------------------------------------
AGENDA_HEAD = re.compile(r"^[ \t]*N[O0][.,]?\s*(?P<app>[A-Z]?[.\-]?\s?\d{4,6}[A-Z0-9]{0,2})\b", re.M)
DOC_NO = re.compile(r"D[O0]C(?:UMENT)?\s*#?\s*(?P<doc>[A-Za-z0-9]{1,3}\s?[0-9OolIsS]{4}\s?-\s?[0-9OolIsS ]{3,8})", re.I)


def app_key(app: str) -> str:
    """'A-8355' / 'A.8355' / 'App No. A8853' / '22286T1' → 'A8355' / 'A8853' / '22286T1'."""
    key = re.sub(r"[^A-Z0-9]", "", app.upper().replace("O", "0"))
    key = re.sub(r"^A0(?=\d)", "A", key)
    return key


def doc_key(doc: str) -> str:
    """OCR'd 'pocuMENT # 02011-513s' → 'O2011-5135' (the matter's recordNumber)."""
    head, _, tail = re.sub(r"\s", "", doc).partition("-")
    digits = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "s": "5", "S": "5"})
    prefix = re.sub(r"[^A-Z]", "", head[:-4].upper().replace("0", "O")) or "O"
    return f"{prefix}{head[-4:].translate(digits)}-{tail.translate(digits)}"


def agenda_items() -> dict[str, list[dict[str, Any]]]:
    """{key: [{date, kind, change, document}]} across every cached agenda/summary, keyed by both
    application number and document (record) number. An item runs from one "NO. <app>" header to
    the next."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in read_jsonl(MEETING_TEXT):
        text = "\n".join(record.get("pages") or [])
        heads = list(AGENDA_HEAD.finditer(text))
        for head, nxt in zip(heads, heads[1:] + [None]):
            body = text[head.end(): nxt.start() if nxt else len(text)]
            change = re.search(r"(?:Ch[ao]nge\s+Reques[lt]|Proposed\s+Change)\s*:?\s*(.+?)(?:\n\s*\n|Pu\S{0,3}ose|\Z)", body, re.S | re.I)
            if not change:
                continue
            doc = DOC_NO.search(body)
            address = re.search(r"Common\s+A\S{0,2}ddress(?:\s+of\s+Property)?\s*:?\s*([^\n]{3,120})", body, re.I)
            item = {"date": record["date"][:10], "kind": record.get("attachment_type"),
                    "address": re.sub(r"\s+", " ", address.group(1)).strip() if address else "",
                    "change": re.sub(r"\s+", " ", change.group(1)).strip()[:300],
                    "document": doc_key(doc.group("doc")) if doc else ""}
            out[app_key(head.group("app"))].append(item)
            if item["document"]:
                out[item["document"]].append(item)
    return out


# ------------------------------------------------------------------------------------------------
# Matter fields
# ------------------------------------------------------------------------------------------------
def app_number(title: str) -> str | None:
    m = re.search(r"App(?:lication)?\.?\s*(?:No\.?|#)\s*([A-Z]?-?\s?\d{4,6}[A-Z0-9]*)", title or "", re.I)
    if m:
        return app_key(m.group(1))
    m = re.search(r"-\s*(A-?\d{4})\b", title or "")
    return app_key(m.group(1)) if m else None


def app_kind(app: str | None) -> str:
    if not app:
        return "unknown"
    if re.search(r"T1$", app):
        return "type_1"
    if app.startswith("A"):
        return "aldermanic"  # map amendment introduced by the ward's alderperson
    return "map_amendment"


def chicago_date(value: Any) -> str:
    if not value:
        return ""
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.astimezone(CHICAGO).strftime("%Y-%m-%d")


def passed_date(row: dict[str, Any], detail: dict[str, Any]) -> str:
    for action in detail.get("actions") or []:
        if re.match(r"passed", action.get("actionName") or "", re.I) and "council" in (action.get("actionByName") or "").lower():
            return chicago_date(action.get("actionDate"))
    return chicago_date(row.get("finalActionDate")) if is_passed(row, detail) else ""


def referred_date(row: dict[str, Any], detail: dict[str, Any]) -> str:
    if row.get("introductionDate"):
        return chicago_date(row["introductionDate"])
    dates = [chicago_date(a.get("actionDate")) for a in detail.get("actions") or [] if (a.get("actionName") or "").lower().startswith("referred")]
    return min(dates) if dates else ""


def status_of(row: dict[str, Any], detail: dict[str, Any]) -> str:
    if is_passed(row, detail):
        return "passed"
    sub = (row.get("subStatus") or row.get("legacyStatus") or row.get("status") or "").lower()
    for key in ("failed", "withdrawn", "filed", "committee", "introduc", "consideration"):
        if key in sub:
            return {"introduc": "introduced", "consideration": "council_consideration", "committee": "in_committee"}.get(key, key)
    return sub or "unknown"


def common_address(text: str) -> str | None:
    m = re.search(r"Common\s+Address(?:\s+of\s+(?:the\s+)?Property)?\s*:?\s*(\d[^\n]{3,80})", text or "", re.I)
    if not m:
        m = re.search(r"(\d[^\n]{3,80})\s*\n\s*Common\s+Address", text or "", re.I)  # Vision puts the label after
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip(" .,")


# ------------------------------------------------------------------------------------------------
def run_parse(*, use_llm: bool = False) -> list[dict[str, Any]]:
    rows = matters_from_pages()
    details = cached_details()
    texts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in read_jsonl(ATTACHMENT_TEXT):
        texts[record["matter_id"]].append(record)
    agenda = agenda_items()
    log(f"parse: {len(rows)} matters, {len(details)} details, {len(texts)} with ordinance text, {len(agenda)} agenda app keys")

    out: list[dict[str, Any]] = []
    for row in rows:
        detail = details.get(row["matterId"]) or {}
        title = row.get("title") or ""
        if row.get("type") != "Ordinance":
            continue
        app = app_number(title)
        status = status_of(row, detail)
        docs = sorted(texts.get(row["matterId"], []), key=lambda r: (not is_substitute(r.get("file_name")), r.get("file_name") or ""))
        full = "\n".join("\n".join(d.get("pages") or []) for d in docs)

        # each document on its own (substitute first): concatenating an original and its
        # substitute would splice two different "to those of" clauses into one chain
        doc_texts = ["\n".join(d.get("pages") or []) for d in docs]
        reads: dict[str, dict[str, Any] | None] = {
            "ordinance": next((r for r in map(read_ordinance, doc_texts) if r), None),
            "application": next((r for r in map(read_application, doc_texts) if r), None)}
        house = re.match(r"\d+", zoning_address(title)["lookup"]) if zoning_address(title) else None
        agenda_hits = [a for a in agenda.get(app or "", []) + agenda.get(row.get("recordNumber") or "", [])
                       if same_place(house.group(0) if house else "", a.get("address", ""))]
        reads["agenda"] = next((r for r in (read_change_request(a["change"]) for a in sorted(agenda_hits, key=lambda a: a["date"], reverse=True)) if r), None)
        method, chosen = "none", None
        for name in ("ordinance", "agenda", "application"):
            if reads[name]:
                method, chosen = f"regex_{name}", reads[name]
                break

        address = zoning_address(title)
        address_source = "title"
        if not address:
            fallback = common_address(full)
            address = {"shown": fallback, "lookup": re.sub(r"^(\d+)-\d+", r"\1", fallback)} if fallback else None
            address_source = "ordinance_text" if address else ""
        map_amendment = bool(re.match(r"\s*zoning\s+reclassification", title, re.I)) or bool(reads["ordinance"])
        out.append({
            "matter_id": row["matterId"],
            "record_number": row.get("recordNumber") or "",
            "title": title,
            "app_no": app or "",
            "app_kind": app_kind(app),
            "type_1": app_kind(app) == "type_1",
            "map_amendment": map_amendment,
            "exact_category": row.get("matterCategory") == "ZONING RECLASSIFICATIONS",
            "status": status,
            "introduced_date": referred_date(row, detail),
            "passed_date": passed_date(row, detail) if status == "passed" else "",
            "from_districts": "+".join(chosen["from"]) if chosen else "",
            "to_district": chosen["to"] if chosen else "",
            "district_chain": ">".join(chosen["chain"]) if chosen else "",
            "parse_method": method,
            "agenda_change": agenda_hits[0]["change"] if agenda_hits else "",
            "agrees_with_agenda": _agree(reads["ordinance"], reads["agenda"]),
            "address": address["shown"] if address else "",
            "address_lookup": address["lookup"] if address else "",
            "address_source": address_source,
            "ordinance_text_method": "+".join(sorted({d.get("method", "") for d in docs})),
            "ordinance_snippet": (chosen or {}).get("snippet", "")[:400] if chosen else (full and _flat(full)[:300]),
            "url": f"{CLERK_SITE}/Matter/?matterId={row['matterId']}",
        })

    if use_llm:
        from .llm import fill_misses

        fill_misses(out, texts, agenda)

    PROCESSED.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(out[0]))
        writer.writeheader()
        writer.writerows(out)
    _report(out)
    return out


def same_place(house: str, agenda_address: str) -> bool:
    """Guard against OCR'd application numbers joining the wrong agenda item: when both sides
    have a house number, the matter's must appear among the agenda item's address numbers."""
    numbers = re.findall(r"\d{2,6}", agenda_address)
    if not house or not numbers:
        return True
    return house in numbers or any(n.startswith(house[:-1]) and len(n) == len(house) for n in numbers[:1])


def _agree(a: dict[str, Any] | None, b: dict[str, Any] | None) -> str:
    if not a or not b:
        return ""
    def pd(code: str) -> str:
        return "PD" if code.startswith("PD") else code
    same_to = pd(a["to"]) == pd(b["to"]) or a["to"] in b["chain"] or b["to"] in a["chain"]
    return "yes" if same_to and {pd(c) for c in a["from"]} & {pd(c) for c in b["from"]} else "no"


def _report(out: list[dict[str, Any]]) -> None:
    passed = [r for r in out if r["status"] == "passed" and r["map_amendment"]]
    by_method = Counter(r["parse_method"] for r in passed)
    years = Counter(r["passed_date"][:4] for r in passed)
    log(f"wrote {OUT} ({len(out)} ordinances; {len(passed)} passed map amendments)")
    log(f"passed by parse method: {dict(by_method)}")
    miss = by_method.get("none", 0)
    log(f"passed miss rate (no from/to): {miss}/{len(passed)} = {miss / max(1, len(passed)):.1%}")
    agree = Counter(r["agrees_with_agenda"] for r in passed)
    log(f"ordinance vs agenda agreement: {dict(agree)}")
    log(f"passed by year: {sorted(years.items())}")
    log(f"exact-category query ({EXACT_FILTER}) covers {sum(r['exact_category'] for r in passed)} of them")
