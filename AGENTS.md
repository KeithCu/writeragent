# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file summarizes what was learned and what to do next.

> [!IMPORTANT]
> **AI Assistants: You MUST update this file after making (nontrivial) changes to the project.** This ensures the next assistant has all the up-to-date context without needing manual user intervention.

---

## 1. Project Overview

**WriterAgent** is a LibreOffice extension (Python + UNO) that adds generative AI editing to Writer, Calc, and Draw:

- **Extend Selection** (Ctrl+Q): Model continues the selected text
- **Edit Selection** (Ctrl+E): User enters instructions; model rewrites the selection
- **Chat with Document** (Writer, Calc, and Draw): (a) **Sidebar panel**: WriterAgent deck in the right sidebar, multi-turn chat with tool-calling that edits the document; (b) **Persistent History**: Conversations are saved to a local SQLite database and restored automatically using document metadata for robust session tracking; (c) **Menu item** (fallback): Opens input dialog, appends response to end of document (Writer) or to "AI Response" sheet (Calc/Draw)
- **Settings**: Configure endpoint, model, API key, temperature, request timeout, image generation settings (provider, API keys, dimensions), etc.
- **Image Generation & Editing**: Multimodal capabilities via `generate_image` (create and insert) and `edit_image` (Img2Img on selected object) tools.
- **Calc** `=PROMPT()`: Cell formula that calls the model
- **MCP Server** (opt-in): HTTP server on localhost that exposes Writer/Calc/Draw tools to external AI clients (Cursor, Claude Desktop proxy, scripts). Document targeting via `X-Document-URL` header; opt-in via Settings.

**Connection Management & Identification**: WriterAgent includes built-in connection management in `plugin/modules/ai/service.py` that maintains persistent HTTP/HTTPS connections. All requests use unified `USER_AGENT`, `APP_REFERER`, and `APP_TITLE` headers from `core.constants` for consistent identification across providers (OpenRouter, Together AI, etc.).

Config is stored in `writeragent.json` in LibreOffice's user config directory. See `CONFIG_EXAMPLES.md` for examples (Ollama, OpenWebUI, OpenRouter, etc.).

---

## 2. Repository Structure

```
writeragent/
├── plugin/
│   ├── main.py              # MainJob: trigger(), dialogs, loads modules via bootstrap()
│   ├── _manifest.py         # Auto-generated module manifest from plugin.yaml/module.yaml
│   ├── prompt_function.py   # Calc =PROMPT() formula
│   ├── options_handler.py   # Settings dialog
│   ├── framework/           # Core infrastructure (Config, Document, EventBus, Registries)
│   │   ├── config.py        # Central configuration service
│   │   ├── document.py      # Document context and manipulation service
│   │   ├── config_schema.py # Central configuration schema validation
│   │   ├── event_bus.py     # Global pub/sub event bus
│   │   ├── service_registry.py # Dependency injection for services
│   │   ├── tool_registry.py # Tool discovery and execution framework
│   │   ├── module_base.py   # Base class for all modules
│   │   ├── service_base.py  # Base class for injectable services
│   │   ├── tool_base.py     # Base class for tools exposed to AI
│   │   ├── http_server.py   # MCP Server implementation
│   │   └── dialogs.py       # Base helpers for UNO dialogs
│   └── modules/             # Feature-specific modules
│       ├── ai/              # AI provider framework (OpenAI, Ollama, Horde, etc.)
│       │   ├── service.py   # AIService: unified chat/tool-calling interface
│       │   └── providers/   # Provider-specific implementations
│       ├── chatbot/         # Sidebar chat panel UI and interactions
│       │   ├── panel.py     # ChatSession, logic loop (Send/Stop/Clear)
│       │   └── panel_factory.py # UNO Sidebar Factory
│       ├── writer/          # Writer-specific tools and formatting
│       │   ├── format_support.py # HTML/Markdown handling, format-preserving replacement
│       │   └── ops.py       # Tools implementation (styles, comments, track-changes, tables)
│       ├── calc/            # Calc-specific tools and logic
│       ├── draw/            # Draw/Impress page and shape tools
│       ├── batch/           # Batch processing tools
│       ├── tunnel/          # Tunnels (Bore, Cloudflare, Ngrok)
│       └── http/            # MCP Protocol tools
├── pyproject.toml           # Defines project metadata and dependencies
├── Makefile                 # Build system
├── scripts/                 # Build and deploy scripts (make build, make deploy)
├── WriterAgentDialogs/      # XDL dialogs (XML, Map AppFont units)
├── registry/                # Extension registry (Sidebar.xcu, Addons.xcu, etc.)
└── writeragent.json.example # Config templates
```

---

## 3. What Was Done (Dialog Refactor)

### Before
- Settings and Edit Input dialogs were built **programmatically** with `UnoControlDialog`, `UnoControlEditModel`, etc.
- Layout issues: wrong sizing, truncation, poor HiDPI behavior, no scrollbar

### After
- Both dialogs use **XDL files** (XML) loaded via `DialogProvider`
- `WriterAgentDialogs.SettingsDialog` — multi-page dialog (Chat/Text, **Image Settings**, and auto-generated tabs such as **Http**) using the `dlg:page` multi-page approach with tab-switching buttons. **Page 1 (Chat/Text)** uses a compact layout: endpoint, models, API key, Temperature, Max Tokens, Context Len, Additional Instructions. The **Image Settings** tab has shared options (base size, aspect, gallery, translate), **Seed** for reproducibility, and an **AI Horde** section (toggled by "Use AI Horde for Image Generation"). **MCP Server** (Enable checkbox, Port) is on the **Http** tab, not on Page 1.
- `WriterAgentDialogs.EditInputDialog` — label + text field + OK

### Key implementation details
- **DialogProvider with direct package URL**: Dialogs are loaded by their XDL file URL, not the Basic library script URL. This avoids a deadlock that occurs when the sidebar panel is also registered as a UNO component.
  ```python
  pip = self.ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
  base_url = pip.getPackageLocation("org.extension.writeragent")
  dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
  dlg = dp.createDialog(base_url + "/WriterAgentDialogs/SettingsDialog.xdl")
  ```
- **Use `self.ctx`**, not `uno.getComponentContext()` — the extension's component context is required for `PackageInformationProvider` singleton lookup.
- **Populate**: `dlg.getControl("endpoint").getModel().Text = value`
- **Read**: `dlg.getControl("endpoint").getModel().Text` after `dlg.execute()`
- **Manifest** must register the Basic library: `WriterAgentDialogs/` with `application/vnd.sun.star.basic-library`

---

## 3b. Chat with Document (Sidebar + Menu)

The sidebar and menu Chat work for **Writer and Calc** (same deck/UI; ContextList includes `com.sun.star.sheet.SpreadsheetDocument`).

