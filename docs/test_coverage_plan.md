# WriterAgent Test Coverage Improvement Plan

This document outlines 10 equal-sized test coverage improvement tasks for the core functionality of WriterAgent. The objective is to ensure that the critical paths of the system are heavily tested, guaranteeing that document interaction, API communication, and UI tools remain stable.

To avoid merge conflicts and isolate work, these tasks are divided so they can be handed out to separate AI agents. Tasks interacting directly with LibreOffice UNO must use `plugin/testing_runner.py` (native execution), while non-UNO components must use `pytest` (external execution).

---

## 1. Core HTTP Client API Calling (`pytest`)

**What:** Add unit tests for `plugin/modules/http/client.py`. (Currently ~57% covered, critical path for LLM communication).
**Why:** The `LlmClient` class manages the core connection to LLM providers. Testing stream chunking, error handling, keep-alive headers, and the fallback to client-side tool parsers prevents silent failures when an API changes.
**How:**
- Create `plugin/tests/test_http_client.py`.
- Mock `urllib.request.urlopen` (or the underlying `HTTPConnection`) to return canned text and streaming JSON responses.
- Verify that `stream_request_with_tools` correctly yields `("chunk", text)`, `("tool_call", ...)` based on raw byte input.
- Assert that API keys and custom endpoint URLs are correctly injected into the headers.

---

## 2. Config Storage & Event Pub/Sub (`pytest`)

**What:** Expand tests for `plugin/framework/config.py` and `plugin/framework/event_bus.py`.
**Why:** While some config synchronization is tested, the complex rules for merging defaults, handling `api_keys_by_endpoint`, and notifying observers via the global `EventBus` are core to the Settings UI keeping state with the sidebar.
**How:**
- Create/Update `plugin/tests/test_config_sync.py` and `test_event_bus.py`.
- Write tests that mock the file system (or use a temp directory) to read/write `writeragent.json`.
- Test `get_api_key_for_endpoint` and `set_api_key_for_endpoint` under various existing/missing config states.
- Test that updating a config key successfully fires the corresponding event and the callback logic triggers correctly.

---

## 3. Sidebar Chatbot Tools & Parsers (`pytest`)

**What:** Test the non-UNO logic inside `plugin/modules/chatbot/send_handlers.py` and `plugin/modules/chatbot/web_research.py`.
**Why:** The sidebar manages the routing between "standard chat," "web research," and "image generation." Ensuring the `ToolCallingAgent` logic (smolagents integration) correctly structures ReAct loops and caches results is vital.
**How:**
- Create `plugin/tests/test_chatbot_handlers.py`.
- Mock `plugin.framework.image_service.ImageService` and verify that when `chat_direct_image` is True, `handle_send_image` routes correctly.
- Mock HTTP requests in the `DuckDuckGoSearchTool` and `VisitWebpageTool` to verify the web search sub-agent returns synthesized answers without touching UNO code.

---

## 4. MCP Server Routing & Handlers (`pytest`)

**What:** Expand test coverage for `plugin/modules/http/routes.py`, `mcp_protocol.py`, and `server.py`.
**Why:** External AI clients (like Claude or Cursor) rely on the MCP server. Testing that JSON-RPC payloads are correctly routed to the document queue and that cross-origin restrictions (CORS) are strictly enforced is essential for stability and security.
**How:**
- Create `plugin/tests/test_mcp_server.py`.
- Mock the `socketserver.TCPServer` and dispatch raw HTTP POST requests to the `handle_mcp_post` handler.
- Verify that the `X-Document-URL` header correctly populates the target document context.
- Assert that requests originating from non-localhost IP addresses are rejected by the CORS security layer.

---

## 5. Writer Document Content & Formatting (`testing_runner`)

**What:** Add native LibreOffice tests for `plugin/modules/writer/format_support.py` and `plugin/modules/writer/ops.py`.
**Why:** Reading text out of Writer and writing text back in (especially while preserving highlights, bolding, and colors) is the fundamental value of the extension. Format-preserving replacements must not regress.
**How:**
- Update `plugin/tests/test_writer.py` (which runs via the `native_test` decorator).
- Setup a hidden `TextDocument` with a few paragraphs containing mixed formatting (e.g., a bold word, a red word).
- Call `apply_document_content(old_content="word", content="new_word")` on the live document.
- Assert via the `TextCursor` properties that the newly inserted text retains the exact character formatting of the text it replaced.

