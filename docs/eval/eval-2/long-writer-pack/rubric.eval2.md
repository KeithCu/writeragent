# rubric.eval2 — soft long Writer pack oracle (v1)

WriterAgent-native. There is no gold [`rubric_pretty.txt`](rubric_pretty.txt).
Derived from the **writer prompt + the two research fixtures**, not from
a GDPval row. This variant scores the **saved open Writer pack**
(`.odt` / `.docx`). Do **not** fail on Word vs ODT.

## What is scored

The harness opens the saved trial artifact (`final_pack.odt`, or
`.docx` if that is what was saved). **Chat Ready / STREAM_DONE is
ignored.** An empty brief that ended Ready still fails. Extract ODT
`text:h` and `text:p` (document order), plus DOCX paragraphs. Detect
TOC fields / equivalent Contents index, named heading styles, and
review comments (`office:annotation` / `word/comments.xml`).

| # | Check | Source | Fail closed |
|---|--------|--------|-------------|
| 1 | Writer doc present; body non-empty; word band **300–4000** | Multi-section capital brief | Missing / unreadable / no text; `< 300` or `> 4000` |
| 2 | Identity: **Northhaven**, **Civic Library**, **CL-2026-ANNEX** | Prompt + program facts | Any identity missing |
| 3 | Base renovation **$4.2 million**; at least one annex cost **$1.8M** or **$2.4M** | Program facts | Missing base cost or both annex costs |
| 4 | Construction window **April 2027** and **October 2028** | Program facts | Either date missing |
| 5 | Section themes: purpose/scope; budget/schedule; public-access/hours; open decision (annex / funding / weekend staff) | Writer prompt | Any theme missing |
| 6 | TOC field **or** a Contents index at the start that names later headings | Writer prompt | Headings only; no TOC / Contents index |
| 7 | At least **3** named heading styles (`text:h` / Heading 1–n / Title). Contents-line styles do not count | Writer prompt | Entire body Default / unstyled |
| 8 | At least **one** review comment with ≥8 non-husk characters | Writer prompt; Decision Log | No comments; empty / `Error:` comment |
| 9 | Ban empty / Ready-only / `Error:` husks | Observed residue | Body matches `Error:` / `#DIV/0!` husks or is blank |

Wrong fixture facts fail even if the pack is well formatted. A typed
Contents list that repeats later heading titles counts as an equivalent
index. Bold Default paragraphs are not named styles.

## Soft / later (not v1)

Exact heading wording, comment author names, three comments vs one,
+5 style, and Word vs ODT. Do not fail on `.docx` vs `.odt`.

## Non-goals

- A second change-control form or a slide deck.
- A GDPval gold-rubric grader or a product Ready predicate.
- Unparking slot 7 (headed letterhead template).
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **50** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_long_writer_oracle.py` or
`scripts/eval_2_headed.py --task long-writer-pack --score`.

## Open

- `[eng S]` Extend the long Writer pack oracle with the deferred checks: exact heading wording, three comments instead of one, and style extras. (§ Soft / later (not v1))
