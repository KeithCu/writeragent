# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file lists **invariants**, **entry points**, and **easy mistakes**. Everything else is in the linked modules and docs—open those when you change behavior.

> [!IMPORTANT]
> **Docs:** After any nontrivial change, update documentation. Prefer the **topic doc** under `docs/`; touch **`AGENTS.md`** only when the change affects **many areas** or **global rules**.

> [!IMPORTANT]
> **Tests:** New features and bugfixes **must** include tests.
> - **Unit:** `plugin/tests/`, **pytest** when logic can be mocked.
> - **UNO / LibreOffice:** `plugin/tests/uno/` via **`testing_runner.py`** (no pytest)—use **`@native_test`**, **`@setup`**, **`@teardown`**; test functions take **`ctx`**.
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

**Layout:** `plugin/` → `framework/` (config, service, state, logging), `modules/` (ai, chatbot—including shared UNO dialogs/listeners/settings UI, writer, calc, draw, http), [`extension/`](extension/) (OXT resources, [`WriterAgentDialogs/`](extension/WriterAgentDialogs/), [`idl/`](extension/idl/), [`metadata/`](extension/metadata/)), [`scripts/`](scripts/), [`Makefile`](Makefile), [`pyproject.toml`](pyproject.toml).

---

## Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw (Impress paths where registered).

- **Chat:** Sidebar + menu chat (Writer/Calc deck; Draw per code paths)—multi-turn, tools, history (SQLite when available, else JSON under `writeragent_history.db.d/`).
- **Extend / Edit selection:** Writer uses `get_string_without_tracked_deletions()` for prompts; undo/session details in [`plugin/doc/document_helpers.py`](plugin/doc/document_helpers.py).
- **Settings:** `writeragent.json` under the LibreOffice user profile—see **Config** in [Tips](#tips-and-sharp-edges); keys and validation in [`plugin/framework/config.py`](plugin/framework/config.py).
- **Memory (experimental):** [`plugin/chatbot/memory.py`](plugin/chatbot/memory.py); `MEMORY_GUIDANCE` in [`plugin/framework/constants.py`](plugin/framework/constants.py)—full notes [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md).
- **Calc:** `=PROMPT()` — [`plugin/prompt_function.py`](plugin/prompt_function.py).
- **Eval / benchmarks:** [`scripts/prompt_optimization/README.md`](scripts/prompt_optimization/README.md), [`docs/eval-dev-plan.md`](docs/eval-dev-plan.md).

**Python:** Dev/tooling **3.11–3.14** ([`pyproject.toml`](pyproject.toml)); **extension runtime** is whatever LibreOffice bundles (often older). **Shipped code under `plugin/` must not rely on stdlib newer than that runtime.**

**GPL v3+**; prior contributors credited in headers/installer.

---

## Build and quality commands

| Command | Role |
|---------|------|
| `make manifest` | Generates [`plugin/_manifest.py`](plugin/_manifest.py) (gitignored). Used by type-check and tests on clean checkouts. Missing manifest → [`load_manifest()`](plugin/framework/module_base.py) raises **`RuntimeError`**. |
| `make check` | **`ty`** only |
| `make build` | **`ty`** + **`ruff`** + bundle |
| `make typecheck` | **`ty`** + **mypy** + **pyright** |
| `make test` | Full typecheck + **bandit** (see `[tool.bandit]` in [`pyproject.toml`](pyproject.toml)) + pytest + LO tests (skip if no `soffice`) |
| `make release` | **`make test`** then **`release-build`** (includes **`openrouter-catalog`** → [`extension/metadata/openrouter_models.json`](extension/metadata/openrouter_models.json)—not in OXT—plus [`default_models.py`](plugin/framework/default_models.py), translations, OXT) |
| `make fix-uno` | Link system UNO into `.venv` so `import uno` resolves for checkers |

**Ruff:** `[tool.ruff]` line length **320** (Ruff’s maximum; fits dense one-line calls without wrapping); `[tool.ruff.format]` **`skip-magic-trailing-comma` true**—see [`pyproject.toml`](pyproject.toml). **`make build`** runs **`ruff check`**; not part of **`make test`**.

**Optional:** **`make pyrefly`** — not in **`make test`**; see [`docs/type-checking.md`](docs/type-checking.md).

Restart LibreOffice after **`make deploy`**.

---

## HTTP / LLM (summary)

All wire behavior—dev/release system prefix, date prefix on first system message, leaked `<|…|>` token stripping, logging/redaction, **50ms minimum between sends** on an `LlmClient` instance, Anthropic/Gemini shims, local HTTPS retry—is implemented in [`plugin/framework/client/llm_client.py`](plugin/framework/client/llm_client.py). Read that file when changing requests.

Persistent connections: [`plugin/ai/service.py`](plugin/ai/service.py). Per-endpoint auth and headers: [`plugin/framework/client/auth.py`](plugin/framework/client/auth.py).

Fallback parsing when the API returns text without `tool_calls`: `get_parser_for_model` → [`plugin/contrib/tool_call_parsers/`](plugin/contrib/tool_call_parsers/). **Smolagents** post-processing goes through [`WriterAgentSmolModel`](plugin/chatbot/smol_agent.py) only (wire details in [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md)). Hermes: leading `/` messages on ACP skip `[DOCUMENT CONTENT]` wrapping; spawn `hermes acp`—use full path to `hermes` if `PATH` in LibreOffice is narrow.

---

## Cross-cutting invariants

- **`self.ctx`** for extension lookups (`PackageInformationProvider`, dialogs)—not `uno.getComponentContext()` when the component context differs.
- **FSM** ([`plugin/framework/service.py`](plugin/framework/service.py)): pure `next_state` only—no UNO/I/O inside transitions; effects live in panel/MCP.
- **Streaming:** worker → `queue.Queue`; first tuple element must be [`StreamQueueKind`](plugin/framework/async_stream.py) (**enum members**, not bare strings). Drain on main thread with **`toolkit.processEventsToIdle()`** via **`run_async_worker_with_drain`** / **`get_toolkit(ctx)`**. No UNO **`XTimerListener`** for sidebar streaming.
- **Smol HTTP:** [`WriterAgentSmolModel`](plugin/chatbot/smol_agent.py) is the **only** path that should call `LlmClient.request_with_tools` for vendored smolagents—do not add a parallel HTTP stack; use [`smol_agent.py`](plugin/chatbot/smol_agent.py) (includes adapter, model, and factory).
- **Document context (chat):** each send replaces the `[DOCUMENT CONTENT]` system message. **Calc** `get_calc_context_for_chat` requires **`ctx`** from panel/MainJob—never `uno.getComponentContext()` on that path.
- **Tool registry:** `uno_services` match first, then `doc_types`. **`get_schemas`** matches **`execute`**. Gateway tools must list **every** supported UNO service (e.g. draw delegation includes Draw + Impress). Writer [`charts.py`](plugin/writer/charts.py) / [`shapes.py`](plugin/writer/shapes.py) share tool **names** with Calc/Draw—the Writer class must declare the **union** of `uno_services` or execution rejects documents.
- **Threading:** [`run_in_background`](plugin/framework/worker_pool.py) instead of raw `threading.Thread`; long external processes → [`AsyncProcess`](plugin/framework/process_manager.py).
- **Errors:** `WriterAgentException` / **`format_error_payload`** ([`plugin/framework/errors.py`](plugin/framework/errors.py)); tools via `_tool_error`. Do not assume **`DocumentCache`**—it is not active.

UNO helpers are split: [`uno_context.py`](plugin/framework/uno_context.py), [`document_helpers.py`](plugin/doc/document_helpers.py), [`dialogs.py`](plugin/chatbot/dialogs.py)—no monolithic `uno_helpers.py`.

---

## Tips and sharp edges

### Sidebar / chat / streaming

- Resolve document with **`frame.getController().getModel()`** first (same window as sidebar), then desktop fallback—sidebar query focus breaks desktop-only resolution ([`SendButtonListener._get_document_model`](plugin/chatbot/panel.py), tests in [`plugin/tests/test_send_button_listener_document_model.py`](plugin/tests/test_send_button_listener_document_model.py)).
- **`setVisible(True)`** after `createContainerWindow()` for the panel.
- **Menu chat:** no tool-calling; same doc-detection idea as sidebar.
- **Stop** on main chat path: assistant may get `"No response."` for strict role alternation (e.g. Mistral); UI still shows stopped.
- **Reasoning:** [`plugin/main.py`](plugin/main.py) sends `reasoning: { effort: 'minimal' }`; UI shows `[Thinking] …` before the answer.
- **Web research / toggles:** in [`panel_factory.py`](plugin/chatbot/panel_factory.py), **never** `for _ in …` in path loops (**`_` shadows gettext**). Item listeners for research/direct image: **override `on_item_state_changed` on the class**, not nested in `__init__`, or toggles never fire ([`BaseItemListener`](plugin/chatbot/listeners.py)).
- **Librarian mode:** starts when `USER.md` is empty; [`SendButtonListener`](plugin/chatbot/panel.py) keeps `_in_librarian_mode` until [`send_handlers.py`](plugin/chatbot/send_handlers.py) sees `switch_mode` / `switch_to_document_mode`. **`USER.md`** is storage only—not the handoff signal alone.
- **`upsert_memory` visibility:** main chat via [`tool_loop_state.py`](plugin/chatbot/tool_loop_state.py); librarian uses [`librarian.py`](plugin/chatbot/librarian.py) + `chat_append_callback` so updates show even when search-thinking is off.

### Dialogs (XDL)

- **`DialogProvider`**: package **`base_url` + XDL path** only—never `vnd.sun.star.script:…?location=application` with sidebar components (**deadlock**).
- Load: `DialogProvider.createDialog(base_url + "/WriterAgentDialogs/…")`; `base_url` from **`PackageInformationProvider` + `self.ctx`**.
- Multi-page: **`dlg:page`** on controls + `dlg.getModel().Step`; **not** `tabpagecontainer` / `tabpage` (silent failure).
- **AppFont** for geometry; explicit layout—no flex. **TabListener** must subclass **`unohelper.Base`** + **`XActionListener`**—see pattern in [`plugin/chatbot/dialogs.py`](plugin/chatbot/dialogs.py).
- **ListBox/ComboBox:** set **`StringItemList`**, not only `.Text`.
- **`translate_dialog`:** [`dialogs.py`](plugin/chatbot/dialogs.py). Chat sidebar does **not** re-translate on every `config:changed`—only at wiring/load.
- **`legacy_ui`:** do not pass saved config through gettext (empty string → PO garbage). **`_(msg)`** requires `str` ([`plugin/framework/i18n.py`](plugin/framework/i18n.py)).
- **`legacy_ui.input_box`:** if `execute()` is false (ESC/close), **do not** `dispose()` the dialog again—**double dispose can segfault** LibreOffice.

### Tools / Writer / Calc

- **Specialized tools in tests:** `plugin.main.get_tools().get("tool_name")`—not fragile internal imports; see [`tool.py`](plugin/framework/tool.py).
- **In-place specialized mode:** `USE_SUB_AGENT` / `active_domain` / [`ToolCallingMixin._refresh_active_tools_for_session`](plugin/chatbot/tool_loop.py)—[`plugin/framework/constants.py`](plugin/framework/constants.py).
- **HTML / content:** [`format_support.py`](plugin/writer/format_support.py)—prefer **plain-text** `apply_document_content` to preserve character formatting; **`safe_json_loads`** repair/LaTeX clash recovery in [`plugin/framework/errors.py`](plugin/framework/errors.py). Math segments: [`html_math_segment.py`](plugin/writer/html_math_segment.py), [`math_formula_insert.py`](plugin/writer/math_formula_insert.py).
- **Outline API:** `get_document_tree`, `get_heading_children` in [`outline.py`](plugin/writer/outline.py)—legacy names like `get_document_outline` are obsolete.
- **Grammar proofreader:** [`plugin/writer/locale/ai_grammar_proofreader.py`](plugin/writer/locale/ai_grammar_proofreader.py), [`grammar_proofread_locale.py`](plugin/writer/locale/grammar_proofread_locale.py) (`GRAMMAR_REGISTRY_LOCALE_TAGS`, UNO `Locale` bridging, Unicode terminals, abbrev/Thai chunking, `looks_complete_sentence`, worker caps/prompt, `parse_grammar_json`), [`grammar_proofread_text.py`](plugin/writer/locale/grammar_proofread_text.py) (BreakIterator split, offsets, sentence scheduling), [`grammar_proofread_cache.py`](plugin/writer/locale/grammar_proofread_cache.py) (LRU), [`grammar_work_queue.py`](plugin/writer/locale/grammar_work_queue.py) (`GrammarWorkItem`, batch dedup, enqueue supersede / stale helpers, sequential LLM worker + queue). Service **`__init__(self, ctx, *args)`** required—LibreOffice uses `createInstanceWithArgumentsAndContext`. Keep top-level imports minimal. XCU/locale parity: [`grammar_proofread_locale.py`](plugin/writer/locale/grammar_proofread_locale.py), [`plugin/tests/writer/locale/test_grammar_linguistic_xcu.py`](plugin/tests/writer/locale/test_grammar_linguistic_xcu.py). Queue/cache semantics: [`docs/realtime-grammar-checker-plan.md`](docs/realtime-grammar-checker-plan.md).
- **Calc JSON schemas (Gemini/OpenRouter):** no union types—use **`"type": "array"` + `items`**; normalize a single string to a one-element list in execute.
- **Calc specialized** (pivot, conditional formatting, filters, forms, …): [`docs/calc-specialized-toolsets.md`](docs/calc-specialized-toolsets.md)—future pivot ideas also at top of [`plugin/calc/pivot.py`](plugin/calc/pivot.py).

### Config

- Paths: Linux `~/.config/libreoffice/{4,24}/user/writeragent.json`; macOS `~/Library/Application Support/LibreOffice/4/user/`; Windows `%APPDATA%\LibreOffice\4\user\`.
- **`set_config`:** skips write and `config:changed` when unchanged. Unknown keys via `get_config` / `get_config_int` → **`CONFIG_KEY_NOT_FOUND`** with `details["key"]`.
- **OpenRouter merge:** optional `openrouter_chat_extra` — [`merge_openrouter_chat_extra`](plugin/framework/client/llm_client.py); blocked keys include `messages`, `tools`, `tool_choice`, `stream`.
- **Settings UI:** **`core`** must stay skipped in auto-generated tabs ([`manifest_registry.py`](scripts/manifest_registry.py) + [`legacy_ui.py`](plugin/chatbot/legacy_ui.py) agree) or Settings crashes (`btn_tab_core`).
- Defaults and provider tables: [`plugin/framework/default_models.py`](plugin/framework/default_models.py). **`chat_max_tool_rounds`:** empty string → fallback 25 with debug log.
- **Chat-related keys:** `chat_context_length`, `chat_max_tokens`, `additional_instructions` (see [`plugin/framework/config.py`](plugin/framework/config.py), [`plugin/framework/constants.py`](plugin/framework/constants.py)).

### Logging / MCP / misc

- Logs: same directory as `writeragent.json` (else `~/writeragent_debug.log`). **`redact_sensitive_payload_for_log`** on HTTP debug ([`plugin/framework/logging.py`](plugin/framework/logging.py)).
- **MCP:** HTTP threads → main-thread [`drain_mcp_queue`](plugin/mcp/mcp_protocol.py); **`X-Document-URL`** for targeting—[`document_helpers.py`](plugin/doc/document_helpers.py). Start/stop from [`plugin/main.py`](plugin/main.py) bootstrap / [`McpModule`](plugin/mcp/__init__.py)—localhost, no auth.
- **Images:** endpoint uses **`get_image_model`** (not chat model); [`image_utils.py`](plugin/writer/image_utils.py), [`image_tools.py`](plugin/writer/image_tools.py); [docs/image-generation.md](docs/image-generation.md).
- **Outline / navigation helpers:** ignore stale **DocumentCache** mentions in comments—cache class is not active.
- **Settings ↔ XDL:** `MainJob._get_settings_field_specs()` in [`plugin/main.py`](plugin/main.py) must match control names.
- **`WriterAgentDialogs`** folder name matches `dialog.xlb` library name.
- **`is_writer(model)`** — Writer has draw pages; do not use **`getDrawPages`** alone as the Writer test.
- **No env API keys** in production; no **`tempfile.mktemp()`**.
- **Python:** do not shadow **`logging`** or module **`log`** inside functions.

### Tests and debug menus

- **`$(LO_PYTHON) -m plugin.testing_runner`:** [`plugin/testing_runner.py`](plugin/testing_runner.py) snapshots [`NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS`](plugin/tests/testing_utils.py) between UNO modules. Real PyUNO loaded → **`setup_uno_mocks()`** must not replace **`uno`** with **`MagicMock`**.
- **Debug menu suites** ([`plugin/main.py`](plugin/main.py) `_run_test_suite`): run **`run_module_suite` on the UI thread**—do not wrap in **`run_blocking_in_thread`** (UNO tools need main thread).

---

## Deep dives (link index)

| Topic | Doc |
|-------|-----|
| Chat sidebar implementation | [docs/chat-sidebar-implementation.md](docs/chat-sidebar-implementation.md) |
| Streaming / threading | [docs/streaming-and-threading.md](docs/streaming-and-threading.md) |
| Smol vs main chat HTTP | [docs/smol-main-chat-tool-architecture.md](docs/smol-main-chat-tool-architecture.md) |
| Writer specialized tool tiers | [docs/writer-specialized-toolsets.md](docs/writer-specialized-toolsets.md) |
| Styles / LLM styling | [docs/llm-styles.md](docs/llm-styles.md) |
| Writer API references | [docs/bookmarks-api-reference.md](docs/bookmarks-api-reference.md), [docs/footnotes-api-reference.md](docs/footnotes-api-reference.md), [docs/page-api-reference.md](docs/page-api-reference.md), [docs/writer-tracking-api-reference.md](docs/writer-tracking-api-reference.md) |
| LO-DOM & Semantic Tree | [docs/lo-dom-semantic-tree.md](docs/lo-dom-semantic-tree.md) |
| Draw/Impress specialized | [docs/draw-impress-specialized-toolsets.md](docs/draw-impress-specialized-toolsets.md), [docs/shape_support.md](docs/shape_support.md) |
| Calc specialized | [docs/calc-specialized-toolsets.md](docs/calc-specialized-toolsets.md) |
| Calc filters / formatting | [docs/calc-conditional-formatting.md](docs/calc-conditional-formatting.md), [docs/calc-sheet-filter.md](docs/calc-sheet-filter.md) |
| Agent Search / Web | [docs/agent-search.md](docs/agent-search.md) |
| MCP protocol | [docs/mcp-protocol.md](docs/mcp-protocol.md) |
| Localization / translations / `make extract-strings` | [docs/localization.md](docs/localization.md), [locales/README.md](locales/README.md) |
| Audio Architecture | [docs/audio-architecture.md](docs/audio-architecture.md) |
| Image generation | [docs/image-generation.md](docs/image-generation.md) |
| Math / HTML import design | [docs/libreoffice-html-math-dev-plan.md](docs/libreoffice-html-math-dev-plan.md), [docs/math-extraction-editing-dev-plan.md](docs/math-extraction-editing-dev-plan.md) |
| Grammar pipeline (cache, queue) | [docs/realtime-grammar-checker-plan.md](docs/realtime-grammar-checker-plan.md) |
| Test Architecture | [docs/test_architecture_analysis.md](docs/test_architecture_analysis.md) |
| LLM Hacks & Workarounds | [docs/llm-hacks.md](docs/llm-hacks.md) |
| Experimental memory / roadmap | [docs/agent-memory-and-skills.md](docs/agent-memory-and-skills.md), [docs/ROADMAP.md](docs/ROADMAP.md), [docs/robustness-roadmap.md](docs/robustness-roadmap.md) |

---

## Static type checking (ty)

Primary workflows and checker scope: [`docs/type-checking.md`](docs/type-checking.md). **`make check`** → **`ty`** only; **`make build`** → **`ty`** + **`ruff`**; **`make typecheck`** → **`ty`** + **mypy** + **pyright**; **`make test`** adds **bandit** then pytest (see [Build](#build-and-quality-commands)). **`types-unopy`** (dev); **`make fix-uno`** links UNO into `.venv`.

**Common fixes:** `Protocol` for mixin hosts; `TYPE_CHECKING` + **`ruff`** `TC` rules for imports used only in hints; `cast(Any, …)` / `cast(Iterable, …)` where stubs are thin; explicit `None` checks. **UNO interface overrides:** match stub parameter names exactly (e.g. `actionPerformed(self, rEvent)`) or **`ty`/pyright** report `invalid-method-override`.

---

## Debugging

- **`make deploy`** vs **`make repack`**: full rebuild/deploy vs re-zip only.
- New extension components: [`extension/META-INF/manifest.xml`](extension/META-INF/manifest.xml).
- Buffered logs: `/tmp` scratch + `flush=True` when needed.

---

## References

- Dialog DTD (LibreOffice tree): `xmlscript/dtd/dialog.dtd`
- GUI DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces