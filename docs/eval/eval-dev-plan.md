# WriterAgent: Evaluation System Development Plan (Internal Edition)

**Current string-harness work** lives in
[`string-harness-upgrade.md`](string-harness-upgrade.md) (core schemas,
inner specialized `LlmClient` loop, document worlds, process/`=PY` score;
no LO ranking). This file is the older hybrid/LO roadmap. Phase F
`=PY` dest rows (`py_refuse_overlap`, `py_no_bulk_read`) and DrawWorld
(flowchart tree + `shape_connect`) are **shipped** in the 17-task pack.

This plan covers the WriterAgent prompt optimization + evaluation system (`scripts/prompt_optimization/`). Ranking is `--backend string` only (17 tasks). Specialized Draw/Calc work uses a bounded inner `LlmClient` loop (`delegate_to_specialized_*` → domain schemas → `specialized_workflow_finished`), not SmolAgents. See `ideas.md` for the original ~50 ideas; the shipped pack is the 17 in `dataset.py`.

## Current Status

The evaluation system lives in `scripts/prompt_optimization/`:
- `run_eval.py` / `run_eval_multi.py`: Main entrypoints (`LlmClient` + tool loop from `llm_chat_eval.py`). Eval does not set sampling temperature (LlmClient / provider default). `run_eval_multi.py` **refuses** a full catalog sweep unless `--models` or `--yes-all-models` is set. `--gold-model` runs only with `--generate-golds`.
- Default: `--backend string` (Writer/Draw/Calc worlds in `eval_worlds.py` via `string_eval_tools.py`). Core schemas from `ToolRegistry.get_schemas`. Flowchart uses `delegate_to_specialized_draw_toolset(domain="shapes")`; sort uses `delegate_to_specialized_calc_toolset(domain="ranges")` then one-column `sort_range` (two stable passes for Product then Revenue).
- `--backend lo`: Headless UNO via `tools_lo.py` (fidelity smoke, not ranking).
- Judging: Hard gate is substring + **result oracles** + **process oracles**. Quality LLM-as-judge runs **after** the hard gate for resume, rewriting, summarization, and the two table tasks. Creative weights are accuracy-first (50/20/30); tables are formatting-heavy after the gate (20/80). Unparseable judge JSON retries once, then keeps the hard pass (`judge_score=None`). Rank by hard pass / agent score / quality; C²/$ is secondary.
- Dataset: 17 tasks in `dataset.py` `ALL_EXAMPLES`. `gold_standards.json` is hand-written from the rubrics.
- `--student scripted` (`scripted_student.py`): no API key; pass is `example_passed` (substring + oracles + process). `-j` is threads. Do not use `tests/eval_runner.py`. Do not set `WRITERAGENT_TESTING=1` for LO eval.
- CI / pytest: `tests/scripts/test_eval_oracles.py` and `test_scripted_eval_pack.py` replay `--backend string --student scripted` (no OpenRouter). Prompt-text pins live in `tests/scripts/test_eval_prompts.py`. Headless `--backend lo --student scripted` is `@pytest.mark.integration`; local: `python scripts/prompt_optimization/run_eval.py --backend lo --student scripted --no-bust-cache -v`.

The 50 test cases live in [`ideas.md`](ideas.md) (20 Writer, 20 Calc, 5 Draw, 5 Multimodal; categorized by level with modes for judging).

---

## Hybrid Evaluation Strategy for Draw, Flowcharts & Images (New)

`DrawWorld` in `eval_worlds.py` shipped the tree/`shape_connect` path (no separate `--backend drawjson`). Remaining gaps: `image_generate`, vision/multimodal, and LO geometry/z-order. **Screenshots are not needed**.

**Recommended path (non-LO first)**:
- **DrawWorld** (shipped; this section used to call it DrawJSONBackend): Maintains a mutable JSON tree. Mock `get_draw_tree`, `shape_upsert` (flowchart-*, connectors), `shape_connect`, `shape_group`, `shape_summary`. `dispatch_string_tool` extended for Draw tools. Final state for judging = serialized tree JSON (structural diff on nodes, connections, text, geometry with tolerances) or LLM-as-Judge on tree.
- `plugin/draw/tree.py:GetDrawTree` is the perfect "DOM" — recursive JSON with `type`, `text`, `geometry`, `connected_start`/`connected_end` (by name/text), `children` for groups. Its description explicitly says "Use this instead of requesting a screenshot to understand the layout, text, connections, and hierarchy of objects (like flowcharts or diagrams)."
- For `image_generate` (`plugin/writer/images.py`, `plugin/writer/image_utils.py`): Mock `ImageService.image_generate` to return fixed temp path; state adds an "image" node to tree or HTML sentinel. Judge on tool result JSON (`status: "ok"`) + presence in final tree.
- Verification: Extend `eval_core.py` for tree-based `expected_contains` (node paths) or JSON-aware judge. No pixel comparison.

**LO transition**: Use `--backend lo` with Draw doc (`private:factory/sdraw`) + real tools for fidelity tests (real insertion, styles, z-order, rendering). See `tests/draw/test_draw_uno.py` for patterns (`_exec_tool`, assertions on JSON + UNO counts/positions). `get_draw_context_for_chat` in `plugin/draw/bridge.py` provides lighter text summary.

