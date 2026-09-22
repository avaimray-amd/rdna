"""Legacy OLE2 (Compound File Binary) formats.

Covers the pre-2007 Office binaries plus a few other CFB-based formats:
    .doc .dot   Word 6/95/97-2003
    .xls .xlt   Excel 97-2003 (via xlrd)
    .ppt .pot   PowerPoint 97-2003
    .vsd        Visio 2003-2010 (heuristic)
    .msg        Outlook message
    .one .onetoc2  OneNote (heuristic)
    .mpp        MS Project (metadata only)
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from ..core import (
    ParsedDoc,
    ParseError,
    clean_text,
    markdown_table,
    printable_runs,
    read_bytes,
    register,
    require,
)

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _olefile():
    return require("olefile")


def _open_ole(path: Path):
    olefile = _olefile()
    if not olefile.isOleFile(str(path)):
        raise ParseError("not an OLE2 compound file")
    return olefile.OleFileIO(str(path))


def _ole_meta(ole) -> dict[str, str]:
    meta: dict[str, str] = {}
    try:
        raw = ole.get_metadata()
    except Exception:  # noqa: BLE001 - metadata is best effort
        return meta
    for attr in ("title", "subject", "author", "last_saved_by", "revision_number",
                 "create_time", "last_saved_time", "num_pages", "num_words", "company"):
        value = getattr(raw, attr, None)
        if isinstance(value, bytes):
            value = value.decode("cp1252", "replace")
        if value:
            meta[attr] = str(value).strip()
    return meta


def _redirect_if_not_ole(path: Path) -> ParsedDoc | None:
    """Handle files with a legacy extension that are really something else."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"PK\x03\x04"):
        from . import ooxml

        ext = path.suffix.lower()
        parser = {
            ".doc": ooxml.parse_docx, ".dot": ooxml.parse_docx,
            ".xls": ooxml.parse_xlsx, ".xlt": ooxml.parse_xlsx,
            ".ppt": ooxml.parse_pptx, ".pot": ooxml.parse_pptx,
            ".vsd": ooxml.parse_vsdx,
        }.get(ext, ooxml.parse_docx)
        doc = parser(path)
        doc.warnings.append("file has a legacy extension but is really a ZIP/OOXML file")
        return doc
    if head.startswith(b"{\\rt"):
        from .plain import parse_rtf

        doc = parse_rtf(path)
        doc.warnings.append("file has a .doc extension but is really RTF")
        return doc
    if head.startswith(b"\x0d\x0aDOT") or head[:4] == b"digr":
        return None
    if not head.startswith(CFB_MAGIC):
        return None
    return None


# --------------------------------------------------------------------------
# Word 97-2003 (.doc)
# --------------------------------------------------------------------------

_DOC_CTRL = {
    0x07: "\n",   # cell / row mark
    0x0B: "\n",   # hard line break
    0x0C: "\n\n",  # page break
    0x0D: "\n",   # paragraph mark
    0x0E: "\n",   # column break
    0x1E: "-",    # non-breaking hyphen
    0x1F: "",     # optional hyphen
    0xA0: " ",    # non-breaking space
}
_DOC_DROP = {0x01, 0x02, 0x03, 0x04, 0x05, 0x08, 0x13, 0x14, 0x15, 0x28}


def _clean_doc_chars(raw: str) -> str:
    out: list[str] = []
    for ch in raw:
        code = ord(ch)
        if code in _DOC_DROP:
            continue
        out.append(_DOC_CTRL.get(code, ch))
    return "".join(out)


def _doc_pieces(wd: bytes, table: bytes, fc_clx: int, lcb_clx: int) -> str:
    clx = table[fc_clx: fc_clx + lcb_clx]
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:
        cb = struct.unpack_from("<H", clx, pos + 1)[0]
        pos += 3 + cb
    if pos >= len(clx) or clx[pos] != 0x02:
        raise ParseError("piece table (Pcdt) not found in CLX")
    lcb_pcdt = struct.unpack_from("<I", clx, pos + 1)[0]
    pcdt = clx[pos + 5: pos + 5 + lcb_pcdt]
    n = (len(pcdt) - 4) // 12
    if n <= 0:
        raise ParseError("empty piece table")
    cps = struct.unpack_from("<%dI" % (n + 1), pcdt, 0)
    pcd_base = 4 * (n + 1)
    chunks: list[str] = []
    for i in range(n):
        fc = struct.unpack_from("<I", pcdt, pcd_base + 8 * i + 2)[0]
        length = cps[i + 1] - cps[i]
        if length <= 0:
            continue
        if fc & 0x40000000:
            offset = (fc & 0x3FFFFFFF) // 2
            raw = wd[offset: offset + length].decode("cp1252", "replace")
        else:
            offset = fc
            raw = wd[offset: offset + length * 2].decode("utf-16-le", "replace")
        chunks.append(raw)
    return "".join(chunks)


