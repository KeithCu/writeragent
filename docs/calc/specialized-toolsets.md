# Calc specialized toolsets (nested delegation)

This document describes how Calc implements **nested delegation** for specialized toolsets, similar to Writer's approach. For detailed background on the delegation model, API design philosophies (Fine-grained vs. Fat APIs), and architecture overview, see [Writer specialized toolsets](../writer/specialized-toolsets.md).

Core tools use `verb_noun`; specialized tools use `domain_verb` across all apps (naming rule and examples in the Writer doc above).

This document focuses on **Calc-specific** domains, implementation status, and feature coverage.

---

## 1. Calc-specific domains and implementation

LibreOffice Calc supports a large surface area through UNO: cells, ranges, formulas, sheets, charts, pivot tables, named ranges, data validation, conditional formatting, and more. WriterAgent implements **nested delegation** for Calc using the same architecture as Writer:

- **Tier filtering** in `ToolRegistry.get_tools` / `get_schemas`
- **Domain bases** (`ToolCalc*Base`) with `tier = "specialized"` and `specialized_domain`
- **Gateway tool**: `delegate_to_subagent` (`tier = "core"`, `is_async()`)
- **System prompt**: `SPECIALIZED_DELEGATION_TEMPLATE` in `plugin/framework/prompts.py` (domain catalog in `DelegateToSubagent.description_for_services`)

