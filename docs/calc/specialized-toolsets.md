# Calc specialized toolsets (nested delegation)

This document describes how Calc implements **nested delegation** for specialized toolsets, similar to Writer's approach. For detailed background on the delegation model, API design philosophies (Fine-grained vs. Fat APIs), and architecture overview, see [Writer specialized toolsets](../writer/specialized-toolsets.md).

This document focuses on **Calc-specific** domains, implementation status, and feature coverage.

---

## 1. Calc-specific domains and implementation

LibreOffice Calc supports a large surface area through UNO: cells, ranges, formulas, sheets, charts, pivot tables, named ranges, data validation, conditional formatting, and more. WriterAgent implements **nested delegation** for Calc using the same architecture as Writer:

- **Tier filtering** in `ToolRegistry.get_tools` / `get_schemas`
- **Domain bases** (`ToolCalc*Base`) with `tier = "specialized"` and `specialized_domain`
- **Gateway tool**: `delegate_to_specialized_calc_toolset` (`tier = "core"`, `is_async()`)
- **System prompt**: `CALC_SPECIALIZED_DELEGATION_TEMPLATE` in `plugin/framework/prompts.py`

For implementation details, see the [Writer documentation](../writer/specialized-toolsets.md#3-implementation-reference).

---

## 1.1 Architecture and in-process PyUNO integration

Calc chat and tool capabilities in WriterAgent were adapted from the standalone `libre_calc_ai` project, translated and re-architected to run strictly **in-process** within LibreOffice's Python runtime.

- **Single-Process PyUNO**: WriterAgent executes all Calc operations directly inside LibreOffice's Python environment. It does **not** launch socket/pipe bridge servers (`BridgeServer`/`BridgeClient`), standalone PyUNO processes, or external PyQt5 UIs.
- **Modular Component Layer**:
  - `calc_bridge`: In-process document, sheet, and cell resolution from the active component context.
  - `calc_address_utils`: Pure Python A1 address and range parsing/formatting.
  - `calc_inspector`: Analytical read operations (`read_cell_range`, cell details, formatting metadata).
  - `calc_sheet_analyzer`: Sheet structure analysis, used range bounds, and data region detection.
  - `calc_error_detector`: Formula error code mapping (`#N/A`, `#VALUE!`, `#REF!`, `#NAME?`, etc.) exposed via `detect_and_explain_errors`.
  - `calc_manipulator`: Cell/range write operations, style application, range clearing, and chart creation.
- **Component Context Requirement**: All document lookups require a valid UNO component context `ctx` passed from the sidebar panel or MainJob menu dispatch. Callers must pass this `ctx` explicitly so WriterAgent never falls back to `uno.getComponentContext()`, which could resolve a different context and cause wrong-document bugs.

---

## 1.2 Calc chat context assembly

When sending a user query in Calc, WriterAgent dynamically builds a structured document snapshot via `get_calc_context_for_chat(model, max_context, ctx)` in [`plugin/doc/document_helpers.py`](../../plugin/doc/document_helpers.py) (delegating to `plugin.calc.analyzer`):

- **Context Payload**:
  - Document URL and filename.
  - Active sheet name.
  - Used range dimensions (e.g., `A1:F50`, total rows × columns).
  - Column headers (first row of the used range).
  - Active selection range (e.g., `B2:D10`) and, for small selections, a textual preview of selected cell values.
- **Context Refresh**: Context is regenerated on every chat send so the LLM always sees the current sheet state.

---

## 1.3 System prompt and formula syntax guidance

Calc chat uses `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` (defined in [`plugin/framework/prompts.py`](../../plugin/framework/prompts.py)). Prompt selection is governed by `get_chat_system_prompt_for_document(model, additional_instructions)`, ensuring Writer and Calc prompts are never mixed.

- **Formula Parameter Separator**: Calc formulas use **semicolons** (`;`) as parameter separators (e.g. `=SUM(A1; A2)` or `=IF(A1>0; "Yes"; "No")`), matching LibreOffice Calc standards.
- **Cross-Sheet References**: Cross-sheet references use **dot notation** (e.g. `Orders.A1` or `'Sheet Name'.B5`). Excel-style exclamation points (`Sheet1!A1`) cause `#NAME?` errors in Calc.
- **3-Step Workflow**: System prompts guide the LLM to follow a 3-step execution pattern:
  1. Obtain sheet summary / small cell range peek (`get_sheet_summary` or `read_cell_range`).
  2. Execute direct operations via tools without lengthy conversational explanations.
  3. Provide a brief confirmation referencing modified cell/range addresses.
- **Bulk CSV / Formula Range Writing**: `write_formula_range` performs bulk programmatic `setDataArray`/`setFormulaArray` updates, and can ingest raw CSV string blocks (parsing delimiters automatically) without file I/O overhead.

---

## 2. Calc domains and feature coverage

WriterAgent organizes Calc tools into specialized domains to keep the main chat toolset focused. Below is the current implementation status and roadmap.

### Rich HTML in a single cell

The **`insert_cell_html`** tool ([`plugin/calc/cells.py`](../../plugin/calc/cells.py), implementation [`rich_html.py`](../../plugin/calc/rich_html.py)) uses **`tier = "core"`** on the main Calc tool list (not a delegated `specialized_domain`). It **replaces the text in one cell** on the **active sheet** with content parsed from an HTML string:

- **Mechanism**: A **hidden temporary Writer** document loads the fragment with the same **`HTML (StarWriter)`** filter and cursor import path as Writer tools; the body is **selected** with a text cursor (not the view cursor—required for hidden docs), then **`getTransferable` → `select(cell)` → `insertTransferable`** on the Calc controller. After paste, the insert path **drops the SwTransferable and releases SystemClipboard if it still owns that transferable**, then closes the temp Writer (and `dispose()` only if the RuntimeUID is still on the desktop). `SwXTextView.getTransferable` builds a `SwTransferable` bound to that Writer shell (`PrepareForCopy`); closing the source while it is still the Windows OLE clipboard owner is the remaining mechanism that matches GHA 33763078357 / 33731356375 (sheet-level reset returns; the next SfxObjectShell call — first `isReadonly`, then `getDocumentProperties` — blocks). Clipboard is touched only when it actually holds our transferable. The temp Writer uses target **`_wa_calc_html`** (FrameSearchFlag `CREATE|GLOBAL`) and `Hidden=True` — not `_default` (GHA 33667530529) and not `_blank` (same target as `testing_runner`'s Windows keeper; 542's switch did not unblock 33699746211). If the load returns an already-open Writer RuntimeUID, that doc is not closed. Step logs go to **stderr** as well as `writeragent.calc` (clipboard snapshot, transferable id, writers after close). `testing_runner` prints `KEEPER load/close`, Windows `soffice` PIDs via `tasklist`, `TEST call` / `TEST returned` around the test body, and the FAIL exception on `TEST end`. GHA 33703959362 timed out after `insert_cell_html: execute done` with no `TEST end` — `test_insert_cell_html` now breadcrumbs each post-execute assert / cell read / portion enum / a pre-teardown `getDocumentProperties` probe, and `@with_native_doc` logs enter / body-returned / teardown for that test only (including `_reset_calc_doc` clearContents, a desktop/clipboard/RuntimeUID probe before the udprop write, and per-UNO-step traces inside `set_document_property`). `testing_runner` also arms `faulthandler.dump_traceback_later(90, repeat=True)` to **stderr** (all Python threads) for that test only and cancels it on `TEST end` — not gdb; in-process stacks only; not gated on `WRITERAGENT_CI_DEBUG`. GHA 33707990007 hung in calc native-doc wipe on `doc.isReadonly()` via `set_document_scripts` after `clearContents`; `_clear_writeragent_udprops` writes `DOCUMENT_SCRIPTS_UDPROP` with `set_document_property` and does not call `isReadonly` (do not skip that write — GHA 33763078357 relocated the hang to `getDocumentProperties()`).
- **Use when**: The model needs **mixed character formatting in one cell**; plain **`write_formula_range` / `set_string`** cannot express that.
- **Limits**: **One cell** per call; **no** embedded images/OLE; math-in-HTML is not a goal for Calc. Callers must supply a **valid UNO component context** on `ToolContext.ctx` (in-process tests and the sidebar pass this; a `None` context can break `get_desktop` in some embed scenarios).

### Consolidated Chart Tool (`manage_charts`)

Charts are a single specialized-domain tool: **`manage_charts`** ([`plugin/calc/charts.py`](../../plugin/calc/charts.py)). Skinny list/info/upsert/delete classes are Dummy backends for its `action` dispatcher (hidden from LLM/MCP lists and the scripting API).

- **Tiers and Visibility**: **`tier = specialized`**, `domain = charts` on Calc (`ToolCalcChartBase`), Writer (`ToolWriterChartBase`), and Draw (`ToolDrawChartBase`) — same pattern as shapes (`shape_upsert`). One registry slot per name (last module load wins); union `uno_services` on Writer/Draw wrappers. **All apps** use `delegate_to_specialized_{calc|writer|draw}_toolset(domain="charts")`, not the main chat list. Per-app core tier (Calc-only) needs registry multi-bind — see `ManageCharts` docstring in [`charts.py`](../../plugin/calc/charts.py).
- **Style and Color Support**: Supports arbitrary background color (`bg_color`) and data series color styling (`colors` array or single `color` / `series_color` string). Colors are parsed dynamically and can be CSS/X11 names (e.g., `green`, `darkgreen`, `yellow`), hex values with or without the `#` prefix (e.g., `#0f0`, `#00FF00`, `00ff00`), or functional RGB/RGBA syntax (e.g., `rgba(255, 0, 0, 0.5)`).
- **Mechanism**: Mandatory `action` (`"list"`, `"get_info"`, `"create"`, `"edit"`, `"delete"`) routes to the Dummy backend classes.

### Formulas & Range Writing API Comparison (`write_formula_range` vs. `set_cell_formula`)

WriterAgent's `write_formula_range` tool takes a different design approach than Collabora's `set_cell_formula` tool:
- **WriterAgent (`write_formula_range`)**: **Range-centric bulk writing**. Operates directly via programmatic PyUNO `setDataArray()` and `setFormulaArray()`. Accepts structured rectangular datasets (tuples, 2D arrays, or raw CSV blocks) and commits them in a single transaction. This is highly efficient for generating tables and does not affect the active UI cursor selection.
- **Collabora (`set_cell_formula`)**: **Coordinate-centric scattered writing**. Sequentially dispatches `.uno:GoToCell` and `.uno:EnterString` for each discrete `{cell, formula}` pair. While flexible for scattered cells, it is highly verbose for LLMs to generate, performs expensive UI updates, and hijacks the user's cursor selection.
- **Decision & Parity Plan**: We retain the range-centric PyUNO API for performance and cursor/focus safety. In the future, we plan to adopt Collabora's capability for scattered updates by supporting a batch parameter in `write_formula_range` or adding a dedicated `write_cell_batch` tool that retrieves and updates discrete `XCell` objects programmatically, without utilizing UI dispatches.

---

## 3. Implementation status and roadmap

### 3.1 Current implementation

| Domain / area | WriterAgent status | Module & tools | Notes |
|---------------|--------------------|----------------|-------|
| **Cells** | ✅ Implemented | `cells.py`: `read_cell_range`, `write_formula_range`, `set_style`, `insert_cell_html` ([`rich_html.py`](../../plugin/calc/rich_html.py)), merge/sort/delete helpers | Basic range + style + **HTML → rich text in one cell** ([§ Rich HTML in a single cell](#rich-html-in-a-single-cell)). Full read/write date & time lifecycle (ISO + duration `PT…` wire): [date-time-handling.md](date-time-handling.md). `read_cell_range` returns ISO/`PTnHnMnS` in `value` with `type` / `format_category`; `write_formula_range` ingests the same shapes as real Calc serials. |
| **Ranges** | ✅ Implemented | `cells.py`: Get/SetRangeValues, Get/SetRangeFormulas | — |
| **Sheets** | ✅ Implemented | `sheets.py`, `sheet_filter.py`: ListSheets, CreateSheet, SwitchSheet, GetSheetSummary, `apply_sheet_filter`, `clear_sheet_filter`, `get_sheet_filter` | Basic sheet ops + AutoFilter |
| **Formulas & Error Detection** | ✅ Implemented | `cells.py` (`write_formula_range` + compound undo); `list_calc_functions` / `evaluate_formula`; `detect_and_explain_errors` ([`calc_error_detector.py`](../../plugin/calc/error_detector.py)); `FormulaDepChain` ([`formula_dep_chain.py`](../../plugin/calc/formula_dep_chain.py)) | Formula evaluation, error explanation (`#N/A`, `#VALUE!`, `#REF!`, `#NAME?`), dependency tracking |
| **Charts** | ✅ Implemented | `charts.py`: `manage_charts` only (shared with Writer and Draw; skinny classes are Dummy backends) | Fat API: `list`, `get_info`, `create`, `edit`, `delete` actions |
| **Named Ranges** | ✅ Implemented | [`named_ranges.py`](../../plugin/calc/named_ranges.py): `named_range_list`, `named_range_get_info`, `named_range_add`, `named_range_edit`, `named_range_delete`, `named_range_create_from_titles` | Domain-first naming. Global and sheet-local scopes, `NamedRangeFlag` bitmasks (print area, filter criteria), base position parsing, dynamic formulas, header-based creation (`Border.TOP/BOTTOM/LEFT/RIGHT`), and transparent resolution in `read_cell_range` / `write_formula_range`. |
| **Data Validation** | ✅ Implemented | `validation.py`: SetDataValidation, GetDataValidationRules | Specialized tier |
| **Conditional Formatting** | ✅ Implemented | [`conditional.py`](../../plugin/calc/conditional.py): `add_conditional_format`, `list_conditional_formats`, `remove_conditional_formats` — [UNO / roadmap](conditional-formatting.md) | Specialized tier |
| **Analysis** | Hidden from chat | [`analysis.py`](../../plugin/calc/analysis.py) etc. | LLM tools are `ToolBaseDummy`. Calc chat compute is `=PY()` (live formula into empty cells). Helpers remain for Run Python Script / tests. |
| **Pivot Tables** | ✅ Implemented | `pivot.py`: CreatePivotTable, RefreshPivotTable, GetPivotTableData, ListPivotTables | Specialized tier |
| **Tables** | ✅ Implemented | `tables.py`: CreateTable, GetTableInfo, SetTableStyle | — |
| **Shapes** | ✅ Implemented | `shapes.py`: Create/Edit/DeleteShape (shared with Writer/Draw) | — |
| **Comments** | ✅ Implemented | `comments.py`: ListCellComments, AddCellComment, DeleteCellComment | Specialized tier |
| **Forms** | ✅ Implemented | `forms.py`: FormCreate, FormGenerate, FormListControls, FormCreateControl, FormEditControl, FormDeleteControl (shared with Writer) | Specialized tier |

### 3.2 Future enhancements (roadmap)

| Feature | Status | Notes |
|---------|--------|-------|
| **Macros** | ❌ Not implemented | Macro recording/execution, VBA compatibility |
| **Goal Seek** | ✅ Implemented | Target value analysis |
| **Solver** | ✅ Implemented | Optimization scenarios, constraint solving |
| **Scenarios** | ❌ Not implemented | Scenario manager, what-if analysis |
| **Data Tables** | ❌ Not implemented | One-way and two-way data tables |
| **External Data** | ❌ Not implemented | Database connections, SQL queries, web queries |
| **Advanced Forms** | ❌ Not implemented | Advanced form features, database integration, complex validation |
| **Advanced Chart Features** | ✅ Partial | Trend lines, error bars, secondary axes |
| **Pivot Chart Creation** | ❌ Not implemented | Direct pivot chart creation from data |
| **Dynamic Named Ranges** | ✅ Implemented | Formula-based range definitions and sheet-scoped names supported |
| **Array Formulas** | ✅ Partial | Basic support, matrix operations TBD |
| **Structured References** | ❌ Not implemented | Table-based formula references |
| **Table Slicers** | ❌ Not implemented | Interactive filtering controls |
| **Sheet Protection** | ❌ Not implemented | Cell/range locking, password protection |
| **Change Tracking** | ❌ Not implemented | Collaborative editing, comment history |

### 3.3 Cross-cutting improvements

- **MCP / API opt-in:** Config or query parameter to list `specialized` tools on `tools/list`
- **Performance tuning:** Timeouts and step limits for sub-agent execution
- **Telemetry:** Track domain usage to prioritize development
- **Documentation:** Keep [`AGENTS.md`](../../AGENTS.md) synchronized

For testing and operations details, see the [Writer documentation](../writer/specialized-toolsets.md#4-testing-and-operations).

---

## 4. Summary

| Concern | Mechanism |
|---------|-----------|
| Smaller default tool list | `exclude_tiers` default in `ToolRegistry.get_tools` / `get_schemas` |
| Domain grouping | `ToolCalc*Base.specialized_domain` + `tier = "specialized"` |
| User/model entry point | `delegate_to_specialized_calc_toolset` (`tier = "core"`, async) |
| Sub-agent completion | `final_answer` (`tier = "specialized_control"`) |
| Prompt teaching | `CALC_SPECIALIZED_DELEGATION_TEMPLATE` in `plugin/framework/prompts.py` |
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
