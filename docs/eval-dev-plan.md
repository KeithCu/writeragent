# WriterAgent: Evaluation System Development Plan (Internal Edition)

This plan covers the WriterAgent prompt optimization + evaluation system (`scripts/prompt_optimization/`). It supports fast non-LO evaluation via `StringDocState` (default `--backend string` in `llm_chat_eval.py`) for Writer text/HTML tasks, `DrawDocState` for shapes/flowcharts, and `CalcStringState` for data sorting (`data_sorting`) and tax column (`tax_column`) tests. Full LO (`--backend lo`) for fidelity. See `docs/archive/eval-ideas.md` (annotated with LO requirements) for the original ~50 test cases. New Calc tests implemented in `string_eval_tools.py:231` (CalcStringState with `sort_range`, `write_cell_range`, `get_sheet_summary`, `snapshot()` JSON output) and `llm_chat_eval.py:221` (task detection + schemas).

## Current Status

The evaluation system lives in `scripts/prompt_optimization/`:
- `run_eval.py` / `run_eval_multi.py`: Main entrypoints (use `LlmClient` + tool loop from `llm_chat_eval.py`).
- Default: `--backend string` (`string_eval_tools.py:StringDocState` — pure Python HTML/string mutations for `get_document_content`/`apply_document_content`/`find_text`; no LO).
- `--backend lo`: Headless Writer via `tools_lo.py` + real `ToolRegistry`.
- Judging: Honest `expected_contains` / `reject_contains` plus **result oracles** on the exported final document (`oracles.py`). LLM-as-a-Judge is **creative-only** (resume, logical rewriting, summarization). `gold_standards.json` is hand-written from the rubrics.
- Current dataset: 15 tasks in `dataset.py` `ALL_EXAMPLES` (12 Writer including `style_consistency`, `smart_summarization`, `section_refactor`, `comment_management`, plus `flowchart_gen` + `data_sorting` + `tax_column`).
- `--student scripted` (`scripted_student.py`): no API key; pass is `example_passed` (substring + oracles) on exported state. `--backend lo` is headless UNO (`tools_lo.py`), not an in-memory mock. `-j` is threads; UNO is serialized on `_lo_thread`. Do not use `tests/eval_runner.py` for this harness. Do not set `WRITERAGENT_TESTING=1` for LO eval.
- CI / pytest: `tests/scripts/test_eval_oracles.py` and `test_scripted_eval_pack.py` replay `--backend string --student scripted` (no OpenRouter). Headless `--backend lo --student scripted` is `@pytest.mark.integration` (excluded from `make pytest`); skipped unless `soffice` + real `uno` are available; local command: `python scripts/prompt_optimization/run_eval.py --backend lo --student scripted --no-bust-cache -v`.

The 50 test cases live in [`docs/archive/eval-ideas.md`](docs/archive/eval-ideas.md) (20 Writer, 20 Calc, 5 Draw, 5 Multimodal; categorized by level with modes for judging).

---

## Hybrid Evaluation Strategy for Draw, Flowcharts & Images (New)

Current string backend cannot easily handle `create_shape`, `get_draw_tree`, `image_generate`, or complex Draw state. **Screenshots are not needed**.

**Recommended path (non-LO first)**:
- **DrawJSONBackend** (parallel to `StringDocState`): Maintains a mutable JSON tree. Mock `get_draw_tree`, `shape_upsert` (flowchart-*, connectors), `shape_connect`, `shape_group`, `shape_summary`. `dispatch_string_tool` extended for Draw tools. Final state for judging = serialized tree JSON (structural diff on nodes, connections, text, geometry with tolerances) or LLM-as-Judge on tree.
- `plugin/draw/tree.py:GetDrawTree` is the perfect "DOM" — recursive JSON with `type`, `text`, `geometry`, `connected_start`/`connected_end` (by name/text), `children` for groups. Its description explicitly says "Use this instead of requesting a screenshot to understand the layout, text, connections, and hierarchy of objects (like flowcharts or diagrams)."
- For `image_generate` (`plugin/writer/images.py`, `plugin/writer/image_utils.py`): Mock `ImageService.image_generate` to return fixed temp path; state adds an "image" node to tree or HTML sentinel. Judge on tool result JSON (`status: "ok"`) + presence in final tree.
- Verification: Extend `eval_core.py` for tree-based `expected_contains` (node paths) or JSON-aware judge. No pixel comparison.

**LO transition**: Use `--backend lo` with Draw doc (`private:factory/sdraw`) + real tools for fidelity tests (real insertion, styles, z-order, rendering). See `tests/draw/test_draw_uno.py` for patterns (`_exec_tool`, assertions on JSON + UNO counts/positions). `get_draw_context_for_chat` in `plugin/draw/bridge.py` provides lighter text summary.

