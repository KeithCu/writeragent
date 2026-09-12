# Eval-2 AFC catalog — cross-model pain points

**Date:** 2026-09-12  
**Sources:** merged PR #741 catalog table (FIFO through DeepSeek V4.1 Flash), run stamps under `docs/eval/eval-2/afc-sample-83d10b06/runs/`, `headed-failure-autopsy.md`, `first-principles-analysis-v4.md`, ODS oracle `scripts/eval_2_ods_oracle.py` (tip ~`2bd13bf4` after #741+#746).  
**Bar:** popular / multi-model product levers — not single-model cheats, not fixture one-liners, not eval gaming.  
**Catalog snapshot:** 24 AFC cells → 2 HAPPY (gate `google/gemini-3.8-flash`, catalog star `inception/mercury-2.5-preview`), 21 NOT_HAPPY, 1 BLOCKED (Solar Pro 4 infra).

This note ranks **recurring failure shapes** that hit several models, then proposes **DO + why** product/prompt/tool moves Keith can act on. Implementation stays out of scope unless a change clearly helps a cluster of ≥3 models — then propose here, don’t ship yet.

---

## 0. Happy path (what “good” looks like)

| Model | Shape that passed |
|---|---|
| Gate Gemini 3.8 Flash | Sample + SSC present; S≥R with parseable R (smoother era). |
| Mercury 2.5 Preview | Sample + SSC; near-full Population copy on Sample (`sample_data_rows≈1518`) but **S=494 ≥ R=68** and R labeled so the oracle parses it. |

Mercury shows the oracle accepts a “dump + flag” Sample if S≥R and R is parseable. Most NOT_HAPPY models never reach that bar — they miss sheets, leave Sample empty, leave K unflagged, or compute a sample size the oracle cannot read as R.

---

## 1. Taxonomy (refined from #741 + run notes)

Counts are **distinct catalog models** in the cluster (a model can appear in more than one if notes show compound fails). Evidence paths are under `docs/eval/eval-2/afc-sample-83d10b06/runs/<stamp>/`.

### C1 — Missing Sample and/or SSC entirely (Population-only / wrong workbook shape)

**Blast radius: high (≥5 models)**

| Models | Stamp / note |
|---|---|
| Nemotron 3.5 Lightning | `20260912-0310-…` — sheets: Population only; Err:507 in J/K |
| Qwen 3.8-27b | `20260912-0443-…` — Population + `=PY` attempts; never created Sample/SSC |
| GLM 5.3 Flash | `20260912-0515-…` — hung round 0 / `finish_reason=length`; no tools |
| MiniMax M3 | `20260912-0617-…` — Population + `Analysis` (`#NAME?`); no Sample/SSC |
| DeepSeek V4.1 Flash | `20260912-0630-…` — API timeout mid `read_cell_range`; leftover Population only |

**Shape:** Ready (or hang→Ready) without the two named deliverable tabs. Work stays on Population (filters, criteria columns, `=PY` probes) or invents an `Analysis` tab.

**Product read:** create/copy path still not the default plan for many models; large-range reads / `=PY` on Population compete with “create empty tab → copy/populate.”

### C2 — Empty Sample (tab exists, 0 data rows)

**Blast radius: high (≥4 models)**

| Models | Stamp / note |
|---|---|
| Grok 4.6 | `20260912-0342-…` — Sample + SSC present; Sample empty; R missing |
| Muse Spark 1.3 | `20260912-0409-…` — same shape |
| Granite 4.2 8b | `20260912-0546-…` — Sample blank; stall on `read_cell_range` |
| DeepSeek V4 Flash 0731 | `20260912-0621-…` — Sample empty (PreContract / deal wire noise); SSC blank |

Also: Laguna XS (`20260912-0429-…`) — Sample empty **and** SSC renamed (see C5).

**Shape:** `create_sheet` (or equivalent) ran; populate/copy never did. Matches the first-principles “create ≠ Sample deliverable” miss.

### C3 — R hole / S=0 with incomplete Sample (largest cluster)

**Blast radius: very high (≥10 models)**

Models with Sample+SSC present but oracle `R=None` and/or `S=0` despite rows:

