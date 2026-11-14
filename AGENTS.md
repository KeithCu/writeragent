# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file lists **invariants** and **easy mistakes**. Everything else is in the linked modules and docs—open those when you change behavior. Entry points and topic hubs: [`docs/repo-map.md`](docs/repo-map.md).

**Translations:** WriterAgent extracts and translates strings itself (`make extract-strings` and the translation workflow). Mark new user-facing strings with the existing gettext wrappers. Agents and subprocesses **must not translate UI strings**, fill `msgstr` values, or edit `.po`/`.pot` catalogs. Do not spend tokens on translations.

**Docs:** After any nontrivial change, **update documentation**. Prefer the **topic doc** under `docs/`; touch `AGENTS.md` only when the change affects many areas or global rules.

**Complexity:** This codebase is complicated for its size. When asked to do a new feature, always figure out the way **using the least amount of code** or extra complexity. Using existing functions, there are many functions which can just be used or refactored to make the change small for a new feature. If you find ways to lower technical debt, while adding a feature, put that in your plan.

## Tests

New features and bugfixes **must include tests**.
- **No change-detector tests:** Do not assert on prompt or tool-description prose, log text, static config or catalog constants, removed-feature lists, signatures/exports already enforced by type checkers, or exact mock call order/choreography, unless the test cites a real bug. Keep structural guards (section order, uniqueness, routing) and tests that cite a real bug.
- **Unit:** `tests/`, pytest (`make pytest`) when logic can be mocked. Test files should match the source module name (e.g. `foo.py` -> `test_foo.py`). **Always add new test cases to the matching `test_` file** to maintain consistent naming and visible coverage.
- **UNO / LibreOffice:** collocated `*_uno.py` files via `testing_runner.py` (`make test-uno`, no pytest)—use `@native_test`, `@setup`, `@teardown`; test functions take `ctx`. Follow the same module-matching rule (e.g. `foo.py` -> `test_foo_uno.py`).
- **Enumerations:** All `XEnumeration` walks (portions, paragraphs, annotations, desktop components) must use `enum_has_more` (`plugin/framework/uno_context.py`) because `not enum.hasMoreElements()` does not terminate on mock objects.
- **Execution Policy:** **Do not run tests or `make typecheck` before starting work** unless you need output from a test to understand a failure. Assume the tree and tests are already green. After edits, run `make test-bot` and `make typecheck` before each commit (incremental on staged, unstaged, and untracked Python files — usually seconds; skips `thread-safety-lint`, and incremental `basedpyright`/`mypy` do not catch errors in unchanged files broken by a signature change — `make typecheck-full` / CI catches those). `make typecheck-full` takes about **one minute** — wait for it; do not poll every few seconds. Run `make test-full` **only if making large refactors** or cross-cutting changes.

When unsure of LibreOffice / UNO API behavior, **inspect it directly**! Do not guess, add layers of speculative fallback code, or flail. You have a full running LibreOffice instance and a fast native test runner:
```bash
.venv/bin/python plugin/testing_runner.py tests/chatbot/test_hamburger_menu_uno.py
```
See `tests/chatbot/test_hamburger_menu_uno.py` as an example: write a quick `@native_test` function (takes `ctx`), instantiate the UNO service, and inspect `dir(obj)` or test the exact call directly. In seconds you get the real runtime methods, argument counts, and types.

## Comments

Explain the code that is here **now**, for a reader who would otherwise be lost. They are the bridge from opaque to understandable and maintainable code. Assume the reader is not a coding expert and does not already know this project. Write clear comments with enough detail to follow—two or three short sentences is often right; do not use cryptic one-liners.
- **Do not write “what went wrong” boilerplate** or bug-history narration: no “this used to…”, “previously…”, “the bug was…”, “Bugfix:”, how the old code failed, or a postmortem of the fix.
- It is fine (and often useful) to say in present tense what would go wrong for the user or the program if this code were not here—that explains the current invariant, not the war story of the bug.
- Some files have almost no comments; adding footnotes where a reader would be lost is normal.
- **LibreOffice / UNO / Etc. quirks:** When matching upstream behavior, **cite source** (file + line or function), not a vague “like Lightproof.”

---

## Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw (Impress paths where registered).

**Python:** Dev/tooling **3.11–3.13** (`pyproject.toml`); dev `.venv` is pinned to **3.13** via `.python-version` (3.14 lacks wheels for some dev deps such as spaCy). **Extension runtime** is whatever LibreOffice bundles (often older). **Shipped code under `plugin/` must not rely on stdlib newer than that runtime.**

**GPL v3+**; prior contributors credited in headers/installer.

---

## Essential commands

