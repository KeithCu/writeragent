# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file lists **invariants**, **entry points**, and **easy mistakes**. Everything else is in the linked modules and docs—open those when you change behavior. Entry points (area, role, paths) are in [Key files](#key-files-entry-points) at the bottom.

> [!IMPORTANT]
> **Docs:** After any nontrivial change, update documentation. Prefer the **topic doc** under `docs/`; touch **`AGENTS.md`** only when the change affects **many areas** or **global rules**.
> [!IMPORTANT]
> **Complexity:** This codebase is complicated for its size. When asked to do a new feature, always figure out the way using the least amount of code or extra complexity. Using existing functions, there are many functions which can just be used or refactored to make the change small for a new feature.

If you find ways to lower technical debt, while adding a feature, put that in your plan.

> [!IMPORTANT]
> **Tests:** New features and bugfixes **must** include tests.
> - **Unit:** `tests/`, **pytest** when logic can be mocked. Test files should match the source module name (e.g. `foo.py` -> `test_foo.py`). **Always add new test cases to the matching `test_` file to maintain consistent naming and visible coverage.**
> - **UNO / LibreOffice:** `tests/uno/` or `_uno.py` suffix via **`testing_runner.py`** (no pytest)—use **`@native_test`**, **`@setup`**, **`@teardown`**; test functions take **`ctx`**. **Follow the same module-matching rule (e.g. `foo.py` -> `test_foo_uno.py`).**
> - **Execution Policy:** Run tests for the specific files modified plus **`make typecheck`**. Run full **`make test`** ONLY IF making large refactors or cross-cutting changes.

> [!IMPORTANT]
> **Comments:** Write why this code is there for the reader who would otherwise be **lost**. **Good comments are the bridge** from opaque to understandable and maintainable code. Some files have no comments: inserting footnotes is standard, little different from other UNO objects. Meanwhile some comments are critical to understanding why the code is there. Write clear, short comments.
> - **Bugfixes (required):** at the fix, **what was wrong**, **how it happened**, and **why this change** fixes it.
> - **LibreOffice / UNO / Etc.:** quirks. When matching upstream behavior, cite **source** (file + line or function), not a vague “like Lightproof.”

---

## Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw (Impress paths where registered).

- **Chat:** Sidebar + menu chat (Writer/Calc deck; Draw per code paths)—multi-turn, tools, history (SQLite when available, else JSON under `writeragent_history.db.d/`).
- **Extend / Edit selection:** Writer uses `get_string_without_tracked_deletions()` in `text_helpers` for prompts; undo/session details in `document_helpers`.
- **Settings:** `writeragent.json` under the LibreOffice user profile—see `config` module doc.
- **Memory (experimental):** `memory` + `MEMORY_GUIDANCE` in `prompts` — [docs/hermes-agent-patterns.md](docs/hermes-agent-patterns.md).
- **Calc:** `=PROMPT()` and `=PYTHON()` add-ins (see Key files).
- **Eval / benchmarks:** `make run_eval` / `scripts/benchmark.py` → `scripts/prompt_optimization/` — [scripts/prompt_optimization/README.md](scripts/prompt_optimization/README.md), [docs/eval-dev-plan.md](docs/eval-dev-plan.md).

**Python:** Dev/tooling **3.11–3.13** (`pyproject.toml`); dev `.venv` is pinned to **3.13** via `.python-version` (3.14 lacks wheels for some dev deps such as spaCy). **Extension runtime** is whatever LibreOffice bundles (often older). **Shipped code under `plugin/` must not rely on stdlib newer than that runtime.**

**GPL v3+**; prior contributors credited in headers/installer.

---

## Essential commands

| Command | When to use |
|---------|-------------|
| `make typecheck` | After edits (required with targeted tests). Checker details: [docs/type-checking.md](docs/type-checking.md) |
| `make deploy` | WriterAgent OXT: build + install/cache sync; **restart LibreOffice** (or `make deploy writer/calc/draw/impress` to launch) |
| `make deploy-core` | LibrePy OXT only (`build/LibrePy.oxt`); **removes WriterAgent**. Install one OXT at a time. |
| `make test` | Large or cross-cutting changes only (includes typecheck, SAST, pytest, LO tests) |
| `make build` | Produce `build/WriterAgent.oxt` only (no install) |
| `make build-core` | Produce `build/LibrePy.oxt` only (no install) |
| `make release` | Typecheck + bandit, verify a stripped tree in `/tmp` (`compileall` + pytest + LO tests), then build/register `build/WriterAgent.oxt` |

Usual targets generate `plugin/_manifest.py` when needed. Other Makefile targets exist for fuzz and niche tooling—see the `Makefile` when you need them.

---

## HTTP / LLM (summary)

Chat and tool calls go through `llm_client` (see its module doc). Persistent connections live in `ai/service`; auth headers in `auth`.

The librarian / smolagents path must use `WriterAgentSmolModel` in `smol_agent`—do not add a second HTTP client. Details: [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md), [docs/llm-hacks.md](docs/llm-hacks.md).

---

## Cross-cutting invariants

