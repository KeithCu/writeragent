# Notes — what changed vs gold

Purpose: **harness debug** for the first eval-2 **multidoc + Draw form-fill**
sibling. Tenant / Cadaver are Writer-primary with folder refs. This task
pre-opens a Writer risk memo **and** an editable Draw Form-920 stand-in.
Eval-2 is not a multi-model benchmark yet (that comes when ~10 siblings
exist).

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical
to gold `prompt.txt`):

1. `Use the attached blank form` / `Attach the completed form as a separate PDF document.` → `The blank Change Control Tracking Form is already open as a Draw document in this session. Fill that form…` Gold assumes a new PDF; v1 fills the pre-opened Draw stand-in. WriterAgent does **not** edit PDFs / AcroForms.
2. QA email + Teams note stay gold tasks, written as **clearly labeled sections in this open Writer document**.
3. `The completed risk assessment should be attached as a separate Word document.` → `Write the completed risk assessment in this open Writer document. Cite key fields from the filled Change Control form…`
4. `review the source materials` → `review the source materials in this folder` (same “already in session” shape as Tenant / Cadaver).

No smoother-path remaps of RMS-3333 / CompCello / QY-GEL / endotoxin
anchors. Do **not** add a one-liner that says “don’t invent COA numbers”
— the oracle fail-closes on those anchors instead.

The Writer prompt does **not** name product internals (`send_peer_work/send_peer_result`,
`fill_draw_fields`, specialized-domain tags). GMP / change-control wording
is the gold claim.

## Pre-open recipe (v1 cheat)

Harness `--launch` opens **both** docs before START (no spawn-new):

1. `MR Risk Assessment Summary.odt` (blank Writer; chat starts here)
2. `Change Control Form.odg` (editable Draw stand-in)
3. Folder refs (not opened): `Anti foam COA_MR.pdf`, `Material Spec_MR.odt`

Still **manual** for a headed Scrolly trial: open the **Draw sidebar once**
so the form is a live peer; paste `prompt.writeragent.txt` in the Writer
sidebar. Watch the UNO message box and `writeragent_debug.log`.

TODO (harness, later — not this fixture): auto-open the Draw WriterAgent
deck / register the peer without a human click. Do not invent a second
panel-factory here.

## Oracle softness (headed false-FAIL lessons)

Tenant (#665) / Cadaver (#668) taught brittle exact-string gold matching
fails real headed drafts. Soften (do **not** weaken RMS-3333, CompCello,
QY-GEL / Antifoam, Report Result, `< 1 EU`, quarantine/hold, husk ban;
do **not** change `prompt.writeragent.txt`):

1. Form “substantially filled” = ≥5 of 12 named `fld_*` boxes **and** ≥80
   filled characters — not gold-PDF string equality.
2. Identity facts may appear in **either** memo or form.
3. QA email / Teams note / process-gap / SOP aliases (see
   [`rubric.eval2.md`](rubric.eval2.md)).
4. Word band **200–2500**.
5. Expert gold uses **XX-CELL** / **Lot A23-044** / item `AF-COMP-333`.
   Those contradict the prompt (QY-GEL) and the COA lot (`00004515`).
   Eval-2 scores the **saved trial pair**, not that gold file. Do **not**
   require gold’s invented codes.
6. Identity ids fold hyphen-like characters (U+2010, U+2011, U+2212, NBSP)
   to ASCII before the RMS-3333 / QY-GEL checks. Do **not** drop the
   RMS-3333 requirement — only the encoding false-red.
7. Form-cite aliases include prompt wording (Change Control Request / CCR /
   completed … change control), not only tracking form / filled form /
   Form-920. Still fail if the memo never acknowledges the form-side
   deliverable.

## Not changed

- Four gold tasks (form, QA email, Teams note, risk assessment)
- RMS-3333 / CompCello / QY-GEL Antifoam / endotoxin mismatch
- Vendor memo two months prior; leftover inbox; no centralized process
- Gold rubric, `task.json`, `meta.txt`, `prompt.gdpval.txt`, gold bytes
- `docs/eval/gdpval/` contents (this PR does not edit that tree)
- Everyday `chatbot.max_tool_rounds` default (15)

Harness pass/fail for this variant is [`rubric.eval2.md`](rubric.eval2.md),
not a letter-shift of `rubric_pretty.txt`.
