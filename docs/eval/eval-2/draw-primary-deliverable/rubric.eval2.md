# rubric.eval2 — soft Draw-primary process-map oracle (v1)

Derived from the **gold prompt + process-map claim**, not by
letter-shifting gold [`rubric_pretty.txt`](rubric_pretty.txt). Gold
asks for a PDF process map. This variant scores the **saved open Draw
canvas** (`.odg`). Do **not** fail on PDF vs ODG. Do **not** score a
Writer memo as a substitute.

Leave [`docs/eval/gdpval/`](../../gdpval/) untouched.

## What is scored

The harness opens the saved trial drawing (`final_drawing.odg`,
`Process Flow Map.odg`, or a trial directory). **Chat Ready /
STREAM_DONE is ignored.** A title-only page that ended Ready still
fails. Extract Draw `draw:frame` / `draw:custom-shape` (and other node
shapes) text plus `draw:connector` / `draw:line` / `draw:polyline`.

| # | Check | Source | Fail closed |
|---|--------|--------|-------------|
| 1 | Draw `.odg` present; ≥**6** labeled shapes (title-only “Process Flow Map” excluded) and ≥**80** labeled characters | Process map, not an empty canvas | Missing / unreadable / title-only husk |
| 2 | ≥**2** connectors (flow links) | Gold: at least one node + one arrow; headed needs more than orphans | Boxes with no flow |
| 3 | **Clearbend** (Logistics Hub) | Prompt + gold title criterion | Facility name missing |
| 4 | Automation / automated / conveyable **and** manual / incompatible / non-conveyable | Prompt two lanes | Either lane theme missing |
| 5 | Separation **decision** / classification / compatibility | Prompt decision point | Theme missing |
| 6 | **Scan** (barcode / OCR / RFID) | Prompt + gold scanning step | Theme missing |
| 7 | **Sort** / divert / auto routing | Prompt + gold automation sort | Theme missing |
| 8 | Failure / jam / no-read / reject / exception | Prompt failure reroute | Theme missing |
| 9 | Receive / unload / induct / inbound / loading | Prompt intake before split | Theme missing |
| 10 | Start / end / dispatch / outbound | Gold terminals | Theme missing |
| 11 | Manual triage / rework (relabel / repack / rescan / …) | Prompt manual path | Theme missing |
| 12 | Ban empty / Ready-only / `Error:` husks | Observed residue | Body matches `Error:` / `#DIV/0!` husks or is blank |

Wrong process identity fails even if the page is well formatted.

## Soft / later (not v1)

Exact geometry, z-order, gold-PDF string equality, diamond vs rectangle
conventions, “exactly two outgoing connectors” from the decision, and
+1 style (intended-use footer, connector-label microcopy). Do not fail
on `.odg` vs gold `.pdf`.

## Non-goals

- Filling or scoring the gold PDF.
- A Writer memo that describes a chart.
- Impress / `PresentationDocument`.
- GMP Form-920 `fld_*` fill.
- A full gold-rubric grader or a product Ready predicate.
- Raising the everyday `chatbot.max_tool_rounds` default (15). Headed
  helper writes **50** for this task. Schema **max is 200**.

CLI: `scripts/eval_2_draw_oracle.py` or
`scripts/eval_2_headed.py --task draw-primary --score`.

## Open

- `[eng S]` Extend the Draw process-map oracle with the deferred checks: geometry and z-order, diamond vs rectangle conventions, exactly two outgoing connectors from the decision, and style extras like an intended-use footer. (§ Soft / later (not v1))
