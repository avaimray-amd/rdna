"""Vector and raster graphics: .svg .emf .wmf .drawio .png .jpg .gif .webp .bmp .eps .ttf

Diagrams are where a lot of hardware documentation actually lives, so the EMF/WMF
parsers pull out the embedded text-out records (box labels, signal names).
"""

from __future__ import annotations

import base64
import re
import struct
import urllib.parse
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

from ..core import ParsedDoc, ParseError, clean_text, read_bytes, register

SVG_NS = "{http://www.w3.org/2000/svg}"


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------


@register([".svg"], "Scalable Vector Graphics")
def parse_svg(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    if data[:2] == b"\x1f\x8b":
        data = __import__("gzip").decompress(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError(f"invalid SVG XML: {exc}") from exc

    meta = {k: v for k, v in root.attrib.items() if k in ("width", "height", "viewBox")}
    lines: list[str] = []
    for node in root.iter():
        local = node.tag.split("}")[-1]
        if local in ("title", "desc"):
            if node.text and node.text.strip():
                lines.append(f"[{local}] {node.text.strip()}")
        elif local == "text":
            text = " ".join(t.strip() for t in node.itertext() if t.strip())
            if text:
                lines.append(text)
    meta["text_elements"] = len(lines)
    return ParsedDoc(str(path), "svg", clean_text("\n".join(lines)), meta)


# --------------------------------------------------------------------------
# draw.io / diagrams.net
# --------------------------------------------------------------------------


def _inflate_drawio(payload: str) -> str | None:
    try:
        raw = base64.b64decode(payload)
    except Exception:  # noqa: BLE001
        return None
    for wbits in (-15, 15, 47):
        try:
            return urllib.parse.unquote(zlib.decompress(raw, wbits).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


@register([".drawio", ".dio"], "draw.io / diagrams.net diagram")
def parse_drawio(path: Path) -> ParsedDoc:
    text = read_bytes(path).decode("utf-8", "replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError(f"invalid draw.io XML: {exc}") from exc

    diagrams: list[tuple[str, ET.Element]] = []
    if root.tag == "mxfile":
        for diagram in root.findall("diagram"):
            name = diagram.get("name", "Page")
            model = diagram.find("mxGraphModel")
            if model is not None:
                diagrams.append((name, model))
                continue
            payload = (diagram.text or "").strip()
            decoded = _inflate_drawio(payload) if payload else None
            if decoded:
                try:
                    diagrams.append((name, ET.fromstring(decoded)))
                except ET.ParseError:
                    continue
    elif root.tag == "mxGraphModel":
        diagrams.append((path.stem, root))

    chunks: list[str] = []
    for name, model in diagrams:
        labels: dict[str, str] = {}
        edges: list[tuple[str, str, str]] = []
        for cell in model.iter("mxCell"):
            cell_id = cell.get("id", "")
            value = _strip_html(cell.get("value", ""))
            if cell.get("edge") == "1":
                edges.append((value, cell.get("source", ""), cell.get("target", "")))
            elif value:
                labels[cell_id] = value
        for obj in model.iter("object"):
            value = _strip_html(obj.get("label", ""))
            if value:
                labels[obj.get("id", "")] = value
        block = [f"## Diagram: {name}", "", "### Nodes", ""]
        block += [f"- {v}" for v in labels.values()] or ["*(none)*"]
        if edges:
            block += ["", "### Edges", ""]
            for label, src, dst in edges:
                arrow = f"{labels.get(src, '?')} -> {labels.get(dst, '?')}"
                block.append(f"- {arrow}" + (f"  ({label})" if label else ""))
        chunks.append("\n".join(block))

    return ParsedDoc(str(path), "drawio", clean_text("\n\n".join(chunks)),
                     {"diagrams": len(diagrams)})


# --------------------------------------------------------------------------
# EMF / WMF
# --------------------------------------------------------------------------

EMR_HEADER = 1
EMR_EXTTEXTOUTA = 83
EMR_EXTTEXTOUTW = 84
EMR_SMALLTEXTOUT = 108


def _emf_text(data: bytes) -> tuple[list[str], dict[str, object]]:
    meta: dict[str, object] = {}
    labels: list[str] = []
    pos = 0
    size = len(data)
    while pos + 8 <= size:
        rec_type, rec_size = struct.unpack_from("<II", data, pos)
        if rec_size < 8 or pos + rec_size > size:
            break
        if rec_type == EMR_HEADER and pos == 0:
            try:
                bounds = struct.unpack_from("<4i", data, 8)
                frame = struct.unpack_from("<4i", data, 24)
                n_records, = struct.unpack_from("<I", data, 52)
                n_desc, off_desc = struct.unpack_from("<II", data, 60)
                meta["bounds_px"] = f"{bounds[2] - bounds[0]}x{bounds[3] - bounds[1]}"
                meta["frame_0.01mm"] = f"{frame[2] - frame[0]}x{frame[3] - frame[1]}"
                meta["records"] = n_records
                if n_desc and off_desc + n_desc * 2 <= size:
                    desc = data[off_desc:off_desc + n_desc * 2].decode("utf-16-le", "replace")
                    meta["description"] = desc.replace("\x00", " ").strip()
            except struct.error:
                pass
        elif rec_type in (EMR_EXTTEXTOUTA, EMR_EXTTEXTOUTW):
            try:
                n_chars, off_string = struct.unpack_from("<II", data, pos + 44)
            except struct.error:
                pos += rec_size
                continue
            start = pos + off_string
            if n_chars and 0 < n_chars < 1 << 16 and start < size:
                if rec_type == EMR_EXTTEXTOUTW:
                    raw = data[start:start + n_chars * 2].decode("utf-16-le", "replace")
                else:
                    raw = data[start:start + n_chars].decode("cp1252", "replace")
                text = raw.replace("\x00", "").strip()
                if text:
                    labels.append(text)
        elif rec_type == EMR_SMALLTEXTOUT:
            try:
                c_chars, fu_options = struct.unpack_from("<II", data, pos + 16)
                offset = pos + 36 + (0 if fu_options & 0x0100 else 16)
                if c_chars and offset < size:
                    raw = data[offset:offset + c_chars * (1 if fu_options & 0x0200 else 2)]
                    text = raw.decode("cp1252" if fu_options & 0x0200 else "utf-16-le", "replace")
                    text = text.replace("\x00", "").strip()
                    if text:
                        labels.append(text)
            except struct.error:
                pass
        pos += rec_size
    return labels, meta


@register([".emf", ".emz"], "Enhanced Metafile vector drawing")
def parse_emf(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    if data[:2] == b"\x1f\x8b":
        data = __import__("gzip").decompress(data)
    if len(data) < 88 or struct.unpack_from("<I", data, 40)[0] != 0x464D4520:
        raise ParseError("missing ' EMF' signature")
    labels, meta = _emf_text(data)
    meta["text_records"] = len(labels)
    return ParsedDoc(
        str(path), "emf", clean_text("\n".join(labels)), meta,
        ["EMF is a drawing command stream: only text labels are recovered, not shapes, "
         "arrows or layout. Reading order follows the record order, not the visual layout."],
        fidelity="partial",
    )


META_TEXTOUT = 0x0521
META_EXTTEXTOUT = 0x0A32


@register([".wmf", ".wmz"], "Windows Metafile vector drawing")
def parse_wmf(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    if data[:2] == b"\x1f\x8b":
        data = __import__("gzip").decompress(data)
    meta: dict[str, object] = {}
    pos = 0
    if data[:4] == b"\xd7\xcd\xc6\x9a":
        left, top, right, bottom = struct.unpack_from("<4h", data, 6)
        inch, = struct.unpack_from("<H", data, 14)
        meta["bounds"] = f"{right - left}x{bottom - top} units, {inch}/inch"
        pos = 22
    if len(data) < pos + 18:
        raise ParseError("file too small to be a WMF")
    pos += 18

    labels: list[str] = []
    size = len(data)
    while pos + 6 <= size:
        rec_words, func = struct.unpack_from("<IH", data, pos)
        rec_bytes = rec_words * 2
        if rec_bytes < 6 or pos + rec_bytes > size:
            break
        params = pos + 6
        try:
            if func == META_EXTTEXTOUT:
                cch, opts = struct.unpack_from("<HH", data, params + 4)
                offset = params + 8 + (8 if opts & 0x0006 else 0)
                if 0 < cch < 1 << 15 and offset + cch <= size:
                    text = data[offset:offset + cch].decode("cp1252", "replace").strip("\x00 ")
                    if text.strip():
                        labels.append(text.strip())
            elif func == META_TEXTOUT:
                cch, = struct.unpack_from("<H", data, params)
                offset = params + 2
                if 0 < cch < 1 << 15 and offset + cch <= size:
                    text = data[offset:offset + cch].decode("cp1252", "replace").strip("\x00 ")
                    if text.strip():
                        labels.append(text.strip())
        except struct.error:
            pass
        pos += rec_bytes
    meta["text_records"] = len(labels)
    return ParsedDoc(
        str(path), "wmf", clean_text("\n".join(labels)), meta,
        ["WMF is a drawing command stream: only text labels are recovered."],
        fidelity="partial",
    )


# --------------------------------------------------------------------------
# Raster images (dimensions only)
# --------------------------------------------------------------------------


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack_from(">H", data, pos + 2)[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack_from(">HH", data, pos + 5)
            return width, height
        pos += 2 + length
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if data[:3] != b"GIF":
        return None
    return struct.unpack_from("<HH", data, 6)


def _bmp_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"BM":
        return None
    width, height = struct.unpack_from("<ii", data, 18)
    return width, abs(height)


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8 ":
        w, h = struct.unpack_from("<HH", data, 26)
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


@register([".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico"],
          "Raster image (dimensions only, no OCR)")
def parse_image(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    size = None
    for probe in (_png_size, _jpeg_size, _gif_size, _bmp_size, _webp_size):
        size = probe(data)
        if size:
            break
    meta: dict[str, object] = {"format": path.suffix.lstrip(".").lower()}
    if size:
        meta["dimensions"] = f"{size[0]}x{size[1]}"
    text_chunks: list[str] = []
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        for match in re.finditer(rb"(tEXt|iTXt)(.{0,200}?)\x00", data[8:], re.S):
            text_chunks.append(match.group(2).decode("latin-1", "replace"))
    return ParsedDoc(
        str(path), "image", clean_text("\n".join(text_chunks)), meta,
        ["raster image: no text extraction (OCR is not installed). "
         "Use the file path with a vision-capable model if the contents matter."],
        fidelity="metadata-only",
    )


@register([".eps", ".ps"], "PostScript / Encapsulated PostScript")
def parse_eps(path: Path) -> ParsedDoc:
    data = read_bytes(path)
    if data[:4] == b"\xc5\xd0\xd3\xc6":  # DOS EPS binary wrapper
        offset, length = struct.unpack_from("<II", data, 4)
        data = data[offset:offset + length]
    text = data.decode("latin-1", "replace")
    comments = [line for line in text.split("\n") if line.startswith("%%")][:80]
    shown = re.findall(r"\((?:[^()\\]|\\.){2,}\)\s*(?:show|Tj)", text)
    labels = [s.rsplit(")", 1)[0].lstrip("(") for s in shown]
    body = "\n".join(comments)
    if labels:
        body += "\n\n## Text shown\n\n" + "\n".join(dict.fromkeys(labels))
    return ParsedDoc(str(path), "postscript", clean_text(body),
                     {"bytes_text": len(text)}, fidelity="partial")


@register([".ttf", ".otf", ".woff", ".woff2", ".eot"], "Font file (metadata only)")
def parse_font(path: Path) -> ParsedDoc:
    return ParsedDoc(
        str(path), "font", "", {"format": path.suffix.lstrip(".")},
        ["font file: no document content"], fidelity="metadata-only",
    )
