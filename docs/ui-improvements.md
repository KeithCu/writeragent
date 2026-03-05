# AGENTS.md Update Plan: Step-by-Step Roadmap to Amazing UX

Based on my research of the codebase (main.py, chat_panel.py, core/document.py, core/format_support.py, document_tools.py, core/calc_tools.py, constants.py), here's a focused plan to elevate LocalWriter's user experience. I've prioritized adding Draw support (completing the core LibreOffice suite: Writer, Calc, Draw/Impress) and significant UX enhancements like safer workflows, predictive suggestions, and richer context. I've deprioritized codebase refactoring (e.g., no major structural changes) and kept features within LibreOffice's UNO/Cython/Python constraints.

## Phase 1: Add Draw Support (LibreOffice Draw Documents)
Draw documents (com.sun.star.drawing.DrawingDocument) are vector graphics pages with shapes, connectors, text boxes, etc. This adds tool-calling for editing, context for chat, and menu integration.

### Step 1.1: Define Draw Tools and System Prompt
- **Create `core/draw_tools.py`**: Mirror `core/calc_tools.py` structure. Define `DRAW_TOOLS` schemas for shape creation/editing (rectangles, lines, text boxes), positioning, styling (colors, fonts), and page management. Tools include:
  - `list_pages`: List pages by name/index.
  - `create_shape`: Add rectangle, ellipse, line, text box, connector (parameters: type, x/y/w/h, text, style).
  - `edit_shape`: Modify existing shape (select by name/ID, update position, text, style).
  - `delete_shape`: Remove shape(s) by name/ID or selection.
  - `get_draw_summary`: Describe pages, shape count, types, and text content.
  - `set_page_background`: Change page background color/size.
- **Implement helpers in `core/draw_bridge.py`**: Cross-file reuse (like `core/calc_bridge.py`). Add functions for page enumeration, shape access, and property manipulation (e.g., `get_pages()`, `get_shapes_on_page(page)`, `create_shape(shape_type, x, y, w, h, text=None)`).
- **Update `core/constants.py`**: Add `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT` with workflow (describe → plan edits → use tools → summarize). Group tools like Calc (SHAPES, PAGE MANAGEMENT, TEXT EDITING). Require semicolon syntax for params; instruct to edit step-by-step.
- **Python Library Reference**: None needed; use LibreOffice UNO interfaces (e.g., `com.sun.star.drawing.DrawingDocument`, `DrawPage`, `XShape`).

### Step 1.2: Context and Chat Integration
- **Extend context in `core/document.py`**: Detect Draw docs (`hasattr(model, "DrawPages")`). Add `get_draw_context_for_chat(model, max_context, ctx)`: Summarize pages, shapes (count by type), text content, active page, used area. Similar to `get_calc_context_for_chat`.
- **Update Chat System**: Modify `get_chat_system_prompt_for_document` to use Draw prompt for Draw docs. Update chat_panel.py and main.py to use DRAW_TOOLS and `execute_draw_tool` (new dispatcher in `core/draw_tools.py`).
- **Menu/Chat Support**: Extend `main.py` triggers (ExtendSelection, EditSelection, ChatWithDocument) to handle Draw docs (e.g., cell selection becomes shape selection). Use Draw analog to spreadsheet ranges. Update chat menu to work on shapes/text.

### Step 1.3: UI and Sidebar Recognition
- **Sidebar Activation**: Update `LocalWriterDialogs/ChatPanelDialog.xcu` to include `com.sun.star.drawing.DrawingDocument` in ContextList (like Writer/Calc).
- **Settings/Menus**: Ensure "LocalWriter → Settings" and menu items appear in Draw. Test Extend/Edit on selected shapes (populate prompt with shape text).
- **Testing**: Add "RunDrawTests" in main.py for integration tests (create shapes, edit text, verify context).
- **Estimated Effort**: 40-50 hours (tool schemas + UNO discovery + testing). Libraries: None additional.

### Step 1.4: Impress Support Extension (Since You Mentioned Draw/Impress)
- Impress (presentations) uses `com.sun.star.presentation.PresentationDocument`, similar to Draw but with slides (DrawPages). Reuse Draw tools with slide-specific extras (e.g., slide transitions, notes). Minimal delta: rename "pages" to "slides", add notes handling.

## Phase 2: Better Context Tools (Selection/Cursor/Metadata Awareness)
Enhance context to include more document details for richer chat and editing. Make selections smarter (include surrounding paragraphs, inline styles).

### Step 2.1: Expand Document Context Functions
- **Writer Metadata**: In `get_document_context_for_chat`, add: word count, paragraph count, fonts used, images/tables count, style stats (e.g., "H1: 5"). Detect selection type (text, table cell) and embed inline styles (bold/italic).
- **Calc Metadata**: In `get_calc_context_for_chat`, add: formulas count, charts count, errors count, column types (text/number/date). Include selection cell details (formula, dependents).
- **Draw Metadata**: In `get_draw_context_for_chat` (new), add: font families, color palette, connector count, average shape size.
- **Python Library Reference**: None; parse UNO objects for stats (e.g., enumerate paragraphs, query properties).

### Step 2.2: Dynamic Context Refresh
- On each chat send, always refresh context (already does first/last excerpts, selection markers). Add config option `context_include_metadata` (default: true) to include or omit stats for speed.
- **Estimated Effort**: 20 hours (UNO property queries + string building). UX Impact: More informative summaries (e.g., "Word count: 1200, Images: 3") help users guide AI (e.g., "ignore images" or "fix grammar in bold text").

## Phase 3: Safer Editing Workflows (Propose-First, Explicit Accept/Reject)
Change tool-calling to preview changes and ask for confirmation. Prevents accidental overwrites.

### Step 3.1: Propose Mode for Chat Tools
- **Modify Tool Execution**: In `chat_panel.py` tool loop, after AI proposes changes, display preview (e.g., "Propose: Replace 'old' with 'new' at positions... Accept/Reject?") instead of applying immediately. Use buttons or text commands (e.g., user types "accept" to proceed).
- **Preview Integration**: For Writer/Calc/Draw, generate human-readable diff (e.g., "Insert <new shape> at (100,50)" or "Change formula in B2 to =SUM(A1:A10)"). Store proposed changes in session.
- **Flags**: Add config `safe_edit_mode` (default: false for power users; true for conservative). In safe mode, tools explain changes and wait for "confirm" before execution.

### Step 3.2: Accept/Reject UI
- **Sidebar Enhancements**: Add "Accept Suggested" / "Reject" buttons in ChatPanelDialog.xdl (next to Send/Stop). Bind to new listeners; on accept, apply queued changes with undo grouping.
- **Chat Commands**: Allow text-based (e.g., "accept edit", "reject shape creation") for simplicity.
- **Estimated Effort**: 20-30 hours (UI controls, tool runner queuing, diff generation). UX Impact: Builds trust; users see "AI suggests replacing 5 cells" before applying.

## Phase 4: Predictive “Future Words” Suggestions to Speed Up Typing
Add autocomplete in chat input or document editing, predicting next words/phrases based on context.

### Step 4.1: Light Suggestion Model
- **Implement Suggester**: Create `core/suggestion_engine.py`. Use n-gram (trigram) model trained on user document textsnippet (last few paragraphs). On typing, suggest completions (e.g., for "The quick", suggest "brown fox").
- **Integration**: Hook into ChatPanel's query control (extend XTextListener or timer). On space, compute suggestions; display in dropdown or inline tooltip. For document editing, add optional "Auto-suggest on Selection" menu.
- **Data Source**: Build n-grams from recent context (1-2 pages). Cache in memory; rebuild on document changes.
- **Config**: `enable_suggestions` (default: true), `suggestion_max_length` (e.g., 10 chars).

### Step 4.2: Pay-As-You-Type in Sidebar
- **UI Addition**: Modify ChatPanelDialog.xdl: Add small suggestion box below query field, updating live.
- **Performance**: Async suggestion calc (don't block typing); limit to 3-5 suggestions.
- **Python Library Reference**: Use `nltk` or `collections.Counter` for n-grams (lightweight; ship nltk data subset if needed). NLP library (e.g., `spacy`) for sentence context, but keep minimal (~100KB addition).
- **Estimated Effort**: 25 hours (model training, UI integration, UNO event handling). UX Impact: Speeds chat; e.g., typing "Edit the" suggests "heading style".

## Phase 5: Reliability/Safety Foundations (Timeouts, Errors, Rollback)
Strengthen existing (timeouts via config; errors via show_error); add rollback grouping.

### Step 5.1: Undo Grouping for AI Edits
- **In Tool Execution**: Wrap changes in UNO undo context ((model.enterUndoContext("AI Edit")) ... leaveUndoContext()). If errors, use "AI Edit (Failed)" or partial.
- **All Paths**: Apply to Extend/Edit/Chat tools. Config `enable_undo_grouping` (default: true).
- **Estimated Effort**: 10 hours (UNO calls in wrappers).

### Step 5.2: Enhanced Error Handling
- **Clear Messages**: Improve `format_error_for_display` with suggestions (e.g., "Network timeout: Check endpoint; try 'localhost:5000' for local Ollama").
- **Hang Detection**: Beef up watchdog; on hang, show "Force Stop?" prompt.
- **Estimated Effort**: 5-10 hours (message templating).

### Step 5.3: Config for Safety
- Add SettingsDialog fields: `safe_edit_mode`, `enable_suggestions`, `enable_undo_grouping`, `context_include_metadata`.
- Ensure all new features respect them.
- **Estimated Effort**: 5 hours (update settings UI/build).

## Overall Timeline and Dependencies
- **Total Estimated Effort**: 150-200 hours (spread over 2-3 months; parallelizable).
- **Priority**: Phase 1 first (core completeness), then 2/3/4/5 (UX polish).
- **Testing**: Add integration tests (e.g., in main.py) for each; use existing calc_tests as template.
- **No Libraries Added Initially**: Rely on Pure Python + UNO; evaluate `nltk` for suggestions if 100KB ok.
- **Backwards Compatibility**: All changes additive; existing config keys preserved.

This plan focuses UX only, as requested. Ready to implement! Start with Phase 1?