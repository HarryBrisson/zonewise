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

The archive can't be refetched, so it is mirrored to S3: s3://zonewise-archive/city_zoning_archive/
(private, versioned; override with ZONEWISE_ARCHIVE_BUCKET, empty = local only). Each archive run
syncs both ways before and after: copies missing on either side are copied over (checked against
the manifest sha256), and the two manifests are merged line by line. A fresh checkout therefore
pulls the whole history on its first refresh. If S3 is unreachable the run logs it and carries on;
the next run catches up.
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
BUCKET = os.environ.get("ZONEWISE_ARCHIVE_BUCKET", "zonewise-archive")
PREFIX = "city_zoning_archive/"


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
    mirror()  # pull first: a fresh checkout must know the history before deciding to skip
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
    mirror()
    return entry


def read_copy(entry: dict[str, Any]) -> dict[str, Any]:
    return json.loads(gzip.decompress((ARCHIVE / entry["path"]).read_bytes()))


# ------------------------------------------------------------------------------------------------
# S3 mirror
# ------------------------------------------------------------------------------------------------
def _sha_of_gz(body: bytes) -> str:
    return hashlib.sha256(gzip.decompress(body)).hexdigest()


def sync_s3(client: Any, bucket: str = BUCKET) -> dict[str, int]:
    """Two-way sync of the archive folder with s3://bucket/PREFIX. Copies first, manifest last, so
    a manifest on either side never lists a copy that side lacks. Never deletes anything."""
    remote = {o["Key"][len(PREFIX):] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX)
              for o in page.get("Contents") or []}
    remote_lines: list[str] = []
    if "manifest.jsonl" in remote:
        remote_lines = client.get_object(Bucket=bucket, Key=PREFIX + "manifest.jsonl")["Body"].read().decode().splitlines()
    local_lines = MANIFEST.read_text().splitlines() if MANIFEST.exists() else []
    entries: dict[str, dict[str, Any]] = {}
    for line in local_lines + remote_lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:  # a torn last line
            continue
        entries.setdefault(json.dumps(entry, sort_keys=True), entry)
    merged = sorted(entries.values(), key=lambda e: e["fetched_at"])
    counts = {"up": 0, "down": 0}
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    for entry in merged:
        name = entry.get("path")
        if not name:
            continue
        local = ARCHIVE / name
        if local.exists() and name not in remote:
            body = local.read_bytes()
            if _sha_of_gz(body) != entry["sha256"]:
                raise SystemExit(f"archive: {name} does not match its manifest sha256; not uploading")
            client.put_object(Bucket=bucket, Key=PREFIX + name, Body=body, ContentType="application/gzip",
                              Metadata={"sha256": entry["sha256"]})
            counts["up"] += 1
        elif not local.exists() and name in remote:
            body = client.get_object(Bucket=bucket, Key=PREFIX + name)["Body"].read()
            if _sha_of_gz(body) != entry["sha256"]:
                raise SystemExit(f"archive: s3 {name} does not match its manifest sha256")
            tmp = ARCHIVE / f".{name}.tmp"
            tmp.write_bytes(body)
            os.replace(tmp, local)
            counts["down"] += 1
    text = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in merged)
    if text.splitlines() != local_lines:
        tmp = ARCHIVE / ".manifest.jsonl.tmp"
        tmp.write_text(text)
        os.replace(tmp, MANIFEST)
    if text.splitlines() != remote_lines:
        client.put_object(Bucket=bucket, Key=PREFIX + "manifest.jsonl", Body=text.encode(), ContentType="application/x-ndjson")
    return counts


def mirror() -> None:
    """sync_s3 against the real bucket; an unreachable S3 is logged, never fatal."""
    if not BUCKET:
        return
    try:
        import boto3

        counts = sync_s3(boto3.client("s3"))
        log(f"archive: s3://{BUCKET}/{PREFIX} in sync (uploaded {counts['up']}, downloaded {counts['down']})")
    except Exception as error:  # noqa: BLE001 - the local copy is kept; the next run retries
        log(f"archive: S3 mirror FAILED, local copy only until the next run: {type(error).__name__}: {error}")
