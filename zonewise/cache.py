"""Paths, HTTP and the append-only JSONL caches every stage reads from.

Rule: anything fetched or computed expensively (eLMS pages, PDF text, OCR, geocoder batches, LLM
calls) is appended to a JSONL file under data/raw/ BEFORE anything is derived from it, so every
later stage can rerun offline from the cache.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
LAYERS = DATA / "layers"
USER_AGENT = "zonewise (https://github.com/HarryBrisson/zonewise; byop/v1 measure source)"

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, *, timeout: int = 90, tries: int = 6, accept: str = "application/json",
             byte_range: int | None = None, meta: dict[str, Any] | None = None) -> bytes:
    """GET with backoff; raises the last error. 4xx other than 429 fail at once. `byte_range`
    asks for only the first N bytes; `meta` receives the full size from Content-Range."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            headers = {"User-Agent": USER_AGENT, "Accept": accept}
            if byte_range:
                headers["Range"] = f"bytes=0-{byte_range - 1}"
            with urlopen(Request(url, headers=headers), timeout=timeout) as response:
                body = response.read()
                if meta is not None:
                    total = (response.headers.get("Content-Range") or "").rpartition("/")[2]
                    meta["total_bytes"] = int(total) if total.isdigit() else len(body)
                return body
        except HTTPError as error:
            last = error
            if error.code < 500 and error.code != 429:
                raise
            if error.code == 429:  # eLMS rate-limits around 8 parallel clients
                time.sleep(5 * 2 ** attempt)
                continue
        except (URLError, TimeoutError, ConnectionError) as error:
            last = error
        time.sleep(2 ** attempt)
    assert last is not None
    raise last


def get_json(url: str, **kwargs: Any) -> Any:
    return json.loads(http_get(url, **kwargs).decode("utf-8"))


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Thread-safe single-line append (one JSON object per line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locks_guard:
        lock = _locks.setdefault(path, threading.Lock())
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with lock, path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:  # a torn last line from a killed run
                    continue


@contextmanager
def run_lock(name: str) -> Iterator[None]:
    """One fetch per stage at a time (two writers would double-append the caches)."""
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f".{name}.lock"
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another `{name}` run holds {path}")
    try:
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