**When to require LO** (analysis of [`ideas.md`](ideas.md)):
- **String/DrawJSON sufficient** (~40%): Pure text cleanup, logical rewriting, basic table engineering (HTML), bullet consistency, format preservation, simple shape creation (via tree mutation). Flowchart Gen (#3 in Draw) is ideal for tree-based eval (check connections, node types/text).
- **Requires LO or advanced mock for fidelity** (most Calc, many Writer structural, all Draw/Multimodal):
  - Writer: Styles, comments, track changes, TOC, headers/footers, section breaks, style mapping, bibliography (UNO-specific).
  - Calc: Formulas, conditional formatting, pivot tables, charts, multi-sheet ops (20/20 tests).
  - Draw (5/5): Z-order, grouping, precise layout/alignment, scaling — tree JSON handles most; full LO for geometry/rendering edge cases.
  - Multimodal (5/5): Vision (OCR, captioning, spatial audit on images/diagrams) — needs `image_generate` + insertion or real image fixtures (`multimodal_vision.odt`).
- **Recommendation**: DrawWorld covers Draw/flowchart ranking without screenshots. Use `--backend lo` for Calc/Writer fidelity smoke and as a gold standard for UNO-only features. This avoids making all evals "harder" while enabling image/tool-calling evals via metadata/tree. Aligns with AGENTS.md testing policy (unit tests for mocks, UNO tests for real document interaction).

See previous analysis for architecture diagram (StringBackend → DrawJSONBackend → LOBackend; judge on final tree/HTML).

---

## Updated Phase 2: Roadmap & Next Steps

### A. Expand Test Suite (Completed hardening)
- Hardened key tests in [`scripts/prompt_optimization/dataset.py`](scripts/prompt_optimization/dataset.py) (BULK_CLEANUP, REFORMAT_RESUME, LOGICAL_REWRITING, TABLE_ENGINEERING, BULLET_CONSISTENCY, TAX_COLUMN, STYLE_CONSISTENCY, COMMENT_MANAGEMENT) with stricter instructions, edge cases, precise rubrics referencing judge weights/gold, expanded contains/rejects, tool hints (per plan). TABLE_FROM_MESS and structural Draw/Calc kept as baseline. No new full tests added ("don't go crazy").
- Ported/updated from [`ideas.md`](ideas.md).
- Categorize by LO requirement (see above). Update `AGENTS.md` after changes.

### B. Multimodal & Image Evaluation
- Mock `image_generate` + tree/image node in state.
- Fixtures: `tests/fixtures/multimodal_vision.odt`, image assets.
- Judge on inserted image metadata + caption accuracy.

### C. Test Fixtures
- Expand with Draw-specific tree golds in `gold_standards.json`.
- `long_summarization.odt`, `complex_calc.ods`.

### D. Advanced Reporting & CI
- Integrate with `run_eval_multi.py` (already supports multi-model IpD).
- ~~Add `--backend drawjson` flag.~~ DrawWorld is the string-backend Draw tree; no extra flag.
- UNO tests for Draw eval path (`tests/draw/`).

### E. LO Transition Strategy
- Keep `--backend string` (WriterWorld / DrawWorld / CalcWorld) as primary for speed/CI.
- LO for validation of specialized tools (`ToolWriterSpecialBase`, `ToolDrawSpecialBase`, `get_draw_tree`).
- Update `AGENTS.md` prompt optimization section with hybrid guidance.

### F. Calc `=PY()` placement (shipped in the 17-task pack)

**Hypothesis:** a few limitation words on main chat beat a second specialized domain. Dest / spill / peek live on `write_formula_range` (`plugin/calc/cells.py`); MIPROv2 can later rewrite that description plus the remaining `CALC_FORMULA_SYNTAX` / pointer in `CALC_CORE_DIRECTIVES` (`plugin/framework/prompts.py`).

Calc chat no longer delegates `domain="python"`; models must `write_formula_range` of `=PY("result = …"; DataRange)` into an **empty cell outside DataRange**. Rows in `dataset.py`:

| id | Ask | Pass | Fail | Status |
|----|-----|------|------|--------|
| refuse overlap | put the formula in **H1**, data A1:H500 | dest J1/I1 and says H1 is inside the range | writes H1 | **shipped** (`py_refuse_overlap`) |
| no bulk read | unique-rows via `=PY` | no `read_cell_range` of A1:H500 / the spill | dumping the block into chat | **shipped** (`py_no_bulk_read`) |
| unique beside | drop dupes on A1:H500 onto the sheet | `=PY` dest **J1** (or first empty col / other sheet) | dest inside A1:H500; `domain=python`; chat-only | dropped (pack stays even) |
| in-place reframe | write unique rows **back onto** A1:H500 | same as unique beside + short circular explanation | `=PY` in A1 | dropped |

Scoring: dest vs parsed data range on `--backend string` (`CalcWorld` records dest + formula). LO later for spill. Next ranking run is live `--backend string` (do not regenerate golds first). Optimize output if needed: `optimized_calc_py_prompt.json`.

### G. Calc sort/tax prompt lift (current — PRs 616 / 617 / 618)

Shared Calc prompts and the tax oracle are better after this sequence.
Ranking dropped full `glm-5.3` and selectively remeasured sort+tax.
Production still ships **one** Calc prompt for all models — there is no
20b-only fork. The full 17-task pack was **not** re-run.

Source plan: [`oss-20b-eval.md`](oss-20b-eval.md). Scores:
[`benchmarks.md`](benchmarks.md).

#### What is true now

- Sort goes specialized Calc `domain="ranges"` then `sort_range`, not
  in-place `=PY`. `sort_range` **requires** `has_header`.
- `CALC_CORE_DIRECTIVES` is structured: `FORMULAS:` (per-row relative
  Do-because) then `SORT:` last (routing + plain `has_header`
  constraint). The sort because-clause does **not** name
  `write_formula_range` / `=PY`.
- Tax oracle `_tax_formula_ok` accepts equivalent relative 8% forms
  (`B2*0.08`, `0.08*B2`, `B2*8%`, `8/100`, `$` / spaces / decimal comma)
  without loosening wrong-row, wrong-factor, or Price-column junk.
- `write_formula_range` fails loud when a JSON leaf count does not match
  the A1 cell count (string `CalcWorld` too).
- Ranking catalog: `z-ai/glm-5.3-flash` only.

#### What shipped

| PR | What landed |
|----|-------------|
| [610](https://github.com/KeithCu/writeragent/pull/610) | Sort routing + relative tax formulas + required `has_header` + Do-because lines (shared prompt). |
| [616](https://github.com/KeithCu/writeragent/pull/616) | `_tax_formula_ok` equivalents; sort Do-line because-clause no longer names `write_formula_range` / `=PY`. |
| [617](https://github.com/KeithCu/writeragent/pull/617) | `FORMULAS:` / `SORT:` headers; `has_header` as a plain constraint; SORT routing last; `write_formula_range` fails on length mismatch. |
| [618](https://github.com/KeithCu/writeragent/pull/618) | Dropped `z-ai/glm-5.3` from the ranking catalog (keep flash). Selective re-run of **only** `data_sorting` + `tax_column`; Pareto + [`benchmarks.md`](benchmarks.md) refreshed. |

**Method.** k=3 factorial on 20b+120b locked the 610 cell (`both` =
prompt + schema). Factorial sensitivity is not a single-shot catalog
`hard_pass` bit. Later scores are selective merges ([613](https://github.com/KeithCu/writeragent/pull/613)
then 618) via `merge_benchmark_results.py`.

#### Latest selective help/hurt vs prior (613) snapshot

Per-model **hard_pass** on the two Calc tasks after 616/617 vs the
[PR 613](https://github.com/KeithCu/writeragent/pull/613) snapshot.
Remaining catalog only.

**`data_sorting`**

| Outcome | Models |
|---------|--------|
| HELP (F→P) | `z-ai/glm-5.3-flash`, `upstage/solar-pro4`, `bytedance-seed/seed-2.0-mini`, `google/gemini-3.5-flash-lite`, `google/gemma-4-26b-a4b-it`, `minimax/minimax-m3` |
| HURT (P→F) | **`openai/gpt-oss-20b`** |

**`tax_column`**

| Outcome | Models |
|---------|--------|
| HELP (F→P) | `qwen/qwen3.8-27b`, `upstage/solar-pro4`, `bytedance-seed/seed-2.0-mini`, `meta/muse-glimmer-30b`, `mistralai/mistral-small-2603`, `openai/gpt-5.6-luna`, `poolside/laguna-xs-2.1` |
| HURT (P→F) | **`openai/gpt-oss-20b`**, `inception/mercury-2.5-preview` |

Two of the old 610/613 tax “hurts” (`qwen3.8-27b`, `glm-5.3`) were
oracle FNs — fixed by 616. `solar-pro4` tax helped after the 617
length-harden path. `glm-5.3-flash` sort helped after the 616
because-clause + 617 structure.

#### Next watches

1. Do forensic `gpt-oss-20b` sort+tax fails from the 618 details
   **before** more shared-prompt churn — it helped under 610 then hurt
   under 616/617; gate models matter.
2. Do k≥3 factorial on 20b+120b before the next Calc directive edit
   (single-shot catalog bits are noisy).
3. Do selective-only remeasure (sort+tax) after prompt tweaks; merge
   into the snapshot (`merge_benchmark_results.py`); avoid a full
   17-task pack until spend allows.
4. Do keep the catalog without full `glm-5.3`; flash only.
5. Do watch `mercury-2.5-preview` tax hurt and incomplete/429 models
   (`laguna-xs` sort; carried `qwen3.8-flash` / `nemotron-3.5-lightning`
   rows).
6. Optional: does required `has_header` still earn keep? Re-check only
   after 20b routing is understood.

Cross-links: [`oss-20b-eval.md`](oss-20b-eval.md),
[`benchmarks.md`](benchmarks.md),
[PR 610](https://github.com/KeithCu/writeragent/pull/610),
[PR 616](https://github.com/KeithCu/writeragent/pull/616),
[PR 617](https://github.com/KeithCu/writeragent/pull/617),
[PR 618](https://github.com/KeithCu/writeragent/pull/618).

---
*Updated Dev Plan v2.4 — Phase G current state (Sep 2026; PRs 610 / 616 / 617 / 618)*
