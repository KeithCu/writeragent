# rubric.eval2 — soft Writer + Calc peer-write oracle (v1)

Derived from the **gold prompt + email-trail + store-list fixtures**,
not by letter-shifting gold [`rubric_pretty.txt`](rubric_pretty.txt).
Gold asks for a new Excel budget plus a Word email. This variant scores
the **saved open Writer draft** (`.odt` / `.docx`) **and** the **Calc
workbook** (`.ods` / `.xlsx`). Do **not** fail on Word vs ODT vs ODS vs
XLSX.

Leave [`docs/eval/gdpval/`](../../gdpval/) untouched.

## What is scored

The harness opens the saved trial pair (`final_memo.odt` +
`final_workbook.ods`, or a trial directory). **Chat Ready / STREAM_DONE
is ignored.** An empty email or a still-blank scaffold that ended Ready
still fails. Extract ODT `text:h` and `text:p`; workbook cell text from
the budget file (not a store-list research copy).

| # | Check | Source | Fail closed |
|---|--------|--------|-------------|
| 1 | Writer doc present; body non-empty; word band **40–2000** | Draft email | Missing / unreadable / no text; `< 40` or `> 2000` |
| 2 | Calc workbook **was written**: more than the staged scaffold, and not a research-only store-list copy | Writer prompt (fill the open workbook) | Unchanged husk; store-list pasted with no budget work |
| 3 | Cost comparison theme (original vs revised, unit cost, and/or program cost) | Writer prompt | Workbook has no budget comparison |
| 4 | **Floorstand**, **store(s)**, **shelf strip** (accept floor stand / floorstands; hyphen-like chars fold to ASCII) | Prompt + gold rubric | Any identity missing in email **or** workbook |
| 5 | Email **cites** the budget / program workbook (budget workbook / floorstand budget / program cost) | Writer prompt | No workbook cite in the email |
| 6 | Email summarizes floor-stand display budget (units / program budget / new total — aliases OK) | Writer prompt | Theme missing |
| 7 | Ban empty / Ready-only / `Error:` husks | Observed residue | Body or cells match `Error:` / `#DIV/0!` husks or are blank |

Identity facts may live in **either** deliverable. A workbook that is
only the original store list or final matrix still fails even if the
email is well formatted.

## Soft / later (not v1)

Exact gold dollars ($17.69 / $24,670.80 / $1,868.39), unit counts
(1,228 / 1,257 / 1,289 / 1,320), Store 4099 / 3737 status strings,
email subject-line microcopy, sheet-tab cosmetics, highlight colors,
and +5 style. Do not fail on `.docx` vs `.odt` vs `.ods` vs `.xlsx`.

## Non-goals

- Scoring AFC sampling flags or GMP form `fld_*` boxes.
- Treating Cadaver Budget as this write target (research-only there).
- Spawning a new untitled workbook as the “deliverable” (v1 pre-opens
  the scaffold).
- A full gold-rubric grader or a product Ready predicate.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **150** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_floorstand_oracle.py` or
`scripts/eval_2_headed.py --task writer-calc-peer-write --score`.

## Open

- `[eng S]` Extend the peer-write oracle with the deferred checks: exact gold dollars and unit counts, Store 4099 and 3737 status strings, sheet-tab cosmetics and highlight colors. (§ Soft / later (not v1))
