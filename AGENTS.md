# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file lists **invariants**, **entry points**, and **easy mistakes**. Everything else is in the linked modules and docs—open those when you change behavior.

> [!IMPORTANT]
> **Docs:** After any nontrivial change, update documentation. Prefer the **topic doc** under `docs/`; touch **`AGENTS.md`** only when the change affects **many areas** or **global rules**.
> [!IMPORTANT]
> **Complexity:** This codebase is complicated for its size. When asked to do a new feature, always figure out the way using the least amount of code or extra complexity. Using existing functions, there are many functions which can just be used or refactored to make the change small for a new feature. 

If you find ways to lower technical debt, while adding a feature, put that in your plan.



> [!IMPORTANT]
> **Tests:** New features and bugfixes **must** include tests.
> - **Unit:** `tests/`, **pytest** when logic can be mocked. Test files should match the source module name (e.g. `foo.py` -> `test_foo.py`). **Always add new test cases to the matching `test_` file to maintain consistent naming and visible coverage.**
> - **UNO / LibreOffice:** `tests/uno/` or `_uno.py` suffix via **`testing_runner.py`** (no pytest)—use **`@native_test`**, **`@setup`**, **`@teardown`**; test functions take **`ctx`**. **Follow the same module-matching rule (e.g. `foo.py` -> `test_foo_uno.py`).**
> - Run **`make test`** before you consider the work done.

> [!IMPORTANT]
> **Comments:** Write why this code is there for the reader who would otherwise be **lost**. **Good comments are the bridge** from opaque to understandable and maintainable code. Some files have no comments: inserting footnotes is standard, little different from other UNO objects. Meanwhile some comments are critical to understanding why the code is there. Write clear, short comments.
> - **Bugfixes (required):** at the fix, **what was wrong**, **how it happened**, and **why this change** fixes it.
> - **LibreOffice / UNO / Etc.:** quirks. When matching upstream behavior, cite **source** (file + line or function), not a vague “like Lightproof.”

---

## Quick orientation — start here by task

| Area | Primary entry points |
|------|---------------------|
| Bootstrap, settings apply, MCP bootstrap | [`plugin/main.py`](plugin/main.py) |
| Sidebar, send, document resolution | [`plugin/chatbot/panel_factory.py`](plugin/chatbot/panel_factory.py), [`plugin/chatbot/panel.py`](plugin/chatbot/panel.py) |
| Tool loop / chat FSM | [`plugin/chatbot/tool_loop.py`](plugin/chatbot/tool_loop.py), [`plugin/chatbot/tool_loop_state.py`](plugin/chatbot/tool_loop_state.py) |
| HTTP / LLM | [`plugin/framework/client/llm_client.py`](plugin/framework/client/llm_client.py) (`make_chat_request`, `request_with_tools`, token stripping, shims, pacing) |
| Tools registry | [`plugin/framework/tool.py`](plugin/framework/tool.py) |
| UNO document helpers | [`plugin/doc/document_helpers.py`](plugin/doc/document_helpers.py) |
| Config / keys / LRU | [`plugin/framework/config.py`](plugin/framework/config.py) |
| Dialogs / XDL helpers | [`plugin/chatbot/dialogs.py`](plugin/chatbot/dialogs.py) |
| Async UI drain | [`plugin/framework/async_stream.py`](plugin/framework/async_stream.py), [`plugin/framework/uno_context.py`](plugin/framework/uno_context.py) (`get_toolkit`) |
| Writer HTML / apply content | [`plugin/writer/format_support.py`](plugin/writer/format_support.py) |
| Errors / `safe_json_loads` | [`plugin/framework/errors.py`](plugin/framework/errors.py) |
| Weekly extension update check | [`plugin/chatbot/extension_update_check.py`](plugin/chatbot/extension_update_check.py) |
| Python venv sandbox / scripting | Public script API: [`plugin/scripting/`](plugin/scripting/) (`analysis`, `viz`, `calc_functions`, … — lazy facades). Venv subprocess implementation: [`plugin/scripting/venv/`](plugin/scripting/venv/) (worker IPC + compute; not for user import paths). Policy: [`import_policy.py`](plugin/scripting/import_policy.py), whitelist + spawn env [`sandbox.py`](plugin/scripting/sandbox.py), worker [`venv_worker.py`](plugin/scripting/venv_worker.py), diagnostics [`venv_diagnostics.py`](plugin/scripting/venv_diagnostics.py) |
| Embeddings / folder FTS | [`plugin/embeddings/`](plugin/embeddings/) (host cache, indexers, tools); venv worker in [`plugin/embeddings/venv/`](plugin/embeddings/venv/); RPC in [`plugin/framework/client/embeddings_service.py`](plugin/framework/client/embeddings_service.py), [`embedding_client.py`](plugin/framework/client/embedding_client.py), [`folder_fts_service.py`](plugin/framework/client/folder_fts_service.py) — [docs/embeddings.md](docs/embeddings.md) |
| Vision / OCR | [`plugin/vision/`](plugin/vision/) (host runner, egress, templates, LLM `extract_text_from_image` via `domain=vision`); venv worker in [`plugin/vision/venv/`](plugin/vision/venv/); RPC in [`plugin/scripting/client.py`](plugin/scripting/client.py) `run_vision`; gating in [`vision_availability.py`](plugin/vision/vision_availability.py) — [docs/image-recognition.md](docs/image-recognition.md) |
| PPT-Master (Impress/Draw sidebar) | Adapter code [`plugin/contrib/ppt_master/`](plugin/contrib/ppt_master/) (upstream pin + symbol map in [`README.md`](plugin/contrib/ppt_master/README.md)); upstream via user skill tree; UNO in [`plugin/ppt_master/`](plugin/ppt_master/); session [`plugin/chatbot/ppt_master.py`](plugin/chatbot/ppt_master.py) — [integration plan + roadmap](docs/ppt-master-integration-plan.md#roadmap) |

**Layout:** `plugin/` → `framework/` (config, service, state, logging), `modules/` (ai, chatbot—including shared UNO dialogs/listeners/dialog_views/settings_dialog UI, writer, calc, draw, http), [`extension/`](extension/) (OXT resources, [`WriterAgentDialogs/`](extension/WriterAgentDialogs/), [`idl/`](extension/idl/), [`metadata/`](extension/metadata/)), [`scripts/`](scripts/), [`Makefile`](Makefile), [`pyproject.toml`](pyproject.toml).

---

## Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw (Impress paths where registered).

- **Chat:** Sidebar + menu chat (Writer/Calc deck; Draw per code paths)—multi-turn, tools, history (SQLite when available, else JSON under `writeragent_history.db.d/`).
- **Extend / Edit selection:** Writer uses `get_string_without_tracked_deletions()` for prompts; undo/session details in [`plugin/doc/document_helpers.py`](plugin/doc/document_helpers.py).
- **Settings:** `writeragent.json` under the LibreOffice user profile—see [`plugin/framework/config.py`](plugin/framework/config.py) module doc.
- **Memory (experimental):** [`plugin/chatbot/memory.py`](plugin/chatbot/memory.py); `MEMORY_GUIDANCE` in [`plugin/framework/constants.py`](plugin/framework/constants.py)—full notes [docs/hermes-agent-patterns.md](docs/hermes-agent-patterns.md).
- **Calc:** `=PROMPT()` — [`plugin/calc/prompt_addin.py`](plugin/calc/prompt_addin.py) / [`plugin/calc/prompt_function.py`](plugin/calc/prompt_function.py); `=PYTHON()` — [`plugin/calc/python/addin.py`](plugin/calc/python/addin.py) / [`plugin/calc/python/function.py`](plugin/calc/python/function.py).
- **Eval / benchmarks:** `make run_eval` / [`scripts/benchmark.py`](scripts/benchmark.py) → [`scripts/prompt_optimization/`](scripts/prompt_optimization/) (`eval_auth.py` for CLI credentials; judge via `LlmClient`). Setup: `uv sync`, `make eval-deps`. [`scripts/prompt_optimization/README.md`](scripts/prompt_optimization/README.md), [`docs/eval-dev-plan.md`](docs/eval-dev-plan.md).

**Python:** Dev/tooling **3.11–3.13** ([`pyproject.toml`](pyproject.toml)); dev `.venv` is pinned to **3.13** via [`.python-version`](.python-version) (3.14 lacks wheels for some dev deps such as spaCy). **Extension runtime** is whatever LibreOffice bundles (often older). **Shipped code under `plugin/` must not rely on stdlib newer than that runtime.**

**GPL v3+**; prior contributors credited in headers/installer.

---

## Build and quality commands

| Command | Role |
|---------|------|
| `make manifest` | Generates [`plugin/_manifest.py`](plugin/_manifest.py) (gitignored). Used by type-check and tests on clean checkouts. Missing manifest → [`load_manifest()`](plugin/framework/module_base.py) raises **`RuntimeError`**. |
| `make check` | **`ty`** only |
| `make build` | **`ty`** + **`ruff-fix`** then **`ruff`** + bundle |
| `make typecheck` | **`ty`** + **mypy** + **pyright** |
| `make test` | Full typecheck + **opengrep-lint** + pytest + LO tests + **bandit** (see `[tool.bandit]` in [`pyproject.toml`](pyproject.toml); skip if no `soffice`) |
| `make opengrep-lint` | Opengrep UNO thread + vendored security rules (ERROR; part of `make test`) |
| `make opengrep-rules-sync` | Refresh pinned third-party rules under `tests/semgrep/third_party/` |
| `make release` | **`make test`** then **`release-build`** (includes **`openrouter-catalog`** → [`extension/metadata/openrouter_models.json`](extension/metadata/openrouter_models.json)—not in OXT—plus [`default_models.py`](plugin/framework/default_models.py), translations, OXT) |
| `make ensure-uno` | Link system UNO into `.venv` if `import uno` fails (auto-run before typecheck/test) |
| `make fix-uno` | Same as **`ensure-uno`** with verbose output |

**Ruff:** `[tool.ruff]` line length **320** (Ruff’s maximum; fits dense one-line calls without wrapping); `[tool.ruff.format]` **`skip-magic-trailing-comma` true**—see [`pyproject.toml`](pyproject.toml). **`make build`** runs **`ruff-fix`** then **`ruff check`** (`ruff-for-build`); standalone **`make ruff`** is check-only. Not part of **`make test`**.

**Optional:** **`make pyrefly`** — not in **`make test`**; see [`docs/type-checking.md`](docs/type-checking.md).

Restart LibreOffice after **`make deploy`** (or use `make deploy writer/calc/draw/impress` to launch automatically).

---

## HTTP / LLM (summary)

All wire behavior is in [`plugin/framework/client/llm_client.py`](plugin/framework/client/llm_client.py) (see module doc and inline constants). Persistent connections: [`plugin/ai/service.py`](plugin/ai/service.py). Auth/headers: [`plugin/framework/client/auth.py`](plugin/framework/client/auth.py). Smolagents HTTP goes through [`WriterAgentSmolModel`](plugin/chatbot/smol_agent.py) only — [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md). Other quirks: [docs/llm-hacks.md](docs/llm-hacks.md).

---

## Cross-cutting invariants

- **`self.ctx`** for extension lookups (`PackageInformationProvider`, dialogs)—not `uno.getComponentContext()` when the component context differs.
- **FSM** ([`plugin/framework/service.py`](plugin/framework/service.py)): pure `next_state` only—no UNO/I/O inside transitions; effects live in panel/MCP.
- **Streaming:** worker → `queue.Queue`; first tuple element must be [`StreamQueueKind`](plugin/framework/async_stream.py) (**enum members**, not bare strings). Drain on main thread with **`toolkit.processEventsToIdle()`** via **`run_async_worker_with_drain`** / **`get_toolkit(ctx)`**. No UNO **`XTimerListener`** for sidebar streaming.
- **Smol HTTP:** [`WriterAgentSmolModel`](plugin/chatbot/smol_agent.py) is the **only** path that should call `LlmClient.request_with_tools` for vendored smolagents—do not add a parallel HTTP stack; use [`smol_agent.py`](plugin/chatbot/smol_agent.py) (includes adapter, model, and factory).
- **Document context (chat):** each send replaces the `[DOCUMENT CONTENT]` system message. **Calc** `get_calc_context_for_chat` requires **`ctx`** from panel/MainJob—never `uno.getComponentContext()` on that path.
- **Tool registry:** `uno_services` match first, then `doc_types`. **`get_schemas`** matches **`execute`**. Main-chat tools use **`tier="core"`** (default); nested specialized toolsets use **`specialized`** / **`specialized_control`** (omitted from default lists). Gateway tools must list **every** supported UNO service (e.g. draw delegation includes Draw + Impress). Writer [`charts.py`](plugin/writer/charts.py) / [`shapes.py`](plugin/writer/shapes.py) share tool **names** with Calc/Draw—the Writer class must declare the **union** of `uno_services` or execution rejects documents.
- **Threading:** [`run_in_background`](plugin/framework/worker_pool.py) instead of raw `threading.Thread`; long external processes → [`AsyncProcess`](plugin/framework/process_manager.py). Non-release bundles enable the Layer A UNO thread guard by default ([`thread_guard.py`](plugin/framework/thread_guard.py); opt out with `WRITERAGENT_UNO_THREAD_GUARD=0`; release OXT stubs it off). Guard document models at boundaries via [`guard_uno`](plugin/framework/thread_guard.py) (`get_active_document`, frame `_get_document_model`, `resolve_document_by_url`, `open_document_for_read`); use [`get_ctx()`](plugin/framework/uno_context.py) for `ToolContext`, not raw bootstrap `self.ctx`.
- **Errors:** `WriterAgentException` / **`format_error_payload`** ([`plugin/framework/errors.py`](plugin/framework/errors.py)); tools via `_tool_error`. Do not assume **`DocumentCache`**—it is not active.

UNO helpers are split: [`uno_context.py`](plugin/framework/uno_context.py), [`document_helpers.py`](plugin/doc/document_helpers.py), [`dialogs.py`](plugin/chatbot/dialogs.py)—no monolithic `uno_helpers.py`.

---

## Tips and sharp edges

Area-specific rules live in module docstrings and topic docs — open those when you edit the area. The [Deep dives](#deep-dives-link-index) table is the index.

- **Sidebar / chat / streaming:** Resolve document **frame-only** via `frame.getController().getModel()` ([`panel.py`](plugin/chatbot/panel.py)); stop/cancellation must use **`resolve_stop_checker()`**, not the panel flag alone. Prompt routing, modes, librarian, reasoning: [docs/chat-sidebar-implementation.md](docs/chat-sidebar-implementation.md). Streaming, stop scope, worker threads: [docs/streaming-and-threading.md](docs/streaming-and-threading.md).
- **Dialogs (XDL):** Load via `DialogProvider` + extension `base_url` — see module doc in [`plugin/chatbot/dialogs.py`](plugin/chatbot/dialogs.py). Settings UI: [`plugin/chatbot/dialog_views.py`](plugin/chatbot/dialog_views.py).
- **Tools / Writer / Calc:** Resolve tools in tests via `plugin.main.get_tools().get("tool_name")`. Math/HTML import: [docs/math-tex.md](docs/math-tex.md). Grammar pipeline: [docs/realtime-grammar-checker-plan.md](docs/realtime-grammar-checker-plan.md). Calc specialized: [docs/calc-specialized-toolsets.md](docs/calc-specialized-toolsets.md). Python venv sandbox: [docs/enabling_numpy_in_libreoffice.md](docs/enabling_numpy_in_libreoffice.md); domain helpers: [docs/numpy-domains.md](docs/numpy-domains.md).
- **Config:** `init_config(ctx)` once at bootstrap; all other I/O has no `ctx` — see module doc in [`plugin/framework/config.py`](plugin/framework/config.py).
- **Logging / MCP:** Logs in `writeragent_debug.log` beside `writeragent.json`. Default **`log_level`** is **WARN** in shipped LibrePy (no `plugin/tests/` in the OXT); dev checkouts with `plugin/tests/` default to **DEBUG**. Override with `"log_level": "DEBUG"` in `writeragent.json` and restart LibreOffice. **`enable_agent_log`** is separate (structured agent traces only). Use **`log.exception("Context")`** in unexpected `except` blocks. MCP drains on main thread — [docs/mcp-protocol.md](docs/mcp-protocol.md). Images: [docs/image-generation.md](docs/image-generation.md). No env API keys in production; no **`tempfile.mktemp()`**.
- **Tests / debug menus:** UNO tests via [`plugin/testing_runner.py`](plugin/testing_runner.py); debug menu suites run on the UI thread — [docs/test_architecture_analysis.md](docs/test_architecture_analysis.md).

### Global Python

Do not shadow **`logging`**, module **`log`**, or gettext **`_`**. UI modules import **`_`** from [`plugin/framework/i18n.py`](plugin/framework/i18n.py); do **not** bind bare **`_`** as a variable (`for _ in …`, `a, _, _ = fn()`, `except Exception as _:`). Use a named discard (`unused`, `idx`) instead. Private helpers named `_foo` are fine.

---

## Deep dives (link index)

| Topic | Doc |
|-------|-----|
| Chat sidebar implementation | [docs/chat-sidebar-implementation.md](docs/chat-sidebar-implementation.md) |
| Rich text control sidebar | [docs/rich-text-control-sidebar.md](docs/rich-text-control-sidebar.md) |
| Streaming / threading | [docs/streaming-and-threading.md](docs/streaming-and-threading.md) |
| Smol vs main chat HTTP | [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md) |
| Writer specialized tool tiers | [docs/writer-specialized-toolsets.md](docs/writer-specialized-toolsets.md) |
| Styles / LLM styling | [docs/llm-styles.md](docs/llm-styles.md) |
| Writer API references | [docs/bookmarks-api-reference.md](docs/bookmarks-api-reference.md), [docs/footnotes-api-reference.md](docs/footnotes-api-reference.md), [docs/page-api-reference.md](docs/page-api-reference.md), [docs/writer-tracking-api-reference.md](docs/writer-tracking-api-reference.md) |
| Reviewable agent edits (surgical redlines, toolbar) | [docs/reviewable-agent-edits.md](docs/reviewable-agent-edits.md) |
| LO-DOM & Semantic Tree | [docs/lo-dom-semantic-tree.md](docs/lo-dom-semantic-tree.md) |
| Draw/Impress specialized | [docs/draw-impress-specialized-toolsets.md](docs/draw-impress-specialized-toolsets.md), [docs/shape_support.md](docs/shape_support.md) |
| Calc specialized | [docs/calc-specialized-toolsets.md](docs/calc-specialized-toolsets.md) |
| Calc filters / formatting | [docs/calc-conditional-formatting.md](docs/calc-conditional-formatting.md), [docs/calc-sheet-filter.md](docs/calc-sheet-filter.md) |
| Embeddings / folder FTS | [docs/embeddings.md](docs/embeddings.md) |
| NumPy / Python venv bridge | [docs/enabling_numpy_in_libreoffice.md](docs/enabling_numpy_in_libreoffice.md), [docs/numpy-serialization.md](docs/numpy-serialization.md) |
| NumPy domain helpers (Viz, Symbolic, Units, Text, …) | [docs/numpy-domains.md](docs/numpy-domains.md) |
| Agent Search / Web | [docs/agent-search.md](docs/agent-search.md) |
| MCP protocol | [docs/mcp-protocol.md](docs/mcp-protocol.md) |
| Localization / translations / `make extract-strings` | [docs/localization.md](docs/localization.md), [locales/README.md](locales/README.md) |
| Audio Architecture | [docs/audio-architecture.md](docs/audio-architecture.md) |
| Image generation | [docs/image-generation.md](docs/image-generation.md) |
| Image recognition (local OCR / detection) | [docs/image-recognition.md](docs/image-recognition.md) — code under [`plugin/vision/`](plugin/vision/) |
| PPT-Master (Impress/Draw) | [docs/ppt-master-integration-plan.md](docs/ppt-master-integration-plan.md) (architecture + [roadmap](docs/ppt-master-integration-plan.md#roadmap)) — adapters [`plugin/contrib/ppt_master/`](plugin/contrib/ppt_master/); upstream from user skill tree |
| Math / HTML import design | [docs/math-tex.md](docs/math-tex.md) |
| Grammar pipeline (cache, queue) | [docs/realtime-grammar-checker-plan.md](docs/realtime-grammar-checker-plan.md) |
| Test Architecture | [docs/test_architecture_analysis.md](docs/test_architecture_analysis.md) |
| LLM Hacks & Workarounds | [docs/llm-hacks.md](docs/llm-hacks.md) |
| Experimental memory / roadmap | [docs/hermes-agent-patterns.md](docs/hermes-agent-patterns.md), [docs/ROADMAP.md](docs/ROADMAP.md), [docs/robustness-roadmap.md](docs/robustness-roadmap.md) |

---

## Static type checking (ty)

See [docs/type-checking.md](docs/type-checking.md) for checker scope, UNO patterns, and annotation fixes. **`make check`** → **`ty`** only; full matrix in [Build](#build-and-quality-commands).

---

## Debugging

- **`make deploy`** vs **`make repack`**: full rebuild/deploy vs re-zip only.
- New extension components: [`extension/META-INF/manifest.xml`](extension/META-INF/manifest.xml).
- Buffered logs: `/tmp` scratch + `flush=True` when needed.

---

## References

- Dialog DTD (LibreOffice tree): `xmlscript/dtd/dialog.dtd`
- GUI DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces