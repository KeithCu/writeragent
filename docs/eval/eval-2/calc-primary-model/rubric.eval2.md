# rubric.eval2 — soft Calc-primary model oracle (v1)

Derived from the **gold prompt + Raw Data fixture**, not by
letter-shifting gold [`rubric_pretty.txt`](rubric_pretty.txt) or AFC
`rubric.eval2.md` (no Sample / SSC / column K). Gold asks for a new
Excel reporting package. This variant scores **sheets in the open
workbook**. Do **not** fail on `.xlsx` vs `.ods`.

Leave [`docs/eval/gdpval/`](../../gdpval/) untouched.

## What is scored

The harness opens the saved trial artifact (`final_workbook.ods`, or
`.xlsx` if that is what was saved). **Chat Ready / STREAM_DONE is
ignored.** Empty `sheet_create` tabs that ended Ready still fail.

| # | Check | Source | Fail closed |
|---|--------|--------|-------------|
| 1 | Workbook present and readable | Headed save | Missing / unreadable |
| 2 | **≥5** populated schedule sheets beyond `Raw Data` | Writer prompt components 1–5 | Fewer than five non-raw sheets with rows; header-only / blank tabs |
| 3 | Component themes identifiable (income / trend / rank / region / metrics) | Writer prompt §1–5. Names may vary | Any of the five themes missing from sheet names **and** body labels |
| 4 | Amount columns use **formulas** (relative refs), not a pasted constant column | Gold “derived figures are from formulas”; miss = pin / hardcoded | Too few formula cells on schedule sheets |
| 5 | Fill vs pin: relative refs **adjust** down a column | Same actuation bug AFC saw, new claim | ≥4 identical relative-formula strings in one column |
| 6 | Factor / rate tokens from the fixture: **Revenue (Units)** (ARPU denom), **Project Backlog (Units)**, **Implementation Hours**, **Shared Service Allocations** (or Allocations) | Prompt metrics + fixture account names | Any required factor token missing |
| 7 | Period map: **M1–M12** / 2023 and **M13–M24** / 2024; last two months **M23** and **M24** | Prompt + fixture headers | Period anchors missing |
| 8 | **10 branches** (Branch 1–10) and **Regions A–G** | Prompt §3–4; Corporate is in the fixture but is not one of the ten | Too few branch names, or a missing region letter |
| 9 | Model line items: Revenue, COGS, SG&A, ARPU, EBITDA, gross margin | Prompt §1–5 | Any theme missing |
| 10 | Ban dominant husks / `Error:` / `#DIV/0!` residue | Observed Ready-empty residue | ≥50% of scored schedule cells are husks |

`__Anonymous_Sheet_DB__*` is a conversion artifact, not a deliverable
sheet.

## Soft / later (not v1)

Dropdown data-validation chrome, exact gold NPV / ranking order, chart
styling, and a full gold-rubric grader. Do not fail on `.xlsx` vs `.ods`.

## Non-goals

- AFC sampling / flag-count / SSC oracles.
- A product Ready predicate.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **150** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_calc_primary_oracle.py` or
`scripts/eval_2_headed.py --task calc-primary-model --score`.