- **Sidebar panel**: WriterAgent deck in Writer's or Calc's right sidebar; panel has Response area, Ask field, Send button, Stop button, and Clear button. When the user changes Settings (e.g. model or additional instructions), the sidebar is notified via **config-change listeners** in `plugin/framework/config.py` (`add_config_listener`, `notify_config_changed`); the panel refreshes its model and prompt selectors from config so they stay in sync. Listeners use weakref so panels can be GC'd without unregistering.
  - **Auto-scroll**: The response area automatically scrolls to the bottom as text is streamed or tools are called, ensuring the latest AI output is always visible.
  - **Stop button**: A dedicated "Stop" button allows users to halt AI generation mid-stream. It is enabled only while the AI is active and disabled when idle.
  - **Undo grouping**: AI edits performed during tool-calling rounds are grouped into a single undo context ("AI Edit"). Users can revert all changes from an AI turn with a single Ctrl+Z.
  - **Send/Stop button state (lifecycle-based)**: "AI is busy" is defined by the single run of `actionPerformed`: Send is disabled (Stop enabled) at the **start** of the run, and re-enabled (Stop disabled) **only** in the `finally` block when `_do_send()` has returned. No dependence on internal job_done or drain-loop state. `_set_button_states(send_enabled, stop_enabled)` uses per-control try/except with a simple `control.getModel().Enabled = val` check so a UNO failure on one control cannot leave Send stuck disabled. `SendButtonListener._send_busy` is set True at run start and False in finally for external checks. This prevents multiple concurrent requests.
- **Implementation**: `plugin/modules/chatbot/panel_factory.py` (ChatPanelFactory, ChatPanelElement, ChatToolPanel); `ContainerWindowProvider` + `ChatPanelDialog.xdl`; `setVisible(True)` required after `createContainerWindow()`.
- **Tool-calling**: `panel_factory.py` (and the menu path in `plugin/main.py`) detect document type using robust service-based identification (`supportsService`) in `plugin/framework/document.py`. This ensures Writer, Calc, and Draw/Impress documents are never misidentified. **Gotcha**: `hasattr(model, "getDrawPages")` is `True` for Writer (drawing layer for shapes), so strict service checks are required.
    - **Writer**: `com.sun.star.text.TextDocument`. `plugin/modules/writer/tools.py` exposes **WRITER_TOOLS** = `get_document_content`, `apply_document_content`, `find_text` (in `plugin/modules/writer/format_support.py`) + `tool_get_document_outline`, `tool_get_heading_content`, `tool_read_paragraphs`, `tool_insert_at_paragraph`, `tool_get_document_stats`, `list_styles`, `get_style_info`, `list_comments`, `add_comment`, `delete_comment`, `set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes`, `list_tables`, `read_table`, `write_table_cells` (in `plugin/modules/writer/tables.py`) + `generate_image`, `edit_image`.
    - **Calc**: `com.sun.star.sheet.SpreadsheetDocument`. `plugin/modules/calc/tools.py` exposes **CALC_TOOLS** and `execute_calc_tool`; core logic in `core/calc_*.py`.
    - **Draw/Impress**: `com.sun.star.drawing.DrawingDocument` or `com.sun.star.presentation.PresentationDocument`. `plugin/modules/draw/tools.py` exposes **DRAW_TOOLS** and `execute_draw_tool`. Includes slide/page management (`add_slide`, `delete_slide`) and speaker notes context.
- **Menu fallback**: Menu item "Chat with Document" opens input dialog, streams response with no tool-calling. **Writer**: appends to document end. **Calc**: streams to "AI Response" sheet. Both sidebar and menu use the same robust document detection.
- **Config keys** (used by chat): `chat_context_length`, `chat_max_tokens`, `additional_instructions` (in Settings).
- **Unified Prompt System**: See Section 3c.

### Document context for chat (current implementation)

- **Refreshed every Send**: On each user message we re-read the document and rebuild the context; the single `[DOCUMENT CONTENT]` system message is **replaced** (not appended), so the conversation history grows but the context block does not duplicate.
- **Writer**: `plugin/framework/document.py` provides `get_document_context_for_chat(model, max_context, include_end=True, include_selection=True, ctx=None)` which builds one string with: document length (metadata); **start and end excerpts** (for long docs, first/last half of `chat_context_length` with `[DOCUMENT START]` / `[DOCUMENT END]` / `[END DOCUMENT]` labels); **selection/cursor**: `(start_offset, end_offset)` from `get_selection_range(model)` with **`[SELECTION_START]`** / **`[SELECTION_END]`** injected at those positions (capped for very long selections). Helpers: `get_document_end`, `get_selection_range`, `get_document_length`, `get_text_cursor_at_range`, `_inject_markers_into_excerpt()`).
- **Calc**: For Calc documents, `get_document_context_for_chat(..., ctx=...)` delegates to `get_calc_context_for_chat(model, max_context, ctx)` in `plugin/framework/document.py`. **`ctx` is required for Calc** (component context from panel or MainJob); do not use `uno.getComponentContext()` in this path. Calc context includes: document URL, active sheet name, used range, column headers, current selection range, and (for small selections) selection content. See [Calc support from LibreCalc.md](Calc%20support%20from%20LibreCalc.md).
- **Scope**: Chat with Document only. Extend Selection and Edit Selection are legacy and unchanged.

### Web search sub-agent (sidebar toggle)

- **Web search checkbox**: The chat sidebar includes a **Web search** checkbox (`web_search_check`) below the Send/Stop/Clear buttons. When checked for a send:
  - The panel bypasses normal Chat with Document behavior (no document context or document tools are used for that turn).
  - It directly invokes the `web_research` tool from `plugin/modules/writer/tools.py`, which runs the `ToolCallingAgent`-based sub-agent (`DuckDuckGoSearchTool` + `VisitWebpageTool`) to research the query.
  - The synthesized answer is streamed back into the response area as `AI (web): ...`, without modifying the document.
  - When unchecked (default), the sidebar behaves as standard Chat with Document; the main model may still call `web_research` autonomously via tool-calling when appropriate.
  - The sub-agent uses smolagents' JSON-in-text parsing for tool calls; if the model returns malformed or missing JSON for a tool call, WriterAgent now falls back to the last useful text the web agent produced instead of surfacing a low-level "no JSON blob" error to the user.
  - **Web cache (disk)**: Search and webpage results from the smolagents tools (`DuckDuckGoSearchTool`, `VisitWebpageTool` in `plugin/contrib/smolagents/default_tools.py`) are cached on disk in a SQLite DB at `{user_config_dir}/writeragent_web_cache.db`. Cache is shared across processes (retry on lock). Total size is bounded by config `web_cache_max_mb` (default 50, clamp 1–500; 0 disables). On cache hit the entry is touched (LRU) so it is not evicted soon. Key normalization: search = collapse whitespace; page = URL strip. All cache logic lives in `default_tools.py`; `tool_web_research` in `document_tools.py` passes `cache_path` and `cache_max_mb` from config.

### Markdown tool-calling (current)

- **get_markdown**: Returns the document (or selection/range) as Markdown. Parameters: optional `max_chars`, optional `scope` (`"full"` | `"selection"` | `"range"`); when `scope="range"`, required `start` and `end` (character offsets). Result JSON includes **`document_length`** so the AI can replace the whole doc with `apply_markdown(target="range", start=0, end=document_length)` or use `target="full"`. When `scope="range"`, result also includes `start` and `end` echoed back. Implementation: for full scope tries `XStorable.storeToURL` with FilterName `"Markdown"` to a temp file; on failure or for selection/range uses structural fallback (paragraph enumeration + `ParaStyleName` → headings, lists, blockquote). See `plugin/modules/writer/markdown_support.py`.
- **apply_markdown / apply_document_content**: Inserts or replaces content using Markdown/HTML **with native formatting**, or plain text **with format preservation**. Parameters: `content` (string), `target` (`"beginning"` | `"end"` | `"selection"` | `"search"` | **`"full"`** | **`"range"`**); when `target="search"`, also `search`, optional `all_matches`, `case_sensitive`; when **`target="range"`**, required **`start`** and **`end`** (character offsets). **`target="full"`** replaces the entire document (clear all, insert at start). **`target="range"`** replaces the character span `[start, end)` with the markdown (no need to send the original text back). Preferred flow for "make my resume look nice" or reformat: call `get_markdown(scope="full")` once, then `apply_markdown(markdown=<new content>, target="full")` or `target="range", start=0, end=document_length` — **only the new markdown is sent**, never the original document text. Implementation: writes markdown to a temp `.md` file, then **`cursor.insertDocumentFromURL(file_url, {FilterName: "Markdown"})`** at the chosen position; for `"full"` uses `_insert_markdown_full`; for `"range"` uses `get_text_cursor_at_range()` then `setString("")` and `insertDocumentFromURL`. See `format_support.py`.
- **Temperature**: Controls randomness (0.0=deterministic, 1.0=creative). Set to `-1` (default) to let the model use its own default setting.
**Note**: Both Markdown and HTML injection are implemented; further testing will determine the default path for rich formatting and layout control.
  - **Format-preserving replacement (auto-detected)**: When `target="search"` and the replacement content is **plain text** (no Markdown/HTML markup detected by `_content_has_markup()`), the system automatically uses `_replace_text_preserving_format()` instead of `insertDocumentFromURL`. This replaces text **character-by-character**, so every per-character property (CharBackColor, CharColor, CharWeight, CharHeight, CharPosture, CharUnderline, etc.) is preserved — including exotic formatting the AI has no knowledge of. If the new text is longer, extra characters inherit formatting from the last original character; if shorter, leftover characters are deleted.
    - **Auto-detect logic** (`_content_has_markup()`): Scans content for common Markdown patterns (`**`, `# `, `` ` ` ``, `|---`) and HTML tags (`<b>`, `<table>`, `</`, etc.). If markup is found → import path (existing behavior). If plain text → format-preserving path. Deliberately errs on the side of detecting markup, since a false positive just falls back to the existing behavior. No tool schema changes and no AI decision needed — works identically for 30B local models and frontier models.
    - **Why auto-detect works**: The operation type and content type are naturally correlated. Small text edits like "change Joe Blow to Jane Doe" are sent as `target="search"` with plain text content like `"Jane Doe"` — auto-detect sees no markup → preserves all formatting (bold, italic, background colors, everything). Structural rewrites like "make this look pretty" or "convert to a table" naturally use `target="full"` or `target="range"` with markdown/HTML content — the auto-detect sees markup → uses the import path to apply the new formatting. No system prompt guidance is needed because the AI's natural behavior already routes correctly.
    - **Important subtlety**: The format-preserving path preserves ALL character properties, not just background colors. Bold (`CharWeight`), italic (`CharPosture`), underline, font size, font color — these are all per-character UNO properties that survive the single-character `setString()` call. So if the AI replaces `"Joe"` (bold+red-bg) with `"Jan"` (plain text), the result is `"Jan"` still bold with the same red background. The AI does NOT need to re-specify formatting it read from the document.
    - **Edge case**: If the AI unnecessarily wraps a simple replacement in markdown (e.g., sends `"**Jane** Doe"` instead of `"Jane Doe"`), the `**` triggers markup detection and the import path is used, losing background colors. This is a model behavior quirk, not a code issue — the import path is what we had before this feature, so it's no worse than previous behavior. Future hybrid approach: strip markup to plain text, do format-preserving replacement, then apply the markup as character properties on top.
    - **Implementation**: `_replace_text_preserving_format()` and `_apply_preserving_format_at_search()` in `format_support.py`. Tests in `format_tests.py` verify `CharBackColor` preservation for same-length, longer, and shorter replacements.


### System prompt and reasoning (latest)

- **Chat** uses `get_chat_system_prompt_for_document(model, additional_instructions)` in `plugin/framework/constants.py` so the correct prompt is chosen by document type: **Writer** → `DEFAULT_CHAT_SYSTEM_PROMPT` + additional_instructions (get_markdown/apply_markdown, presume document editing, translate/proofread, no preamble); **Calc** → `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` + additional_instructions (semicolon formula syntax, 4-step workflow: understand → get state → use tools → short confirmation; tools grouped READ / WRITE & FORMAT / SHEET MANAGEMENT / CHART / ERRORS). Used by both sidebar and menu Chat.
- **Reasoning tokens**: `plugin/main.py` sends `reasoning: { effort: 'minimal' }` on all chat requests.
- **Thinking display**: Reasoning tokens are shown in the response area as `[Thinking] ... /thinking`. When thinking ends we append a newline after ` /thinking` so the following response text starts on a new line.
- **Persistent Chat History**: Logic in `plugin/framework/history_db.py`. Uses a polyfill strategy: SQLAlchemy if available, fallback to native `sqlite3`.
    - **Schema**: Simple `message_store` table compatible with LangChain's SQL history JSON format.
    - **Database Path**: Stored in LibreOffice user config directory (`writeragent_history.db`).
    - **Robust Session IDs**: Instead of document URLs (which break on rename), we store a `WriterAgentSessionID` in the document's **`UserDefinedProperties`** via `getDocumentProperties()`. If missing, we generate one (hash or UUID) and persist it to the file. This ensures history remains linked even if the file is moved or renamed.
    - **Save-As Awareness**: History logic handles "Save As" by either branching the history (generating a new ID) or inheriting it, depending on the implementation.

See [CHAT_SIDEBAR_IMPLEMENTATION.md](CHAT_SIDEBAR_IMPLEMENTATION.md) for implementation details.

- **Streaming I/O**: pure Python queue + main-thread drain
  All streaming paths (sidebar tool-calling, sidebar simple stream, Writer Extend/Edit/menu Chat, Calc) use the same pattern so the UI stays responsive without relying on UNO Timers/listeners:
  - **Worker thread**: Runs blocking API/streaming (e.g. `stream_completion`, `stream_request_with_tools`), puts items on a **`queue.Queue`** (`("chunk", text)`, `("thinking", text)`, `("stream_done", ...)`, `("error", e)`, `("stopped",)`).
  - **Main thread**: After starting the worker, runs a **drain loop**: `q.get(timeout=0.1)` → process item (append text, update status, call on_done/on_error) → **`toolkit.processEventsToIdle()`**. Repeats until job_done.
  - **Connection Keep-Alive**: `LlmClient` uses `http.client.HTTPConnection` (or `HTTPSConnection`) for persistent connections. The client instance is cached in `plugin/modules/chatbot/panel_factory.py` (sidebar), `plugin/main.py` (MainJob), and `plugin/prompt_function.py` (Calc =PROMPT()) to reuse connections across multiple requests, significantly improving performance for multi-turn chat and cell recalculations.
  - **Streaming edge cases (LiteLLM-inspired):** `finish_reason=error` → raise; repeated identical content chunks → raise (infinite-loop guard); `finish_reason=stop` with tool_calls → remap to `tool_calls`; delta normalization for Mistral/Azure (`role`/`tool.type`/`function.arguments`). See [LITELLM_INTEGRATION.md](LITELLM_INTEGRATION.md).

---

## 3d. Multi-Document Scoping Fix

**Issue**: When multiple Calc (or Writer) documents were open, the AI agent in one sidebar would edit the wrong document because tool executions and context building used global `desktop.getCurrentComponent()` instead of the document associated with the sidebar's frame.

**Root Cause**: Sidebar panels were not properly scoped to their respective documents. The `CalcBridge` and document context functions relied on the global active document, which changes with user focus.

**Fix**:
- Modified `CalcBridge.__init__()` and `DrawBridge.__init__()` to take a specific document (`doc`) instead of global context (`ctx`).
- Updated `execute_calc_tool()`, `execute_draw_tool()`, and `execute_tool()` to take `doc` directly.
- Changed `get_document_context_for_chat()` and `get_calc_context_for_chat()` to take `doc` instead of `ctx`.
- In `panel_factory.py`, each panel uses `self.doc = self.xFrame.getController().getModel()` and passes it to all operations.
- Menu chat continues to use the active document as expected.

**Result**: Each sidebar panel now operates independently on its associated document, preventing cross-contamination when multiple documents are open.

---

## 3e. Calc plugin refactor and tool framework migration

- **Calc plugin modules** under `plugin/modules/calc` now mirror the core Calc helpers more closely (`plugin/modules/calc/address_utils.py`, `plugin/modules/calc/analyzer.py`, `plugin/modules/calc/inspector.py`, `plugin/modules/calc/error_detector.py`, `plugin/modules/calc/manipulator.py`) with small, focused docstrings that explain only non-obvious behavior (UNO assumptions, fallbacks, and porting notes). Earlier AI-driven churn that rewrote docstrings and comments without changing behavior was reverted so diffs stay tight and readable.
- **Behavioral changes preserved**: We keep the functional refactor that routes Calc tools through the plugin framework (`plugin.framework.tool_registry`, `ToolContext`) and the new modular tools (`plugin/modules/calc/cells.py`, `formulas.py`, `sheets.py`). `plugin/modules/calc/tools.py` is now a thin compatibility shim that builds `CALC_TOOLS` from the registry and forwards `execute_calc_tool` calls into the framework.
- **Inspector / Analyzer / ErrorDetector**: Unused “extra analysis” helpers (`detect_data_regions`, `find_empty_cells`, `get_column_statistics`, `get_cell_precedents`, `get_cell_dependents`, `analyze_spreadsheet_structure`) were removed from the plugin copies after confirming they are not called from the new framework. Error explanations now derive precedents by parsing formulas directly, but legacy public entry points and result shapes (addresses, error codes, suggestions) remain compatible with tests and existing prompts.
- **CalcBridge / Manipulator**: `CalcBridge` gained clearer error handling for non-spreadsheet documents and an explicit `get_active_sheet` contract, and `CellManipulator` keeps the newer style/number-format helpers and CSV import utilities while restoring concise, stable docstrings. Overall, Calc plugin diffs against git focus on real behavior changes (tool wiring, error/precedent handling, style application) rather than wording-only comment updates.

---

## 3c. Unified Prompt System with History

The "Additional Instructions" (previously system prompts) are now unified across **Chat, Edit Selection, and Extend Selection** into a single configuration key with a history dropdown (ComboBox).

- **Implementation**:
    - **Shared LRU Logic**: `plugin/framework/config.py` contains `populate_combobox_with_lru()` and `update_lru_history()` used by all dialogs and features.
    - **Unified Key**: All features use the `additional_instructions` config key. LEGACY: The key was renamed from `chat_system_prompt` to avoid legacy data from "full system prompt" iterations.
    - **History Persistence**: Up to 10 entries are stored in `prompt_lru` (JSON list). Not per-endpoint.
- **Behavior**:
    - **Dropdown (ComboBox)**: Settings and Edit Selection input show a dropdown of recent instructions. The Chat sidebar does **not** show additional instructions (configured in Settings only; see Section 3d for sidebar controls).
    - **Multiline Support**: LibreOffice ComboBoxes are single-line. We display a preview in the list and restore full multiline content upon selection.
    - **Prompt Construction**:
        - **Chat**: `get_chat_system_prompt_for_document(model, additional_instructions)` so Writer and Calc get the correct base prompt; in both cases `additional_instructions` is appended.
        - **Edit/Extend**: `additional_instructions` is used as the primary guiding prompt (representing the special system role for that edit).

---

## 3d. Multimodal AI (Image Generation & Editing)

WriterAgent can generate and edit images inside Writer and Calc via tools exposed to the chat LLM. Two backends are supported; they differ in API shape and where the “model” is configured.

### Providers

- **AI Horde** (`plugin/framework/aihordeclient/`): Dedicated **async image API** (not an LLM). Submit job → poll `generate/check` and `generate/status` until done → download images. Uses its own API key and model list (e.g. Stable Diffusion, SDXL). Built-in queueing, progress (informer), Img2Img and inpainting. Non-blocking UI via `run_blocking_in_thread` + `toolkit.processEvents()` in the informer. Config: `image_provider=aihorde`, `aihorde_api_key`, plus image dimensions/steps/NSFW etc.
- **Endpoint** (config value `endpoint`): Uses the **endpoint URL/port and API key from Settings** — the same values the user configured for chat. Only the **model** differs: chat uses the text model, image generation uses **`image_model`**. Single request/response (no queue). Config: `image_provider=endpoint`, and **`image_model`** for the image model id (fallback: text model). **`image_model_lru`** holds recently used image models for combobox dropdowns. Legacy: a previous config value for this provider is also accepted and treated as `endpoint`.

**Image edit (img2img)** is supported by **AI Horde** (already) and by **endpoint** when the backend supports it: **OpenRouter** (source image in chat message with `modalities: ["image"]`) and **Together** and other OpenAI-compatible endpoints that accept `image_url` on `/images/generations`. See [docs/features/image-generation.md](docs/features/image-generation.md) for the provider matrix and how to add edit for new providers.

### Text model vs image model

- **`text_model`** (backward compat: read `model` if unset): The chat/LLM model. Used by Chat, Extend/Edit Selection, and `get_api_config()` (exposed to LlmClient as `"model"`). See `plugin/framework/config.py` `get_text_model(ctx)`.
- **`image_model`**: Used when `image_provider=endpoint`. Same endpoint and API key as chat; this key selects which model handles image requests. Comboboxes (Settings + Chat sidebar) are filled from **`image_model_lru`**; after a successful image generation via the endpoint, the model used is pushed into that LRU.

### ImageService and tools

- **ImageService** (`plugin/framework/image_service.py`): `get_provider(name)` returns `AIHordeImageProvider` or `EndpointImageProvider`. For `name=="endpoint"` it builds API config from `get_api_config(ctx)` (endpoint URL + API key from Settings) and sets `api_config["model"]` to `image_model` or `get_text_model(ctx)`. `generate_image(prompt, provider_name=..., **kwargs)` merges config defaults (width, height, steps, etc.), optional prompt translation, then calls `provider.generate(prompt, **kwargs)`; returns list of local temp file paths.
- **Image tools** (`plugin/framework/image_tools.py`): **`get_selected_image_base64(model, ctx=None)`** exports the selected graphic (Writer `GraphicObject` or Calc `GraphicObjectShape`) to PNG and returns base64 for Img2Img; pass `ctx` from panel/MainJob for Calc. **`insert_image`** / **`replace_image_in_place`** insert or replace the image in the document; **`add_image_to_gallery`** adds to the LibreOffice Media Gallery.
- **Tools exposed to LLM** (`plugin/modules/writer/tools.py`): **`generate_image`** (prompt, optional width, height, provider) — generates and inserts; **`edit_image`** (prompt, optional strength, provider) — Img2Img on selected image, replace or insert. Both use ImageService; when provider is the chat endpoint, after success they update `image_model_lru`.

