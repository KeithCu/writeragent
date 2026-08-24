# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file lists **invariants** and **easy mistakes**. Everything else is in the linked modules and docs—open those when you change behavior. Entry points and topic hubs: [`docs/repo-map.md`](docs/repo-map.md).

> [!IMPORTANT]
> **Docs:** After any nontrivial change, update documentation. Prefer the **topic doc** under `docs/`; touch **`AGENTS.md`** only when the change affects **many areas** or **global rules**.
> [!IMPORTANT]
> **Complexity:** This codebase is complicated for its size. When asked to do a new feature, always figure out the way using the least amount of code or extra complexity. Using existing functions, there are many functions which can just be used or refactored to make the change small for a new feature.

If you find ways to lower technical debt, while adding a feature, put that in your plan.

> [!IMPORTANT]
> **Tests:** New features and bugfixes **must** include tests.
> - **Unit:** `tests/`, **pytest** when logic can be mocked. Test files should match the source module name (e.g. `foo.py` -> `test_foo.py`). **Always add new test cases to the matching `test_` file to maintain consistent naming and visible coverage.**
> - **UNO / LibreOffice:** `tests/uno/` or `_uno.py` suffix via **`testing_runner.py`** (no pytest)—use **`@native_test`**, **`@setup`**, **`@teardown`**; test functions take **`ctx`**. **Follow the same module-matching rule (e.g. `foo.py` -> `test_foo_uno.py`).**
> - **Execution Policy:** Do **not** run tests or **`make typecheck`** before starting work unless you need output from a test to understand a failure. Assume the tree and tests are already green. After edits, run tests for the files you changed plus **`make typecheck`**. Typecheck takes about **one minute** — wait for it; do not poll every few seconds. Run full **`make test`** ONLY IF making large refactors or cross-cutting changes.

> [!TIP]
> **When unsure of LibreOffice / UNO API behavior, inspect it directly!**
> Do not guess, add layers of speculative fallback code, or flail. You have a full running LibreOffice instance and a fast native test runner:
> ```bash
> .venv/bin/python plugin/testing_runner.py tests/chatbot/test_hamburger_menu_uno.py
> ```
> See `tests/chatbot/test_hamburger_menu_uno.py` as an example: write a quick `@native_test` function (takes `ctx`), instantiate the UNO service, and inspect `dir(obj)` or test the exact call directly. In seconds you get the real runtime methods, argument counts, and types.

> [!IMPORTANT]
> **Comments:** Write why this code is there for the reader who would otherwise be **lost**. **Good comments are the bridge** from opaque to understandable and maintainable code. Some files have no comments: inserting footnotes is standard, little different from other UNO objects. Meanwhile some comments are critical to understanding why this code is there. Write clear, short comments.
> - **Bugfixes (required):** at the fix, **what was wrong**, **how it happened**, and **why this change** fixes it.
> - **LibreOffice / UNO / Etc.:** quirks. When matching upstream behavior, cite **source** (file + line or function), not a vague “like Lightproof.”

---

## Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw (Impress paths where registered).

- **Chat:** Sidebar + menu chat (Writer/Calc deck; Draw per code paths)—multi-turn, tools, history (SQLite when available, else JSON under `writeragent_history.db.d/`).
- **Extend / Edit selection:** Writer uses `get_string_without_tracked_deletions()` in `text_helpers` for prompts; undo/session details in `plugin/writer/edit_review.py`.
- **Settings:** `writeragent.json` under the LibreOffice user profile—see `config` module doc.
- **Memory (experimental):** `memory` + `MEMORY_GUIDANCE` in `prompts` — [docs/archive/hermes-agent-patterns.md](docs/archive/hermes-agent-patterns.md).
- **Calc:** `=PROMPT()` and `=PYTHON()` add-ins (see [`docs/repo-map.md`](docs/repo-map.md)).
- **Eval / benchmarks:** `make run_eval` / `scripts/benchmark.py` → `scripts/prompt_optimization/` — [scripts/prompt_optimization/README.md](scripts/prompt_optimization/README.md), [docs/eval-dev-plan.md](docs/eval-dev-plan.md).

**Python:** Dev/tooling **3.11–3.13** (`pyproject.toml`); dev `.venv` is pinned to **3.13** via `.python-version` (3.14 lacks wheels for some dev deps such as spaCy). **Extension runtime** is whatever LibreOffice bundles (often older). **Shipped code under `plugin/` must not rely on stdlib newer than that runtime.**

**GPL v3+**; prior contributors credited in headers/installer.

---

## Essential commands

