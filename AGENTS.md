# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file summarizes invariants and where to look in code.

> [!IMPORTANT]
> **AI Assistants: You MUST update this file after making (nontrivial) changes to the project.** This ensures the next assistant has up-to-date context without manual handoff.

---

## Quick orientation

Common touchpoints: [`plugin/main.py`](plugin/main.py) (MainJob, settings apply), [`plugin/modules/chatbot/panel_factory.py`](plugin/modules/chatbot/panel_factory.py) (sidebar, `SendButtonListener`), [`plugin/modules/chatbot/tool_loop.py`](plugin/modules/chatbot/tool_loop.py), [`plugin/modules/chatbot/panel.py`](plugin/modules/chatbot/panel.py), [`plugin/framework/document.py`](plugin/framework/document.py), [`plugin/framework/config.py`](plugin/framework/config.py), [`plugin/modules/http/client.py`](plugin/modules/http/client.py), [`plugin/framework/errors.py`](plugin/framework/errors.py), [`plugin/framework/dialogs.py`](plugin/framework/dialogs.py), [`plugin/modules/writer/format_support.py`](plugin/modules/writer/format_support.py). Deep dive: [CHAT_SIDEBAR_IMPLEMENTATION.md](CHAT_SIDEBAR_IMPLEMENTATION.md). Writer nested tool domains (`delegate_to_specialized_writer_toolset`, tier filtering): [docs/features/writer-specialized-toolsets.md](docs/features/writer-specialized-toolsets.md).

---

## 1. Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw:

- **Build & Dev**: `make build`, `make deploy`. **External tools (ty, pytest)**: `make fix-uno` to link system UNO into `.venv`.
- **Extend Selection** (Ctrl+Q) / **Edit Selection** (Ctrl+E): model continues or rewrites the selection.
- **Chat with Document**: sidebar (multi-turn + tool-calling), persistent history (SQLite when available, else JSON under `writeragent_history.db.d/`), menu fallback (Writer: append; Calc: "AI Response" sheet).
- **Settings**: endpoint, models, keys, timeouts, image provider, MCP, etc. Config: `writeragent.json` in LibreOffice user config. Examples: [CONFIG_EXAMPLES.md](CONFIG_EXAMPLES.md).
- **Experimental memory/skills** (file-backed `USER.md` / `MEMORY.md`, skills as `skills/<name>/SKILL.md`): [`plugin/modules/chatbot/memory.py`](plugin/modules/chatbot/memory.py), [`plugin/modules/chatbot/skills.py`](plugin/modules/chatbot/skills.py); tool registration commented out in [`plugin/modules/chatbot/__init__.py`](plugin/modules/chatbot/__init__.py). Writer prompt includes `MEMORY_GUIDANCE` / `SKILLS_GUIDANCE` in [`plugin/framework/constants.py`](plugin/framework/constants.py). Full description: [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md) (Hermes reference: automatic memory-in-prompt, frozen snapshot, periodic background review agent; WriterAgent injection not enabled).
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
- **Config sync**: `add_config_listener` / `notify_config_changed` in [`plugin/framework/config.py`](plugin/framework/config.py); weakref on listeners.
- **Lifecycle**: Send disabled / Stop enabled at start of `actionPerformed`; restored in **`finally`** after `_do_send()` returns. `_set_button_states` uses per-control try/except. `_send_busy` mirrors that window.
- **FSM** ([`plugin/framework/state.py`](plugin/framework/state.py)): Pure `next_state(state, event) → FsmTransition`; **no** UNO/I/O/logging/`EventBus` inside transitions. Effects run in panel/mixins/MCP. Composite: [`sidebar_state.py`](plugin/modules/chatbot/sidebar_state.py) (`send`, `tool_loop`, mirrored `audio`). `ToolCallingMixin` clears the tool-loop slice in **`finally`** after drain. **`EventBus`**: loose notifications only (e.g. config, `mcp:request`), not the FSM driver. Other `*_state.py` modules under `chatbot/` and `http/mcp_state.py` for domains.
- **Panel**: [`panel_factory.py`](plugin/modules/chatbot/panel_factory.py) + `ChatPanelDialog.xdl`; **`setVisible(True)`** after `createContainerWindow()`. Resize: [`panel_resize.py`](plugin/modules/chatbot/panel_resize.py), wired from [`panel_wiring.py`](plugin/modules/chatbot/panel_wiring.py).
- **Doc type**: [`plugin/framework/document.py`](plugin/framework/document.py) (`supportsService`).

**Document context** (chat only): Each send **replaces** the single `[DOCUMENT CONTENT]` system message. Writer: `get_document_context_for_chat` (excerpts, `[SELECTION_START]`/`[SELECTION_END]`). Calc: `get_calc_context_for_chat` — **`ctx` required** (panel/MainJob); never `uno.getComponentContext()` on this path. Signature pattern: `(model, max_context, ctx=None)`.

**Streaming**: Worker thread → `queue.Queue` (`chunk`, `thinking`, `stream_done`, `error`, `stopped`); main thread drain loop + **`toolkit.processEventsToIdle()`**. **Do not** use UNO `XTimerListener` in the sidebar for this. [`run_blocking_with_pump`](plugin/framework/async_stream.py) for blocking callers (e.g. `=PROMPT()`). **`LlmClient`** cached on sidebar, MainJob, and `prompt_function` for keep-alive. Edge cases: [LITELLM_INTEGRATION.md](LITELLM_INTEGRATION.md).

**History**: [`plugin/modules/chatbot/history_db.py`](plugin/modules/chatbot/history_db.py); `HAS_SQLITE` in [`plugin/framework/sqlite_available.py`](plugin/framework/sqlite_available.py). Session id: `WriterAgentSessionID` in document **UserDefinedProperties** (not URL-only).

**Reasoning**: `plugin/main.py` sends `reasoning: { effort: 'minimal' }`; UI shows `[Thinking] … /thinking` then newline before answer.

**Audio** ([`audio_recorder.py`](plugin/modules/chatbot/audio_recorder.py), `contrib/audio/`): One `AudioRecorder` per `SendButtonListener`; PortAudio failures → `"[Audio error: …]"` in UI.

### Web search checkbox

Bypasses document context/tools for that send; calls `web_research`. **`panel_factory.py`**: `ResearchChatToggledListener` imports `_` inside `on_item_state_changed` — the path-climbing loop at top of file must **not** use `for _ in …` (shadows gettext `_`). **Approval** (`chatbot.prompt_for_web_research`): Send = Accept, Stop = Change (edit query in [`show_web_search_query_edit_dialog`](plugin/framework/dialogs.py)), Clear = Reject. **`WebResearchTool.is_async()`** so approval does not deadlock the UI thread. ACP permission: `submit_approval` / QUERYBOX for Yes/No. Details: [`web_research.py`](plugin/modules/chatbot/web_research.py), [`web_research_chat.py`](plugin/modules/chatbot/web_research_chat.py), smolagents in `plugin/contrib/smolagents/` (cache, UA split DDG/Wikipedia vs browser UA — see `default_tools.py`, `constants.py`).

### Tools by document type

Paragraph tools in [`content.py`](plugin/modules/writer/content.py) are **`ToolBaseDummy`** until rebased. **Specialized tier** (`ToolWriterSpecialBase` in [`base.py`](plugin/modules/writer/base.py)): tables (e.g. `ToolWriterTableBase`), styles, layout (frames), shapes/charts in doc, indexes, fields, bookmarks, embedded — omitted from default chat/MCP tool lists via `exclude_tiers` in [`tool_registry.py`](plugin/framework/tool_registry.py).

**Testing specialized tools**: Tests should retrieve tools via `plugin.main.get_tools().get("tool_name")` rather than direct internal imports. This avoids regressions when tools are moved between specialized modules and allows bypassing tier-based filtering.

**Shape Enhancements**: Shape tools for Draw/Impress (`create_shape`, `edit_shape`) have been enhanced to support generic shape types (e.g. `connector` or direct UNO class names) and rich formatting properties like `fill_color`, `line_color`, `line_width`, `font_size`, `font_name`, and `rotation_angle`. The `shapes_connect` and `shapes_group` tools are also fully implemented in Draw and inherited by Writer via `ToolWriterShapeBase`.

**Writer Fields Specialized Tools**: Text fields in LibreOffice are implemented using `doc.createInstance("com.sun.star.text.textfield.<TYPE>")` (like `PageNumber`, `DateTime`). See `plugin/modules/writer/fields.py` for tools to insert, list, and delete fields natively using property reflection.

**Tool Compatibility**: `ToolRegistry` prioritizes `uno_services` matches (strict), but falls back to `doc_types` if no service match is found. This ensures tools remain accessible in test environments or across slightly different LibreOffice flavors.

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

---

## 6. Images (summary)

**Providers**: AI Horde ([`aihordeclient/`](plugin/framework/aihordeclient/)) vs **endpoint** (same URL/key as chat, **`image_model`** / `image_model_lru`). **Direct image** sidebar checkbox: `chat_direct_image` → `execute_tool("generate_image", …)`. ImageService: [`image_service.py`](plugin/framework/image_service.py); insertion/cursor rules: [`image_tools.py`](plugin/framework/image_tools.py) (ViewCursor → TextCursor before `insertTextContent`). Full keys: [IMAGE_GENERATION.md](IMAGE_GENERATION.md).

---

## 7. Writer navigation helpers

In [`plugin/framework/document.py`](plugin/framework/document.py): `build_heading_tree`, `ensure_heading_bookmarks`, `resolve_locator`, etc. **`DocumentCache`** in that file is **commented out** (not active); ignore stale references to cache invalidation in old notes. **`get_document_outline` / `get_heading_content`** exist but are **not registered** as tools. Paragraph batch tools in `content.py` remain off unless rebased to `ToolBase`.

---

## 8. Client-side tool parsers & agent backend

Fallback when API returns text without `tool_calls`: [`plugin/modules/http/client.py`](plugin/modules/http/client.py) → `get_parser_for_model` → [`plugin/contrib/tool_call_parsers/`](plugin/contrib/tool_call_parsers/). Hermes backend: leading `/` messages forwarded on ACP without `[DOCUMENT CONTENT]` wrapping. Upstream lineage: [hermes-agent](https://github.com/NousResearch/hermes-agent).

---

## 9. Threading and subprocesses

Use [`run_in_background`](plugin/framework/worker_pool.py) instead of raw `threading.Thread`; long-running external processes → [`AsyncProcess`](plugin/framework/process_manager.py).

---

## 10. Shared helpers

- **`MainJob._apply_settings_result`**: single apply path for settings (`main.py`).
- **Logging** ([`plugin/framework/logging.py`](plugin/framework/logging.py)): `init_logging(ctx)` once; `debug_log` → `writeragent_debug.log`; `agent_log` → `writeragent_agent.log` if enabled; watchdog helpers.
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

### Format preservation

Prefer `apply_document_content` with **plain-text** `content` for format-preserving replacement; HTML/markdown goes through the normal import path.

---

## 12. Config file

Paths: Linux `~/.config/libreoffice/{4,24}/user/writeragent.json`; macOS `~/Library/Application Support/LibreOffice/4/user/`; Windows `%APPDATA%\LibreOffice\4\user\`. **`get_current_endpoint(ctx)`** for normalized URL. Keys: `api_keys_by_endpoint`, `text_model`/`model`, `model_lru`, `image_model`, `image_model_lru`, chat keys above. **`core`** module skipped in auto-generated Settings tabs (`manifest_registry.py` + `legacy_ui.py` **must** agree or Settings crashes on missing `btn_tab_core`). Validation: `WriterAgentConfig.validate()` strips bogus gettext headers from strings; dotted keys merged via `_build_validated_config_export()` — see [`plugin/tests/test_i18n.py`](plugin/tests/test_i18n.py).

---

## 13. Logs

Same directory as `writeragent.json` (else `~/writeragent_debug.log`). **Image errors**: Horde → debug log `[AIHorde]`; endpoint → enable agent log, check `writeragent_agent.log` for `generate_image` `result_snippet`. **`translate_dialog` / i18n** details: §11.

---

## 14. Build and install

```bash
make build
make deploy   # or: unopkg remove org.extension.writeragent
make test     # pytest + in-LO runner (skips if no soffice)
```

Also: `make build-no-recording`, `make release` (runs `make test` first, then builds a smaller bundle without bundled plugin tests; strips debug menu). **Translations**: `make build` runs `extract-strings` (regenerates `writeragent.pot`), then **`merge-translations`** (`msgmerge --update` each `writeragent.po` + `msgattrib --no-obsolete` so strings removed from sources drop out of `.po` files), then optional AI fill via `translate_missing.py`, then `compile-translations` (`.mo`). Details → [`plugin/locales/README.md`](plugin/locales/README.md).

Restart LibreOffice after deploy.

---

## 15. MCP server (essentials)

- Queue from HTTP threads → **main-thread** `drain_mcp_queue` ([`mcp_protocol.py`](plugin/modules/http/mcp_protocol.py)).
- **AsyncCallback** ~100ms from [`plugin/main.py`](plugin/main.py) (not chat-drain-only).
- **`X-Document-URL`** → `resolve_document_by_url` in `document.py`; else active document.
- Config: `mcp_enabled`, `mcp_port` (default 8765); Http tab in Settings. Started from **MainJob**, not sidebar. Localhost, no auth.

---

## 16. Optional / experimental / roadmap

- **Future refactors**: centralized config read/write path in `config.py`; doc-type registry for `_do_send` in `panel_factory.py`.
- **Experimental memory/skills**: [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md) (tools not registered until chatbot `auto_discover` is enabled).
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

The project uses `ty` for static type checking.

- **Dependencies**: Requires `types-unopy` (in `dev` group) for LibreOffice API stubs.
- **UNO Resolution**: Because the `uno` module is typically provided by the system (not PyPI), you MUST run `make fix-uno` to symlink the system UNO paths into your `.venv`. Otherwise, `ty` will fail to resolve `import uno` or `com.sun.star` types.
- **Interface Signatures**: When overriding UNO interfaces (e.g., `XActionListener`, `XEventListener`), you must match the argument names in the `.pyi` stubs exactly (e.g., `actionPerformed(self, rEvent)`, `disposing(self, Source)`). Mismatched names will trigger `invalid-method-override` errors.
- **Base Classes**: Base classes like `ModuleBase` and `ToolBase` use `str | None = None` for attributes that are set dynamically at load time to satisfy type assignment rules.
- **Casting**: Use `typing.cast` and `isinstance` checks to assist the checker in complex dynamic scenarios (like `streaming_deltas.py` or UNO property maps).

---

## 22. Debugging

- **`make deploy`** vs **`make repack`**: full rebuild/deploy vs re-zip only.
- New components: [`extension/META-INF/manifest.xml`](extension/META-INF/manifest.xml).
- Buffered logs: `/tmp` scratch + `flush=True` when needed.
