# AGENTS.md — reading the RDNA documentation tree

The sibling `../Docs` folder is a restored hardware-document snapshot. Paths
under `C:\RDNA` below are examples; use the actual migrated checkout root.
Most of the real content is
locked inside binary containers: 2,300+ `.docx`, 1,300+ `.xlsx`, 800+ `.pptx`,
190 Visio drawings, 200 PDFs, 60 OneNote sections and ~100 EMF/WMF diagrams.

**`grep` / text search cannot see inside any of those.** A "no results" answer from
a plain text search across `Docs/` is meaningless. Use the `docparse` toolkit in
this folder instead.

The one exception is `Docs\RDNA4_Transcripts\` — see below.

## The one rule

> Before concluding that something is not documented, search it with `docparse`,
> not with grep.

## Quick start

```powershell
cd C:\RDNA\Agent
$py = ".\.venv\Scripts\python.exe"

# What is in a folder, and can we parse it?
& $py -m docparse types "..\Docs\gfx12\block\sq"

# Search inside binary documents (regex, case-insensitive)
& $py -m docparse search "..\Docs\gfx12" -p "wave64" -i --jobs 8

# Read one document as markdown
& $py -m docparse extract "...\GFX12_SX_MAS.docx" --max-chars 40000

# Metadata + what could NOT be recovered, without dumping the text
& $py -m docparse info "...\some_diagram.vsdx"
```

If the `rdna-docparse` MCP server is connected, prefer its tools
(`docs_search`, `docs_outline`, `docs_extract`, `docs_info`, `docs_list_types`)
over shelling out — same engine, structured results, no quoting problems.

## Recommended workflow

1. **Locate** — `docs_search` / `docparse search` with a regex over the whole
   subtree. Start broad, use `--ext` only once you know which format holds the
   answer.
2. **Triage** — `docs_outline` on the hits to see the section structure, and
   `docs_info` to check `fidelity` before trusting the content.
3. **Read** — `docs_extract` the relevant document, paging with `offset` if it is
   long. Cite the source path and the heading you took the statement from.

The first search over a folder is slow (documents are decompressed and parsed);
results are cached on disk keyed by path+size+mtime, so repeat searches are ~10x
faster. `docparse cache --clear` if you ever suspect a stale entry.

## The RDNA4 training transcripts — plain text, grep away

`Docs\RDNA4_Transcripts\Session_01.txt` … `Session_10.txt` are UTF-8 text, so
ordinary text search works on them and is the fastest way in. They are the
ten-part "trip down the RDNA 4 graphics pipeline" training series, extracted from
the source `.docx` with `docparse` (fidelity `full`).

Each file starts with a `SUMMARY` block — a one-line overview, a `Topics covered:`
bullet list and a `Key terms:` line, fenced by `===` rules — followed by the
transcript as alternating `timestamp` / spoken-text lines. Read the summary blocks
first to pick a session, then search or read inside it.

| File | Covers |
|---|---|
| `Session_01` | Pipeline overview; command processor (CPF/CPG/CPC/MES), PM4, driver stack |
| `Session_02` | GPU state and the GRBM; geometry engine, index fetch, vertex reuse |
| `Session_03` | SIMD/wavefront fundamentals; SPI wave launch, occupancy limits |
| `Session_04` | Work group processor: sequencer, SP datapath, EXEC mask, `s_waitcnt` |
| `Session_05` | Memory system 1: TA/TD, address swizzle, LDS banking, barriers |
| `Session_06` | Memory system 2: GL0/GL1/GL2/MALL, UTC translation, DCC, data fabric |
| `Session_07` | Primitive assembler (PAF/PAB), clipping, viewport transform, filters |
| `Session_08` | Scan converter: rasterisation, binning, hi-Z, VRS, quad packing |
| `Session_09` | Pixel pipe: interpolation, DB/CB render backends, OREO, blending |
| `Session_10` | Ray tracing (BVH8, OBB, intersection HW) and DX12 work graphs |

These are speech-to-text transcripts, so names, acronyms and the occasional number
are mangled. Treat them as orientation and as a way to find the right block — a
spec under `Docs/gfx12` or `Docs/gfx13` outranks a transcript for exact values.

## Read the fidelity field before you trust the text

Every parse reports one of four levels. This matters more than it sounds — a
`heuristic` result is a keyword index, not a quotable source.

| fidelity | Meaning | Formats |
|---|---|---|
| `full` | Structure preserved: headings, tables, slides, sheets, diagram edges | docx, pptx, xlsx, vsdx, pdf, csv, html, rtf, svg, drawio, text |
| `partial` | Text is real but flattened or incomplete | legacy `.doc`/`.ppt`, EMF/WMF (labels only, no layout), PDFs with image-only pages, damaged/salvaged ZIPs |
| `heuristic` | Strings scraped from a binary blob; order and grouping are unreliable | `.one` (OneNote), `.vsd` (binary Visio), unreadable `.xls` |
| `metadata-only` | No text at all | images, fonts, `.mpp` |

Always surface the `warnings` list to the user when it is non-empty — it tells
them exactly what was lost (e.g. "3 pages yielded no text: scanned images").

## Known limitations, and what to do about them

- **Images and scanned PDF pages** — there is no OCR. If a `.png`/`.jpg` matters,
  hand the file path to a vision-capable model rather than to `docparse`.
- **EMF/WMF diagrams** — only the text-out records are recovered, and in record
  order, not visual order. Many AMD diagrams have their text converted to
  outlines, in which case nothing is recoverable; say so instead of guessing.
- **OneNote `.one`** — heuristic only. Use it to find *which* section mentions a
  term, then ask the user to open that section.
- **`.mpp` (MS Project)** — undocumented format; only stream names are listed.
- **Encrypted / IRM-protected files** — reported clearly; they need to be opened
  in Office and re-saved before anything can read them.
- **Damaged ZIP containers** — `docparse` salvages what it can and marks the
  result `partial`. Do not present salvaged text as authoritative.

## Hard constraints

- The toolkit is **read-only by design**. There is no code path that writes to a
  source document, and none should be added. `Docs/` is a Perforce tree — treat
  it as immutable.
- Never modify anything under `Docs/`. Extracted copies go somewhere else
  (`docparse dump --out <scratch dir>`). `Docs\RDNA4_Transcripts\` is the one
  agent-generated folder in that tree and is not under Perforce control.
- Large files: `.xls` workbooks over ~30 MB fall back to string recovery; a few
  `.vec`/`.tm7` files are multi-megabyte text. Use `--max-bytes` to skip them
  when searching broadly.

## Layout of this folder

```
Agent/
  docparse/            the package (core, cache, cli, parsers/)
    parsers/ooxml.py   docx pptx xlsx vsdx  (+ damaged-ZIP salvage)
    parsers/ole.py     doc xls ppt vsd msg one mpp
    parsers/pdf.py     pdf
    parsers/graphics.py svg drawio emf wmf images eps fonts
    parsers/plain.py   text markup source csv json html rtf ipynb gz/tar
  mcp_server.py        MCP stdio server exposing the same engine
  tests/               smoke_corpus.py (real files), smoke_mcp.py (protocol)
  .cache/              extracted-text cache (safe to delete)
```
