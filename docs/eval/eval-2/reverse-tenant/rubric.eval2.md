# rubric.eval2 — soft Reverse Tenant (Theatre CBA) oracle

Derived from the **gold prompt + CBA excerpt + sample roster**, not by
letter-shifting gold [`rubric_pretty.txt`](rubric_pretty.txt). Gold asks
for a new Excel file named `Theatre CBA.xlsx`. This variant scores the
**saved open Calc workbook** (`.ods` / `.xlsx`). Do **not** fail on
Excel vs ODS.

Leave [`docs/eval/gdpval/`](../../gdpval/) untouched.

## What is scored

The harness opens the saved trial artifact (`final_workbook.ods`, or
`.xlsx` if that is what was saved; a trial directory works too).
**Chat Ready / STREAM_DONE is ignored.** An empty workbook that ended
Ready still fails. The Writer `CBA excerpt` is a **read**; it is recorded
as present when it sits beside the workbook and is **not** required to
pass a saved `final_workbook`.

| # | Check | Source | Fail closed |
|---|--------|--------|-------------|
| 1 | Workbook present; ≥**12** populated cells | Calc is the deliverable | Missing / unreadable / staged-blank Sheet1 |
| 2 | CBA / contract / payroll / wage theme (accept contractor, wages, weekly guarantee; gold never writes “CBA”) | Prompt + excerpt | Theme missing |
| 3 | Theatre / musician / orchestra aliases (accept contractor + instrument names) | Prompt | Theme missing |
| 4 | Roster **or** schedule label | Prompt + sample roster | Neither label |
| 5 | ≥**3** sample-roster instruments (synthesizer, violin, viola, cello, acoustic bass, guitar, trumpet, woodwind, French horn) | Roster fixture | Too few instruments |
| 6 | ≥**3** payroll categories among audit, sound check, rehearsal, performance, premium, doubling, vacation | Gold prompt categories | Too few categories |
| 7 | Ban dominant husks (`Error:` / `#DIV/0!` / `PYTHONFUNCTION` / `_deal_*`) | Observed residue | ≥50% of scored cells are husks |

Wrong product (a Tenant-style Writer memo) fails because this CLI
scores a workbook, not a memo.

## Soft / later (not v1)

Exact gold payroll dollars, dropdown cosmetics, named-range layout,
per-musician totals, and +5 style. Do not fail on `.xlsx` vs `.ods`.
Do not require the Writer brief to stay byte-identical.

## Non-goals

- Reusing Tenant Retention section titles or Harborview counts.
- Scoring the Writer excerpt as the deliverable.
- A full gold-rubric grader or a product Ready predicate.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **150** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_reverse_tenant_oracle.py` or
`scripts/eval_2_headed.py --task reverse-tenant --score`.

## Open

- `[eng M few]` Extend the Reverse Tenant oracle with the deferred checks: exact gold payroll dollars, named-range layout, per-musician totals and dropdown cosmetics. (§ Soft / later (not v1))
