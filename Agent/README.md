# docparse — read-only text extraction for the RDNA docs tree

The `Docs` folder is mostly binary: Word, Excel, PowerPoint, Visio, PDF, OneNote,
Outlook messages and Windows metafiles. Ordinary search tools cannot look inside
any of them. `docparse` turns every one of those into markdown/plain text so that
you (and coding agents) can search and read them.

Nothing here ever opens a source document for writing.

## Setup

Run from the Agent folder in the migrated checkout. The hardware Docs snapshot
is the sibling `../Docs` directory; restore it using the workspace migration
guide first. Generated caches and Python environments are not part of Git.

```powershell
cd C:\RDNA\Agent
.\setup.ps1
```

That creates `.venv` and installs three dependencies (`olefile`, `pypdf`, `xlrd`).
Everything else uses the Python standard library.

## Command line

```powershell
$py = ".\.venv\Scripts\python.exe"

& $py -m docparse parsers                     # every extension understood
& $py -m docparse types  <folder>             # inventory a tree, flag gaps
& $py -m docparse info   <file>...            # metadata + fidelity + warnings
& $py -m docparse extract <file>... [--max-chars N] [--json] [--out DIR]
& $py -m docparse search <folder> -p <regex> [-i] [-l] [--ext .docx,.pdf] [--jobs 8]
& $py -m docparse dump   <folder> --out <dir> # mirror the tree as .md files
& $py -m docparse cache  [--clear]
```

`search` is the workhorse: it parses each document and greps the extracted text.
Results are cached on disk (keyed by path + size + mtime), so the second search
over the same tree is roughly ten times faster.

## Python API

```python
from docparse import parse

doc = parse(r"..\Docs\gfx12\block\sx\GFX12_SX_MAS.docx")
print(doc.kind, doc.fidelity)   # 'word-ooxml' 'full'
print(doc.meta["author"], doc.meta["pages"])
print(doc.text[:2000])          # markdown: headings, tables, lists
for warning in doc.warnings:
    print("!", warning)
```

`docparse.cache.parse_cached(path)` is the same thing with the on-disk cache.

## Formats

| Group | Extensions | Fidelity |
|---|---|---|
| Word | `.docx .docm .dotx .dotm` | full — headings, tables, lists, headers/footers, footnotes, comments |
| Word (legacy) | `.doc .dot .wbk` | partial — piece-table text extraction, structure flattened |
| Excel | `.xlsx .xlsm .xltx .xltm` | full — every sheet as a markdown table, dates decoded |
| Excel (legacy) | `.xls .xlt` | full via `xlrd`; heuristic string recovery if that fails |
| PowerPoint | `.pptx .pptm .potx` | full — per slide, incl. tables and speaker notes |
| PowerPoint (legacy) | `.ppt .pot .pps` | partial — record-order text recovery |
| Visio | `.vsdx .vsdm .vssx .vstx` | full — page names, shape text, and connector edges |
| Visio (legacy) | `.vsd .vss .vst` | heuristic — strings only, no structure |
| PDF | `.pdf` | full — outline plus per-page text; no OCR for scanned pages |
| Diagrams | `.drawio .svg .emf .wmf .eps` | drawio/svg full; metafiles partial (text labels only) |
| OneNote | `.one .onetoc2 .onepkg` | heuristic — keyword index only |
| Outlook | `.msg .oft` | full — headers plus body |
| MS Project | `.mpp .mpt` | metadata only (undocumented format) |
| Images | `.png .jpg .gif .bmp .webp .tif .ico` | metadata only — dimensions, PNG text chunks |
| Text & code | ~120 extensions incl. `.adoc .rst .md .csv .xml .json .html .rtf .ipynb .sv .cpp` | full |
| Archives | `.gz .tgz` | inner file parsed; tarballs listed with text members inlined |

`docparse parsers` prints the authoritative list.

Extras worth knowing about:

- **Damaged ZIP recovery.** Corrupt `.docx`/`.vsdx`/`.xlsx` files are salvaged by
  scanning local file headers and by repairing truncated XML parts. The result is
  marked `partial` and carries a warning.
- **Extension/format mismatches** are resolved by content sniffing: a `.doc` that
  is really OOXML or RTF, a `.xlsx` that is really a legacy `.xls`, a `.dot` that
  is really Graphviz — all get routed to the right parser.
- **Encrypted / IRM-protected** Office files are detected and reported with an
  actionable message instead of a stack trace.

## MCP server

`mcp_server.py` exposes the same engine as MCP tools (`docs_search`,
`docs_extract`, `docs_info`, `docs_outline`, `docs_list_types`) over stdio, with
no third-party MCP library. Configuration lives in `.vscode/mcp.json`; see
`.github/instructions/rdna-docs.instructions.md` for how to enable it.

## Tests

```powershell
& $py tests\smoke_corpus.py "..\Docs" --per-ext 5
& $py tests\smoke_mcp.py
& $py tests\smoke_mcp.py --docs-root "..\Docs"
```

`smoke_corpus.py` samples real files of every extension present in the tree and
reports per-extension success, kind, fidelity and average extracted size. It is
the fastest way to see whether a change broke anything.

The default MCP test uses this README and does not require hardware documents.
The optional `--docs-root` test checks Word and Visio documents from the SX block.
