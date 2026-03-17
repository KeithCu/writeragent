# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file summarizes what was learned and what to do next.

> [!IMPORTANT]
> **AI Assistants: You MUST update this file after making (nontrivial) changes to the project.** This ensures the next assistant has all the up-to-date context without needing manual user intervention.

---

## 1. Project Overview

**WriterAgent** is a LibreOffice extension (Python + UNO) that adds generative AI editing to Writer, Calc, and Draw:

- **Extend Selection** (Ctrl+Q): Model continues the selected text
- **Edit Selection** (Ctrl+E): User enters instructions; model rewrites the selection
- **Chat with Document** (Writer, Calc, and Draw): (a) **Sidebar panel**: WriterAgent deck in the right sidebar, multi-turn chat with tool-calling that edits the document; (b) **Persistent History**: Conversations are saved to a local SQLite database (when available) and restored automatically using document metadata for robust session tracking; (c) **Menu item** (fallback): Opens input dialog, appends response to end of document (Writer) or to "AI Response" sheet (Calc/Draw)
- **Settings**: Configure endpoint, model, API key, temperature, request timeout, image generation settings (provider, API keys, dimensions), etc.
- **Image Generation & Editing**: Multimodal capabilities via a unified `generate_image` tool (create new or edit selected with `source_image='selection'`). All image backends implement a common contract: `ImageProvider.generate()` returns `(paths_list, error_message_str)` so callers (e.g., `ImageService.generate_image` and Writer tools) can distinguish “no image” from a provider error. `EndpointImageProvider.generate()` in `plugin/framework/image_utils.py` follows this tuple contract, and tests in `plugin/tests/test_image_service_refactor.py` assert it.
- **Calc** `=PROMPT()`: Cell formula that calls the model
- **MCP Server** (opt-in): HTTP server on localhost that exposes Writer/Calc/Draw tools to external AI clients (Cursor, Claude Desktop proxy, scripts). Document targeting via `X-Document-URL` header; opt-in via Settings.

**Connection Management & Identification**: WriterAgent includes built-in connection management in `plugin/modules/ai/service.py` that maintains persistent HTTP/HTTPS connections. All requests use unified `USER_AGENT`, `APP_REFERER`, and `APP_TITLE` headers from `core.constants` for consistent identification across providers (OpenRouter, Together AI, etc.). API-key based authentication is centralized in `plugin/framework/auth.py`, which maps endpoints (OpenRouter, Together, DeepSeek, local Ollama, custom) to provider profiles and builds the correct auth headers for `LlmClient` based on the per-endpoint key stored in config. `LlmClient._headers()` delegates to this module but also preserves backward compatibility for simple/local endpoints: if an `api_key` is configured and no `Authorization` header was attached by provider logic, it adds `Authorization: Bearer <api_key>` (so Ollama-style `http://localhost:11434` endpoints can still use a Bearer token when the user sets one, and remain unauthenticated when `api_key` is empty). For **local HTTPS endpoints**, the HTTP client now **tries normal certificate verification first** and, if a certificate-validation error occurs (`self-signed`, hostname mismatch, local issuer missing), **automatically retries once with an unverified SSL context**. No user-facing certificate toggle is exposed.

Config is stored in `writeragent.json` in LibreOffice's user config directory. See `CONFIG_EXAMPLES.md` for examples (Ollama, OpenWebUI, OpenRouter, etc.).

**Licensing**: In March 2026, the project was relicensed from MPL 2.0 to **GPL v3 (or later)** to better support community contributions and clarify patent grants. Original work by John Balis (MPL 2.0) is attributed in file headers and the installer. Significant contributions to the framework and build system were provided by [quazardous](https://github.com/quazardous/), and Calc integration features were provided by **LibreCalc AI Assistant** (originally under MIT).

**Environment Support (March 2026 Update)**: Supported Python versions were pruned to **3.11 through 3.14**. Support for Python 3.9 and 3.10 was dropped to reduce maintenance overhead and extension size. Additionally, experimental **free-threaded** binaries for Python 3.14 (314t) were removed from `contrib/audio` as they are not used by the standard LibreOffice interpreter, saving ~4MB of disk space.

---

## 2. Repository Structure

```
writeragent/
├── plugin/
│   ├── main.py              # MainJob: trigger(), dialogs, loads modules via bootstrap()
│   ├── _manifest.py         # Auto-generated module manifest from plugin.yaml/module.yaml
│   ├── prompt_function.py   # Calc =PROMPT() formula
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
│       │   ├── panel.py     # ChatSession, Send/Stop/Clear wiring, delegates to mixins
│       │   ├── send_handlers.py # SendHandlersMixin: audio, web-research, image, agent backends
│       │   ├── tool_loop.py # ToolCallingMixin: multi-round tool-calling engine & simple stream
│       │   └── panel_factory.py # UNO Sidebar Factory
│       ├── writer/          # Writer-specific tools and formatting
│       │   ├── format_support.py # HTML handling, format-preserving replacement
│       │   └── ops.py       # Tools implementation (styles, comments, track-changes, tables)
│       ├── calc/            # Calc-specific tools and logic
│       ├── draw/            # Draw/Impress page and shape tools
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

## 3. Dialogs (current design)

- Settings and Edit Selection dialogs are defined as **XDL files** under `WriterAgentDialogs/` and loaded via `DialogProvider.createDialog(base_url + "/WriterAgentDialogs/SettingsDialog.xdl")`.
- Dialogs use `dlg:page` for multi-page layouts; tabs are wired with `XActionListener` classes that set `dlg.getModel().Step`.
- Always use the extension component context (`self.ctx`) with `PackageInformationProvider` to resolve `base_url`; do **not** call `uno.getComponentContext()` here.

---

## 3b. Chat with Document (Sidebar + Menu)

The sidebar and menu Chat work for **Writer and Calc** (same deck/UI; ContextList includes `com.sun.star.sheet.SpreadsheetDocument`).

- **Sidebar panel**: WriterAgent deck in Writer's or Calc's right sidebar; panel has Response area, Ask field, Send button, Stop button, and Clear button. **Theme Matching**: The sidebar dynamically matches its background color to the current LibreOffice color scheme (e.g. Dark Mode) by reading `DialogColor` from the global configuration. When the user changes Settings (e.g. model or additional instructions), the sidebar is notified via **config-change listeners** in `plugin/framework/config.py` (`add_config_listener`, `notify_config_changed`); the panel refreshes its model and prompt selectors from config so they stay in sync. Listeners use weakref so panels can be GC'd without unregistering.
  - **Auto-scroll**: The response area automatically scrolls to the bottom as text is streamed or tools are called, ensuring the latest AI output is always visible.
  - **Stop button**: A dedicated "Stop" button allows users to halt AI generation mid-stream. It is enabled only while the AI is active and disabled when idle. The button immediately closes the network connection to break any hanging reads, ensuring control is returned to the user instantly.
  - **Undo grouping**: AI edits performed during tool-calling rounds are grouped into a single undo context ("AI Edit"). Users can revert all changes from an AI turn with a single Ctrl+Z.
  - **Send/Stop button state (lifecycle-based)**: "AI is busy" is defined by the single run of `actionPerformed`: Send is disabled (Stop enabled) at the **start** of the run, and re-enabled (Stop disabled) **only** in the `finally` block when `_do_send()` has returned. No dependence on internal job_done or drain-loop state. `_set_button_states(send_enabled, stop_enabled)` uses per-control try/except with a simple `control.getModel().Enabled = val` check so a UNO failure on one control cannot leave Send stuck disabled. `SendButtonListener._send_busy` is set True at run start and False in finally for external checks. This prevents multiple concurrent requests.
- **Implementation**: `plugin/modules/chatbot/panel_factory.py` (ChatPanelFactory, ChatPanelElement, ChatToolPanel); `ContainerWindowProvider` + `ChatPanelDialog.xdl`; `setVisible(True)` required after `createContainerWindow()`. The sidebar layout and resize behavior (anchoring controls and stretching the response area) are handled by `_PanelResizeListener` in `plugin/modules/chatbot/panel_resize.py`, which is wired from `panel_factory.py`.
- **Tool-calling**: WriterAgent detects document type using robust service-based identification (`supportsService`) in `plugin/framework/document.py`.
- **Obsolete Models Removed**: In March 2026, references to GPT-4o, GPT-4o mini, and Gemini 2.0 Flash were removed from the functional model catalog (`DEFAULT_MODELS`) and framework heuristics. Benchmarks and smoke tests were preserved for legacy comparison.
- **OpenRouter STT Cleanup**: OpenRouter's STT model list was streamlined to keep `google/gemini-3.1-flash-lite-preview` as the primary cross-platform audio-native option.
    - **Writer**: `com.sun.star.text.TextDocument`. Writer tools are auto-discovered from `plugin/modules/writer/` (and `plugin/modules/writer/tools/`) and expose `get_document_content`, `apply_document_content` (with **`old_content`** for StrReplace-style section replacement; no positions needed), `search_in_document` (in `plugin/modules/writer/search.py`; use `return_offsets` for character positions) + `get_document_tree`, `read_paragraphs`, `insert_at_paragraph`, `get_document_stats`, `list_styles`, `get_style_info`, `list_tables`, `read_table`, `write_table_cells` (in `plugin/modules/writer/tables.py`) + `generate_image` (create or edit with `source_image='selection'`). Additional advanced tools (comments, undo/redo, image management, tracked changes, navigation, sections/bookmarks, document protection, etc.) are implemented but currently not exposed to the tool API (see `ToolBaseDummy`). The former `replace_in_document` tool was removed; use `apply_document_content(old_content=..., content=...)` instead.
    - **Calc**: `com.sun.star.sheet.SpreadsheetDocument`. Calc tools are auto-discovered from `plugin/modules/calc/tools/`; core logic in `core/calc_*.py`. Additional tools for **charts** (list, create, edit, delete) and **conditional formatting** (list, add, remove, clear) are available in `plugin/modules/calc/charts.py` and `plugin/modules/calc/conditional.py`.
    - **Draw/Impress**: `com.sun.star.drawing.DrawingDocument` or `com.sun.star.presentation.PresentationDocument`. Draw/Impress tools are auto-discovered from `plugin/modules/draw/` and provide slide/page management (`add_slide`, `delete_slide`), shape management (`list_pages`, `get_draw_summary`, `create_shape`, `edit_shape`, `delete_shape`), slide text + speaker notes inspection (`read_slide_text`), and presentation metadata (`get_presentation_info`). Chat and MCP use `get_draw_context_for_chat` in `plugin/framework/document.py`, which summarizes slides, shapes, and notes; all of these behaviors are aligned with the current `nelson-mcp` Draw/Impress implementation.
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
  - **User agent & web cache (disk)**: Search and webpage results from the smolagents tools (`DuckDuckGoSearchTool`, `VisitWebpageTool` in `plugin/contrib/smolagents/default_tools.py`) are cached on disk in a SQLite DB at `{user_config_dir}/writeragent_web_cache.db` when SQLite is available. If the `sqlite3` module is not available (e.g. some bundled Python builds), web cache is disabled (no-op). Cache is shared across processes (retry on lock). Total size is bounded by config `web_cache_max_mb` (default 50, clamp 1–500; 0 disables). Expiration is bounded by config `web_cache_validity_days` (default 7 days, clamp 1-30). Expired entries are safely evicted when encountered. Key normalization: search = collapse whitespace; page = URL strip. All cache logic lives in `default_tools.py`; `web_research.py` passes `cache_path`, `cache_max_mb`, and `cache_max_age_days` from config.
    - **UA selection**: The smolagents HTTP layer uses `_get_user_agent_for_url()` to switch between two constants in `plugin/framework/constants.py`: `USER_AGENT` (WriterAgent identifier: `WriterAgent (https://github.com/keithcu/WriterAgent)`) **specifically for DuckDuckGo and Wikipedia**, and `BROWSER_USER_AGENT` (a Firefox-style UA) for all other hosts by default (on the assumption that many random sites are paranoid and block non-browser UAs). This avoids duplicating UA strings while ensuring Wikipedia/DDG see the truthful WriterAgent identity.

### HTML tool-calling (current)

- **get_document_content**: Returns the document (or a selection/range) as HTML; used to give the model structure-aware context. See `plugin/modules/writer/format_support.py`.
- **apply_document_content**: Preferred Writer tool for edits. Use `old_content` + `content` for section replacement, or `target` (`"full"`, `"range"`, `"search"`, etc.) for positional edits. Plain-text replacements auto-use a **format-preserving** path so existing character formatting (colors, bold, etc.) is kept.
- **Temperature**: Controls randomness (0.0=deterministic, 1.0=creative). Set to `-1` (default) to let the model use its own default.

### System prompt and reasoning (latest)

- **Chat** uses `get_chat_system_prompt_for_document(model, additional_instructions)` in `plugin/framework/constants.py` so the correct prompt is chosen by document type: **Writer** → `DEFAULT_CHAT_SYSTEM_PROMPT` + additional_instructions (get_markdown/apply_markdown, presume document editing, translate/proofread, no preamble); **Calc** → `DEFAULT_CALC_CHAT_SYSTEM_PROMPT` + additional_instructions (semicolon formula syntax, 4-step workflow: understand → get state → use tools → short confirmation; tools grouped READ / WRITE & FORMAT / SHEET MANAGEMENT / CHART / ERRORS). Used by both sidebar and menu Chat.
- **Reasoning tokens**: `plugin/main.py` sends `reasoning: { effort: 'minimal' }` on all chat requests.
- **Thinking display**: Reasoning tokens are shown in the response area as `[Thinking] ... /thinking`. When thinking ends we append a newline after ` /thinking` so the following response text starts on a new line.
- **Persistent Chat History**: Logic in `plugin/framework/history_db.py`. Uses native `sqlite3` for chat history persistence when available; **SQLite is optional**: if `sqlite3` is not available (e.g. some Windows/bundled Python builds), history falls back to JSON files in `writeragent_history.db.d/`. Global guard: `plugin/framework/sqlite_available.py` (`HAS_SQLITE`).
    - **Schema**: Simple `message_store` table compatible with LangChain's SQL history JSON format.
    - **Database Path**: Stored in LibreOffice user config directory (`writeragent_history.db`).
    - **Robust Session IDs**: Instead of document URLs (which break on rename), we store a `WriterAgentSessionID` in the document's **`UserDefinedProperties`** via `getDocumentProperties()`. If missing, we generate one (hash or UUID) and persist it to the file. This ensures history remains linked even if the file is moved or renamed.
    - **Save-As Awareness**: History logic handles "Save As" by either branching the history (generating a new ID) or inheriting it, depending on the implementation.

See [CHAT_SIDEBAR_IMPLEMENTATION.md](CHAT_SIDEBAR_IMPLEMENTATION.md) for implementation details.

- **Streaming I/O**: pure Python queue + main-thread drain
  All streaming paths (sidebar tool-calling, sidebar simple stream, Writer Extend/Edit/menu Chat, Calc) use the same pattern so the UI stays responsive without relying on UNO Timers/listeners:
  - **Worker thread**: Runs blocking API/streaming (e.g. `stream_completion`, `stream_request_with_tools`), puts items on a **`queue.Queue`** (`("chunk", text)`, `("thinking", text)`, `("stream_done", ...)`, `("error", e)`, `("stopped",)`.
  - **Main thread**: After starting the worker, runs a **drain loop**: `q.get(timeout=0.1)` → process item (append text, update status, call on_done/on_error) → **`toolkit.processEventsToIdle()`**. Repeats until job_done.
  - **Generalized Helper**: `run_blocking_with_pump(ctx, func, *args, **kwargs)` in `plugin/framework/async_stream.py` encapsulates this pattern for synchronous-appearing calls (like Calc `=PROMPT()`), allowing them to block *their* logic while still pumping the global UI events so the application remains responsive.
  - **Connection Keep-Alive**: `LlmClient` uses `http.client.HTTPConnection` (or `HTTPSConnection`) for persistent connections. The client instance is cached in `plugin/modules/chatbot/panel_factory.py` (sidebar), `plugin/main.py` (MainJob), and `plugin/prompt_function.py` (Calc =PROMPT()) to reuse connections across multiple requests, significantly improving performance for multi-turn chat and cell recalculations.
  - **Streaming edge cases (LiteLLM-inspired):** `finish_reason=error` → raise; repeated identical content chunks → raise (infinite-loop guard); `finish_reason=stop` with tool_calls → remap to `tool_calls`; delta normalization for Mistral/Azure (`role`/`tool.type`/`function.arguments`). See [LITELLM_INTEGRATION.md](LITELLM_INTEGRATION.md).
- **OpenRouter STT Cleanup**: Cleaned up the STT model list for OpenRouter by removing redundant "audio" capabilities from text/image models and removing OpenRouter from the Whisper entry (as it's not supported there). Added `google/gemini-3.1-flash-lite-preview` to the default catalog as the primary STT model.

### Audio recording (chat sidebar)

- Audio recording in the chat sidebar uses a bundled `sounddevice` + PortAudio stack (see `plugin/modules/chatbot/audio_recorder.py` and `contrib/audio/`).
- On systems where the PortAudio backend is missing or incompatible, attempts to start recording can fail with low-level errors (including `AssertionError` from `sounddevice`). These are now caught and surfaced as a user-friendly `"[Audio error: ...]"` message in the sidebar instead of crashing.
- Typical causes: no microphone devices, misconfigured audio stack, or an older PortAudio library (`libportaudio2`) that does not match the bundled bindings. The error message hints at installing/upgrading PortAudio on Linux when appropriate.

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
- **Tools exposed to LLM** (`plugin/modules/writer/images.py`): **`generate_image`** (prompt, optional `source_image='selection'`, strength, width, height, provider) — creates new image or edits selected (Img2Img). Uses ImageService; when provider is the chat endpoint, after success updates `image_model_lru`.

### UI and config

- **Settings** (`WriterAgentDialogs/SettingsDialog.xdl`): Tabbed. **General** tab: Text/Chat Model and **Image model (same endpoint as chat)** comboboxes (LRU). **Image Settings** tab: shared section (width, height, auto gallery, insert frame, translate prompt) and **AI Horde** section (provider enabled via **"Use AI Horde for Image Generation"** on this tab, `aihorde_api_key`, CFG scale, steps, max wait, NSFW) with a fixedline separator. All image-related keys applied via `_apply_settings_result` in `plugin/main.py`.
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

- **Tool-calling HTML list normalization**: The `apply_document_content` Writer tool now accepts `content` as a JSON array of HTML/paragraph strings at the schema level (matching the system prompt examples). If a provider still sends `content` as a JSON-encoded string (e.g. `"[\\"<h1>...</h1>\\", \\"<p>...</p>\\"]"`), the tool parses it back into a list before importing. This prevents stray paragraphs containing literal `", "` or bracket artifacts when the model follows the "list of strings" hint but the transport layer serializes it as a single string.
- **StrReplace-style section replacement**: For replacing a section without finding character positions, use **`old_content`** + **`content`**. The AI sends the existing text (or HTML excerpt from `get_document_content`) as `old_content` and the replacement as `content`; the system finds the text (converting HTML to plain text via LibreOffice when needed) and replaces it. This replaces the previous flow of `search_in_document(return_offsets=true)` then `apply_document_content(target="range", start=..., end=...)`. The `replace_in_document` tool was removed in favor of this single-tool pattern.

---
 
 ## 3f. Client-side Tool Call Parsers & Hermes
 
 WriterAgent includes client-side parsers to support local models (e.g., Hermes, Mistral, Llama, DeepSeek) that output raw text tool call markers (like `<tool_call>` or `[TOOL_CALLS]`) instead of structured JSON API responses.
 
 - **Trigger**: Standard response processing in `plugin/modules/http/client.py` (`request_with_tools` and `stream_request_with_tools`) includes a fallback: if `tool_calls` is missing from the provider reply but text `content` is present, it uses `get_parser_for_model(model_name)` to process the string.
 - **Implementation**: Located in **`plugin/contrib/tool_call_parsers/`**. 
 - **Shims**: High-level individual parser modules use an internal `openai_compat.py` mock layer so that upstream files do not crash without an `openai` library installation inside the LibreOffice Python environment.
 - **Hermes Slash Commands**: When the Agent Backend is set to Hermes, WriterAgent supports Hermes' native slash commands (like `/help`, `/reset`, `/model`). It detects the `/` prefix and forwards the message exactly as-is over the ACP stdio transport, bypassing `[DOCUMENT CONTENT]` context wrapping so the Hermes agent intercepts it cleanly.
 - **Credits**: Extracted and adapted from **[hermes-agent](https://github.com/NousResearch/hermes-agent)** (specifically code from its `environments/tool_call_parsers/` directory). Retaining original logic structures for easy upstream merging updates.
 
 ---

## 3g. Threading and Process Management Consolidation
 
 **Issue**: The codebase had scattered, ad-hoc usages of `threading.Thread` and `subprocess.Popen` for background tasks, leading to redundancy, potential resource leaks, and inconsistent error handling.
 
 **Fix**:
 - Created `plugin/framework/worker_pool.py` with `run_in_background` to standardize daemon thread creation with built-in exception catching and logging.
 - Created `plugin/framework/process_manager.py` with `AsyncProcess` to handle `subprocess.Popen` lifecycles, stream draining (stdout/stderr), and exit callbacks asynchronously.
 - Refactored `plugin/modules/http/mcp_protocol.py`, `plugin/main.py`, `plugin/framework/dialogs.py`, `plugin/modules/chatbot/send_handlers.py`, and `plugin/modules/chatbot/tool_loop.py` to use `run_in_background` instead of bare `threading.Thread`.
 - Refactored `plugin/modules/launcher/__init__.py` and `plugin/modules/tunnel/__init__.py` to manage their external CLIs using `AsyncProcess`.
 - Refactored `plugin/modules/agent_backend/cli_backend.py` to use `run_in_background` for its reader loops.
 
 **Result**: Threading and process management are now centralized in the framework, making the system more robust and easier to maintain.
 
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

- **Goal**: When making text edits, preserve character-level formatting (fonts, colors, bold/italic) even if the replacement length changes.
- **How to use it**: Prefer the Writer tool `apply_document_content` with **plain-text** `content` where possible; the implementation in `plugin/modules/writer/format_support.py` automatically chooses a format-preserving path for simple text replacements.
- **Gotcha**: Avoid feeding HTML-wrapped strings into the format-preserving path; keep raw text for preservation, and let HTML/Markdown content go through the normal import path.


## 5. Config File

- **Path**: LibreOffice UserConfig directory + `writeragent.json`
  - Linux: `~/.config/libreoffice/4/user/writeragent.json` (or `24/user` for LO 24)
  - macOS: `~/Library/Application Support/LibreOffice/4/user/writeragent.json`
  - Windows: `%APPDATA%\LibreOffice\4\user\writeragent.json`
- **Single file**: No presets or multiple configs. To use a different setup, copy your config to the path above as `writeragent.json`.
- **Settings Dialog Integration**: AI and service configurations are unified in the `WriterAgent -> Settings` dialog. The Settings dialog tabs for service configuration (e.g., Http, Launcher, Chatbot, Agent Backend) are auto-generated from `module.yaml` configuration schemas by `scripts/generate_manifest.py`. Writer has no config so no Writer tab; Tunnel is skipped in the UI (see Section 3).
- **Settings dialog** reads/writes this file via `get_config()` / `set_config()` in `plugin/framework/config.py`. Use **`get_current_endpoint(ctx)`** for the normalized current endpoint URL (single source; used by plugin/main.py and panel_factory.py).
- **Chat-related keys**: `chat_context_length` (default 8000), `chat_max_tokens` (default 512 menu / 16384 sidebar), `additional_instructions`. Also **per-endpoint API keys**: `api_keys_by_endpoint` (JSON map: normalized endpoint URL → API key); `get_api_key_for_endpoint(ctx, endpoint)` / `set_api_key_for_endpoint(ctx, endpoint, key)` in `plugin/framework/config.py`. Legacy `api_key` is migrated once into the map under the current endpoint and then removed. Settings dialog shows and saves the key for the selected endpoint.
- **Model keys**: `text_model` (chat/LLM model; backward compat: `model`), `model_lru` (recent text models); `image_model` (image model when using chat endpoint for images), `image_model_lru` (recent image models). See Section 3d.

---

## 5b. Log Files

- **Unified debug log**: Written by `debug_log(msg, context=...)` with prefixes `[API]`, `[Chat]`, `[Markdown]`, `[AIHorde]`. Paths set once via `init_logging(ctx)`; no ctx needed at call sites.
- **Agent log** (NDJSON, optional): `writeragent_agent.log` in the same directory. Written by `agent_log(...)` only when config key `enable_agent_log` is true (default false). Used for hypothesis/debug tracking.
- **Runtime refresh**: `init_logging(ctx)` re-reads `enable_agent_log` and `log_level` on subsequent calls, so turning agent logging on in Settings takes effect without requiring a LibreOffice restart.
- **Watchdog**: If no activity for the threshold (e.g. 30s), a line is written to the debug log and the status control shows "Hung: ...".

### Finding log files (and image generation debugging)

Debug log path is the **same directory as `writeragent.json`** (LibreOffice user config from `PathSettings.UserConfig`). Filename: `writeragent_debug.log`. If the user config dir is unavailable at init, fallback is `~/writeragent_debug.log`. Typical locations (check the one that matches your LibreOffice version):

- Linux: `~/.config/libreoffice/4/user/writeragent_debug.log` or `~/.config/libreoffice/24/user/writeragent_debug.log`
- macOS: `~/Library/Application Support/LibreOffice/4/user/writeragent_debug.log` (or `24/user/`)
- Windows: `%APPDATA%\LibreOffice\4\user\writeragent_debug.log` (or `24\user\`)
- **Fallback**: `~/writeragent_debug.log` (and `~/writeragent_agent.log` for the agent log)

If logs appear empty, check both versioned user dirs (e.g. `4/user` and `24/user`) and your home directory for the fallback file. Write failures (e.g. permissions) are silent; the code does not surface them.

**Which logs show image generation failures:**

- **AI Horde** (`image_provider=aihorde`): `writeragent_debug.log` — search for `[AIHorde]` for request flow, errors, and stack traces. `plugin/framework/aihordeclient/` uses `debug_log` and `log_exception` with context `"AIHorde"`.
- **Endpoint** (`image_provider=endpoint`): Debug log only shows `[Chat] Tool call: generate_image(...)` (no error text). For the actual error: enable **Settings → Enable agent log**, reproduce, then open `writeragent_agent.log` and look for `"Tool result"` with `tool` `"generate_image"` — the error is in `data.result_snippet`. `plugin/framework/image_utils.py` does not write to the debug log.

---

## 5c. Chatbot REST API & Calc chart tool cleanup

- **Chatbot REST API handler (optional)**: `plugin/modules/chatbot/__init__.py` now treats the legacy Chat API handler (`plugin.modules.chatbot.handler.ChatApiHandler`) as **optional**. If the handler module/class is missing, route registration for `/api/chat` is skipped with a clear warning log instead of failing module initialization, so `make test` and other entry points no longer emit repeated “Failed to load module chatbot” errors. When a handler implementation is added back in the future, the routes will be registered automatically again.
- **Calc `create_chart` tool de-duplication**: A duplicate `create_chart` tool definition in `plugin/modules/calc/sheets.py` was removed so the canonical implementation in `plugin/modules/calc/charts.py` is the only one discovered by the tool registry. This eliminates noisy “Tool already registered, replacing: create_chart” warnings while preserving the existing chart-creation behavior.

## 6. Build and Install

```bash
make build
make deploy   # or remove first: unopkg remove org.extension.writeragent
```

Restart LibreOffice after install/update. Test: menu **WriterAgent → Settings** and **WriterAgent → Edit Selection**.

**Build without voice recording:** Run `make build-no-recording` (or `make build NO_RECORDING=1`) to produce an .oxt that excludes voice/audio recording: the bundle omits `contrib/audio/` and `plugin/modules/chatbot/audio_recorder.py`; the Chat sidebar stays and the Record button is simply not shown. This reduces extension size when recording is not needed.

**Release build**: `make release` builds an .oxt without `plugin/tests/` or `plugin/testing_runner.py`. The build script reads `extension/Addons.xcu`, strips the **Debug** submenu node (Run format tests, Run calc tests, etc.), and writes the result to the bundle so the release menu has no test entries.

**Testing & QA**:
- **make test**: Runs both standard `pytest` (for core logic) and an in-process `testing_runner` (for UNO/LibreOffice integration). The `Makefile` automatically detects a Python interpreter with the `uno` module available, even when running from within a virtual environment.
- **Localized Style Support**: Writer integration tests handle localized style names (e.g., fallback between "Default Paragraph Style" and "Standard") to ensure compatibility across different OS/Linux builds.

---

## 7. What to Do Next

### Optional refactoring (future work)
- **plugin/framework/config.py**: Introduce internal helpers for "read full config" / "write full config" (e.g. build on `get_config_dict`) so `set_config`/`remove_config` share one write path and future caching or storage changes touch one place.
- **panel_factory.py**: Consider a small doc-type registry (Writer/Calc/Draw → tools + execute function) so choosing tools and executor in `_do_send` is data-driven and adding a new doc type doesn't require editing a long if/elif chain.

### Chat settings in UI — DONE
- ~~Expose `chat_context_length`, `chat_max_tokens`, `additional_instructions` in the Settings dialog~~ (implemented in SettingsDialog.xdl).
  - *Fix:* Added missing fields into `_get_settings_field_specs()` in `settings_dialog.py` so that they are explicitly saved and applied.

### Writer Tools Expansion — DONE
- ~~**Writer tool set expansion**~~: Added 12 new Writer tools in `plugin/modules/writer/ops.py` and wired into `plugin/modules/writer/tools.py`. Removed 7 legacy unused functions. New tools: `list_styles`, `get_style_info`, `list_comments`, `add_comment`, `delete_comment`, `set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes`, `list_tables`, `read_table`, `write_table_cells`. System prompt updated to mention them.

### MCP Server (external AI client access) — DONE
- **`plugin/modules/http/mcp_protocol.py`**: `_Future`, `execute_on_main_thread_mcp`, `drain_mcp_queue` — work is queued from HTTP handler threads and drained on the main thread.
- **`plugin/modules/http/mcp_protocol.py`** and **`plugin/modules/http/server.py`**: MCP JSON-RPC (POST `/mcp`, POST `/sse`, POST `/messages`, POST `/debug`). **Document targeting**: Implemented via `X-Document-URL` HTTP header; the server reads the header and resolves the document by enumerating `desktop.getComponents()` and matching `getURL()`. Used for `tools/list` and `tools/call`; when the header is absent, the server falls back to the active document. See `plugin/framework/document.py` (`resolve_document_by_url`) and `docs/mcp-protocol.md`. Port utilities in server/routes as needed.
- **Idle-time draining**: **AsyncCallback thread** in `plugin/main.py` (Path A). A background Python thread schedules `XCallback` via `com.sun.star.awt.AsyncCallback` every 100ms, which safely executes `drain_mcp_queue()` on the main VCL thread. Option B (piggyback on the chat stream drain loop) was **not** used — it would only service MCP during active chat, which is inadequate for standalone MCP use.
- **Config**: `mcp_enabled` (default false), `mcp_port` (default 8765). MCP settings are on the **Http** tab of the Settings dialog (auto-generated from `plugin/modules/http/module.yaml`); Enable MCP Server checkbox, Port field, "Localhost only, no auth." label.
- **Menu**: "Toggle MCP Server" and "MCP Server Status" under WriterAgent. Status dialog shows RUNNING/STOPPED, port, URL, and health check. Auto-start: when user saves Settings with MCP enabled, server (and timer) start if not already running.
- **Icons**: `assets/` includes `running_16.png`, `running_26.png`, `starting_16.png`, `starting_26.png`, `stopped_16.png`, `stopped_26.png` (from libreoffice-mcp-extension).
- See **`MCP_PROTOCOL.md`** for protocol details and architecture.

- **Document Tree & Navigation (DONE)**: Ported `build_heading_tree`, `ensure_heading_bookmarks`, and `resolve_locator` to `plugin/framework/document.py`. New tools `get_document_outline` and `get_heading_content` provide structured access to long documents.

- **Performance (DONE)**: Frequently used regular expressions (e.g., cell reference parsing in `error_detector.py`) are pre-compiled as module-level constants to avoid redundant compilation and cache lookups. Batch operations and efficient set comprehensions are preferred for large-scale document or spreadsheet analysis.

## 7b. Future Roadmap

- **Richer Context**: Metadata awareness (word counts, styles, formula dependencies).
- **Safer Workflows**: Propose-first execution with user confirmation (diff preview).
- **Predictive Typing**: Trigram-based "ghost text" for real-time drafting assist.
- **Reliability Foundations**: Robust timeouts, clear error prompts, and rollback safety.
- **Suite Completeness**: Finalizing Draw and Impress slide/shape toolsets.
- **Offline First**: Continued focus on performance with the fastest local models (Ollama, etc.) to ensure privacy and speed without cloud dependencies.
- **Hybrid Orchestrator / Handover**: Future capability to "hand over" complex tasks from the sidebar to high-power external CLI agents (like `claude-code`) using the built-in MCP server.

Image generation and AI Horde integration are **complete** (unified `generate_image` with optional edit via `source_image='selection'`, AI Horde + endpoint providers, Image Settings tab with shared vs Horde-only sections).

For the DSPy/OpenRouter prompt evaluation framework under `scripts/prompt_optimization/`, the multi-model suite in `model_configs.py` now also includes **`nvidia/nemotron-3-super-120b-a12b:free`**, so you can run `run_eval_multi.py` against this free Nemotron 3 Super 120B variant and see it ranked in the intelligence-per-dollar leaderboard.

---

### 7d. Experimental Planning Todo Store (Hermes-style) — March 2026

- Added `plugin/contrib/todo_store.py`, an internal in-memory `TodoStore` adapted from `hermes-agent`'s `tools/todo_tool.py`. It provides a session-scoped task list with items `{id, content, status}` and helper `todo_tool()` that returns a JSON payload mirroring Hermes's schema. It intentionally lives under `plugin/contrib/` because the logic originates from an external project.
- Added `plugin/modules/chatbot/tools/` with `__init__.py` and an **inert** `todo.py`. The file contains a commented-out `TodoTool(ToolBase)` example that wraps `TodoStore` (importing from `plugin.contrib.todo_store`) but lives entirely inside a module docstring, so the tool registry does **not** discover or register it yet.
- Added comments in `plugin/modules/chatbot/tool_loop.py` and `panel.py` that show where to:
  - Attach a `TodoStore` instance to `SendButtonListener` / `ToolContext.services` (key `"todo_store"`).
  - Reset the store on Clear so each chat session starts with an empty task list.
- Added a commented planning block below `DEFAULT_CHAT_SYSTEM_PROMPT` in `plugin/framework/constants.py` describing how to use a `todo` tool for multi-step planning. This guidance is not currently part of the live prompt; when we decide to expose the planning tool, that comment can be inlined into the prompt and the `TodoTool` code can be uncommented.

Net effect: the code for a Hermes-compatible planning todo tool is present and documented, but **no new tools are exposed to LLMs yet**. Enabling it will be a small, explicit change: uncomment the `TodoTool` implementation in `chatbot/tools/todo.py`, wire `TodoStore` into `ToolContext.services`, and move the commented planning section into the active Writer chat system prompt.

## 7c. Sidebar theming (dark mode) — March 2026 cleanup

- **What we tried**:
  - Ported nelson-mcp's idea of reading dialog colors from `/org.openoffice.Office.UI/ColorScheme` → `ColorSchemes` → current scheme, probing keys like `DialogColor`, `WindowColor`, `AppBackground`, and nested `Color` nodes.
  - Added a lot of logging and fallbacks (scheme-name heuristics, hierarchical names, direct leaf paths) in `get_sidebar_background_color` and wired it into the chat sidebar via `_apply_sidebar_theme`.
- **What we observed**:
  - On a modern LibreOffice build with `COLOR_SCHEME_LIBREOFFICE_AUTOMATIC`, all of those configuration APIs returned `None` for the actual RGB values (even though `Color` showed up as a property/element name).
  - Meanwhile, LibreOffice itself was already theming the sidebar **controls** correctly in both light and dark mode; our only visible regression was the root container occasionally being forced to a hard-coded light gray.
- **Final decision**:
  - **Disable custom theming for the chat sidebar**: `_apply_sidebar_theme` in `plugin/modules/chatbot/panel_wiring.py` is now a no-op that just logs and returns.
  - **Remove `get_sidebar_background_color` entirely** from `plugin/framework/uno_helpers.py`; no callers remain.
  - Rely on LibreOffice's own VCL theming for both the sidebar container and all controls. This matches what users actually see working in practice and avoids fragile, version-specific ColorScheme probing.

**Takeaway for future work**: Before adding ColorScheme-based theming, confirm that the configuration API actually returns usable RGB values on the target LibreOffice versions. If not, prefer letting LibreOffice handle dark/light mode by default and only layer on minimal, well-tested overrides.

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
- **API Keys / Security**: API keys MUST be handled via the Settings dialog and stored in `writeragent.json`. Never bake in fallbacks to environment variables (like `OPENROUTER_API_KEY`) in production code, as this bypasses the user's manual configuration and complicates privacy auditing. Env vars are for developer testing ONLY. For temporary file creation, strictly avoid the deprecated and insecure `tempfile.mktemp()` to prevent race condition vulnerabilities. Instead, use `tempfile.mkstemp()` (immediately closing the returned file descriptor) or `tempfile.NamedTemporaryFile()`.
- **MCP Server**: The MCP HTTP server and UNO Timer for `drain_mcp_queue` are started from `plugin/main.py` only (not from the sidebar). Server binds to localhost only; no authentication. Document targeting is implemented via the `X-Document-URL` header: the server resolves the document by enumerating desktop components and matching URL; fallback is the active document. External clients should send this header when multiple documents are open to avoid races.
- **Calc tool schemas (Gemini/OpenRouter)**: Google Gemini (e.g. via OpenRouter) rejects union types in tool parameters (e.g. `"type": ["string", "array"]`) and expects `required` properties to be single-type. Calc tools in `plugin/modules/calc/cells.py` and `formulas.py` use `"type": "array"` with `"items": {"type": "string"}` for `range_name`, and single types for other params; execute methods normalize a single string to `[string]` so callers can still pass one range.

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
