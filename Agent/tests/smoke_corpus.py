"""Corpus smoke test: sample real files of every extension found under a root
and report how each parser behaved.

    python tests/smoke_corpus.py "C:\\Users\\shiny\\Desktop\\RDNA\\Docs" --per-ext 3
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docparse import parse  # noqa: E402
from docparse.core import ParseError  # noqa: E402
from docparse.cli import SKIP_DIRS  # noqa: E402


def collect(root: Path, per_ext: int, seed: int) -> dict[str, list[Path]]:
    buckets: dict[str, list[Path]] = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            buckets[Path(name).suffix.lower()].append(Path(dirpath) / name)
    rng = random.Random(seed)
    return {ext: rng.sample(files, min(per_ext, len(files))) for ext, files in sorted(buckets.items())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--per-ext", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ext", action="append")
    ap.add_argument("--max-bytes", type=int, default=80_000_000)
    ap.add_argument("--traceback", action="store_true")
    ap.add_argument("--show-text", type=int, default=0, help="print N chars of extracted text")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    wanted = None
    if args.ext:
        wanted = {e if e.startswith(".") else "." + e for v in args.ext for e in v.split(",")}

    buckets = collect(Path(args.root), args.per_ext, args.seed)
    failures = 0
    total = 0
    print(f"{'EXT':<11} {'OK':>3}/{'N':<3} {'KIND':<22} {'CHARS':>9} {'SEC':>6}  NOTES")
    for ext, files in buckets.items():
        if wanted and ext not in wanted:
            continue
        ok = 0
        kinds: set[str] = set()
        chars: list[int] = []
        elapsed = 0.0
        notes: list[str] = []
        for path in files:
            total += 1
            if path.stat().st_size > args.max_bytes:
                notes.append(f"skipped {path.name} ({path.stat().st_size / 1e6:.0f} MB)")
                continue
            start = time.perf_counter()
            try:
                doc = parse(path)
                ok += 1
                kinds.add(doc.kind)
                chars.append(len(doc.text))
                if doc.fidelity != "full":
                    kinds.add(f"({doc.fidelity})")
                if args.show_text:
                    print(f"\n--- {path}\n{doc.text[:args.show_text]}\n---")
            except ParseError as exc:
                failures += 1
                notes.append(f"{path.name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                notes.append(f"{path.name}: UNEXPECTED {type(exc).__name__}: {exc}")
                if args.traceback:
                    traceback.print_exc()
            elapsed += time.perf_counter() - start
        avg = sum(chars) // len(chars) if chars else 0
        print(f"{ext or '(none)':<11} {ok:>3}/{len(files):<3} {','.join(sorted(kinds))[:22]:<22} "
              f"{avg:>9} {elapsed:>6.2f}  {'; '.join(notes)[:160]}")
    print(f"\n{total} file(s) attempted, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
