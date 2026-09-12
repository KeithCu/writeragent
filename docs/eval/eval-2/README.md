# eval-2 — WriterAgent hard-task variants

**Different benchmark from the 17-task string pack**
([`docs/eval/benchmarks.md`](../benchmarks.md) / Pareto). Same scoring
philosophy (hard / partial / cost), but headed multi-doc GDPval-style
tasks that are much harder. One filled task (AFC or any Ready slot)
still counts as a hard benchmark. Sibling charts:
[`benchmarks.md`](benchmarks.md) — do not fold into the string-pack Pareto.

**Eval-2 is for debugging the harness.** Iterate here. Do **not** edit
`docs/eval/gdpval/` gold trees except by **adding** a new untouched gold
id. Catalog-wide sweeps still wait; empty cells mean no in-repo headed
stamp. Empty cost or partial stays empty — do not invent run costs or
check counts.

`test_gold_prompt_is_byte_copy_of_hf_tree` compares `prompt.txt` and
`prompt.gdpval.txt` as bytes to the HF `task.json` `prompt` (LF).
`.gitattributes` pins those files to `eol=lf` so Windows checkout does
not rewrite them as CRLF.

Living headed autopsy (product bar + peer polarity + next experiments): [`headed-failure-autopsy.md`](headed-failure-autopsy.md).
Cross-model AFC catalog pain points (after #741 FIFO): [`afc-cross-model-pain-points.md`](afc-cross-model-pain-points.md).
Scoreboard + SVG charts: [`benchmarks.md`](benchmarks.md).
Paid Nemotron 3 Ultra 550B (`nvidia/nemotron-3-ultra-550b-a55b`) and
Nemotron 3 Super 120B (`nvidia/nemotron-3-super-120b-a12b`) are
registered on the eval catalog for later AFC/string stamps — no headed
cells yet.

Each subdirectory is one experiment. **Ready** means headed helper +
oracle exist. **Stub** means fixture notes only — not headed-ready gold.

| # | Experiment | App | Status | Gold / source |
|---|------------|-----|--------|----------------|
| 1 | [`tenant-retention-ed2bc14c/`](tenant-retention-ed2bc14c/) | Writer + folder read | Ready | `ed2bc14c-99ac-4a2a-8467-482a1a5d67f3` |
| 2 | [`cadaver-proposal-61b0946a/`](cadaver-proposal-61b0946a/) | Writer + budget research | Ready | `61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0` |
| 3 | [`afc-sample-83d10b06/`](afc-sample-83d10b06/) | Calc / sampling-style | Ready | `83d10b06-26d1-4636-a32c-23f92c57f30b` |
| 4 | [`gmp-change-control-58ac1cc5/`](gmp-change-control-58ac1cc5/) | Writer + Draw form, peer fill | Ready | `58ac1cc5-5754-4580-8c9c-8c67e1a9d619` |
| 5 | [`writer-calc-peer-write/`](writer-calc-peer-write/) | Writer → Calc write (Floorstand budget) | Ready | `c3525d4d-2012-45df-853e-2d2a0e902991` |
| 6 | [`calc-primary-model/`](calc-primary-model/) | Calc-primary model (not sampling) | Ready | `5f6c57dd-feb6-4e70-b152-4969d92d1608` |
| 7 | [`writer-headed-template/`](writer-headed-template/) | Writer on a real template | Stub / **PARKED** | `a46d5cd2-55fe-48fa-a4c6-6aaf6b9991b5` gold tree in-repo; **PARKED** (headed letterhead) |
| 8 | [`draw-primary-deliverable/`](draw-primary-deliverable/) | Draw-primary (process flow map) | Ready | `8a7b6fca-60cc-4ae3-b649-971753cbf8b9` |
| 9 | [`reverse-tenant/`](reverse-tenant/) | Calc deliverable; Writer brief sibling | Ready | `4520f882-715a-482d-8e87-1cb3cbdfe975` |
| 10 | [`long-writer-pack/`](long-writer-pack/) | Long Writer pack (TOC + styles + comments) | Headed-ready (native fixture) | WriterAgent-native; no in-repo gold fits |

Headed helper: `scripts/eval_2_headed.py` writes `chatbot.max_tool_rounds`
to **50** (AFC / Tenant / Cadaver / Long Writer pack / Draw-primary) or
**150** (GMP Change Control / Writer→Calc Floorstand / Reverse Tenant /
Calc-primary model) and restores when done. Everyday default stays
**15**. Schema **max is 200** so a trial can temporarily set 80 or 200
without clamp.
Do not hand-edit `writeragent.json`. Do not open `fixtures/` or the task
directory — `--launch` stages a clean trial dir so `document_research`
cannot list prompt/rubric/gold.

**Debug log (standing rule):** every headed run must keep a **full**
copy of `writeragent_debug.log`. LO restart for the next trial re-inits
logging (`Debug log active`) and can reset the live profile file —
Floorstand Gemini `20260909-1748` lost its tool-call trace that way.
`--launch` snapshots the live log on Enter / Ctrl-C into `--run-dir`
(typically `docs/eval/eval-2/<task>/runs/<stamp>/writeragent_debug.log`)
or an auto-stamp `YYYYMMDD-HHMM` under that task’s `runs/`. Mid-stall,
**before** restarting LibreOffice:

```bash
.venv/bin/python scripts/save_eval2_debug_log.py docs/eval/eval-2/<task>/runs/<stamp>
```

Slot **7 stays PARKED** and is **not wired** into `--task` / `--launch` /
`--score`. Do not invent helper flags for it. See that stub `run.md`.

```bash
# Calc / AFC (default)
.venv/bin/python scripts/eval_2_headed.py --launch
.venv/bin/python scripts/eval_2_headed.py --score docs/eval/eval-2/afc-sample-83d10b06/runs/<stamp>/final_workbook.ods

# Writer / Tenant Retention
.venv/bin/python scripts/eval_2_headed.py --task tenant-retention --launch
.venv/bin/python scripts/eval_2_headed.py --task tenant-retention --score docs/eval/eval-2/tenant-retention-ed2bc14c/runs/<stamp>/final_memo.odt

# Writer / Collaborative Cadaver Program Proposal
.venv/bin/python scripts/eval_2_headed.py --task cadaver-proposal --launch
.venv/bin/python scripts/eval_2_headed.py --task cadaver-proposal --score docs/eval/eval-2/cadaver-proposal-61b0946a/runs/<stamp>/final_proposal.odt

# Writer + Draw / GMP Change Control
.venv/bin/python scripts/eval_2_headed.py --task gmp-change-control --launch
.venv/bin/python scripts/eval_2_headed.py --task gmp-change-control --score docs/eval/eval-2/gmp-change-control-58ac1cc5/runs/<stamp>/final_memo.odt

# Writer + Calc / Floorstand holiday budget
.venv/bin/python scripts/eval_2_headed.py --task writer-calc-peer-write --launch
.venv/bin/python scripts/eval_2_headed.py --task writer-calc-peer-write --score docs/eval/eval-2/writer-calc-peer-write/runs/<stamp>/final_memo.odt

# Calc / Branch profitability (Calc-primary model)
.venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --launch
.venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --score docs/eval/eval-2/calc-primary-model/runs/<stamp>/final_workbook.ods

# Calc / Reverse Tenant (Theatre CBA)
.venv/bin/python scripts/eval_2_headed.py --task reverse-tenant --launch
.venv/bin/python scripts/eval_2_headed.py --task reverse-tenant --score docs/eval/eval-2/reverse-tenant/runs/<stamp>/final_workbook.ods

# Writer / Long Writer pack (native TOC + styles + comments)
.venv/bin/python scripts/eval_2_headed.py --task long-writer-pack --launch
.venv/bin/python scripts/eval_2_headed.py --task long-writer-pack --score docs/eval/eval-2/long-writer-pack/runs/<stamp>/final_pack.odt

# Draw-primary / Process Flow Map
.venv/bin/python scripts/eval_2_headed.py --task draw-primary --launch
.venv/bin/python scripts/eval_2_headed.py --task draw-primary --score docs/eval/eval-2/draw-primary-deliverable/runs/<stamp>/final_drawing.odg
```

AFC `--launch` still copies **only** `Population v2.ods` into
`$TMP/writeragent-eval2-afc`. Tenant `--launch` copies the renewal letter
`.odt` + exit-survey `.xlsx` into `$TMP/writeragent-eval2-tenant` and
opens a blank `Tenant Retention Strategy.odt`. Cadaver `--launch` copies
`Cadaver Budget.xlsx` into `$TMP/writeragent-eval2-cadaver` and opens a
blank `Collaborative Cadaver Program Proposal.odt` (budget is
research-only). GMP `--launch` copies the COA PDF + Material Spec `.odt`
+ Draw form stand-in into `$TMP/writeragent-eval2-gmp`, writes a blank
`MR Risk Assessment Summary.odt`, and opens **both** the Draw stand-in
and that memo (Writer last). The gold PDF is not the write target.
Floorstand `--launch` copies the email-trail `.odt` + original store-list
`.ods` + final-matrix `.ods` + empty budget scaffold into
`$TMP/writeragent-eval2-writer-calc`, writes a blank
`Draft Floorstand Email.odt`, and opens **both** the Calc scaffold and
that email (Writer last). The gold deliverable xlsx is not the write
target. Store lists are research-only.
Calc-primary `--launch` copies **only**
`Raw Data for Branch Profitability Final.ods` into
`$TMP/writeragent-eval2-calc-primary` and opens that copy in Calc.
Reverse Tenant `--launch` copies the CBA excerpt `.odt` + sample roster
`.xlsx` into `$TMP/writeragent-eval2-reverse-tenant`, writes a blank
`Theatre CBA.ods`, and opens **both** the Writer brief and that workbook
(Calc last). The Writer excerpt is research-only.
Long Writer `--launch` copies the two native research ODTs into
`$TMP/writeragent-eval2-long-writer` and opens a blank
`Northhaven Civic Library Capital Brief.odt` (no peer).
Draw-primary `--launch` copies **only** `Process Flow Map.odg` into
`$TMP/writeragent-eval2-draw` and opens that canvas (Draw is the
product). The gold PDF is not staged.

Oracles: [`scripts/eval_2_ods_oracle.py`](../../scripts/eval_2_ods_oracle.py)
(AFC workbook),
[`scripts/eval_2_tenant_oracle.py`](../../scripts/eval_2_tenant_oracle.py)
(Writer memo),
[`scripts/eval_2_cadaver_oracle.py`](../../scripts/eval_2_cadaver_oracle.py)
(Writer proposal),
[`scripts/eval_2_gmp_oracle.py`](../../scripts/eval_2_gmp_oracle.py)
(Writer memo + Draw form),
[`scripts/eval_2_floorstand_oracle.py`](../../scripts/eval_2_floorstand_oracle.py)
(Writer email + Calc budget),
[`scripts/eval_2_calc_primary_oracle.py`](../../scripts/eval_2_calc_primary_oracle.py)
(branch-profitability workbook),
[`scripts/eval_2_reverse_tenant_oracle.py`](../../scripts/eval_2_reverse_tenant_oracle.py)
(Theatre CBA workbook),
[`scripts/eval_2_long_writer_oracle.py`](../../scripts/eval_2_long_writer_oracle.py)
(Writer pack: TOC / styles / comments), and
[`scripts/eval_2_draw_oracle.py`](../../scripts/eval_2_draw_oracle.py)
(Draw process map). Rubrics:
[`afc-sample-83d10b06/rubric.eval2.md`](afc-sample-83d10b06/rubric.eval2.md),
[`tenant-retention-ed2bc14c/rubric.eval2.md`](tenant-retention-ed2bc14c/rubric.eval2.md),
[`cadaver-proposal-61b0946a/rubric.eval2.md`](cadaver-proposal-61b0946a/rubric.eval2.md),
[`gmp-change-control-58ac1cc5/rubric.eval2.md`](gmp-change-control-58ac1cc5/rubric.eval2.md),
[`writer-calc-peer-write/rubric.eval2.md`](writer-calc-peer-write/rubric.eval2.md),
[`calc-primary-model/rubric.eval2.md`](calc-primary-model/rubric.eval2.md),
[`reverse-tenant/rubric.eval2.md`](reverse-tenant/rubric.eval2.md),
[`long-writer-pack/rubric.eval2.md`](long-writer-pack/rubric.eval2.md),
[`draw-primary-deliverable/rubric.eval2.md`](draw-primary-deliverable/rubric.eval2.md).
Slot 7 stays PARKED; no CLI scorer.