| Command | When to use |
|---------|-------------|
| `make typecheck` | After edits (required with targeted tests). Checker details: [docs/framework-type-checking.md](docs/framework-type-checking.md) |
| `make deploy` | WriterAgent OXT: build + install/cache sync; **restart LibreOffice** (or `make deploy writer/calc/draw/impress` to launch) |
| `make deploy-core` | LibrePy OXT only (`build/LibrePy.oxt`); **removes WriterAgent**. Install one OXT at a time. |
| `make test` | Large or cross-cutting changes only (includes typecheck, SAST, pytest, LO tests) |
| `make build` | Produce `build/WriterAgent.oxt` only (no install) |
| `make build-core` | Produce `build/LibrePy.oxt` only (no install) |
| `make release` | Typecheck + bandit, verify a stripped tree in `/tmp` (`compileall` + pytest + LO tests), then build/register `build/WriterAgent.oxt` |

Usual targets generate `plugin/_manifest.py` when needed. Other Makefile targets exist for fuzz and niche tooling—see the `Makefile` when you need them.

---

## HTTP / LLM (summary)

Chat and tool calls go through `llm_client` (see its module doc). Persistent connections live in the HTTP client; auth headers in `auth`.

The librarian / smolagents path must use `WriterAgentSmolModel` in `smol_agent`—do not add a second HTTP client. Details: [docs/chat-smol-tool-architecture.md](docs/chat-smol-tool-architecture.md), [docs/chat-llm-hacks.md](docs/chat-llm-hacks.md).

---

## Cross-cutting invariants

Rules that apply in many places. Breaking them causes wrong-document bugs, frozen UI, or tools that never run. Paths: [`docs/repo-map.md`](docs/repo-map.md).

- **Use the extension’s `self.ctx`, not a fresh UNO context.** Lookups for package info, dialogs, and similar must use the component context the extension was given. Calling `uno.getComponentContext()` can return a different context and quietly break those lookups. Same idea for Calc chat context: `get_calc_context_for_chat` needs `ctx` from the panel / MainJob, not a bootstrap call.

- **Keep the chat FSM pure.** In `service`, `next_state` only computes the next state—no UNO calls and no I/O. Side effects (UI updates, MCP, document work) belong in the panel or MCP layers.

- **Stream on a worker; drain on the UI thread.** Background work pushes tuples onto a `queue.Queue`. The first element must be a `StreamQueueKind` **enum member**, not a bare string. Drain with `run_async_worker_with_drain` / `get_toolkit(ctx)` so the UI processes events via `toolkit.processEventsToIdle()`. Do not use UNO `XTimerListener` for sidebar streaming. More: [docs/framework-streaming-and-threading.md](docs/framework-streaming-and-threading.md).

- **Refresh document context each chat send.** Each user send replaces the `[DOCUMENT CONTENT]` system message so the model sees the current document, not a stale snapshot.

- **Register tools so schemas and execution agree.** Matching uses `uno_services` first, then `doc_types`. Anything advertised by `get_schemas` must be runnable via `execute`. Default main-chat tools are `tier="core"`; nested specialized sets use `specialized` / `specialized_control` and are omitted from default lists. Gateway tools must list **every** UNO service they support (e.g. Draw **and** Impress). Writer `charts` / `shapes` share tool **names** with Calc/Draw—the Writer class must declare the **union** of those services or execution rejects the document.

- **Do not start raw threads for background work.** Use `run_in_background`. Short fire-and-forget jobs share a bounded daemon pool; pass `dedicated=True` (or `daemon=False`) for servers, pipe drains, infinite loops, and any job another thread will `join()`. Long subprocesses use `AsyncProcess`; if stderr is piped, drain it continuously or redirect it, or the process can deadlock ([docs/framework-threading.md](docs/framework-threading.md)). Dev builds enable a UNO thread guard by default (`thread_guard`; set `WRITERAGENT_UNO_THREAD_GUARD=0` to opt out; release OXTs stub it off). Wrap document-model access at boundaries with `guard_uno` (e.g. `get_active_document`, frame `_get_document_model`, `resolve_document_by_url`, `open_document_for_read`). For `ToolContext`, use `get_ctx()`—not the raw bootstrap `self.ctx`. Details: [docs/framework-uno-thread-safety.md](docs/framework-uno-thread-safety.md).

- **Surface errors through the shared helpers.** Prefer `WriterAgentException` and `format_error_payload` (`errors`). Tools should fail via `_tool_error`. There is no active `DocumentCache`—do not assume one.

- **Two products, one OXT at a time.** WriterAgent (`make deploy`, `plugin/main.py`) vs LibrePy (`make build-core` / `deploy-core`, `plugin/main_core.py`, `extension-core/`). `deploy-core` removes WriterAgent. Dual-install overlay is **not shipped**. File list: [`scripts/librepy_bundle_paths.py`](scripts/librepy_bundle_paths.py). Packaging: [docs/scripting-librepy-split.md](docs/scripting-librepy-split.md).

- **LibrePy-safe document helpers.** Linebreaks, tracked-deletion reads, heading trees, path, selection text, Writer text slices, and selection range / char count: `plugin/doc/text_helpers.py`. Type guards: `doc_type.py`. Document properties: `udprops.py`. Do **not** import `document_helpers` from LibrePy paths (WriterAgent chat context / `DocumentService`). Do **not** re-export the light helpers from `document_helpers`.

