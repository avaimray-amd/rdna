"""Exercise the MCP server end to end over a pipe (initialize, list, call)."""

from __future__ import annotations

import json
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs-root", type=Path, help="Optional hardware Docs root for SX document checks")
    args = parser.parse_args()
    docs = args.docs_root / "gfx12/block/sx" if args.docs_root else ROOT / "README.md"
    outline = docs / "GFX12_SX_MAS.docx" if args.docs_root else docs
    info = docs / "GFX12_SX_block_diagram.vsdx" if args.docs_root else docs
    python = str(PYTHON if PYTHON.is_file() else sys.executable)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "docs_search",
                    "arguments": {"root": str(docs), "pattern": "wave64" if args.docs_root else "docparse", "max_files": 3}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "docs_outline",
                    "arguments": {"path": str(outline), "max_headings": 8}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "docs_info", "arguments": {"path": str(info)}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run([python, str(ROOT / "mcp_server.py")], input=payload,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    if proc.stderr.strip():
        print("STDERR:", proc.stderr[:2000])
    failures = int(proc.returncode != 0)
    response_ids = []
    for line in proc.stdout.splitlines():
        message = json.loads(line)
        response_ids.append(message.get("id"))
        if "error" in message:
            status = "expected-error" if message["id"] == 6 else "ERROR"
            if status == "ERROR":
                failures += 1
            print(f"id={message['id']} {status}: {message['error']['message']}")
            continue
        result = message["result"]
        if "tools" in result:
            print(f"id={message['id']} tools: {[t['name'] for t in result['tools']]}")
        elif "content" in result:
            text = result["content"][0]["text"]
            if result.get("isError"):
                failures += 1
            print(f"id={message['id']} isError={result.get('isError')} "
                  f"{len(text)} chars\n    " + "\n    ".join(text.splitlines()[:6]))
        else:
            print(f"id={message['id']} {json.dumps(result)[:120]}")
    if sorted(response_ids) != [1, 2, 3, 4, 5, 6]:
        print(f"Unexpected response IDs: {response_ids}")
        failures += 1
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
