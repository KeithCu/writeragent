# Notes — what changed vs gold

Purpose: **harness debug** for the first eval-2 **Calc-primary model**
sibling. AFC is sampling / variance / flags. This task is a five-tab
branch-profitability reporting package whose **formulas must be right**.
Eval-2 is not a multi-model benchmark yet (that comes when ~10 siblings
exist).

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical
to gold `prompt.txt`):

1. `Using the attached Excel spreadsheet containing raw financial data`
   → `This spreadsheet titled ‘Raw Data’ contains raw financial data`
   so the trial assumes the gold reference sheet is already open in Calc.
2. `please develop the following Excel-based models and schedules. All
   schedules should be built in Excel` → `Please develop the following
   models and schedules in this open workbook.` Gold assumes a new Excel
   file; this variant stays in-workbook (same spirit as AFC).
3. `within the same workbook` / `as per raw data file` → `within this
   workbook` / `as per the Raw Data sheet` so the fixture tab name is
   the one LibreOffice actually shows.

No smoother-path remaps of Branch 1–10, Regions A–G, M1–M24, or account
names. Those strings are already cell text on `Raw Data`. Do **not** add
a one-liner that says “don’t invent rates” or names product internals —
the oracle fail-closes on formula/model anchors instead.

## Miss modes (keep distinct from AFC)

1. **Wrong factor** — ARPU / backlog / hours-per-head applied to the
   wrong series (Orders instead of Revenue Units; headcount mix-up).
2. **Pinned formula** — one relative formula string stamped down a
   column with no row adjust (same actuation bug AFC saw, new claim).
3. **Empty sheet_create** — a new tab exists and looks like a
   component; no schedule populated.

Not in scope: Sample / SSC / column K, sample-flag counts.

## Round budget

Headed helper writes **150** (not AFC’s 50). Five dropdown-driven
schedules plus ranking / regional / monthly metrics need more
create-sheet + formula + fill-down turns than AFC’s two-tab sampling.
Starving the loop at 50 would manufacture the empty-tab miss. Schema
**max is 200**. Everyday default stays **15**.

## Oracle softness

Exact gold NPV, dropdown data-validation chrome, and chart styling are
**soft / later**. V1 scores populated component sheets, formula trail,
relative fill vs pin, fixture factor tokens, and the husk ban. Do not
fail on `.xlsx` vs `.ods`. HF has **no** deliverable workbook — eval-2
scores the **saved trial ODS**, not a gold file.

## Not changed

- Five gold components (IS comparison, monthly trend, ranking,
  regional, metrics)
- M1–M12 = FY2023; M13–M24 = FY2024; M23 vs M24 MoM
- Variance sign convention (revenue up = positive; COGS / SG&A up =
  negative)
- Rank exactly the 10 branches; Regions A–G; Corporate is in the
  fixture but is not one of the ten
- Gold rubric, `task.json`, `meta.txt`, `prompt.gdpval.txt`
- `docs/eval/gdpval/` contents (this PR does not edit that tree)
- Everyday `chatbot.max_tool_rounds` default (15)

Harness pass/fail for this variant is [`rubric.eval2.md`](rubric.eval2.md),
not a letter-shift of `rubric_pretty.txt`.