**When to require LO** (analysis of [`docs/archive/eval-ideas.md`](docs/archive/eval-ideas.md)):
- **String/DrawJSON sufficient** (~40%): Pure text cleanup, logical rewriting, basic table engineering (HTML), bullet consistency, format preservation, simple shape creation (via tree mutation). Flowchart Gen (#3 in Draw) is ideal for tree-based eval (check connections, node types/text).
- **Requires LO or advanced mock for fidelity** (most Calc, many Writer structural, all Draw/Multimodal):
  - Writer: Styles, comments, track changes, TOC, headers/footers, section breaks, style mapping, bibliography (UNO-specific).
  - Calc: Formulas, conditional formatting, pivot tables, charts, multi-sheet ops (20/20 tests).
  - Draw (5/5): Z-order, grouping, precise layout/alignment, scaling — tree JSON handles most; full LO for geometry/rendering edge cases.
  - Multimodal (5/5): Vision (OCR, captioning, spatial audit on images/diagrams) — needs `image_generate` + insertion or real image fixtures (`multimodal_vision.odt`).
- **Recommendation**: Start with DrawJSONBackend for Draw/flowchart tests (fast, no LO dependency, solves "how to measure flowchart without screenshots"). Use LO backend for Calc/Writer fidelity suite and as gold standard. This avoids making all evals "harder" while enabling image/tool-calling evals via metadata/tree. Aligns with AGENTS.md testing policy (unit tests for mocks, UNO tests for real document interaction).

See previous analysis for architecture diagram (StringBackend → DrawJSONBackend → LOBackend; judge on final tree/HTML).

---

## Updated Phase 2: Roadmap & Next Steps

### A. Expand Test Suite (Completed hardening)
- Hardened key tests in [`scripts/prompt_optimization/dataset.py`](scripts/prompt_optimization/dataset.py) (BULK_CLEANUP, REFORMAT_RESUME, LOGICAL_REWRITING, TABLE_ENGINEERING, BULLET_CONSISTENCY, TAX_COLUMN, STYLE_CONSISTENCY, COMMENT_MANAGEMENT) with stricter instructions, edge cases, precise rubrics referencing judge weights/gold, expanded contains/rejects, tool hints (per plan). TABLE_FROM_MESS and structural Draw/Calc kept as baseline. No new full tests added ("don't go crazy").
- Ported/updated from [`docs/archive/eval-ideas.md`](docs/archive/eval-ideas.md).
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
- Add `--backend drawjson` flag.
- UNO tests for Draw eval path (`tests/draw/`).

### E. LO Transition Strategy
- Keep string/DrawJSON as primary for speed/CI.
- LO for validation of specialized tools (`ToolWriterSpecialBase`, `ToolDrawSpecialBase`, `get_draw_tree`).
- Update `AGENTS.md` prompt optimization section with hybrid guidance.

### F. Calc `=PY()` placement (future — do not implement in the same change as hiding Calc `python`)

**Hypothesis:** a few limitation words on main chat beat a second specialized domain. Dest / spill / peek live on `write_formula_range` (`plugin/calc/cells.py`); MIPROv2 can later rewrite that description plus the remaining `CALC_FORMULA_SYNTAX` / pointer in `CALC_CORE_DIRECTIVES` (`plugin/framework/prompts.py`).

Calc chat no longer delegates `domain="python"`; models must `write_formula_range` of `=PY("result = …"; DataRange)` into an **empty cell outside DataRange**. Future eval rows (not in `dataset.py` yet):

| id | Ask | Pass | Fail |
|----|-----|------|------|
| unique beside | drop dupes on A1:H500 onto the sheet | `=PY` dest **J1** (or first empty col / other sheet) | dest inside A1:H500; `domain=python`; chat-only |
| refuse overlap | put the formula in **H1**, data A1:H500 | dest J1/I1 and says H1 is inside the range | writes H1 |
| in-place reframe | write unique rows **back onto** A1:H500 | same as unique beside + short circular explanation | `=PY` in A1 |
| no bulk read | same unique-rows ask | no `read_cell_range` of A1:H500 / the spill | dumping the block into chat (overloads context) |

Scoring: dest vs parsed data range; optional judge. Start `--backend string` after `CalcStringState` records dest + formula; LO later for spill. Optimize output: `optimized_calc_py_prompt.json`. If short main-chat wording cannot pick J1 over H1, *then* try a nested `=PY` playbook — do not add that hop until this eval exists.

---
*Updated Dev Plan v2.1 — Calc `=PY` eval notes (Aug 2026)*
