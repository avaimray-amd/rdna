"""Dependency-free MCP (Model Context Protocol) stdio server exposing docparse.

Registered in .vscode/mcp.json, this gives an agent first-class tools for
reading the binary documentation instead of shelling out.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout.
Everything is read-only; there is no tool that writes to a source document.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docparse import supported_extensions  # noqa: E402
from docparse.cache import parse_cached  # noqa: E402
from docparse.cli import _ext_set, walk_files  # noqa: E402
from docparse.core import ParseError  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
MAX_RESULT_CHARS = 60_000

TOOLS = [
    {
        "name": "docs_search",
        "description": (
            "Regex search INSIDE binary documentation (Word, Excel, PowerPoint, Visio, "
            "PDF, OneNote, EMF/WMF diagrams) as well as plain text. Use this instead of "
            "grep whenever the answer might live in a .docx/.xlsx/.pptx/.vsdx/.pdf, "
            "because grep cannot see inside those containers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Folder (or single file) to search."},
                "pattern": {"type": "string", "description": "Python regular expression."},
                "ignore_case": {"type": "boolean", "default": True},
                "extensions": {
                    "type": "string",
                    "description": "Optional comma-separated filter, e.g. '.docx,.pdf'.",
                },
                "max_files": {"type": "integer", "default": 40},
                "max_per_file": {"type": "integer", "default": 5},
            },
            "required": ["root", "pattern"],
        },
    },
    {
        "name": "docs_extract",
        "description": (
            "Extract the full text of one document as markdown. Headings, tables, slide "
            "numbers, sheet names and Visio page/connection structure are preserved where "
            "the format allows. Use offset/max_chars to page through long documents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "default": 40000},
                "offset": {"type": "integer", "default": 0},
                "include_metadata": {"type": "boolean", "default": True},
            },
            "required": ["path"],
        },
    },
    {
        "name": "docs_info",
        "description": (
            "Metadata and extraction fidelity for one document without returning its "
            "text: author, revision, page/slide/sheet counts, and any warnings about "
            "what could not be recovered. Call this first on large or unfamiliar files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "docs_outline",
        "description": (
            "Return only the heading/section structure of a document (markdown headings, "
            "slide titles, sheet names, Visio page names). Cheap way to decide which part "
            "of a long specification to read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "max_headings": {"type": "integer", "default": 200}},
            "required": ["path"],
        },
    },
    {
        "name": "docs_list_types",
        "description": "Inventory the file types under a folder and say which ones can be parsed.",
        "inputSchema": {
            "type": "object",
            "properties": {"root": {"type": "string"}},
            "required": ["root"],
        },
    },
]


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n\n[... truncated; {len(text) - MAX_RESULT_CHARS} more chars. Use docs_extract with offset to continue ...]"


def tool_docs_search(args: dict) -> str:
    pattern = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case", True) else 0)
    exts = _ext_set([args["extensions"]]) if args.get("extensions") else None
    max_files = int(args.get("max_files", 40))
    max_per_file = int(args.get("max_per_file", 5))
    lines: list[str] = []
    scanned = 0
    matched = 0
    for path in walk_files(Path(args["root"]), exts, [], 200_000_000):
        scanned += 1
        try:
            doc = parse_cached(path)
        except (ParseError, OSError, MemoryError):
            continue
        hits = []
        for number, line in enumerate(doc.text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"  {number}: {line.strip()[:300]}")
                if len(hits) >= max_per_file:
                    break
        if hits:
            matched += 1
            lines.append(str(path))
            lines += hits
            if matched >= max_files:
                lines.append(f"[stopped after {max_files} matching files]")
                break
    header = f"{matched} matching file(s) out of {scanned} scanned.\n"
    return _truncate(header + "\n".join(lines) if lines else header + "No matches.")


def tool_docs_extract(args: dict) -> str:
    doc = parse_cached(Path(args["path"]))
    offset = int(args.get("offset", 0))
    max_chars = int(args.get("max_chars", 40000))
    body = doc.text[offset: offset + max_chars]
    head = ""
    if args.get("include_metadata", True):
        head = doc.render(max_chars=0, show_meta=True).rstrip() + "\n\n"
    tail = ""
    remaining = len(doc.text) - (offset + len(body))
    if remaining > 0:
        tail = f"\n\n[... {remaining} more chars; call again with offset={offset + len(body)} ...]"
    return _truncate(head + body + tail)


def tool_docs_info(args: dict) -> str:
    doc = parse_cached(Path(args["path"]))
    payload = {
        "path": doc.path,
        "kind": doc.kind,
        "fidelity": doc.fidelity,
        "text_chars": len(doc.text),
        "metadata": doc.meta,
        "warnings": doc.warnings,
    }
    return json.dumps(payload, indent=2, default=str)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def tool_docs_outline(args: dict) -> str:
    doc = parse_cached(Path(args["path"]))
    limit = int(args.get("max_headings", 200))
    out: list[str] = []
    for number, line in enumerate(doc.text.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if match:
            out.append(f"{number:>6}  {match.group(1)} {match.group(2)}")
            if len(out) >= limit:
                break
    if not out:
        return f"{doc.path}: no headings found ({doc.kind}, {len(doc.text)} chars)."
    return f"{doc.path} ({doc.kind}, {len(doc.text)} chars)\n\n" + "\n".join(out)


def tool_docs_list_types(args: dict) -> str:
    supported = supported_extensions()
    counts: dict[str, int] = {}
    for path in walk_files(Path(args["root"]), None, [], None):
        counts[path.suffix.lower() or "(none)"] = counts.get(path.suffix.lower() or "(none)", 0) + 1
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    lines = [f"{count:>7}  {ext:<12} {supported.get(ext, 'no dedicated parser (text sniff / metadata only)')}"
             for ext, count in rows]
    return "\n".join(lines)


HANDLERS = {
    "docs_search": tool_docs_search,
    "docs_extract": tool_docs_extract,
    "docs_info": tool_docs_info,
    "docs_outline": tool_docs_outline,
    "docs_list_types": tool_docs_list_types,
}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "rdna-docparse", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        handler = HANDLERS.get(params.get("name", ""))
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"unknown tool {params.get('name')!r}"}}
        try:
            text = handler(params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:  # noqa: BLE001 - report to the client, never crash
            result = {"content": [{"type": "text",
                                   "text": f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"}],
                      "isError": True}
    elif method in ("ping",):
        result = {}
    elif method is not None and method.startswith("notifications/"):
        return None
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