| Models | Typical oracle line |
|---|---|
| gpt-oss-120b | S=585 R=None (near-full dump; SSC present) |
| gpt-5.6-luna | S=68 R=None; `Required sample size rounded up \| 66` |
| gpt-oss-20b | S=0 R=None; Sample ~80 rows; K often Err:508/509 husks |
| gemini-3.5-flash-lite | S=0 R=None; Sample 81; K often Err:508/509 husks |
| gemma-4-31b-it | S=0 R=None; SSC Err:508/#NAME?; K husks 508/509 |
| muse-glimmer-30b | S=0 R=None; Sample 81, **K empty**; `Required sample (rounded up) \| 65` |
| mistral-small-2603 | S=0 R=None; Sample **A–H only (8 cols, no J/K)** |
| qwen3.8-flash | S=68 R=None; `Required minimum sample size \| 68`; SSC Err:508/509/#VALUE! |
| granite / grok / muse-spark / deepseek-v4-flash | often compound with C2; Granite `Sample Size: 68` same cell; Muse Spark SSC inputs only (N/confidence), no final labeled integer |

**Two distinct product holes inside this cluster:**

1. **R unparseable / label near-misses** — SSC has workings / a claimed n≈65–68, but no cell the oracle accepts. Oracle only accepts a label matching `Sample size` / `Required sample size` / `R` in one cell and the integer in the **next** cell, or inline `R = N` (`parse_required_sample_size` / `_R_LABEL_RE` in `scripts/eval_2_ods_oracle.py`). Catalog near-misses that still score `R=None`: Luna `Required sample size rounded up | 66` (`rounded up` breaks `_R_LABEL_RE`); Qwen Flash `Required minimum sample size | 68`; Muse Glimmer `Required sample (rounded up) | 65`; Granite single-cell `Sample Size: 68` (colon, same cell); Muse Spark SSC inputs only (N/confidence) with no final labeled integer. **Parseable contrasts:** Mercury `Sample Size|68`; Gemma-26 `Required sample size|65`; Laguna-S `Required Sample Size|66`. Do **not** expand the regex for “rounded up” / “minimum” / “Calculated Sample Size” paraphrases or same-cell `Label: N`.
2. **S=0 despite Sample rows** — column K is not literal flag `1` on enough rows: Muse Glimmer Sample 81 rows but **K empty** (header says Sample); Mistral Small Sample **A–H only (8 cols, no J/K)** → S=0; Gemma-26 flags parked in **col G** as `1.0`, not K (also undersized — C6); oss-20b / Flash Lite / Gemma-31 K often Err:508/509 husks → S=0.

### C4 — SSC formula / `=PY` husks

**Blast radius: medium (≥3 models)**

| Models | Errors seen |
|---|---|
| gemma-4-31b-it | Err:508 / #NAME? on SSC; husks 81/810 on Sample |
| qwen3.8-flash | Err:508 / 509 / #VALUE! on SSC; Scratch Err:513 |
| seed-2.0-mini | malformed `=PY("""` husk-dominated Sample (1/1); SSC Err:501 |

**Product read:** models reach for `=PY` / exotic formulas for sample-size arithmetic that ordinary Calc (or a single labeled value write) would handle. Same family as the fill-down / anti-husk teachings in first-principles §2.1–2.2.

### C5 — Wrong sheet naming (spaces vs underscores)

**Blast radius: low today (1 clear underscore hit) — still product clarity**

| Model | Note |
|---|---|
| Laguna XS 2.1 | Created `Sample_Size_Calculation` → oracle treats SSC **missing** (spaces required: `Sample Size Calculation`) |
| MiniMax M3 | Invented `Analysis` instead of Sample/SSC (C1 compound; single-model-ish) |
| gpt-oss-120b | Extras `SampleTest` / `Sample_old` alongside Sample — aliases, not the deliverable titles |

**Flag:** underscore + invented aliases are the same hygiene miss. Prefer teaching exact titles on `create_sheet` over a fuzzy oracle sheet-name match.

### C6 — Undersized Sample vs parseable R

**Blast radius: low–medium (1–2 clear)**

| Model | Note |
|---|---|
| gemma-4-26b-a4b-it | First catalog parseable R=65; Sample only 10 rows; flags in **G** as `1.0` not K; S=0 → `S < R` |
| Laguna S 2.1 | R=66 on SSC but **Sample sheet missing** (C1/C7 compound) |

**Product read:** even when R labeling works, models don’t grow Sample / flags to meet R.

### C7 — Near-full Population dump as Sample

