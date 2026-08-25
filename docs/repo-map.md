# Repo map — entry points and topic hubs

**Assume the reader knows nothing about this project.** Invariants and
easy mistakes live in the repo-root [`AGENTS.md`](../AGENTS.md). This
page is the **catalog**: where to start by task, and which topic doc
to open. Area gotchas that are not global live next to the code in
`plugin/<area>/AGENTS.md`.

Living feature plans can sit beside these hubs without being listed
here (see [`ROADMAP.md`](ROADMAP.md)).

## Key files (entry points)

Start here by task.

**Layout:** `plugin/` (framework, chatbot, writer, calc, draw, scripting, librepy, …), `extension/` (WriterAgent OXT), `extension-core/` (LibrePy OXT), `scripts/`, `Makefile`, `pyproject.toml`.

| Area | Role | Paths |
|------|------|-------|
| Bootstrap / MCP | WriterAgent bootstrap, settings apply, MCP startup | [`plugin/main.py`](../plugin/main.py) |
| LibrePy bootstrap | Core OXT: `=PY()`, Python menus, Settings → Python; no chat/MCP | [`plugin/main_core.py`](../plugin/main_core.py), [`plugin/librepy/`](../plugin/librepy/), [`plugin/calc/python/addin_librepy.py`](../plugin/calc/python/addin_librepy.py) |
| Sidebar / send | Sidebar factory, panel, document resolution | [`plugin/chatbot/panel_factory.py`](../plugin/chatbot/panel_factory.py), [`plugin/chatbot/panel.py`](../plugin/chatbot/panel.py) |
| Tool loop / chat FSM | Main chat tool loop and state machine | [`plugin/chatbot/tool_loop.py`](../plugin/chatbot/tool_loop.py), [`plugin/chatbot/tool_loop_state.py`](../plugin/chatbot/tool_loop_state.py) |
| Smol / librarian ReAct | Separate ReAct runtime (shares `LlmClient`); do **not** merge with the main chat FSM | [`plugin/chatbot/smol_agent.py`](../plugin/chatbot/smol_agent.py) — [docs/chat-smol-tool-architecture.md](chat-smol-tool-architecture.md) |
| Agent backends | Optional external backends (`agent_backend.backend_id` when not `builtin`). ACP CLIs share [`acp_backend.py`](../plugin/agent_backend/acp_backend.py); set immutable `default_extra_args` when the official CLI needs a subcommand and settings args are empty. Grok overrides `_apply_default_extra_args` for `startswith("grok")`. Base `send()` drains prompt-result `contentBlocks`. | [`plugin/agent_backend/`](../plugin/agent_backend/) |
| HTTP / LLM | Chat requests, tools, token stripping, pacing | [`plugin/framework/client/llm_client.py`](../plugin/framework/client/llm_client.py) (`make_chat_request`, `request_with_tools`, …), [`plugin/framework/client/http_transport.py`](../plugin/framework/client/http_transport.py), [`plugin/framework/client/auth.py`](../plugin/framework/client/auth.py) |
| Tools registry | Tool registration and schemas | [`plugin/framework/tool.py`](../plugin/framework/tool.py) |
| UNO document helpers | Writer chat-context assembler and `DocumentService` (WriterAgent). Thin dispatcher: Writer via `text_helpers`, Calc via lazy `plugin.calc.analyzer`, Draw/Impress via `plugin.draw.bridge.get_draw_context_for_chat`. | [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py) |
| Light document helpers | Linebreaks, tracked-deletion reads, heading tree, path, selection text, Writer text slices; type guards; UD props (LibrePy-safe) | [`plugin/doc/text_helpers.py`](../plugin/doc/text_helpers.py), [`plugin/doc/doc_type.py`](../plugin/doc/doc_type.py), [`plugin/doc/udprops.py`](../plugin/doc/udprops.py) |
| Config / keys / LRU | `writeragent.json`, keys, LRU | [`plugin/framework/config.py`](../plugin/framework/config.py) (I/O, cache, getters); [`plugin/framework/config_schema.py`](../plugin/framework/config_schema.py) (pure schema/coercion — import from here, not `config`) |
| Dialogs / XDL | Dialog load helpers and settings UI | [`plugin/chatbot/dialogs.py`](../plugin/chatbot/dialogs.py), [`plugin/chatbot/dialog_views.py`](../plugin/chatbot/dialog_views.py), [`plugin/chatbot/settings_dialog.py`](../plugin/chatbot/settings_dialog.py), [`plugin/chatbot/eval_dashboard_ui.py`](../plugin/chatbot/eval_dashboard_ui.py), [`plugin/chatbot/bug_report.py`](../plugin/chatbot/bug_report.py) |
| Agent manual | On-demand `get_guidance` topics + agent-backend full manual | [`plugin/chatbot/agent_manual.py`](../plugin/chatbot/agent_manual.py) |
| Async UI drain | Stream queue drain on the UI thread (`get_toolkit`, `get_ctx`) | [`plugin/framework/async_stream.py`](../plugin/framework/async_stream.py), [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py) |
| Writer HTML / apply | HTML import and apply-content paths (callers `import format as format_support`) | [`plugin/writer/format.py`](../plugin/writer/format.py) |
| Writer charts / shapes | Shared tool names with Calc/Draw; declare union of `uno_services` | [`plugin/writer/specialized/charts.py`](../plugin/writer/specialized/charts.py), [`plugin/writer/specialized/shapes.py`](../plugin/writer/specialized/shapes.py) |
| Errors | `WriterAgentException`, `safe_json_loads`, tool errors | [`plugin/framework/errors.py`](../plugin/framework/errors.py) |
| FSM / service | Pure `next_state` only; no UNO/I/O in transitions | [`plugin/framework/service.py`](../plugin/framework/service.py) |
| Threading / UNO guard | `run_in_background`, `AsyncProcess`, Layer A `guard_uno` | [`plugin/framework/worker_pool.py`](../plugin/framework/worker_pool.py), [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py) |
| UNO listeners / i18n | UNO listeners; gettext `_` for UI | [`plugin/framework/uno_listeners.py`](../plugin/framework/uno_listeners.py), [`plugin/framework/i18n.py`](../plugin/framework/i18n.py) |
| Memory / prompts | Experimental memory + `MEMORY_GUIDANCE`; mode prompts live next to their modules (index in `prompts.py`) | [`plugin/chatbot/memory.py`](../plugin/chatbot/memory.py), [`plugin/framework/prompts.py`](../plugin/framework/prompts.py) |
| Extension update check | Weekly WriterAgent / LibrePy / LibreHarper update check | [`plugin/chatbot/extension_update_check.py`](../plugin/chatbot/extension_update_check.py) |
| Calc `=PROMPT()` / `=PYTHON()` | Calc spreadsheet function add-ins (LibrePy uses `addin_librepy.py` instead of `addin.py`) | [`plugin/calc/prompt_addin.py`](../plugin/calc/prompt_addin.py), [`plugin/calc/prompt_function.py`](../plugin/calc/prompt_function.py), [`plugin/calc/python/addin.py`](../plugin/calc/python/addin.py), [`plugin/calc/python/addin_librepy.py`](../plugin/calc/python/addin_librepy.py), [`plugin/calc/python/function.py`](../plugin/calc/python/function.py) |
| Scripting / venv | Public script API, sandbox policy, venv worker (not for user imports) | [`plugin/scripting/`](../plugin/scripting/), [`plugin/scripting/venv/`](../plugin/scripting/venv/), [`plugin/scripting/import_policy.py`](../plugin/scripting/import_policy.py), [`plugin/scripting/sandbox.py`](../plugin/scripting/sandbox.py), [`plugin/scripting/venv_worker.py`](../plugin/scripting/venv_worker.py), [`plugin/scripting/venv_diagnostics.py`](../plugin/scripting/venv_diagnostics.py) |
| Embeddings / folder FTS | Host indexers + venv worker + RPC | [`plugin/embeddings/`](../plugin/embeddings/), [`plugin/embeddings/venv/`](../plugin/embeddings/venv/), [`plugin/framework/client/embeddings_service.py`](../plugin/framework/client/embeddings_service.py), [`plugin/framework/client/embedding_client.py`](../plugin/framework/client/embedding_client.py), [`plugin/framework/client/folder_fts_service.py`](../plugin/framework/client/folder_fts_service.py) — [docs/embeddings.md](embeddings.md) |
| Vision / OCR | Host runner + venv worker + `run_vision` | [`plugin/vision/`](../plugin/vision/), [`plugin/vision/venv/`](../plugin/vision/venv/), [`plugin/scripting/client.py`](../plugin/scripting/client.py), [`plugin/vision/vision_availability.py`](../plugin/vision/vision_availability.py) — [docs/image-recognition.md](image-recognition.md) |
| PPT-Master | Impress/Draw adapters and session | [`plugin/contrib/ppt_master/`](../plugin/contrib/ppt_master/) ([README](../plugin/contrib/ppt_master/README.md)), [`plugin/ppt_master/`](../plugin/ppt_master/), [`plugin/chatbot/ppt_master.py`](../plugin/chatbot/ppt_master.py) — [integration plan](archive/ppt-master-integration-plan.md#roadmap) |
| Tests (unit pytest) | Headless pytest; no live soffice | `make pytest` — `-m "not slow and not integration" --ignore-glob='*_uno.py'` |
| Tests (UNO runner) | Native UNO tests (`@native_test`, `ctx`) | [`plugin/testing_runner.py`](../plugin/testing_runner.py) (`make test-run` after pytest) |
| Eval / benchmarks | CLI eval harness and prompt optimization | [`scripts/benchmark.py`](../scripts/benchmark.py), [`scripts/prompt_optimization/`](../scripts/prompt_optimization/) |
| Extension packaging | OXT resources; register new components in manifest | [`extension/`](../extension/) (`Dialogs/`, `idl/`, `metadata/`), [`extension/META-INF/manifest.xml`](../extension/META-INF/manifest.xml) |
| Build / tooling | Make targets, package metadata, Python pin, LibrePy file list | [`Makefile`](../Makefile), [`pyproject.toml`](../pyproject.toml), [`.python-version`](../.python-version), [`scripts/librepy_bundle_paths.py`](../scripts/librepy_bundle_paths.py) |

## Deep dives (link index)

| Topic | Doc |
|-------|-----|
| Chat sidebar implementation | [docs/chat-sidebar-implementation.md](chat-sidebar-implementation.md) |
| Rich text control sidebar | [docs/chat-rich-text-control-sidebar.md](chat-rich-text-control-sidebar.md) |
| Streaming / threading | [docs/framework-streaming-and-threading.md](framework-streaming-and-threading.md) |
| Threading architecture (pool, marshal, MCP) | [docs/framework-threading.md](framework-threading.md) |
| UNO thread-safety enforcement | [docs/framework-uno-thread-safety.md](framework-uno-thread-safety.md) |
| Smol vs main chat HTTP | [docs/chat-smol-tool-architecture.md](chat-smol-tool-architecture.md) |
| Writer specialized tool tiers | [docs/writer-specialized-toolsets.md](writer-specialized-toolsets.md) |
| Styles / LLM styling | [docs/writer-llm-styles.md](writer-llm-styles.md) |
| Writer API references | [docs/writer-bookmarks-api-reference.md](writer-bookmarks-api-reference.md), [docs/writer-footnotes-api-reference.md](writer-footnotes-api-reference.md), [docs/writer-page-api-reference.md](writer-page-api-reference.md), [docs/writer-tracking-api-reference.md](writer-tracking-api-reference.md) |
| Reviewable agent edits (surgical redlines, toolbar) | [docs/writer-reviewable-agent-edits.md](writer-reviewable-agent-edits.md) |
| LO-DOM & Semantic Tree | [docs/writer-lo-dom-semantic-tree.md](writer-lo-dom-semantic-tree.md) |
| Draw/Impress specialized | [docs/draw-impress-specialized-toolsets.md](draw-impress-specialized-toolsets.md), [docs/draw-shape-support.md](draw-shape-support.md) |
| Calc specialized | [docs/calc-specialized-toolsets.md](calc-specialized-toolsets.md) |
| Calc filters / formatting | [docs/calc-conditional-formatting.md](calc-conditional-formatting.md), [docs/calc-sheet-filter.md](calc-sheet-filter.md) |
| Calc date / time lifecycle | [docs/calc-date-time-handling.md](calc-date-time-handling.md) |
| Embeddings / folder FTS | [docs/embeddings.md](embeddings.md) |
| LibrePy / WriterAgent packaging split | [docs/scripting-librepy-split.md](scripting-librepy-split.md) |
| NumPy / Python venv bridge | [docs/enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md), [docs/calc-py-data-shapes.md](calc-py-data-shapes.md), [docs/scripting-numpy-serialization.md](scripting-numpy-serialization.md) |
| Scripting domain registries (shipped) | [docs/archive/scripting-domain-debt-dev-plan.md](archive/scripting-domain-debt-dev-plan.md) |
| NumPy domain helpers (Viz, Symbolic, Units, Text, …) | [docs/scripting-numpy-domains.md](scripting-numpy-domains.md) |
| Excel / Calc `=PY` design stance | [docs/scripting-ms-py-compatibility.md](scripting-ms-py-compatibility.md) |
| Agent Search / Web | [docs/agent-search.md](agent-search.md) |
| MCP protocol | [docs/mcp-protocol.md](mcp-protocol.md) |
| Localization / translations | [docs/localization.md](localization.md), [locales/README.md](../locales/README.md) |
| Audio Architecture | [docs/audio-architecture.md](audio-architecture.md) |
| Image generation | [docs/image-generation.md](image-generation.md) |
| Image recognition (local OCR / detection) | [docs/image-recognition.md](image-recognition.md) |
| PPT-Master (Impress/Draw) | [docs/archive/ppt-master-integration-plan.md](archive/ppt-master-integration-plan.md) (architecture + [roadmap](archive/ppt-master-integration-plan.md#roadmap)) |
| Math / HTML import design | [docs/writer-math-tex.md](writer-math-tex.md) |
| Grammar pipeline (cache, queue) | [docs/writer-grammar-checker-plan.md](writer-grammar-checker-plan.md) |
| Test Architecture | [docs/archive/test_architecture_analysis.md](archive/test_architecture_analysis.md) |
| Type checking | [docs/framework-type-checking.md](framework-type-checking.md) |
| UNO Dialogs & Wizards | [docs/framework-uno-dialogs.md](framework-uno-dialogs.md) |
| LLM Hacks & Workarounds | [docs/chat-llm-hacks.md](chat-llm-hacks.md) |
| Experimental memory / roadmap | [docs/archive/hermes-agent-patterns.md](archive/hermes-agent-patterns.md), [docs/ROADMAP.md](ROADMAP.md), [docs/framework-robustness-roadmap.md](framework-robustness-roadmap.md) |
| LLM evals / benchmarks | [docs/eval-benchmarks.md](eval-benchmarks.md), [scripts/prompt_optimization/README.md](../scripts/prompt_optimization/README.md) |

## References

- Dialog DTD (LibreOffice tree): `xmlscript/dtd/dialog.dtd`
- GUI DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces
