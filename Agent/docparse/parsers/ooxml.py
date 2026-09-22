"""Office Open XML formats: .docx .docm .dotx .xlsx .xlsm .pptx .pptm .vsdx .vssx

All of these are ZIP containers full of XML, so they are handled with the
standard library only (zipfile + xml.etree). Nothing is ever written back.
"""

from __future__ import annotations

import datetime as _dt
import io
import re
import struct
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

from ..core import ParsedDoc, ParseError, clean_text, markdown_table, read_bytes, register

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCTERMS = "{http://purl.org/dc/terms/}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
V = "{http://schemas.microsoft.com/office/visio/2012/main}"


_ZIP_NAME_OK = re.compile(r"^[A-Za-z0-9_./\-+ ()\[\]&,'#@%!~$]{1,255}$")


class SalvagedZip:
    """Best-effort reader for a damaged ZIP: scans local file headers directly.

    Entries whose deflate stream is corrupt are kept up to the point of failure,
    which is usually enough to still recover most of the text.
    """

    damaged = True

    def __init__(self, data: bytes) -> None:
        self._entries: dict[str, bytes] = {}
        self.partial: list[str] = []
        pos = 0
        while True:
            index = data.find(b"PK\x03\x04", pos)
            if index < 0:
                break
            pos = index + 4
            try:
                _v, _f, method, _t, _d, _crc, _csz, usz, name_len, extra_len = struct.unpack_from(
                    "<HHHHHIIIHH", data, index + 4
                )
            except struct.error:
                break
            name = data[index + 30: index + 30 + name_len].decode("utf-8", "replace")
            body = index + 30 + name_len + extra_len
            if not name or name.endswith("/") or not _ZIP_NAME_OK.match(name):
                continue
            if method == 0:
                self._entries[name] = data[body: body + usz]
                continue
            if method != 8:
                continue
            decompressor = zlib.decompressobj(-15)
            out = bytearray()
            try:
                for offset in range(body, len(data), 65536):
                    out += decompressor.decompress(data[offset: offset + 65536])
                    if decompressor.eof:
                        break
                else:
                    out += decompressor.flush()
            except zlib.error:
                self.partial.append(name)
            if out:
                self._entries[name] = bytes(out)

    def namelist(self) -> list[str]:
        return list(self._entries)

    def read(self, name: str) -> bytes:
        try:
            return self._entries[name]
        except KeyError:
            raise KeyError(name) from None

    def open(self, name: str):
        return io.BytesIO(self.read(name))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_MARKER = b"E\x00n\x00c\x00r\x00y\x00p\x00t\x00e\x00d\x00P\x00a\x00c\x00k\x00a\x00g\x00e"


