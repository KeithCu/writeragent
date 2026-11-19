# Notes — what changed vs gold

Purpose: **harness debug** for the first eval-2 sibling where **Draw is
the product** — a process flow map. Invert GMP Change Control: that
task pre-opens a Draw **form stand-in** so a Writer risk memo can be
filled. Here the open Draw document **is** the deliverable. Writer is
optional or absent. Score `get_draw_tree` + shapes / fill, not a Writer
memo that happens to mention a diagram.

Eval-2 is not a multi-model benchmark yet (that comes when ~10 siblings
exist).

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical
to gold `prompt.txt`):

1. `Create a high-quality process map in PDF` → `Create a high-quality process map in this already-open Draw document`. Gold assumes a new PDF; v1 builds the pre-opened Draw canvas. WriterAgent does **not** edit PDFs.

No smoother-path remaps of Clearbend, automation vs manual lanes, scan /
sort, or failure reroute. Do **not** add a one-liner that says “don’t
invent process steps” — the oracle fail-closes on those anchors instead.

The Draw prompt does **not** name product internals (`get_draw_tree`,
`shape_upsert`, `shape_connect`, specialized-domain tags). Process-map
wording is the gold claim. Peer v1 rejects `PresentationDocument` — do
**not** use Impress as a substitute.

## Pre-open recipe (v1 cheat)

Harness `--launch` opens **only** the Draw canvas before START (no
Writer memo, no gold PDF):

1. `Process Flow Map.odg` (editable Draw stand-in; chat starts here)
2. Gold `Process Flow Map.pdf` stays under `gold/` / the gdpval tree —
   research-only if a human opens it; **not staged**

Still **manual** for a headed Scrolly trial: open the **Draw sidebar**;
paste `prompt.writeragent.txt` there. Watch the UNO message box and
`writeragent_debug.log`.

TODO (harness, later — not this fixture): auto-open the Draw WriterAgent
deck without a human click. Do not invent a second panel-factory here.

## Oracle softness

Tenant / Cadaver / GMP taught brittle exact-string gold matching fails
real headed drafts. Soften (do **not** weaken Clearbend, automation vs
manual, scan, sort, decision, failure/reroute, husk ban; do **not**
change `prompt.writeragent.txt`):

1. Drawing present = ≥**6** labeled shapes (title-only “Process Flow
   Map” does not count) and ≥**80** labeled characters — not gold-PDF
   string equality.
2. Connectors = ≥**2** `draw:connector` / `draw:line` / `draw:polyline`.
3. Process anchors accept aliases (conveyable / non-conveyable,
   jam / no-read / reject, receive / unload / induct, triage / rework).
4. Hyphen-like characters fold to ASCII before those searches.
5. Eval-2 scores the **saved trial `.odg`**, not the gold PDF. Do **not**
   require gold-PDF geometry, z-order, or exact diamond vs rectangle
   conventions.

## Not changed

- Clearbend Logistics Hub inbound piece-flow claim
- Automation vs manual lanes; classification decision; scan; sort;
  failure reroute into the manual workflow
- Gold rubric, `task.json`, `meta.txt`, `prompt.gdpval.txt`, gold PDF
  bytes
- `docs/eval/gdpval/` contents (this PR does not edit that tree)
- Everyday `chatbot.max_tool_rounds` default (15)

Harness pass/fail for this variant is [`rubric.eval2.md`](rubric.eval2.md),
not a letter-shift of `rubric_pretty.txt`.

## Open

- `[eng S few]` Harness: open the Draw WriterAgent sidebar deck automatically for a headed trial instead of a human click. (§ Pre-open recipe (v1 cheat))
