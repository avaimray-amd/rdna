---
description: 'Use when searching, reading, quoting or citing anything in the RDNA hardware documentation tree (Desktop/RDNA/Docs) — .docx specs, .xlsx tables, .pptx decks, .vsdx block diagrams, .pdf, OneNote, EMF/WMF diagrams, or the plain-text RDNA4 training transcripts. Explains why grep fails on that tree and how to use the docparse toolkit instead.'
---

# Reading the RDNA documentation

The sibling `../Docs` folder is a read-only snapshot of Perforce hardware specifications.
Roughly 60% of the files are binary containers — Word, Excel, PowerPoint, Visio,
PDF, OneNote, Outlook messages, Windows metafiles. **Text search tools cannot see
inside them**, so an empty grep result across `Docs/` proves nothing.

Use the toolkit in the Agent workspace folder. `C:\RDNA` examples refer to the
chosen checkout root on this device, not the original desktop path.

The exception is `Docs\RDNA4_Transcripts\` — plain text, grep it directly.

## Preferred: MCP tools

If the `rdna-docparse` MCP server is connected, use its tools:

| Tool | Use it for |
|---|---|
| `docs_search` | Regex search inside binary documents. Start here. |
| `docs_outline` | Heading/slide/sheet structure of one document — cheap triage. |
| `docs_extract` | Full text of one document as markdown; page with `offset`. |
| `docs_info` | Metadata, extraction fidelity and warnings, without the text. |
| `docs_list_types` | What file types live under a folder and which are parseable. |

## Fallback: command line

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -m docparse search "..\Docs\gfx12" -p "<regex>" -i --jobs 8
& $py -m docparse extract "<file>" --max-chars 40000
& $py -m docparse info    "<file>"
```

Run from the Agent folder (for example `C:\RDNA\Agent`). Quote every path — they contain
backslashes and spaces.

## The RDNA4 training transcripts

`Docs\RDNA4_Transcripts\Session_01.txt` … `Session_10.txt` are UTF-8 text — use
ordinary text search on them, not `docparse`. They transcribe the ten-part "trip
down the RDNA 4 graphics pipeline" training series: 1 pipeline overview + command
processor, 2 GPU state/GRBM + geometry engine, 3 SIMD/wavefronts + SPI, 4 work
group processor, 5-6 memory system, 7 primitive assembler, 8 scan converter,
9 pixel pipe (DB/CB, OREO), 10 ray tracing + work graphs.

Each file opens with a `SUMMARY` block (overview, `Topics covered:`, `Key terms:`)
fenced by `===` rules; read those first to pick a session. The body is alternating
`timestamp` / spoken-text lines.

They are speech-to-text, so acronyms and numbers are often mangled. Use them for
orientation and to find the right block; a `gfx12`/`gfx13` spec always outranks a
transcript for exact values, and a transcript should be cited as such.

## Rules

1. **Search before concluding.** Never say "this is not documented" without a
   `docs_search` over the relevant subtree.
2. **Check `fidelity` before quoting.** `full` is quotable. `partial` is real text
   but flattened or incomplete. `heuristic` (OneNote, binary `.vsd`, unreadable
   `.xls`) is a keyword index only — never quote it as a specification. Say which
   one you relied on.
3. **Report warnings.** If the parse returned warnings (scanned PDF pages, damaged
   container, encrypted file, EMF with no text records), tell the user rather than
   filling the gap with plausible-sounding hardware knowledge.
4. **Cite the path and the heading**, not just the filename.
5. **Never write to `Docs/`.** It is a Perforce tree and the toolkit is read-only
   by design. Extract to a scratch folder if you need copies. The sole exception
   is `Docs\RDNA4_Transcripts\`, which is agent-generated and not in Perforce.
6. **No OCR.** For images and image-only PDF pages there is no text. Point the
   user at the file path instead of speculating about its contents.

## Enabling the tooling

The migrated workspace includes Agent as a root. Its `.vscode/mcp.json` uses
`${workspaceFolder:Agent}` to resolve the interpreter and server paths.
Start `rdna-docparse` through VS Code's MCP server controls when needed.
Run `Agent\setup.ps1` once to create the virtual environment.
