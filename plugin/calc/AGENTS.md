# Calc

Root invariants still apply (one OXT at a time, `get_calc_context_for_chat`
needs `ctx` from the panel / MainJob, specialized tiers omitted from
default tool lists).

## Entry points

- `=PROMPT()`: `prompt_addin.py`, `prompt_function.py`
- `=PYTHON()` / LibrePy: `python/addin.py`, `python/addin_librepy.py`, `python/function.py`
- Do **not** drop `analyzer.py` from the LibrePy bundle (reserved).

Topic docs: [docs/calc/specialized-toolsets.md](../../docs/calc/specialized-toolsets.md),
[docs/calc/conditional-formatting.md](../../docs/calc/conditional-formatting.md),
[docs/calc/sheet-filter.md](../../docs/calc/sheet-filter.md),
[docs/calc/date-time-handling.md](../../docs/calc/date-time-handling.md),
[docs/calc/py-data-shapes.md](../../docs/calc/py-data-shapes.md),
[docs/enabling_numpy_in_libreoffice.md](../../docs/enabling_numpy_in_libreoffice.md).

## Sharp edges

- `=PROMPT()` must not `processEventsToIdle` during recalc (`run_blocking_in_thread(..., pump_idle=False)`). Pumping re-enters the formula engine (`#VALUE!`). `=PY()` already blocks without a VCL pump.
- XAddIn never names the calling cell or document. Spill/image/scalar-cache only when `locate_formula_cell_in_doc` finds a **unique** formula origin. Off-main shared-kernel session/init only when exactly one Calc workbook is recorded. Off-main auto-spill must **not** treat a missing `target_doc` as a matrix formula: `get_cached_calc_document()` supplies the UI-thread model when the session is unambiguous (do not UNO on it off-main); locate + write are posted to the UI thread. Two recorded workbooks stay corner-only.
- LibrePy uses `addin_librepy.py` instead of `addin.py`.
- Collabora Online `=PY()` files store `…PYTHONCOMPUTEFUNCTIONS.GETPY`; on open LibrePy rewrites that prefix ([`python/collabora_formula.py`](python/collabora_formula.py)). Do not register Collabora’s UNO service from the OXT.
- Nested specialized sets use `specialized` / `specialized_control` and are omitted from default main-chat lists. Callers use `delegate_to_specialized_calc_toolset`.
- `plugin/scripting/venv/calc_functions_*.py` alphabet splits are intentional; do not merge them.
- `float(...)` inside `=PYTHON("...")` formula strings → Calc lexer `#NAME?`. Use code-in-cell or bare `np.sum` (see enabling-numpy doc).
- In tests, resolve tools with `plugin.main.get_tools().get("tool_name")`.