def _redirect_if_legacy(path: Path) -> ParsedDoc | None:
    """A modern extension on a legacy OLE2 binary: hand it to the right parser."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head != CFB_MAGIC:
        return None
    if _ENCRYPTED_MARKER in read_bytes(path)[:131072]:
        raise ParseError(
            "encrypted OOXML package (password or Information Rights Management). "
            "Open it in Office with your credentials and save a decrypted copy if "
            "the content is needed."
        )
    from . import ole

    ext = path.suffix.lower()
    parser = None
    if ext.startswith((".doc", ".dot")):
        parser = ole.parse_doc
    elif ext.startswith(".xl"):
        parser = ole.parse_xls
    elif ext.startswith(".p"):
        parser = ole.parse_ppt
    elif ext.startswith((".vs", ".vst")):
        parser = ole.parse_vsd
    if parser is None:
        raise ParseError("OLE2 compound file with an unexpected extension")
    doc = parser(path)
    doc.warnings.append(
        f"{ext} extension but the file is really a legacy OLE2 binary; "
        "parsed with the legacy reader"
    )
    return doc


def _open(path: Path):
    try:
        zf = zipfile.ZipFile(path)
        zf.damaged = False  # type: ignore[attr-defined]
        return zf
    except zipfile.BadZipFile:
        pass
    data = read_bytes(path)
    if data[:8] == CFB_MAGIC:
        raise ParseError("OLE2 compound file, not an OOXML package")
    if b"PK\x03\x04" not in data[:4096]:
        raise ParseError(
            "not a valid OOXML/ZIP container (encrypted, corrupt, or actually a "
            "legacy binary Office file with a modern extension)"
        )
    salvaged = SalvagedZip(data)
    if not salvaged.namelist():
        raise ParseError("ZIP container is damaged and nothing could be salvaged")
    return salvaged


_XML_TEXT_RE = re.compile(r">([^<>]{2,400})<")
_HAS_WORD = re.compile(r"[A-Za-z]{2}")


def _raw_text_sweep(zf, prefixes: tuple[str, ...]) -> list[str]:
    """Last resort for a damaged container: pull text nodes out of whatever bytes
    were recovered, without requiring the XML to be well formed."""
    found: dict[str, None] = {}
    for name in zf.namelist():
        if not name.startswith(prefixes):
            continue
        try:
            data = zf.read(name)
        except (KeyError, OSError):
            continue
        for match in _XML_TEXT_RE.finditer(data.decode("utf-8", "replace")):
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if len(value) >= 2 and _HAS_WORD.search(value):
                found.setdefault(value, None)
    return list(found)


def _damage_warnings(zf) -> list[str]:
    if not getattr(zf, "damaged", False):
        return []
    note = ("ZIP container is damaged; entries were recovered by scanning local file "
            "headers. Content may be incomplete or garbled.")
    partial = getattr(zf, "partial", [])
    if partial:
        note += " Truncated parts: " + ", ".join(sorted(partial)[:10])
    return [note]


def _xml(zf, name: str) -> ET.Element | None:
    try:
        data = zf.read(name)
    except (KeyError, zipfile.BadZipFile, zlib.error, OSError):
        return None
    if not data.strip():
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return _repair_xml(data)


_TAG_SCAN = re.compile(r'''<(/?)([A-Za-z_][\w:.-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(/?)>''', re.S)
_BARE_AMP = re.compile(r"&(?![a-zA-Z]{1,10};|#\d{1,6};|#x[0-9a-fA-F]{1,5};)")


def _repair_xml(data: bytes) -> ET.Element | None:
    """Salvage a truncated XML part by closing every element left open."""
    text = data.decode("utf-8", "replace")
    cut = text.rfind(">")
    if cut <= 0:
        return None
    text = _BARE_AMP.sub("&amp;", text[: cut + 1])
    stack: list[str] = []
    for match in _TAG_SCAN.finditer(text):
        closing, name, _attrs, self_close = match.groups()
        if closing:
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        elif not self_close:
            stack.append(name)
    try:
        return ET.fromstring(text + "".join(f"</{name}>" for name in reversed(stack)))
    except ET.ParseError:
        return None


def _core_props(zf) -> dict[str, str]:
    root = _xml(zf, "docProps/core.xml")
    meta: dict[str, str] = {}
    if root is None:
        return meta
    for tag, key in (
        (DC + "title", "title"),
        (DC + "subject", "subject"),
        (DC + "creator", "author"),
        (CP + "lastModifiedBy", "last_modified_by"),
        (CP + "revision", "revision"),
        (DCTERMS + "created", "created"),
        (DCTERMS + "modified", "modified"),
        (CP + "category", "category"),
        (CP + "keywords", "keywords"),
    ):
        node = root.find(tag)
        if node is not None and node.text:
            meta[key] = node.text.strip()
    app = _xml(zf, "docProps/app.xml")
    if app is not None:
        for node in app:
            local = node.tag.split("}")[-1]
            if local in ("Pages", "Slides", "Words", "Company", "Application", "TitlesOfParts"):
                if node.text and node.text.strip():
                    meta[local.lower()] = node.text.strip()
    return meta


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------


def _para_text(node: ET.Element) -> str:
    parts: list[str] = []
    for el in node.iter():
        tag = el.tag
        if tag == W + "t":
            parts.append(el.text or "")
        elif tag == W + "tab":
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            parts.append("\n")
        elif tag == W + "noBreakHyphen":
            parts.append("-")
        elif tag == WP + "docPr":
            label = el.get("descr") or el.get("name") or "image"
            parts.append(f"[image: {label}]")
        elif tag == W + "instrText" and el.text and "HYPERLINK" in el.text:
            match = re.search(r'HYPERLINK\s+"([^"]+)"', el.text)
            if match:
                parts.append(f"<{match.group(1)}>")
    return "".join(parts).strip()


def _para_style(node: ET.Element) -> tuple[str, bool]:
    ppr = node.find(W + "pPr")
    if ppr is None:
        return "", False
    style_node = ppr.find(W + "pStyle")
    style = (style_node.get(W + "val") or "") if style_node is not None else ""
    is_list = ppr.find(W + "numPr") is not None
    return style, is_list


_HEADING_RE = re.compile(r"^(?:heading|hd|ttl)(\d)", re.I)


def _render_para(node: ET.Element) -> str:
    text = _para_text(node)
    if not text:
        return ""
    style, is_list = _para_style(node)
    match = _HEADING_RE.match(style)
    if match:
        level = min(int(match.group(1)), 6)
        return "#" * level + " " + text
    if style.lower() in ("title", "doctitle"):
        return "# " + text
    if is_list:
        return "- " + text.replace("\n", " ")
    if style.lower().startswith("caption"):
        return f"*{text}*"
    return text


def _table_rows(tbl: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in tbl.findall(W + "tr"):
        row: list[str] = []
        for tc in tr.findall(W + "tc"):
            cell_parts = [_para_text(p) for p in tc.findall(W + "p")]
            for inner in tc.findall(W + "tbl"):
                cell_parts.append("[nested table]")
                rows_inner = _table_rows(inner)
                cell_parts += [" / ".join(r) for r in rows_inner]
            row.append("\n".join(x for x in cell_parts if x))
        rows.append(row)
    return rows


def _walk_body(body: ET.Element) -> list[str]:
    out: list[str] = []
    for child in body:
        if child.tag == W + "p":
            rendered = _render_para(child)
            if rendered:
                out.append(rendered)
        elif child.tag == W + "tbl":
            table = markdown_table(_table_rows(child))
            if table:
                out.append("")
                out.append(table)
                out.append("")
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                out += _walk_body(content)
    return out


@register([".docx", ".docm", ".dotx", ".dotm"], "Word 2007+ document (OOXML)")
def parse_docx(path: Path) -> ParsedDoc:
    redirected = _redirect_if_legacy(path)
    if redirected is not None:
        return redirected
    with _open(path) as zf:
        meta = _core_props(zf)
        warnings = _damage_warnings(zf)
        salvage = _raw_text_sweep(zf, ("word/",)) if warnings else []
        root = _xml(zf, "word/document.xml")
        if root is None:
            raise ParseError("word/document.xml missing or unparseable")
        body = root.find(W + "body")
        blocks = _walk_body(body if body is not None else root)

        extras: list[str] = []
        for name in sorted(n for n in zf.namelist() if re.match(r"word/(header|footer)\d*\.xml", n)):
            sub = _xml(zf, name)
            if sub is None:
                continue
            lines = [t for t in (_para_text(p) for p in sub.iter(W + "p")) if t]
            if lines:
                extras.append(f"\n## [{Path(name).stem}]\n\n" + "\n".join(lines))
        for name, label in (
            ("word/footnotes.xml", "Footnotes"),
            ("word/endnotes.xml", "Endnotes"),
            ("word/comments.xml", "Comments"),
        ):
            sub = _xml(zf, name)
            if sub is None:
                continue
            lines = [t for t in (_para_text(p) for p in sub.iter(W + "p")) if t]
            if lines:
                extras.append(f"\n## [{label}]\n\n" + "\n".join(lines))

    text = clean_text("\n\n".join(blocks) + "\n" + "\n".join(extras))
    if not text and salvage:
        text = "\n".join(salvage)
    meta["paragraphs"] = len(blocks)
    return ParsedDoc(str(path), "word-ooxml", text, meta, warnings,
                     "partial" if warnings else "full")


# --------------------------------------------------------------------------
# PowerPoint
# --------------------------------------------------------------------------


def _shape_text(shape: ET.Element) -> str:
    paras: list[str] = []
    for para in shape.iter(A + "p"):
        runs = [(t.text or "") for t in para.iter(A + "t")]
        line = "".join(runs).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def _slide_body(root: ET.Element) -> list[str]:
    out: list[str] = []
    seen: set[int] = set()
    for tbl in root.iter(A + "tbl"):
        for node in tbl.iter():
            seen.add(id(node))
        rows = []
        for tr in tbl.findall(A + "tr"):
            rows.append([_shape_text(tc).replace("\n", " ") for tc in tr.findall(A + "tc")])
        table = markdown_table(rows)
        if table:
            out.append(table)
    for shape in root.iter():
        if shape.tag not in (P + "sp", P + "pic"):
            continue
        if id(shape) in seen:
            continue
        text = _shape_text(shape)
        if text:
            out.append(text)
        elif shape.tag == P + "pic":
            name = ""
            for nv in shape.iter(P + "cNvPr"):
                name = nv.get("descr") or nv.get("name") or ""
            if name:
                out.append(f"[image: {name}]")
    return out


def _slide_number(name: str) -> int:
    match = re.search(r"(\d+)", Path(name).stem)
    return int(match.group(1)) if match else 0


@register([".pptx", ".pptm", ".potx"], "PowerPoint 2007+ presentation (OOXML)")
def parse_pptx(path: Path) -> ParsedDoc:
    redirected = _redirect_if_legacy(path)
    if redirected is not None:
        return redirected
    chunks: list[str] = []
    with _open(path) as zf:
        meta = _core_props(zf)
        warnings = _damage_warnings(zf)
        salvage = _raw_text_sweep(zf, ("ppt/",)) if warnings else []
        slides = sorted(
            (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=_slide_number,
        )
        for name in slides:
            root = _xml(zf, name)
            if root is None:
                continue
            num = _slide_number(name)
            body = _slide_body(root)
            chunks.append(f"## Slide {num}\n\n" + ("\n\n".join(body) if body else "*(no text)*"))
            notes_name = f"ppt/notesSlides/notesSlide{num}.xml"
            notes_root = _xml(zf, notes_name)
            if notes_root is not None:
                notes = [t for t in ("".join(x.text or "" for x in p.iter(A + "t")).strip()
                                     for p in notes_root.iter(A + "p")) if t]
                notes = [n for n in notes if not n.isdigit()]
                if notes:
                    chunks.append("**Speaker notes:** " + " ".join(notes))
        meta["slides"] = len(slides)
    text = clean_text("\n\n".join(chunks))
    if not text and salvage:
        text = "\n".join(salvage)
    return ParsedDoc(str(path), "powerpoint-ooxml", text, meta,
                     warnings, "partial" if warnings else "full")


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

_BUILTIN_DATE_FMTS = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57, 59}
_DATE_CHARS = re.compile(r"(?<!\\)[ymdhs]", re.I)
_EXCEL_EPOCH = _dt.datetime(1899, 12, 30)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    root = _xml(zf, "xl/sharedStrings.xml")
    if root is None:
        return []
    out: list[str] = []
    for si in root.findall(S + "si"):
        out.append("".join(t.text or "" for t in si.iter(S + "t")))
    return out


def _date_style_ids(zf: zipfile.ZipFile) -> set[int]:
    root = _xml(zf, "xl/styles.xml")
    if root is None:
        return set()
    custom: dict[int, str] = {}
    numfmts = root.find(S + "numFmts")
    if numfmts is not None:
        for nf in numfmts.findall(S + "numFmt"):
            try:
                custom[int(nf.get("numFmtId", "-1"))] = nf.get("formatCode", "")
            except ValueError:
                continue
    date_ids: set[int] = set()
    cell_xfs = root.find(S + "cellXfs")
    if cell_xfs is None:
        return date_ids
    for idx, xf in enumerate(cell_xfs.findall(S + "xf")):
        try:
            fmt_id = int(xf.get("numFmtId", "0"))
        except ValueError:
            continue
        if fmt_id in _BUILTIN_DATE_FMTS:
            date_ids.add(idx)
        elif fmt_id in custom:
            code = re.sub(r'"[^"]*"', "", custom[fmt_id])
            code = re.sub(r"\[[^\]]*\]", "", code)
            if _DATE_CHARS.search(code):
                date_ids.add(idx)
    return date_ids


def _sheet_targets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    wb = _xml(zf, "xl/workbook.xml")
    rels = _xml(zf, "xl/_rels/workbook.xml.rels")
    if wb is None:
        return []
    rel_map: dict[str, str] = {}
    if rels is not None:
        for rel in rels:
            rid = rel.get("Id")
            target = rel.get("Target", "")
            if rid:
                target = target[1:] if target.startswith("/") else "xl/" + target.lstrip("./")
                rel_map[rid] = target.replace("xl/xl/", "xl/")
    out: list[tuple[str, str]] = []
    sheets = wb.find(S + "sheets")
    for sheet in (sheets if sheets is not None else []):
        name = sheet.get("name", "Sheet")
        rid = sheet.get(R + "id", "")
        state = sheet.get("state", "visible")
        target = rel_map.get(rid)
        if target and target in zf.namelist():
            out.append((f"{name}{' [hidden]' if state != 'visible' else ''}", target))
    return out


def _col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - 64)
    return max(idx - 1, 0)


def _fmt_number(raw: str) -> str:
    try:
        value = float(raw)
    except ValueError:
        return raw
    return str(int(value)) if value.is_integer() and abs(value) < 1e15 else f"{value:g}"


def _read_sheet(
    zf: zipfile.ZipFile, target: str, shared: list[str], date_ids: set[int], max_rows: int
) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    truncated = False
    with zf.open(target) as fh:
        for _event, el in ET.iterparse(fh, events=("end",)):
            if el.tag != S + "row":
                continue
            row: list[str] = []
            for c in el.findall(S + "c"):
                idx = _col_index(c.get("r", "")) if c.get("r") else len(row)
                while len(row) < idx:
                    row.append("")
                ctype = c.get("t", "n")
                if ctype == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(S + "t"))
                else:
                    v = c.find(S + "v")
                    value = v.text or "" if v is not None else ""
                    if ctype == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                    elif ctype == "b":
                        value = "TRUE" if value == "1" else "FALSE"
                    elif ctype in ("n", "") and value:
                        try:
                            style = int(c.get("s", "-1"))
                        except ValueError:
                            style = -1
                        if style in date_ids:
                            try:
                                serial = float(value)
                                stamp = _EXCEL_EPOCH + _dt.timedelta(days=serial)
                                value = stamp.strftime(
                                    "%Y-%m-%d" if serial == int(serial) else "%Y-%m-%d %H:%M"
                                )
                            except (ValueError, OverflowError):
                                value = _fmt_number(value)
                        else:
                            value = _fmt_number(value)
                row.append((value or "").replace("\n", " ").strip())
            el.clear()
            while row and not row[-1]:
                row.pop()
            rows.append(row)
            if len(rows) >= max_rows:
                truncated = True
                break
    while rows and not any(rows[-1]):
        rows.pop()
    return rows, truncated


@register([".xlsx", ".xlsm", ".xltx", ".xltm"], "Excel 2007+ workbook (OOXML)")
def parse_xlsx(path: Path, max_rows: int = 5000) -> ParsedDoc:
    redirected = _redirect_if_legacy(path)
    if redirected is not None:
        return redirected
    chunks: list[str] = []
    warnings: list[str] = []
    with _open(path) as zf:
        meta = _core_props(zf)
        warnings += _damage_warnings(zf)
        shared = _shared_strings(zf)
        date_ids = _date_style_ids(zf)
        targets = _sheet_targets(zf)
        for name, target in targets:
            rows, truncated = _read_sheet(zf, target, shared, date_ids, max_rows)
            if truncated:
                warnings.append(f"sheet '{name}' truncated at {max_rows} rows")
            chunks.append(f"## Sheet: {name}\n")
            if not rows:
                chunks.append("*(empty)*")
                continue
            chunks.append(markdown_table(rows))
        meta["sheets"] = len(targets)
        if any(n.startswith("xl/") and n.endswith(".bin") for n in zf.namelist()):
            warnings.append("workbook contains VBA macros (not extracted)")
    return ParsedDoc(str(path), "excel-ooxml", clean_text("\n\n".join(chunks)), meta, warnings)


# --------------------------------------------------------------------------
# Visio (modern)
# --------------------------------------------------------------------------


@register([".vsdx", ".vsdm", ".vssx", ".vstx"], "Visio 2013+ drawing (OOXML)")
def parse_vsdx(path: Path) -> ParsedDoc:
    redirected = _redirect_if_legacy(path)
    if redirected is not None:
        return redirected
    chunks: list[str] = []
    with _open(path) as zf:
        meta = _core_props(zf)
        warnings = _damage_warnings(zf)
        salvage = _raw_text_sweep(zf, ("visio/",)) if warnings else []
        page_names: dict[str, str] = {}
        pages_xml = _xml(zf, "visio/pages/pages.xml")
        rels = _xml(zf, "visio/pages/_rels/pages.xml.rels")
        rel_map = {r.get("Id"): r.get("Target", "") for r in rels} if rels is not None else {}
        if pages_xml is not None:
            for page in pages_xml:
                rel = page.find(V + "Rel")
                rid = rel.get(R + "id") if rel is not None else None
                if rid and rid in rel_map:
                    page_names[Path(rel_map[rid]).name] = page.get("Name", "Page")

        page_files = sorted(
            (n for n in zf.namelist() if re.fullmatch(r"visio/pages/page\d+\.xml", n)),
            key=_slide_number,
        )
        for name in page_files:
            root = _xml(zf, name)
            if root is None:
                continue
            label = page_names.get(Path(name).name, Path(name).stem)
            shapes: list[str] = []
            id_to_text: dict[str, str] = {}
            id_to_name: dict[str, str] = {}
            for shape in root.iter(V + "Shape"):
                text_node = shape.find(V + "Text")
                text = re.sub(r"\s+", " ", "".join(text_node.itertext()).strip()) if text_node is not None else ""
                shape_id = shape.get("ID", "")
                if not shape_id:
                    continue
                id_to_name[shape_id] = shape.get("NameU") or shape.get("Name") or ""
                if text:
                    shapes.append(text)
                    id_to_text[shape_id] = text

            # A Visio connector is itself a shape; its Connect rows bind its
            # Begin/End endpoints to the shapes it joins.
            ends: dict[str, dict[str, str]] = {}
            for conn in root.iter(V + "Connect"):
                connector = conn.get("FromSheet", "")
                cell = conn.get("FromCell", "")
                target = conn.get("ToSheet", "")
                if not connector or not target:
                    continue
                if cell.startswith("Begin"):
                    ends.setdefault(connector, {})["from"] = target
                elif cell.startswith("End"):
                    ends.setdefault(connector, {})["to"] = target
            connects: list[str] = []
            for connector, pair in ends.items():
                src = id_to_text.get(pair.get("from", "")) or id_to_name.get(pair.get("from", ""), "")
                dst = id_to_text.get(pair.get("to", "")) or id_to_name.get(pair.get("to", ""), "")
                if not src or not dst:
                    continue
                edge_label = id_to_text.get(connector, "")
                arrow = f"{src} -> {dst}"
                connects.append(f"{arrow}  ({edge_label})" if edge_label else arrow)

            block = [f"## Page: {label}", "", "### Shapes", ""]
            block += [f"- {t}" for t in shapes] or ["*(no text shapes)*"]
            if connects:
                block += ["", "### Connections", ""] + [f"- {c}" for c in dict.fromkeys(connects)]
            chunks.append("\n".join(block))
        meta["pages"] = len(page_files)
    text = clean_text("\n\n".join(chunks))
    if not text and salvage:
        text = "## Recovered shape text (unordered)\n\n" + "\n".join(f"- {t}" for t in salvage)
    return ParsedDoc(str(path), "visio-ooxml", text, meta,
                     warnings, "partial" if warnings else "full")


@register([".thmx"], "Office theme package")
def parse_thmx(path: Path) -> ParsedDoc:
    with _open(path) as zf:
        names = zf.namelist()
    return ParsedDoc(
        str(path), "office-theme", "\n".join(names),
        {"parts": len(names)},
        ["theme package: only the part list is meaningful"],
        fidelity="metadata-only",
    )