@register([".doc", ".dot", ".wbk"], "Word 97-2003 binary document (or Graphviz .dot)")
def parse_doc(path: Path) -> ParsedDoc:
    redirected = _redirect_if_not_ole(path)
    if redirected is not None:
        return redirected
    if not _olefile().isOleFile(str(path)):
        from .plain import parse_text_like, sniff_is_text

        if sniff_is_text(path):
            doc = parse_text_like(path)
            doc.warnings.append("not a Word binary; parsed as plain text (e.g. Graphviz .dot)")
            return doc
        raise ParseError("not an OLE2 compound file and not text")

    ole = _open_ole(path)
    try:
        meta = _ole_meta(ole)
        if not ole.exists("WordDocument"):
            raise ParseError("no WordDocument stream (not a Word binary file)")
        wd = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", wd, 0x000A)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        n_fib = struct.unpack_from("<H", wd, 0x0002)[0]
        warnings: list[str] = []
        fidelity = "full"
        if flags & 0x8000:
            warnings.append("document is encrypted/password protected")
        if n_fib >= 193 and ole.exists(table_name):
            table = ole.openstream(table_name).read()
            fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
            if lcb_clx:
                raw = _doc_pieces(wd, table, fc_clx, lcb_clx)
            else:
                fc_min, fc_mac = struct.unpack_from("<II", wd, 0x0018)
                raw = wd[fc_min:fc_mac].decode("cp1252", "replace")
                fidelity = "partial"
        else:
            fc_min, fc_mac = struct.unpack_from("<II", wd, 0x0018)
            raw = wd[fc_min:fc_mac].decode("cp1252", "replace")
            fidelity = "partial"
            warnings.append("Word 6/95 format: simple text span extraction only")
        meta["fib_version"] = n_fib
    finally:
        ole.close()

    text = clean_text(_clean_doc_chars(raw))
    warnings.append("binary Word format: tables and headings are flattened to plain text")
    return ParsedDoc(str(path), "word-binary", text, meta, warnings, fidelity)


# --------------------------------------------------------------------------
# Excel 97-2003 (.xls)
# --------------------------------------------------------------------------


@register([".xls", ".xlt", ".xlsb"], "Excel 97-2003 binary workbook")
def parse_xls(path: Path, max_rows: int = 5000) -> ParsedDoc:
    redirected = _redirect_if_not_ole(path)
    if redirected is not None:
        return redirected
    if path.suffix.lower() == ".xlsb":
        raise ParseError(
            "the .xlsb binary workbook format is not supported; re-save as .xlsx"
        )

    xlrd = require("xlrd")
    warnings: list[str] = []
    try:
        book = xlrd.open_workbook(str(path), formatting_info=False, on_demand=True)
        sheets = book.sheets()
    except Exception as exc:  # noqa: BLE001 - xlrd raises bare AssertionError on odd files
        return _xls_string_fallback(path, f"{type(exc).__name__}: {exc}")

    chunks: list[str] = []
    for sheet in sheets:
        rows: list[list[str]] = []
        limit = min(sheet.nrows, max_rows)
        if sheet.nrows > max_rows:
            warnings.append(f"sheet '{sheet.name}' truncated at {max_rows} rows")
        for r in range(limit):
            row: list[str] = []
            for cell in sheet.row(r):
                row.append(_xls_cell(xlrd, book, cell))
            while row and not row[-1]:
                row.pop()
            rows.append(row)
        while rows and not any(rows[-1]):
            rows.pop()
        chunks.append(f"## Sheet: {sheet.name}\n")
        chunks.append(markdown_table(rows) if rows else "*(empty)*")
    meta = {"sheets": book.nsheets, "sheet_names": ", ".join(book.sheet_names())}
    return ParsedDoc(str(path), "excel-binary", clean_text("\n\n".join(chunks)), meta, warnings)


