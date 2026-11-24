# rubric.eval2 — soft Writer + Draw form oracle (v1)

Derived from the **gold prompt + COA + RMS fixture**, not by
letter-shifting gold [`rubric_pretty.txt`](rubric_pretty.txt). Gold
asks for a Word risk report plus a filled PDF. This variant scores the
**saved open Writer memo** (`.odt` / `.docx`) **and** the **Draw
stand-in** (`.odg`). Do **not** fail on Word vs ODT vs PDF.

Leave [`docs/eval/gdpval/`](../../gdpval/) untouched.

## What is scored

The harness opens the saved trial pair (`final_memo.odt` +
`final_form.odg`, or a trial directory). **Chat Ready / STREAM_DONE is
ignored.** An empty memo or a still-blank form that ended Ready still
fails. Extract ODT `text:h` and `text:p`; Draw `draw:frame` text by
`fld_*` name.

| # | Check | Source | Fail closed |
|---|--------|--------|-------------|
| 1 | Writer doc present; body non-empty; word band **200–2500** | Risk memo + QA email + Teams note | Missing / unreadable / no text; `< 200` or `> 2500` |
| 2 | Draw stand-in **substantially filled**: ≥**5** of 12 named `fld_*` boxes and ≥**80** filled characters | Writer prompt §1; Form-920 Section 1 stand-in | Too few named blanks, or only a few characters |
| 3 | **RMS-3333**, **CompCello**, **QY-GEL / Antifoam** (accept Antiform; hyphen-like chars fold to ASCII `-`) | Prompt + gold rubric; spec typo Antiform | Any identity missing in memo **or** form |
| 4 | Endotoxin mismatch: **Report Result** and **`< 1` EU** (aliases: less than 1 / below 1) | Prompt + COA / RMS | Either anchor missing |
| 5 | Discrepancy / mismatch / non-conforming | Prompt | Theme missing |
| 6 | Quarantine / hold + RMS update / change control | Prompt §1 | Either theme missing |
| 7 | Memo **cites** the filled form (change-control form / Form-920 / Change Control Request / CCR / draft or completed change control) | Writer prompt §4 | No form cite in the memo |
| 8 | QA escalation email section + deviation **or** requalification ask | Writer prompt §2 | Missing section or ask |
| 9 | Internal / Teams / status summary | Writer prompt §3 | Missing note |
| 10 | Vendor notification (two months / report only / vendor memo); departed employee; centralized tracking; SOP | Writer prompt §4 | Any of those four themes missing |
| 11 | Ban empty / Ready-only / `Error:` husks | Observed residue | Body matches `Error:` / `#DIV/0!` husks or is blank |

Identity facts may live in **either** deliverable. Wrong COA/RMS
anchors fail even if the memo is well formatted.

## Soft / later (not v1)

Exact Form-920 layout, gold PDF strings, Change Control IDs, lot
`00004515` (allowed if taken from the COA, not required), expert-gold
**XX-CELL** / **A23-044** / **AF-COMP-333**, email subject-line
microcopy, owner names, and +5 style. Do not fail on `.docx` vs `.odt`
vs `.odg`.

## Non-goals

- Filling the flat gold PDF / AcroForm.
- Spawning a new Draw document (v1 pre-opens the stand-in).
- A full gold-rubric grader or a product Ready predicate.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **150** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_gmp_oracle.py` or
`scripts/eval_2_headed.py --task gmp-change-control --score`.

## Open

- `[eng S few]` Extend the GMP oracle with the deferred checks: exact Form-920 layout, gold PDF strings, Change Control IDs, email subject microcopy and owner names. (§ Soft / later (not v1))
