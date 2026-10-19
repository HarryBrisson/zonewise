"""LLM-select for regex misses: the model picks the from/to districts from a list of codes that
regex already found in the matter's own texts; anything outside that list is rejected. Every
request and raw response is appended to data/raw/llm_select.jsonl and reused by prompt hash, so a
rerun costs nothing.

Needs OPENAI_API_KEY; ZONEWISE_LLM_MODEL picks the model (default a cheap tier).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.request import Request, urlopen

from .cache import RAW, append_jsonl, now_iso, read_jsonl
from .elms import log
from .parse import _flat, codes_in

CACHE = RAW / "llm_select.jsonl"
MODEL = os.environ.get("ZONEWISE_LLM_MODEL", "gpt-5.4-mini")
ENDPOINT = "https://api.openai.com/v1/chat/completions"

SYSTEM = (
    "You read Chicago zoning reclassification ordinances (often OCR text with errors). "
    "Pick the district the property is rezoned FROM (one or more) and the final district it is "
    "rezoned TO, choosing ONLY codes from the candidate list. If the text does not say, answer null. "
    'Reply as JSON: {"from": [codes], "to": code or null, "confidence": "high"|"medium"|"low"}.'
)


def _window(text: str) -> str:
    flat = _flat(text)
    anchor = re.search(r"ORDINANCE|Change\s+Request|Present\s+Zoning", flat, re.I)
    start = max(0, anchor.start() - 200) if anchor else 0
    return flat[start:start + 3500]


def _call(payload: dict[str, Any], key: str) -> dict[str, Any]:
    request = Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode())


def fill_misses(rows: list[dict[str, Any]], texts: dict[str, list[dict[str, Any]]], agenda: dict[str, list[dict[str, Any]]]) -> None:
    misses = [r for r in rows if r["status"] == "passed" and r["map_amendment"] and r["parse_method"] == "none"]
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log(f"llm: OPENAI_API_KEY not set; {len(misses)} passed misses left unparsed")
        return
    cached = {r["prompt_sha"]: r for r in read_jsonl(CACHE)}
    filled = 0
    for row in misses:
        text = "\n".join("\n".join(d.get("pages") or []) for d in texts.get(row["matter_id"], []))
        changes = [a["change"] for a in agenda.get(row["app_no"], [])]
        context = _window(text) + ("\n\nCommittee agenda change request(s):\n" + "\n".join(changes) if changes else "")
        candidates = codes_in(context)
        if len(candidates) < 2:
            continue
        user = f"Title: {row['title']}\nCandidates: {', '.join(candidates)}\n\nText:\n{context}"
        payload = {"model": MODEL, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                   "response_format": {"type": "json_object"}}
        sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        record = cached.get(sha)
        if record is None:
            try:
                response = _call(payload, key)
            except Exception as error:  # noqa: BLE001
                log(f"llm: {row['record_number']}: {type(error).__name__}: {error}")
                continue
            record = {"prompt_sha": sha, "matter_id": row["matter_id"], "fetched_at": now_iso(),
                      "request": payload, "response": response}
            append_jsonl(CACHE, record)
            cached[sha] = record
        try:
            answer = json.loads(record["response"]["choices"][0]["message"]["content"])
        except (KeyError, IndexError, json.JSONDecodeError):
            continue
        picked_from = [c for c in answer.get("from") or [] if c in candidates]
        picked_to = answer.get("to") if answer.get("to") in candidates else None
        if picked_from and picked_to:
            row.update(from_districts="+".join(picked_from), to_district=picked_to, district_chain=picked_to,
                       parse_method=f"llm_select:{answer.get('confidence', '')}")
            filled += 1
    log(f"llm: filled {filled}/{len(misses)} passed misses with {MODEL}")
