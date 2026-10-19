"""Census Bureau batch geocoder (free, no key): rezoning address → [lon, lat].

Request format copied from ward-wise-civic-tech apps/ward_wise_explorer/services/census_geocode.py
(`batch_geocode`), with the raw CSV response of every batch appended to
data/raw/census_geocode.jsonl before parsing, and batches of 1,000 instead of 500.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any
from urllib.request import Request, urlopen

from .cache import PROCESSED, RAW, USER_AGENT, append_jsonl, now_iso, read_jsonl
from .elms import log

CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CACHE = RAW / "census_geocode.jsonl"
BATCH = 1000


def clean_lookup(address: str) -> str:
    """'4936 N Lincoln Ave No. 1S' → '4936 N Lincoln Ave'; '7740-7744 S Chicago Ave' → '7740 S Chicago Ave'."""
    a = re.sub(r"^(\d+)\s*-\s*\d+[A-Z]?", r"\1", address.strip())
    a = re.sub(r"\s+(?:No\.|Unit|#|Ste\.?|Suite)\s*\S+$", "", a, flags=re.I)
    a = re.sub(r"\s+(?:and|&)\s+.*$", "", a, flags=re.I)
    return a.strip(" .,")


def _batch(addresses: list[str], timeout: int = 300) -> str:
    rows = "\n".join(f'{i},"{a.replace(chr(34), "")}",Chicago,IL,' for i, a in enumerate(addresses)) + "\n"
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"batch.csv\"\r\n"
        f"Content-Type: text/csv\r\n\r\n{rows}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    request = Request(CENSUS_BATCH, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                       "User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_response(addresses: list[str], text: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in csv.reader(io.StringIO(text)):
        if len(line) < 3 or not line[0].isdigit() or int(line[0]) >= len(addresses):
            continue
        point = None
        if line[2] == "Match" and len(line) > 5 and "," in line[5]:
            lon, lat = line[5].split(",")
            point = [float(lon), float(lat)]
        out[addresses[int(line[0])]] = {"point": point, "match": line[2], "exactness": line[3] if len(line) > 3 else "",
                                        "matched_address": line[4] if len(line) > 4 else ""}
    return out


def cached_points() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(CACHE):
        out.update(parse_response(record["addresses"], record["response"]))
    return out


def run_geocode() -> dict[str, dict[str, Any]]:
    import csv as _csv

    rows = list(_csv.DictReader((PROCESSED / "rezonings.csv").open()))
    wanted = sorted({clean_lookup(r["address_lookup"]) for r in rows
                     if r["status"] == "passed" and r["address_lookup"] and r["address_lookup"][0].isdigit()})
    have = cached_points()
    todo = [a for a in wanted if a not in have]
    log(f"geocode: {len(wanted)} addresses, {len(have)} cached, {len(todo)} to send")
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        text = _batch(chunk)
        append_jsonl(CACHE, {"fetched_at": now_iso(), "benchmark": "Public_AR_Current", "addresses": chunk, "response": text})
        log(f"  batch {i // BATCH + 1}: {sum(1 for v in parse_response(chunk, text).values() if v['point'])}/{len(chunk)} matched")
    points = cached_points()
    hit = sum(1 for a in wanted if (points.get(a) or {}).get("point"))
    log(f"geocode: {hit}/{len(wanted)} matched ({hit / max(1, len(wanted)):.1%})")
    return points
