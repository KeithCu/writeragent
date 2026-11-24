# AGENTS.md — Context for AI Assistants

**Assume the reader knows nothing about this project.** This file lists global **invariants** and **easy mistakes**. Everything else lives in [`docs/repo-map.md`](docs/repo-map.md) and the linked docs—open those when changing behavior. (Hermes truncates context files at 20,000 characters; keep invariants here and catalogs in `docs/repo-map.md`.)

**Translations:** WriterAgent extracts and translates strings itself (`make extract-strings`; `locales/*.pot` is generated at build). Wrap new user-facing strings in `_()` (`from plugin.framework.i18n import _`), and never use `_` as a variable name (use `_idx`). Agents and subprocesses **must not translate UI strings**, fill `msgstr` values, or edit `.po`/`.pot` catalogs.

**Docs:** After any nontrivial change, **update documentation**. Prefer the **topic doc** under `docs/`; touch `AGENTS.md` only when the change affects many areas or global rules.

**Complexity:** This codebase is complicated for its size. When asked to build a feature, always find the approach using the **least amount of code** and complexity, reusing or refactoring existing functions. Include technical-debt reductions in your plan when you spot them.

---

## Tests

New features and bugfixes **must include tests**.
- **No change-detector tests:** Do not assert on prompt or tool-description prose, log text, static config or catalog constants, removed-feature lists, signatures/exports already enforced by type checkers, or exact mock call order/choreography, unless the test cites a real bug. Keep structural guards (section order, uniqueness, routing) and bug-regression tests.
- **Unit:** `tests/`, pytest, when logic can be mocked. Match the source module name (e.g. `foo.py` -> `test_foo.py`) and add new cases to the matching `test_` file.
- **UNO / LibreOffice:** Collocated `*_uno.py` files run by `testing_runner.py` (not pytest; GitHub CI runs them on every PR)—use `@native_test`, `@setup`, `@teardown`; test functions take `ctx`. Follow the same module-matching rule (`foo.py` -> `test_foo_uno.py`).
- **Enumerations:** All `XEnumeration` walks (portions, paragraphs, annotations, desktop components) must use `enum_has_more` (`plugin/framework/uno_context.py`) because `not enum.hasMoreElements()` does not terminate on mock objects.
- **Execution Policy (the one testing rule):** **Do not run tests or `make typecheck` before starting work** unless you need failure output to understand a bug; assume the tree is already green. After edits, run `make test-bot`, then `make typecheck`, one at a time in the foreground (never in a background or polling loop); both must pass before each commit. `make test-bot` already picks the unit tests for your changes (changed test files, tests named after a changed module, tests that import it, and tests that name a changed data file), so do not also run those tests with pytest. Do not run whole test packages, `make pytest`, `make test`, `make test-full` or `make test-uno`, and do not run existing `*_uno.py` tests: GitHub CI runs them.
- **Inspect UNO directly:** When unsure of UNO API behavior, do not guess or add speculative fallbacks—write a throwaway `@native_test` probe (see `tests/chatbot/test_hamburger_menu_uno.py` as an example) and run only that file against an isolated LibreOffice (pool coders: `pipeline/uno-probe.sh <file>`; elsewhere `.venv/bin/python plugin/testing_runner.py <file>`). Probes check LibreOffice behaviour; they are not a substitute for CI's UNO run.

---

## Comments

Explain the code that is here **now** for a reader who does not know this project (two or three short sentences where needed; avoid cryptic one-liners).
- **No “what went wrong” boilerplate:** No “this used to…”, “previously…”, “Bugfix:”, or postmortems. Stating in present tense what would break if the code were absent is fine.
- **Rewrite comments whole.** When changing a comment, replace the full sentence; never leave half of the old one beside the new one.
- **Upstream quirks:** When matching LibreOffice / UNO / upstream behavior, **cite the exact source** (file + line or function).

---

## Project overview

**WriterAgent** is a LibreOffice extension (Python + UNO) for Writer, Calc, and Draw (Impress paths where registered).

**Python:** Dev/tooling **3.11–3.13** (`pyproject.toml`), with `.venv` pinned to **3.13** (`.python-version`). Extension runtime is whatever LibreOffice bundles (often older)—**shipped code under `plugin/` must not rely on stdlib newer than that runtime.**

---

## Essential commands

| Command | When to use |
|---------|-------------|
| `make typecheck` | Run after `make test-bot`, before each commit (incremental; `CI=true` or `TYPECHECK_FULL=1` for full). Details: [docs/framework/type-checking.md](docs/framework/type-checking.md) |
| `make test-bot` | Unit tests for your uncommitted changes (xdist; full suite when `conftest.py`, a shared test helper, `Makefile` or `pyproject.toml` changed). Run before each commit, then `make typecheck`. |
| `make test` | Incremental typecheck + pytest + UNO tests, for a human run before release work; agents use `make test-bot` + `make typecheck`. |
| `make build` | Produce `build/WriterAgent.oxt` only (no install). |
| `make deploy` | Build + install WriterAgent OXT; **restart LibreOffice** (or `make deploy writer/calc/draw/impress` to launch). |
| `make build-core` / `make deploy-core` | LibrePy OXT only (`build/LibrePy.oxt`); `deploy-core` **removes WriterAgent**. |
| `make eval-ab SUITE=...` | Test a prompt change (`calc`, `writer`, `draw`, `all`, `string`, `lo`). |

---

## Cross-cutting invariants

- **Use the extension’s `self.ctx`, not `uno.getComponentContext()`.** Package info, dialogs, and Calc chat context lookups must use the component context the extension/panel was given.
- **Never block the UI thread or hold SolarMutex across I/O.** Stream on a worker and drain on the UI thread; see [docs/framework/streaming-and-threading.md](docs/framework/streaming-and-threading.md).
- **Do not start raw threads for background work.** Use `run_in_background` (pass `dedicated=True` for servers, infinite loops, and jobs another thread will `join()`) and `AsyncProcess` for long subprocesses ([docs/framework/threading.md](docs/framework/threading.md)). Wrap document-model access at boundaries with `guard_uno`, and use `ToolContext.get_ctx()` rather than raw `self.ctx` ([docs/framework/uno-thread-safety.md](docs/framework/uno-thread-safety.md)).
- **Surface errors through the shared helpers.** Prefer `WriterAgentException`, `format_error_payload` (`errors`), and `_tool_error` for tools. UI lifecycle uses `suppress_disposed`; document tools re-raise disposal via `is_disposal` / `DocumentDisposedError`—do not catch `uno.Exception` to hide disposal ([docs/framework/exception-policy.md](docs/framework/exception-policy.md)).
- **Two products, one OXT at a time.** WriterAgent (`plugin/main.py`) vs. LibrePy (`plugin/main_core.py`, `extension-core/`). Dual-install overlay is **not shipped** ([docs/scripting/librepy-split.md](docs/scripting/librepy-split.md)).

---

## Habits reviews keep removing

These are the most common findings in recent reviews (details and examples: [docs/framework/excess-complexity-archaeology.md](docs/framework/excess-complexity-archaeology.md)). Avoid them in new code.
- **No defensive code for states that can't occur.** No `getattr`/`hasattr`/`isinstance`/`try` guards around values whose type or presence is already guaranteed (a method every document has, a dict a function always returns, a generated module's attribute).
- **Decide a fact once.** Validate, default, clamp or look something up at one owner and pass the result along; don't re-check it at every layer or inside a loop.
- **Imports at the top.** Use an in-function import only at a host/child/bundle boundary, for real weight, or for a named cycle, and say which in a short comment. Never repeat a guarded import of the same module in several functions; use one helper.
- **One owner, no copies.** Before writing a helper, search for an existing one (e.g. doc-type detection); call it instead of adding a near-copy.
- **No investigation residue.** Remove timers, extra logging and debug dumps added while chasing a bug before committing.
- **Public API, not private reach-ins.** Don't read another module's `_private` names; expose a property or function.

---

## Area guides & coding rules

Area-specific rules and “do not redo” lists live next to the code (read before editing):
- **When editing `plugin/chatbot/`:** [`plugin/chatbot/AGENTS.md`](plugin/chatbot/AGENTS.md)
- **When editing `plugin/writer/`:** [`plugin/writer/AGENTS.md`](plugin/writer/AGENTS.md)
- **When editing `plugin/calc/`:** [`plugin/calc/AGENTS.md`](plugin/calc/AGENTS.md)
- **When editing `plugin/scripting/` or LibrePy:** [`plugin/scripting/AGENTS.md`](plugin/scripting/AGENTS.md)

**Logging & safety:** In unexpected `except` blocks, use **`log.exception("Context")`**. Do not read API keys from the environment in production; do not use **`tempfile.mktemp()`**.

**Packaging:** Register new extension components in `extension/META-INF/manifest.xml`.

**Renames and moves:** Move files with `git mv`, then run [`scripts/rename_identifier.py`](scripts/rename_identifier.py) (`--dry-run` first; pass dotted paths like `plugin.mcp`, not bare words like `mcp`, or use `--import-from`/`--import-to`). Fix non-Python references (Makefile, docs, lint configs) using `rg`, and run `scripts/generate_manifest.py` if module folders moved.

**Global Python naming:** Do not reuse **`logging`** or module **`log`** for unrelated variables.