- **`plugin.framework.client` package init is lazy.** HTTP / errors / provider detection load immediately; `LlmClient`, embeddings, and analysis load on attribute access. LibrePy may import `requests` / `provider_detection`. Do not import `llm_client` or embeddings from LibrePy paths.

UNO helpers are intentionally split (`uno_context`, `text_helpers` / `doc_type` / `udprops`, `document_helpers` for chat context / `DocumentService`, `dialogs`)—there is no monolithic `uno_helpers.py`.

---

## Tips and sharp edges

Area-specific rules live in module docstrings, topic docs, and the
area `AGENTS.md` next to the code. Hermes injects those area files
when you open that tree; other agents should open them explicitly.

- **When editing `plugin/chatbot/`:** read [`plugin/chatbot/AGENTS.md`](plugin/chatbot/AGENTS.md)
- **When editing `plugin/writer/`:** read [`plugin/writer/AGENTS.md`](plugin/writer/AGENTS.md)
- **When editing `plugin/calc/`:** read [`plugin/calc/AGENTS.md`](plugin/calc/AGENTS.md)
- **When editing `plugin/scripting/` or LibrePy:** read [`plugin/scripting/AGENTS.md`](plugin/scripting/AGENTS.md)

**Config:** Call `init_config(ctx)` once at bootstrap. Later config I/O does not take `ctx` — see the `config` module doc.

**Logging / MCP:** Logs go to `writeragent_debug.log` next to `writeragent.json`. `enable_agent_log` is separate (structured agent traces only). In unexpected `except` blocks, use **`log.exception("Context")`**. MCP work drains on the main thread ([docs/mcp-protocol.md](docs/mcp-protocol.md)). Do not read API keys from the environment in production; do not use **`tempfile.mktemp()`**. For scratch debug files under `/tmp`, prefer `flush=True`.

**Tests / packaging:** UNO tests go through `testing_runner`; debug-menu suites run on the UI thread ([docs/archive/test_architecture_analysis.md](docs/archive/test_architecture_analysis.md)). New extension components must be registered in `extension/META-INF/manifest.xml`.

### Global Python

Do not reuse the names **`logging`**, module **`log`**, or gettext **`_`** for unrelated variables. UI code imports **`_`** from `i18n`. Never bind bare `_` as a throwaway (`for _ in …`, `a, _, _ = fn()`, `except Exception as _:`)—use a real name (`unused`, `idx`). Private helpers named `_foo` are fine.

### Do not redo (already shipped)

- Do **not** invent `python_config.py` or rename `writeragent.json` for LibrePy.
- Do **not** split `payload_codec.py` flatten/unpack without serialization A/B tests ([docs/scripting-numpy-serialization.md](docs/scripting-numpy-serialization.md)).
- Envelope-detector `@deal` + Hypothesis oracles on `payload_codec` (`is_split_grid`, `is_multi_data`, image / dataframe / calc_range) are **shipped**. Source of truth: [docs/scripting-serialization-verification.md](docs/scripting-serialization-verification.md).
- Scripting domain registries (Phases 1–6) are shipped — do not add a fourth ad-hoc registry ([docs/archive/scripting-domain-debt-dev-plan.md](docs/archive/scripting-domain-debt-dev-plan.md)).
- `calc_functions_*.py` alphabet splits are intentional; do not merge them.
- Do **not** drop `plugin/calc/analyzer.py` from the LibrePy bundle (reserved for later use).
- Do **not** slim `trusted_action_registry.py` / `venv_diagnostics.py` for LibrePy while those modules still work.

---

## Where to look

**Layout:** `plugin/` (framework, chatbot, writer, calc, draw, scripting, librepy, …), `extension/` (WriterAgent OXT), `extension-core/` (LibrePy OXT), `scripts/`, `Makefile`, `pyproject.toml`.

Full entry-point table and topic-hub catalog: [`docs/repo-map.md`](docs/repo-map.md).

Hubs (open the topic doc; do not grow this list here):

| Need | Start |
|------|--------|
| Bootstrap / MCP | `plugin/main.py` |
| LibrePy bootstrap | `plugin/main_core.py` |
| Sidebar / send | `plugin/chatbot/panel.py` |
| Tool loop / FSM | `plugin/chatbot/tool_loop.py` |
| HTTP / LLM | `plugin/framework/client/llm_client.py` |
| UNO thread guard | `plugin/framework/thread_guard.py` |
| Light vs chat document helpers | `plugin/doc/text_helpers.py` / `document_helpers.py` |
| Native UNO tests | `plugin/testing_runner.py` |

Why this file is short: Hermes truncates each context file at 20,000
characters (head+tail). Keep **invariants** here; keep **catalogs**
in `docs/repo-map.md`.

## References

- Dialog DTD (LibreOffice tree): `xmlscript/dtd/dialog.dtd`
- GUI DevGuide: https://wiki.documentfoundation.org/Documentation/DevGuide/Graphical_User_Interfaces