| Command | When to use |
|---------|-------------|
| `make typecheck` | Run before each commit (required with targeted tests). Incremental basedpyright, bandit, opengrep, pyspector, and mypy on staged, unstaged, and untracked Python files plus full `ty` in parallel (skips `thread-safety-lint`; `CI=true` or `TYPECHECK_FULL=1` runs `typecheck-full`). Details: [docs/framework/type-checking.md](docs/framework/type-checking.md) |
| `make typecheck-full` | Full-repo checkers including `basedpyright` library-source walk and `thread-safety-lint` (used by CI and `make release`; standalone `make thread-safety-lint` also available). |
| `make deploy` | WriterAgent OXT: build + install/cache sync; **restart LibreOffice** (or `make deploy writer/calc/draw/impress` to launch) |
| `make deploy-core` | LibrePy OXT only (`build/LibrePy.oxt`); **removes WriterAgent**. |
| `make test-bot` | Incremental pytest only on changed files/modules (run with `make typecheck` before each commit). |
| `make pytest` | Full unit pytest only (`-m "not slow and not integration" --ignore-glob='*_uno.py'` plus xdist `-n auto --dist=loadgroup`; `PYTEST_WORKERS=0` for serial). No live soffice. |
| `make test-uno` | UNO / LibreOffice tests only: runs `testing_runner.py` with serial live LibreOffice instance. |
| `make test` | Incremental typecheck + incremental pytest + LO tests. |
| `make test-full` | Large or cross-cutting changes only (includes typecheck, SAST, full pytest, LO tests, excel-py-roundtrip) |
| `make build` | Produce `build/WriterAgent.oxt` only (no install) |
| `make build-core` | Produce `build/LibrePy.oxt` only (no install) |
| `make release` | Typecheck + bandit, verify a stripped tree in `/tmp` (`compileall` + pytest + LO tests), then build/register `build/WriterAgent.oxt` |

Usual targets generate `plugin/_manifest.py` when needed. Other Makefile targets exist for fuzz and niche tooling—see the `Makefile` when you need them.

---

## HTTP / LLM (summary)

Chat and tool calls go through `llm_client` (see its module doc). Persistent connections live in the HTTP client; auth headers in `auth`.

The librarian / smolagents path must use `WriterAgentSmolModel` in `smol_agent`—do not add a second HTTP client. Details: [docs/chat/smol-tool-architecture.md](docs/chat/smol-tool-architecture.md), [docs/chat/llm-hacks.md](docs/chat/llm-hacks.md).

---

## Cross-cutting invariants

Rules that apply in many places. Breaking them causes wrong-document bugs, frozen UI, or tools that never run. Paths: [`docs/repo-map.md`](docs/repo-map.md).

- **Use the extension’s `self.ctx`, not a fresh UNO context.** Lookups for package info, dialogs, and similar must use the component context the extension was given. Calling `uno.getComponentContext()` can return a different context and quietly break those lookups. Same idea for Calc chat context: `get_calc_context_for_chat` needs `ctx` from the panel / MainJob, not a bootstrap call.

- **Keep the chat FSM pure.** `next_state` lives in `plugin/chatbot/tool_loop_state.py` and only computes the next state—no UNO calls and no I/O. `plugin/framework/service.py` holds the frozen markers (`BaseState`, `FsmTransition`) and `ServiceRegistry`. Side effects (UI updates, MCP, document work) belong in the panel or MCP layers.

- **Stream on a worker; drain on the UI thread.** Background work pushes tuples onto a `queue.Queue`. The first element must be a `StreamQueueKind` **enum member**, not a bare string. When `AsyncCallback` exists, `run_stream_drain_loop` handles the items already queued and **returns to the VCL loop**, then re-arms with `addCallback` or a ~100 ms idle delay. Do not put the `while` / `Queue.get` loop back inside that callback: it holds SolarMutex across the idle wait. The blocking loop remains only when no callback can be armed (unit tests, eval harness). Do not use UNO `XTimerListener` for sidebar streaming. `run_blocking_in_thread(..., pump_idle=False)` stays a non-pumping wait. More: [docs/framework/streaming-and-threading.md](docs/framework/streaming-and-threading.md).

- **Refresh document context each chat send.** Each request ends with a fresh trailing `[DOCUMENT CONTENT]` snapshot (merged into the trailing user message on chat turns, or the trailing tool message in tool rounds); the stored history never contains it and is never rewritten. The system prompt contains the base instructions and user memory, which remain stable across requests to maximize prompt cache hits. Why: providers cache an exact prefix, and rewriting `messages[0]` on every send misses that cache.

- **Register tools so schemas and execution agree.** Matching uses `uno_services` first, then `doc_types`. Anything advertised by `get_schemas` must be runnable via `execute`. Default main-chat tools are `tier="core"`; nested specialized sets use `specialized` / `specialized_control` and are omitted from default lists. Gateway tools must list **every** UNO service they support (e.g. Draw **and** Impress). Writer `charts` / `shapes` share tool **names** with Calc/Draw—the Writer class must declare the **union** of those services or execution rejects the document.

