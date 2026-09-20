# Notes — Reverse Tenant (Theatre CBA)

Purpose: **harness debug** that **inverts Tenant Retention**. Calc is the
only write deliverable. Writer is the brief / instructions sibling. The
open workbook reads the `.odt` excerpt (and the sample roster), then
**writes the model**.

Eval-2 is not a multi-model benchmark yet. This folder is headed-ready
for a live trial; it is not a multi-model benchmark row.

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical
to gold `prompt.txt`):

1. `Use the attached collective bargaining agreement (CBA) excerpt to build a spreadsheet in Excel` → `Use the collective bargaining agreement (CBA) excerpt in this folder to build a spreadsheet in this open workbook`. Gold assumes a new Excel file; the trial assumes a blank Calc workbook is already open.
2. `A sample roster and schedule have been attached as reference materials` → `A sample roster and schedule are in this folder as reference materials`.
3. Closing session lines: this workbook is the deliverable; the Writer `CBA excerpt` holds the contract; do not rewrite that Writer document.

No smoother-path remaps of wage-table dollars, musician names, or
section numbers. Do **not** add a one-liner that says “don’t invent
rates” — the oracle fail-closes on CBA / theatre / roster anchors
instead.

The Writer prompt does **not** name product internals
(`document_research`, `send_peer_work/send_peer_result`, specialized-domain tags).
Theatre / CBA / contractor wording is the gold claim.

## How it differs from existing siblings

| Sibling | Open / write | Other file |
|---------|--------------|------------|
| Tenant | Writer memo is the deliverable | Letter + exit survey are **reads** |
| Cadaver | Writer proposal | Budget is research-only |
| AFC | Calc sampling | No Writer sibling |
| Slot 5 peer-write | Writer **starts**; Calc is a second write via peer | Two-turn write pair |
| **This task** | **Calc is the only write** | Writer `.odt` is the brief |

## Pre-open recipe

Harness `--launch` opens **both** docs before START (no spawn-new):

1. `CBA excerpt.odt` (Writer brief; research / read-only)
2. `Theatre CBA.ods` (blank Calc; chat starts here)
3. Folder ref (not opened): `Sample roster and schedule.xlsx`

Paste `prompt.writeragent.txt` in the **Calc** sidebar. The Writer file
is not a second write target.

Headed start is **150** rounds: this is a complex payroll model
(rates + roster + schedule + premiums / doubling / vacation + CBA
conflict flags) that must read a Writer brief sibling. Same budget as
GMP multidoc. Schema **max is 200**. Everyday default stays **15**.

## Oracle softness

Exact gold payroll dollars (synthesizer audit `$504.12`, per-person
totals, …) are **not** v1. Soften (do **not** drop CBA / theatre /
roster anchors or the husk ban; do **not** change
`prompt.writeragent.txt`):

1. Workbook present and populated (not the staged blank Sheet1).
2. CBA / contract / payroll / wage labels (accept contractor, wages,
   weekly guarantee — gold never writes the letters “CBA”).
3. Theatre / musician / orchestra aliases (gold uses contractor +
   instrument names).
4. Roster / schedule labels plus ≥3 sample-roster instruments.
5. ≥3 payroll categories among audit, sound check, rehearsal,
   performance, premium, doubling, vacation.
6. Husk ban. Writer brief is recorded as present when it sits beside
   the workbook; missing brief does not fail a saved
   `final_workbook`.

## Not changed

- Gold theatre / contractor / weekly-payroll claim
- Sample roster instruments and service types
- CBA excerpt wage table and Article 4 / 7 / 10 / 11 terms
- Gold rubric, `task.json`, `meta.txt`, `prompt.gdpval.txt`, gold bytes
- `docs/eval/gdpval/` contents (this PR does not edit that tree)
- Everyday `chatbot.max_tool_rounds` default (15)

Harness pass/fail for this variant is [`rubric.eval2.md`](rubric.eval2.md),
not a letter-shift of `rubric_pretty.txt`.
