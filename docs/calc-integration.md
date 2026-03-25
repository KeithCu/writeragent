---
name: Calc Support from LibreCalc AI
overview: "Calc support (chat/tools) is now implemented in LocalWriter, reusing and adapting the in-process Calc UNO layer, tool set, and error detector from libre_calc_ai-1.0.2."
status: "COMPLETE (Feb 2026)"
todos: []
isProject: false
---

Calc support (chat/tools) is now fully integrated into LocalWriter. We have reused the following components from [libre_calc_ai-1.0.2](libre_calc_ai-1.0.2/) in-process, translated and adapted for LocalWriter's architecture.

---

## IMPLEMENTATION SUMMARY (Feb 2026)

All planned modules are implemented in `core/` and integrated into the **Chat Sidebar** and **LibreOffice Menu**.

- **Core Calc Logic**: Ported and translated `calc_bridge.py`, `calc_address_utils.py`, `calc_inspector.py`, `calc_sheet_analyzer.py`, `calc_error_detector.py`, and `calc_manipulator.py`.
- **AI Toolset**: `CALC_TOOLS` in `core/calc_tools.py` (read ranges, write formulas, format, merge, sheet management, chart creation, detect_and_explain_errors).
- **Dynamic Tool Loading**: `chat_panel.py` detects document type and switches between `WRITER_TOOLS` and `CALC_TOOLS`.
- **Calc Chat Context**: `get_calc_context_for_chat(model, max_context, ctx)` in `core/document.py` provides a summary of the active sheet and selection. **Requires `ctx`** (component context); callers pass it from the panel or MainJob so we never use `uno.getComponentContext()` in this path.
- **Robust prompt selection**: `get_chat_system_prompt_for_document(model, additional_instructions)` in `core/constants.py` is the single source of truth for the chat system prompt. It returns `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` for Calc and `DEFAULT_CHAT_SYSTEM_PROMPT` for Writer, so Writer/Calc prompts cannot be mixed. Used by `chat_panel.py` and `main.py` for both sidebar and menu Chat.
- **Calc System Prompt**: `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` in `core/constants.py` states semicolon formula syntax, a 4-step workflow (understand → get state if needed → use tools → short confirmation), and tools grouped by use (READ / WRITE & FORMAT / SHEET MANAGEMENT / CHART / ERRORS). Structure inspired by libre_calc_ai `prompt_templates.py` (workflow, grouped tools, “do not explain—do the operation”).
- **Menu**: "Chat with Document" for Calc in `main.py` (streams response to "AI Response" sheet; uses same prompt helper and `ctx` for context).
- **Tests**: `tests/test_calc_address_utils.py` and `core/calc_tests.py`.

---

## 1. In-process “bridge”: document from ctx

**Do not use:** Their socket/pipe `connect()` and BridgeServer/BridgeClient.

**Use:** The same abstraction they use when running inside LO: get document/sheet/cell from the current component context.

- **Source:** [CalcAI/core/uno_bridge.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/core/uno_bridge.py) (lines 155–217 and helpers).
- **Status:** **IMPLEMENTED** in `core/calc_bridge.py`.
- **How it works:** Uses `XSCRIPTCONTEXT` or `officehelper.bootstrap()` to get the UNO context. Exposes `get_active_document()`, `get_active_sheet()`, etc.

---

## 2. Address utilities (copy or merge)

**Source:** [CalcAI/core/address_utils.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/core/address_utils.py)

- **Status:** **IMPLEMENTED** in `core/calc_address_utils.py`.
- **Action:** Pure Python address handling for A1/range parsing.

---

## 3. Cell inspector (read operations)

**Source:** [CalcAI/core/cell_inspector.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/core/cell_inspector.py)

- **Status:** **IMPLEMENTED** in `core/calc_inspector.py`.
- **Action:** Ported all analytical methods and translated Turkish comments/docstrings.

---

## 4. Sheet analyzer

**Source:** [CalcAI/core/sheet_analyzer.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/core/sheet_analyzer.py)

- **Status:** **IMPLEMENTED** in `core/calc_sheet_analyzer.py`.
- **Action:** Used to build the AI context in `core/document.py:get_calc_context_for_chat()`.

---

## 5. Error detector (formula errors)

**Source:** [CalcAI/core/error_detector.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/core/error_detector.py)

- **Status:** **IMPLEMENTED** in `core/calc_error_detector.py`.
- **Action:** Full error code mapping ported and translated. Exposed as `detect_and_explain_errors` tool.

---

## 6. Cell manipulator (write / format / structure)

**Source:** [CalcAI/core/cell_manipulator.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/core/cell_manipulator.py)

- **Status:** **IMPLEMENTED** in `core/calc_manipulator.py`.
- **Action:** Tier 1 and 2 methods implemented. Added Tier 3 `create_chart` support.

---

## 7. Tool definitions and dispatcher

**Source:** [CalcAI/llm/tool_definitions.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/llm/tool_definitions.py)

- **Status:** **IMPLEMENTED** in `core/calc_tools.py`.
- **Action:** Defined `CALC_TOOLS` and `execute_calc_tool` dispatcher. Integrated with `chat_panel.py` and `main.py`.

---

## 8. System prompt for Calc

**Source:** [CalcAI/llm/prompt_templates.py](libre_calc_ai-1.0.2/Scripts/python/CalcAI/llm/prompt_templates.py)

- **Status:** **IMPLEMENTED** in [core/constants.py](core/constants.py).
- **Implementation:** `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` plus `get_chat_system_prompt_for_document(model, additional_instructions)` so the correct prompt is chosen by document type (Calc vs Writer). The Calc prompt includes:
  - “Do not explain—do the operation directly using tools” and “Perform as many steps as needed in one turn when possible.”
  - A 4-step **WORKFLOW**: understand → get state (get_sheet_summary/read_cell_range) if needed → use tools → short confirmation (mention cell/range addresses when changing).
  - **FORMULA SYNTAX**: Semicolon (;) as argument separator; correct vs wrong examples.
  - **TOOLS** grouped by use: READ / WRITE & FORMAT / SHEET MANAGEMENT / CHART / ERRORS (only tools we expose).
- Structure inspired by libre_calc_ai’s prompt_templates (workflow, grouped tools).

---

## 9. Context for each Calc request

**Source:** interface.py `_build_context_func` and sheet_analyzer usage.

- **Status:** **IMPLEMENTED** in `core/document.py`: `get_calc_context_for_chat(model, max_context, ctx)`.
- **Shape:** Builds a string with document URL, active sheet name, used range (rows × columns), column headers, current selection range, and (for small selections) selection content. Uses `SheetAnalyzer.get_sheet_summary()` and selection from the controller.
- **Context parameter:** `ctx` is **required** (component context from panel or MainJob). No `uno.getComponentContext()` in this path. `get_document_context_for_chat(..., ctx=None)` requires `ctx` when the document is Calc.

---

## 10. Integration with LocalWriter (no bridge)

- **Entry point:** Chat from Calc uses the same sidebar/menu as Writer; `ctx` comes from the UNO component (panel or MainJob). Both pass `ctx` into `get_document_context_for_chat` so the extension context is always used.
- **Single process:** All Calc code runs in LO’s Python. No BridgeServer, BridgeClient, or subprocess.
- **LlmClient:** Reuse [core/api.py](core/api.py) (streaming, tool-calling, reasoning). Pass **CALC_TOOLS** and `execute_calc_tool` when the active document is a spreadsheet.
- **UI:** Same sidebar (LocalWriter deck) and menu for Writer and Calc; ContextList includes `com.sun.star.sheet.SpreadsheetDocument`. Response area + input + Send/Stop. No PyQt5.
- **Undo:** Undo grouping for AI edits is out of scope for now (Writer has the same limitation).

---

## 11. Suggested file layout (when you add Calc)

```
core/
  calc_address_utils.py   # address_utils (from libre_calc_ai)
  calc_bridge.py          # thin in-process get_active_document/sheet/cell/selection
  calc_inspector.py       # CellInspector-style read (read_range, get_cell_details, ...)
  calc_sheet_analyzer.py  # get_sheet_summary, optional detect_data_regions
  calc_error_detector.py  # detect_and_explain_errors
  calc_manipulator.py     # write_formula, set_cell_style, merge_cells, sort_range, ...
  calc_tools.py           # CALC_TOOLS (schemas) + execute_calc_tool / CalcToolDispatcher
```

Optional: a single `core/calc.py` that re-exports the public API (get_calc_context, get_sheet_summary, execute_calc_tool, CALC_TOOLS) so chat_panel or a Calc panel only imports from one place.

---

## 12. What not to take from libre_calc_ai

- **interface.py** (script entry, subprocess launch, bridge server): Not needed; LocalWriter uses UNO service/sidebar.
- **BridgeServer / BridgeClient:** Not needed; everything in-process.
- **PyQt5 UI (main_window, chat_widget, settings_dialog):** Not needed; use LO sidebar/XDL.
- **Their config (settings.json, .env, multi-provider):** Keep LocalWriter config (localwriter.json, single endpoint); optional env overrides only if you want.
- **event_listener.py:** Only needed if you want “auto-refresh on document change”; can be added later with UNO listeners in-process.

---

- **Status:** **DONE**. Integrated into `main.py` and `chat_panel.py`.
- **Order of Implementation (Actual):**
  1. Ported and translated core logic (`calc_bridge` to `calc_manipulator`).
  2. Integrated `get_calc_context_for_chat` in `core/document.py`.
  3. Created `core/calc_tools.py` with schemas and dispatcher.
  4. Updated `chat_panel.py` for document type detection and tool swapping.
  5. Updated `main.py` for menu support and response-to-sheet flow.
  6. Added tests in `tests/test_calc_address_utils.py` and `core/calc_tests.py`.
  7. **Robustness:** Added `get_chat_system_prompt_for_document()` in `core/constants.py`; all chat entry points use it so Writer/Calc prompt cannot be mixed. Added required `ctx` parameter to `get_document_context_for_chat` and `get_calc_context_for_chat`; callers pass component context (no `uno.getComponentContext()` in this path).
  8. **Calc system prompt:** Added `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` with workflow, grouped tools, and formula syntax; structure inspired by libre_calc_ai `prompt_templates.py`.
Based on my analysis of your Calc tool-calling API implementation in `core/calc_tools.py`, `core/calc_bridge.py`, `core/calc_manipulator.py`, `core/calc_inspector.py`, `core/calc_sheet_analyzer.py`, `core/calc_error_detector.py`, and related files, I'll summarize what's currently implemented and suggest targeted improvements to make it better, more robust, and more capable.

### Current Strengths
- **Comprehensive coverage**: You have a solid foundation covering most common Calc operations (reading, writing, formatting,_sheet management, charts, error detection, sorting, clearing).
- **Modular architecture**: Clean separation of concerns (`Bridge` for low-level UNO, `Inspector` for reading, `Manipulator` for writing, `Analyzer` for structure, `Detector` for errors).
- **Context-aware system prompt**: The `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` in `core/constants.py` effectively guides the AI to use tools properly, reminds about semicolon syntax, and structures workflow (understand → read → use tools → confirm).
- **Performance optimizations**: Persistent HTTP connections, global shared instances for tools (avoiding recreation per call), and batch-friendly logic in many places.
- **Error handling**: `format_error_for_display` for user-friendly messages, detailed error explanations in `ErrorDetector`, and exception catching throughout.
- **Calc-specific context**: `get_calc_context_for_chat` provides basic sheet summary (name, range, headers, selection), which is a good start for relevant context without overwhelming tokens.

### Areas for Improvement

#### 1. **Enhanced Tool Coverage and Capabilities**
Your current 11 tools cover the essentials, but expanding to more advanced operations would make the AI more powerful. Prioritized additions:

- **Data Analysis Tools**:
  - `add_auto_filter(range_str, has_header=True)`: Enable/disable filters on ranges (building on your existing `set_auto_filter` logic in `CellManipulator`).
  - `start_data_pivot(range_str, target_cell)`: Create pivot tables with drag-and-drop configuration (target_cell for where to place the pivot).
  - `apply_conditional_formatting(range_str, rule_type, condition, style)`: Add color scales, icon sets, or data bars (e.g., rule_type="color_scale", condition="values_above_threshold", style={"min_color": "#FFA500"}).

- **Advanced Manipulation**:
  - `insert_columns(position, count=1)` and `insert_rows(position, count=1)`: Already in `CellManipulator`, but add to `CALC_TOOLS` schema for AI access.
  - `delete_columns(col_letter, count=1)` and `delete_rows(row_num, count=1)`: Also already implemented—expose them.
  - `set_column_width(col, width_mm)` and `set_row_height(row, height_mm)`: In `CellManipulator` but not exposed.
  - `auto_fit_columns(range_str)`: Analogous to existing `auto_fit_column`, but ranged.
  - `copy_paste_range(source_range, target_cell, operation="copy"|"cut")`: Extend copy_range for cut/paste.
  - `import_csv(file_path, delimiter, target_cell)` and `export_csv(range_str, file_path)`: For data import/export.

- **Validation and Advanced Formatting**:
  - `add_data_validation(range_str, type, condition)`: E.g., "list", "whole_number", "date" with min/max/conditions.
  - `group_rows(range_str)` / `ungroup_rows`: For outlining collapsible groups.
  - `freeze_panes(row, col)`: Freeze/split panes at specific row/column.

- **Statistical/Business Analysis**:
  - `calculate_statistics(range_str)`: Mean, median, std dev, quartiles, etc., above your column-level `column_statistics`.
  - `create_custom_chart(type, data, labels, axes_customization)`: Extend your `create_chart` with more options (e.g., multi-axis, stacked).

**Implementation Notes**: Most of these build directly on your existing `CellManipulator` and `CalcBridge` classes. For new complex features like pivots/charts, leverage LibreOffice's API (e.g., `DatabaseRange` for filters/pivots, `XChartDocument` for advanced charting). Expose them in `CALC_TOOLS` with clear schemas (e.g., enum-validation for types).

#### 2. **Tool Usability and AI Guidance Improvements**
- **Smarter Parameters and Defaults**:
  - For tools like `sort_range`, add optional `orientation="rows"` (default) or `"columns"` (sort by rows).
  - For `create_chart`, enhance to support primary/secondary axes and custom colors.
  - Add response details: After `write_formula`, include the computed result (e.g., "Total: 123.45" if it's a formula).

- **Composited/Batch Tools**: Reduce tool-call chain length for common workflows.
  - `create_table(range_str, headers_list, has_borders=True)`: Combine `write_formula` for headers, `set_cell_style` for borders, `merge_cells` for multi-column headers.
  - `format_range(range_str, preset="header"|"data"|"total")`: Apply predefined style combos (e.g., header=bold+centered, total=bold+border).

- **Better Error Recovery**:
  - Tools should validate inputs (e.g., check if range exists before operations) and provide actionable feedback.
  - Add `undo_last_tool()` or `revert_range(range_str)` using LibreOffice's undo API (model's `XUndoManager`).

#### 3. **Enhanced Context and Memory for Better AI Performance**
- **Richer Document Context** (`get_calc_context_for_chat`):
  - Include a small preview of data (first 5-10 rows of used range) for AI understanding—analogous to Writer's excerpts.
  - Add sheet-level summary: % of empty cells, data types (numeric/text/formula ratios), last modified regions.
  - Track "working memory": In the chat panel, cache last 3 operations (e.g., "Wrote formulas in A1:A5") and include in context.

