# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file summarizes invariants and where to look in code.

> [!IMPORTANT]
> **AI Assistants: You MUST update this file after making (nontrivial) changes to the project.** This ensures the next assistant has up-to-date context without manual handoff.

---

## Quick orientation

Common touchpoints: [`plugin/main.py`](plugin/main.py) (MainJob, settings apply), [`plugin/modules/chatbot/panel_factory.py`](plugin/modules/chatbot/panel_factory.py) (sidebar, `SendButtonListener`), [`plugin/modules/chatbot/tool_loop.py`](plugin/modules/chatbot/tool_loop.py), [`plugin/modules/chatbot/panel.py`](plugin/modules/chatbot/panel.py), [`plugin/framework/document.py`](plugin/framework/document.py), [`plugin/framework/config.py`](plugin/framework/config.py), [`plugin/framework/extension_update_check.py`](plugin/framework/extension_update_check.py) (weekly update check), [`plugin/modules/http/client.py`](plugin/modules/http/client.py), [`plugin/framework/errors.py`](plugin/framework/errors.py), [`plugin/framework/dialogs.py`](plugin/framework/dialogs.py), [`plugin/modules/writer/format_support.py`](plugin/modules/writer/format_support.py). Deep dive: [CHAT_SIDEBAR_IMPLEMENTATION.md](CHAT_SIDEBAR_IMPLEMENTATION.md). Writer nested tool domains (`delegate_to_specialized_writer_toolset`, tier filtering): [docs/features/writer-specialized-toolsets.md](docs/features/writer-specialized-toolsets.md).

---

## 1. Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw:

- **Build & Dev**: `make build` (runs **`ty`** then bundle), `make deploy`. **`plugin/_manifest.py`** is gitignored; **`make ty`**, **`make check`** (ty only), **`make typecheck`** (ty + mypy + pyright), and **`make test`** all use **`make manifest`** where applicable so clean checkouts get a generated manifest before type-check. If the file is still absent, [`plugin/framework/module_loader.py`](plugin/framework/module_loader.py) `load_manifest()` raises **`RuntimeError`** (no silent empty module list). **External tools**: `make fix-uno` to link system UNO into `.venv`. **Typecheckers**: **`make check`** / **`make build`** → **`ty`** only; **`make typecheck`** → **`ty` + mypy + pyright**; **`make test`** → typecheck, then **`bandit`** on **`plugin/`** (excludes **`plugin/contrib`** and **`plugin/tests`**, see **`[tool.bandit]`** in **`pyproject.toml`**), then pytest + LO tests; **`make release`** runs **`make test`** first. Details: [`docs/type-checking.md`](docs/type-checking.md).
- **Extend Selection** (Ctrl+Q) / **Edit Selection** (Ctrl+E): model continues or rewrites the selection.
- **Chat with Document**: sidebar (multi-turn + tool-calling), persistent history (SQLite when available, else JSON under `writeragent_history.db.d/`), menu fallback (Writer: append; Calc: "AI Response" sheet).
- **Settings**: endpoint, models, keys, timeouts, image provider, MCP, etc. Config: `writeragent.json` in LibreOffice user config. Examples: [CONFIG_EXAMPLES.md](CONFIG_EXAMPLES.md).
- **Experimental memory** (file-backed `USER.md` / `MEMORY.md`): [`plugin/modules/chatbot/memory.py`](plugin/modules/chatbot/memory.py) (store raises **`ConfigError`** if the UNO user config directory cannot be resolved). Writer prompt includes `MEMORY_GUIDANCE` in [`plugin/framework/constants.py`](plugin/framework/constants.py). Full description: [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md) (Hermes reference: automatic memory-in-prompt, frozen snapshot, periodic background review agent; WriterAgent injection not enabled).
- **Images**: unified `generate_image` tool; `source_image='selection'` for edit. Contract: `ImageProvider.generate()` → `(paths_list, error_message_str)`. See [`plugin/framework/image_utils.py`](plugin/framework/image_utils.py), [docs/features/image-generation.md](docs/features/image-generation.md), [IMAGE_GENERATION.md](IMAGE_GENERATION.md).
- **Calc** `=PROMPT()`: [`plugin/prompt_function.py`](plugin/prompt_function.py).
- **MCP** (opt-in): localhost HTTP; document targeting via `X-Document-URL`. See [MCP_PROTOCOL.md](MCP_PROTOCOL.md), [docs/mcp-protocol.md](docs/mcp-protocol.md).

**HTTP / auth**: Persistent connections in [`plugin/modules/ai/service.py`](plugin/modules/ai/service.py); `USER_AGENT` / headers from `core.constants`; per-endpoint auth in [`plugin/framework/auth.py`](plugin/framework/auth.py); `LlmClient._headers()` adds `Authorization: Bearer` when appropriate. **Local HTTPS**: verify first, then one retry with unverified context on cert errors (no user toggle).

**Python**: 3.11–3.14 per [`pyproject.toml`](pyproject.toml). GPL v3+; prior contributors credited in headers/installer.

---

## 2. Repository layout

```
writeragent/
├── plugin/
│   ├── main.py, prompt_function.py, _manifest.py
│   ├── framework/     # config, document, state, tool_registry, dialogs, logging, …
│   └── modules/       # ai, chatbot, writer, calc, draw, http (MCP), tunnel, …
├── WriterAgentDialogs/  # XDL
├── registry/, scripts/, Makefile, pyproject.toml
└── writeragent.json.example
```

UNO split (no monolithic `uno_helpers.py`): context helpers → [`plugin/framework/uno_context.py`](plugin/framework/uno_context.py); document helpers → [`plugin/framework/document.py`](plugin/framework/document.py); dialog control helpers (`get_optional`, checkbox helpers, `TabListener`) → [`plugin/framework/dialogs.py`](plugin/framework/dialogs.py).

---

## 3. Dialogs (XDL)

- Load with `DialogProvider.createDialog(base_url + "/WriterAgentDialogs/…")`; `base_url` from `PackageInformationProvider` + **`self.ctx`** — not `uno.getComponentContext()`.
- Multi-page: **`dlg:page` on controls** + `dlg.getModel().Step`; do **not** use `tabpagecontainer` / `tabpage` (not in DTD; silent failure).

---

## 4. Chat, sidebar, and streaming

**Scope**: Sidebar + menu chat for **Writer and Calc** (same deck). Draw supported for chat/tools per product code paths.

- **Theming**: Native VCL (light/dark); no custom color probing.
- **Config sync**: `global_event_bus.subscribe("config:changed", ...)` and `global_event_bus.emit("config:changed", ...)` using [`plugin/framework/event_bus.py`](plugin/framework/event_bus.py).
- **Lifecycle**: Send disabled / Stop enabled at start of `actionPerformed`; restored in **`finally`** after `_do_send()` returns. `_set_button_states` uses per-control try/except. `_send_busy` mirrors that window.
- **FSM** ([`plugin/framework/state.py`](plugin/framework/state.py)): Pure `next_state(state, event) → FsmTransition`; **no** UNO/I/O/logging/`EventBus` inside transitions. Effects run in panel/mixins/MCP. Composite: [`sidebar_state.py`](plugin/modules/chatbot/sidebar_state.py) (`send`, `tool_loop`, mirrored `audio`). `ToolCallingMixin` clears the tool-loop slice in **`finally`** after drain. **`EventBus`**: loose notifications only (e.g. config, `mcp:request`), not the FSM driver. Other `*_state.py` modules under `chatbot/` and `http/mcp_state.py` for domains. Send-handler sidebar paths ([`state_machine.py`](plugin/modules/chatbot/state_machine.py)): `SendHandlerState.handler_type` / `.status` and `CompleteJobEffect.terminal_status` are typed as `Literal` aliases in [`plugin/framework/types.py`](plugin/framework/types.py) (`SendHandlerKind`, `SendHandlerFsmStatus`, `SendHandlerCompleteStatus`). Tool loop: control-flow effects `ExitLoopEffect`, `TriggerNextToolEffect`, `SpawnFinalStreamEffect`, `UpdateDocumentContextEffect` plus UI channel `ToolLoopUIEffect` in [`tool_loop_state.py`](plugin/modules/chatbot/tool_loop_state.py); send-handler UI: `SendHandlerUIEffect` in [`state_machine.py`](plugin/modules/chatbot/state_machine.py); shared `UIEffectKind` for `.kind` in [`types.py`](plugin/framework/types.py); string-literal audits: [`refactor_tool_registry.py`](refactor_tool_registry.py) (`--audit-uieffect`, `--audit-chat-fsm`).
- **Panel**: [`panel_factory.py`](plugin/modules/chatbot/panel_factory.py) + `ChatPanelDialog.xdl`; **`setVisible(True)`** after `createContainerWindow()`. Resize: [`panel_resize.py`](plugin/modules/chatbot/panel_resize.py), wired from [`panel_wiring.py`](plugin/modules/chatbot/panel_wiring.py).
- **Doc type**: [`plugin/framework/document.py`](plugin/framework/document.py) (`supportsService`).

**Document context** (chat only): Each send **replaces** the single `[DOCUMENT CONTENT]` system message. Writer: `get_document_context_for_chat` (excerpts, `[SELECTION_START]`/`[SELECTION_END]`). Calc: `get_calc_context_for_chat` — **`ctx` required** (panel/MainJob); never `uno.getComponentContext()` on this path. Signature pattern: `(model, max_context, ctx=None)`.

**Streaming**: Worker thread → `queue.Queue`; first tuple element must be [`StreamQueueKind`](plugin/framework/async_stream.py) (enum members, not bare strings). Agent backends may emit `TOOL_CALL` / `TOOL_RESULT`; the drain shows them as `[Tool call]` / `[Tool result]` text lines. Main thread drain loop + **`toolkit.processEventsToIdle()`**. **Do not** use UNO `XTimerListener` in the sidebar for this. [`run_blocking_in_thread`](plugin/framework/async_stream.py) pumps the UI while awaiting a result; its internal queue uses [`BlockingPumpKind`](plugin/framework/async_stream.py) only on dequeue (no silent `str` normalization). **`LlmClient`** cached on sidebar, MainJob, and `prompt_function` for keep-alive. Edge cases: [LITELLM_INTEGRATION.md](LITELLM_INTEGRATION.md). Overview: [docs/stream-queue-kind-migration.md](docs/stream-queue-kind-migration.md).

**History**: [`plugin/modules/chatbot/history_db.py`](plugin/modules/chatbot/history_db.py); `HAS_SQLITE` in [`plugin/framework/sqlite_available.py`](plugin/framework/sqlite_available.py). Session id: `WriterAgentSessionID` in document **UserDefinedProperties** (not URL-only).

**Reasoning**: `plugin/main.py` sends `reasoning: { effort: 'minimal' }`; UI shows `[Thinking] … /thinking` then newline before answer.

**Audio** ([`audio_recorder.py`](plugin/modules/chatbot/audio_recorder.py), `contrib/audio/`): One `AudioRecorder` per `SendButtonListener`; PortAudio failures → `"[Audio error: …]"` in UI.

### Web search checkbox

Bypasses document context/tools for that send; calls `web_research`. **`panel_factory.py`**: `ResearchChatToggledListener` imports `_` inside `on_item_state_changed` — the path-climbing loop at top of file must **not** use `for _ in …` (shadows gettext `_`). **Approval** (`chatbot.prompt_for_web_research`): Send = Accept, Stop = Change (edit query in [`show_web_search_query_edit_dialog`](plugin/framework/dialogs.py)), Clear = Reject. In [`tool_loop.py`](plugin/modules/chatbot/tool_loop.py) `execute_fn`, the same HITL wiring applies to `web_research` and to `delegate_to_specialized_{writer,calc,draw}_toolset` when `domain=web_research` (those delegates forward to `WebResearchTool`). **`WebResearchTool.is_async()`** so approval does not deadlock the UI thread. ACP permission: `submit_approval` / QUERYBOX for Yes/No. Details: [`web_research.py`](plugin/modules/chatbot/web_research.py), [`web_research_chat.py`](plugin/modules/chatbot/web_research_chat.py), smolagents in `plugin/contrib/smolagents/` (cache, UA split DDG/Wikipedia vs browser UA — see `default_tools.py`, `constants.py`). The research sub-agent uses **`WebResearchToolCallingAgent`**: `chat_max_tool_rounds` is reflected in **`instructions`** (reserve one step for `final_answer`) and **`ToolCallingAgent.augment_messages_for_step`** injects a per-turn USER line with steps used and remaining before each model call.

**Direct image checkbox** (`chat_direct_image`): `DirectImageCheckListener` in `_wire_image_ui` must override `on_item_state_changed` on the **class** (same pattern as `ResearchChatToggledListener`). Nesting the handler inside `__init__` does not override [`BaseItemListener`](plugin/framework/listeners.py) and the chat/image control swap never runs on toggle. **`generate_image`** is specialized-tier ([`ToolWriterImageBase`](plugin/modules/writer/base.py) in [`images.py`](plugin/modules/writer/images.py)): `is_async()` so provider/HTTP runs on the tool worker (keeps the sidebar drain loop responsive); UNO read/insert steps use [`execute_on_main_thread`](plugin/framework/queue_executor.py) inside the tool (`request_timeout` for waits). Direct image path: `get_tools().execute("generate_image", …)` on the worker — not wrapping the whole call. Omitted from default LLM tool lists; use **`delegate_to_specialized_writer_toolset(domain=images)`** for image tools via the sub-agent (async tools run on the worker in [`specialized.py`](plugin/modules/writer/specialized.py) `WrappedSmolTool`).

**smolagents [`agents.py`](plugin/contrib/smolagents/agents.py)** — Vendored fork ships only `MultiStepAgent` and `ToolCallingAgent` (no `CodeAgent`, Hugging Face Hub save/load, or related helpers). `ToolCallingAgent.augment_messages_for_step` (default no-op) runs after `write_memory_to_messages()` each step so subclasses can prepend/append messages (web research uses this for step budget).

**smolagents [`toolcalling_agent_prompts.py`](plugin/contrib/smolagents/toolcalling_agent_prompts.py)** — Bundled default prompts for smolagents `ToolCallingAgent` (triple-quoted strings, easier to edit than JSON-escaped blobs). Dict `TOOLCALLING_PROMPT_TEMPLATES`: `system_prompt` template includes a swappable few-shot block at `__EXAMPLES_BLOCK__` (defaults: `DEFAULT_EXAMPLES_BLOCK` for web-style tools; presets `LIBRARIAN_EXAMPLES_BLOCK`, `SPECIALIZED_EXAMPLES_BLOCK` for onboarding and specialized sub-agents). Other placeholders: `__TOOLS_LIST__`, `__MANAGED_AGENTS_BLOCK__`, `__CUSTOM_INSTRUCTIONS__` (from `instructions`). If `system_prompt_examples` is omitted, `ToolCallingAgent.initialize_system_prompt` uses `DEFAULT_EXAMPLES_BLOCK`. Pass custom few-shots via `ToolCallingAgent(system_prompt_examples=...)` or bake them into a full template with `build_toolcalling_prompt_templates(examples_block)`. `planning` (`initial_plan`, `update_plan_pre_messages`, `update_plan_post_messages` — Jinja blocks for tools/agents), `managed_agent` (`task`, `report`), `final_answer` (`pre_messages`, `post_messages`). Loaded in `ToolCallingAgent.__init__` when `prompt_templates` is `None`.

### Memory upsert (`upsert_memory`)

Sidebar shows when the model calls `upsert_memory`: **main document chat** uses the tool-loop FSM in [`tool_loop_state.py`](plugin/modules/chatbot/tool_loop_state.py) — a line like `[Memory update: key '…']` when the key is present in arguments, then the usual tool result line. **Librarian onboarding** uses [`librarian.py`](plugin/modules/chatbot/librarian.py): the same style line is sent through `ToolContext.chat_append_callback` (chunk path) so it is visible even when `chatbot.show_search_thinking` is off (other librarian tool progress still goes through the thinking stream only).

### Librarian handoff

Sidebar onboarding still **starts** when `USER.md` is empty, but once the librarian has started the active [`SendButtonListener`](plugin/modules/chatbot/panel.py) keeps an in-memory `_in_librarian_mode` flag so later turns stay with the librarian even after `upsert_memory` writes preferences to `USER.md`. That panel-local flag is cleared only when the librarian path in [`send_handlers.py`](plugin/modules/chatbot/send_handlers.py) receives `status == "switch_mode"` from `switch_to_document_mode`; `USER.md` is preference storage only and no longer doubles as the handoff signal.

### Tools by document type

Paragraph tools in [`content.py`](plugin/modules/writer/content.py) are **`ToolBaseDummy`** until rebased. **Specialized tier** (`ToolWriterSpecialBase` in [`base.py`](plugin/modules/writer/base.py)): styles, page (page styles, margins, headers/footers, columns, page breaks), textframes (`list_text_frames`, `get_text_frame_info`, `set_text_frame_properties`), shapes/charts in doc, indexes, fields, bookmarks, embedded, **images** (`generate_image`, list/insert/replace, …), **track changes** (`set_track_changes`, `get_tracked_changes`, `manage_tracked_changes`), and **Writer** `create_shape` — omitted from default chat/MCP tool lists via `exclude_tiers` in [`tool_registry.py`](plugin/framework/tool_registry.py). `create_shape` remains visible for Draw/Impress default lists (shared tool name; tier exception in `get_tools`).

**Testing specialized tools**: Tests should retrieve tools via `plugin.main.get_tools().get("tool_name")` rather than direct internal imports. This avoids regressions when tools are moved between specialized modules and allows bypassing tier-based filtering.

**In-place specialized mode** (`USE_SUB_AGENT = False` in [`plugin/modules/writer/specialized.py`](plugin/modules/writer/specialized.py) and [`plugin/modules/calc/specialized.py`](plugin/modules/calc/specialized.py)): `ToolRegistry.get_tools(..., active_domain=...)` restricts tools to the matching domain’s specialized classes—Writer (`ToolWriterSpecialBase`), Calc (`ToolCalcSpecialBase`), and Draw (`ToolDrawSpecialBase`)—plus `specialized_workflow_finished` / `final_answer` / `reply_to_user` as applicable. [`ToolCallingMixin._refresh_active_tools_for_session`](plugin/modules/chatbot/tool_loop.py) recomputes OpenAI tool schemas from `session.active_specialized_domain` before each LLM round so in-place delegation stays consistent within one user send.

**Shape Enhancements**: Shape tools for Draw/Impress (`create_shape`, `edit_shape`) have been enhanced to support generic shape types (e.g. `connector` or direct UNO class names), LibreOffice `CustomShape` type strings, and rich formatting properties like `fill_color`, `line_color`, `line_width`, `font_size`, `font_name`, and `rotation_angle`. For names like `octagon`, [`plugin/modules/draw/shapes.py`](plugin/modules/draw/shapes.py) sets `CustomShapeEngine` / `CustomShapeGeometry` (`Type`) **before** `page.add` (inside `safe_create_shape`). In **Writer**, `CustomShapeGeometry` often reads back empty immediately after `page.add`; `CreateShape` re-applies the same engine/geometry when readback is empty so catalog shapes render. `create_shape` also returns `page_index` and `shape_count_after` for debugging. The tool schema describes CustomShape types by category (with examples); the full `flowchart-*` and other names still work at runtime. The `shapes_connect` and `shapes_group` tools are also fully implemented in Draw and inherited by Writer via `ToolWriterShapeBase`. `create_shape` is specialized-tier for Writer (delegate `domain=shapes`); Draw/Impress still list it in the default toolset. In **Writer**, shapes live on the document draw page (floating objects), not in the body text—empty `Document length: 0` in chat context does not mean shapes cannot exist. `DrawShapes.safe_create_shape` sets `AnchorType` to `AT_PAGE` before `page.add`; `CreateShape` then sets `AnchorPageNo` (1-based page, from draw page index) and `HoriOrient`/`VertOrient` to `NONE` so absolute position applies—without `AnchorPageNo`, page-anchored shapes may not display in Writer. After that it re-applies `Position`/`Size`, invalidates the controller window + `processEventsToIdle`, and selects the shape via `queryInterface(uno.getTypeByName("com.sun.star.view.XSelectionSupplier"))` (class-based `queryInterface` can fail under pyuno) so the shape repaints and is selected.

**Writer Fields Specialized Tools**: Text fields in LibreOffice are implemented using `doc.createInstance("com.sun.star.text.textfield.<TYPE>")` (like `PageNumber`, `DateTime`). See `plugin/modules/writer/fields.py` for tools to insert, list, and delete fields natively using property reflection.

**Writer Track Changes Specialized Tools**: Track changes (redlines) tools are implemented in `plugin/modules/writer/tracking.py` using `XRedlinesSupplier` to enumerate changes, `RecordChanges` document property to start/stop tracking, and UNO dispatch commands (`.uno:AcceptTrackedChange`, etc.) against the selected text for accepting and rejecting modifications. Also includes tools to manage document comments (`Annotation` text fields) via `doc.createInstance("com.sun.star.text.textfield.Annotation")`.

**Tool Compatibility**: `ToolRegistry` prioritizes `uno_services` matches (strict), but falls back to `doc_types` if no service match is found. This ensures tools remain accessible in test environments or across slightly different LibreOffice flavors. `get_schemas` (chat/MCP) applies the same filter as `execute`, so gateway tools must list every supported UNO service (e.g. `delegate_to_specialized_draw_toolset` includes both Draw and Impress presentation services).

**Shared tool names (Writer vs Calc/Draw)**: [`plugin/modules/writer/charts.py`](plugin/modules/writer/charts.py) and [`plugin/modules/writer/shapes.py`](plugin/modules/writer/shapes.py) register the same `name` as Calc/Draw tools; the last class registered wins. Those Writer subclasses must list a **union** of every document `uno_services` the inherited `execute()` supports (e.g. Text + Spreadsheet for chart tools, Text + Drawing + Presentation for shape tools), or `ToolRegistry.execute` will reject Calc/Draw documents.

**Menu chat**: No tool-calling; same doc detection as sidebar.

**Chat config keys**: `chat_context_length`, `chat_max_tokens`, `additional_instructions`.

### HTML / Writer edits

- `get_document_content` / `apply_document_content`: see [`format_support.py`](plugin/modules/writer/format_support.py). **`apply_document_content`** accepts `content` as JSON array of HTML strings; also coerces a JSON-encoded string of an array when providers double-encode.
- **Format preservation**: Prefer **plain-text** `content` in `apply_document_content` when you want to keep character formatting; avoid HTML-wrapped strings on that path (see §9).

### Unified prompts

`additional_instructions` everywhere (Chat, Edit, Extend); LRU in `prompt_lru` (10 entries); [`populate_combobox_with_lru`](plugin/framework/config.py), etc. Chat builds prompt via `get_chat_system_prompt_for_document` in [`constants.py`](plugin/framework/constants.py). Sidebar has model/image comboboxes but not additional-instructions text (Settings only for that).

---

## 5. Calc (plugin)

Tools go through `tool_registry` / `ToolContext`. Modular tools: `cells.py`, `formulas.py`, `sheets.py`; `tools.py` builds `CALC_TOOLS` and forwards execution. **Gemini/OpenRouter**: no union types in JSON schemas; use `"type": "array"` + `items` for ranges; execute layers accept a single string as one-element list.
**Specialized Calc Toolsets**: Operations like managing cell comments, conditional formatting, sheet management, and image management have been moved from the main toolset to specialized domains (`comments`, `conditional_formatting`, `sheets`, `images`). To access these tools, the main agent must use `delegate_to_specialized_calc_toolset(domain=...)`. These tools inherit from `ToolCalcSpecialBase` and are filtered out of the core tool list.

---

## 6. Images (summary)

**Providers**: AI Horde ([`aihordeclient/`](plugin/framework/aihordeclient/)) vs **endpoint** (same URL/key as chat, **`image_model`** / `image_model_lru`). **Direct image** sidebar checkbox: `chat_direct_image` → `execute_tool("generate_image", …)`. ImageService: [`image_service.py`](plugin/framework/image_service.py); insertion/cursor rules: [`image_tools.py`](plugin/framework/image_tools.py) (ViewCursor → TextCursor before `insertTextContent`). Full keys: [IMAGE_GENERATION.md](IMAGE_GENERATION.md).

---

## 7. Writer navigation helpers

In [`plugin/framework/document.py`](plugin/framework/document.py): `build_heading_tree`, `ensure_heading_bookmarks`, `resolve_locator`, etc. **`DocumentCache`** in that file is **commented out** (not active); ignore stale references to cache invalidation in old notes. **`get_document_outline` / `get_heading_content`** exist but are **not registered** as tools. Paragraph batch tools in `content.py` remain off unless rebased to `ToolBase`.

---

## 8. Client-side tool parsers & agent backend

Fallback when API returns text without `tool_calls`: [`plugin/modules/http/client.py`](plugin/modules/http/client.py) → `get_parser_for_model` → [`plugin/contrib/tool_call_parsers/`](plugin/contrib/tool_call_parsers/). Hermes backend: leading `/` messages forwarded on ACP without `[DOCUMENT CONTENT]` wrapping. Upstream lineage: [hermes-agent](https://github.com/NousResearch/hermes-agent). Chat **Agent backends → Hermes** spawns only `hermes acp`; if LibreOffice’s environment has a narrower `PATH` than your shell, set **Path / URL** to the full `hermes` executable and leave **Extra arguments** empty for the default `acp`.

---

## 9. Threading and subprocesses

Use [`run_in_background`](plugin/framework/worker_pool.py) instead of raw `threading.Thread`; long-running external processes → [`AsyncProcess`](plugin/framework/process_manager.py).

---

## 10. Shared helpers

- **`MainJob._apply_settings_result`**: single apply path for settings (`main.py`).
- **Logging** ([`plugin/framework/logging.py`](plugin/framework/logging.py)): `init_logging(ctx)` once; `debug_log` → `writeragent_debug.log`; `agent_log` → `writeragent_agent.log` if enabled; watchdog helpers. **`redact_sensitive_payload_for_log`** deep-copies API request/response JSON and strips embedded base64 before debug logs (`LlmClient`, `image_utils`).
- **`SendButtonListener._send_busy`**, `_set_button_states`: see §4.
- **`format_error_for_display`**: `core/api` for cells/dialogs.
- **Programmatic dialogs**: `add_dialog_*` in [`dialogs.py`](plugin/framework/dialogs.py).

---

## 11. LibreOffice dialogs — critical rules

- **Map AppFont** for positions/sizes; not raw pixels.
- **No flex/auto-size**; explicit control geometry; tabs via `dlg:page` + `Step`.
- **Package URL only** for `DialogProvider` — never `vnd.sun.star.script:…?location=application` with sidebar UNO components (deadlock).
- **TabListener** must implement **`XActionListener`** + `unohelper.Base` — a plain class raises `value does not implement …XActionListener`:
  ```python
  import unohelper
  from com.sun.star.awt import XActionListener
  class TabListener(unohelper.Base, XActionListener):
      def __init__(self, dialog, page):
          self._dlg, self._page = dialog, page
      def actionPerformed(self, ev):
          self._dlg.getModel().Step = self._page
      def disposing(self, ev): pass
  ```
- **Optional controls**: `get_optional`, checkbox helpers from **`plugin/framework/dialogs.py`**.
- **ListBox/ComboBox**: populate **`StringItemList`**, not only `.Text`.
- **translate_dialog**: walk `XControlContainer` with `queryInterface`; fallback `ElementNames` — see [`dialogs.py`](plugin/framework/dialogs.py). **`legacy_ui.py`**: do not pass saved config values through `gettext` (empty string → PO header garbage).
- **`legacy_ui.input_box`** (`EditInputDialog.xdl`, Extend/Edit Selection, etc.): when `execute()` returns false (ESC or window close — there is no separate Cancel button), **do not** call `XComponent.dispose()` on the dialog; the toolkit may already have torn it down, and a second dispose can segfault the office (nothing reaches Python logs).

### Format preservation

Prefer `apply_document_content` with **plain-text** `content` for format-preserving replacement; HTML/markdown goes through the normal import path.

---

## 12. Config file

Paths: Linux `~/.config/libreoffice/{4,24}/user/writeragent.json`; macOS `~/Library/Application Support/LibreOffice/4/user/`; Windows `%APPDATA%\LibreOffice\4\user\`. **`get_current_endpoint(ctx)`** for normalized URL. **`fetch_available_models`**: [`populate_combobox_with_lru`](plugin/framework/config.py) may call it when filling model comboboxes; it only runs when [`endpoint_url_suitable_for_v1_models_fetch`](plugin/framework/config.py) passes (http/https with `localhost`, a dotted hostname, or an IP). **`_model_fetch_cache`** memoizes `/v1/models` results (including failures) for the **LibreOffice process** using a normalized base URL. **Settings** endpoint field ([`legacy_ui.settings_box`](plugin/framework/legacy_ui.py)): after **1 second** idle since the last keystroke, suitable endpoints trigger `/v1/models` fetch on a **background thread**; combobox refresh runs on the **main thread** via [`post_to_main_thread`](plugin/framework/queue_executor.py) so the dialog stays responsive. Preset selection uses the same async fetch without debounce. Keys: `api_keys_by_endpoint`, `text_model`/`model`, `model_lru`, `image_model`, `image_model_lru`, chat keys above. Combobox LRU keys (`prompt_lru`, scoped `model_lru@…`, `endpoint_lru`, etc.) are not dataclass/MODULE fields; when absent from JSON, [`_resolve_default`](plugin/framework/config.py) returns `[]` for the known list (see `_LRU_LIST_CONFIG_KEY_PREFIXES`). **Extension update check** (optional key): `extension_update_check_epoch` — Unix time of the last weekly attempt to fetch published `update.xml` from GitHub ([`extension_update_check.py`](plugin/framework/extension_update_check.py)); if the published version is newer than [`EXTENSION_VERSION`](plugin/version.py), the user gets an info box suggesting **Tools → Extension Manager**. Scheduled once per LibreOffice process when the chat sidebar finishes wiring ([`panel_wiring._wireControls`](plugin/modules/chatbot/panel_wiring.py)), after `init_logging`, via [`run_in_background`](plugin/framework/worker_pool.py); UI uses [`QueueExecutor.post`](plugin/framework/queue_executor.py). Users who never open the sidebar will not run the check until they do. **`core`** module skipped in auto-generated Settings tabs (`manifest_registry.py` + `legacy_ui.py` **must** agree or Settings crashes on missing `btn_tab_core`). Validation: `WriterAgentConfig.validate()` strips bogus gettext headers from strings; coerces invalid/empty numeric fields (e.g. `chat_max_tool_rounds` → default 25); dotted keys merged via `_build_validated_config_export()` — see [`plugin/tests/test_i18n.py`](plugin/tests/test_i18n.py). **`get_config_int`**: empty string or `None` in JSON resolves via `_resolve_default` like a missing key (no crash).

---

## 13. Logs

Same directory as `writeragent.json` (else `~/writeragent_debug.log`). **Image errors**: Horde → debug log `[AIHorde]`; endpoint → enable agent log, check `writeragent_agent.log` for `generate_image` `result_snippet`. **`translate_dialog` / i18n** details: §11.

---

## 14. Build and install

```bash
make build
make deploy   # or: unopkg remove org.extension.writeragent
make test     # ty + mypy + pyright + bandit, then pytest + in-LO runner (skips if no soffice)
```

Also: `make build-no-recording`, `make release` (runs **`make test`** first—**`ty` + mypy + pyright + bandit**, then pytest + LO tests—then builds a smaller bundle without bundled plugin tests; strips debug menu). **Translations**: overview → [`docs/localization.md`](docs/localization.md). `make build` runs `preview-translations` (refresh `writeragent.pot` + `translate_missing.py --preview` for the localization status table only), then `compile-translations`. Full template + PO merge: `make extract-strings` (runs `xgettext`, YAML merge, then **`merge-translations`**: `msgmerge --update` each `writeragent.po` + `msgattrib --no-obsolete`). Optional AI fill: `translate_missing.py` / `make auto-translate` when `OPENROUTER_API_KEY` is set. Contributor steps → [`plugin/locales/README.md`](plugin/locales/README.md).

Restart LibreOffice after deploy.

---

## 15. MCP server (essentials)

- Queue from HTTP threads → **main-thread** `drain_mcp_queue` ([`mcp_protocol.py`](plugin/modules/http/mcp_protocol.py)).
- **AsyncCallback** ~100ms from [`plugin/main.py`](plugin/main.py) (not chat-drain-only).
- **`X-Document-URL`** → `resolve_document_by_url` in `document.py`; else active document.
- Config: `mcp_enabled`, `mcp_port` (default 8765); Http tab in Settings. When `mcp_enabled`, [`HttpModule.start_background`](plugin/modules/http/__init__.py) runs from [`bootstrap()`](plugin/main.py) after modules load (restart picks up saved config). [`apply_settings_result`](plugin/framework/settings_dialog.py) emits `config:changed` with `ctx` only (no `key`); [`HttpModule._on_config_changed`](plugin/modules/http/__init__.py) treats that as a bulk apply and starts/stops the HTTP server to match `http.mcp_enabled` immediately on OK. LibreOffice may bootstrap more than once (e.g. sidebar vs menu); [`HttpModule`](plugin/modules/http/__init__.py) keeps a single primary instance and shared listener so the MCP port is not bound twice. Localhost, no auth.

---

## 16. Optional / experimental / roadmap

- **Future refactors**: centralized config read/write path in `config.py`; doc-type registry for `_do_send` in `panel_factory.py`.
- **Experimental memory**: [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md) (tools not registered until chatbot `auto_discover` is enabled).
- **Experimental todo** ([`plugin/contrib/todo_store.py`](plugin/contrib/todo_store.py), [`plugin/modules/chatbot/tools/todo.py`](plugin/modules/chatbot/tools/todo.py)): not registered; enable by uncommenting tool, wiring `TodoStore` on `ToolContext.services`, prompt note in `constants.py`.
- **Roadmap** (high level): richer context, safer confirm workflows, Draw/Impress completeness, local-first, optional MCP handoff to external agents.

---

## 17. Error handling

Use `WriterAgentException` hierarchy and **`format_error_payload`** ([`plugin/framework/errors.py`](plugin/framework/errors.py)); structured tool errors, not raw strings. Avoid stringifying raw UNO exceptions in logs. **`safe_json_loads`** for defensive JSON. Tools: `_tool_error` / `ToolBase`. **Automatic `ToolBase` discovery** in [`tool_registry.py`](plugin/framework/tool_registry.py). On UNO stale objects, catch and abort gracefully (document cache class is currently disabled — do not assume `DocumentCache` exists).

---

## 18. Gotchas (index)

- Settings field names: **`MainJob._get_settings_field_specs()`** must match XDL control names; `_apply_settings_result` follows it.
- **`WriterAgentDialogs`** folder name ↔ `dialog.xlb` `library:name`.
- **`self.ctx`** for extension lookups; not `uno.getComponentContext()`.
- **DialogProvider**: use **package** `base_url` + XDL path — never `vnd.sun.star.script:…?location=application` with the sidebar component registered (deadlock). See §11.
- Sidebar: **`setVisible(True)`**; document from **`xFrame.getController().getModel()`**; never `getCurrentComponent()` for tool execution; stream via **queue + drain + `processEventsToIdle()`** (§4), not UNO timer.
- **`is_writer(model)`** — not `getDrawPages` alone (Writer has draw pages).
- **No env API keys** in production; no `tempfile.mktemp()`.
- **Stop** on main chat path: append assistant `"No response."` for strict role alternation (e.g. Mistral); UI still shows stopped; web-research-only path differs.
- **Python**: do not shadow **`logging`** or module-level **`log`** inside functions.
- **MCP**: start from `main.py` only; use **`X-Document-URL`** when multiple docs open (§15).
- **Calc JSON schemas (Gemini/OpenRouter)**: no union types in tool params; use concrete `array` + `items`; normalize single string to one-element list in execute (§5).

---

## 19. References

- DTD: `xmlscript/dtd/dialog.dtd` (LibreOffice tree)
- DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces

---

## 21. Static Type Checking (ty)

The project uses `ty` for static type checking. Background on the cleanup, configuration, and workflow: [`docs/type-checking.md`](docs/type-checking.md).

- **mypy**: Cross-check via **`make mypy`** (`mypy` in the `dev` group); also runs as part of **`make typecheck`** and **`make test`** (and **`make release`**, which calls **`make test`**). Same manifest / UNO setup as `ty`; contrib and tests are silenced in config to match ty’s scope. Chat mixins declare `client` / `audio_wav_path` on **`SendHandlersMixin`** and **`ToolCallingMixin`** so mypy can type combined **`SendButtonListener`**; **`fetch_available_models`** cache is **`dict[str, list[str] | None]`** (negative cache stores `None`).
- **Pyright**: CLI via **`make pyright`** (`pyright` in the `dev` group); **`[tool.pyright]`** in **`pyproject.toml`** scopes **`plugin/`** and excludes **`plugin/contrib`** and **`plugin/tests`**. Included in **`make typecheck`** and **`make test`** (and **`make release`** via **`make test`**); not in **`make check`** / **`make build`** alone. Overlaps Pylance but may disagree with **`ty`** / **`mypy`**. For override consistency, **`ToolBase`** declares **`execute(...) -> dict[str, Any]`**, **`is_async() -> bool`**, and **`execute_safe(...) -> dict[str, Any]`**; **`AgentBackend.is_available`** is annotated **`-> bool`** (subclasses may return False). **Pyright-only strictness and fix patterns** (optional chaining, overrides, IDL bases, etc.) are summarized in [`docs/type-checking.md`](docs/type-checking.md).
- **Bandit**: Security lint via **`make bandit`** (`bandit[toml]` in `dev`); runs after **`make typecheck`** as part of **`make test`** (not **`make typecheck`** alone). **`[tool.bandit]`** **`exclude_dirs`** omit **`plugin/contrib`** and **`plugin/tests`**. The Makefile runs **`bandit --severity-level medium`** (low-severity noise such as **`B110`** is not reported); **`skips`** in **`pyproject.toml`** include **`B310`** / **`B314`** where urllib/XML use is intentional.
- **Dependencies**: Requires `types-unopy` (in `dev` group) for LibreOffice API stubs.
- **UNO Resolution**: Because the `uno` module is typically provided by the system (not PyPI), you MUST run `make fix-uno` to symlink the system UNO paths into your `.venv`. Otherwise, `ty` will fail to resolve `import uno` or `com.sun.star` types.
- **Python Version & Syntax**: Use modern 3.11+ syntax: `list[str]`, `dict[str, Any]`, and `str | None` instead of `List`, `Dict`, or `Optional`.
- **Annotation Patterns**:
    - **Protocols for Mixins**: When a mixin accesses attributes/methods from its host class, define a `Protocol` (e.g., `ToolLoopHost`) and annotate the mixin methods with `self: ToolLoopHost`.
    - **TYPE_CHECKING**: Wrap UNO and other complex imports in `if TYPE_CHECKING:` blocks to avoid runtime issues while still providing context to `ty`.
    - **Dynamic Attributes**: For objects with dynamic attributes (like attaching results to a `threading.Event`), use `setattr(obj, "name", val)` and `getattr(obj, "name")` to satisfy the static analyzer's "unresolved-attribute" rules.
    - **Casting for UNO/Dynamic Types**: Use `cast(Any, ...)` for UNO constants (like `CellContentType.EMPTY`) that may lack full stubs.
    - **Iterator Casting**: Use `cast(Iterable, agent.run(...))` for complex generators that `ty` cannot automatically infer as iterable.
    - **None/Check Normalization**: Explicitly check for `None` before casting or converting (e.g., `int(val) if val is not None else 0`).
- **Interface Signatures**: When overriding UNO interfaces (e.g., `XActionListener`, `XEventListener`), you must match the argument names in the `.pyi` stubs exactly (e.g., `actionPerformed(self, rEvent)`, `disposing(self, Source)`). Mismatched names will trigger `invalid-method-override` errors.

---

## 22. Debugging

- **`make deploy`** vs **`make repack`**: full rebuild/deploy vs re-zip only.
- New components: [`extension/META-INF/manifest.xml`](extension/META-INF/manifest.xml).
- Buffered logs: `/tmp` scratch + `flush=True` when needed.
