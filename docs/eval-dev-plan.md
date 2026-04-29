# LocalWriter: Evaluation System Development Plan (Internal Edition)

This plan covers the WriterAgent prompt optimization + evaluation system (`scripts/prompt_optimization/`). It supports fast non-LO evaluation via `StringDocState` (default `--backend string` in `llm_chat_eval.py`) for Writer text/HTML tasks, `DrawDocState` for shapes/flowcharts, and `CalcStringState` for data sorting (`data_sorting`) and tax column (`tax_column`) tests. Full LO (`--backend lo`) for fidelity. See `docs/archive/eval-ideas.md` (annotated with LO requirements) for the original ~50 test cases. New Calc tests implemented in `string_eval_tools.py:231` (CalcStringState with `sort_range`, `write_cell_range`, `get_sheet_summary`, `snapshot()` JSON output) and `llm_chat_eval.py:221` (task detection + schemas).

## Current Status

The evaluation system lives in `scripts/prompt_optimization/`:
- `run_eval.py` / `run_eval_multi.py`: Main entrypoints (use `LlmClient` + tool loop from `llm_chat_eval.py`).
- Default: `--backend string` (`string_eval_tools.py:StringDocState` — pure Python HTML/string mutations for `get_document_content`/`apply_document_content`/`find_text`; no LO).
- `--backend lo`: Headless Writer via `tools_lo.py` + real `ToolRegistry`.
- Judging: Substring checks + LLM-as-a-Judge (`eval_core.py`, `metric.py`; structural vs creative weighting; gold_standards.json from high-tier teacher; quadratic IpD = Correctness² / Cost).
- Current dataset: 8 Writer tasks in `dataset.py` (table_from_mess, reformat_resume, etc.; see `ALL_EXAMPLES`).

The 50 test cases live in [`docs/archive/eval-ideas.md`](docs/archive/eval-ideas.md) (20 Writer, 20 Calc, 5 Draw, 5 Multimodal; categorized by level with modes for judging).

---

## Hybrid Evaluation Strategy for Draw, Flowcharts & Images (New)

Current string backend cannot easily handle `create_shape`, `get_draw_tree`, `generate_image`, or complex Draw state. **Screenshots are not needed**.

**Recommended path (non-LO first)**:
- **DrawJSONBackend** (parallel to `StringDocState`): Maintains a mutable JSON tree. Mock `get_draw_tree`, `create_shape` (flowchart-*, connectors), `edit_shape`, `shapes_connect`, `shapes_group`, `get_draw_summary`. `dispatch_string_tool` extended for Draw tools. Final state for judging = serialized tree JSON (structural diff on nodes, connections, text, geometry with tolerances) or LLM-as-Judge on tree.
- `plugin/modules/draw/tree.py:GetDrawTree` is the perfect "DOM" — recursive JSON with `type`, `text`, `geometry`, `connected_start`/`connected_end` (by name/text), `children` for groups. Its description explicitly says "Use this instead of requesting a screenshot to understand the layout, text, connections, and hierarchy of objects (like flowcharts or diagrams)."
- For `generate_image` (`plugin/modules/writer/images.py`, `plugin/framework/image_utils.py`): Mock `ImageService.generate_image` to return fixed temp path; state adds an "image" node to tree or HTML sentinel. Judge on tool result JSON (`status: "ok"`) + presence in final tree.
- Verification: Extend `eval_core.py` for tree-based `expected_contains` (node paths) or JSON-aware judge. No pixel comparison.

**LO transition**: Use `--backend lo` with Draw doc (`private:factory/sdraw`) + real tools for fidelity tests (real insertion, styles, z-order, rendering). See `plugin/tests/uno/test_draw.py` for patterns (`_exec_tool`, assertions on JSON + UNO counts/positions). `get_draw_context_for_chat` in `plugin/framework/document.py` provides lighter text summary.

**When to require LO** (analysis of [`docs/archive/eval-ideas.md`](docs/archive/eval-ideas.md)):
- **String/DrawJSON sufficient** (~40%): Pure text cleanup, logical rewriting, basic table engineering (HTML), bullet consistency, format preservation, simple shape creation (via tree mutation). Flowchart Gen (#3 in Draw) is ideal for tree-based eval (check connections, node types/text).
- **Requires LO or advanced mock for fidelity** (most Calc, many Writer structural, all Draw/Multimodal):
  - Writer: Styles, comments, track changes, TOC, headers/footers, section breaks, style mapping, bibliography (UNO-specific).
  - Calc: Formulas, conditional formatting, pivot tables, charts, multi-sheet ops (20/20 tests).
  - Draw (5/5): Z-order, grouping, precise layout/alignment, scaling — tree JSON handles most; full LO for geometry/rendering edge cases.
  - Multimodal (5/5): Vision (OCR, captioning, spatial audit on images/diagrams) — needs `generate_image` + insertion or real image fixtures (`multimodal_vision.odt`).
- **Recommendation**: Start with DrawJSONBackend for Draw/flowchart tests (fast, no LO dependency, solves "how to measure flowchart without screenshots"). Use LO backend for Calc/Writer fidelity suite and as gold standard. This avoids making all evals "harder" while enabling image/tool-calling evals via metadata/tree. Aligns with AGENTS.md testing policy (unit tests for mocks, UNO tests for real document interaction).

See previous analysis for architecture diagram (StringBackend → DrawJSONBackend → LOBackend; judge on final tree/HTML).

---

## Updated Phase 2: Roadmap & Next Steps

### A. Expand Test Suite (In Progress)
- Port high-priority cases from [`docs/archive/eval-ideas.md`](docs/archive/eval-ideas.md) into `dataset.py` (focus on string/DrawJSON compatible first: more Writer formatting, table, bullet; add Draw flowchart example).
- Added DrawJSONBackend support in `llm_chat_eval.py` / `draw_eval_tools.py` (future).
- Categorize by LO requirement (see above).

### B. Multimodal & Image Evaluation
- Mock `generate_image` + tree/image node in state.
- Fixtures: `tests/fixtures/multimodal_vision.odt`, image assets.
- Judge on inserted image metadata + caption accuracy.

### C. Test Fixtures
- Expand with Draw-specific tree golds in `gold_standards.json`.
- `long_summarization.odt`, `complex_calc.ods`.

### D. Advanced Reporting & CI
- Integrate with `run_eval_multi.py` (already supports multi-model IpD).
- Add `--backend drawjson` flag.
- UNO tests for Draw eval path (`plugin/tests/uno/`).

### E. LO Transition Strategy
- Keep string/DrawJSON as primary for speed/CI.
- LO for validation of specialized tools (`ToolWriterSpecialBase`, `ToolDrawSpecialBase`, `get_draw_tree`).
- Update `AGENTS.md` prompt optimization section with hybrid guidance.

---
*Updated Dev Plan v2.0 — Hybrid Non-LO + DrawJSON + LO Fidelity (Apr 2026)*