**Blast radius: medium (2 models; 1 HAPPY)**

| Model | Note |
|---|---|
| gpt-oss-120b | sample_data_rows=1516; S=585; R=None → FAIL |
| Mercury 2.5 | sample_data_rows=1518; S=494 ≥ R=68 → **PASS** |

**Product read:** dump+flag can be oracle-legal. Product HAPPY for humans may still want a tighter sample — keep that as observer taste, not an oracle harden that would fail Mercury. Prefer teaching **S≥R + labeled R + K flags**, not “don’t copy wide ranges.”

### C8 — Tool-loop / timeout / length mid-run

**Blast radius: medium (≥4), product plumbing more than prompt tips**

| Model | Failure |
|---|---|
| DeepSeek V4.1 Flash | API timed out mid `read_cell_range` |
| GLM 5.3 Flash | `finish_reason=length` round 0; no tools |
| Granite 4.2 | Hung round 46 on `read_cell_range` |
| Laguna S | length-truncated mid loop |

**Product read:** stall / timeout / large-read recovery — separate from AFC sheet contract. Don’t fold into model-specific prompt cheats.

### C9 — Infra BLOCKED (exclude from model pain)

| Model | Cause |
|---|---|
| Solar Pro 4 | sqlite `message_store` missing after wiped `writeragent_history.db` (0 bytes); Send never reached LLM |

Treat as harness/history wipe infra — **not** a model capability miss.

---

## 2. Ranked multi-model levers (DO + why)

Ordered by **blast radius × product generality**. Prefer colocated tool/prompt tips over eval-task essays. No interactive Ready gates on AFC predicates (first-principles §2.4 / §6).

### L1 — Make “create → populate” unavoidable at action time

**Hits:** C1 + C2 (≥8 models).

**DO:**
- Keep / sharpen `create_sheet` description + ok payload (“sheet created; **no cells copied**”) — already present in `plugin/calc/sheets.py`; verify the ok string stays visible in tool results models actually see.
- Colocate on `write_formula_range` with `source` (block copy; there is **no** separate `copy_range` tool): one line — “empty tab is not the deliverable; copy or write the data block onto the new sheet.”
- Keep `CALC_WORKFLOW` create≠populate one-liner; don’t restack a grab-bag essay.

**Why:** Empty Sample with tabs present is still one of the most common Ready-looking fails. Teaching at call time beats restating step 4 in the task prompt alone.

### L2 — Exact sheet-title contract on `create_sheet`

**Hits:** C5 now; reduces C1 false “I created SSC” when titles drift.

**DO:**
- On `create_sheet` `sheet` parameter description: “Use the **exact** title the user asked for (preserve spaces; do not snake_case or invent aliases).”
- Optional: ok payload echoes the created name so a rename mistake is visible in the next turn.

**Why:** Laguna XS shows underscore aliases make the oracle (and a picky user) treat the tab as missing. MiniMax `Analysis` and oss-120b `SampleTest`/`Sample_old` are the same family. Exact-title hygiene is general Calc hygiene, not an AFC cheat.

### L3 — Teach labeled final answers for “required sample size / R”

**Hits:** C3 R-hole (largest single fail mode across the catalog).

**DO (product-general, not oracle softens):**
- When the user asks for sample-size **workings**, a light Calc tip (workflow or `write_formula_range` values): put a label **exactly** like `Sample size` / `Required sample size` / `R` in one cell and the **final integer** in the **next** cell (or write `R = <n>` inline). Do **not** paraphrase (“rounded up”, “minimum”, “Calculated Sample Size”) and do **not** write `Sample Size: 68` in one cell — `_R_LABEL_RE` rejects those near-misses (Luna / Qwen Flash / Muse Glimmer / Granite).
- Do **not** expand the oracle regex to accept “rounded up” / “minimum” / “n=” paraphrases — that is eval softening. Fix the teaching so models emit the labels users/oracles already understand (Mercury `Sample Size|68`; Gemma-26 `Required sample size|65`; Laguna-S `Required Sample Size|66`).

**Why:** Many models *compute* ~65–68 and still score `R=None`. The gap is labeling, not Cochran math. Gate + Mercury prove parseable R is achievable.

### L4 — Flag column = literal `1` (selection marks)

**Hits:** C3 S=0 with nonempty Sample (≥6 models).