### UI and config

- **Settings** (`WriterAgentDialogs/SettingsDialog.xdl`): Tabbed. **Chat/Text** tab: Text/Chat Model and **Image model (same endpoint as chat)** comboboxes (LRU). **Image Settings** tab: shared section (width, height, auto gallery, insert frame, translate prompt) and **AI Horde** section (provider enabled via **"Use AI Horde for Image Generation"** on this tab, `aihorde_api_key`, CFG scale, steps, max wait, NSFW) with a fixedline separator. All image-related keys applied via `_apply_settings_result` in `plugin/main.py`.
- **Chat sidebar** (`WriterAgentDialogs/ChatPanelDialog.xdl`, `plugin/modules/chatbot/panel_factory.py`): **AI Model** combobox (text model → `text_model`, `model_lru`) and **Image model (same endpoint as chat)** combobox (→ `image_model`, `image_model_lru`). **"Use Image model"** checkbox (config `chat_direct_image`): when checked, the current message is sent directly to the image pipeline (AI Horde or image model per Settings) for Writer, Calc, and Draw — no chat model round-trip. Orthogonal to which tools are given to the LLM; uses `document_tools.execute_tool("generate_image", ...)` for all doc types. No additional-instructions control in the sidebar; extra instructions come from config only when building the system prompt.

### Config keys (summary)

- Image: `image_provider`, `image_model`, `image_model_lru`, `aihorde_api_key`, `image_width`, `image_height`, `image_cfg_scale`, `image_steps`, `image_nsfw`, `image_censor_nsfw`, `image_max_wait`, `image_auto_gallery`, `image_insert_frame`, `image_translate_prompt`, `image_translate_from`. Chat sidebar: `chat_direct_image` (bool) — "Use Image model" checkbox; when true, message goes directly to image tool. See [IMAGE_GENERATION.md](IMAGE_GENERATION.md) for full mapping and handover notes.

---

## 3e. Advanced Navigation & Caching (Writer)

To improve UI responsiveness and AI navigation in complex documents, we ported performance optimizations from the `libreoffice-mcp-extension`:

- **Document Metadata Cache**: `core.document.DocumentCache` provides a per-document singleton to cache expensive UNO calls:
    - `length`: Total character count.
    - `para_ranges`: Enumeration of all top-level paragraph containers.
    - `page_cache`: Resolution of locators to page numbers.
- **Cache Invalidation**: Automatically triggered in `execute_tool` (in `document_tools.py`) for any document-mutating operation (apply content, style, comments, etc.).
- **Hierarchical Navigation**:
    - `build_heading_tree()`: Single-pass scan of `OutlineLevel` to build a JSON tree of the document structure.
    - `ensure_heading_bookmarks()`: Generates hidden, stable bookmarks (`_mcp_...`) for all headings, allowing the AI to reference sections even as text shifts.
    - `resolve_locator()`: Resolves structured strings (e.g., `heading:1.2`, `paragraph:5`) to document positions.
- **New Navigation Tools**: `get_document_outline` (full tree), `get_heading_content` (fetch section text), `read_paragraphs` (read by offset), and `insert_at_paragraph` (precise positioning).

---

## 4. Shared Helpers

- **`MainJob._apply_settings_result(self, result)`** (`plugin/main.py`): Applies settings dialog result to config. Used by both Writer and Calc settings branches.
- **`plugin/framework/logging.py`**:
  - Call `init_logging(ctx)` once from an entry point (e.g. start of `trigger`, or when the chat panel wires controls). Sets global log paths and optional `enable_agent_log` from config.
  - `debug_log(msg, context=None)` — single debug file. Writes to `writeragent_debug.log` in user config dir (or `~/writeragent_debug.log`). Use `context="API"`, `"Chat"`, or `"Markdown"` for prefixed lines. No ctx passed at write time.
  - `agent_log(location, message, ...)` — NDJSON to `writeragent_agent.log` (user config or `~/`), only if config `enable_agent_log` is true.
  - Watchdog: `update_activity_state(phase, ...)`, `start_watchdog_thread(ctx, status_control)` for hang detection (logs and status "Hung: ..." if no activity for threshold).
- **`SendButtonListener._send_busy`** (`panel_factory.py`): Boolean; True from run start until the `finally` block of `actionPerformed` (single source of truth for "is the AI running?"). Used together with lifecycle-based `_set_button_states(send_enabled, stop_enabled)`.
- **`core/api.format_error_for_display(e)`**: Returns user-friendly error string for cells/dialogs (e.g. `"Error: Connection refused..."`).
- **Dialog control helpers** (`plugin/framework/dialogs.py`):
  - `add_dialog_button(dlg_model, name, label, x, y, width, height, push_button_type=None, enabled=True)`
  - `add_dialog_label(dlg_model, name, label, x, y, width, height, multiline=True)`
  - `add_dialog_edit(dlg_model, name, text, x, y, width, height, readonly=False)`
  - `add_dialog_hyperlink(dlg_model, name, label, url, x, y, width, height)`
  These encapsulate the boilerplate required to create and insert UNO control models into a dialog model.

---

## 5. Critical Learnings: LibreOffice Dialogs

### Units
- **Map AppFont** units: device- and HiDPI-independent. 1 unit ≈ 1/4 char width, 1/8 char height.
- XDL uses Map AppFont for `dlg:left`, `dlg:top`, `dlg:width`, `dlg:height`
- **Do not** use raw pixels for layout; they break on HiDPI

### No automatic layout
- LibreOffice dialogs have **no flexbox, no auto-size**. Every control needs explicit position/size.
- Scrollbars require manual implementation (complex). Prefer splitting into tabs or keeping content compact.

### Recommended approach: XDL + DialogProvider (direct package URL)
- Design dialogs as **XDL files** (XML). Edit `WriterAgentDialogs/*.xdl` directly.
- Load via `DialogProvider.createDialog(base_url + "/WriterAgentDialogs/DialogName.xdl")` where `base_url` comes from `PackageInformationProvider.getPackageLocation()`.
- **Do NOT** use the Basic library script URL format (`vnd.sun.star.script:LibraryName.DialogName?location=application`) — it deadlocks when sidebar UNO components are also registered.
- The Dialog Editor in LibreOffice Basic produces XDL; you can also hand-write or generate it.

### XDL format (condensed)
- Root: `<dlg:window>` with `dlg:id`, `dlg:width`, `dlg:height`, `dlg:title`, `dlg:resizeable`
- Content: `<dlg:bulletinboard>` containing controls
- Controls: `dlg:text` (label), `dlg:textfield`, `dlg:button`, `dlg:combobox`, `dlg:fixedline` with `dlg:id`, `dlg:left`, `dlg:top`, `dlg:width`, `dlg:height`, `dlg:value`
- DTD: `xmlscript/dtd/dialog.dtd` in LibreOffice source