def _xls_string_fallback(path: Path, reason: str) -> ParsedDoc:
    """xlrd choked (very old BIFF, or a partly damaged workbook): recover strings."""
    ole = _open_ole(path)
    try:
        meta = _ole_meta(ole)
        blobs = []
        for entry in ole.listdir(streams=True, storages=False):
            if entry[-1].startswith("\x05"):
                continue
            try:
                blobs.append(ole.openstream(entry).read())
            except OSError:
                continue
    finally:
        ole.close()
    seen: dict[str, None] = {}
    for blob in blobs:
        for run in printable_runs(blob, min_len=4):
            run = run.strip()
            if len(run) >= 4 and re.search(r"[A-Za-z]{3}", run):
                seen.setdefault(run, None)
    return ParsedDoc(
        str(path), "excel-binary", clean_text("\n".join(seen)), meta,
        [f"xlrd could not read this workbook ({reason}); cell strings were recovered "
         "heuristically. Row/column structure and numeric values are NOT available."],
        fidelity="heuristic",
    )


def _xls_cell(xlrd, book, cell) -> str:
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            parts = xlrd.xldate_as_tuple(cell.value, book.datemode)
            if parts[:3] == (0, 0, 0):
                return "%02d:%02d:%02d" % parts[3:]
            return "%04d-%02d-%02d" % parts[:3]
        except Exception:  # noqa: BLE001
            return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return "#ERR"
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        value = cell.value
        return str(int(value)) if float(value).is_integer() and abs(value) < 1e15 else f"{value:g}"
    return str(cell.value).replace("\n", " ").strip()


# --------------------------------------------------------------------------
# PowerPoint 97-2003 (.ppt)
# --------------------------------------------------------------------------

_PPT_TEXT_CHARS = 0x0FA0
_PPT_TEXT_BYTES = 0x0FA8
_PPT_CSTRING = 0x0FBA
_PPT_SLIDE = 0x03EE


def _walk_ppt_records(data: bytes, start: int, end: int, out: list[str], depth: int = 0) -> None:
    pos = start
    while pos + 8 <= end and depth < 24:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", data, pos)
        pos += 8
        if rec_len > end - pos:
            rec_len = end - pos
        body_end = pos + rec_len
        if (ver_inst & 0x000F) == 0x000F:
            _walk_ppt_records(data, pos, body_end, out, depth + 1)
        elif rec_type == _PPT_SLIDE:
            out.append("\x00SLIDE\x00")
        elif rec_type == _PPT_TEXT_CHARS:
            out.append(data[pos:body_end].decode("utf-16-le", "replace"))
        elif rec_type in (_PPT_TEXT_BYTES, _PPT_CSTRING):
            if rec_type == _PPT_CSTRING:
                out.append(data[pos:body_end].decode("utf-16-le", "replace"))
            else:
                out.append(data[pos:body_end].decode("cp1252", "replace"))
        pos = body_end


@register([".ppt", ".pot", ".pps"], "PowerPoint 97-2003 binary presentation")
def parse_ppt(path: Path) -> ParsedDoc:
    redirected = _redirect_if_not_ole(path)
    if redirected is not None:
        return redirected

    ole = _open_ole(path)
    try:
        meta = _ole_meta(ole)
        stream_name = "PowerPoint Document"
        if not ole.exists(stream_name):
            raise ParseError("no 'PowerPoint Document' stream")
        data = ole.openstream(stream_name).read()
    finally:
        ole.close()

    parts: list[str] = []
    _walk_ppt_records(data, 0, len(data), parts)
    slide_no = 0
    lines: list[str] = []
    for part in parts:
        if part == "\x00SLIDE\x00":
            slide_no += 1
            lines.append(f"\n## Slide {slide_no}\n")
            continue
        text = part.replace("\r", "\n").replace("\x0b", "\n").replace("\x00", "")
        text = "\n".join(t.strip() for t in text.split("\n") if t.strip())
        if text:
            lines.append(text)
    meta["slides"] = slide_no
    return ParsedDoc(
        str(path), "powerpoint-binary", clean_text("\n".join(lines)), meta,
        ["binary PowerPoint: text is recovered from raw records, ordering may differ from the slide layout"],
        fidelity="partial",
    )


# --------------------------------------------------------------------------
# Visio 2003-2010 (.vsd)
# --------------------------------------------------------------------------


