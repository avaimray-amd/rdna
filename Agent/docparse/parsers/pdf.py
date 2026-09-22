"""PDF text extraction (pypdf)."""

from __future__ import annotations

from pathlib import Path

from ..core import ParsedDoc, ParseError, clean_text, register, require


@register([".pdf"], "Portable Document Format")
def parse_pdf(path: Path, max_pages: int = 2000) -> ParsedDoc:
    pypdf = require("pypdf")
    warnings: list[str] = []
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"pypdf could not open the file: {exc}") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise ParseError("PDF is encrypted and needs a password") from None
        warnings.append("PDF was encrypted with an empty owner password")

    meta: dict[str, object] = {"pages": len(reader.pages)}
    try:
        info = reader.metadata or {}
        for key in ("/Title", "/Author", "/Subject", "/Creator", "/Producer", "/CreationDate"):
            value = info.get(key)
            if value:
                meta[key.lstrip("/").lower()] = str(value)
    except Exception:  # noqa: BLE001
        pass

    chunks: list[str] = []
    try:
        outline = _outline_lines(reader)
        if outline:
            chunks.append("## Outline\n\n" + "\n".join(outline))
    except Exception:  # noqa: BLE001
        pass

    empty_pages = 0
    for index, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            text = ""
            warnings.append(f"page {index}: {type(exc).__name__}")
        text = text.strip()
        if not text:
            empty_pages += 1
            continue
        chunks.append(f"\n<!-- page {index} -->\n\n{text}")

    if len(reader.pages) > max_pages:
        warnings.append(f"stopped after {max_pages} pages")
    if empty_pages:
        warnings.append(
            f"{empty_pages} page(s) yielded no text (scanned images or vector-only figures; "
            "OCR would be required)"
        )
    fidelity = "partial" if empty_pages else "full"
    return ParsedDoc(str(path), "pdf", clean_text("\n".join(chunks)), meta, warnings, fidelity)


def _outline_lines(reader) -> list[str]:
    lines: list[str] = []

    def walk(items, depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                title = getattr(item, "title", None)
                if title:
                    lines.append("  " * depth + f"- {title}")

    walk(reader.outline, 0)
    return lines[:500]
