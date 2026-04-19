# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file summarizes invariants and where to look in code. It is **self-contained** (agents are not given `README.md`); build commands and orientation live here.

> [!IMPORTANT]
> **AI Assistants: You MUST update this file after making (nontrivial) changes to the project.** This ensures the next assistant has up-to-date context without manual handoff.

> [!IMPORTANT]
> **Testing Requirement: When adding a feature or fixing a bug, you MUST add test code.**
> - Use **unit tests** (in `plugin/tests/`) for logic that can be mocked.
> - Use **UNO tests** (in `plugin/tests/uno/`) to verify that code calling into LibreOffice works correctly in a real environment.
> - Run **`make test`** to ensure full coverage and prevent regressions.

---

## Quick orientation

Common touchpoints: [`plugin/main.py`](plugin/main.py) (MainJob, settings apply), [`plugin/modules/chatbot/panel_factory.py`](plugin/modules/chatbot/panel_factory.py) (sidebar, `SendButtonListener`), [`plugin/modules/chatbot/tool_loop.py`](plugin/modules/chatbot/tool_loop.py), [`plugin/modules/chatbot/panel.py`](plugin/modules/chatbot/panel.py), [`plugin/framework/document.py`](plugin/framework/document.py), [`plugin/framework/config.py`](plugin/framework/config.py), [`plugin/framework/extension_update_check.py`](plugin/framework/extension_update_check.py) (weekly update check), [`plugin/modules/http/client.py`](plugin/modules/http/client.py), [`plugin/framework/errors.py`](plugin/framework/errors.py), [`plugin/framework/dialogs.py`](plugin/framework/dialogs.py), [`plugin/modules/writer/format_support.py`](plugin/modules/writer/format_support.py). Deep dive: [CHAT_SIDEBAR_IMPLEMENTATION.md](CHAT_SIDEBAR_IMPLEMENTATION.md). Writer nested tool domains (`delegate_to_specialized_writer_toolset`, tier filtering): [docs/features/writer-specialized-toolsets.md](docs/features/writer-specialized-toolsets.md).

---

## 1. Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw:

- **Build & Dev**: `make build` (runs **`ty`** then **`ruff`** on **`plugin/`**, then bundle), `make deploy`. **`plugin/_manifest.py`** is gitignored; **`make ty`**, **`make check`** (ty only), **`make ruff`** (Ruff lint + flake8-type-checking **`TC`** rules; see **`[tool.ruff]`** in **`pyproject.toml`**), **`make typecheck`** (ty + mypy + pyright), and **`make test`** all use **`make manifest`** where applicable so clean checkouts get a generated manifest before type-check. If the file is still absent, [`plugin/framework/module_loader.py`](plugin/framework/module_loader.py) `load_manifest()` raises **`RuntimeError`** (no silent empty module list). **External tools**: `make fix-uno` to link system UNO into `.venv`. **Typecheckers**: **`make check`** → **`ty`** only; **`make build`** → **`ty`** + **`ruff`**; **`make typecheck`** → **`ty` + mypy + pyright**; **`make test`** → typecheck, then **`bandit`** on **`plugin/`** (excludes **`plugin/contrib`** and **`plugin/tests`**, see **`[tool.bandit]`** in **`pyproject.toml`**), then pytest + LO tests; **`make release`** runs **`make test`** first, then **`release-build`** (includes **`openrouter-catalog`** → [`registry/openrouter_models.json`](registry/openrouter_models.json), not bundled in the OXT, plus [`plugin/framework/default_models.py`](plugin/framework/default_models.py), then bundle). **Experimental**: **`make pyrefly`** runs [Pyrefly](https://pyrefly.org/) on the same scoped tree as ty/mypy/pyright; it is **not** part of **`make typecheck`** / **`make test`**. Details: [`docs/type-checking.md`](docs/type-checking.md).
- **Extend Selection** (Ctrl+Q) / **Edit Selection** (Ctrl+E): model continues or rewrites the selection. Writer selection reads should use `get_string_without_tracked_deletions()` in [`plugin/framework/document.py`](plugin/framework/document.py) so hidden tracked deletions are excluded from prompts. Writer **extend** wraps the streamed append in [`plugin/framework/document.py`](plugin/framework/document.py) `WriterCompoundUndo` so one Ctrl+Z undoes the whole completion. Writer **edit** uses `WriterStreamedRewriteSession`: it wraps the session in `XUndoManager` `enterUndoContext` / `leaveUndoContext` so one Ctrl+Z undoes the entire edit; it streams visible text live with `RecordChanges` temporarily off, keeps a shadow copy of generated text, then rewrites the selection once at the end so Track Changes records a single clean replacement when possible. On finalize failure, it restores the generated text and warns instead of silently losing output.
- **Chat with Document**: sidebar (multi-turn + tool-calling), persistent history (SQLite when available, else JSON under `writeragent_history.db.d/`), menu fallback (Writer: append; Calc: "AI Response" sheet).
- **Settings**: endpoint, models, keys, timeouts, image provider, MCP, etc. Config: `writeragent.json` in LibreOffice user config. Examples: [CONFIG_EXAMPLES.md](CONFIG_EXAMPLES.md).
- **Experimental memory** (file-backed `USER.md` / `MEMORY.md`): [`plugin/modules/chatbot/memory.py`](plugin/modules/chatbot/memory.py) (store raises **`ConfigError`** if the UNO user config directory cannot be resolved). Writer prompt includes `MEMORY_GUIDANCE` in [`plugin/framework/constants.py`](plugin/framework/constants.py). Full description: [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md) (Hermes reference: automatic memory-in-prompt, frozen snapshot, periodic background review agent; WriterAgent injection not enabled).
- **Images**: unified `generate_image` tool; `source_image='selection'` for edit. Contract: `ImageProvider.generate()` → `(paths_list, error_message_str)`. See [`plugin/framework/image_utils.py`](plugin/framework/image_utils.py), [docs/features/image-generation.md](docs/features/image-generation.md), [IMAGE_GENERATION.md](IMAGE_GENERATION.md).
- **Calc** `=PROMPT()`: [`plugin/prompt_function.py`](plugin/prompt_function.py).
- **MCP** (opt-in): localhost HTTP; document targeting via `X-Document-URL`. See [MCP_PROTOCOL.md](MCP_PROTOCOL.md), [docs/mcp-protocol.md](docs/mcp-protocol.md).

**HTTP / auth**: Persistent connections in [`plugin/modules/ai/service.py`](plugin/modules/ai/service.py); `USER_AGENT` / headers from `core.constants`; per-endpoint auth in [`plugin/framework/auth.py`](plugin/framework/auth.py); `LlmClient._headers()` adds `Authorization: Bearer` when appropriate, falling back to legacy Bearer logic for unknown/custom endpoints to ensure backwards compatibility. **Local HTTPS**: verify first, then one retry with unverified context on cert errors (no user toggle).

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

**Prompt optimization / benchmarks** ([`scripts/prompt_optimization/`](scripts/prompt_optimization/README.md)): `run_eval.py` and `run_eval_multi.py` use **`LlmClient`** + multi-round tool loop ([`llm_chat_eval.py`](scripts/prompt_optimization/llm_chat_eval.py)) with the same three tool names as chat; default document backend is **in-memory HTML** (`--backend string`, [`string_eval_tools.py`](scripts/prompt_optimization/string_eval_tools.py)). Use **`--backend lo`** for headless Writer + [`tools_lo.py`](scripts/prompt_optimization/tools_lo.py). **`program.py`** / **`run_optimize.py`** stay **DSPy ReAct** for MIPROv2 prompt experiments. `run_eval_multi.py --generate-golds` defaults to **one dataset example per invocation** unless `--yes-multi-gold`. Eval system prompt: [`get_writer_eval_chat_system_prompt()`](plugin/framework/constants.py). **`normalize_endpoint_url()`** in [`plugin/framework/utils.py`](plugin/framework/utils.py) strips a trailing `/v1` from API base URLs so `LlmClient` does not request `/v1/v1/chat/completions` (eval scripts use the same helper via [`llm_chat_eval.py`](scripts/prompt_optimization/llm_chat_eval.py)); pass **`--model x-ai/...`** without an `openrouter/` prefix for the HTTP API. LO eval tools call [`ToolRegistry.execute(..., bypass_thread_guard=True)`](plugin/framework/tool_registry.py) because UNO runs on the harness worker thread, not Python’s main thread.

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
- **FSM** ([`plugin/framework/state.py`](plugin/framework/state.py)): pure transitions only (no UNO/I/O in `next_state`). Effects in panel mixins / MCP. Composite states: [`sidebar_state.py`](plugin/modules/chatbot/sidebar_state.py), [`tool_loop_state.py`](plugin/modules/chatbot/tool_loop_state.py), [`state_machine.py`](plugin/modules/chatbot/state_machine.py). `ToolCallingMixin` clears the tool-loop slice in **`finally`** after drain. **`ModelCapability`** (`IntFlag` in [`types.py`](plugin/framework/types.py)) drives `LlmClient` / `DEFAULT_MODELS`. **`EventBus`**: notifications only (not the FSM driver). Typed send-handler / UI effect kinds in [`types.py`](plugin/framework/types.py); string-literal audits: [`refactor_tool_registry.py`](refactor_tool_registry.py) (`--audit-uieffect`, `--audit-chat-fsm`).
- **Panel**: [`panel_factory.py`](plugin/modules/chatbot/panel_factory.py) + `ChatPanelDialog.xdl`; **`setVisible(True)`** after `createContainerWindow()`. Resize: [`panel_resize.py`](plugin/modules/chatbot/panel_resize.py), wired from [`panel_wiring.py`](plugin/modules/chatbot/panel_wiring.py).
- **Doc type**: [`plugin/framework/document.py`](plugin/framework/document.py) (`supportsService`).

**Document context** (chat only): Each send **replaces** the single `[DOCUMENT CONTENT]` system message. Writer: `get_document_context_for_chat` (excerpts, `[SELECTION_START]`/`[SELECTION_END]`). Calc: `get_calc_context_for_chat` — **`ctx` required** (panel/MainJob); never `uno.getComponentContext()` on this path. Signature pattern: `(model, max_context, ctx=None)`.

**Streaming**: Worker thread → `queue.Queue`; first tuple element must be [`StreamQueueKind`](plugin/framework/async_stream.py) (enum members, not bare strings). Agent backends may emit `TOOL_CALL` / `TOOL_RESULT`; the drain shows them as `[Tool call]` / `[Tool result]` text lines. Main thread drain loop + **`toolkit.processEventsToIdle()`**. **Do not** use UNO `XTimerListener` in the sidebar for this. [`run_blocking_in_thread`](plugin/framework/async_stream.py) pumps the UI while awaiting a result; its internal queue uses [`BlockingPumpKind`](plugin/framework/async_stream.py) only on dequeue (no silent `str` normalization). **`LlmClient`** cached on sidebar, MainJob, and `prompt_function` for keep-alive. **Native Provider Shims**: `LlmClient` ([`plugin/modules/http/client.py`](plugin/modules/http/client.py)) includes native API transformations for **Anthropic** (direct `/v1/messages` with `x-api-key`) and **Google Gemini** (direct RPC-style `v1beta` with query-parameter keys). These shims bypass OpenAI-compatible requirements for those specific providers. `iterate_sse` in `stream_normalizer.py` handles both standard `data:` SSE and raw JSON chunks (for Gemini). Edge cases: [LITELLM_INTEGRATION.md](LITELLM_INTEGRATION.md). Overview: [docs/stream-queue-kind-migration.md](docs/stream-queue-kind-migration.md).

**History**: [`plugin/modules/chatbot/history_db.py`](plugin/modules/chatbot/history_db.py); `HAS_SQLITE` in [`plugin/framework/sqlite_available.py`](plugin/framework/sqlite_available.py). Session id: `WriterAgentSessionID` in document **UserDefinedProperties** (not URL-only).

**Reasoning**: `plugin/main.py` sends `reasoning: { effort: 'minimal' }`; UI shows `[Thinking] … /thinking` then newline before answer.

**Audio** ([`audio_recorder.py`](plugin/modules/chatbot/audio_recorder.py), `contrib/audio/`): One `AudioRecorder` per `SendButtonListener`; PortAudio failures → `"[Audio error: …]"` in UI.

### Web research & direct image (sidebar)

- **Web research** (checkbox / mode): bypasses normal document context for that send; runs `web_research`. HITL and async behavior: [`web_research.py`](plugin/modules/chatbot/web_research.py), [`web_research_chat.py`](plugin/modules/chatbot/web_research_chat.py), [`tool_loop.py`](plugin/modules/chatbot/tool_loop.py) (`execute_fn`). In **`panel_factory.py`**, `ResearchChatToggledListener`’s path-climbing loop must **not** use `for _ in …` (shadows gettext `_`). Item listeners for research/direct image must **override `on_item_state_changed` on the class**, not nest inside `__init__`, or toggles never fire ([`BaseItemListener`](plugin/framework/listeners.py)).
- **Direct image** (`chat_direct_image`): runs `generate_image` on the tool path; `generate_image` is async (`ToolWriterImageBase` in [`images.py`](plugin/modules/writer/images.py)); sub-agent / delegation: [`specialized.py`](plugin/modules/writer/specialized.py) `WrappedSmolTool`.
- **smolagents** (vendored): [`plugin/contrib/smolagents/agents.py`](plugin/contrib/smolagents/agents.py) (`MultiStepAgent`, `ToolCallingAgent` only). Prompt templates / placeholders: [`toolcalling_agent_prompts.py`](plugin/contrib/smolagents/toolcalling_agent_prompts.py).

### Memory upsert (`upsert_memory`)

Sidebar shows when the model calls `upsert_memory`: **main document chat** uses the tool-loop FSM in [`tool_loop_state.py`](plugin/modules/chatbot/tool_loop_state.py) — a line like `[Memory update: key '…']` when the key is present in arguments, then the usual tool result line. **Librarian onboarding** uses [`librarian.py`](plugin/modules/chatbot/librarian.py): the same style line is sent through `ToolContext.chat_append_callback` (chunk path) so it is visible even when `chatbot.show_search_thinking` is off (other librarian tool progress still goes through the thinking stream only).

### Librarian handoff

Sidebar onboarding still **starts** when `USER.md` is empty, but once the librarian has started the active [`SendButtonListener`](plugin/modules/chatbot/panel.py) keeps an in-memory `_in_librarian_mode` flag so later turns stay with the librarian even after `upsert_memory` writes preferences to `USER.md`. That panel-local flag is cleared only when the librarian path in [`send_handlers.py`](plugin/modules/chatbot/send_handlers.py) receives `status == "switch_mode"` from `switch_to_document_mode`; `USER.md` is preference storage only and no longer doubles as the handoff signal.

### Tools by document type

Some classes in [`content.py`](plugin/modules/writer/content.py) (e.g. **`CloneHeadingBlock`**) are still **`ToolBaseDummy`** until rebased; `get_document_content` / `apply_document_content` are real tools. **Specialized tier** (`ToolWriterSpecialBase` in [`base.py`](plugin/modules/writer/base.py)): styles, page (page styles, margins, headers/footers, columns, page breaks), textframes (`list_text_frames`, `get_text_frame_info`, `set_text_frame_properties`), shapes/charts in doc, indexes, fields, bookmarks, embedded, **images** (`generate_image`, list/insert/replace, …), **track changes** (`set_track_changes`, `get_tracked_changes`, `manage_tracked_changes`), **`structural`** (heading proximity and surroundings, sections, page jump, `get_heading_children` — `ToolWriterStructuralBase` in [`structural.py`](plugin/modules/writer/structural.py), [`navigation.py`](plugin/modules/writer/navigation.py), [`outline.py`](plugin/modules/writer/outline.py)), and **Writer** `create_shape` — omitted from default chat/MCP tool lists via `exclude_tiers` in [`tool_registry.py`](plugin/framework/tool_registry.py). `create_shape` remains visible for Draw/Impress default lists (shared tool name; tier exception in `get_tools`).

**Testing specialized tools**: Tests should retrieve tools via `plugin.main.get_tools().get("tool_name")` rather than direct internal imports. This avoids regressions when tools are moved between specialized modules and allows bypassing tier-based filtering.

**In-place specialized mode** (`USE_SUB_AGENT = False` in [`plugin/framework/constants.py`](plugin/framework/constants.py)): `ToolRegistry.get_tools(..., active_domain=...)` restricts tools to the matching domain’s specialized classes—Writer (`ToolWriterSpecialBase`), Calc (`ToolCalcSpecialBase`), and Draw (`ToolDrawSpecialBase`)—plus `specialized_workflow_finished` / `final_answer` / `reply_to_user` as applicable. [`ToolCallingMixin._refresh_active_tools_for_session`](plugin/modules/chatbot/tool_loop.py) recomputes OpenAI tool schemas from `session.active_specialized_domain` before each LLM round so in-place delegation stays consistent within one user send.

**Shapes** (`create_shape`, `edit_shape`, connect/group): specialized-tier for Writer; default toolset for Draw/Impress. Implementation and Writer vs floating-draw-page behavior: [`plugin/modules/draw/shapes.py`](plugin/modules/draw/shapes.py), [`plugin/modules/writer/shapes.py`](plugin/modules/writer/shapes.py); see [docs/features/writer-specialized-toolsets.md](docs/features/writer-specialized-toolsets.md).

**Fields** / **track changes**: [`plugin/modules/writer/fields.py`](plugin/modules/writer/fields.py), [`plugin/modules/writer/tracking.py`](plugin/modules/writer/tracking.py).

**Tool Compatibility**: `ToolRegistry` prioritizes `uno_services` matches (strict), but falls back to `doc_types` if no service match is found. This ensures tools remain accessible in test environments or across slightly different LibreOffice flavors. `get_schemas` (chat/MCP) applies the same filter as `execute`, so gateway tools must list every supported UNO service (e.g. `delegate_to_specialized_draw_toolset` includes both Draw and Impress presentation services).

**Shared tool names (Writer vs Calc/Draw)**: [`plugin/modules/writer/charts.py`](plugin/modules/writer/charts.py) and [`plugin/modules/writer/shapes.py`](plugin/modules/writer/shapes.py) register the same `name` as Calc/Draw tools; the last class registered wins. Those Writer subclasses must list a **union** of every document `uno_services` the inherited `execute()` supports (e.g. Text + Spreadsheet for chart tools, Text + Drawing + Presentation for shape tools), or `ToolRegistry.execute` will reject Calc/Draw documents.

**Menu chat**: No tool-calling; same doc detection as sidebar.

**Chat config keys**: `chat_context_length`, `chat_max_tokens`, `additional_instructions`.

### HTML / Writer edits
- `get_document_content` / `apply_document_content`: see [`format_support.py`](plugin/modules/writer/format_support.py). **`apply_document_content`** accepts `content` as JSON array of HTML strings; also coerces a JSON-encoded string of an array when providers double-encode.
- **Robust JSON Parsing**: Tool call parsers use `safe_json_loads` (in [`plugin/framework/errors.py`](plugin/framework/errors.py)), which integrates robust repair logic: it attempts standard `json.loads`, then `strict=False` parsing (to handle control chars), followed by a custom `repair_json` step (merging truncated JSON braces and brackets), and finally an `ast.literal_eval` fallback for Python-style literal syntax (inspired by patterns in the `hermes-agent` project).
- **Format preservation**: Prefer **plain-text** `content` in `apply_document_content` when you want to keep character formatting; avoid HTML-wrapped strings on that path (see **Format preservation** under §11).

### Unified prompts

`additional_instructions` everywhere (Chat, Edit, Extend); LRU in `prompt_lru` (10 entries); [`populate_combobox_with_lru`](plugin/framework/config.py), etc. Chat builds prompt via `get_chat_system_prompt_for_document` in [`constants.py`](plugin/framework/constants.py). Sidebar has model/image comboboxes but not additional-instructions text (Settings only for that).

---

## 5. Calc (plugin)

Tools go through `tool_registry` / `ToolContext`. Modular tools: `cells.py`, `formulas.py`, `sheets.py`; `tools.py` builds `CALC_TOOLS` and forwards execution. **Charts** (Calc + Writer + Draw): [`plugin/modules/calc/charts.py`](plugin/modules/calc/charts.py) — Writer uses inline `TextEmbeddedObject`; `list_charts` / `_resolve_chart` / `delete_chart` also consider OLE2 chart shapes on Writer `getDrawPage()` when present. Some pyuno/headless LibreOffice runs do not complete Writer OLE chart insertion (nothing in body text / `getEmbeddedObjects()`); the in-process UNO test `test_writer_chart_polymorphic` skips with `unittest.SkipTest` when `get_chart_info` cannot resolve the chart after `create_chart`. **Analysis** (Goal Seek / Solver): [`plugin/modules/calc/analysis.py`](plugin/modules/calc/analysis.py) — `calc_solver` tries native services (`SolverLinear`, CoinMP, Lpsolve) before enumerating Java NLPSolver engines (avoids UI/frame issues on hidden documents). Overview: [docs/calc-analysis-tools.md](docs/calc-analysis-tools.md). **Gemini/OpenRouter**: no union types in JSON schemas; use `"type": "array"` + `items` for ranges; execute layers accept a single string as one-element list.
**Specialized Calc Toolsets**: Operations like managing cell comments, conditional formatting, standard sheet filter (AutoFilter-style), sheet management, image management, **pivot tables (DataPilot)**, and **form controls** on the active sheet live in specialized domains (`comments`, `conditional_formatting`, `sheet_filter`, `sheets`, `images`, `pivot_tables`, `forms`). To access these tools, the main agent must use `delegate_to_specialized_calc_toolset(domain=...)`. These tools inherit from `ToolCalcSpecialBase` and are filtered out of the core tool list. Conditional formatting (classic `TableConditionalFormat` / `add_conditional_format`, list, remove): [`plugin/modules/calc/conditional.py`](plugin/modules/calc/conditional.py); UNO background and roadmap: [docs/calc-conditional-formatting.md](docs/calc-conditional-formatting.md). Sheet filter: [`plugin/modules/calc/sheet_filter.py`](plugin/modules/calc/sheet_filter.py); [docs/calc-sheet-filter.md](docs/calc-sheet-filter.md) (includes future scope / roadmap in §5). Pivot tools: [`plugin/modules/calc/pivot.py`](plugin/modules/calc/pivot.py) (`ToolCalcPivotBase`). Form tools are implemented in [`plugin/modules/writer/forms.py`](plugin/modules/writer/forms.py): [`ToolWriterFormBase`](plugin/modules/writer/base.py) subclasses both `ToolWriterSpecialBase` and `ToolCalcSpecialBase` (single `specialized_domain`), with union `uno_services` on each concrete tool ([`plugin/modules/calc/forms.py`](plugin/modules/calc/forms.py) re-exports with `Writer*` aliases only).

**Future (Calc pivot / analysis, not implemented)**: Pivot *charts* (chart objects tied to DataPilot), natural-language-only pivot layout without explicit source header names (would need an LLM mapping step, similar to OnlyOffice’s `insertPivotTable` parse). The same list is recorded at the top of [`plugin/modules/calc/pivot.py`](plugin/modules/calc/pivot.py) so it is not lost.

---

## 6. Images (summary)

**Providers**: AI Horde ([`aihordeclient/`](plugin/framework/aihordeclient/)) vs **endpoint** (same URL/key as chat, **`image_model`** / `image_model_lru`). **Direct image** sidebar checkbox: `chat_direct_image` → `execute_tool("generate_image", …)`. ImageService: [`image_service.py`](plugin/framework/image_service.py); insertion/cursor rules: [`image_tools.py`](plugin/framework/image_tools.py) (ViewCursor → TextCursor before `insertTextContent`). Full keys: [IMAGE_GENERATION.md](IMAGE_GENERATION.md).

---

## 7. Writer navigation & outline

- **Helpers in [`document.py`](plugin/framework/document.py)**: `build_heading_tree`, bookmark/locator helpers used by chat and tools. A commented-out **`DocumentCache`** block is not active; ignore stale notes about cache invalidation. Some docstrings still mention “DocumentCache” — treat as legacy wording.
- **Registered outline tools** ([`plugin/modules/writer/outline.py`](plugin/modules/writer/outline.py)): **`get_document_tree`** (heading tree, optional content strategies), **`get_heading_children`** (drill into a heading by locator). Legacy names like `get_document_outline` / `get_heading_content` are **not** the current API.
- **content.py**: `get_document_content` / `apply_document_content` are active `ToolBase` tools; **`CloneHeadingBlock`** (and any remaining dummies) stay off until rebased.

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
- **translate_dialog**: walk `XControlContainer` with `queryInterface`; fallback `ElementNames` — see [`dialogs.py`](plugin/framework/dialogs.py). **`legacy_ui.py`**: do not pass saved config values through `gettext` (empty string → PO header garbage). **`plugin/framework/i18n.py` `_(msg)`**: *msg* must be `str` (`TypeError` otherwise). `WriterAgentException` stringifies before calling `_()`.
- **`legacy_ui.input_box`** (`EditInputDialog.xdl`, Extend/Edit Selection, etc.): when `execute()` returns false (ESC or window close — there is no separate Cancel button), **do not** call `XComponent.dispose()` on the dialog; the toolkit may already have torn it down, and a second dispose can segfault the office (nothing reaches Python logs).

### Format preservation

Prefer `apply_document_content` with **plain-text** `content` for format-preserving replacement; HTML/markdown goes through the normal import path.

---

## 12. Config file

Paths: Linux `~/.config/libreoffice/{4,24}/user/writeragent.json`; macOS `~/Library/Application Support/LibreOffice/4/user/`; Windows `%APPDATA%\LibreOffice\4\user\`. **`get_current_endpoint(ctx)`** for normalized URL. Main keys: `api_keys_by_endpoint`, `text_model`/`model`, `model_lru`, `image_model`, `image_model_lru`, chat keys (see §4). **Provider defaults** (when keys are unset): [`get_provider_defaults()`](plugin/framework/default_models.py) maps known providers from [`DEFAULT_MODELS`](plugin/framework/default_models.py); e.g. Together AI defaults to GPT-OSS 120B (`openai/gpt-oss-120b`), Gemini Flash Image 2.5 (`google/flash-image-2.5`), and Voxtral Mini 3B (`mistralai/Voxtral-Mini-3B-2507`); the catalog also lists GPT-OSS 20B (`openai/gpt-oss-20b`) for Together without the default flag. **OpenRouter-only request body**: optional `openrouter_chat_extra` (object) is deep-merged into `POST /v1/chat/completions` JSON when the endpoint is OpenRouter (`openrouter.ai` in the URL) or when **`is_openrouter`** is set true (for custom proxies). Blocklisted keys are not merged: `messages`, `tools`, `tool_choice`, `stream`. Typical fields: `provider` (routing), `models` / `route`, `plugins`, `user`, `reasoning`. Merge logic: [`plugin/framework/openrouter_chat_extra.py`](plugin/framework/openrouter_chat_extra.py); HTTP: [`plugin/modules/http/client.py`](plugin/modules/http/client.py). **Edit JSON outside the UI**: Settings → General → **Edit config file (JSON)** launches an external editor: Windows default handler / Notepad fallback; macOS `open -t`; Linux prefers Kate, then Gedit, then `$EDITOR`, then `$VISUAL`. If the file is missing, WriterAgent creates a minimal `{}` first. **Provider detection** and headers: [`plugin/framework/auth.py`](plugin/framework/auth.py). **Model list fetch**: [`populate_combobox_with_lru`](plugin/framework/config.py) / `fetch_available_models` when [`endpoint_url_suitable_for_v1_models_fetch`](plugin/framework/config.py) passes; **`_model_fetch_cache`** memoizes `/v1/models` per process; Settings debounces endpoint typing (~1s) then fetches on a **background** thread and refreshes the UI on the **main** thread ([`post_to_main_thread`](plugin/framework/queue_executor.py)). LRU list keys are not dataclass fields — see `_LRU_LIST_CONFIG_KEY_PREFIXES` / [`_resolve_default`](plugin/framework/config.py). **Extension update check**: optional `extension_update_check_epoch`; logic in [`extension_update_check.py`](plugin/framework/extension_update_check.py), scheduled from [`panel_wiring._wireControls`](plugin/modules/chatbot/panel_wiring.py). **`core`** must stay skipped in auto-generated Settings tabs (`manifest_registry.py` + [`legacy_ui.py`](plugin/framework/legacy_ui.py) must agree) or Settings crashes on missing `btn_tab_core`. **`WriterAgentConfig.validate()`** and **`get_config_int`**: see [`plugin/framework/config.py`](plugin/framework/config.py), [`plugin/tests/test_i18n.py`](plugin/tests/test_i18n.py). **`chat_max_tool_rounds`**: omit the key or set a positive integer; an empty string `""` falls back to 25 with a **debug** log only (non-empty invalid values still warn).

---

## 13. Logs

Same directory as `writeragent.json` (else `~/writeragent_debug.log`). **Image errors**: Horde → debug log `[AIHorde]`; endpoint → enable agent log, check `writeragent_agent.log` for `generate_image` `result_snippet`. **`translate_dialog` / i18n** details: §11.

---

## 14. Build and install

```bash
make build
make deploy   # or: unopkg remove org.extension.writeragent
make test     # ty + mypy + pyright + bandit, then pytest + in-LO runner (skips if no soffice); use make ruff / make build for Ruff
```

**Testing policy**: Every new feature or bug fix must include corresponding tests. Prefer native UNO tests when the change affects document interaction (Writer, Calc, Draw).

Also: `make build-no-recording`, `make release` (runs **`make test`** first—**`ty` + mypy + pyright + bandit**, then pytest + LO tests—then **`release-build`**, which depends on **`openrouter-catalog`**: fetch Orca slim OpenRouter models into [`registry/openrouter_models.json`](registry/openrouter_models.json) (not shipped in the extension), merge capabilities/context into curated OpenRouter rows in [`plugin/framework/default_models.py`](plugin/framework/default_models.py), then **`compile-translations`** and the OXT; override the API with **`ORCA_MODELS_URL`**; on fetch failure the sync script uses the last committed JSON if present; the release bundle omits bundled plugin tests and strips the debug menu). Run **`make openrouter-catalog`** alone to refresh the catalog (network; same fallback). **`make build`** and **`make build-no-recording`** also run **`ruff check plugin`** (not part of **`make test`**). **Translations**: overview → [`docs/localization.md`](docs/localization.md). `make build` runs `preview-translations` (refresh `writeragent.pot` + `translate_missing.py --preview` for the localization status table only), then `compile-translations`. Full template + PO merge: `make extract-strings` (runs `xgettext`, YAML merge, then **`merge-translations`**: `msgmerge --update` each `writeragent.po` + `msgattrib --no-obsolete`). Optional AI fill: `translate_missing.py` / `make auto-translate` when `OPENROUTER_API_KEY` is set. Contributor steps → [`plugin/locales/README.md`](plugin/locales/README.md).

Restart LibreOffice after deploy.

**In-process LO tests** (`$(LO_PYTHON) -m plugin.testing_runner`): [`plugin/testing_runner.py`](plugin/testing_runner.py) snapshots/restores `sys.modules` keys in [`plugin/tests/testing_utils.py`](plugin/tests/testing_utils.py) `NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS` between each `plugin/tests/uno/` module so pytest-oriented `setup_uno_mocks()` state does not leak. When real PyUNO is loaded, `setup_uno_mocks()` returns immediately (it must not replace `uno` with `MagicMock`).

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
- **Experimental Pyrefly** (`make pyrefly`, [pyrefly.org](https://pyrefly.org/)): Meta’s Rust-based checker; **`[tool.pyrefly]`** sets include/exclude, **`search-path = ["."]`** for **`plugin.*`** imports under **`TYPE_CHECKING`**, and full-body defaults (**`check-unannotated-defs`**, etc.). Optional comparison pass; not in CI **`make test`** until triaged.
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

## 20. Static Type Checking (ty)

Primary workflow and patterns: [`docs/type-checking.md`](docs/type-checking.md). **`make check`** → **`ty`** only; **`make build`** → **`ty`** + **`ruff`**; **`make typecheck`** → **`ty` + mypy + pyright**; **`make test`** adds **`bandit`** then pytest (see §1). **`types-unopy`** (dev) for UNO stubs. Run **`make fix-uno`** so `import uno` / `com.sun.star` resolve in `.venv`. **`make pyrefly`** is an optional [Pyrefly](https://pyrefly.org/) pass (same scope; not in **`make test`**).

**Common fixes**: use **`Protocol`** for mixin hosts (`self: ToolLoopHost`); `TYPE_CHECKING` for heavy imports; `cast(Any, …)` / `cast(Iterable, …)` where stubs are thin; explicit `None` checks before narrowing. **UNO interface overrides**: match stub parameter names exactly (e.g. `actionPerformed(self, rEvent)`) or `ty`/pyright report `invalid-method-override`.

---

## 21. Debugging

- **`make deploy`** vs **`make repack`**: full rebuild/deploy vs re-zip only.
- New components: [`extension/META-INF/manifest.xml`](extension/META-INF/manifest.xml).
- Buffered logs: `/tmp` scratch + `flush=True` when needed.
