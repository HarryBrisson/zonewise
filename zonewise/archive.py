"""Our own archive of the City zoning map, so rezonings passed from now on get checkpoints.

The portal keeps no history of `dj47-wfun` (Boundaries - Zoning Districts (current)): when a
rezoned polygon is rezoned again, the earlier ordinance's tag is gone. Archived copies taken
monthly keep each tag on record until the next change. citymap.py treats every archived copy as a
snapshot of its own (label = the archive date).

  data/raw/city_zoning_archive/dj47-wfun_<YYYY-MM-DD>.geojson.gz   the GeoJSON exactly as served (Chicago date)
  data/raw/city_zoning_archive/manifest.jsonl                      one line per check:
      {dataset, date, path, sha256 (of the uncompressed GeoJSON), bytes, rows, rows_updated_at,
       fetched_at}   path is null when the content matched the previous copy

A copy is taken only when the dataset's `rowsUpdatedAt` differs from the last one archived.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .cache import RAW, append_jsonl, get_json, http_get, now_iso, read_jsonl
from .elms import log

PORTAL = "https://data.cityofchicago.org"
DATASET = "dj47-wfun"
LIMIT = 50000  # the polygon count is ~15k; a full page means the copy was cut off
ARCHIVE = RAW / "city_zoning_archive"
MANIFEST = ARCHIVE / "manifest.jsonl"


def manifest() -> list[dict[str, Any]]:
    return list(read_jsonl(MANIFEST))


def archived_copies() -> list[dict[str, Any]]:
    """Manifest entries that hold a file, oldest first, one per date (the last written wins)."""
    by_date = {m["date"]: m for m in manifest() if m.get("path") and (ARCHIVE / m["path"]).exists()}
    return [by_date[d] for d in sorted(by_date)]


def rows_updated_at() -> int | None:
    meta = get_json(f"{PORTAL}/api/views/{DATASET}.json")
    return meta.get("rowsUpdatedAt")


def archive_current_map(*, today: str | None = None) -> dict[str, Any] | None:
    """Archive today's dj47-wfun unless it is unchanged since the last copy. Returns the new
    manifest entry, or None when skipped. Safe to rerun: a crash before the manifest line leaves
    only an unlisted file, which the next run overwrites."""
    today = today or datetime.now(ZoneInfo("America/Chicago")).date().isoformat()  # vote dates are Chicago dates
    updated = rows_updated_at()
    history = manifest()
    if history and history[-1].get("rows_updated_at") == updated:
        log(f"archive: {DATASET} unchanged since {history[-1]['date']} (rowsUpdatedAt {updated}), skipped")
        return None
    body = http_get(f"{PORTAL}/resource/{DATASET}.geojson?$limit={LIMIT}", timeout=600, accept="*/*")
    rows = len(json.loads(body)["features"])
    if rows >= LIMIT:
        raise SystemExit(f"archive: {DATASET} returned {rows} rows = $limit; raise LIMIT and page")
    sha = hashlib.sha256(body).hexdigest()
    same = next((m for m in reversed(history) if m.get("path")), None)
    name = None
    if not same or same["sha256"] != sha:
        name = f"{DATASET}_{today}.geojson.gz"
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        tmp = ARCHIVE / f".{name}.tmp"
        tmp.write_bytes(gzip.compress(body, compresslevel=9, mtime=0))
        os.replace(tmp, ARCHIVE / name)
    entry = {"dataset": DATASET, "date": today, "path": name, "sha256": sha, "bytes": len(body), "rows": rows,
             "rows_updated_at": updated, "fetched_at": now_iso()}
    append_jsonl(MANIFEST, entry)
    log(f"archive: {DATASET} {rows} rows → {name or f'(same content as {same['date']}, no file)'}")
    return entry


def read_copy(entry: dict[str, Any]) -> dict[str, Any]:
    return json.loads(gzip.decompress((ARCHIVE / entry["path"]).read_bytes()))
