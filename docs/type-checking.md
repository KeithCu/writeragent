# Static type checking (`ty`)

WriterAgent uses [Astral’s `ty`](https://docs.astral.sh/ty/) on the `plugin/` tree. This document covers **what changed in the code** (especially UNO and extension patterns), **where** it landed, and the **minimum tooling** needed to run the checker. Quick-reference annotation rules live in [§21 of `AGENTS.md`](../AGENTS.md#21-static-type-checking-ty).

---

## Outcome

| Stage | Notes |
|--------|--------|
| Initial | On the order of **1000+** diagnostics before scoping (including vendored `plugin/contrib` and noisy test-only code). |
| After narrowing | Excluding **`plugin/contrib`** and **`plugin/tests`** via `pyproject.toml` focused work on application code; one documented pass fixed on the order of **~141** categorized issues in that scope. |
| Final | **`ty check`** reports **no errors** for the configured include set. **`make check`** runs **`ty`** only; **`make typecheck`** runs **`ty`**, **`mypy`**, and **`pyright`** in sequence; **`make test`** runs those three, then pytest and LO tests (types before tests). **`make release`** calls **`make test`** first, then the release bundle (see **`Makefile`**). |

Static checking does **not** prove LibreOffice runtime behavior: UNO remains highly dynamic. The goal is consistent annotations, usable stubs, and fewer accidental mistakes in Python code.

---

## Tooling (short)

- **`pyproject.toml`** — `[tool.ty.src]`: `include = ["plugin"]`, `exclude = ["plugin/contrib", "plugin/tests"]`.
- **`Makefile`** — `make ty`: ensures `import uno` (via `make fix-uno` if needed), then `python -m ty check --exclude plugin/contrib/`.
- **Dev dependency**: **`types-unopy`** (LibreOffice API stubs). **`make fix-uno`** links system UNO into `.venv` so `uno` and `com.sun.star` resolve; without that, the checker cannot see extension types.

### mypy (optional)

- **`make mypy`** — same prelude as `ty` (`make manifest`, `import uno` → `make fix-uno`), then **`python -m mypy`** using **`[tool.mypy]`** in `pyproject.toml`.
- **Not** part of **`make check`** or **`make build`** alone; it **is** part of **`make typecheck`** and **`make test`**. **`make release`** runs **`make test`** first, so mypy runs there too. Use standalone **`make mypy`** to compare against **`ty`**. Mypy often reports issues `ty` does not (and vice versa).
- **Scope**: `packages = ["plugin"]` with path **`exclude`** plus **`[[tool.mypy.overrides]]`** `ignore_errors = true` for **`plugin.contrib.*`** and **`plugin.tests.*`**. Plain `exclude` alone does not stop mypy from checking vendored contrib when resolving the `plugin` package, so the overrides mirror ty’s “no contrib / no tests” intent.
- **Stubs**: **`types-requests`**, **`types-unopy`**, and overrides for **`officehelper`** and **`sounddevice`** (no PyPI stubs: **`disable_error_code = ["import-untyped"]`** plus an inline **`# type: ignore[import-untyped]`** on the lazy import in **`audio_recorder.py`**) are configured for a usable first run; remaining diagnostics are normal application code until you tighten further.

### Pyright (optional)

- **`make pyright`** — same prelude as **`ty`** / **`mypy`** (`make manifest`, then **`import uno`** → **`make fix-uno`** if needed), then **`python -m pyright`** using **`[tool.pyright]`** in **`pyproject.toml`** (`include` / **`exclude`** mirror **`ty`**).
- **Not** part of **`make check`** or **`make build`** alone; it **is** part of **`make typecheck`** and **`make test`** (and thus **`make release`**). Diagnostics overlap Pylance in the editor; use standalone **`make pyright`** for a quick CLI pass.
- **Status (CLI)**: On the same scoped tree as **`ty`**, Pyright can be driven to **zero errors**; remaining noise is usually **`reportMissingModuleSource`** for **`com.sun.star.*`** imports (stubs exist for typing, but Pyright still warns that it cannot resolve “source” for those modules). That is typically safe to ignore if runtime UNO works.

### Pyrefly (experimental)

- **[Pyrefly](https://pyrefly.org/)** — Meta’s Rust-based type checker and language server; **`make pyrefly`** runs **`python -m pyrefly check`** with the same **`import uno`** / **`make fix-uno`** prelude as **`make pyright`**.
- **Not** part of **`make check`**, **`make typecheck`**, or **`make test`**. Use as an optional fourth opinion while triaging; **`[tool.pyrefly]`** in **`pyproject.toml`** sets **`project-includes`**, **`project-excludes`**, and **`python-version`** (see [Pyrefly configuration](https://pyrefly.org/en/docs/configuration/)).
- Like other static checkers, Pyrefly treats **`typing.TYPE_CHECKING` as true**, so imports and types under **`if TYPE_CHECKING:`** participate in analysis. Config sets **`search-path = ["."]`** so **`from plugin...`** in those blocks resolves from the repo root, and **`check-unannotated-defs`**, **`infer-return-types`**, and **`infer-with-first-use`** match Pyrefly’s defaults for full analysis of checked modules.
- Until the project drives Pyrefly to **zero errors**, CI should not gate on it; treat output as experimental signal alongside ty/mypy/pyright.

---

## Pyright vs `ty` and mypy (what differed in practice)

All three tools share **`types-unopy`**, **`make fix-uno`**, and the same **`plugin/`** scope (contrib and tests excluded). **`make check`** and **`make build`** run **`ty`** only (fast gate). **`make test`** runs **`ty`**, **`mypy`**, and **`pyright`**, then tests. **`make release`** runs **`make test`** (same gate) before building the release **`.oxt`**. A full Pyright pass still found **real issues and strictness gaps** that **`ty`** (and **`mypy`**) often did not report on the same codebase, or reported much less loudly.

### Optional and `None` narrowing

- **`reportOptionalMemberAccess`**: Pyright is aggressive about **calling methods on values that may be `None`**. Example: **`DrawBridge.get_active_page()`** can return **`None`** at runtime; without an explicit **`if page is None: ...`** early exit, uses like **`page.getCount()`** were errors under Pyright even when **`ty`** accepted the file. **Fix**: guard or assert before use (Draw shape tools in **`plugin/modules/draw/shapes.py`**).
- **`reportPossiblyUnboundVariable`**: Assignments only on some branches (e.g. **`compiled`** only inside **`if use_regex:`**, or **`restore_snapshot`** only inside **`try`**) can be flagged when a later branch uses the name. **Fix**: initialize before the branch (**`compiled = None`**) or declare **`restore_snapshot: dict[str, Any] | None = None`** before **`try`**, then assign (**`search.py`**, **`testing_runner.py`**).

### Overrides, bases, and variance

- **`reportIncompatibleMethodOverride`**: Pyright checks **return types and container types** against **`types-unopy`** strictly. Examples: **`XDispatchProvider.queryDispatch`** returning **`None`** where the stub expects **`XDispatch`**, or **`queryDispatches`** returning a **list** where the stub expects a **tuple**. Runtime UNO often allows this; **fix** is either to match the stub shape or a **targeted `# pyright: ignore[reportIncompatibleMethodOverride]`** on that method (**`DispatchHandler`** in **`main.py`**).
- **`reportGeneralTypeIssues`**: A **second base class** loaded from the Java/IDL bridge (e.g. **`XPromptFunction`** from **`org.extension.writeragent`**) is not always treated as a valid class base. **Fix**: stub base inheriting **`unohelper.Base`** for **`ImportError`** fallbacks, plus **`# pyright: ignore[reportGeneralTypeIssues]`** on the concrete class when the real IDL base is present (**`prompt_function.py`**).
- **`reportIncompatibleVariableOverride`**: Multiple mixins declaring the **same attribute** (e.g. **`client`**) with types that Pyright considers **incompatible under invariance** (mutable **`Protocol`** fields vs concrete class). **`ty`** may not emit the same diagnostic; resolving it may require aligning annotations, widening a **`Protocol`** field, or structural refactors (**chatbot panel / mixins**).
- **`list` invariance** (Pyright / strict typing): Passing **`list[ChatMessage]`** where an API is typed as **`list[ChatMessage | dict[...]]`** can fail in Pyright; **`ty`** may be looser. **Fix**: **`cast(...)`** or widen the target API type (**`smol_model`** paths).

### Config and JSON-shaped values

- **`reportArgumentType`** on **`int(...)`**: **`get_config(ctx, key)`** is effectively **JSON-shaped** (**`Any`** / wide unions). Pyright rejects **`int(get_config(...))`** when the inferred type includes non-numeric shapes. **Fix**: use **`get_config_int` / `get_config_str`** with an explicit **`-> int`** (or **`str`**) helper signature (**`config.py`**, call sites such as **`prompt_function.py`**).

### `dict` payload widening

- If the first assignments build a **`dict[str, str]`**, later **`payload["details"] = {...}`** can fail in Pyright. **`ty`** may not flag the same. **Fix**: annotate **`payload: dict[str, Any]`** or **`cast(dict[str, Any], ...)`** (**`format_error_payload`**, **`tool_registry`** merges).

### `getattr` / UNO context chains

- Nested patterns like **`getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())`** triggered **`reportAttributeAccessIssue`** / optional access on **`Any`**. **Fix**: small helper or sequential **`getattr`** + **`callable`** checks, **`assert smgr is not None`**, then **`cast(Any, smgr).createInstanceWithContext(...)`** (**`uno_context`**, **`dialogs._load_xdl`**, **`image_tools`**, **`queue_executor`**, **`main`** icon loading).

### Import / branch typing quirks

- **`urllib`**: Importing **`urllib.error`** (or similar) **inside** a function that also uses **`urllib.request`** / **`urllib.parse`** can make Pyright **narrow** the **`urllib`** package incorrectly. Prefer **module-level** imports for **`urllib`** submodules.
- **`try` / `except ImportError`**: Fallback functions like **`def is_writer(model): return False`** can be inferred as **`-> Literal[False]`** while the imported symbol is **`(Any) -> bool`**, producing **`reportAssignmentType`** when both arms assign into one logical “slot”. **Fix**: explicit **`-> bool`** on the fallbacks (**`testing_runner.py`**).

### Optional modules and guards

- **`sqlite3`** may be typed as optional; Pyright wants **`assert sqlite3 is not None`** on paths that use it after **`HAS_SQLITE`**.
- **`user_config_dir(ctx)`** as **`str | None`**: filesystem stores should **`raise ConfigError`** rather than joining on **`None`** (**`MemoryStore`**, **`SkillsStore`**).

### smolagents and FSM helpers

- **`model_output`** not always **`str`**: guard with **`isinstance`** or **`str(...)`** before **`strip()`** (several agent/chat paths).
- **`EffectInterpreter.current_state`**: declare **`SendHandlerState | None`** where **`None`** is a real state (**`send_handlers`**).

### Cross-check workflow

After Pyright-driven edits, run **`make ty`** (or **`make test`**) anyway: fixes for Pyright do **not** always change **`ty`**, and occasionally one tool will disagree. **`make build`** enforces **`ty`**; **`make test`** / **`make release`** enforce all three tools.

---

## UNO and extension-heavy patterns (what actually changed)

These are the recurring themes that dominated the cleanup, beyond “add `str | None` everywhere.”

### 1. `com.sun.star` imports and optional UNO

Many modules import constants from `com.sun.star.*`. Stubs or resolution can fail; some code paths must run **without** LibreOffice (tests, analysis). The pattern is: **try real imports**, else **`cast(Any, …)`** integer stand-ins so the rest of the module still type-checks.

See [`plugin/modules/calc/error_detector.py`](../plugin/modules/calc/error_detector.py) (and similarly analyzer/inspector): `CellContentType`, `FormulaResult`, and a fallback branch with `cast(Any, 0)` … `cast(Any, 4)`.

Some imports stay as `# type: ignore[unresolved-import]` where the checker still cannot resolve a particular `com.sun.star` module path.

### 2. Structs, `Any`, and callbacks

`uno.createUnoStruct("com.sun.star.beans.PropertyValue")` and similar return values that stubs treat loosely. The codebase uses **`cast(Any, …)`** where a struct is built and passed through (e.g. [`plugin/modules/writer/format_support.py`](../plugin/modules/writer/format_support.py)).

[`plugin/framework/queue_executor.py`](../plugin/framework/queue_executor.py) passes **`uno.Any("void", None)`** into UNO callbacks; that line is explicitly ignored where the stub contract does not match pyuno’s usage.

### 3. Listener / interface overrides: **parameter names matter**

`types-unopy` expects **the same parameter names as the `.pyi` stubs**. Implementations of `XActionListener`, `XEventListener`, etc. must use names like **`rEvent`** and **`Source`**, not arbitrary `ev` / `e`, or `ty` raises **`invalid-method-override`**.

Examples: [`plugin/framework/dialogs.py`](../plugin/framework/dialogs.py) (`TabListener`: `actionPerformed(self, rEvent)`, `disposing(self, Source)`), [`plugin/modules/chatbot/panel_resize.py`](../plugin/modules/chatbot/panel_resize.py) (`on_window_resized(self, rEvent)` and use of `rEvent.Source`).

### 4. `queryInterface` and dynamic objects

Runtime UNO uses **`queryInterface`** heavily; return types are often opaque. Class-based `queryInterface` can be unreliable under pyuno (see `AGENTS.md`); typing-wise, code may need **`# type: ignore[attr-defined]`** or narrow casts after a successful query. Draw/Writer code that obtains `XSelectionSupplier` and similar follows this pattern.

### 5. Mixins: **`Protocol` for the host**

`ToolCallingMixin` and send handlers are mixed into large panel classes. **`ToolLoopHost`** in [`plugin/modules/chatbot/tool_loop.py`](../plugin/modules/chatbot/tool_loop.py) and **`SendHandlerHost`** in [`plugin/modules/chatbot/send_handlers.py`](../plugin/modules/chatbot/send_handlers.py) declare the attributes and methods the mixin expects so `self` is checkable without circular imports.

### 6. `TYPE_CHECKING` imports

Heavy or circular imports (e.g. `LlmClient`, `ChatSession`) are imported under **`if TYPE_CHECKING:`** at the top of the mixin modules so runtime import order stays unchanged but static analysis sees the types.

### 7. Dynamic attributes on events / worker glue

When attaching extra fields to objects (e.g. approval flows on events), the code uses **`setattr` / `getattr`** so the analyzer does not treat unknown attributes as errors—see tool-loop paths that set things like `query_override` on events ([`plugin/modules/chatbot/tool_loop.py`](../plugin/modules/chatbot/tool_loop.py)).

### 8. Context and services

[`plugin/framework/i18n.py`](../plugin/framework/i18n.py) uses **`cast(Any, ctx).getServiceManager()`** (or similar) because the UNO context type surface does not always expose what we need cleanly in stubs.

### 9. Targeted `# type: ignore` codes

Prefer **specific** ignore codes (`attr-defined`, `override`, `unresolved-import`, …) over blanket ignores. Reserve them for **pyuno/UNO boundaries**, third-party quirks, or legacy hotspots—not for silencing ordinary Python mistakes.

---

## Other recurring fixes (non-UNO)

- **Explicit generics**: `list[str]`, `dict[str, Any]`, `str | None` instead of untyped collections.
- **Narrowing**: `if x is not None` before use; avoid forcing the checker to assume values are defined.
- **`cast(Iterable, …)`** for generators that `ty` does not infer as iterable (see §21).
- **Registry / service construction**: dynamic class registration may need small ignores where instantiation is reflection-like ([`plugin/framework/service_registry.py`](../plugin/framework/service_registry.py)).

---

## Files touched (representative list from the cleanup)

Roughly **40+** files were edited; groupings below match the original tracking notes.

**Framework**

- [`plugin/framework/errors.py`](../plugin/framework/errors.py), [`image_utils.py`](../plugin/framework/image_utils.py), [`legacy_ui.py`](../plugin/framework/legacy_ui.py), [`logging.py`](../plugin/framework/logging.py), [`service_registry.py`](../plugin/framework/service_registry.py), [`settings_dialog.py`](../plugin/framework/settings_dialog.py), [`smol_model.py`](../plugin/framework/smol_model.py), [`state.py`](../plugin/framework/state.py), [`tool_registry.py`](../plugin/framework/tool_registry.py)

**Entry / backends**

- [`plugin/main.py`](../plugin/main.py), [`plugin/modules/agent_backend/builtin.py`](../plugin/modules/agent_backend/builtin.py)

**Calc**

- [`plugin/modules/calc/analyzer.py`](../plugin/modules/calc/analyzer.py), [`error_detector.py`](../plugin/modules/calc/error_detector.py), [`formulas.py`](../plugin/modules/calc/formulas.py), [`inspector.py`](../plugin/modules/calc/inspector.py), [`legacy.py`](../plugin/modules/calc/legacy.py), [`manipulator.py`](../plugin/modules/calc/manipulator.py)

**Chatbot / sidebar**

- Panel, factory, wiring, resize, state machine, send handlers, tool loop, web research, history, audio paths, etc. under [`plugin/modules/chatbot/`](../plugin/modules/chatbot/)

**Writer / HTTP / infra**

- Writer tools and format paths; HTTP client/errors; plus build/docs updates (`Makefile`, `AGENTS.md`, locales where relevant).

---

## Code examples (patterns from the old notes)

**Narrow ignores at UNO boundaries**

```python
obj.method_call()  # type: ignore[attr-defined]
```

**Explicit annotations where the body is still dynamic**

```python
def process_data(data: Any) -> Any:
    return data.process()  # type: ignore[no-any-return]
```

**Unions and optional values**

```python
variable: str | int | None = get_value()
if obj is not None:
    obj.method()
```

**Override compatibility with stubs**

```python
def actionPerformed(self, rEvent: ActionEvent) -> None:  # type: ignore[override]
    ...
```

(Prefer matching stub **parameter names** exactly so `ignore[override]` is unnecessary when possible.)

---

## Lessons learned

1. **Incremental fixes** (small batches + `ty check`) beat large single dumps.
2. **Many errors share one pattern** (especially overrides and `com.sun.star` imports).
3. **UNO needs explicit boundaries**: ignores and casts at pyuno edges, not scattered through pure Python logic.
4. **Keep stub names** for listeners/interfaces aligned with `types-unopy`.

---

## What developers should run

1. **`make fix-uno`** when `import uno` fails in the venv.
2. **`make ty`** or **`make check`** before quick iterations; **`make test`** (or **`make release`** before shipping) for **`ty` + mypy + pyright** plus pytest and LO tests.
3. When adding features, follow §21 in `AGENTS.md`, the UNO patterns above, and—if you use Pyright—the **Pyright vs `ty` and mypy** section for strictness that may not show up in **`make ty`**.