Rules that apply in many places. Breaking them causes wrong-document bugs, frozen UI, or tools that never run. Paths are in [Key files](#key-files-entry-points).

- **Use the extension’s `self.ctx`, not a fresh UNO context.** Lookups for package info, dialogs, and similar must use the component context the extension was given. Calling `uno.getComponentContext()` can return a different context and quietly break those lookups. Same idea for Calc chat context: `get_calc_context_for_chat` needs `ctx` from the panel / MainJob, not a bootstrap call.

- **Keep the chat FSM pure.** In `service`, `next_state` only computes the next state—no UNO calls and no I/O. Side effects (UI updates, MCP, document work) belong in the panel or MCP layers.

- **Stream on a worker; drain on the UI thread.** Background work pushes tuples onto a `queue.Queue`. The first element must be a `StreamQueueKind` **enum member**, not a bare string. Drain with `run_async_worker_with_drain` / `get_toolkit(ctx)` so the UI processes events via `toolkit.processEventsToIdle()`. Do not use UNO `XTimerListener` for sidebar streaming. More: [docs/streaming-and-threading.md](docs/streaming-and-threading.md).

- **Refresh document context each chat send.** Each user send replaces the `[DOCUMENT CONTENT]` system message so the model sees the current document, not a stale snapshot.

- **Register tools so schemas and execution agree.** Matching uses `uno_services` first, then `doc_types`. Anything advertised by `get_schemas` must be runnable via `execute`. Default main-chat tools are `tier="core"`; nested specialized sets use `specialized` / `specialized_control` and are omitted from default lists. Gateway tools must list **every** UNO service they support (e.g. Draw **and** Impress). Writer `charts` / `shapes` share tool **names** with Calc/Draw—the Writer class must declare the **union** of those services or execution rejects the document.

- **Do not start raw threads for background work.** Use `run_in_background`. Short fire-and-forget jobs share a bounded daemon pool; pass `dedicated=True` (or `daemon=False`) for servers, pipe drains, infinite loops, and any job another thread will `join()`. Long subprocesses use `AsyncProcess`; if stderr is piped, drain it continuously or redirect it, or the process can deadlock ([docs/threading_architecture.md](docs/threading_architecture.md)). Dev builds enable a UNO thread guard by default (`thread_guard`; set `WRITERAGENT_UNO_THREAD_GUARD=0` to opt out; release OXTs stub it off). Wrap document-model access at boundaries with `guard_uno` (e.g. `get_active_document`, frame `_get_document_model`, `resolve_document_by_url`, `open_document_for_read`). For `ToolContext`, use `get_ctx()`—not the raw bootstrap `self.ctx`. Details: [docs/uno-thread-safety-enforcement.md](docs/uno-thread-safety-enforcement.md).

- **Surface errors through the shared helpers.** Prefer `WriterAgentException` and `format_error_payload` (`errors`). Tools should fail via `_tool_error`. There is no active `DocumentCache`—do not assume one.

- **Two products, one OXT at a time.** WriterAgent (`make deploy`, `plugin/main.py`) vs LibrePy (`make build-core` / `deploy-core`, `plugin/main_core.py`, `extension-core/`). `deploy-core` removes WriterAgent. Dual-install overlay is **not shipped**. File list: [`scripts/librepy_bundle_paths.py`](scripts/librepy_bundle_paths.py). Packaging: [docs/libreoffice-core-python-extension-split.md](docs/libreoffice-core-python-extension-split.md).

- **LibrePy-safe document helpers.** Linebreaks, tracked-deletion reads, heading trees, path, and Writer selection range / char count: `plugin/doc/text_helpers.py`. Type guards: `doc_type.py`. Document properties: `udprops.py`. Do **not** import `document_helpers` from LibrePy paths (it pulls Calc analyzer / chat context). Do **not** re-export the light helpers from `document_helpers`.

- **`plugin.framework.client` package init is lazy.** HTTP / errors / provider detection load immediately; `LlmClient`, embeddings, and analysis load on attribute access. LibrePy may import `requests` / `provider_detection`. Do not import `llm_client` or embeddings from LibrePy paths.

UNO helpers are intentionally split (`uno_context`, `text_helpers` / `doc_type` / `udprops`, `document_helpers` for chat/undo, `dialogs`)—there is no monolithic `uno_helpers.py`.

---

## Tips and sharp edges

Area-specific rules live in module docstrings and topic docs—open those when you edit that area. Entry points: [Key files](#key-files-entry-points). Topic docs: [Deep dives](#deep-dives-link-index).

- **Sidebar / chat:** Resolve the document from the **frame only** (`frame.getController().getModel()` in `panel`). For Stop / cancel, use **`resolve_stop_checker()`**—not a panel boolean alone. Modes and routing: [docs/chat-sidebar-implementation.md](docs/chat-sidebar-implementation.md). Streaming details: [docs/streaming-and-threading.md](docs/streaming-and-threading.md).

- **Dialogs (XDL):** Load with `DialogProvider` and the extension `base_url` (see `dialogs` module doc). Settings UI is in `dialog_views`.

- **Tools / Writer / Calc:** In tests, resolve tools with `plugin.main.get_tools().get("tool_name")`. Deeper topics: [docs/math-tex.md](docs/math-tex.md), [docs/realtime-grammar-checker-plan.md](docs/realtime-grammar-checker-plan.md), [docs/calc-specialized-toolsets.md](docs/calc-specialized-toolsets.md), [docs/enabling_numpy_in_libreoffice.md](docs/enabling_numpy_in_libreoffice.md), [docs/calc-py-data-shapes.md](docs/calc-py-data-shapes.md), [docs/numpy-domains.md](docs/numpy-domains.md).

- **Config:** Call `init_config(ctx)` once at bootstrap. Later config I/O does not take `ctx`—see the `config` module doc.

- **Logging / MCP:** Logs go to `writeragent_debug.log` next to `writeragent.json`. Shipped LibrePy (`make deploy-core`) defaults to **`log_level` WARN**; a checkout that still has `plugin/tests/` defaults to **DEBUG**. Override in `writeragent.json` and restart LibreOffice. `enable_agent_log` is separate (structured agent traces only). In unexpected `except` blocks, use **`log.exception("Context")`**. MCP work drains on the main thread ([docs/mcp-protocol.md](docs/mcp-protocol.md)). Image generation: [docs/image-generation.md](docs/image-generation.md). Do not read API keys from the environment in production; do not use **`tempfile.mktemp()`**. For scratch debug files under `/tmp`, prefer `flush=True`.

- **Tests / packaging:** UNO tests go through `testing_runner`; debug-menu suites run on the UI thread ([docs/test_architecture_analysis.md](docs/test_architecture_analysis.md)). New extension components must be registered in `extension/META-INF/manifest.xml`.

### Global Python

Do not reuse the names **`logging`**, module **`log`**, or gettext **`_`** for unrelated variables. UI code imports **`_`** from `i18n`. Never bind bare `_` as a throwaway (`for _ in …`, `a, _, _ = fn()`, `except Exception as _:`)—use a real name (`unused`, `idx`). Private helpers named `_foo` are fine.

### Do not redo (already shipped)

- Do **not** invent `python_config.py` or rename `writeragent.json` for LibrePy.
- Do **not** split `payload_codec.py` flatten/unpack without serialization A/B tests ([docs/numpy-serialization.md](docs/numpy-serialization.md)).
- Envelope-detector `@deal` + Hypothesis oracles on `payload_codec` (`is_split_grid`, `is_multi_data`, image / dataframe / calc_range) are **shipped**. Source of truth: [docs/serialization-verification-plan.md](docs/serialization-verification-plan.md).
- Scripting domain registries (Phases 1–6) are shipped — do not add a fourth ad-hoc registry ([docs/scripting-domain-debt-dev-plan.md](docs/scripting-domain-debt-dev-plan.md)).
- `calc_functions_*.py` alphabet splits are intentional; do not merge them.
- Do **not** drop `plugin/calc/analyzer.py` from the LibrePy bundle (reserved for later use).
- Do **not** slim `trusted_action_registry.py` / `venv_diagnostics.py` for LibrePy while those modules still work.

---

## Key files (entry points)

Start here by task. Topic docs: [Deep dives](#deep-dives-link-index).

**Layout:** `plugin/` (framework, chatbot, writer, calc, draw, scripting, librepy, …), `extension/` (WriterAgent OXT), `extension-core/` (LibrePy OXT), `scripts/`, `Makefile`, `pyproject.toml`.

| Area | Role | Paths |
|------|------|-------|
| Bootstrap / MCP | WriterAgent bootstrap, settings apply, MCP startup | [`plugin/main.py`](plugin/main.py) |
| LibrePy bootstrap | Core OXT: `=PY()`, Python menus, Settings → Python; no chat/MCP | [`plugin/main_core.py`](plugin/main_core.py), [`plugin/librepy/`](plugin/librepy/), [`plugin/calc/python/addin_librepy.py`](plugin/calc/python/addin_librepy.py) |
| Sidebar / send | Sidebar factory, panel, document resolution | [`plugin/chatbot/panel_factory.py`](plugin/chatbot/panel_factory.py), [`plugin/chatbot/panel.py`](plugin/chatbot/panel.py) |
| Tool loop / chat FSM | Main chat tool loop and state machine | [`plugin/chatbot/tool_loop.py`](plugin/chatbot/tool_loop.py), [`plugin/chatbot/tool_loop_state.py`](plugin/chatbot/tool_loop_state.py) |
| Smol / librarian ReAct | Separate ReAct runtime (shares `LlmClient`); do **not** merge with the main chat FSM | [`plugin/chatbot/smol_agent.py`](plugin/chatbot/smol_agent.py) — [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md) |
| Agent backends | Optional external backends (`agent_backend.backend_id` when not `builtin`) | [`plugin/agent_backend/`](plugin/agent_backend/) |
| HTTP / LLM | Chat requests, tools, token stripping, pacing | [`plugin/framework/client/llm_client.py`](plugin/framework/client/llm_client.py) (`make_chat_request`, `request_with_tools`, …), [`plugin/ai/service.py`](plugin/ai/service.py), [`plugin/framework/client/auth.py`](plugin/framework/client/auth.py) |
| Tools registry | Tool registration and schemas | [`plugin/framework/tool.py`](plugin/framework/tool.py) |
| UNO document helpers | Chat context, undo/stream, URL/frame resolve (WriterAgent) | [`plugin/doc/document_helpers.py`](plugin/doc/document_helpers.py) |
| Light document helpers | Linebreaks, tracked-deletion reads, heading tree, path; type guards; UD props (LibrePy-safe) | [`plugin/doc/text_helpers.py`](plugin/doc/text_helpers.py), [`plugin/doc/doc_type.py`](plugin/doc/doc_type.py), [`plugin/doc/udprops.py`](plugin/doc/udprops.py) |
| Config / keys / LRU | `writeragent.json`, keys, LRU | [`plugin/framework/config.py`](plugin/framework/config.py) |
| Dialogs / XDL | Dialog load helpers and settings UI | [`plugin/chatbot/dialogs.py`](plugin/chatbot/dialogs.py), [`plugin/chatbot/dialog_views.py`](plugin/chatbot/dialog_views.py), [`plugin/chatbot/settings_dialog.py`](plugin/chatbot/settings_dialog.py) |
| Async UI drain | Stream queue drain on the UI thread (`get_toolkit`, `get_ctx`) | [`plugin/framework/async_stream.py`](plugin/framework/async_stream.py), [`plugin/framework/uno_context.py`](plugin/framework/uno_context.py) |
| Writer HTML / apply | HTML import and apply-content paths (callers `import format as format_support`) | [`plugin/writer/format.py`](plugin/writer/format.py) |
| Writer charts / shapes | Shared tool names with Calc/Draw; declare union of `uno_services` | [`plugin/writer/charts.py`](plugin/writer/charts.py), [`plugin/writer/shapes.py`](plugin/writer/shapes.py) |
| Errors | `WriterAgentException`, `safe_json_loads`, tool errors | [`plugin/framework/errors.py`](plugin/framework/errors.py) |
| FSM / service | Pure `next_state` only; no UNO/I/O in transitions | [`plugin/framework/service.py`](plugin/framework/service.py) |
| Threading / UNO guard | `run_in_background`, `AsyncProcess`, Layer A `guard_uno` | [`plugin/framework/worker_pool.py`](plugin/framework/worker_pool.py), [`plugin/framework/thread_guard.py`](plugin/framework/thread_guard.py) |
| UNO listeners / i18n | UNO listeners; gettext `_` for UI | [`plugin/framework/uno_listeners.py`](plugin/framework/uno_listeners.py), [`plugin/framework/i18n.py`](plugin/framework/i18n.py) |
| Memory / prompts | Experimental memory + `MEMORY_GUIDANCE` | [`plugin/chatbot/memory.py`](plugin/chatbot/memory.py), [`plugin/framework/prompts.py`](plugin/framework/prompts.py) |
| Extension update check | Weekly WriterAgent / LibrePy / LibreHarper update check | [`plugin/chatbot/extension_update_check.py`](plugin/chatbot/extension_update_check.py) |
| Calc `=PROMPT()` / `=PYTHON()` | Calc spreadsheet function add-ins (LibrePy uses `addin_librepy.py` instead of `addin.py`) | [`plugin/calc/prompt_addin.py`](plugin/calc/prompt_addin.py), [`plugin/calc/prompt_function.py`](plugin/calc/prompt_function.py), [`plugin/calc/python/addin.py`](plugin/calc/python/addin.py), [`plugin/calc/python/addin_librepy.py`](plugin/calc/python/addin_librepy.py), [`plugin/calc/python/function.py`](plugin/calc/python/function.py) |
| Scripting / venv | Public script API, sandbox policy, venv worker (not for user imports) | [`plugin/scripting/`](plugin/scripting/), [`plugin/scripting/venv/`](plugin/scripting/venv/), [`plugin/scripting/import_policy.py`](plugin/scripting/import_policy.py), [`plugin/scripting/sandbox.py`](plugin/scripting/sandbox.py), [`plugin/scripting/venv_worker.py`](plugin/scripting/venv_worker.py), [`plugin/scripting/venv_diagnostics.py`](plugin/scripting/venv_diagnostics.py) |
| Embeddings / folder FTS | Host indexers + venv worker + RPC | [`plugin/embeddings/`](plugin/embeddings/), [`plugin/embeddings/venv/`](plugin/embeddings/venv/), [`plugin/framework/client/embeddings_service.py`](plugin/framework/client/embeddings_service.py), [`plugin/framework/client/embedding_client.py`](plugin/framework/client/embedding_client.py), [`plugin/framework/client/folder_fts_service.py`](plugin/framework/client/folder_fts_service.py) — [docs/embeddings.md](docs/embeddings.md) |
| Vision / OCR | Host runner + venv worker + `run_vision` | [`plugin/vision/`](plugin/vision/), [`plugin/vision/venv/`](plugin/vision/venv/), [`plugin/scripting/client.py`](plugin/scripting/client.py), [`plugin/vision/vision_availability.py`](plugin/vision/vision_availability.py) — [docs/image-recognition.md](docs/image-recognition.md) |
| PPT-Master | Impress/Draw adapters and session | [`plugin/contrib/ppt_master/`](plugin/contrib/ppt_master/) ([README](plugin/contrib/ppt_master/README.md)), [`plugin/ppt_master/`](plugin/ppt_master/), [`plugin/chatbot/ppt_master.py`](plugin/chatbot/ppt_master.py) — [integration plan](docs/ppt-master-integration-plan.md#roadmap) |
| Tests (UNO runner) | Native UNO tests (`@native_test`, `ctx`) | [`plugin/testing_runner.py`](plugin/testing_runner.py) |
| Eval / benchmarks | CLI eval harness and prompt optimization | [`scripts/benchmark.py`](scripts/benchmark.py), [`scripts/prompt_optimization/`](scripts/prompt_optimization/) |
| Extension packaging | OXT resources; register new components in manifest | [`extension/`](extension/) (`Dialogs/`, `idl/`, `metadata/`), [`extension/META-INF/manifest.xml`](extension/META-INF/manifest.xml) |
| Build / tooling | Make targets, package metadata, Python pin, LibrePy file list | [`Makefile`](Makefile), [`pyproject.toml`](pyproject.toml), [`.python-version`](.python-version), [`scripts/librepy_bundle_paths.py`](scripts/librepy_bundle_paths.py) |

---

## Deep dives (link index)

| Topic | Doc |
|-------|-----|
| Chat sidebar implementation | [docs/chat-sidebar-implementation.md](docs/chat-sidebar-implementation.md) |
| Rich text control sidebar | [docs/rich-text-control-sidebar.md](docs/rich-text-control-sidebar.md) |
| Streaming / threading | [docs/streaming-and-threading.md](docs/streaming-and-threading.md) |
| Threading architecture (pool, marshal, MCP) | [docs/threading_architecture.md](docs/threading_architecture.md) |
| UNO thread-safety enforcement | [docs/uno-thread-safety-enforcement.md](docs/uno-thread-safety-enforcement.md) |
| Smol vs main chat HTTP | [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md) |
| Writer specialized tool tiers | [docs/writer-specialized-toolsets.md](docs/writer-specialized-toolsets.md) |
| Styles / LLM styling | [docs/llm-styles.md](docs/llm-styles.md) |
| Writer API references | [docs/bookmarks-api-reference.md](docs/bookmarks-api-reference.md), [docs/footnotes-api-reference.md](docs/footnotes-api-reference.md), [docs/page-api-reference.md](docs/page-api-reference.md), [docs/writer-tracking-api-reference.md](docs/writer-tracking-api-reference.md) |
| Reviewable agent edits (surgical redlines, toolbar) | [docs/reviewable-agent-edits.md](docs/reviewable-agent-edits.md) |
| LO-DOM & Semantic Tree | [docs/lo-dom-semantic-tree.md](docs/lo-dom-semantic-tree.md) |
| Draw/Impress specialized | [docs/draw-impress-specialized-toolsets.md](docs/draw-impress-specialized-toolsets.md), [docs/shape_support.md](docs/shape_support.md) |
| Calc specialized | [docs/calc-specialized-toolsets.md](docs/calc-specialized-toolsets.md) |
| Calc filters / formatting | [docs/calc-conditional-formatting.md](docs/calc-conditional-formatting.md), [docs/calc-sheet-filter.md](docs/calc-sheet-filter.md) |
| Calc date / time lifecycle | [docs/calc-date-time-handling.md](docs/calc-date-time-handling.md) |
| Embeddings / folder FTS | [docs/embeddings.md](docs/embeddings.md) |
| LibrePy / WriterAgent packaging split | [docs/libreoffice-core-python-extension-split.md](docs/libreoffice-core-python-extension-split.md) |
| NumPy / Python venv bridge | [docs/enabling_numpy_in_libreoffice.md](docs/enabling_numpy_in_libreoffice.md), [docs/calc-py-data-shapes.md](docs/calc-py-data-shapes.md), [docs/numpy-serialization.md](docs/numpy-serialization.md) |
| Scripting domain registries (shipped) | [docs/scripting-domain-debt-dev-plan.md](docs/scripting-domain-debt-dev-plan.md) |
| NumPy domain helpers (Viz, Symbolic, Units, Text, …) | [docs/numpy-domains.md](docs/numpy-domains.md) |
| Excel / Calc `=PY` design stance | [docs/ms-py-libreoffice-compatibility.md](docs/ms-py-libreoffice-compatibility.md) |
| Agent Search / Web | [docs/agent-search.md](docs/agent-search.md) |
| MCP protocol | [docs/mcp-protocol.md](docs/mcp-protocol.md) |
| Localization / translations | [docs/localization.md](docs/localization.md), [locales/README.md](locales/README.md) |
| Audio Architecture | [docs/audio-architecture.md](docs/audio-architecture.md) |
| Image generation | [docs/image-generation.md](docs/image-generation.md) |
| Image recognition (local OCR / detection) | [docs/image-recognition.md](docs/image-recognition.md) |
| PPT-Master (Impress/Draw) | [docs/ppt-master-integration-plan.md](docs/ppt-master-integration-plan.md) (architecture + [roadmap](docs/ppt-master-integration-plan.md#roadmap)) |
| Math / HTML import design | [docs/math-tex.md](docs/math-tex.md) |
| Grammar pipeline (cache, queue) | [docs/realtime-grammar-checker-plan.md](docs/realtime-grammar-checker-plan.md) |
| Test Architecture | [docs/test_architecture_analysis.md](docs/test_architecture_analysis.md) |
| Type checking | [docs/type-checking.md](docs/type-checking.md) |
| UNO Dialogs & Wizards | [docs/uno-dialog-and-wizard-reference.md](docs/uno-dialog-and-wizard-reference.md) |
| LLM Hacks & Workarounds | [docs/llm-hacks.md](docs/llm-hacks.md) |
| Experimental memory / roadmap | [docs/hermes-agent-patterns.md](docs/hermes-agent-patterns.md), [docs/ROADMAP.md](docs/ROADMAP.md), [docs/robustness-roadmap.md](docs/robustness-roadmap.md) |
| LLM evals / benchmarks | [docs/benchmarks.md](docs/benchmarks.md), [scripts/prompt_optimization/README.md](scripts/prompt_optimization/README.md) |

---

## References

- Dialog DTD (LibreOffice tree): `xmlscript/dtd/dialog.dtd`
- GUI DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces
