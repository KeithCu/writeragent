# Why Nemotron Super beat Ultra on eval-1 string-pack

**Date:** 2026-09-12  
**Run:** `docs/eval/string-pack-runs/nemotron3-ultra-super-20260912-1658.json` (+ `_details.json`)  
**Models:** `nvidia/nemotron-3-super-120b-a12b` vs `nvidia/nemotron-3-ultra-550b-a55b`  
**Scope:** Analysis only — no product tip / oracle changes from this note.

## Headline

| Metric | Super 120B | Ultra 550B |
|---|---:|---:|
| **Correctness** (avg) | **0.904** | **0.821** |
| Hard pass rate | 12/17 ≈ **0.706** | 14/17 ≈ **0.824** |
| Document pass rate | 0.765 | 0.824 |
| Total tokens (sum) | ~1.37M | ~0.95M |
| Catalog $/M in / out | $0.085 / $0.4 | $0.625 / $3.125 |

**Inversion:** Ultra wins **hard-pass count** (14 vs 12) but loses **average Correctness**. Super’s fewer hard passes are concentrated process/oracle fails on tasks that still score well on the document, while Ultra’s Correctness damage is a few near-zero Writer/table cells that Super nails.

Stored `total_cost_usd` in this run JSON is still 0 (Value column fix is a separate track) — use catalog rates + tokens for cost intuition, not invented USD.

---

## Per-task delta (Correctness)

17 tasks each. **10 ties** at Correctness 1.0 (or both 1.0 with hard-pass differences noted below). **3 Super wins**, **4 Ultra wins**.

### Super higher Correctness

| Task | Cat | Ultra C / HP | Super C / HP | ΔC | What happened |
|---|---|---|---|---:|---|
| `py_refuse_overlap` | structural | 0.00 / F | 1.00 / F | +1.00 | Both hit `max_tool_rounds`. Ultra: repeated process/oracle “formula does not reference the data range.” Super: process “dest H1 overlaps the data range” but **document still scored 1.0**. Same timeout class; Ultra’s final sheet state failed the oracle harder. |
| `smart_summarization` | creative | 0.00 / F | 0.90 / T | +0.90 | Ultra oracle: `summary missing '10k'` (scaling RPS left out of the executive bullets). Super kept the five required stats and hard-passed. |
| `table_engineering` | structural | 0.12 / T | 1.00 / T | +0.88 | Ultra hard-passed document/agent but **judge Correctness 0.12**: Total row put the sum in the **Quantity** column, left Price empty. Super: Total = Σ(Price×Quantity) in the right place; judge 1.0. |

### Ultra higher Correctness

| Task | Cat | Ultra C / HP | Super C / HP | ΔC | What happened |
|---|---|---|---|---:|---|
| `data_sorting` | structural | 1.00 / T | 0.00 / F | +1.00 | Ultra sorted cleanly. Super burned **~390k tokens**, `max_tool_rounds`, oracle `header row is not first` — sheet header destroyed (Aardvark / n/a / huge constants). Classic hand-rewrite / sort thrash; Tips B `sort_range` failure mode. |
| `section_refactor` | structural | 1.00 / T | 0.80 / F | +0.20 | Super missing expected substring `See the Goal` (rewrote to HTML + bold “Goal”). Ultra kept the phrase. |
| `reformat_resume` | creative | 0.90 / T | 0.76 / T | +0.14 | Both hard-pass; Ultra edged judge/quality. |
| `logical_rewriting` | creative | 0.94 / T | 0.90 / T | +0.04 | Noise-level. |

### Hard-pass disagreements (same Correctness or not)

| Task | Ultra HP | Super HP | Note |
|---|---|---|---|
| `bulk_cleanup` | T (C=1.0) | **F** (C=1.0) | Super process: **`domain=python is not allowed`** — Correctness still 1.0, hard-pass killed by specialized-python reach. |
| `data_sorting` | T | F | See above (Super meltdown). |
| `section_refactor` | T | F | Missing `See the Goal`. |
| `smart_summarization` | F | T | Ultra missed `10k`. |

Both fail hard-pass on `py_refuse_overlap` and `py_no_bulk_read` (Calc `=PY` / tool-round storms).

### Category averages (Correctness)

| Category | Ultra | Super | n |
|---|---:|---:|---:|
| creative | 0.613 | **0.853** | 3 |
| structural | 0.866 | **0.914** | 14 |

Super’s Correctness lead is mostly **creative + one bad Ultra table**, not a blanket structural win (and Ultra still owns the cleanest Calc sort).

---

## Hypotheses (grounded in traces)

1. **Ultra is sharper on Calc actuation when it stays in-tool, but bleeds Correctness on Writer “shape” tasks.**  
   `data_sorting` is the clearest Ultra win (Super self-destructs the sheet). Conversely Ultra’s `table_engineering` and `smart_summarization` misses are **layout / required-stat** failures with short token counts — not round exhaustion. Bigger model ≠ better total-row placement or “include 10k RPS in the five bullets.”

2. **Super’s Correctness edge is a few high-Δ Writer/table saves, not lower error rate everywhere.**  
   Three Super wins sum to a large Correctness lift (+1.0 / +0.90 / +0.88). Ultra’s four wins are one disaster recovery (`data_sorting`) plus small margins. That alone can invert avg Correctness while Ultra still hard-passes more cells.

3. **Hard-pass vs Correctness measure different things — and Super trips process gates.**  
   On `bulk_cleanup`, Super delivers a clean document (C=1.0) but calls **forbidden `domain=python`**, so agent/hard-pass fail. Ultra never takes that hop and hard-passes. Counting hard-passes favors Ultra; averaging Correctness favors Super.

4. **Both Nemotrons struggle on Calc `=PY` refuse / no-bulk-read tasks** (`py_refuse_overlap`, `py_no_bulk_read`): max tool rounds, formula-range / overlap process failures. Super sometimes still leaves a document the scorer likes (`py_refuse_overlap` C=1.0); Ultra’s leftover sheet fails the data-range oracle. Do **not** read that as Super “understanding” PY better — both timed out.

5. **Tokens ≠ quality.** Super used **~45% more** total tokens (~1.37M vs ~0.95M) and still lost hard-pass rate. Extra tokens show up as sort thrash (`data_sorting` ~390k) and long cleanup paths, not as a universal thoroughness win. Catalog price still favors Super per token ($0.085/$0.4 vs $0.625/$3.125 per M).

6. **No product cheat recommended from this pair alone.**  
   Failures map to known general levers already on the board (sort via `sort_range`, don’t invent headers; table totals in the named column; summary must include named figures; don’t delegate Writer cleanup to `domain=python`). Keep tips general — not Nemotron-specific.

---

## Bottom line

- **Correctness 0.904 vs 0.821:** Super wins because Ultra zeros / near-zeros a couple of Writer/table cells (`smart_summarization`, `table_engineering`) while Super’s big miss (`data_sorting`) is one cell Ultra cleans up — and Super still scores document 1.0 on a PY timeout Ultra fails.
- **Hard pass 14/17 vs 12/17:** Ultra wins because Super’s process miss (`domain=python` on `bulk_cleanup`) and sort meltdown / section substring drop cost hard-passes even when Correctness is high or close.
- **Practical read:** for this string-pack, **Super is the better Correctness/$ intuition** (cheaper catalog rates + higher avg C); **Ultra is the stricter “did the agent stay in policy / finish Calc sorts”** model. Neither replaces the other for eval-2 AFC (both NOT_HAPPY there on separate stamps).

## Sources

- `docs/eval/string-pack-runs/nemotron3-ultra-super-20260912-1658.json`
- `docs/eval/string-pack-runs/nemotron3-ultra-super-20260912-1658_details.json`
- `scripts/prompt_optimization/model_configs.py` (catalog $/M)
- Landed with [#753](https://github.com/KeithCu/writeragent/pull/753) string-pack + AFC catalog rows
