"""Core types and helpers shared by every parser.

Everything in this package is READ-ONLY: no parser ever opens a source file
for writing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable


MAX_INLINE_BYTES = 512 * 1024 * 1024


class ParseError(RuntimeError):
    """Raised when a file cannot be parsed at all."""


@dataclasses.dataclass
class ParsedDoc:
    """Result of parsing one file."""

    path: str
    kind: str
    text: str = ""
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)
    fidelity: str = "full"  # full | partial | heuristic | metadata-only

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def render(self, max_chars: int | None = None, show_meta: bool = True) -> str:
        out: list[str] = []
        if show_meta:
            out.append(f"# {Path(self.path).name}")
            out.append("")
            out.append(f"- source: `{self.path}`")
            out.append(f"- kind: {self.kind}")
            out.append(f"- fidelity: {self.fidelity}")
            for key, value in self.meta.items():
                if value in (None, "", [], {}):
                    continue
                out.append(f"- {key}: {_one_line(value)}")
            for warning in self.warnings:
                out.append(f"- WARNING: {warning}")
            out.append("")
            out.append("---")
            out.append("")
        body = self.text
        if max_chars is not None and len(body) > max_chars:
            note = f"\n\n[... truncated, {len(self.text) - max_chars} more chars ...]" if max_chars else ""
            body = body[:max_chars] + note
        out.append(body)
        return "\n".join(out)


def _one_line(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= 300 else text[:300] + "..."


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[[Path], ParsedDoc]] = {}
_DESCRIPTIONS: dict[str, str] = {}


def register(extensions: Iterable[str], description: str):
    """Decorator registering a parser for one or more lowercase extensions."""

    def deco(func: Callable[[Path], ParsedDoc]):
        for ext in extensions:
            _REGISTRY[ext.lower()] = func
            _DESCRIPTIONS[ext.lower()] = description
        return func

    return deco


def supported_extensions() -> dict[str, str]:
    return dict(sorted(_DESCRIPTIONS.items()))


def get_parser(ext: str) -> Callable[[Path], ParsedDoc] | None:
    return _REGISTRY.get(ext.lower())


def parse(path: str | os.PathLike[str]) -> ParsedDoc:
    """Parse any supported file. Falls back to a text/binary sniffer."""
    from . import parsers  # noqa: F401  (populates the registry)

    p = Path(path)
    if not p.is_file():
        raise ParseError(f"not a file: {p}")
    parser = get_parser(p.suffix)
    if parser is None:
        parser = _fallback
    try:
        doc = parser(p)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill a batch
        raise ParseError(f"{type(exc).__name__}: {exc}") from exc
    doc.meta.setdefault("bytes", p.stat().st_size)
    return doc


def _fallback(path: Path) -> ParsedDoc:
    from .parsers.plain import parse_text_like, sniff_is_text

    if sniff_is_text(path):
        doc = parse_text_like(path)
        doc.warnings.append("no dedicated parser; treated as plain text")
        return doc
    return ParsedDoc(
        path=str(path),
        kind="binary",
        text="",
        meta={"extension": path.suffix},
        warnings=["unsupported binary format; only size metadata available"],
        fidelity="metadata-only",
    )


# --------------------------------------------------------------------------
# small utilities used by parsers
# --------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Normalise line endings and collapse runs of >2 blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def markdown_table(rows: list[list[str]]) -> str:
    """Render rows as a GitHub markdown table (first row = header)."""
    rows = [[(c or "").replace("|", "\\|").replace("\n", "<br>") for c in row] for row in rows if row]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def printable_runs(data: bytes, min_len: int = 4) -> list[str]:
    """Extract ASCII and UTF-16LE string runs from a binary blob."""
    found: list[str] = []
    ascii_re = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
    found += [m.group().decode("ascii") for m in ascii_re.finditer(data)]
    utf16_re = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len)
    found += [m.group().decode("utf-16-le", "ignore") for m in utf16_re.finditer(data)]
    return found


def file_key(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()


def require(module: str, package: str | None = None):
    """Import an optional dependency with an actionable error message."""
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - environment dependent
        pkg = package or module
        raise ParseError(
            f"missing optional dependency '{pkg}'. Install with: "
            f"{Path(sys.executable).name} -m pip install {pkg}"
        ) from exc


def read_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_INLINE_BYTES:
        raise ParseError(f"file too large to load ({path.stat().st_size} bytes)")
    with open(path, "rb") as fh:
        return fh.read()


def bytes_io(path: Path) -> io.BytesIO:
    return io.BytesIO(read_bytes(path))