- **Tool History Integration**: The AI forgets intermediate steps; enhance prompts to remind the AI of multi-step plans (e.g., "Don't forget to merge headers after writing them.").

#### 4. **Robustness and Error Handling**
- **Validation Layer**: Before executing tools, validate ranges (e.g., via `SheetAnalyzer.detect_data_regions()` to ensure the range is coherent).
- **Unsafe Operations**: For destructive tools like `clear_range`, require confirmation (add `confirm=True` parameter, defaulting to True for safety).
- **Fallbacks**: If a visual operation fails (e.g., chart creation due to no data), explain why and suggest alternatives instead of just throwing.
- **Cross-Session State**: Your global `_get_tools` instances are good, but add loading/saving of sheet state for resumed sessions.

#### 5. **Performance and Scaling**
- **Lazy Reading**: For large ranges, `read_cell_range` should page results or sample (e.g., max 1000 cells; warn if truncated).
- **Bulk Operations**: Instead of single `write_formula` calls in loops, add `write_formula_batch(cell_values_dict)` for writing multiple cells in one go.
- **Caching**: Cache sheet summaries and headers for 30s to avoid redundant UNO calls in rapid tool chains.

#### 6. **Integration with LibreOffice Ecosystem**
- **Sheet-Level Locking**: For multi-user scenarios (rare in Calc), use `XSheetOperation.lockRange()` during tool execution.
- **Extension Compatibility**: Test with Calc add-ins (e.g., ensure your tools work alongside statistical extensions).
- **Localization**: Your error messages assume English; make them locale-aware if expanding.

### Implementation Plan and Next Steps
1. **Prioritize and Prototype**: Start with easy wins like exposing `insert_rows/columns` and `set_column_width` (already coded). Then pivot tables, as they're common requests.
2. **Update `CALC_TOOLS` Schema**: Ensure new tools have detailed descriptions and examples in schemas for better AI usage.
3. **Test Thoroughly**: Use your existing `calc_tests.py` and add scenarios for edge cases (merged cells, weird ranges).
4. **System Prompt Refinement**: Update `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` to include instructions for new tools (e.g., "Use insert_rows/delete_columns for structure changes.").
5. **Metrics and Logging**: Add `agent_log` calls in new tools for debugging (e.g., log successful range operations with sizes).


#### Bulk Operations and CSV Import (Completed)

**Goal**: Address inefficiencies in handling large datasets and multi-cell formatting. Reduces AI tool-call overhead for common tasks.

**Completed/Fixed**:
- Fixed `set_cell_style` to properly handle range formatting (single method now detects ranges and delegates to `set_range_style`). No more one-by-one AI calls for ranges.
- Range number formats now apply to entire range (new `set_range_number_format` method).
- Exposed `delete_rows`, `delete_columns` (backend methods existed; now full tools). Insert tools removed as CSV import provides better bulk data insertion.
- Added `write_formula_range` tool: Efficiently writes formulas or values to ranges. Accepts single value for entire range or array for individual cells.
- Enhanced `write_formula_range` tool: Parses CSV string (e.g., "Name,Age\nAlice,30\nBob,25") and bulk-inserts into sheet starting at a cell. No file I/O required—ideal for AI-generated or pasted data.
- The method handles text/number detection automatically, supports custom delimiters (',', ';', etc.), and provides error handling for malformed CSV.
- Updated `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` to reference new tools: "Use `write_formula_range` for bulk writes or bulk CSV data inserts. `set_cell_style` works on ranges (e.g. 'A1:D10') for efficient formatting."

**Next Steps** (Future Enhancements):
- Add `write_formula_batch` for bulk cell writes (dict of cell->value) to avoid loops.
- Enhance `read_cell_range` with paging (e.g., `max_cells: int` parameter, warn if truncated) for large ranges.












Overall, your implementation is already quite capable—users can perform table creation, calculations, formatting, and charts. These enhancements would elevate it to handle complex data analysis and formatting tasks, making the AI assistant more proactive and less error-prone. If you'd like assistance implementing specific tools or testing, let me know!