---
mode: agent
description: 'Find and summarise what the RDNA hardware documentation says about a topic, searching inside .docx/.xlsx/.pptx/.vsdx/.pdf/OneNote files that grep cannot read.'
---

# Find in the RDNA docs

Topic to research: `${input:topic:What should I look for? e.g. "LDS bank conflicts on gfx12"}`
Subtree to search: `${input:subtree:C:\RDNA\Docs\gfx12}`

The path above is an example. Resolve the actual Docs workspace folder before searching.

Do this:

1. Turn the topic into **two or three regex patterns**, including likely hardware
   spellings and abbreviations (e.g. `wave ?64`, `super-?tile`, `LDS.{0,12}bank`).
2. Run `docs_search` (or `python -m docparse search <subtree> -p <regex> -i --jobs 8`)
   for each pattern. Do not filter by extension on the first pass — the answer is
   as likely to be in a Visio block diagram or an Excel table as in a Word spec.
3. Group the hits by document. For the most promising 2-4 documents, call
   `docs_outline` to find the relevant section, then `docs_extract` to read it.
4. Check `docs_info` fidelity for anything you intend to quote.

Then report:

- A short answer to the question, in plain language, defining each term before
  using it.
- A **Sources** list: full path, document kind, the heading or slide/sheet/page
  the statement came from, and the fidelity level.
- A **Gaps** list: patterns that matched nothing, files that could not be parsed,
  and anything that was image-only and therefore unreadable.

Do not fill gaps with general GPU knowledge presented as if it came from these
documents. If the docs do not say it, say that they do not say it.
