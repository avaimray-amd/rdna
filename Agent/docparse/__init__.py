"""Read-only document parsing toolkit for the RDNA docs tree.

Typical use::

    from docparse import parse
    doc = parse(r"C:\\Users\\shiny\\Desktop\\RDNA\\Docs\\gfx12\\...\\spec.docx")
    print(doc.text)

Or from the command line::

    python -m docparse extract <file>
    python -m docparse search <root> --pattern "wave64"
"""

from .core import (
    ParsedDoc,
    ParseError,
    get_parser,
    parse,
    supported_extensions,
)
from . import parsers as _parsers  # noqa: F401  (registers every parser)

__all__ = [
    "ParsedDoc",
    "ParseError",
    "parse",
    "get_parser",
    "supported_extensions",
]

__version__ = "1.0.0"
