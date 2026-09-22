"""On-disk cache of extracted text, keyed by path + size + mtime.

Parsing a 20 MB .docx or a 200-page .pdf is slow; searching a whole tree twice
should not pay that cost twice. The cache stores plain UTF-8 text only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .core import ParsedDoc, ParseError, file_key, parse

DEFAULT_CACHE = Path(os.environ.get("DOCPARSE_CACHE", Path(__file__).resolve().parent.parent / ".cache"))


def cache_dir() -> Path:
    return DEFAULT_CACHE


def load(path: Path) -> ParsedDoc | None:
    entry = DEFAULT_CACHE / f"{file_key(path)}.json"
    if not entry.is_file():
        return None
    try:
        raw = json.loads(entry.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ParsedDoc(**raw)


def store(doc: ParsedDoc, path: Path) -> None:
    try:
        DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
        entry = DEFAULT_CACHE / f"{file_key(path)}.json"
        entry.write_text(json.dumps(doc.to_dict(), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def parse_cached(path: Path, use_cache: bool = True) -> ParsedDoc:
    if not use_cache:
        return parse(path)
    cached = load(path)
    if cached is not None:
        return cached
    doc = parse(path)
    store(doc, path)
    return doc


def clear() -> int:
    if not DEFAULT_CACHE.is_dir():
        return 0
    count = 0
    for entry in DEFAULT_CACHE.glob("*.json"):
        try:
            entry.unlink()
            count += 1
        except OSError:
            pass
    return count


def stats() -> tuple[int, int]:
    if not DEFAULT_CACHE.is_dir():
        return 0, 0
    files = list(DEFAULT_CACHE.glob("*.json"))
    return len(files), sum(f.stat().st_size for f in files)


__all__ = ["parse_cached", "clear", "stats", "cache_dir", "ParseError"]
