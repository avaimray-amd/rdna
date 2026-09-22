---
name: rdna-docs
description: 'Use when a question needs evidence from the RDNA hardware documentation tree at Desktop/RDNA/Docs — searching or reading .docx specs, .xlsx tables, .pptx decks, .vsdx block diagrams, .pdf, OneNote sections, EMF/WMF diagrams, or the plain-text RDNA4 architecture training transcripts. Also use when a plain text search over that tree returned nothing, when the user asks "what do the docs say about X", or when a claim about gfx12/gfx13 hardware needs a citation. Covers the docparse toolkit, extraction fidelity levels, and the known unreadable formats.'
---

# Researching the RDNA documentation tree

## Why this skill exists

The Docs workspace folder (a sibling of Agent) holds the restored hardware
documentation snapshot. The majority of the actual
specification content is inside compressed binary containers:

| Count | Type |
|---|---|
| ~2,300 | Word `.docx` / `.doc` |
| ~1,450 | Excel `.xlsx` / `.xlsm` / `.xls` |
| ~825 | PowerPoint `.pptx` / `.ppt` |
| ~190 | Visio `.vsdx` / `.vsd` |
| ~206 | PDF |
| ~62 | OneNote `.one` |
| ~98 | EMF / WMF diagrams |

`grep`, `ripgrep`, and the editor's text search read none of these. **A negative
text-search result over `Docs/` is not evidence of absence.**

The one folder that *is* plain text is `Docs\RDNA4_Transcripts\` — see below.

## The toolkit

The Agent workspace folder contains `docparse`, a read-only extraction
package, and an MCP server that exposes it. Read `Agent/AGENTS.md` for the full
picture; the essentials are below.

### If the MCP server is connected

Use `docs_search` → `docs_outline` → `docs_extract`, with `docs_info` for
fidelity checks and `docs_list_types` to survey a folder.

### Otherwise, the CLI

```powershell
cd C:\RDNA\Agent
$py = ".\.venv\Scripts\python.exe"

& $py -m docparse search "<folder>" -p "<regex>" -i --jobs 8   # find
& $py -m docparse info    "<file>"                              # triage
& $py -m docparse extract "<file>" --max-chars 40000            # read
```

If `.venv` does not exist, run `.\setup.ps1` first.
Replace `C:\RDNA` with the actual checkout root on this device. The Docs
snapshot is restored separately using the workspace migration guide.

## Procedure

1. **Broaden the query first.** Hardware docs are inconsistent: try `wave ?64`,
   `wave-64`, `WAVE64`. Two or three regexes beat one exact string.
2. **Search the whole subtree, no extension filter.** Block diagrams (`.vsdx`)
   and register tables (`.xlsx`) answer as many questions as the Word specs do.
3. **Triage with `info`/`outline` before extracting.** Some specs are 300 KB of
   text; extract the section you need rather than the whole file.
4. **Check fidelity before quoting** (see the table below).
5. **Cite path + heading + fidelity.** Report what you could not read.

## Start here for pipeline-level questions: the RDNA4 transcripts

`Docs\RDNA4_Transcripts\Session_01.txt` … `Session_10.txt` are UTF-8 text, so
`grep_search` works on them directly. They are the ten-part "trip down the RDNA 4
graphics pipeline" training series, extracted from the source `.docx` at fidelity
`full`. For "how does block X fit into the pipeline" or "why does the hardware do
Y", these are usually a faster and more readable entry point than the specs, and
they name the exact blocks to search for next.

Each file begins with a `SUMMARY` block — one-line overview, `Topics covered:`
bullets, `Key terms:` line — fenced by `===` rules, then the transcript as
alternating `timestamp` / spoken-text lines. Read the summary blocks to choose a
session before reading any body text.

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

**Treat them as orientation, not as a specification.** They are speech-to-text, so
speaker names, acronyms and numbers are frequently mangled ("Navi 4" vs "Navi 48",
garbled block names). Any figure taken from a transcript must be confirmed against
`Docs/gfx12` or `Docs/gfx13` before it is quoted as fact, and a citation should
say it came from a transcript.

## Fidelity levels — read this before quoting anything

| Level | What it means | Safe to quote? |
|---|---|---|
| `full` | Structure preserved (headings, tables, slides, sheets, diagram edges) | Yes |
| `partial` | Real text, but flattened or incomplete — legacy `.doc`/`.ppt`, EMF/WMF labels, PDFs with image-only pages, salvaged damaged ZIPs | Yes, with the caveat stated |
| `heuristic` | Strings scraped out of a binary blob; order and grouping unreliable — OneNote, binary `.vsd`, unreadable `.xls` | No. Use only to locate the file, then ask the user to open it |
| `metadata-only` | No text — images, fonts, `.mpp` | No |

## Known dead ends — say so rather than guessing

- **No OCR.** `.png`/`.jpg` and scanned PDF pages contain no retrievable text.
  Give the user the path; a vision-capable model can look at it.
- **EMF/WMF with text converted to outlines** yields zero labels. The parser
  reports `text_records: 0` — that means the diagram is unreadable, not empty.
- **OneNote** is heuristic only; there is no open parser for the format.
- **`.mpp`** (MS Project) is undocumented; only OLE stream names are available.
- **Encrypted / IRM-protected** Office files must be re-saved from Office first.
- **Damaged containers** are salvaged where possible and flagged `partial`; do not
  present salvaged fragments as authoritative.

## Constraints

- `Docs/` is a Perforce tree. **Never write to it.** The toolkit has no write path
  and none should be added. The only agent-generated folder there is
  `Docs\RDNA4_Transcripts\`, which is outside Perforce control.
- The first search over a folder is slow; results are cached by path+size+mtime.
  Use `docparse cache --clear` only if you suspect a stale entry.
- Skip pathological files when searching broadly: `--max-bytes 30000000`.
