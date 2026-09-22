"""Command line interface: python -m docparse <command>"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import cache as _cache
from .core import ParseError, parse, supported_extensions

SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".cache", ".venv", "venv"}
BINARY_ONLY_KINDS = {"image", "font"}


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def walk_files(root: Path, exts: set[str] | None, exclude: list[str], max_bytes: int | None):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            if exts is not None and path.suffix.lower() not in exts:
                continue
            rel = str(path).replace("\\", "/")
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in exclude):
                continue
            if max_bytes is not None:
                try:
                    if path.stat().st_size > max_bytes:
                        continue
                except OSError:
                    continue
            yield path


def _ext_set(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out: set[str] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip().lower()
            if item:
                out.add(item if item.startswith(".") else "." + item)
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_types(args: argparse.Namespace) -> int:
    supported = supported_extensions()
    root = Path(args.root)
    counts: Counter[str] = Counter()
    for path in walk_files(root, None, args.exclude, None):
        counts[path.suffix.lower() or "(no extension)"] += 1
    rows = []
    for ext, count in counts.most_common():
        desc = supported.get(ext)
        status = "yes" if desc else "fallback"
        rows.append((count, ext, status, desc or "sniffed as text, or metadata only"))
    if args.json:
        print(json.dumps([{"count": c, "ext": e, "supported": s, "parser": d} for c, e, s, d in rows], indent=2))
        return 0
    print(f"{'COUNT':>7}  {'EXT':<12} {'PARSED':<9} DESCRIPTION")
    for count, ext, status, desc in rows:
        print(f"{count:>7}  {ext:<12} {status:<9} {desc}")
    total_unsupported = sum(c for c, _e, s, _d in rows if s != "yes")
    print(f"\n{sum(counts.values())} files, {total_unsupported} without a dedicated parser")
    return 0


def cmd_parsers(args: argparse.Namespace) -> int:
    supported = supported_extensions()
    grouped: dict[str, list[str]] = {}
    for ext, desc in supported.items():
        grouped.setdefault(desc, []).append(ext)
    if args.json:
        print(json.dumps(grouped, indent=2))
        return 0
    for desc, exts in sorted(grouped.items()):
        print(f"{', '.join(sorted(exts)):<45} {desc}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    failures = 0
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for raw in args.paths:
        path = Path(raw)
        try:
            doc = _cache.parse_cached(path, use_cache=not args.no_cache)
        except (ParseError, OSError) as exc:
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if args.json:
            payload = doc.to_dict()
            if args.max_chars:
                payload["text"] = payload["text"][: args.max_chars]
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            rendered = doc.render(max_chars=args.max_chars, show_meta=not args.no_meta)
        if out_dir:
            target = out_dir / (path.stem + (".json" if args.json else ".md"))
            target.write_text(rendered, encoding="utf-8")
            print(f"wrote {target}")
        else:
            print(rendered)
            if len(args.paths) > 1:
                print("\n" + "=" * 78 + "\n")
    return 1 if failures else 0


def cmd_info(args: argparse.Namespace) -> int:
    results = []
    for raw in args.paths:
        path = Path(raw)
        try:
            doc = _cache.parse_cached(path, use_cache=not args.no_cache)
            entry = {"path": str(path), "kind": doc.kind, "fidelity": doc.fidelity,
                     "chars": len(doc.text), "meta": doc.meta, "warnings": doc.warnings}
        except (ParseError, OSError) as exc:
            entry = {"path": str(path), "error": str(exc)}
        results.append(entry)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0
    for entry in results:
        print(f"\n{entry['path']}")
        if "error" in entry:
            print(f"  ERROR: {entry['error']}")
            continue
        print(f"  kind      : {entry['kind']}")
        print(f"  fidelity  : {entry['fidelity']}")
        print(f"  text chars: {entry['chars']}")
        for key, value in entry["meta"].items():
            print(f"  {key:<10}: {str(value)[:200]}")
        for warning in entry["warnings"]:
            print(f"  WARNING   : {warning}")
    return 0


_SEARCH_STATE: dict[str, object] = {}


def _search_one(path_str: str):
    """Worker: returns (path, [(line_no, line)], error)."""
    pattern: re.Pattern[str] = _SEARCH_STATE["pattern"]  # type: ignore[assignment]
    use_cache: bool = _SEARCH_STATE["use_cache"]  # type: ignore[assignment]
    max_hits: int = _SEARCH_STATE["max_hits"]  # type: ignore[assignment]
    path = Path(path_str)
    try:
        doc = _cache.parse_cached(path, use_cache=use_cache)
    except (ParseError, OSError, MemoryError) as exc:
        return path_str, [], f"{type(exc).__name__}: {exc}"
    hits = []
    for number, line in enumerate(doc.text.splitlines(), start=1):
        if pattern.search(line):
            hits.append((number, line.strip()[:400]))
            if len(hits) >= max_hits:
                break
    return path_str, hits, None


def _init_worker(pattern_source: str, flags: int, use_cache: bool, max_hits: int) -> None:
    _SEARCH_STATE["pattern"] = re.compile(pattern_source, flags)
    _SEARCH_STATE["use_cache"] = use_cache
    _SEARCH_STATE["max_hits"] = max_hits


def cmd_search(args: argparse.Namespace) -> int:
    flags = re.IGNORECASE if args.ignore_case else 0
    if args.fixed_string:
        pattern_source = re.escape(args.pattern)
    else:
        pattern_source = args.pattern
    try:
        re.compile(pattern_source, flags)
    except re.error as exc:
        print(f"bad regex: {exc}", file=sys.stderr)
        return 2

    exts = _ext_set(args.ext)
    paths = [str(p) for p in walk_files(Path(args.root), exts, args.exclude, args.max_bytes)]
    if args.limit_files:
        paths = paths[: args.limit_files]

    total_hits = 0
    matched_files = 0
    errors: list[str] = []
    results: list[dict[str, object]] = []

    _init_worker(pattern_source, flags, not args.no_cache, args.max_per_file)
    if args.jobs > 1 and len(paths) > 4:
        executor = ProcessPoolExecutor(
            max_workers=args.jobs,
            initializer=_init_worker,
            initargs=(pattern_source, flags, not args.no_cache, args.max_per_file),
        )
        iterator = executor.map(_search_one, paths, chunksize=8)
    else:
        executor = None
        iterator = map(_search_one, paths)

    try:
        for path_str, hits, error in iterator:
            if error:
                errors.append(f"{path_str}: {error}")
                continue
            if not hits:
                continue
            matched_files += 1
            total_hits += len(hits)
            if args.json:
                results.append({"path": path_str, "hits": [{"line": n, "text": t} for n, t in hits]})
            elif args.files_only:
                print(path_str)
            else:
                print(path_str)
                for number, line in hits:
                    print(f"  {number}: {line}")
            if args.max_files and matched_files >= args.max_files:
                break
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    if args.json:
        print(json.dumps({"pattern": args.pattern, "files_scanned": len(paths),
                          "files_matched": matched_files, "hits": total_hits,
                          "results": results, "errors": errors[:50]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\n{total_hits} match(es) in {matched_files} of {len(paths)} file(s)")
        if errors and args.show_errors:
            print(f"\n{len(errors)} unreadable file(s):", file=sys.stderr)
            for error in errors[:50]:
                print("  " + error, file=sys.stderr)
        elif errors:
            print(f"({len(errors)} file(s) could not be parsed; use --show-errors)", file=sys.stderr)
    return 0 if matched_files else 1


def _dump_one(path_str: str):
    root: Path = _SEARCH_STATE["root"]  # type: ignore[assignment]
    out_root: Path = _SEARCH_STATE["out"]  # type: ignore[assignment]
    use_cache: bool = _SEARCH_STATE["use_cache"]  # type: ignore[assignment]
    path = Path(path_str)
    try:
        doc = _cache.parse_cached(path, use_cache=use_cache)
    except (ParseError, OSError, MemoryError) as exc:
        return path_str, False, f"{type(exc).__name__}: {exc}"
    if doc.kind in BINARY_ONLY_KINDS and not doc.text:
        return path_str, False, "no text"
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    target = out_root / rel.parent / (rel.name + ".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc.render(), encoding="utf-8")
    return path_str, True, None


def _init_dump(root: str, out: str, use_cache: bool) -> None:
    _SEARCH_STATE["root"] = Path(root)
    _SEARCH_STATE["out"] = Path(out)
    _SEARCH_STATE["use_cache"] = use_cache


def cmd_dump(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    exts = _ext_set(args.ext)
    paths = [str(p) for p in walk_files(root, exts, args.exclude, args.max_bytes)]
    _init_dump(str(root), str(out), not args.no_cache)
    written = 0
    skipped = 0
    if args.jobs > 1 and len(paths) > 4:
        executor = ProcessPoolExecutor(max_workers=args.jobs, initializer=_init_dump,
                                       initargs=(str(root), str(out), not args.no_cache))
        iterator = executor.map(_dump_one, paths, chunksize=8)
    else:
        executor = None
        iterator = map(_dump_one, paths)
    try:
        for path_str, ok, error in iterator:
            if ok:
                written += 1
                if args.verbose:
                    print(f"ok   {path_str}")
            else:
                skipped += 1
                if args.verbose:
                    print(f"skip {path_str}: {error}")
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    print(f"wrote {written} file(s) to {out}; skipped {skipped}")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    if args.clear:
        print(f"removed {_cache.clear()} cache entries from {_cache.cache_dir()}")
        return 0
    count, size = _cache.stats()
    print(f"{_cache.cache_dir()}: {count} entries, {size / 1e6:.1f} MB")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m docparse",
        description="Read-only extraction of text from binary documents (Word, Excel, "
                    "PowerPoint, Visio, PDF, OneNote, metafiles, ...).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    common_cache = argparse.ArgumentParser(add_help=False)
    common_cache.add_argument("--no-cache", action="store_true", help="bypass the extracted-text cache")

    walk_opts = argparse.ArgumentParser(add_help=False)
    walk_opts.add_argument("--ext", action="append", help="only these extensions, e.g. --ext .docx,.pdf")
    walk_opts.add_argument("--exclude", action="append", default=[],
                           help="glob of paths to skip (repeatable)")
    walk_opts.add_argument("--max-bytes", type=int, default=200_000_000,
                           help="skip files larger than this (default 200 MB)")
    walk_opts.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) // 2),
                           help="parallel worker processes")

    p = sub.add_parser("types", parents=[], help="inventory the file types under a folder")
    p.add_argument("root")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_types)

    p = sub.add_parser("parsers", help="list every extension this toolkit understands")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_parsers)

    p = sub.add_parser("extract", parents=[common_cache], help="print the text of one or more files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-chars", type=int, default=None, help="truncate output")
    p.add_argument("--no-meta", action="store_true", help="omit the metadata header")
    p.add_argument("--out", help="write to this folder instead of stdout")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("info", parents=[common_cache], help="metadata and parse fidelity only")
    p.add_argument("paths", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("search", parents=[common_cache, walk_opts],
                       help="regex search inside binary documents")
    p.add_argument("root")
    p.add_argument("-p", "--pattern", required=True)
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("-F", "--fixed-string", action="store_true", help="treat the pattern literally")
    p.add_argument("-l", "--files-only", action="store_true")
    p.add_argument("--max-per-file", type=int, default=20)
    p.add_argument("--max-files", type=int, default=0, help="stop after N matching files")
    p.add_argument("--limit-files", type=int, default=0, help="only look at the first N files")
    p.add_argument("--show-errors", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("dump", parents=[common_cache, walk_opts],
                       help="extract a whole tree into a mirror of .md files")
    p.add_argument("root")
    p.add_argument("--out", required=True)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("cache", help="inspect or clear the extraction cache")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_cache)

    return ap


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
