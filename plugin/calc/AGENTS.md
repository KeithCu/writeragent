# Calc

Root invariants still apply (one OXT at a time, `get_calc_context_for_chat`
needs `ctx` from the panel / MainJob, specialized tiers omitted from
default tool lists).

## Entry points

- `=PROMPT()`: `prompt_addin.py`, `prompt_function.py`
- `=PYTHON()` / LibrePy: `python/addin.py`, `python/addin_librepy.py`, `python/function.py`
- Do **not** drop `analyzer.py` from the LibrePy bundle (reserved).

Topic docs: [docs/calc/prompt-function.md](../../docs/calc/prompt-function.md),
[docs/calc/specialized-toolsets.md](../../docs/calc/specialized-toolsets.md),
[docs/calc/conditional-formatting.md](../../docs/calc/conditional-formatting.md),
[docs/calc/sheet-filter.md](../../docs/calc/sheet-filter.md),
[docs/calc/date-time-handling.md](../../docs/calc/date-time-handling.md),
[docs/calc/py-data-shapes.md](../../docs/calc/py-data-shapes.md),
[docs/enabling_numpy_in_libreoffice.md](../../docs/enabling_numpy_in_libreoffice.md).

## Sharp edges

- `=PROMPT()` must not `processEventsToIdle` during recalc (`run_blocking_in_thread(..., pump_idle=False)`). Pumping re-enters the formula engine (`#VALUE!`). `=PY()` already blocks without a VCL pump.
- `=PY()` gets the calling **document** from LibreOffice's hidden caller argument (`XPropertySet` first parameter in `XPythonFunction.idl`); that is the only source. No front-window guess, open-documents search, or last-active cache; a missing caller returns `Error: no calling document`. The calling **cell** is still unknown: spill/image/scalar-cache only when `locate_formula_cell_in_doc` finds a **unique** formula origin in that document. Off-main the doc is only passed through (no UNO on it): no shared session id or init kwargs there, and locate + write for auto-spill are posted to the UI thread.
- Unsaved workbooks all have `getURL() == ""`. In-memory spill keys and `LOADED_DOCUMENTS` use `_lifecycle_key` (RuntimeUID) in that case; saved books stay keyed by file URL. `calc:unsaved:{uuid}` is its own workbook id. Spill orphan cleanup saves `WriterAgentSpillRegistry` on the sheet's owner, not the focused window.
- LibrePy uses `addin_librepy.py` instead of `addin.py`.
- Collabora Online `=PY()` files store `…PYTHONCOMPUTEFUNCTIONS.GETPY`; on open LibrePy rewrites that prefix ([`python/collabora_formula.py`](python/collabora_formula.py)). Do not register Collabora’s UNO service from the OXT.
- Nested specialized sets use `specialized` / `specialized_control` and are omitted from default main-chat lists. Callers use `delegate_to_specialized_calc_toolset`.
- `plugin/scripting/venv/calc_functions_*.py` alphabet splits are intentional; do not merge them.
- `float(...)` inside `=PYTHON("...")` formula strings → Calc lexer `#NAME?`. Use code-in-cell or bare `np.sum` (see enabling-numpy doc).
- In tests, resolve tools with `plugin.main.get_tools().get("tool_name")`.
- Sheet names starting with `_` (xlsx→ods `__Anonymous_Sheet_DB__*`, etc.) stay in the workbook. Omit them from agent-facing lists (`list_sheets`, `get_sheet_summary`, chat context, `named_range_list(scope="all")`, unqualified named-range fallback). Do **not** delete them at trial/document open (parked).
- Named-range rename must reject names `ScRangeData::IsNameValid` would reject (`A1`, dots, leading digits, spaces) before `setName`. `addNewByName` already validates; `setName` does not. A bare name in `named_range_get_info`, `named_range_edit`, and `named_range_delete` prefers the active sheet's local range over a same-spelled global name. Explicit `scope="global"` or a sheet name still forces that container.
