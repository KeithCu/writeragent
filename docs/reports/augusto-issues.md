# Augusto Zolet Fork Issues Status Report

Assessment of open issues #1–#7 on [`Augusto-Zolet/writeragent`](https://github.com/Augusto-Zolet/writeragent/issues) against current `master` of `KeithCu/writeragent`.

---

## Issue 1: `page_get_style_properties` fails with `TypeError` on page style `Standard`

- **Upstream issue:** `Augusto-Zolet/writeragent#1` (`[test] page_get_style_properties fails with TypeError on page style Standard`)
- **Status:** **fixed**
- **Evidence:**
  - Fixed in PR #1302 (`19706702b`, `Merge pull request #1302 from Augusto-Zolet/office-bug-fixes`). When headers or footers are off (the default on a new Writer document), LibreOffice returns `None` for `HeaderHeight`, `FooterHeight`, `HeaderBodyDistance`, and `FooterBodyDistance`, so dividing by `100.0` raised `TypeError`.
  - Preserved in current `master` in `get_page_style_properties` (`plugin/writer/page.py:570-571`), which maps `None` directly to `None` instead of dividing by `100.0`.
  - Covered by `test_get_page_style_properties_without_header_or_footer` in `tests/writer/test_page.py` (1 passed in `.venv/bin/pytest tests/writer/test_page.py -k test_get_page_style_properties_without_header_or_footer`).
- **What remains:** Nothing.
- **Remaining size:** —

---

## Issue 2: No tool to open or convert a new document; venv script sandbox blocks `subprocess`

