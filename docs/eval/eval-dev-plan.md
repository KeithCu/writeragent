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

### G. Calc sort/tax prompt lift (Sep 2026 — PR 610 / selective re-rank)

Retrospective for the shared Calc prompt work that came out of
[`oss-20b-eval.md`](oss-20b-eval.md). Keith may keep [PR 610](https://github.com/KeithCu/writeragent/pull/610)
despite some catalog regressions; this section is the durable record of what
shipped, what the factorial and selective re-measure actually showed, and
what to do next. Product prompts and ranking JSON are **not** edited here.

Source plan: [`oss-20b-eval.md`](oss-20b-eval.md) (gpt-oss-20b lift ideas from
the Sep 1 ranking). Production still ships **one** Calc prompt for all
models — there is no 20b-only fork.

#### Starting point (Sep 1 catalog)

17-task `--backend string` ranking in `benchmark_results*.json` (see
[`benchmarks.md`](benchmarks.md)). The 20b failures called out in
`oss-20b-eval.md` included:

| Task | What 20b did | Kind |
|------|--------------|------|
| `data_sorting` | In-place `=PY` over the range; header destroyed | tool routing |
| `tax_column` | Absolute `=B2*0.08` on every row instead of a relative per-row formula | per-row formulas |

The same source plan also suggested Draw flowchart routing (#3) and Writer
needle-token lines (#5). Those were **not** part of the 610 ship.

#### What shipped in PR 610 (Calc patches kept)

From the oss-20b-eval suggestions, Keith locked Calc patches **1+2+4** and
dropped Draw #3 and Writer needle #5:

1. **Sort routing.** Models should use specialized Calc `domain="ranges"` /
   `sort_range`, not in-place `=PY` for sort.
2. **Relative per-row formulas** for tax-style writes (Banana → `=B3*0.08`,
   not a copied `B2`).
4. **Tool-description** improvements for `sort_range` (and related).

Also after factorial learning:

- `sort_range` schema **always requires** `has_header`. Runtime already
  defaulted `True` when the arg was omitted; models often passed
  `has_header=false` **explicitly**, which bypassed that default.
- Calc directives split into short **Do Z because Y** lines (sort routing +
  `has_header`), not a mashed Don't+Do pair. Don't+Do was ~2× prompt; small
  models parse DO+why better.
- Shared prompt for all models. Nemotron-specific sort experiments stay
  parked until the 20b/120b story is settled (see
  [`nemotron-35-eval.md`](nemotron-35-eval.md)).

#### Factorial A/B (method)

- **k=3**, `data_sorting` only, `openai/gpt-oss-20b` and
  `openai/gpt-oss-120b` via OpenRouter `:nitro`.
- 8-variant grid on prompt/schema cells (routing / tool-desc /
  `has_header` required / DO lines / combinations), with instrumented
  `has_header` call logging.
- Locked winner cell for the shared ship: **`both`** (prompt + schema
  together). No further master prompt churn after that lock.
- Factorial headline vs the master-like `neither` cell: **120b** sort
  `1/3 → 3/3`; **20b** sort unchanged `1/3` in that cell (ties elsewhere
  at `2/3`). Across calls, more explicit `has_header=false` than
  `true`; fails often correlated with explicit false — hence required
  `has_header` + DO lines.
- Factorial k=3 sensitivity is **not** the same thing as a single-shot
  catalog `hard_pass` bit. The later catalog re-run (below) is the
  single-shot measure.

#### Selective catalog re-run (PR 613)

**Not** a full 17-task re-rank (spend-limited OpenRouter key). Re-ran only
`data_sorting` + `tax_column` across the catalog, merged into the Sep 1
summaries via `merge_benchmark_results.py`, and refreshed Pareto /
[`benchmarks.md`](benchmarks.md). Comparison metric: per-model **hard_pass**
vs the Sep 1 details for those two tasks.

##### `data_sorting` vs Sep 1

| Outcome | Models |
|---------|--------|
| **Helped (F→P)** | `deepseek/deepseek-v4-flash-0731`, `inception/mercury-2.5-preview`, `meta/muse-spark-1.3-contributor`, `openai/gpt-oss-20b` |
| **Hurt (P→F)** | `z-ai/glm-5.3-flash` |
| Unchanged pass | `google/gemma-4-31b-it`, `ibm-granite/granite-4.2-8b`, `meta/muse-glimmer-30b`, `openai/gpt-5.6-luna`, `openai/gpt-oss-120b`, `poolside/laguna-s-2.1`, `qwen/qwen3.8-27b`, `x-ai/grok-4.6`, `z-ai/glm-5.3` |
| Unchanged fail | `bytedance-seed/seed-2.0-mini`, `google/gemini-3.5-flash-lite`, `minimax/minimax-m3`, `mistralai/mistral-small-2603`, `upstage/solar-pro4` |
| Error / incomplete (no fair compare) | `google/gemma-4-26b-a4b-it`, `poolside/laguna-xs-2.1` |
| New catalog rows (no Sep 1) | `qwen/qwen3.8-flash` pass; `nvidia/nemotron-3.5-lightning` fail |

Headline: **net help on sort**. 20b helped (F→P). 120b unchanged pass
(already green on Sep 1 — the factorial gain was vs `neither` under k=3,
not vs the Sep 1 catalog bit).

##### `tax_column` vs Sep 1

| Outcome | Models |
|---------|--------|
| **Helped (F→P)** | `google/gemini-3.5-flash-lite`, `openai/gpt-oss-20b`, `poolside/laguna-s-2.1` |
| **Hurt (P→F)** | `qwen/qwen3.8-27b`, `upstage/solar-pro4`, `z-ai/glm-5.3` |
| Unchanged pass | `deepseek/deepseek-v4-flash-0731`, `google/gemma-4-31b-it`, `inception/mercury-2.5-preview`, `meta/muse-spark-1.3-contributor`, `minimax/minimax-m3`, `openai/gpt-oss-120b`, `x-ai/grok-4.6`, `z-ai/glm-5.3-flash` |
| Unchanged fail | `bytedance-seed/seed-2.0-mini`, `google/gemma-4-26b-a4b-it`, `ibm-granite/granite-4.2-8b`, `meta/muse-glimmer-30b`, `mistralai/mistral-small-2603`, `nvidia/nemotron-3.5-lightning`, `openai/gpt-5.6-luna` |
| Error / incomplete (no fair compare) | `poolside/laguna-xs-2.1` |
| New catalog rows (no Sep 1) | `qwen/qwen3.8-flash` pass |

##### Combined take

Worth keeping 610 for the catalog needle move (especially 20b on **both**
tasks). Regressions to work down:

| Task | Hurt models |
|------|-------------|
| `data_sorting` | `z-ai/glm-5.3-flash` |
| `tax_column` | `qwen/qwen3.8-27b`, `upstage/solar-pro4`, `z-ai/glm-5.3` |

Catalog note for **future** ranking runs: drop `z-ai/glm-5.3`
(expensive/overkill); keep `z-ai/glm-5.3-flash` as the more likely-used
model. Keep `glm-5.3` in the current 613 snapshot since it already ran.

#### Next steps

1. Do per-regression failure forensics on the four hurt models
   (`glm-5.3-flash` sort; `qwen3.8-27b` / `solar-pro4` / `glm-5.3` tax) —
   read tool traces and args from the details JSON — **before** more prompt
   churn, because shared prompts need surgical fixes, not another global
   rewrite.
2. Do a targeted A/B on `has_header` wording vs schema-only for
   `z-ai/glm-5.3-flash` sort (it hurt on sort while tax stayed pass) to
   isolate whether required bool + DO line over-constrained header
   detection.
3. Do a tax-only micro A/B for `qwen/qwen3.8-27b` / `upstage/solar-pro4` /
   `z-ai/glm-5.3` on relative-formula wording **without** touching sort
   lines, because the tax hurts did not all co-occur with sort hurts.
4. Do keep factorial discipline: k≥3 on 20b+120b gate models before
   shipping shared Calc prompt changes. Single-shot catalog `hard_pass` is
   noisy (factorial k=3 sensitivity ≠ one catalog bit).
5. Do selective re-measure (`data_sorting` + `tax_column` only) after the
   next prompt tweak rather than a full 17-task catalog until spend allows
   — merge into the prior snapshot the same way as [PR 613](https://github.com/KeithCu/writeragent/pull/613)
   (`merge_benchmark_results.py`).
6. Do park Nemotron-specific sort forks until the shared-prompt
   regressions above are understood.
7. Do optionally re-run the incomplete models (`google/gemma-4-26b-a4b-it`,
   `poolside/laguna-xs-2.1`) when key budget allows so Pareto is not
   carrying stale Sep 1 bits for those ids.

Cross-links: [`oss-20b-eval.md`](oss-20b-eval.md),
[PR 610](https://github.com/KeithCu/writeragent/pull/610),
[PR 613](https://github.com/KeithCu/writeragent/pull/613),
[`benchmarks.md`](benchmarks.md),
[`nemotron-35-eval.md`](nemotron-35-eval.md).

---
*Updated Dev Plan v2.3 — Phase G retrospective (Sep 2026; PR 610 / PR 613)*