**DO:**
- Colocate near write paths used for flags: Sample must include flag column **K** with the **literal** `1` (or a fill that evaluates to 1) — not a criteria summary elsewhere, not `1.0` parked in col G, not a formula that husks (Err:508/509).
- After copy, verify column count covers **J/K** (Mistral Small stopped at A–H). Optional self-check: before finishing a “mark sample rows” task, verify K has enough literal `1`s (Muse Glimmer: 81 rows, K empty).

**Why:** Sample rows without K=`1` is the dominant reason S=0 while the sheet “looks done.” This is ordinary instruction-following on write tools, reusable beyond AFC.

### L5 — Prefer ordinary formulas / value writes over `=PY` for small SSC arithmetic

**Hits:** C4 (≥3) and parts of C1 (Qwen 27b `=PY` on Population).

**DO:**
- Keep fill-down + anti-husk teachings on `write_formula_range` (already moving with Tips A/B elsewhere).
- One colocated reminder: sample-size **final n** and simple ratios → ordinary Calc or a plain value write; reserve `=PY` for reductions that need Python, into **one** cell outside the data.

**Why:** Err:508/#NAME?/#VALUE! and malformed `=PY("""` turn SSC into unreadable R and husk Sample. Same lever that helps string-Calc pin/husk regressions.

### L6 — Soft pre-finish checklist (prompt, not Ready FSM)

**Hits:** C1–C3 compound Ready-with-empty.

**DO:**
- A short general Calc confirmation habit (already partially in `CALC_WORKFLOW` step 3): before the final “done”, name the deliverable sheets and whether they have data (e.g. “Sample has N rows; SSC shows Sample size = R”).
- **Do not** gate interactive Ready on AFC sheet predicates.

**Why:** Many runs claim Ready while Sample is empty or R is missing. A confirmation habit is product UX; Ready remains loop-local.

### L7 — Stall / large-read recovery (plumbing)

**Hits:** C8.

**DO:**
- Separate workstream: timeout visibility, preserve debug logs on API timeout, steer `read_cell_range` truncation toward fill-down / small peeks (first-principles §2.1).
- Not a per-model tip.

**Why:** DeepSeek V4.1 / Granite / GLM failed before workbook shape could recover. Fixing stalls helps every headed task, not only AFC.

### L8 — S≥R once R is known (selection size)

**Hits:** C6 (gemma-26b clear; others latent once L3 lands).

**DO:**
- Light tip when a required sample size R is known: ensure flagged Sample rows ≥ R (grow selection or copy more rows + flags) before finishing.
- Keep oracle `S≥R` as-is.

**Why:** First catalog parseable R still failed because Sample stopped at 10 rows. Matters more after L3 makes R visible.

---

## 2b. 2026-09-12 review addendum — minimal fix set

**Short answer.** Do **three** things and treat the rest as one-line polish:

1. **L9 — selection as one row-wise formula + fill-down.** Attacks the `S=0` half of C3, the largest cluster (≥10 models), using shipped fill-down.
2. **L11 — machine-readable answer cell.** Attacks the `R=None` half of C3 (≥6 models that compute ~65–68 and still score `R=None`).
3. **L10 — finish-time deliverable verification.** Attacks C1/C2/C6 (missing/empty Sample tabs), the other ≥9-model mass.

L2/L4/L5/L12/L13 are single-line sharpenings — land them alongside because they cost almost nothing. L6/L7/L8 are supporting or parallel-track. Chasing C8 stalls, C9 infra, or single-model quirks is out of scope here.

**Correction to §2:** block copy is the `source` argument on `write_formula_range`; there is **no** `copy_range` tool. Any plan that waits on a new copy tool is blocked on a tool we are not building.

### Already in the tree (verify wording, don't re-spec)

| Item | State at tip (`2bd13bf4` era) |
|---|---|
| L1 | `create_sheet` description + ok payload already say “no cells copied”; `CALC_WORKFLOW` step 2 repeats it. |
| L5 (partial) | Truncated-read message already steers fill-down first, `=PY` peek second; `write_formula_range` already has `source` + relative-ref fill-down. |
| L3 / L4 / L6 / L8 | Open — no label convention, no finish check, no S≥R coupling. |

### Shipped 2026-09-12 (this review)

| Lever | Landed where |
|---|---|
| L9 | `CALC_WORKFLOW` step 2 `SELECT:` line + colocated `WriteCellRange.parameters.values` sentence: one `=IF(OR(<criterion>; …);1;0)` in the named flag column + fill-down, then `COUNTIF` (in a scratch cell). Also writes literal `1`s, so it covers the teaching half of L4. |
| L11 | `CALC_WORKFLOW` step 2 `ANSWER:` line + the same `values` sentence: computed result in its own cell with a plain label **naming the quantity in the user's words** (e.g. `Sample size` — no "rounded up"/"minimum"); ordinary formula/value, not `=PY`. Bare `R` stays in this doc / oracle notes only. |
| L10 | `CALC_WORKFLOW` step 3: `get_sheet_summary` each named deliverable sheet before finishing. Uses the **core** summary tool, not `list_sheets` (that is `sheets`-domain specialized). |
| L13 | The step-2 copy line now says the copy must cover every column the user named (flags/variance). |
| L12 | `CreateSheet.parameters.sheet` now carries the exact-title contract: preserve spaces, no snake_case / invented aliases. |

Tests: `tests/framework/test_constants.py::test_calc_workflow_teaches_selection_answer_and_deliverable_verify`, `tests/calc/test_cells.py::test_write_formula_range_values_teaches_fill_down_not_json_pin`, and `tests/calc/test_sheets.py::test_create_sheet_description_and_ok_mentions_no_cells_copied`. Next: retest the slice (Grok empty-Sample, Luna/Qwen-Flash R-hole, Muse-Glimmer K-empty, Nemotron missing sheets).

### L9 — Selection is one row-wise boolean formula + fill-down

**Hits:** C3 `S=0` (≥6 models) and every “select rows matching a rule” request.

**DO:** Teach (on the `write_formula_range` values line and/or `CALC_WORKFLOW` step 2):
- When the user lists criteria, do **not** hand-pick rows. Put **one** `=IF(OR(<crit1>;<crit2>;…);1;0)` in the first data cell of the column the user named for flags (AFC: K) and fill-down over the table.
- Each criterion is a cell comparison on the named columns, e.g. `E2="Willett Bank Rome"`, `F2="Terrorist Financing breaches"`, `OR(G2=0;H2=0)`. Guard the variance ratio (`H2=0`) and wrap it in `IFERROR(...)`.
- Then confirm the count before finishing: `COUNTIF(K2:K1518;1)` (or `get_sheet_summary` on the Sample sheet) ≥ the required sample size.

**Why:** every observed `S=0` shape is “the marks did not land in the flag column” — K empty, `1.0` parked in G, or the copy stopped at A–H. A single row-wise OR formula (a) makes “each selected row satisfies ≥1 criterion” true by construction, (b) writes literal numeric `1`s the oracle/user can count, (c) is ordinary Calc reused by the general select-rows task. **Caveat:** the OR can flag a large share of rows; that is oracle-legal (Mercury passed with a near-full dump) and users can tighten it, so don’t cap it.

### L10 — Finish-time deliverable verification

**Hits:** C1 + C2 + C6 (≥9 compound).

**DO:** Extend `CALC_WORKFLOW` step 3 only: when the request names deliverable sheets, `get_sheet_summary` each named sheet and confirm it exists, is non-empty (`row_count` > header), and shows the named result cell; create/populate before finishing if not. Use the **core** summary tool only — do **not** call `list_sheets` (`sheets`-domain specialized).
- Do **not** gate interactive Ready (unchanged invariant). This is a tool-using habit, not an FSM predicate.

**Why:** “tab exists but empty” and “never created the named tab” are the two most common Ready-looking fails; one cheap `get_sheet_summary` surfaces both. General to every multi-sheet Calc task.

### L11 — Machine-readable answer cell for computed results

**Hits:** C3 R-hole (≥6 models) + C4.

**DO:** One line near the sample-size/answer write: put the final number in **its own cell** with a plain label in the **adjacent** cell (`Sample size` | `68`), computed with an ordinary Calc formula (e.g. `=ROUNDUP(...;0)`) or a plain value — not `=PY`. Do not put a label + qualifier + number in one string (“Required sample size (rounded up): 66”), and do not merge the cells.

**Why:** Luna / Qwen Flash / Muse Glimmer / Granite all landed ~65–68 and scored `R=None` on label shape alone. Adjacent label/value + a normal formula is recalc-safe, standard sheet design and generalizes to “show your workings.” Keep `_R_LABEL_RE` unchanged.

### L12 — Exact-title contract on `create_sheet`

**Hits:** C5 (cheap; also prevents false “I made the tab”).

**DO:** Extend `CreateSheet.parameters.sheet` (today just “New sheet name”): “Use the exact title from the request, spaces included; do not snake_case or invent aliases (`Sample Size Calculation`, not `Sample_Size_Calculation` or `Analysis`).” The ok message already echoes the created name.

**Why:** Laguna XS’s underscore made the tab read as missing; MiniMax `Analysis` is the same hygiene miss. Fully general naming discipline.

### L13 — Copy width must cover the named columns (folds into L4)

**Hits:** C3 `S=0`.

**DO:** Sharpen the existing `source`-copy sentence: “Copy the columns the user named — if variance/flags live in J/K, the copy range must reach K; if the source is narrower, add J/K to the destination after copying.”

**Why:** Mistral Small copied A–H only and scored `S=0` on a non-empty Sample; same shape as Muse Glimmer’s empty K.

### Revised cut order (supersedes §4)

1. **L9 + L13** — selection formula + copy width (`S=0` half of C3).
2. **L11** — machine-readable R (`R=None` half of C3).
3. **L10** — finish verification (C1/C2/C6).
4. **L12 + L2 remainder + L4** — one-line sharpenings (C5, flag column).
5. **L5 + L6 + L8** — supporting lines once the above land.
6. **L7** — stall plumbing, parallel track.

**Retest slice:** Grok / Luna / Muse Glimmer / Nemotron Super on tip `969d4776` — results in **§2c** (0/4 HAPPY; only Grok R parse clear). Not a full 24-model FIFO.

### Still don’t chase

- Single-model shapes (Laguna underscore, Seed `=PY("""`, Solar infra) — general hygiene covers the first two; infra is out of scope.
- Oracle regex / fuzzy sheet-name matching — keeps everyone honest; rejected as eval softening.
- Full selection *quality* (all-criteria coverage, representative size) — track as a secondary product metric; the oracle is intentionally soft (Mercury).

---

## 2c. PR #750 deep-dive — what shipped, why, what the retest did

**Date:** 2026-09-12 (living note on branch `eval-2/afc-minimal-fixes`; keep #750 draft/unready while this evolves)  
**Product tip:** `969d4776` (ANSWER soften) on top of `305bac07` (named quantity + L12) / `9d1d96b1` (no-qualifier + L10 doc) / `2be8c4ed` (SELECT / ANSWER / verify).  
**Bar:** honest headed evidence only — no invented HAPPY. None of the four retest cells are HAPPY.

### 1. What we implemented (product tips on this PR)

| Lever | Where | Teaching (tip wording at `969d4776`) |
|---|---|---|
| **L9 SELECT** | `CALC_WORKFLOW` step 2 + `WriteCellRange.parameters.values` | One `=IF(OR(<criterion>; …);1;0)` in the **named flag column** first data cell, fill-down; then `COUNTIF` **in a scratch cell** (do not overwrite flags). Do not hand-pick rows or park the mark elsewhere. |
| **L11 ANSWER** | same | Computed result in its **own cell**; plain label **naming the quantity in the user’s words** in the adjacent cell (e.g. `Sample size` — **no** “rounded up” / “minimum”); ordinary formula/value, **not** `=PY`. Bare `R` deliberately **dropped** from the product tip (oracle/`R = N` notes stay in this doc only). |
| **L10 verify** | `CALC_WORKFLOW` step 3 | Before finish: `get_sheet_summary` each **named deliverable** sheet; confirm exists + has data; create/populate first if not. Core tool only — **not** specialized `list_sheets`. No Ready FSM gate. |
| **L13 copy-width** | `CALC_WORKFLOW` step 2 create/populate line | Copy via `write_formula_range` `source` must cover every column the user named (flags/variance). (There is **no** separate `copy_range` tool.) |
| **L12 exact-title** | `CreateSheet.parameters.sheet` | Exact title the user asked for; preserve spaces; no snake_case / invented aliases. Tool remains **specialized** `sheets` (outer may not see this tip until create is delegated). |

Tests lock the workflow / values / create_sheet strings (`test_calc_workflow_teaches_selection_answer_and_deliverable_verify`, `test_write_formula_range_values_teaches_fill_down_not_json_pin`, `test_create_sheet_description_and_ok_mentions_no_cells_copied`).

**Not in this PR tip (related in-flight):** [#751](https://github.com/KeithCu/writeragent/pull/751) outer `instruction` after specialized sheets create (web-research pattern) for create≠populate — may help when Sample is never populated because create happened in a nested hop the outer never finishes. Do not treat #751 as shipped on #750.

### 2. Why (map to catalog clusters)

| Teaching | Cluster attacked | Intent |
|---|---|---|
| L9 SELECT | C3 `S=0` (K empty / wrong column / husked flags) | Row-wise OR + fill-down writes literal `1`s where the user named flags; COUNTIF confirms count without clobbering K. |
| L11 ANSWER | C3 `R=None` + C4 | Adjacent label/value + ordinary Calc so a computed sample size is machine-readable; ban qualifier burial (`Rounded up sample size: 68`). Soften after review: user’s words, not oracle magic strings / bare `R`. |
| L10 verify | C1 / C2 / C6 | Catch empty or missing named deliverable tabs before “done” — habit, not Ready gate. |
| L13 | C3 width (Mistral A–H) | Sample that stops before J/K cannot carry flags. |
| L12 | C5 | Underscore / invented titles (`Sample_Size_Calculation`, `Analysis`) read as missing. |

No oracle softens; no interactive Ready predicates. Pass/fail stays document-local.

### 3. What worked / what didn’t — headed retest on tip `969d4776`

Slice stamps under `docs/eval/eval-2/afc-sample-83d10b06/runs/` (parent-driven #750 retest, not catalog FIFO). **0 / 4 HAPPY.**

| Model | Stamp | vs prior catalog | What moved | What didn’t |
|---|---|---|---|---|
| **Grok 4.6** | `20260912-1603-pr750-grok-4.6` | Prior `0342`: empty Sample + R=None (2 fails) | **ANSWER partial win:** SSC has parseable **R=65** with plain `Sample size`\|65 | **Sample sheet still missing** (only failure left). Exhausted rounds / final stream NetworkError. L10 did not force Sample into existence before finish. |
| **GPT-5.6 Luna** | `20260912-1624-pr750-gpt-5.6-luna` | Prior `0150`: S=68 R=None | Sample grew (S=199, ~200 rows) | **ANSWER tip did not stick:** UI still showed **`Required sample size (rounded up)`\|68** → oracle **R=None**. Same Luna near-miss shape the tip was written to kill. |
| **Muse Glimmer 30B** | `20260912-1637-pr750-muse-glimmer-30b` | Prior `0353`: Sample 81 rows, S=0 R=None | Population J/K looked better (SELECT visual) | **Sample sheet regression** — Sample **missing**; invented **Audit Summary**; R=None. Worse workbook shape than prior on the deliverable tab. L12 did not stop the alias tab. |
| **Nemotron 3 Super** | `20260912-1646-pr750-nemotron-3-super` | Same-day catalog `1513`: Sample 1516 + rounded-up 68 / R=None | Sample more selective (81 rows vs near-full dump) | **R still None**; Sample J/K blank on inspection; SSC blankish. R-hole unresolved; SELECT marks did not clearly land on Sample. |

**Clear product win (only one):** Grok’s labeled **R=65**. Everything else is mixed or flat.

**Plain failures (say it):**

1. **ANSWER is not reliable across models.** Grok obeyed; Luna repeated the exact “rounded up” burial the tip forbids. Softening to “user’s words (e.g. Sample size)” was the right anti-oracle move, but it did **not** fix Luna on this tip. Do not claim L11 “solved” C3 R-hole.
2. **L10 verify did not save Sample-missing.** Grok and Muse finished (or errored out) without a Sample tab. A workflow sentence is weaker than create→populate actuation when rounds exhaust or the model invents `Audit Summary` instead.
3. **SELECT did not translate to oracle S.** Muse’s K work stayed on Population while Sample vanished; Super’s Sample lacked J/K. L9 alone does not guarantee flags on the **deliverable** sheet.
4. **No HAPPY cell on the slice.** Do not merge #750 on “teachings look good in code” — retest says otherwise.

### 4. Separate stamps (not #750 tip — do not conflate)

| Stamp | Model | Role |
|---|---|---|
| `20260912-1506-nemotron-3-ultra-550b` | Ultra 550B | Pre-#750 / master catalog land — Population-only NOT_HAPPY (Scrolly first-class JSON/SVG track). |
| `20260912-1513-nemotron-3-super-120b` | Super 120B | Pre-#750 catalog baseline — near-full Sample + rounded-up / R=None. Retest `1646` is the #750 comparison, not the catalog cell. |

### 5. What to iterate next (doc advice — no new tip code in this commit)

Ranked by what the slice actually blocked:

1. **Sample presence / create→populate** — biggest remaining hole (Grok, Muse). Watch **#751** outer instruction after specialized create; if that lands, re-run Grok + Muse before stacking more ANSWER wording.
2. **ANSWER adherence for Luna-class models** — tip text alone failed once. Prefer a sharper colocated example or a write-path result echo (“wrote label `Sample size` + value”) over expanding `_R_LABEL_RE`. Still no oracle softens.
3. **SELECT on the deliverable sheet** — teach (when iterating code) that flags must land on the sheet the user named as Sample, not only Population; L13 copy-width is necessary but wasn’t enough when Sample was never created or J/K stayed blank.
4. **Keep #750 draft** until a retest shows ≥1 slice HAPPY or a clear multi-model R recovery beyond Grok.

**Retest order when tips change again:** Grok (Sample missing + R regression check), Luna (rounded-up label), Muse (Sample missing / alias tabs), Super (R + Sample J/K). Optional: Qwen Flash / Ultra — not required for the primary slice.

---

## 3. Explicitly out of scope

| Item | Why |
|---|---|
| Single-model tips (e.g. only Laguna underscore, only Seed `=PY("""`) | Not multi-model blast radius; fold into general L2/L5 if useful |
| Softening oracle R regex / sheet-name fuzzy match | Eval gaming; do **not** expand `_R_LABEL_RE` to accept “rounded up” / “minimum” paraphrases or same-cell `Sample Size: 68`; prefer teaching exact titles + labels |
| Interactive Ready gates on Sample/SSC | Rejected in first-principles; pass/fail stays document-local |
| Task-local “AFC checklist” essay in the user prompt only | Prefer colocated tool tips; task prompt already names sheets |
| Ban near-full Sample dumps | Would punish Mercury’s HAPPY path; observer taste ≠ oracle |
| Solar Pro 4 `message_store` / wiped history.db | Infra BLOCKED — harness wipe, not model pain |
| Fixture-specific one-liners / gold letter shifts | Wrong deliverable family (gold vs in-workbook) |

---

## 4. Suggested cut order (for a later product PR — not this doc)

**Superseded by §2b’s revised cut order** (L9–L13). Kept below as the original ranking.

1. **L1 + L2** — create/populate + exact titles (C1/C2/C5).  
2. **L3 + L4** — labeled R + literal K flags (C3 bulk).  
3. **L5** — anti-husk / ordinary formulas on SSC (C4).  
4. **L6** — soft confirmation habit.  
5. **L7** — stall plumbing (parallel track).  
6. **L8** — S≥R once R parse rates rise.

Retest bar: re-run a **slice** of C1–C3 models (e.g. Grok empty-Sample, Luna R-hole, Nemotron missing sheets, gemma-31b SSC errors) — not a full 24-model FIFO — after each cut. Prefer product HAPPY + oracle PASS; don’t chase C²/$ with cheats.

---

## 5. Appendix — catalog quick map

| Cluster | Models (abbrev) |
|---|---|
| C1 missing sheets | Nemotron, Qwen-27b, GLM, MiniMax, DS-V4.1 |
| C2 empty Sample | Grok 4.6, Muse Spark, Granite, DS-V4 Flash (+ Laguna XS) |
| C3 R hole / S=0 | oss-120b, Luna, oss-20b, Flash Lite, Gemma 31B, Muse Glimmer, Mistral Small, Qwen Flash, … |
| C4 SSC husks | Gemma 31B, Qwen Flash, Seed mini |
| C5 rename | Laguna XS; MiniMax `Analysis`; oss-120b extras |
| C6 undersized | Gemma 26B |
| C7 dump | oss-120b (FAIL), Mercury (PASS) |
| C8 stall | DS-V4.1, GLM, Granite, Laguna S |
| C9 infra | Solar Pro 4 BLOCKED |
| HAPPY | Gemini 3.8 Flash (gate), Mercury 2.5 Preview (catalog) |
