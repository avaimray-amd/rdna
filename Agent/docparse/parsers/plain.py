"""Text-ish formats, plus a few wrappers (.rtf, .gz, .ipynb, .csv, .html)."""

from __future__ import annotations

import csv as _csv
import gzip
import io
import json
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from ..core import ParsedDoc, ParseError, clean_text, markdown_table, read_bytes, register

TEXT_EXTS = [
    # documentation / markup
    ".txt", ".md", ".markdown", ".adoc", ".asciidoc", ".asc", ".rst", ".org", ".qmd",
    ".bib", ".tex", ".puml", ".gv", ".mmd", ".inv",
    # config / data
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env",
    ".list", ".map", ".def", ".vec", ".edn", ".hvp", ".psv", ".pipe", ".tm7", ".rdl",
    ".filters", ".vcxproj", ".sln", ".user", ".props", ".targets", ".manifest",
    # source
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh", ".cxx", ".inl", ".cl", ".cu",
    ".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".rb", ".pl", ".pm", ".sh", ".bash",
    ".ps1", ".psm1", ".bat", ".cmd", ".tcl", ".m", ".lua", ".go", ".rs", ".java",
    ".sv", ".svh", ".v", ".vh", ".vhd", ".sva", ".s", ".asm", ".rc", ".idl",
    ".cmake", ".mk", ".make", ".mak", ".gradle", ".spec",
    ".css", ".scss", ".less", ".sql", ".diff", ".patch", ".log", ".lst",
]

_ENCODINGS = ("utf-8-sig", "utf-16", "cp1252", "latin-1")


def decode(data: bytes) -> tuple[str, str]:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "utf-16"
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", "replace"), "latin-1"


def sniff_is_text(path: Path, probe: int = 8192) -> bool:
    with open(path, "rb") as fh:
        chunk = fh.read(probe)
    if not chunk:
        return True
    if b"\x00" in chunk and not chunk[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False
    printable = sum(1 for b in chunk if 0x20 <= b < 0x7F or b in (9, 10, 13) or b >= 0x80)
    return printable / len(chunk) > 0.90


@register(TEXT_EXTS, "Plain text / markup / source code")
def parse_text_like(path: Path) -> ParsedDoc:
    text, encoding = decode(read_bytes(path))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return ParsedDoc(
        str(path), "text", text.strip(),
        {"encoding": encoding, "lines": text.count("\n") + 1},
    )


@register([".xml"], "XML document")
def parse_xml(path: Path) -> ParsedDoc:
    text, encoding = decode(read_bytes(path))
    root_match = re.search(r"<\s*([A-Za-z_][\w:.-]*)", text)
    meta = {"encoding": encoding, "root_element": root_match.group(1) if root_match else "?"}
    return ParsedDoc(str(path), "xml", text.strip(), meta)


@register([".json", ".jsonc", ".jsonl", ".geojson"], "JSON data")
def parse_json(path: Path) -> ParsedDoc:
    text, encoding = decode(read_bytes(path))
    meta: dict[str, object] = {"encoding": encoding}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            meta["top_level_keys"] = ", ".join(list(data)[:40])
        elif isinstance(data, list):
            meta["items"] = len(data)
        text = json.dumps(data, indent=2, ensure_ascii=False)
    except json.JSONDecodeError as exc:
        meta["parse_error"] = str(exc)
    return ParsedDoc(str(path), "json", text.strip(), meta)


@register([".csv", ".tsv"], "Delimited table")
def parse_csv(path: Path, max_rows: int = 5000) -> ParsedDoc:
    text, encoding = decode(read_bytes(path))
    sample = text[:8192]
    try:
        dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except _csv.Error:
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[list[str]] = []
    warnings: list[str] = []
    reader = _csv.reader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        rows.append([c.strip() for c in row])
        if len(rows) >= max_rows:
            warnings.append(f"truncated at {max_rows} rows")
            break
    meta = {"encoding": encoding, "delimiter": repr(delimiter), "rows": len(rows),
            "columns": max((len(r) for r in rows), default=0)}
    return ParsedDoc(str(path), "csv", markdown_table(rows), meta, warnings)


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")
        if tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if not self._skip and data.strip():
            self.parts.append(data.strip())


@register([".html", ".htm", ".xhtml"], "HTML page")
def parse_html(path: Path) -> ParsedDoc:
    text, encoding = decode(read_bytes(path))
    stripper = _Stripper()
    stripper.feed(text)
    body = re.sub(r"[ \t]{2,}", " ", " ".join(stripper.parts))
    return ParsedDoc(str(path), "html", clean_text(body),
                     {"encoding": encoding, "title": stripper.title})


# --------------------------------------------------------------------------
# RTF
# --------------------------------------------------------------------------

_RTF_SPECIAL = {"par": "\n", "line": "\n", "tab": "\t", "page": "\n\n",
                "sect": "\n\n", "cell": "\t", "row": "\n", "emdash": "-",
                "endash": "-", "lquote": "'", "rquote": "'",
                "ldblquote": '"', "rdblquote": '"', "bullet": "- ", "~": " "}
_RTF_SKIP_DESTS = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
                   "themedata", "colorschememapping", "latentstyles", "datastore",
                   "generator", "listtable", "listoverridetable", "rsidtbl", "xmlnstbl"}
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)?[ ]?|\\'([0-9a-fA-F]{2})|\\([\\{}])|([{}])|([^\\{}]+)")


