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
| gpt-5.6-luna | S=68 R=None; claimed ~66 on SSC |
| gpt-oss-20b | S=0 R=None; Sample ~80 rows |
| gemini-3.5-flash-lite | S=0 R=None; Sample 81 |
| gemma-4-31b-it | S=0 R=None; SSC Err:508/#NAME?; husks |
| muse-glimmer-30b | S=0 R=None; Sample 81 (B9 visually ~65) |
| mistral-small-2603 | S=0 R=None; Ready claiming Variance/Sample/SSC |
| qwen3.8-flash | S=68 R=None; SSC Err:508/509/#VALUE! |
| granite / grok / muse-spark / deepseek-v4-flash | often compound with C2 |

**Two distinct product holes inside this cluster:**

1. **R unparseable** — SSC has workings / a claimed n≈65–68, but no cell the oracle accepts. Oracle only accepts labeled values matching `Sample size` / `R` / inline `R = N` (`parse_required_sample_size` in `scripts/eval_2_ods_oracle.py`). “min sample 66”, “n=66”, “final required sample size ≈ 65” without that label shape → `R=None`.
2. **S=0 despite Sample rows** — column K is not literal flag `1` on enough rows (blank K, wrong column, formula husks, or selection never written).

### C4 — SSC formula / `=PY` husks

**Blast radius: medium (≥3 models)**

| Models | Errors seen |
|---|---|
| gemma-4-31b-it | Err:508 / #NAME? on SSC; husks 81/810 on Sample |
| qwen3.8-flash | Err:508 / 509 / #VALUE! on SSC; Scratch Err:513 |
| seed-2.0-mini | malformed `=PY("""` husk-dominated Sample (1/1); SSC Err:501 |

**Product read:** models reach for `=PY` / exotic formulas for sample-size arithmetic that ordinary Calc (or a single labeled value write) would handle. Same family as the fill-down / anti-husk teachings in first-principles §2.1–2.2.

### C5 — Wrong sheet naming (spaces vs underscores)

**Blast radius: low today (1 clear catalog hit) — still product clarity**

| Model | Note |
|---|---|
| Laguna XS 2.1 | Created `Sample_Size_Calculation` → oracle treats SSC **missing** (spaces required: `Sample Size Calculation`) |

**Flag:** not multi-model *yet*, but the failure mode is general (models invent underscore titles). Prefer teaching exact titles on `create_sheet` over softening the oracle.

### C6 — Undersized Sample vs parseable R

**Blast radius: low–medium (1–2 clear)**

| Model | Note |
|---|---|
| gemma-4-26b-a4b-it | First catalog parseable R=65; Sample only 10 rows; S=0 → `S < R` |
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
- Colocate on `copy_range` / `write_formula_range` (source+dest): one line — “empty tab is not the deliverable; copy or write the data block onto the new sheet.”
- Keep `CALC_WORKFLOW` create≠populate one-liner; don’t restack a grab-bag essay.

**Why:** Empty Sample with tabs present is still one of the most common Ready-looking fails. Teaching at call time beats restating step 4 in the task prompt alone.

### L2 — Exact sheet-title contract on `create_sheet`

**Hits:** C5 now; reduces C1 false “I created SSC” when titles drift.

**DO:**
- On `create_sheet` `sheet` parameter description: “Use the **exact** title the user asked for (preserve spaces; do not snake_case or invent aliases).”
- Optional: ok payload echoes the created name so a rename mistake is visible in the next turn.

**Why:** Laguna XS shows underscore aliases make the oracle (and a picky user) treat the tab as missing. Exact-title hygiene is general Calc hygiene, not an AFC cheat.

### L3 — Teach labeled final answers for “required sample size / R”

**Hits:** C3 R-hole (largest single fail mode across the catalog).

**DO (product-general, not oracle softens):**
- When the user asks for sample-size **workings**, a light Calc tip (workflow or `write_formula_range` values): put the **final integer** in a cell beside a clear label such as `Sample size` or `R` (or write `R = <n>` in one cell).
- Do **not** expand the oracle regex to accept every paraphrase (“n=”, “min sample”, “≈”) — that is eval softening. Fix the teaching so models emit the labels users/oracles already understand.

**Why:** Many models *compute* ~65–68 and still score `R=None`. The gap is labeling, not Cochran math. Gate + Mercury prove parseable R is achievable.

### L4 — Flag column = literal `1` (selection marks)

**Hits:** C3 S=0 with nonempty Sample (≥6 models).

**DO:**
- Colocate near write paths used for flags: when the user asks to mark selected rows with `"1"`, write the **literal** `1` (or a fill that evaluates to 1) into the named flag column — not a criteria summary elsewhere, not a formula that husks.
- Optional self-check one-liner in Calc workflow: before finishing a “mark sample rows” task, verify the flag column has enough `1`s.

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

## 3. Explicitly out of scope

| Item | Why |
|---|---|
| Single-model tips (e.g. only Laguna underscore, only Seed `=PY("""`) | Not multi-model blast radius; fold into general L2/L5 if useful |
| Softening oracle R regex / sheet-name fuzzy match | Eval gaming; prefer teaching exact titles + labels |
| Interactive Ready gates on Sample/SSC | Rejected in first-principles; pass/fail stays document-local |
| Task-local “AFC checklist” essay in the user prompt only | Prefer colocated tool tips; task prompt already names sheets |
| Ban near-full Sample dumps | Would punish Mercury’s HAPPY path; observer taste ≠ oracle |
| Solar Pro 4 `message_store` / wiped history.db | Infra BLOCKED — harness wipe, not model pain |
| Fixture-specific one-liners / gold letter shifts | Wrong deliverable family (gold vs in-workbook) |

---

## 4. Suggested cut order (for a later product PR — not this doc)

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
| C5 rename | Laguna XS |
| C6 undersized | Gemma 26B |
| C7 dump | oss-120b (FAIL), Mercury (PASS) |
| C8 stall | DS-V4.1, GLM, Granite, Laguna S |
| C9 infra | Solar Pro 4 BLOCKED |
| HAPPY | Gemini 3.8 Flash (gate), Mercury 2.5 Preview (catalog) |
