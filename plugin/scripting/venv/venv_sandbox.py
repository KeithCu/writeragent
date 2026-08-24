# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv worker sandbox: path setup for vendored smolagents + LocalPythonExecutor.

Used by worker_harness.py (venv child adds repo root to sys.path for ``plugin.*`` imports).
Import policy is only VENV_AUTHORIZED_IMPORTS passed to LocalPythonExecutor—no find_spec pre-checks.

Trusted host helpers (vision, embeddings, …) use ``run_trusted_action`` via the worker
harness / ``trusted_action_registry`` — not string stubs through this sandbox.
"""

from __future__ import annotations

import ast
import datetime
import importlib
import logging
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

from plugin.contrib.smolagents.local_python_executor import InterpreterError, LocalPythonExecutor
from plugin.scripting.payload_codec import (
    PAYLOAD_DATAFRAME,
    child_pack_result,
    describe_wire_value,
    is_split_grid,
    find_image_payloads,
)
from plugin.scripting.config_limits import python_exec_timeout_default
from plugin.framework.constants import AUTO_IMPORTS
from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS

# Shared-kernel executors keyed by workbook session_id (calc:…). Cleared on reset_session,
# document OnUnload (workbook_lifecycle), or worker process exit.
_SESSION_EXECUTORS: dict[str, LocalPythonExecutor] = {}
_SESSION_LOCK = threading.Lock()

# Init scripts run once in calc:{workbook}:init; isolated cells seed from that snapshot.
_INIT_SCRIPT_HASH: dict[str, str] = {}
_CELL_SESSION_INIT_DIGEST: dict[str, str] = {}
_INIT_STATE_SKIP_KEYS = frozenset(
    {
        "__name__",
        "_print_outputs",
        "_operations_count",
        "result",
        "data",
        "ranges",  # always-list of CalcRange; re-injected each run
        "xl",  # binding-only Excel data bridge; re-injected each run
    }
)


def is_module_imported(code_str: str, module_name: str) -> bool:
    """Check if ``module_name`` is imported in any form in ``code_str``."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        # Fallback to simple substring match in case of syntax error.
        return f"import {module_name}" in code_str or f"from {module_name}" in code_str

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name or alias.name.startswith(module_name + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == module_name or (node.module and node.module.startswith(module_name + ".")):
                return True
    return False


_OPTIONAL_MODULE_LOCK = threading.Lock()


def optional_module(name: str) -> Any | None:
    with _OPTIONAL_MODULE_LOCK:
        if name in sys.modules:
            mod = sys.modules[name]
            # Ensure module is fully initialized
            spec = getattr(mod, "__spec__", None)
            if spec is not None and getattr(spec, "_initializing", False):
                pass
            else:
                return mod
        try:
            return importlib.import_module(name)
        except Exception:
            return None


def apply_auto_imports(code: str) -> tuple[str, int]:
    """Prepend imports from AUTO_IMPORTS if missing and available. Returns (new_code, lines_added)."""
    prepended_lines = []
    for module_name, import_stmt in AUTO_IMPORTS.items():
        if not is_module_imported(code, module_name):
            if optional_module(module_name) is not None:
                prepended_lines.append(import_stmt)

    if not prepended_lines:
        return code, 0

    return "\n".join(prepended_lines) + "\n" + code, len(prepended_lines)


def inject_auto_imports(executor: LocalPythonExecutor, code: str) -> None:
    """Inject auto imports into executor state if referenced but not imported in code."""
    bindings = {}
    for module_name, import_stmt in AUTO_IMPORTS.items():
        if not is_module_imported(code, module_name):
            mod = optional_module(module_name)
            if mod is not None:
                alias = import_stmt.split(" as ")[-1].strip() if " as " in import_stmt else module_name
                bindings[alias] = mod
    if bindings:
        executor.send_variables(bindings)


def serialize_result(obj: Any) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values (split_grid for large numeric/mixed arrays).

    DataFrames (and named Series) are returned as a dataframe envelope with 'columns' and 'data'
    (the latter is a split_grid envelope when large enough, or nested lists). This replaces the
    previous to_dict(orient="records") path which produced expensive list-of-dicts and bypassed
    the binary grid fast path.
    """
    try:
        return _serialize_result_impl(obj)
    except Exception:
        log.exception(
            "venv_sandbox serialize_result failed for value %s",
            describe_wire_value(obj),
        )
        raise


def _capture_open_figures_payload(*, fmt: str = "svg") -> tuple[dict[str, Any] | None, str]:
    """Return (image payload from open pyplot figures, optional stdout note)."""
    plt_mod = optional_module("matplotlib.pyplot")
    if plt_mod is None:
        return None, ""
    fignums = plt_mod.get_fignums()
    if not fignums:
        return None, ""

    figs = [plt_mod.figure(num) for num in fignums]
    note = ""
    if len(figs) > 1:
        items = [_figure_to_image_payload(fig, fmt=fmt) for fig in figs]
        payload = {
            "__wa_payload__": "multi_data",
            "items": items,
        }
        note = f"Captured {len(figs)} open figures.\n"
    else:
        payload = _figure_to_image_payload(figs[0], fmt=fmt)
    plt_mod.close("all")
    return payload, note


def _figure_to_image_payload(fig: Any, *, fmt: str = "svg") -> dict[str, Any]:
    """Render a matplotlib Figure to an image payload envelope.

    *fmt* ``"svg"`` (default) produces resolution-independent vector graphics that
    render crisply at any zoom in LibreOffice Calc/Writer.  ``"png"`` produces a
    150 DPI raster, preferred when the consumer cannot handle SVG (e.g. chat HTML).
    """
    import io

    buf = io.BytesIO()
    if fmt == "svg":
        fig.savefig(buf, format="svg", bbox_inches="tight")
    else:
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    buf.seek(0)
    return {"__wa_payload__": "image", "format": fmt, "data": buf.read()}


def _pil_image_to_payload(img: Any) -> dict[str, Any]:
    """Convert a PIL Image to an image payload dict."""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"__wa_payload__": "image", "format": "png", "data": buf.getvalue()}


def _has_custom_serialize_objects(obj: Any) -> bool:
    mpl_fig = optional_module("matplotlib.figure")
    pd_mod = optional_module("pandas")
    pil_mod = optional_module("PIL.Image")

    custom_types = []
    if mpl_fig is not None:
        custom_types.append(mpl_fig.Figure)
    if pd_mod is not None:
        custom_types.extend([pd_mod.DataFrame, pd_mod.Series])
    if pil_mod is not None:
        custom_types.append(pil_mod.Image)

    if not custom_types:
        return False

    custom_tuple = tuple(custom_types)
    if isinstance(obj, custom_tuple):
        return True
    if isinstance(obj, (list, tuple)):
        return any(isinstance(x, custom_tuple) for x in obj)
    if isinstance(obj, dict):
        return any(isinstance(v, custom_tuple) for v in obj.values())
    return False


def _column_label(c: Any) -> str:
    """Flatten a pandas column label. MultiIndex tuples become ``A / x``, not a tuple repr."""
    if isinstance(c, tuple):
        return " / ".join(str(part) for part in c)
    return str(c)


def _dtype_kind(obj: Any) -> str | None:
    dtype = getattr(obj, "dtype", None)
    kind = getattr(dtype, "kind", None)
    return kind if isinstance(kind, str) else None


def _is_numeric_wire_kind(kind: str | None) -> bool:
    """True when ``astype(float64)`` on split_grid is correct.

    datetime64 (``M``) and timedelta64 (``m``) must not take that path — the cast
    is Unix-epoch units, not Calc serials or ISO text.
    """
    return kind in ("i", "u", "f", "b")


def _strip_datetime_tz(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _temporal_cell_to_stdlib(value: Any, pd_mod: Any) -> Any:
    """Convert pandas/numpy temporal values to stdlib types the host can pickle.

    LibreOffice's embedded Python has no pandas/numpy, so Timestamp/datetime64
    must not cross the Pickle5 boundary as native objects.
    """
    try:
        if pd_mod is not None and pd_mod.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, datetime.datetime):
        return _strip_datetime_tz(value).isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value
    to_pydt = getattr(value, "to_pydatetime", None)
    if callable(to_pydt):
        try:
            dt = to_pydt()
            if isinstance(dt, datetime.datetime):
                return _strip_datetime_tz(dt).isoformat()
            if isinstance(dt, datetime.date):
                return dt.isoformat()
            return dt
        except Exception:
            pass
    to_pytd = getattr(value, "to_pytimedelta", None)
    if callable(to_pytd):
        try:
            return to_pytd()
        except Exception:
            pass
    kind = _dtype_kind(value)
    if kind == "M":
        try:
            if pd_mod is not None:
                ts = pd_mod.Timestamp(value)
                if pd_mod.isna(ts):
                    return None
                return _strip_datetime_tz(ts.to_pydatetime()).isoformat()
        except Exception:
            pass
        text = str(value)
        return None if text == "NaT" else text
    if kind == "m":
        try:
            if pd_mod is not None:
                td = pd_mod.Timedelta(value)
                if pd_mod.isna(td):
                    return None
                return td.to_pytimedelta()
        except Exception:
            pass
        item = getattr(value, "item", None)
        if callable(item):
            try:
                py_item = item()
                if isinstance(py_item, datetime.timedelta):
                    return py_item
            except Exception:
                pass
    return value


def _temporal_ndarray_to_python(arr: Any, pd_mod: Any) -> Any:
    """datetime64/timedelta64 ndarray → nested Python lists of stdlib values."""
    if arr.ndim == 0:
        return _temporal_cell_to_stdlib(arr.item() if hasattr(arr, "item") else arr, pd_mod)
    # Iterate datetime64 scalars — .tolist() on datetime64[ns] yields Python ints (ns), not datetimes.
    flat = [_temporal_cell_to_stdlib(v, pd_mod) for v in arr.ravel()]
    if arr.ndim == 1:
        return flat
    nrows, ncols = int(arr.shape[0]), int(arr.shape[1])
    return [flat[i * ncols : (i + 1) * ncols] for i in range(nrows)]


def _serialize_result_impl(obj: Any) -> Any:
    from plugin.scripting.calc_range import CalcRange, is_calc_range_payload

    if isinstance(obj, CalcRange):
        # Bugfix (#412): Returning a 1x1 CalcRange (e.g. result = data in fan-out DAGs)
        # unrolls to a scalar so the host does not treat it as a matrix list result
        # and walk MATRIX_SCALAR_SESSIONS. Multi-cell ranges echo values.
        if obj.shape == (1, 1) and obj.values and obj.values[0]:
            return _serialize_result_impl(obj.values[0][0])
        return child_pack_result(obj.values)
    if is_calc_range_payload(obj):
        return obj
    mpl_fig = optional_module("matplotlib.figure")
    if mpl_fig is not None and isinstance(obj, mpl_fig.Figure):
        return _figure_to_image_payload(obj)
    pil_mod = optional_module("PIL.Image")
    if pil_mod is not None and isinstance(obj, pil_mod.Image):
        return _pil_image_to_payload(obj)
    np_mod = optional_module("numpy")
    pd_mod = optional_module("pandas")
    if np_mod is not None:
        if isinstance(obj, np_mod.ndarray):
            kind = _dtype_kind(obj)
            if kind in ("M", "m"):
                return child_pack_result(_temporal_ndarray_to_python(obj, pd_mod))
            return child_pack_result(obj)
        if isinstance(obj, (np_mod.integer, np_mod.floating, np_mod.bool_)):
            return child_pack_result(obj)
        if isinstance(obj, np_mod.datetime64):
            return _temporal_cell_to_stdlib(obj, pd_mod)
        if isinstance(obj, np_mod.timedelta64):
            return _temporal_cell_to_stdlib(obj, pd_mod)
    if pd_mod is not None:
        if isinstance(obj, pd_mod.DataFrame):
            df: Any = obj
            columns = [_column_label(c) for c in df.columns]
            def _dataframe_cell(value: Any) -> Any:
                return _temporal_cell_to_stdlib(value, pd_mod)

            # Build rectangular data for packing: ndarray fast path for homogeneous numeric;
            # list-of-lists for mixed so strings/None go through the split_grid strings map
            # instead of the old per-row to_dict("records") which defeated binary envelopes.
            # datetime64/timedelta64 skip the numeric path — astype(float64) is Unix epoch, not ISO.
            if len(df) == 0 or len(df.columns) == 0:
                data_part: Any = []
            else:
                try:
                    arr = df.to_numpy(copy=False)
                    kind = _dtype_kind(arr)
                    if kind is not None and _is_numeric_wire_kind(kind):
                        data_part = child_pack_result(arr)
                    else:
                        grid = [[_dataframe_cell(cell) for cell in row] for row in df.itertuples(index=False, name=None)]
                        data_part = child_pack_result(grid)
                except Exception:
                    grid = [[_dataframe_cell(cell) for cell in row] for row in df.itertuples(index=False, name=None)]
                    data_part = child_pack_result(grid)
            return {
                "__wa_payload__": PAYLOAD_DATAFRAME,
                "columns": columns,
                "data": data_part,
            }
        if isinstance(obj, pd_mod.Series):
            s: Any = obj
            name = getattr(s, "name", None)
            if len(s) == 0:
                packed: Any = []
            else:
                try:
                    arr = s.to_numpy(copy=False)
                    kind = _dtype_kind(arr)
                    if kind is not None and _is_numeric_wire_kind(kind):
                        packed = child_pack_result(arr)
                    else:
                        packed = child_pack_result([_temporal_cell_to_stdlib(v, pd_mod) for v in s.tolist()])
                except Exception:
                    packed = child_pack_result([_temporal_cell_to_stdlib(v, pd_mod) for v in s.tolist()])
            if name is not None:
                return {
                    "__wa_payload__": PAYLOAD_DATAFRAME,
                    "columns": [_column_label(name)],
                    "data": packed,
                }
            return packed
    if isinstance(obj, (dict, list, tuple)):
        if _has_custom_serialize_objects(obj):
            if isinstance(obj, dict):
                return {str(k): serialize_result(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [serialize_result(v) for v in obj]
            else:
                return tuple(serialize_result(v) for v in obj)
        return child_pack_result(obj)
    return obj



def _new_executor(timeout_sec: int) -> LocalPythonExecutor:
    executor = LocalPythonExecutor(
        additional_authorized_imports=list(VENV_AUTHORIZED_IMPORTS),
        timeout_seconds=timeout_sec,
    )
    # Upstream only merges BASE_PYTHON_TOOLS (sum, len, …) after send_tools(); without this,
    # static_tools stays None and builtins like sum() are rejected.
    executor.send_tools({})
    return executor


def _get_or_create_session_executor(session_id: str, timeout_sec: int) -> LocalPythonExecutor:
    with _SESSION_LOCK:
        executor = _SESSION_EXECUTORS.get(session_id)
        if executor is None:
            executor = _new_executor(timeout_sec)
            _SESSION_EXECUTORS[session_id] = executor
        else:
            executor.timeout_seconds = timeout_sec
        return executor


def _related_init_session_id(session_id: str) -> str | None:
    """Return ``calc:…:init`` companion for a ``calc:…`` workbook session, if applicable."""
    if session_id.startswith("calc:") and not session_id.endswith(":init"):
        return f"{session_id}:init"
    return None


def _cell_session_for_init(init_session_id: str) -> str | None:
    if init_session_id.endswith(":init"):
        return init_session_id[: -len(":init")]
    return None


def _clear_init_session_unlocked(init_session_id: str) -> None:
    cell_sid = _cell_session_for_init(init_session_id)
    _SESSION_EXECUTORS.pop(init_session_id, None)
    _INIT_SCRIPT_HASH.pop(init_session_id, None)
    if cell_sid:
        _SESSION_EXECUTORS.pop(cell_sid, None)
        _CELL_SESSION_INIT_DIGEST.pop(cell_sid, None)


def reset_sandbox_session(session_id: str) -> dict[str, Any]:
    """Drop the persistent executor for *session_id* (idempotent).

    Also clears the workbook's ``:init`` session when resetting a ``calc:…`` cell session.
    """
    if not (session_id or "").strip():
        return {"status": "error", "message": "No session_id provided."}
    with _SESSION_LOCK:
        _SESSION_EXECUTORS.pop(session_id, None)
        init_sid = _related_init_session_id(session_id)
        if init_sid:
            _SESSION_EXECUTORS.pop(init_sid, None)
            _INIT_SCRIPT_HASH.pop(init_sid, None)
        if session_id.endswith(":init"):
            _INIT_SCRIPT_HASH.pop(session_id, None)
        _CELL_SESSION_INIT_DIGEST.pop(session_id, None)
    return {"status": "ok"}


def clear_all_sandbox_sessions() -> None:
    """Clear every cached session executor (tests)."""
    with _SESSION_LOCK:
        _SESSION_EXECUTORS.clear()
        _INIT_SCRIPT_HASH.clear()
        _CELL_SESSION_INIT_DIGEST.clear()


def _snapshot_init_bindings(init_session_id: str) -> dict[str, Any]:
    """Copy user-visible names from the init executor (references, not deep copies)."""
    with _SESSION_LOCK:
        executor = _SESSION_EXECUTORS.get(init_session_id)
    if executor is None:
        return {}
    return {
        key: value
        for key, value in executor.state.items()
        if key not in _INIT_STATE_SKIP_KEYS and not (isinstance(key, str) and key.startswith("_"))
    }


def _snapshot_init_custom_tools(init_session_id: str) -> dict[str, Any]:
    """Copy user-defined helper functions (custom tools) from the init executor."""
    with _SESSION_LOCK:
        executor = _SESSION_EXECUTORS.get(init_session_id)
    if executor is None:
        return {}
    return dict(executor.custom_tools)


def _seed_executor_from_init(executor: LocalPythonExecutor, init_session_id: str) -> None:
    bindings = _snapshot_init_bindings(init_session_id)
    if bindings:
        executor.send_variables(bindings)
    custom_tools = _snapshot_init_custom_tools(init_session_id)
    if custom_tools:
        executor.custom_tools.update(custom_tools)
        executor.state.update(custom_tools)




def _ensure_init_executed(
    init_session_id: str,
    init_script: str,
    *,
    timeout_sec: int,
    init_script_hash: str | None = None,
) -> dict[str, Any] | None:
    """Run *init_script* once in the persistent init session. Returns error dict or None."""
    script = (init_script or "").strip()
    if not script:
        return None

    digest = init_script_hash or ""
    with _SESSION_LOCK:
        prior = _INIT_SCRIPT_HASH.get(init_session_id)
        if prior is not None and prior != digest:
            _clear_init_session_unlocked(init_session_id)
        elif prior == digest and init_session_id in _SESSION_EXECUTORS:
            return None

    init_executor = _get_or_create_session_executor(init_session_id, timeout_sec)
    inject_auto_imports(init_executor, script)
    result = _run_on_executor(init_executor, script)
    if result.get("status") != "ok":
        with _SESSION_LOCK:
            _SESSION_EXECUTORS.pop(init_session_id, None)
            _INIT_SCRIPT_HASH.pop(init_session_id, None)
        return result


    with _SESSION_LOCK:
        _INIT_SCRIPT_HASH[init_session_id] = digest
    return None


def _inject_excel_xl(executor: LocalPythonExecutor, ranges: tuple[Any, ...] | None = None) -> None:
    """Inject binding-only Excel ``xl()`` closed over *ranges* (may be empty)."""
    from plugin.scripting.excel_xl import make_xl

    executor.send_variables({"xl": make_xl(ranges)})


def _inject_data(executor: LocalPythonExecutor, data: Any | None) -> tuple[Any, ...]:
    """Inject ``ranges`` (always a list) and polymorphic ``data``.

    * One formula arg: ``data`` is that ``CalcRange``; ``ranges == [data]``.
    * Two or more: ``data`` is the same list object as ``ranges``.

    Returns the materialized ranges tuple (empty when *data* is None) so callers
    can bind Excel ``xl()`` to the same ranges.
    """
    if data is None:
        executor.send_variables({"data": None, "ranges": []})
        return ()
    from plugin.scripting.calc_range import materialize_inputs
    from plugin.scripting.payload_codec import describe_wire_value, is_calc_range_payload, is_multi_data, is_split_grid

    if is_split_grid(data) or is_calc_range_payload(data) or is_multi_data(data):
        log.debug("venv_sandbox injecting data %s", describe_wire_value(data))

    ranges = materialize_inputs(data)

    ranges_list = list(ranges)
    if len(ranges_list) == 1:
        data_var: Any = ranges_list[0]
    elif len(ranges_list) >= 2:
        # Same object so ``data is ranges`` under multi-range.
        data_var = ranges_list
    else:
        data_var = None
    variables: dict[str, Any] = {
        "data": data_var,
        "ranges": ranges_list,
    }
    executor.send_variables(variables)
    return ranges


def _inject_bindings(executor: LocalPythonExecutor, bindings: dict[str, Any] | None) -> None:
    """Inject host-provided named values (e.g. selected image bytes) into the sandbox namespace."""
    if not bindings:
        return
    executor.send_variables(dict(bindings))


_RESULT_MISSING = object()


def _run_on_executor(executor: LocalPythonExecutor, code: str) -> dict[str, Any]:
    # Bugfix (#388): shared-kernel leftover ``result`` was used as egress for later
    # last-expression cells. Popping ``result`` after every cell (or before the next)
    # stopped the hijack but also made ``result * 2`` in a later cell NameError.
    # Fix: keep ``result`` in the namespace; use it for egress only when this cell
    # rebound it (identity change). On failure, restore the pre-cell value.
    prior_result = executor.state.get("result", _RESULT_MISSING)
    try:
        code_output = executor(code)


        current = executor.state.get("result", _RESULT_MISSING)
        if current is not _RESULT_MISSING and current is not prior_result:
            result = current
        else:
            result = code_output.output

        serialized = serialize_result(result)

        extra_stdout = ""
        if not find_image_payloads(serialized):
            captured, note = _capture_open_figures_payload()
            if captured is not None:
                serialized = captured
                extra_stdout = note
        else:
            plt_mod = optional_module("matplotlib.pyplot")
            if plt_mod is not None:
                plt_mod.close("all")

        if is_split_grid(serialized):
            log.debug("venv_sandbox worker result %s", describe_wire_value(serialized))
        stdout = (code_output.logs or "") + extra_stdout
        return {
            "status": "ok",
            "result": serialized,
            "stdout": stdout,
        }
    except InterpreterError as e:
        _restore_prior_result(executor, prior_result)
        return {
            "status": "error",
            "message": str(e),
            "stdout": str(executor.state.get("_print_outputs", "")),
        }
    except Exception as e:
        import traceback

        _restore_prior_result(executor, prior_result)
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
        }


def _restore_prior_result(executor: LocalPythonExecutor, prior_result: Any) -> None:
    """Drop a failed cell's partial ``result``; keep the last successful assignment."""
    if prior_result is _RESULT_MISSING:
        executor.state.pop("result", None)
    else:
        executor.state["result"] = prior_result


def run_sandboxed_code(
    code: str,
    data: Any | None = None,
    *,
    bindings: dict[str, Any] | None = None,
    timeout_sec: int | None = None,
    session_id: str | None = None,
    init_script: str | None = None,
    init_session_id: str | None = None,
    init_script_hash: str | None = None,
) -> dict[str, Any]:
    """Run *code* in LocalPythonExecutor.

    Without *session_id*, each call uses a new namespace. With *session_id*, reuse one
    executor per id (shared kernel / workbook session).

    When *init_script* is set, it runs once in *init_session_id* (typically ``calc:…:init``).
    Isolated cell runs seed a fresh executor from that snapshot; shared kernel seeds the
    workbook session executor once, then reuses it for cell code.
    """
    if timeout_sec is None:
        timeout_sec = python_exec_timeout_default()

    # Force non-interactive backend so plt.show() doesn't block in the subprocess.
    mpl = optional_module("matplotlib")
    if mpl is not None and hasattr(mpl, "use"):
        try:
            mpl.use("Agg")
        except Exception:
            pass

    init_sid = init_session_id if isinstance(init_session_id, str) and init_session_id.strip() else None
    if init_sid and (init_script or "").strip():
        init_err = _ensure_init_executed(
            init_sid,
            init_script or "",
            timeout_sec=timeout_sec,
            init_script_hash=init_script_hash,
        )
        if init_err is not None:
            return init_err

    if session_id:
        executor = _get_or_create_session_executor(session_id, timeout_sec)
        if init_sid:
            _seed_executor_from_init(executor, init_sid)
    else:
        executor = _new_executor(timeout_sec)
        if init_sid:
            _seed_executor_from_init(executor, init_sid)

    for k, v in list(executor.state.items()):
        if callable(v) and k not in executor.custom_tools and not (isinstance(k, str) and k.startswith("_")):
            executor.custom_tools[k] = v


    inject_auto_imports(executor, code)
    ranges = _inject_data(executor, data)
    _inject_excel_xl(executor, ranges)
    _inject_bindings(executor, bindings)
    return _run_on_executor(executor, code)