@register([".rtf"], "Rich Text Format")
def parse_rtf(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    if not data.lstrip()[:5].startswith(b"{\\rt"):
        raise ParseError("missing {\\rtf header")
    text = data.decode("latin-1")
    out: list[str] = []
    depth = 0
    skip_depth: int | None = None
    ucskip = 1
    pending_unicode = 0
    for match in _RTF_TOKEN.finditer(text):
        word, arg, hexval, escaped, brace, literal = match.groups()
        if brace == "{":
            depth += 1
            continue
        if brace == "}":
            if skip_depth is not None and depth <= skip_depth:
                skip_depth = None
            depth -= 1
            continue
        if skip_depth is not None:
            continue
        if word:
            if word in _RTF_SKIP_DESTS:
                skip_depth = depth
            elif word == "u":
                try:
                    code = int(arg or "0")
                except ValueError:
                    code = 0
                out.append(chr(code + 65536 if code < 0 else code))
                pending_unicode = ucskip
            elif word == "uc":
                ucskip = int(arg or "1")
            elif word in _RTF_SPECIAL:
                out.append(_RTF_SPECIAL[word])
            continue
        if hexval:
            if pending_unicode:
                pending_unicode -= 1
                continue
            out.append(bytes([int(hexval, 16)]).decode("cp1252", "replace"))
            continue
        if escaped:
            out.append(escaped)
            continue
        if literal:
            if pending_unicode:
                consumed = min(pending_unicode, len(literal))
                literal = literal[consumed:]
                pending_unicode -= consumed
            out.append(literal)
    return ParsedDoc(str(path), "rtf", clean_text("".join(out)), {})


# --------------------------------------------------------------------------
# Notebook + gzip wrapper
# --------------------------------------------------------------------------


@register([".ipynb"], "Jupyter notebook")
def parse_ipynb(path: Path) -> ParsedDoc:
    text, _ = decode(read_bytes(path))
    try:
        nb = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid notebook JSON: {exc}") from exc
    lang = nb.get("metadata", {}).get("kernelspec", {}).get("language", "python")
    chunks: list[str] = []
    for index, cell in enumerate(nb.get("cells", []), start=1):
        source = "".join(cell.get("source", []))
        kind = cell.get("cell_type")
        if kind == "markdown":
            chunks.append(source)
        elif kind == "code":
            chunks.append(f"<!-- cell {index} -->\n```{lang}\n{source}\n```")
            for output in cell.get("outputs", []):
                data = output.get("text") or output.get("data", {}).get("text/plain")
                if data:
                    body = "".join(data) if isinstance(data, list) else str(data)
                    chunks.append("```output\n" + body.strip()[:2000] + "\n```")
    return ParsedDoc(str(path), "notebook", clean_text("\n\n".join(chunks)),
                     {"cells": len(nb.get("cells", [])), "language": lang})


@register([".gz", ".tgz"], "gzip-compressed file or tarball (contents are parsed)")
def parse_gzip(path: Path, max_member_bytes: int = 2_000_000) -> ParsedDoc:
    from ..core import parse as _parse

    inner_name = Path(path.stem)
    inner_suffix = inner_name.suffix or ".txt"
    if path.suffix.lower() == ".tgz":
        inner_suffix = ".tar"
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    if inner_suffix == ".tar":
        return _parse_tar(path, data, max_member_bytes)
    tmp = Path(tempfile.gettempdir()) / f"docparse_{path.stem}{inner_suffix}"
    tmp.write_bytes(data)
    try:
        doc = _parse(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    doc.path = str(path)
    doc.meta["gzip_inner"] = inner_name.name
    doc.warnings.append("content was gzip-compressed")
    return doc


def _parse_tar(path: Path, data: bytes, max_member_bytes: int) -> ParsedDoc:
    import tarfile

    listing: list[str] = []
    chunks: list[str] = []
    warnings: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            listing.append(f"{member.size:>10}  {member.name}")
            suffix = Path(member.name).suffix.lower()
            if suffix not in TEXT_EXTS and suffix not in (".xml", ".json", ".csv"):
                continue
            if member.size > max_member_bytes:
                warnings.append(f"{member.name} skipped ({member.size} bytes)")
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            body, _enc = decode(handle.read())
            chunks.append(f"<!-- {member.name} -->\n{body.strip()}")
    text = "## Archive contents\n\n```\n" + "\n".join(listing) + "\n```\n"
    if chunks:
        text += "\n\n" + "\n\n".join(chunks)
    return ParsedDoc(str(path), "tar.gz", clean_text(text),
                     {"members": len(listing), "text_members": len(chunks)},
                     warnings, fidelity="partial")