### Multi-page dialogs (tabs)
- **Do NOT use `dlg:tabpagecontainer` / `dlg:tabpage`** — these elements are **not in the XDL DTD** and cause `createDialog()` to fail silently with an empty error message.
- Use `dlg:page` attributes on individual controls instead: add `dlg:page="1"` or `dlg:page="2"` to each control. Controls with no `dlg:page` are always visible.
- Set `dlg:page="1"` on the root `<dlg:window>` to set the initial page.
- Switch pages at runtime: `dlg.getModel().Step = 2` (Step property = page number).
- Wire tab buttons with a UNO listener that inherits from **both** `unohelper.Base` and `XActionListener`:
  ```python
  from com.sun.star.awt import XActionListener
  class TabListener(unohelper.Base, XActionListener):
      def __init__(self, dialog, page):
          self._dlg = dialog
          self._page = page
      def actionPerformed(self, ev):
          self._dlg.getModel().Step = self._page
      def disposing(self, ev): pass
  dlg.getControl("btn_tab_chat").addActionListener(TabListener(dlg, 1))
  dlg.getControl("btn_tab_image").addActionListener(TabListener(dlg, 2))
  ```
- **Important**: the listener class must inherit `XActionListener` — passing a plain class raises `value does not implement com.sun.star.awt.XActionListener`.

### Compact layout
- Label height ~10, textfield height ~14, gap label→edit ~1, gap between rows ~2
- Margins ~8. Tighter = more compact but must stay readable.

### Optional Controls
- When wiring controls that might not exist in all XDL versions (e.g. backward compatibility), use **`get_optional(root_window, name)`** from `plugin/framework/uno_helpers.py` (returns control or None). For checkboxes, use **`get_checkbox_state(ctrl)`** / **`set_checkbox_state(ctrl, value)`** and **`is_checkbox_control(ctrl)`** from the same module so LibreOffice control quirks are handled in one place.

### Programmatic Control Creation
- When creating dialogs or controls programmatically (without XDL), use the helpers in `plugin/framework/dialogs.py` (e.g., `add_dialog_button`). This ensures consistent naming, positioning (still using Map AppFont units by convention if the dialog model is set up that way), and property configuration.

---

## 4b. Critical Learnings: Format Preservation

### The Challenge
When replacing text (e.g., correcting a name), we must preserve character-level formatting (fonts, colors, bold/italic) even if the replacement text length differs. By default, LibreOffice replacements inherit the formatting of the *insertion point* (usually the character *before*), which wipes out specific formatting on the replaced text itself.

### The Solution: `_replace_text_preserving_format`
We implemented a custom engine in `plugin/modules/writer/format_support.py` that iterates character-by-character.
- **Same length**: 1:1 replacement, keeping each character's properties.
- **Longer**: 1:1 for the overlap, then insert extra chars inheriting from the last original char.
- **Shorter**: 1:1 for the overlap, then delete the leftover original chars.

### Critical Implementation Details (Gotchas)
1.  **"Insert After + Delete" Strategy (Robustness)**:
    - **Problem**: `setString()` on a selection is flaky at paragraph boundaries (often inherits formatting from the *next* char instead of the replaced one), and "insert and replace" can wipe attributes.
    - **Solution**: Do not replace in-place. Instead, **insert** the new character immediately *after* the old one (inheriting its exact attributes), then **delete** the old character.
    - **Optimization**: If `new_char == old_char`, skip the operation entirely.

2.  **Performance (O(N) Traversal)**:
    - **Don't** create a new cursor from the document start for every character (`O(N^2)`). This hangs for >500 chars (30s+).
    - **Do** use a single **persistent cursor** for traversal. Move it relative to its current position (`goRight(1)`).
    - **Note**: When using "Insert After + Delete", careful cursor management is needed to advance past the newly inserted character without losing sync. Use local `text.createTextCursorByRange(main_cursor)` clones for the insert/delete ops so the main traversal cursor stays stable.

3.  **ProcessEvents Reliability**:
    - **Warning**: `toolkit.processEvents()` can sometimes raise exceptions (especially in test environments or headless contexts). Always wrap it in a `try/except` block and disable if it fails.

2.  **Raw Content vs. HTML Wrapping**:

    - **The Bug**: AI often sends plain text. If `DOCUMENT_FORMAT="html"`, `_ensure_html_linebreaks` wraps this in `<html><body><p>...</p></body></html>`.
    - **The Injection**: If you pass this wrapped string to the format-preserving function, it will replace your document text with literal HTML source code (e.g., replacing "K" with "<", "e" with "h", "i" with "t", etc.), effectively destroying the document.
    - **The Fix**: Always modify `tool_apply_document_content` to capture `raw_content` *before* any HTML processing. Use `raw_content` for the format-preserving path. Use `content` (wrapped) only for the standard `insertDocumentFromURL` path.

3.  **Markup Detection Order**:
    - **Don't** run `_content_has_markup(content)` *after* HTML wrapping. It will always return True (because of the added tags), forcing the non-preserving path.
    - **Do** run it on the **raw input string** immediately.

4.  **Auto-Detection is Key**:
    - The AI doesn't know about `target="search"` vs `target="range"` for formatting. It just calls tools.
    - We must auto-detect plain text in **all** paths (`search`, `range`, `full`). If `content` is plain text, divert to `_replace_text_preserving_format`. This allows "Make this whole paragraph blue" (Markdown path) and "Correct spelling of 'Burtis'" (Preserving path) to work seamlessly with the same tool.


## 5. Config File

- **Path**: LibreOffice UserConfig directory + `writeragent.json`
  - Linux: `~/.config/libreoffice/4/user/writeragent.json` (or `24/user` for LO 24)
  - macOS: `~/Library/Application Support/LibreOffice/4/user/writeragent.json`
  - Windows: `%APPDATA%\LibreOffice\4\user\writeragent.json`
- **Single file**: No presets or multiple configs. To use a different setup, copy your config to the path above as `writeragent.json`.
- **Settings Dialog Integration**: AI and service configurations are unified in the `WriterAgent -> Settings` dialog. The Settings dialog tabs for service configuration (e.g., Writer, Calc, Chatbot, Tunnel, Http) are auto-generated from `module.yaml` configuration schemas by `scripts/generate_manifest.py`.
- **Settings dialog** reads/writes this file via `get_config()` / `set_config()` in `plugin/framework/config.py`. Use **`get_current_endpoint(ctx)`** for the normalized current endpoint URL (single source; used by plugin/main.py and panel_factory.py).
- **Chat-related keys**: `chat_context_length` (default 8000), `chat_max_tokens` (default 512 menu / 16384 sidebar), `additional_instructions`. Also **per-endpoint API keys**: `api_keys_by_endpoint` (JSON map: normalized endpoint URL → API key); `get_api_key_for_endpoint(ctx, endpoint)` / `set_api_key_for_endpoint(ctx, endpoint, key)` in `plugin/framework/config.py`. Legacy `api_key` is migrated once into the map under the current endpoint and then removed. Settings dialog shows and saves the key for the selected endpoint.
- **Model keys**: `text_model` (chat/LLM model; backward compat: `model`), `model_lru` (recent text models); `image_model` (image model when using chat endpoint for images), `image_model_lru` (recent image models). See Section 3d.

---

## 5b. Log Files