For implementation details, see the [Writer documentation](../writer/specialized-toolsets.md#3-implementation-reference).

---

## 1.1 Architecture and in-process PyUNO integration

Calc chat and tool capabilities in WriterAgent were adapted from the standalone `libre_calc_ai` project, translated and re-architected to run strictly **in-process** within LibreOffice's Python runtime.

- **Single-Process PyUNO**: WriterAgent executes all Calc operations directly inside LibreOffice's Python environment. It does **not** launch socket/pipe bridge servers (`BridgeServer`/`BridgeClient`), standalone PyUNO processes, or external PyQt5 UIs.
- **Modular Component Layer**:
  - `bridge.py`: In-process document, sheet, and cell resolution from the active component context.
  - `address_utils.py`: Pure Python A1 address and range parsing/formatting. Sheet-qualified addresses quote the sheet name by Calc's formula rule (`quote_sheet_name`): bare only for an identifier (letter or `_`, then letters, digits, `_`, or combining marks), so `'Q1-2026'.B2` and `'2024'.B2` can be pasted into formulas.
  - `inspector.py`: Analytical read operations (`read_range`, cell details, formatting metadata).
  - `analyzer.py`: Sheet structure analysis, used range bounds, and data region detection.
  - `error_detector.py`: Formula error code mapping exposed via `detect_errors`; `error.code` is the token the cell shows (`#DIV/0!`, `#NULL!`, or `Err:502` where Calc has no `#` form; `ScGlobal::GetErrorString`), the numeric `error_code` is authoritative (`#N/A` is `32767`), suggestions live on `ERROR_TYPES` rows, and results use sheet-qualified addresses.
  - `manipulator.py`: Cell/range write operations, style application, and range clearing (charts are `charts.py`).
- **Component Context Requirement**: All document lookups require a valid UNO component context `ctx` passed from the sidebar panel or MainJob menu dispatch. Callers must pass this `ctx` explicitly so WriterAgent never falls back to `uno.getComponentContext()`, which could resolve a different context and cause wrong-document bugs.

---

## 1.2 Calc chat context assembly

When sending a user query in Calc, WriterAgent dynamically builds a structured document snapshot via `get_calc_context_for_chat(model, max_context, ctx)` in [`plugin/doc/document_helpers.py`](../../plugin/doc/document_helpers.py) (delegating to `plugin.calc.analyzer`):

- **Context Payload**:
  - Document URL and filename.
  - User-visible sheet names (leading `_` tabs such as xlsx→ods `__Anonymous_Sheet_DB__*` are omitted; they stay in the workbook — delete-at-trial-open is parked).
  - Active tab and inspected sheet (when the active tab is an internal `_` sheet, the first user-visible sheet is summarized instead; both names are shown, with a hint to pass `sheet="<inspected>"` to sheet/range tools).
  - Used range dimensions (e.g., `A1:F50`, total rows × columns).
  - Column headers (first row of the used range; date/number-formatted headers use their displayed text; capped at 100 columns with `(+N more columns)`).
  - Active selection range (e.g., `B2:D10`) and, for small selections, a textual preview of selected cell values (omitted with `Selection omitted: the selection belongs to the internal active tab, not the inspected sheet.` when the active tab is an internal `_` sheet).
  - Finished context strings are capped at `max_context` (default `CHAT_DOCUMENT_CONTEXT_MAX_CHARS` = 8000), truncated with `\n\n[... document truncated ...]`.
- **Context Refresh**: Context is regenerated on every chat send so the LLM always sees the current sheet state.
- **`sheet_list` / `get_sheet_summary`**: Same leading-`_` omit rule. `sheet_list` returns objects with `name`, `protected`, and `visible`. The sheets are not deleted.

---

## 1.3 System prompt and formula syntax guidance

Calc chat prompt text is `DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE` in [`plugin/framework/prompts.py`](../../plugin/framework/prompts.py), assembled by `get_chat_system_prompt_for_document(model, additional_instructions)` so Writer and Calc prompts are never mixed.

- **Formula dialects**: Tools accept Excel syntax and Calc syntax; `plugin/calc/formula_dialect.py` parses model text with Calc's `FormulaParser` (Calc API grammar as written wins, then Excel English, then Calc punctuation with `Sheet!A1`) and writes API grammar; read-back stays API grammar; structured references, external file references and `~`/`!` operators are Calc-syntax only. Model-facing text (the Calc prompt, `write_range`, conditional `formula1`, named-range content, and the manual) teaches Calc syntax only, and the converter acts as a silent safety net; inline arrays are `{1;2|3;4}` in `GRAM_API` regardless of UI or locale separators.
- **3-Step Workflow**: System prompts guide the LLM to follow a 3-step execution pattern:
  1. Use the injected sheet context first; call `get_sheet_summary` only when it lacks what is needed (another sheet, first empty row/column, charts, merges). `read_range`'s description and truncated `hint` carry the read-cap guidance. Oversized `read_range` stays capped at 80 cells: peek grid + `row_count`/`column_count`/`cell_count`, and a cardinality-capped `column_distincts` summary (high-unique columns are not listed). An unqualified name is clipped on the sheet that owns the range, a sheet apostrophe is doubled again, a text header cell is left out of that column's distincts, and a peek of more than 2000 rows sets `scanned_rows`.
  2. Do the work with tools. To use a new sheet, create it first via the `sheets` domain (the sheets hop's `instruction` says to populate).
  3. If you changed cells, name the range.
- **Tool mechanics live on the tools**: Fill-down is on `values`. `=PY` placement and `to_pandas` are on the `write_range` description. `set_style` → `insert_html` routing is on `set_style`. OCR routing is the `vision` catalog line (`ToolCalcVisionBase`), and the Calc `delegate_to_subagent` description carries the bound `domain: purpose` catalog (`description_for_services`), while the prompt only points to it. `=PY` plots (`plt`) become a picture anchored at the formula cell. Range and cell parameters state the accepted sheet-prefix and range-list formats with `CALC_SHEET_PREFIX_NOTE` / `CALC_RANGE_LIST_NOTE` (`plugin/calc/base.py`); `insert_html` is excluded because `ScTabViewObj::select` switches tabs when the target is on another sheet. The prompt keeps the 2D-`data` rule because `min`/`max` fail silently.
- **Bulk range writing**: `write_range` writes a 2-D array with one `setDataArray` (formulas per cell with `setFormula`), from a matching range or a top-left cell. A plain string is one value: text with a line break, or with `,`/`;` into a multi-cell range, is refused with `INVALID_PARAM` pointing at the 2-D array. Description order is values / native formulas first and `=PY` spill second (anti-husk `to_pandas()` paragraph stays verbatim), while fill-down lives on the `values` parameter. The `values` parameter distinguishes a single formula string (1-D fill-down/across; relative refs adjust) from a 2-D array (one row per inner array, exact per-cell; repeating the same `=B2*…` pins every row and emits a `REPEATED_FORMULA` warning with a `suggested_call`) or an equivalent JSON string. Excel-style formula text (commas, `Sheet!A1`, `{1,2;3,4}`) is translated to Calc's API grammar by `plugin/calc/formula_dialect.py`. Optional `source` (A1 / `Sheet.A1` / `Sheet!A1`) copies that block onto dest `range` (top-left / source extent) via Calc `copyRange` — formulas + values, relative refs adjust; do not pass `values` with `source`. There is no separate `copy_range` tool. `sort_range` teaches reorder-via-tool plus key/direction and optional `then_by` tie-break keys at the tool (not another CALC_CORE Don't/Do). SortFields is passed as a typed `[]TableSortField` sequence containing the primary key plus any `then_by` keys (a bare tuple is silently ignored by Calc), and the message says `no rows moved` when every key column is unchanged by the sort (and says what to check). `$A$1` refs are accepted, a multi-range write/style/merge/sort/copy is one undo step and a failure undoes that step, and a multi-range array write includes each range's `warning` / `preview` / `array_range` under `ranges`. `write_range` plans every range first (`CellManipulator.plan_write`: resolve, parse, shape check, array measure), so a values/range mismatch, unknown range or occupied array target on any range, named ranges included, is refused before any range changes; a 2-D values/range mismatch names the range and both shapes and suggests the fitting range start.

---

## 1.4 Python specialized agent (two levels)

The python specialized loop is shared with Writer and Draw (`specialized_cross_cutting`). Calc chat does not list `domain="python"` on `delegate_to_subagent` (sheet compute stays `=PY()` / `write_range`). If that loop does run, the outer agent still does venv and symbolic work itself, and calls `python_delegate_domains(domains, task)` for Calc domains such as sheets, ranges, and charts. The inner agent gets `python_run_script` allowlisted to those domains plus `DOMAIN_TOOLS["core"]`, and its instructions list the full script API catalog (names and Args). Shapes mutators are script API calls, not inner LLM tools. See [Writer specialized toolsets §3.5](../writer/specialized-toolsets.md#35-python-specialized-agent-two-levels). `=PY()` keeps `python_tool_domain=""` so formula recalc cannot call document tools.

---

## 2. Calc domains and feature coverage

WriterAgent organizes Calc tools into specialized domains to keep the main chat toolset focused. Below is the current implementation status and roadmap.

### Rich HTML in a single cell

The **`insert_html`** tool ([`plugin/calc/cells.py`](../../plugin/calc/cells.py), implementation [`rich_html.py`](../../plugin/calc/rich_html.py)) uses **`tier = "core"`** on the main Calc tool list (not a delegated `specialized_domain`). It **replaces the content in one cell** (text, number, or formula) with content parsed from an HTML string; `replaced_text` reports what was there. There is no append mode because `ScTabViewObj::insertTransferable` (`sc/source/ui/unoobj/viewuno.cxx`) inserts at a text position only in edit mode (`ScEditShell`) and otherwise replaces the cell. A **sheet prefix** on the cell address (e.g. `Sheet2.A1`, `'Data Sheet'.B2`) selects that sheet; otherwise the active sheet is used.

- **Mechanism**: A **hidden temporary Writer** document loads the fragment with the same **`HTML (StarWriter)`** filter and cursor import path as Writer tools; the body is **selected** with a text cursor (not the view cursor—required for hidden docs), then **`getTransferable` → `select(cell)` → `insertTransferable`** on the Calc controller inside `UndoStepOrRollback` so the cell clear and paste are one undo step and a failed paste undoes the clear. After paste, the insert path **releases SystemClipboard if it still owns that transferable** (only when ours — do not clobber an unrelated user copy), then **reuses** the temp Writer on **`_wa_calc_html`** instead of closing it while Calc still holds the paste (GHA 33771766524: clipboard was empty and `close()` returned, but teardown then hung at `desktop.getComponents()`; GHA 33763078357: the same insert + undo + teardown completes). Failed inserts before paste may still close. Reuse clears the body with `setString("")` before each HTML load. Do not call `gc.collect()` on the transferable — GHA 33771769928 aborted in UNO proxy teardown during Draw. The temp Writer uses target **`_wa_calc_html`** (FrameSearchFlag `CREATE|GLOBAL`) and `Hidden=True` — not `_default` (GHA 33667530529) and not `_blank` (same target as `testing_runner`'s Windows keeper; 542's switch did not unblock 33699746211). If the load returns an already-open Writer RuntimeUID (keeper), that doc is not closed. Step logs go to **stderr** as well as `writeragent.calc`. `testing_runner` prints `KEEPER load/close`, Windows `soffice` PIDs via `tasklist`, `TEST call` / `TEST returned` around the test body, and the FAIL exception on `TEST end`. GHA 33703959362 timed out after `insert_html: execute done` with no `TEST end` — `test_insert_cell_html` now breadcrumbs each post-execute assert / cell read / portion enum / a pre-teardown `getDocumentProperties` probe, and `@with_native_doc` logs enter / body-returned / teardown for that test only (including `_reset_calc_doc` clearContents and a RuntimeUID probe before the udprop write). `testing_runner` also arms `faulthandler.dump_traceback_later(90, repeat=True)` to **stderr** (all Python threads) for that test only and cancels it on `TEST end` — not gdb; in-process stacks only; not gated on `WRITERAGENT_CI_DEBUG`. GHA 33707990007 hung in calc native-doc wipe on `doc.isReadonly()` via `set_document_scripts` after `clearContents`; `_clear_writeragent_udprops` writes `DOCUMENT_SCRIPTS_UDPROP` with `set_document_property` and does not call `isReadonly` (do not skip that write — GHA 33763078357 relocated the hang to `getDocumentProperties()`).
- **Use when**: The model needs **mixed character formatting in one cell**; plain **`write_range` / `set_string`** cannot express that.
- **Limits**: **One cell** per call; **no** embedded images/OLE; math-in-HTML is not a goal for Calc. Callers must supply a **valid UNO component context** on `ToolContext.ctx` (in-process tests and the sidebar pass this; a `None` context can break `get_desktop` in some embed scenarios).

### Chart tools (`chart_list`, `chart_get_info`, `chart_upsert`, `chart_delete`)

Charts are four specialized-domain tools in [`plugin/calc/charts.py`](../../plugin/calc/charts.py), sharing host adapters (`SheetCharts`, `WriterEmbeds`, `DrawPage`). See [`docs/calc/charts.md`](charts.md) for full architecture and lifecycle details.

- **Tiers and Visibility**: **`tier = specialized`**, `domain = charts` on Calc (`ToolCalcChartBase`), Writer (`ToolWriterChartBase`), and Draw (`ToolDrawChartBase`) — same pattern as shapes (`shape_upsert`). One class per tool name in [`charts.py`](../../plugin/calc/charts.py) with union `uno_services` and union `required_core_tools`. **All apps** use `delegate_to_subagent(domain="charts")`, not the main chat list. See `_ChartTool` in [`charts.py`](../../plugin/calc/charts.py).
- **Placement and Position Contract**: Calc accepts a cell address string (e.g., `A1`); on create, an invalid address or mismatched sheet prefix is an error and aborts insertion; on edit, placement is unchanged and produces a warning. Writer accepts `"cursor"` (inserts at view cursor) or `"end"` / omitted (inserts at the end of the document), rejecting any other position string. Draw and Impress omit `position` from their schema.
- **Style and Color Support**: Supports arbitrary background color (`bg_color`) and data series color styling (`colors` array). Colors are parsed dynamically and can be CSS/X11 names (e.g., `green`, `darkgreen`, `yellow`), hex values with or without the `#` prefix (e.g., `#0f0`, `#00FF00`, `00ff00`), or functional RGB/RGBA syntax (e.g., `rgba(255, 0, 0, 0.5)`).
- **Mechanism**: `chart_list`, `chart_get_info`, `chart_upsert`, and `chart_delete` route to the chart action functions on the three chart hosts. List, get, edit, and delete share a unified chart-identity check recognizing chart CLSIDs (including the runtime Writer draw-page shape ID `12d37028-0b55-463d-863a-211463e2c59f` and OLE embed CLSIDs) and unwrapping UNO `ByteSequence` values before comparing GUIDs. Chart names use the lowest free index in the namespace (`Chart_0`, `Chart_1`, etc.) unless a unused requested name is supplied on create. On edit in Calc, `data_range` updates ranges via `setRanges` and `has_header` updates column/row header flags. Bad `headers` or `rows` trigger validation errors (capped at 10,000 cells). Partial styling failures (background color, series colors, axis titles, title, subtitle, legend properties, and unsupported diagram flags) produce `warnings` on a `status: ok` result. Data-apply failure is an error; on creation, any failed data application or insertion drops the newly inserted chart before returning an error, while edit errors leave the chart in place. Deletion removes Calc charts via sheet `removeByName`, Writer inline text embeds via their anchor text (`host.getAnchor().getText().removeTextContent(host)`, so frame- and table-anchored charts are removed too), and Draw/Writer page shapes via `container.remove(host)`. Writer embed creation waits for the chart model with a hard timeout and stops when the idle pump does not finish. `legend_position` writes `ChartLegend.Alignment` as `com.sun.star.chart.ChartLegendPosition` (`TOP` / `BOTTOM` / `LEFT` / `RIGHT`; a placement turns `HasLegend` on; `none` clears it; `has_legend` false plus a placement leaves it off and adds a warning).

### Formulas & Range Writing API Comparison (`write_range` vs. `set_cell_formula`)

WriterAgent's `write_range` tool takes a different design approach than Collabora's `set_cell_formula` tool:
- **WriterAgent (`write_range`)**: **Range-centric bulk writing**. Operates directly via programmatic PyUNO `setDataArray()` for values and `setFormula` per formula cell. Accepts structured rectangular datasets (2-D arrays or a JSON string of one) and commits them in a single transaction. This is highly efficient for generating tables and does not affect the active UI cursor selection. A single ordinary formula into a **1×N or N×1** range fill-down/across with relative A1 adjust (`$` stays absolute) — implemented in Python (`formula_fill.py`), not LO fill/series. `=PY` / PYTHON / PYTHONFUNCTION use a **span-peek**: same-row or single-cell refs outside the quoted Python string adjust; a multi-row DataRange (or unparseable args) stays verbatim so `A1:H1517` is not walked. A 2-D range plus one formula is refused (pass an explicit array). A top-level FILTER/SORT/UNIQUE/SEQUENCE (and the rest of [`ARRAY_FUNCTIONS`](../../plugin/calc/array_formula.py)) is entered with `setArrayFormula` over the measured result — fill-down and array-formula are mutually exclusive. A one-cell target is sized from `=ROWS`/`=COLUMNS` probes placed two columns to the right of the used area (below that area when the sheet already reaches column XFD, so the probe does not overwrite it); an explicit range is used as given (warn `result_does_not_fit` when the result is larger; extra cells show `#N/A` when smaller). Occupied non-origin cells are refused. Optional `array` bool forces array (`true`) or scalar (`false`). `=PY()` remains the spill path for Python reductions.
- **Collabora (`set_cell_formula`)**: **Coordinate-centric scattered writing**. Sequentially dispatches `.uno:GoToCell` and `.uno:EnterString` for each discrete `{cell, formula}` pair. While flexible for scattered cells, it is highly verbose for LLMs to generate, performs expensive UI updates, and hijacks the user's cursor selection.
- **Decision & Parity Plan**: We retain the range-centric PyUNO API for performance and cursor/focus safety. In the future, we plan to adopt Collabora's capability for scattered updates by supporting a batch parameter in `write_range` or adding a dedicated `write_cell_batch` tool that retrieves and updates discrete `XCell` objects programmatically, without utilizing UI dispatches.

---

## 3. Implementation status and roadmap

### 3.1 Current implementation

| Domain / area | WriterAgent status | Module & tools | Notes |
|---------------|--------------------|----------------|-------|
| **Cells** | ✅ Implemented | `cells.py`: `read_range`, `write_range`, `set_style`, `insert_html` ([`rich_html.py`](../../plugin/calc/rich_html.py)), `insert_structure`, merge/unmerge, sort, `delete_structure` | Basic range + style + **HTML → rich text in one cell** ([§ Rich HTML in a single cell](#rich-html-in-a-single-cell); clear + paste are one undo step and a failed paste undoes the clear). Sheet-qualified `insert_html` uses the same prefix rules as other cell tools; `insert_structure` / `delete_structure` take a bare row number or column letter in `start` and an optional `sheet` (active sheet if omitted). `merge_cells` reports hidden non-top-left content (`hidden_count`, `hidden_cells`, `MERGE_HIDES_CONTENT`) and refuses an overlapping merge (`INVALID_PARAM`). Structure results carry `sheet`; `delete_structure` refuses `#REF!` breaks in formula cells or named ranges without `confirm=true` (scan-and-rollback, redo cleared). Full read/write date & time lifecycle (ISO + duration `PT…` wire): [date-time-handling.md](date-time-handling.md). A cell reads the same alone and inside a range (`classify_array_cell`): an error formula is `value: null`, `type: formula` (use `detect_errors` for the token); text typed as `=abc` is `text`; array-formula cells are `formula` with `{=…}`. `read_range` defaults to compact `rows` (ISO/`PTnHnMnS` in values, sparse `types` / `formulas`); `format=cells` returns per-cell `type` / `format_category` / `format_code`. `result` has one entry per requested range, in order (`{status, range, result: [payload]}`), even when one range is requested. Oversized reads stay at 80 cells (peek + size + capped `column_distincts`), an unqualified name is clipped on the sheet that owns the range, a sheet apostrophe is doubled again, the preview's sheet is quoted with `quote_sheet_name`, in the dot form, a text header cell is left out of that column's distincts, and a peek of more than 2000 rows sets `scanned_rows`. `write_range` ingests the same shapes as real Calc serials; `$A$1` refs are accepted, a multi-range write/style/merge/sort/copy is one undo step and a failure undoes that step, and a multi-range array write includes each range's `warning` / `preview` / `array_range` under `ranges`. A write also reports `error_cells` (first 10, sheet-qualified, read after the whole range lands), `text_that_looks_numeric` (locale-free shape check, warn-only), and `apostrophe_removed`, each with a `warnings` Diagnostic; multi-range writes merge them. `write_range` plans every range first (`CellManipulator.plan_write`: resolve, parse, shape check, array measure), so a values/range mismatch, unknown range or occupied array target on any range, named ranges included, is refused before any range changes; a 2-D values/range mismatch names the range and both shapes and suggests the fitting range start. 1-D formula fill-down/across + `=PY` span-peek: [`formula_fill.py`](../../plugin/calc/formula_fill.py). Native array formulas (FILTER/SORT/UNIQUE/SEQUENCE): [`array_formula.py`](../../plugin/calc/array_formula.py) + `setArrayFormula`. Replacing an array checks the new block for occupied cells before clearing the array anchored at the origin, so a refusal leaves the previous array in place; tabular egress and array writes share `refuse_occupied_cells`, which reads the target block once. Teaching: fill-down for row-wise formulas (one string on `values`, not a JSON array of copied refs); `=PY` for small reductions. `sort_range` teaches reorder + key/direction and optional `then_by` tie-break keys at the tool. SortFields is passed as a typed `[]TableSortField` sequence containing the primary key plus any `then_by` keys (a bare tuple is silently ignored by Calc), and the message says `no rows moved` when every key column is unchanged by the sort (and says what to check). |
| **Ranges** | ✅ Implemented | `cells.py`: Get/SetRangeValues, Get/SetRangeFormulas | — |
| **Sheets** | ✅ Implemented | `sheets.py`, `sheet_filter.py`: `sheet_list`, `sheet_create`, `sheet_copy`, `sheet_move`, `sheet_hide`, `sheet_switch`, `get_sheet_summary`, `sheet_apply_filter`, `sheet_clear_filter`, `sheet_get_filter` | Basic sheet ops + AutoFilter. `sheet_list` returns `{name, protected, visible}` objects (still omits leading `_` tabs). `sheet_create` is CRUD-only (tab exists; no cells copied) — populate with `write_range` `source`. Stays specialized. After a sheets hop that created empty tab(s), the gateway forwards the same `instruction` field as web research (`SHEETS_CREATED_NOT_POPULATED_INSTRUCTION`) so the outer can populate. A missing sheet name is `CalcBridge.get_sheet`'s `UnoObjectError`: `No sheet named '…'. Available: …`. `sheet_switch`, `sheet_rename`, `sheet_delete`, `sheet_copy`, `sheet_move`, and `sheet_hide` refuse a leading-`_` internal sheet with `Cannot {verb} internal sheet '…'.`. |
| **Formulas & Error Detection** | ✅ Implemented | `cells.py` (`write_range` + compound undo); `list_functions` / `evaluate_formula`; `detect_errors` ([`error_detector.py`](../../plugin/calc/error_detector.py)); `FormulaDepChain` ([`formula_dep_chain.py`](../../plugin/calc/formula_dep_chain.py)) | Formula evaluation, error explanation (`#N/A`, `#VALUE!`, `#REF!`, `#NAME?`), dependency tracking. The formula-query fallback snapshots at most 80 precedent cells, sets `truncated` when a range is larger, and reports `total_cells`. `evaluate_formula` reports Calc 503 as `#NUM!`, 532 as `#DIV/0!`, 519 as `#VALUE!`, and 522 as a circular reference. It uses a hidden undo context or clears the empty stack and restores `isModified()` on success, leaving workbook history and clean state unchanged. It writes the formula with `getCellByPosition` on the temporary copy only: a sheet prefix is dropped and a defined name is rejected, so the live sheet is not edited. A numeric formula result stays a float, including 0. The formula-query dependency fallback reads `CellRangeAddress.Sheet` and includes that sheet in the precedent address. `detect_errors` caps explanations at 50 per range and returns `total_error_count` and `truncated` (a multi-range call aggregates both, and its `hint` counts the explanations returned); addresses are sheet-qualified (`Sheet2.A1`). |
| **Charts** | ✅ Implemented | `charts.py`: `chart_list`, `chart_get_info`, `chart_upsert`, `chart_delete` (shared with Writer and Draw) | Four skinny tools; `chart_upsert` create/edit by name lookup |
| **Named Ranges** | ✅ Implemented | [`named_ranges.py`](../../plugin/calc/named_ranges.py): `named_range_list`, `named_range_get_info`, `named_range_add`, `named_range_edit`, `named_range_delete`, `named_range_create_from_titles` | Domain-first naming. Global and sheet-local scopes, `NamedRangeFlag` bitmasks (print area, filter criteria), base position parsing, dynamic formulas, header-based creation (`Border.TOP/BOTTOM/LEFT/RIGHT`), and transparent resolution in `read_range` / `write_range` / `set_style` / `named_range_create_from_titles` / chart `data_range` / cell comment tools (comments need a one-cell name). An unknown sheet prefix on `base_cell` is an error (it used to anchor the name on sheet 0). Add and rename both check `ScRangeData::IsNameValid` before touching the range and return `INVALID_NAME`; unknown or out-of-mask flags return `INVALID_FLAGS`. `named_range_edit` runs inside `UndoStepOrRollback` so the edit is one undo step and a failing setter undoes the whole edit. A bare name prefers the active sheet's local range over a same-spelled global name; `scope="global"` or a sheet name forces that container. `named_range_list(scope="all")` and the unqualified sheet fallback skip `_`-prefixed generated sheets. `query_folder_sql` and `CalcBridge.resolve_range_or_address` / `resolve_cell` use `CalcBridge.find_named_range` in this order. |
| **Data Validation** | ❌ Not implemented | — | No `validation.py` / `SetDataValidation` in tree |
| **Conditional Formatting** | ✅ Implemented | [`conditional.py`](../../plugin/calc/conditional.py): `conditional_formatting_add`, `conditional_formatting_list`, `conditional_formatting_remove` — [UNO / roadmap](conditional-formatting.md) | Specialized tier |
| **Analysis** | Hidden from chat | [`analysis.py`](../../plugin/calc/analysis.py) etc. | LLM tools are `ToolBaseDummy`. Calc chat compute is `=PY()` (live formula into empty cells). Helpers remain for Run Python Script / tests. |
| **Pivot Tables** | ✅ Implemented | `pivot.py`: `pivot_table_create`, `pivot_table_refresh`, `pivot_table_delete`, `pivot_table_list` | Specialized tier. A missing sheet is `CalcBridge.get_sheet`'s `UNO_OBJECT_ERROR`; an unknown pivot name is `PIVOT_TABLE_NOT_FOUND` with `hint` / `similar`. A bare name searches agent-visible sheets; `pivot_table_create` refuses a name used on any sheet because LibreOffice renames workbook-wide duplicates, and runs inside `UndoStepOrRollback` so creation is one undo step and a failed refresh undoes the insert. |
| **Tables** | ❌ Not implemented | — | Writer/Draw `table_*` tools exist; not a Calc domain (`CreateTable` / `GetTableInfo` / `SetTableStyle` are not registered names) |
| **Shapes** | ✅ Implemented | `draw/shapes.py`: `shape_upsert`, `shape_delete`, `shape_connect`, `shape_group`, `shape_align`, `shape_distribute`, `shape_create_diagram`, `shape_get_summary` | Shared draw-page tools; not Calc-only modules named `CreateShape` |
| **Comments** | ✅ Implemented | `comments.py`: ListCellComments, AddCellComment, DeleteCellComment | Specialized tier. `cell_comment_list` returns the formatted string from `XSheetAnnotation.getDate()`; a DateTime struct is still formatted if a bridge returns one. |
| **Forms** | ✅ Implemented | `plugin/writer/specialized/forms.py`: FormCreate, FormGenerate, FormListControls, FormCreateControl, FormEditControl, FormDeleteControl (shared with Writer) | Specialized tier. Supports list box; control type table is `CONTROL_TYPE_MAP` in `plugin/draw/tree.py`. Calc label text is written only into an empty or text cell, so a formula or number in the active cell is left alone. `form_create` and `form_generate` return an error when a field fails. |

### 3.2 Future enhancements (roadmap)

| Feature | Status | Notes |
|---------|--------|-------|
| **Macros** | ❌ Not implemented | Macro recording/execution, VBA compatibility |
| **Goal Seek** | ✅ Implemented | Target value analysis |
| **Solver** | ✅ Implemented | Optimization scenarios, constraint solving |
| **Scenarios** | ❌ Not implemented | Scenario manager, what-if analysis |
| **Data Tables** | ❌ Not implemented | One-way and two-way data tables |
| **External Data** | ⚠️ Partial | [`duckdb_tools.py`](../../plugin/calc/duckdb_tools.py): `sql_query_folder` on sibling files | DB links and web queries still open |
| **Advanced Forms** | ❌ Not implemented | Advanced form features, database integration, complex validation |
| **Advanced Chart Features** | ✅ Partial | Trend lines, error bars, secondary axes |
| **Pivot Chart Creation** | ❌ Not implemented | Direct pivot chart creation from data |
| **Dynamic Named Ranges** | ✅ Implemented | Formula-based range definitions and sheet-scoped names supported |
| **Array Formulas** | ✅ Partial | Basic support, matrix operations TBD |
| **Structured References** | ❌ Not implemented | Table-based formula references |
| **Table Slicers** | ❌ Not implemented | LibreOffice has no slicer object; AutoFilter (`sheet_filter_*`) is shipped |
| **Sheet Protection** | ⚠️ Partial | [`sheets.py`](../../plugin/calc/sheets.py): `sheet_protect` | Per-cell unlock / `CellProtection` tools absent |
| **Change Tracking** | ⚠️ Partial | Writer tracking tools on SpreadsheetDocument: `tracking_start` / `stop` / `list` / `show` / `manage` ([`plugin/writer/tracking.py`](../../plugin/writer/tracking.py)) | On Calc, `tracking_list` returns `track_changes_list_unsupported` when `getRedlines` is absent; start/stop still toggle recording — not a full redline UI on every build |

### 3.3 Cross-cutting improvements

- **MCP / API exposure:** Shipped as `mcp.tool_exposure_mode` (`delegate` / `direct_flat` / `direct_discovery`) — see [MCP protocol](../mcp-protocol.md#exposing-specialized-tools-directly-mcptool_exposure_mode-experimental).
- **Performance tuning:** Timeouts and step limits for sub-agent execution
- **Telemetry:** Track domain usage to prioritize development
- **Documentation:** Keep [`AGENTS.md`](../../AGENTS.md) synchronized

For testing and operations details, see the [Writer documentation](../writer/specialized-toolsets.md#4-testing-and-operations).

---

### 3.4 Future ideas (from Opus prompt review, Nov 2026)

These ideas came out of an Opus review of the Calc prompts and tools in November 2026. They are recorded here so they are not forgotten. They are not scheduled work. Some are harder, or would be useful later, but they were deferred for the reasons below.

- **Locale date and number coercion (review rows 6 and 32, coercion half only).** Row 6 also suggested optionally parsing unambiguous locale date forms. Row 32 also suggested optionally coercing strings such as `1,5` based on the document locale. The warn-only half of those rows (report values that look like dates or numbers but were stored as text, with no conversion) shipped in PR 3560. Coercion itself is deferred because guessing locale dates or numbers is risky.

- **Header auto-detect on sort (review row 14, header half only).** The multi-key half shipped via optional `then_by` on `sort_range`; auto-detecting the header row and reporting `header_detected` stays deferred because guessing the header is risky (`has_header` is required so labels do not sort as values) and needs its own eval.

- **Promote common tools out of delegation (review row 15).** Common tasks such as sort, new sheet, and chart are hidden behind delegation, so the prompt has to describe every domain. The suggestion was to promote the three or four most used tools to the top level and keep domain blurbs in the delegate tool description only. Deferred because this redesigns delegation.

- **Create-and-fill a sheet in one call (review row 16).** `sheet_create` currently creates an empty tab. The suggestion was to let sheet creation accept optional `source`/`values` so a new sheet can be created and filled in one call. Deferred as a low-priority API addition.

- **Infer image edits from the selection (review row 19 — shipped in PR #760).** `ImageGenerate.execute` edits the selected graphic when `source_image` is omitted while a graphic is selected. The remaining parent/specialist rule is a single shared constant (`IMAGE_EDIT_TASK_RULE` / `IMAGE_EDIT_TOOL_RULE`) telling the model to keep the user's edit wording rather than paraphrasing as generate-new or deleting first.

- **Render Markdown in the sidebar (review row 21).** The prompt demands HTML, not Markdown, and includes good/bad examples. The suggestion was to render or convert Markdown in the sidebar, which would remove a rule small models often break. Deferred because Markdown rendering in the sidebar is a big UI change.

Since this list was written, four rows were picked up and are no longer deferred: row 1 (PR 3592 adds a silent Excel-grammar fallback when parsing model-written formulas, while the prompt and read tools keep using Calc syntax), row 14's multi-key half (adds optional `then_by` tie-break keys to `sort_range` in one call), row 18 (PR 3596 drops `task` from the vision call and describes it only as running OCR on the selected image), and row 22 (PR 3589 auto-links plain cell references in Calc chat). On `eval-ab SUITE=calc` (3 repeats), `data_sorting` hard passes tied A vs B (`openai/gpt-oss-20b:nitro` 2/3 vs 2/3, `openai/gpt-oss-120b:nitro` 0/3 vs 0/3; overall 26/36 vs 26/36).

Review row 35 is keep-as-is, with no work: undo only right after the model’s own mistake; don’t paste document data into a delegate task; color and polish remain judgment calls. Those are behaviour and user-experience rules, not API mechanics.

### 3.5 Deferred ideas (follow-up triage, Nov 2026)

These came out of the November 2026 follow-up triage (`217-followups-triage.md`). They are worth doing at some point but are not scheduled.

- **Merge cells keeping hidden content (item 19).** The merge tool drops the content of every cell except the top-left one. The LibreOffice UI can instead move the hidden content into the top-left cell (`bContents=true`). The idea is an optional `move_contents` mode on the merge tool. Deferred until a real task or eval needs it.

- **Undo step for `=PY()` plots (item 107).** `plugin/calc/python/image_egress.py` places `=PY()` plot images without an undo bracket, so the user can't undo a plot placement as one step. The idea is to wrap placement in `UndoStepOrRollback`. Deferred because it needs care to stay safe on Collabora Online.

- **Rewrite typed `xl()` formulas (item 364).** `=PY("...xl('A1')...")` written by the model is rewritten into the dependency-tracked form, but the same formula typed straight into a cell is not. The idea is a cell-input hook that rewrites typed formulas too. Deferred because a live sheet-input hook adds complexity to a path that is simple today.

---

## 4. Summary

| Concern | Mechanism |
|---------|-----------|
| Smaller default tool list | `exclude_tiers` default in `ToolRegistry.get_tools` / `get_schemas` |
| Domain grouping | `ToolCalc*Base.specialized_domain` + `tier = "specialized"` |
| User/model entry point | `delegate_to_subagent` (`tier = "core"`, async) |
| Sub-agent completion | `final_answer` (`tier = "specialized_control"`) |
| Prompt teaching | `SPECIALIZED_DELEGATION_TEMPLATE` in `plugin/framework/prompts.py` + `DelegateToSubagent.description_for_services` |
| Execution by name | Unchanged `execute()` — tier only affects **listing**, not **dispatch** |

This design trades a second LLM hop (delegation) for a **cleaner main conversation** and **safer tool choice**, while preserving a path to **full** Calc automation per domain.

---

## 5. References

For complete LibreOffice Calc UNO API documentation:
- [Official LibreOffice API Reference](https://api.libreoffice.org/)
- [LibreOffice Developer's Guide](https://wiki.documentfoundation.org/Documentation/DevGuide)
- [LibreOffice Development Tools](https://help.libreoffice.org/latest/en-US/text/shared/guide/dev_tools.html)
- [PyOOCalc - Python Libre/Open Office Calc interface API (UNO)](https://github.com/panpuchkov/pyoocalc)

For recent feature additions:
- [LibreOffice 26.2 Release Notes](https://www.howtogeek.com/libreoffices-first-big-update-for-2026-has-arrived/)
- [LibreOffice 26.2 New Features](https://9to5linux.com/libreoffice-26-2-open-source-office-suite-officially-released-this-is-whats-new)

## Open

- `[feature M many]` Scattered cell updates in one call (a batch parameter on `write_range` or a `write_cell_batch` tool) without UI dispatches. (§ Formulas & Range Writing API Comparison)
- `[feature M many]` Data validation tools for Calc (no `validation.py` in the tree). (§ 3.1 Current implementation)
- `[feature M few]` Calc table tools and structured references. (§ 3.1 Current implementation, § 3.2 Future enhancements)
- `[feature L few]` Scenario manager and what-if data tables. (§ 3.2 Future enhancements)
- `[feature M few]` Direct pivot chart creation from data. (§ 3.2 Future enhancements)
- `[feature L few]` External data beyond folder SQL, such as database links and web queries. (§ 3.2 Future enhancements)
- `[fidelity S many]` Per-cell unlock and cell-protection tools to complete sheet protection. (§ 3.2 Future enhancements)
- `[fidelity M few]` Fuller change tracking on Calc, where `tracking_list` is unsupported when `getRedlines` is absent. (§ 3.2 Future enhancements)
- `[fidelity S few]` Optional `move_contents` on the merge tool so hidden cell content moves into the top-left cell like the UI does. (§ 3.5 Deferred ideas)
- `[feature S many]` Let `sheet_create` accept `source` or `values` so a new sheet can be created and filled in one call. (§ 3.4 Future ideas)
- `[eng S few]` Wrap `=PY()` plot placement in `UndoStepOrRollback` so it undoes as one step, safe on Collabora Online. (§ 3.5 Deferred ideas)
- `[idea M few]` Rewrite typed `=PY("...xl('A1')...")` formulas into the dependency-tracked form with a cell-input hook. (§ 3.5 Deferred ideas)
- `[idea M many]` Coerce locale dates and numbers such as `1,5` stored as text, beyond the shipped warn-only check. (§ 3.4 Future ideas)
- `[idea M many]` Auto-detect the header row on `sort_range` and report `header_detected`, needing its own eval. (§ 3.4 Future ideas)
- `[idea L]` Promote the most-used tools (sort, new sheet, chart) out of delegation so the prompt describes fewer domains. (§ 3.4 Future ideas)
- `[idea L]` Render Markdown in the sidebar so the prompt no longer has to demand HTML. (§ 3.4 Future ideas)