@register([".vsd", ".vss", ".vst"], "Visio 2003-2010 binary drawing")
def parse_vsd(path: Path) -> ParsedDoc:
    redirected = _redirect_if_not_ole(path)
    if redirected is not None:
        return redirected

    ole = _open_ole(path)
    try:
        meta = _ole_meta(ole)
        blobs: list[bytes] = []
        for entry in ole.listdir(streams=True, storages=False):
            name = entry[-1]
            if name.startswith("\x05"):
                continue
            try:
                blobs.append(ole.openstream(entry).read())
            except OSError:
                continue
    finally:
        ole.close()

    seen: dict[str, None] = {}
    for blob in blobs:
        for run in printable_runs(blob, min_len=4):
            run = run.strip()
            if len(run) < 4 or not re.search(r"[A-Za-z]{3}", run):
                continue
            seen.setdefault(run, None)
    text = "\n".join(seen)
    return ParsedDoc(
        str(path), "visio-binary", clean_text(text), meta,
        ["binary Visio: shape text is recovered heuristically from uncompressed streams; "
         "diagram structure and connections are NOT available. Prefer the .vsdx version if one exists."],
        fidelity="heuristic",
    )


# --------------------------------------------------------------------------
# Outlook message (.msg)
# --------------------------------------------------------------------------

_MSG_PROPS = {
    "0037": "subject",
    "0E1D": "normalized_subject",
    "0C1A": "sender_name",
    "0C1F": "sender_email",
    "0E04": "to",
    "0E03": "cc",
    "007D": "transport_headers",
    "0039": "client_submit_time",
}


@register([".msg", ".oft"], "Outlook message")
def parse_msg(path: Path) -> ParsedDoc:
    ole = _open_ole(path)
    try:
        meta: dict[str, str] = {}
        body = ""
        attachments: list[str] = []
        for entry in ole.listdir(streams=True, storages=False):
            name = entry[-1]
            if not name.startswith("__substg1.0_"):
                continue
            tag = name[12:16].upper()
            enc = name[16:20].upper()
            try:
                raw = ole.openstream(entry).read()
            except OSError:
                continue
            value = raw.decode("utf-16-le", "replace") if enc == "001F" else raw.decode("cp1252", "replace")
            value = value.replace("\x00", "").strip()
            if tag in _MSG_PROPS and value:
                meta.setdefault(_MSG_PROPS[tag], value.replace("\n", " ")[:400])
            elif tag == "1000" and value and len(value) > len(body):
                body = value
            elif tag == "3707" and value:
                attachments.append(value)
        if attachments:
            meta["attachments"] = ", ".join(sorted(set(attachments)))
    finally:
        ole.close()
    return ParsedDoc(str(path), "outlook-msg", clean_text(body), meta)


# --------------------------------------------------------------------------
# OneNote (.one) - no open format spec implementation, heuristic only
# --------------------------------------------------------------------------


@register([".one", ".onetoc2", ".onepkg"], "OneNote section (heuristic text recovery)")
def parse_one(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    runs = [r.strip() for r in printable_runs(data, min_len=6)]
    keep: list[str] = []
    seen: set[str] = set()
    noise = re.compile(
        r"^(?:[0-9A-Fa-f]{8}-|\{[0-9A-Fa-f-]+\}|Microsoft\.|<\?xml|http://schemas)"
    )
    for run in runs:
        run = re.sub(r"\s+", " ", run)
        if len(run) < 6 or run in seen or noise.match(run):
            continue
        letters = sum(ch.isalpha() or ch.isspace() for ch in run)
        if letters / len(run) < 0.6:
            continue
        seen.add(run)
        keep.append(run)
    return ParsedDoc(
        str(path), "onenote", clean_text("\n".join(keep)),
        {"strings_recovered": len(keep)},
        ["OneNote has no open parser here: text is recovered heuristically from raw strings. "
         "Formatting, page structure, ink and images are lost. Treat as a keyword index only."],
        fidelity="heuristic",
    )


# --------------------------------------------------------------------------
# MS Project (.mpp) - proprietary, metadata only
# --------------------------------------------------------------------------


@register([".mpp", ".mpt"], "MS Project plan (metadata only)")
def parse_mpp(path: Path) -> ParsedDoc:
    ole = _open_ole(path)
    try:
        meta = _ole_meta(ole)
        streams = ["/".join(e) for e in ole.listdir(streams=True, storages=False)]
    finally:
        ole.close()
    return ParsedDoc(
        str(path), "ms-project", "\n".join(sorted(streams)), meta,
        ["MS Project uses an undocumented binary format. Only OLE metadata and the stream "
         "list are available. To read task data, export to CSV/XLSX from Project, or use "
         "the Java MPXJ library."],
        fidelity="metadata-only",
    )