- **Do not start raw threads for background work.** Use `run_in_background`. Short fire-and-forget jobs share a daemon pool with a fixed worker count (unbounded submit queue; pool size is controlled by `WRITERAGENT_BG_POOL_WORKERS`, default `2`, valid values are integers `>= 1`); pass `dedicated=True` (or `daemon=False`) for servers, pipe drains, infinite loops, and any job another thread will `join()`. Long subprocesses use `AsyncProcess`; if stderr is piped, drain it continuously or redirect it, or the process can deadlock ([docs/framework/threading.md](docs/framework/threading.md)). Dev builds enable a UNO thread guard by default (`thread_guard`; set `WRITERAGENT_UNO_THREAD_GUARD=0` to opt out; release OXTs stub it off). Wrap document-model access at boundaries with `guard_uno` (e.g. `get_active_document`, frame `_get_document_model`, `resolve_document_by_url`, `open_document_for_read`). For `ToolContext`, use `get_ctx()`—not the raw bootstrap `self.ctx`. Details: [docs/framework/uno-thread-safety.md](docs/framework/uno-thread-safety.md).

- **Surface errors through the shared helpers.** Prefer `WriterAgentException` and `format_error_payload` (`errors`). Tools should fail via `_tool_error`. UI lifecycle uses `suppress_disposed`; document tools re-raise disposal via `is_disposal` / `DocumentDisposedError` — do not catch `uno.Exception` to “avoid” `DisposedException`. There is no active `DocumentCache`—do not assume one. Details: [docs/framework/exception-policy.md](docs/framework/exception-policy.md).

- **Two products, one OXT at a time.** WriterAgent (`make deploy`, `plugin/main.py`) vs LibrePy (`make build-core` / `deploy-core`, `plugin/main_core.py`, `extension-core/`). `deploy-core` removes WriterAgent; `register-built-oxt` / `make release` removes LibrePy. Dual-install overlay is **not shipped**. File list: [`scripts/librepy_bundle_paths.py`](scripts/librepy_bundle_paths.py). Packaging: [docs/scripting/librepy-split.md](docs/scripting/librepy-split.md).

- **LibrePy-safe document helpers.** Linebreaks, tracked-deletion reads, heading trees, path, selection text, Writer text slices, and selection range / char count: `plugin/doc/text_helpers.py`. Type guards: `plugin/framework/doc_type.py`. Document properties: `udprops.py`. Do **not** import `document_helpers` from LibrePy paths (WriterAgent chat context / `DocumentService`). Do **not** re-export the light helpers from `document_helpers`.

- **`plugin.framework.client` package init is lazy.** HTTP / errors / provider detection load immediately; `LlmClient` and analysis (`run_trusted_analysis`) load on attribute access. Embeddings are `plugin.embeddings`, not this package. LibrePy may import `requests` / `provider_detection`. Do not import `llm_client` or `plugin.embeddings` from LibrePy paths.

- **Stop / cancellation policy:** When Stop is clicked, abort waiting on network/HTTP/IPC packets immediately (e.g. streaming LLM, image generation API, subprocess waits). However, do **not** abort document mutations (inserting pictures, appending in-flight text chunks, writing analysis/plots to sheets). Once data is generated and in hand, let it land in the document cleanly rather than implementing complicated defensive abort code or throwing away work. For voice/speech: clicking Stop while recording ends the take and transcribes it into the query box—do **not** abort transcription, throw away the audio, or auto-submit the transcript to the model.

UNO helpers are intentionally split (`uno_context`, `text_helpers`, `plugin/framework/doc_type.py`, `udprops`, `document_helpers` for chat context / `DocumentService`, `dialogs`)—there is no monolithic `uno_helpers.py`.

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

**Logging / MCP:** Logs go to `writeragent_debug.log` next to `writeragent.json`. `enable_agent_log` is separate (structured `[Agent]` traces are written even when `log_level` is above DEBUG). In unexpected `except` blocks, use **`log.exception("Context")`**. MCP work drains on the main thread ([docs/mcp-protocol.md](docs/mcp-protocol.md)). Do not read API keys from the environment in production; do not use **`tempfile.mktemp()`**. For scratch debug files under `/tmp`, prefer `flush=True`.

**Tests / packaging:** UNO tests go through `testing_runner`; debug-menu suites run on the UI thread ([docs/archive/test_architecture_analysis.md](docs/archive/test_architecture_analysis.md)). New extension components must be registered in `extension/META-INF/manifest.xml`. **`locales/*.pot` is generated at build** (`make extract-strings` / `refresh-pot`); do not hand-edit `.po`/`.pot` catalogs or spend review time on POT diffs.

### Global Python

Do not reuse the names **`logging`**, module **`log`**, or gettext **`_`** for unrelated variables. UI code imports **`_`** from `i18n`. Never bind bare `_` as a throwaway (`for _ in …`, `a, _, _ = fn()`, `except Exception as _:`)—use a real name (`unused`, `idx`). Private helpers named `_foo` are fine.

### Do not redo (already shipped)

Do not redo items live in the area `AGENTS.md` files (see [`plugin/scripting/AGENTS.md`](plugin/scripting/AGENTS.md) and [`plugin/calc/AGENTS.md`](plugin/calc/AGENTS.md)).

---

## Where to look

Entry points and topic docs: [`docs/repo-map.md`](docs/repo-map.md). Hermes truncates each context file at 20,000 characters (head+tail); keep invariants here and catalogs in `docs/repo-map.md`.