---

## 6. Writer Structural Navigation (`testing_runner`)

**What:** Test `plugin/modules/writer/tree.py` and `structural.py` natively.
**Why:** When dealing with massive documents, the LLM relies on `build_heading_tree` and `ensure_heading_bookmarks` to jump between sections without losing its place.
**How:**
- Create a new native test inside `plugin/tests/test_writer.py`.
- Generate a document with Heading 1, Heading 2, and several paragraphs using UNO API calls.
- Run `build_heading_tree()` and verify the JSON tree structure matches the document.
- Run `resolve_locator` for `"heading:1.1"` and ensure the returned `para_index` points to the correct UNO text enumeration element.

---

## 7. Calc Cell Manipulation & Ranges (`testing_runner`)

**What:** Expand `plugin/tests/test_calc.py` to cover `plugin/modules/calc/manipulator.py` and `formulas.py`.
**Why:** The AI uses these tools to edit spreadsheets, import CSV data, and read large 2D grids. Broken range calculations or formula syntax errors break the core Calc experience.
**How:**
- Add native tests inside `plugin/tests/test_calc.py`.
- Create a `SpreadsheetDocument` with a populated 3x3 grid.
- Test `read_cell_range("A1:C3")` to ensure the correct 2D array is returned.
- Test `write_cell_formula` with cross-sheet references and ensure the UNO `Formula` property computes correctly.
- Test `import_csv_from_string` and ensure batch writing applies values to the correct `CellRangeAddress`.

---

## 8. Calc Analysis & Chart Generation (`testing_runner`)

**What:** Add native tests for `plugin/modules/calc/charts.py` and `inspector.py`.
**Why:** The ability for the AI to instantly visualize data or debug broken formulas via `error_detector` relies heavily on correct API mapping to LibreOffice chart objects.
**How:**
- Expand `plugin/tests/test_calc.py`.
- Populate a spreadsheet with sample data (e.g., Months vs. Sales).
- Execute `create_chart(range_name="A1:B12", chart_type="bar")`.
- Query the `DrawPage` of the spreadsheet to verify that a `com.sun.star.drawing.OLE2Shape` (Chart) exists at the specified position.
- Test the `error_detector` against a cell explicitly set to `#REF!`.

---

## 9. Draw & Impress Core Tools (`testing_runner`)

**What:** Expand `plugin/tests/test_draw.py` to cover `plugin/modules/draw/shapes.py` and `pages.py`.
**Why:** Draw and Impress capabilities were recently expanded. Creating shapes, extracting speaker notes, and navigating slides are fundamental primitives.
**How:**
- Update `plugin/tests/test_draw.py` (which creates a `PresentationDocument` or `DrawingDocument`).
- Execute `add_slide` and assert `getDrawPages().getCount()` increases.
- Execute `create_shape("rectangle", 1000, 1000, 5000, 3000)`.
- Assert that the shape is added to the page and query its `Position` and `Size` properties via UNO to confirm they match.

---

## 10. Chatbot Sidebar Tool-Loop Integration (`testing_runner`)

**What:** Write a native integration test covering `plugin/modules/chatbot/tool_loop.py` inside a live document context.
**Why:** The ultimate test of the system is the end-to-end loop: simulating a stream of chunks from the LLM that requests a tool, executing that tool via the `ToolRegistry`, and appending the result back to the document.
**How:**
- Create `plugin/tests/test_chatbot_integration.py` containing native tests.
- Initialize a `ChatSession` against a hidden `TextDocument`.
- Mock the `LlmClient` to yield a predefined JSON tool call (e.g., `{"name": "write_table_cells", "arguments": ...}`).
- Run the drain loop and verify that the table in the document is successfully modified, and that the `UndoManager` successfully groups the action into an "AI Edit" context block.