- **Upstream issue:** `Augusto-Zolet/writeragent#2`
- **Status:** **fixed** (by `document_open`, `document_save`, and `document_export` in `plugin/doc/document_file_tools.py`)
- **Evidence:**
  - `document_list_open` (renamed from `list_open_documents` in PR #3582, `8cf1604b2`, at `plugin/doc/document_research_tools.py:75`) lists open documents (including `read_only`), and points to `document_save` and `document_open`.
  - `open_document_for_read` (`plugin/doc/document_research.py:563`) only opens documents hidden/read-only for `document_research` tools and closes them afterward via `close_document_research_document`. No LLM tool exists to open a document in LibreOffice for editing or to convert a document between formats (e.g., `.odt` to `.docx`).
  - In the Python venv sandbox (`plugin/scripting/sandbox.py:367`, `PROCESS_MODULES`), `subprocess`, `multiprocessing`, and `pty` remain blocked by default (`plugin/scripting/module.yaml:135-140`, `python_allow_os` defaults to `false`). Users can opt in via `python_allow_os` or `python_extra_modules` in extension settings, which unblocks `subprocess`, though as noted in the issue comments, allowing arbitrary `subprocess` calls by default is a security risk compared to a dedicated open/convert tool.
- **What remains:** Add an explicit document open / format-conversion tool (or safe scoped action) rather than relying on `subprocess` in the Python sandbox.
- **Remaining size:** M

---

## Issue 3: Pasted images (`GraphicObjectShape`) listed by `get_page_objects` cannot be deleted by any tool

- **Upstream issue:** `Augusto-Zolet/writeragent#3`
- **Status:** **fixed**
- **Evidence:**
  - `shape_delete` (`plugin/draw/shapes.py:1358-1401`, via `_AnyDocumentShapeTool` with `doc_types = ["writer", "calc", "draw", "impress"]` and `_SingleDrawPageContainer` in `plugin/draw/bridge.py:38-52`) works on Writer's draw page (`page=0` or omitted) and removes shapes by `index` or `name` via `parent.remove(shape)`. Deleting by draw-page `index` also avoids the duplicate-name ambiguity described in the issue.
  - `_writer_objects_section` (`plugin/doc/document_helpers.py:149-165`, added in PR #3639, `4e73464f0`) lists Writer draw-page objects (`Objects (index, kind, name, anchor):`) with their draw-page index and name via `plugin.draw.tree.shape_summary_line`.
  - `shape_summary` (which crashed with `position cannot be determined with this method` and `Invalid page index: 7`) has been removed; `page_get_objects` (`plugin/writer/structural.py:88-273`, renamed from `get_page_objects` in PR #3582) reads `HoriOrientPosition`/`VertOrientPosition` before falling back to `getPosition()`.
  - On Writer, `list_graphic_objects` (`plugin/doc/visual_helpers.py`) now scans `doc.getDrawPage()` for `com.sun.star.drawing.GraphicObjectShape` as well as `doc.getGraphicObjects()`, and attaches each object's `shape_index`. `image_list` includes draw-page pictures with `shape_index`, `image_delete` and the other image tools resolve them by name, and duplicate names raise `AMBIGUOUS_NAME` listing the draw-page indexes and directing to `shape_delete(index=N)` without deleting anything.
- **What remains:** Nothing.
- **Remaining size:** —

---

## Issue 4: No tool to open a `.docx` file or copy a template file from within the session

- **Upstream issue:** `Augusto-Zolet/writeragent#4`
- **Status:** **fixed** (by `document_open(path, as_template=True)` and `document_save` in `plugin/doc/document_file_tools.py`)
- **Evidence:**
  - As with Issue #2, WriterAgent has no tool to copy a template file or open a `.docx` file into an editable LibreOffice window (`plugin/doc/document_research_tools.py`, `plugin/doc/document_research.py`).
  - In the Python sandbox, `python_allow_files` (`plugin/scripting/module.yaml:124-130`, `plugin/scripting/sandbox.py:400`) defaults to `false`, blocking `open`, `io`, `shutil`, `pathlib`, and `os` file operations unless the user explicitly enables `python_allow_files` in settings.
  - (`get_device_info` / `device_bash` mentioned in the issue belong to the external MCP host/client environment, not `plugin/`.)
- **What remains:** Add a safe file-copy/template-instantiation and open-document tool (or document the opt-in `python_allow_files` setting if scripting is intended for file copies, paired with a document-open tool).
- **Remaining size:** M

---

## Issue 5: Writer text tools missing from the session when the conversation started with a Calc document active

- **Upstream issue:** `Augusto-Zolet/writeragent#5`
- **Status:** **still open**
- **Evidence:**
  - In `plugin/mcp_server/mcp_protocol.py:714-726` and `999-1026` (`_schemas_for` / `_add_other_open_doc_schemas`, PR #872 `8d28d22b9`), `tools/list` *does* broaden to include tools for all currently open document types when `tools/list` is called.
  - However, `initialize_result` in `plugin/mcp_server/wire_types.py:133-136` advertises `"capabilities": {"tools": {"listChanged": False}, ...}`, and `plugin/mcp_server/mcp_protocol.py` has no `notifications/tools/list_changed` emitter when a new document type (such as Writer after Calc) is opened.
  - MCP clients that fetch and cache `tools/list` once at session start (and perform client-side tool search over that cached list) never re-fetch `tools/list` when a Writer document is opened later in the session, while `manual_read` (`get_guidance` in the issue) is a common tool that inspects the active document dynamically at call time.
- **What remains:** Either advertise `"listChanged": True` and emit `notifications/tools/list_changed` when open document types change, or expose cross-app core tools in `tools/list` from session start (or via `tool_find` in `direct_discovery` mode).
- **Remaining size:** M

---

## Issue 6: No tool to change the font of direct-formatted text or the whole document

- **Upstream issue:** `Augusto-Zolet/writeragent#6`
- **Status:** **still open**
- **Evidence:**
  - `apply_style` (`plugin/writer/styles.py:1224-1267`) supports `clear_direct` values `'none'`, `'style_props'`, and `'all'`:
    - `'none'` preserves direct `CharFontName` overrides;
    - `'style_props'` clears direct font, size, and paragraph indents/spacing/alignment together;
    - `'all'` clears all direct overrides (and is refused for `target='full_document'`).
  - `style_update` (`plugin/writer/styles.py:1518`) updates only the style definition, which existing direct `CharFontName` overrides continue to shadow.
  - Searching `plugin/writer/` shows no tool that updates only `CharFontName` (or `CharFontName` / `CharFontNameAsian` / `CharFontNameComplex`) across a range or whole document (including tables) while preserving font size, bold/italic, alignment, and indents.
- **What remains:** Add a targeted character-property / font-override tool (or a `clear_direct` / font-update mode) that walks body and table text runs to set `CharFontName` (or clear only `CharFontName*`) without touching `CharHeight`, weight/posture, or `Para*` properties.
- **Remaining size:** M

---

## Issue 7: No way to delete a body text box: `frame_list` is empty, `shape_summary` and `get_page_objects` fail

- **Upstream issue:** `Augusto-Zolet/writeragent#7`
- **Status:** **fixed**
- **Evidence:**
  - `frame_delete` (`plugin/writer/specialized/textframes.py`) was added in PR #3207 (`fde4c48a2`, merged Nov 5, 2026) and deletes a `TextFrame` by name via `anchor.getText().removeTextContent(frame)`.
  - `shape_delete` (`plugin/draw/shapes.py:1358-1401`) deletes Draw-page text boxes/shapes on Writer (`page=0` or omitted) via `parent.remove(shape)`.
  - `shape_summary` (which raised `position cannot be determined with this method`) was removed; `_writer_objects_section` (`plugin/doc/document_helpers.py:149-165`, PR #3639 `4e73464f0`) lists Writer draw-page objects safely, and `page_get_objects` (`plugin/writer/structural.py:88-273`, now 0-based per PR #3683 `c104c30b5`) uses `HoriOrientPosition`/`VertOrientPosition` and `_page_at_range(..., retry_if_zero=True)`.
  - Word-style text boxes (`TextBox=True` draw shapes with hidden `TextFrame` `TextBoxContent` omitted by `getTextFrames().getElementNames()`) are now surfaced by `frame_list` and `frame_get_info` with `shape_index` and `shape_name`, and `frame_delete` removes the owning draw shape (`doc.getDrawPage().remove(shape)`) so both the hidden frame and the shape are deleted together.
- **What remains:** `page_get_objects` on a document without layout (hidden load or `page_count 0`) returns empty lists and logs `no text selection`; the header-logo report needs a reproducer. Its `frames` section also still omits Word text boxes.
- **Remaining size:** —

---

## Summary

| # | Issue | Status | Remaining Size |
|---|-------|--------|----------------|
| 1 | `page_get_style_properties` `TypeError` on `Standard` | **fixed** | — |
| 2 | Open/convert new document; sandbox blocks `subprocess` | **still open** | M |
| 3 | Pasted `GraphicObjectShape` listed by `get_page_objects` undeletable | **fixed** | — |
| 4 | Open `.docx` or copy template file from session | **still open** | M |
| 5 | Writer tools missing when MCP session starts on Calc | **still open** | M |
| 6 | Change font of direct-formatted text / whole document | **still open** | M |
| 7 | Delete body text box (`frame_list` / `shape_summary` / `get_page_objects`) | **fixed** | — |