- **Unified debug log**: `~/.config/libreoffice/4/user/config/writeragent_debug.log` (exact path; fallback `~/writeragent_debug.log` if user config dir not found). Written by `debug_log(msg, context=...)` with prefixes `[API]`, `[Chat]`, `[Markdown]`, `[AIHorde]`. Paths set once via `init_logging(ctx)`; no ctx needed at call sites.
- **Agent log** (NDJSON, optional): `writeragent_agent.log` in user config (or `~/`). Written by `agent_log(...)` only when config key `enable_agent_log` is true (default false). Used for hypothesis/debug tracking.
- **Watchdog**: If no activity for the threshold (e.g. 30s), a line is written to the debug log and the status control shows "Hung: ...".

### Finding log files (and image generation debugging)

Log paths are set in `plugin/framework/logging.py` by `init_logging(ctx)` and live in the **same directory as `writeragent.json`** (from `PathSettings.UserConfig` in `plugin/framework/config.py`). Locations to check, in order:

- `~/.config/libreoffice/4/user/writeragent_debug.log` and `writeragent_agent.log`
- `~/.config/libreoffice/24/user/` (same filenames; version-dependent)
- `~/.config/libreoffice/4/user/config/` and `24/user/config/` (some installs)
- **Fallback** (if user config dir unavailable at init): `~/writeragent_debug.log` and `~/writeragent_agent.log`

**Which logs show image generation failures:**

- **AI Horde** (`image_provider=aihorde`): `writeragent_debug.log` — search for `[AIHorde]` for request flow, errors, and stack traces. `plugin/framework/aihordeclient/` uses `debug_log` and `log_exception` with context `"AIHorde"`.
- **Endpoint** (`image_provider=endpoint`): Debug log only shows `[Chat] Tool call: generate_image(...)` (no error text). For the actual error: enable **Settings → Enable agent log**, reproduce, then open `writeragent_agent.log` and look for `"Tool result"` with `tool` `"generate_image"` or `"edit_image"` — the error is in `data.result_snippet`. `plugin/framework/image_service.py` does not write to the debug log.

---

## 6. Build and Install

```bash
make build
make deploy   # or remove first: unopkg remove org.extension.writeragent
```

Restart LibreOffice after install/update. Test: menu **WriterAgent → Settings** and **WriterAgent → Edit Selection**.

**Build without voice recording:** Run `make build-no-recording` (or `make build NO_RECORDING=1`) to produce an .oxt that excludes voice/audio recording: the bundle omits `contrib/audio/` and `plugin/modules/chatbot/audio_recorder.py`; the Chat sidebar stays and the Record button is simply not shown. This reduces extension size when recording is not needed.

---

## 7. What to Do Next

### High priority (from IMPROVEMENT_PLAN.md) — DONE
- ~~Extract shared API helper; add request timeout~~ (implemented: `stream_completion`, `_get_request_timeout`, config `request_timeout`)
- ~~Improve error handling (message box instead of writing errors into selection)~~ (implemented: `show_error()` with MessageBox, `format_error_message()`)
- ~~Refactor duplicate logic~~ (see Section 3c Shared Helpers)

### Dialog-related
- **Config presets**: Add "Load from file" or preset dropdown in Settings so users can switch config files.
- **EditInputDialog**: Consider multiline for long instructions; current layout is single-line.

### Image generation / Settings dialog
- ~~**Reorganize Settings Image tab**~~: Done. The tab is titled **Image Settings** and split into a shared section (width, height, auto gallery, insert frame, translate prompt) and an **AI Horde only** section (API key, CFG scale, steps, max wait, NSFW) with a visual separator so users can ignore Horde-specific options when using the same endpoint as chat.

### Format-preserving replacement
- **Proportional format mapping**: For large length differences, distribute the original formatting pattern proportionally across the new text instead of simple 1:1 character mapping.
- **Paragraph-style preservation**: Handle cases where replacement spans paragraph breaks.
- **Edit Selection streaming**: Apply format-preserving logic to the Edit Selection streaming path for character-level formatting retention during live edits.

### General
- API key and auth for the configured endpoint are already implemented; optional: endpoint preset dropdown in Settings.
- Impress support; Calc range-aware behavior.
- DSPy prompt optimization and evaluation live in `scripts/prompt_optimization/`. `run_eval.py` runs a fixed Writer dataset against the current `DEFAULT_CHAT_SYSTEM_PROMPT` (using mock tools) and reports correctness + token usage; `run_optimize.py` runs DSPy MIPROv2 to search for better system prompts; `run_eval_multi.py` sweeps **multiple models** (from `model_configs.py`) and ranks them by **intelligence per dollar** (average correctness divided by estimated USD cost from list prices).

### Optional refactoring (future work)
- **panel_factory.py**: Split `SendButtonListener._do_send` into smaller methods (e.g. `_do_send_direct_image`, `_do_send_with_tools`, `_do_send_simple_stream`) with a short `_do_send` that validates and dispatches.
- **plugin/main.py**: Split `trigger()` into handlers (e.g. `_handle_mcp`, `_handle_writer`, `_handle_calc`, `_handle_draw`) so `trigger` only delegates; optionally extract settings populate/read into helpers driven by `field_specs`.
- **plugin/framework/config.py**: Introduce internal helpers for "read full config" / "write full config" (e.g. build on `get_config_dict`) so `set_config`/`remove_config` share one write path and future caching or storage changes touch one place.
- **panel_factory.py**: Consider a small doc-type registry (Writer/Calc/Draw → tools + execute function) so choosing tools and executor in `_do_send` is data-driven and adding a new doc type doesn't require editing a long if/elif chain.

### Chat settings in UI — DONE
- ~~Expose `chat_context_length`, `chat_max_tokens`, `additional_instructions` in the Settings dialog~~ (implemented in SettingsDialog.xdl).

### Writer Tools Expansion — DONE
- ~~**Writer tool set expansion**~~: Added 12 new Writer tools in `plugin/modules/writer/ops.py` and wired into `plugin/modules/writer/tools.py`. Removed 7 legacy unused functions. New tools: `list_styles`, `get_style_info`, `list_comments`, `add_comment`, `delete_comment`, `set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes`, `list_tables`, `read_table`, `write_table_cells`. System prompt updated to mention them.

### MCP Server (external AI client access) — DONE
- **`plugin/modules/http/mcp_protocol.py`**: `_Future`, `execute_on_main_thread_mcp`, `drain_mcp_queue` — work is queued from HTTP handler threads and drained on the main thread.
- **`plugin/framework/http_server.py`**: `MCPHttpServer` and `MCPHandler`; GET `/health`, `/tools`, `/`, `/documents`; POST `/tools/{name}`. **Document targeting**: `X-Document-URL` header; server resolves document by iterating `desktop.getComponents()` and matching `getURL()`. Falls back to active document if header absent. Port utilities: `_probe_health`, `_is_port_bound`, `_kill_zombies_on_port` (Windows).
- **Idle-time draining**: **AsyncCallback thread** in `plugin/main.py` (Path A). A background Python thread schedules `XCallback` via `com.sun.star.awt.AsyncCallback` every 100ms, which safely executes `drain_mcp_queue()` on the main VCL thread. Option B (piggyback on the chat stream drain loop) was **not** used — it would only service MCP during active chat, which is inadequate for standalone MCP use.
- **Config**: `mcp_enabled` (default false), `mcp_port` (default 8765). MCP settings are on the **Http** tab of the Settings dialog (auto-generated from `plugin/modules/http/module.yaml`); Enable MCP Server checkbox, Port field, "Localhost only, no auth." label.
- **Menu**: "Toggle MCP Server" and "MCP Server Status" under WriterAgent. Status dialog shows RUNNING/STOPPED, port, URL, and health check. Auto-start: when user saves Settings with MCP enabled, server (and timer) start if not already running.
- **Icons**: `assets/` includes `running_16.png`, `running_26.png`, `starting_16.png`, `starting_26.png`, `stopped_16.png`, `stopped_26.png` (from libreoffice-mcp-extension).
- See **`MCP_PROTOCOL.md`** for protocol details and architecture.

