# Notes — what changed vs gold

Purpose: **harness debug** for the first eval-2 **two-turn peer mutation
of an already-open workbook**. Writer starts. Calc is a **write** target,
not research-only. Differs from Cadaver (budget research-only) and AFC
(Calc-only sampling). Eval-2 is not a multi-model benchmark yet (that
comes when ~10 siblings exist).

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical
to gold `prompt.txt`):

1. `attached as Holiday Floorstand Store List Original.xlsx` /
   `Attached are the email threads … (Email Trail Floorstands.docx)` /
   `the final store list is attached for reference (Holiday Matrix final count.xlsx)`
   → files **in this folder** (same “already in session” shape as Tenant /
   Cadaver / GMP). Extensions dropped so `.ods` / `.odt` siblings still match.
2. `Please deliver an Excel file.` → `The holiday floorstand budget workbook
   is already open as a Calc document in this session. Fill that workbook.`
   Gold assumes a new Excel file; v1 fills the pre-opened scaffold.
3. `Please also deliver a draft email in Word document format.` →
   `Write the draft email in this open Writer document.` plus a cite of
   the updated budget workbook (updated units, program-budget change,
   new total).
4. `If you are unsure of a figure, leave that cell blank rather than
   guessing.` — same leave-blank spirit as GMP form blanks. Do **not**
   add a one-liner that says “don’t invent store counts.”

No smoother-path remaps of $0.25 / Store 4099 / Store 3737 / overage.
Do **not** add eval-oracle cheat phrases or exact gold totals
($24,670.80 / 1,320) to the product prompt.

The Writer prompt does **not** name product internals (`send_peer_work/send_peer_result`,
`specialize`, specialized-domain tags). Floorstand / shelf-strip wording
is the gold claim.

## Pre-open recipe (v1 cheat)

Harness `--launch` opens **both** docs before START (no spawn-new):

1. `Draft Floorstand Email.odt` (blank Writer; chat starts here)
2. `Holiday Floorstand Budget.ods` (empty two-tab Calc scaffold)
3. Folder refs (not opened): `Email Trail Floorstands.odt`,
   `Holiday Floorstand Store List Original.ods`,
   `Holiday Matrix final count.ods`

Still **manual** for a headed Scrolly trial: open the **Calc sidebar once**
so the workbook is a live peer; paste `prompt.writeragent.txt` in the
Writer sidebar. Watch the UNO message box and `writeragent_debug.log`.

TODO (harness, later — not this fixture): auto-open the Calc WriterAgent
deck / register the peer without a human click. Do not invent a second
panel-factory here.

## Oracle softness (headed false-FAIL lessons)

Tenant (#665) / Cadaver (#668) / GMP taught brittle exact-string gold
matching fails real headed drafts. Soften (do **not** weaken floorstand /
stores / shelf strip, pair-present, “workbook was written”, Writer cites
budget/program, husk ban; do **not** change `prompt.writeragent.txt`):

1. Workbook “was written” = more than the staged scaffold **and** not a
   research-only copy of a store-list ref. Cost-comparison theme required.
   Exact gold dollars / 1,320 units / $24,670.80 are **not** required.
2. Identity facts (floorstand / stores / shelf strip) may appear in
   **either** email or workbook.
3. Email word band **40–2000** (gold draft is short; Ready-empty is far
   shorter).
4. Expert gold email microcopy (“Please see attached excel summary”) is
   not required. Eval-2 scores the **saved trial pair**, not that gold file.
5. Do **not** fail on `.docx` vs `.odt` vs `.ods` vs `.xlsx`.

## Not changed

- Gold claim: $0.25 shelf-strip increase; Store 4099 / 3737 status;
  overage from Production; original vs revised unit and program cost;
  final store list with added locations; draft email summarizing units,
  budget change, and new total
- Gold rubric, `task.json`, `meta.txt`, `prompt.gdpval.txt`, gold bytes
- `docs/eval/gdpval/` contents (this PR does not edit that tree)
- Everyday `chatbot.max_tool_rounds` default (15)

Harness pass/fail for this variant is [`rubric.eval2.md`](rubric.eval2.md),
not a letter-shift of `rubric_pretty.txt`.