- **Document Tree & Navigation (DONE)**: Ported `build_heading_tree`, `ensure_heading_bookmarks`, and `resolve_locator` to `plugin/framework/document.py`. New tools `get_document_outline` and `get_heading_content` provide structured access to long documents.

---

## 7b. Future Roadmap

- **Richer Context**: Metadata awareness (word counts, styles, formula dependencies).
- **Safer Workflows**: Propose-first execution with user confirmation (diff preview).
- **Predictive Typing**: Trigram-based "ghost text" for real-time drafting assist.
- **Reliability Foundations**: Robust timeouts, clear error prompts, and rollback safety.
- **Suite Completeness**: Finalizing Draw and Impress slide/shape toolsets.
- **Offline First**: Continued focus on performance with the fastest local models (Ollama, etc.) to ensure privacy and speed without cloud dependencies.
- **Hybrid Orchestrator / Handover**: Future capability to "hand over" complex tasks from the sidebar to high-power external CLI agents (like `claude-code`) using the built-in MCP server.

Image generation and AI Horde integration are **complete** (generate_image, edit_image, AI Horde + endpoint providers, Image Settings tab with shared vs Horde-only sections).

---

## 8. Gotchas

- **Settings dialog fields**: The list of settings is defined in **`MainJob._get_settings_field_specs()`** (single source); `_apply_settings_result` derives apply keys from it. Settings dialog field list in XDL must match the names in that method.
- **Library name**: `WriterAgentDialogs` (folder name) must match `library:name` in `dialog.xlb`.
- **DialogProvider deadlock**: Using `vnd.sun.star.script:...?location=application` URLs with `DialogProvider.createDialog()` will deadlock when the sidebar panel (panel_factory.py) is also registered as a UNO component. Always use direct package URLs instead (see Section 3).
- **Use `self.ctx` for PackageInformationProvider**: `uno.getComponentContext()` returns a limited global context that cannot look up extension singletons. Always use `self.ctx` (the context passed to the UNO component constructor).
- **dtd reference**: XDL uses `<!DOCTYPE dlg:window PUBLIC "... "dialog.dtd">`. LibreOffice resolves this from its installation.
- **Chat sidebar visibility**: After `createContainerWindow()`, call `setVisible(True)` on the returned window; otherwise the panel content stays blank.
- **Chat panel imports**: `plugin/modules/chatbot/panel_factory.py` uses `_ensure_extension_on_path()` to add the extension dir to `sys.path` so `from main import MainJob` and `from document_tools import ...` work.
- **Logging**: Call `init_logging(ctx)` once from an entry point that has ctx. Then use `debug_log(msg, context="API"|"Chat"|"Markdown")` and `agent_log(...)`; both use global paths. Do not add new ad-hoc log paths.
- **Streaming in sidebar**: Do not use UNO Timer or `XTimerListener` for draining the stream queue—the type is not available in the sidebar context. Use the pure Python pattern: worker + `queue.Queue` + main-thread loop with `toolkit.processEventsToIdle()` (see "Streaming I/O" in Section 3b).
- **Document scoping in sidebar**: Each sidebar panel instance must operate on its associated document only. Use `self.xFrame.getController().getModel()` to get the document for the panel's frame. Do not rely on global `desktop.getCurrentComponent()` as it changes with user focus and causes the AI to edit the wrong document when multiple documents are open. Tool executions and context building must pass the specific document to avoid cross-document contamination.
- **Strict Verification**: `SendButtonListener` tracks `initial_doc_type` during `_wireControls`. In `_do_send`, it re-verifies the document type. If it differs from the initial type, it logs an error and refuses to send. This prevents document-type "leakage" and ensures the AI never uses the wrong tools.
- **Writer has a Drawing Layer**: `hasattr(model, "getDrawPages")` returns `True` for Writer documents because they have a drawing layer for shapes. Always use `is_writer(model)` (via `supportsService`) to avoid misidentifying Writer as Draw.
- **Context function signatures**: All document context functions should follow the signature `(model, max_context, ctx=None)`. Missing the `ctx` default can lead to `TypeError` during document type transitions in the sidebar.
- **API Keys / Security**: API keys MUST be handled via the Settings dialog and stored in `writeragent.json`. Never bake in fallbacks to environment variables (like `OPENROUTER_API_KEY`) in production code, as this bypasses the user's manual configuration and complicates privacy auditing. Env vars are for developer testing ONLY.
- **MCP Server**: The MCP HTTP server and UNO Timer for `drain_mcp_queue` are started from `plugin/main.py` only (not from the sidebar). Server binds to localhost only; no authentication. External clients target a document via the `X-Document-URL` header to avoid races with the active document.

---

## 9. References

- LibreOffice xmlscript: `~/Desktop/libreoffice/xmlscript/` (if you have a local clone)
- DTD: `xmlscript/dtd/dialog.dtd`
- Example XDL: `odk/examples/DevelopersGuide/Extensions/DialogWithHelp/DialogWithHelp/Dialog1.xdl`
- DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces

---

## 10. Debugging Tips (Agent Hard-won Lessons)

### UNO UI Controls
- **Populating ListBox/ComboBox**: Setting `.Text` or `.String` is often not enough for selection lists. Use the **`StringItemList`** property (a tuple of strings) to populate a `ListBox` or `ComboBox` model. The UI will not show items otherwise.
- **Dynamic Options**: Use `options_provider` in your config schema and a corresponding resolver in `SettingsHandler` to fetch dynamic lists (like AI providers or models) at runtime.

### Python Scoping & Imports
- **DO NOT shadow global modules**: Be extremely careful with `import logging` inside functions if `logging` is also imported at the top level. This can cause `UnboundLocalError` when the function tries to use the global `logging` but sees a local "not yet initialized" name instead.
- **Traceback Logging**: When debugging silent failures in UNO dispatch, catch `Exception` and use `traceback.format_exc()` to write to a hard-coded file in `/tmp/`. Standard `logging` might not be initialized or visible yet.

### Build & Deploy
- **`make deploy` vs `make repack`**: If code changes aren't appearing in the application, your `.oxt` bundle might be out of sync. Use `make deploy` for a full clean/build/reinstall. `make repack` only re-zips the *existing* bundle directory and might miss new file edits.
- **Check `manifest.xml`**: If a new UNO component or XCU file isn't working, verify it is registered in `extension/META-INF/manifest.xml`.

### Multi-process Logging
- LibreOffice/Python logs can be buffered. If you don't see your changes, check `/tmp/` logs first or use `flush=True` (or `f.flush()`) when writing diagnostic files.
