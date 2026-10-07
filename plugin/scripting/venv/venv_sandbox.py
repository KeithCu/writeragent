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
import copy
import datetime
import decimal
from collections import OrderedDict
import fractions
import importlib
import logging
import math
import sys
import threading
import time
import types
from contextvars import ContextVar
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
from plugin.scripting.ipc import UserStopped
from plugin.framework.constants import AUTO_IMPORTS
from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS

# Shared-kernel executors keyed by workbook session_id (calc:…). Cleared on reset_session,
# document OnUnload (workbook_lifecycle), or worker process exit.
_SESSION_EXECUTORS: dict[str, LocalPythonExecutor] = {}
_SESSION_LOCK = threading.Lock()
_MAX_ISOLATED_INIT_SNAPSHOTS = 32
_ISOLATED_INIT_LRU: OrderedDict[str, None] = OrderedDict()


def _record_isolated_init_access_unlocked(init_session_id: str) -> None:
    _ISOLATED_INIT_LRU[init_session_id] = None
    _ISOLATED_INIT_LRU.move_to_end(init_session_id)
    while len(_ISOLATED_INIT_LRU) > _MAX_ISOLATED_INIT_SNAPSHOTS:
        oldest, _ = _ISOLATED_INIT_LRU.popitem(last=False)
        _SESSION_EXECUTORS.pop(oldest, None)
        _INIT_SCRIPT_HASH.pop(oldest, None)

# Cell / RPS session for the current execute. Isolated runs leave this None so
# DuckDB and similar caches stay per-request. Init-only ids are not stored here
# (``calc:…:init`` would otherwise leak a catalog across Isolated cells).
_CURRENT_SANDBOX_SESSION: ContextVar[str | None] = ContextVar(
    "sandbox_session_id", default=None
)
# Distinct from the session id: isolated executes set the id to None, and host
# callers never enter run_sandboxed_code. DuckDB uses this to refuse a cell
# that names another workbook's catalog.
_SANDBOX_EXECUTE: ContextVar[bool] = ContextVar("sandbox_execute", default=False)


def current_sandbox_session_id() -> str | None:
    """Workbook session id for this sandboxed execute, or ``None`` (isolated)."""
    return _CURRENT_SANDBOX_SESSION.get()


def sandbox_execute_active() -> bool:
    """True while ``run_sandboxed_code`` is on this thread (including isolated)."""
    return _SANDBOX_EXECUTE.get()


def _install_timeout_context_pool() -> None:
    """Copy sandbox ContextVars onto the SIGALRM fallback thread.

    What was wrong: ``local_python_executor.timeout`` runs the cell on a
    ``ThreadPoolExecutor`` worker when SIGALRM cannot be installed (Windows,
    or not the main thread). That worker starts with an empty context, so
    ``current_sandbox_session_id`` and ``sandbox_execute_active`` were the
    defaults and ``session_duckdb(other_id)`` opened another workbook.
    Why this works: the vendored timeout looks up ``ThreadPoolExecutor`` on
    its module when the fallback runs. Submit through ``copy_context().run``
    so the worker sees the session ``run_sandboxed_code`` just set. The
    vendored file stays unchanged; it must keep using that module global.
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from collections.abc import Callable
        from concurrent.futures import Future

    from plugin.contrib.smolagents import local_python_executor as lpe

    current = lpe.ThreadPoolExecutor
    if getattr(current, "_writeragent_copies_context", False):
        return

    class _ContextThreadPoolExecutor(ThreadPoolExecutor):
        _writeragent_copies_context: bool = True

        def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
            ctx = contextvars.copy_context()
            return super().submit(ctx.run, fn, *args, **kwargs)

    # The vendored name is the stdlib class. This subclass is what timeout()
    # constructs on the SIGALRM fallback path. setattr: mypy rejects assigning
    # over a class object ("Cannot assign to a type").
    setattr(lpe, "ThreadPoolExecutor", _ContextThreadPoolExecutor)


_install_timeout_context_pool()


def _reset_session_duckdb(session_id: str | None) -> None:
    """Close the Phase D DuckDB catalog for *session_id* (LibrePy has no module)."""
    try:
        from plugin.scripting.venv.duckdb_sql import reset_session_duckdb
    except ImportError:
        return
    reset_session_duckdb(session_id)


def _inject_session_duckdb(executor: LocalPythonExecutor) -> None:
    """Bind ``session_duckdb`` / ``run_sql`` / ``invalidate_session_tables`` when DuckDB helpers ship."""
    try:
        from plugin.scripting.venv.duckdb_sql import (
            invalidate_session_tables,
            run_sql,
            session_duckdb,
        )
    except ImportError:
        return

    # Bugfix: run_sql used to ignore its scoped_dir argument and then read
    # executor.state["scoped_dir"]. Bindings inject the host folder, but the
    # cell can assign scoped_dir (set_value writes that state) before calling
    # run_sql, and resolve_flat_file_path then accepted files under the
    # rewritten folder. Capture the host path at inject time — after bindings,
    # before user code — and do not consult state again.
    # run_sandboxed_code drops a previous execute's scoped_dir before bindings,
    # so this read is only the folder this execute actually bound.
    host_scoped_dir = executor.state.get("scoped_dir")
    if not isinstance(host_scoped_dir, str) or not host_scoped_dir.strip():
        host_scoped_dir = None

    def run_sql_bound(
        sql: str,
        con: Any | None = None,
        files: list[str] | dict[str, str] | None = None,
        scoped_dir: str | None = None,
        **kwargs: Any,
    ) -> Any:
        del scoped_dir
        return run_sql(sql, con, files, scoped_dir=host_scoped_dir, **kwargs)

    helpers = {
        "session_duckdb": session_duckdb,
        "invalidate_session_tables": invalidate_session_tables,
        "run_sql": run_sql_bound,
    }
    executor.send_variables(helpers)
    executor.custom_tools.update(helpers)


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
        "session_duckdb",  # rebound each execute; not an init-script binding
        "invalidate_session_tables",
        "run_sql",
        "scoped_dir",  # document folder; rebound each =PY() from the host
    }
)


def is_module_imported(code_str: str, module_name: str) -> bool:
    """Check if ``module_name`` is imported in any form in ``code_str``.

    Skip reusing sandbox_cache's parsed AST: cache misses and mutated trees
    make that easy to get wrong, and parse cost is noise compared with exec.
    """
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
    if name in sys.modules:
        mod = sys.modules[name]
        spec = getattr(mod, "__spec__", None)
        if spec is None or not getattr(spec, "_initializing", False):
            return mod
    with _OPTIONAL_MODULE_LOCK:
        if name in sys.modules:
            mod = sys.modules[name]
            spec = getattr(mod, "__spec__", None)
            if spec is None or not getattr(spec, "_initializing", False):
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


def _parse_bound_names(code_str: str) -> set[str]:
    """Return names bound (assigned, defined, or imported) in *code_str*."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        return set()

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
    return bound


def inject_auto_imports(executor: LocalPythonExecutor, code: str) -> None:
    """Inject auto imports into executor state if not already bound or imported in code."""
    bound_names = _parse_bound_names(code)
    bindings = {}
    for module_name, import_stmt in AUTO_IMPORTS.items():
        alias = import_stmt.split(" as ")[-1].strip() if " as " in import_stmt else module_name
        if alias in bound_names or alias in executor.state:
            continue
        if not is_module_imported(code, module_name):
            mod = optional_module(module_name)
            if mod is not None:
                bindings[alias] = mod
    if bindings:
        executor.send_variables(bindings)


# Leaves the host ``_SafeUnpickler`` accepts. Anything else is a script error,
# not a worker kill: a rejected global used to terminate every workbook session.
_HOST_PICKLE_LEAVES = (type(None), bool, int, float, str, bytes, bytearray, complex)


def _coerce_host_pickle_scalar(obj: Any, pd_mod: Any) -> Any:
    """Turn one value into a type LibreOffice's unpickler allows.

    ``to_calc_compatible`` on the host never ran for these: the frame failed
    to unpickle first. Timedelta uses Calc's fractional-day number (1.0 = 24h).
    """
    if isinstance(obj, _HOST_PICKLE_LEAVES):
        return obj
    if isinstance(obj, datetime.datetime):
        return _strip_datetime_tz(obj).isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, datetime.time):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds() / 86400.0
    if isinstance(obj, (decimal.Decimal, fractions.Fraction)):
        return float(obj)
    if isinstance(obj, range):
        return list(obj)
    converted = _temporal_cell_to_stdlib(obj, pd_mod)
    if isinstance(converted, datetime.timedelta):
        return converted.total_seconds() / 86400.0
    if isinstance(converted, (datetime.datetime, datetime.date, datetime.time)):
        return _coerce_host_pickle_scalar(converted, pd_mod)
    if converted is not obj:
        return converted
    # What was wrong: child_pack turns a bare np.int64 into a Python int, but a
    # grid of those scalars took the list path and skipped that. The boundary
    # check then rejected the grid. .item() is the Python value the host can unpickle.
    np_mod = optional_module("numpy")
    if np_mod is not None and isinstance(obj, np_mod.generic):
        try:
            plain = obj.item()
        except Exception:
            return obj
        if plain is not obj:
            return _coerce_host_pickle_scalar(plain, pd_mod)
    return obj


def _coerce_host_pickle_tree(obj: Any, pd_mod: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _coerce_host_pickle_tree(v, pd_mod) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_host_pickle_tree(v, pd_mod) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_coerce_host_pickle_tree(v, pd_mod) for v in obj)
    if isinstance(obj, set):
        return {_coerce_host_pickle_tree(v, pd_mod) for v in obj}
    if isinstance(obj, frozenset):
        return frozenset(_coerce_host_pickle_tree(v, pd_mod) for v in obj)
    scalar = _coerce_host_pickle_scalar(obj, pd_mod)
    if scalar is not obj and isinstance(scalar, (list, tuple, dict, set, frozenset)):
        return _coerce_host_pickle_tree(scalar, pd_mod)
    return scalar


def _reject_host_unpickleable(obj: Any, *, depth: int = 0) -> None:
    """Raise when *obj* would be a hostile frame on the host unpickler.

    The child can pickle many globals. The host only rebuilds builtins and a
    few NumPy reconstructors, and a ``ValueError`` there kills the worker.
    """
    if depth > 64:
        raise ValueError("Result is too deeply nested to cross the LibreOffice pickle boundary")
    if isinstance(obj, _HOST_PICKLE_LEAVES):
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            _reject_host_unpickleable(key, depth=depth + 1)
            _reject_host_unpickleable(value, depth=depth + 1)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for value in obj:
            _reject_host_unpickleable(value, depth=depth + 1)
        return
    raise ValueError(
        f"Result type {type(obj).__module__}.{type(obj).__name__} "
        "cannot cross the LibreOffice pickle boundary"
    )


def serialize_result(obj: Any) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values (split_grid for large numeric/mixed arrays).

    DataFrames (and named Series) are returned as a dataframe envelope with 'columns' and 'data'
    (the latter is a split_grid envelope when large enough, or nested lists). This replaces the
    previous to_dict(orient="records") path which produced expensive list-of-dicts and bypassed
    the binary grid fast path.
    """
    try:
        out = _serialize_result_impl(obj)
        # A type we did not convert must not leave the child: the host treats
        # the unpickle error as a bad frame and restarts every workbook.
        _reject_host_unpickleable(out)
        return out
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


# One container level missed {"sheets": [df, df]} and [{"stats": df}]. Those
# took child_pack_result, which raises ValueError and drops a successful cell.
# Deeper than this is treated as a plain container (child_pack / pickle reject).
_CUSTOM_SERIALIZE_MAX_DEPTH = 8


def _custom_serialize_types() -> tuple[type, ...]:
    mpl_fig = optional_module("matplotlib.figure")
    pd_mod = optional_module("pandas")
    pil_mod = optional_module("PIL.Image")
    np_mod = optional_module("numpy")
    custom_types: list[type] = []
    if mpl_fig is not None:
        custom_types.append(mpl_fig.Figure)
    if pd_mod is not None:
        custom_types.extend([pd_mod.DataFrame, pd_mod.Series])
    if pil_mod is not None:
        custom_types.append(pil_mod.Image)
    if np_mod is not None:
        custom_types.append(np_mod.ndarray)
    return tuple(custom_types)


def _contains_custom_serialize(obj: Any, custom_tuple: tuple[type, ...], depth: int) -> bool:
    if isinstance(obj, custom_tuple):
        return True
    if depth >= _CUSTOM_SERIALIZE_MAX_DEPTH:
        return False
    if isinstance(obj, (list, tuple, set, frozenset)):
        return any(_contains_custom_serialize(item, custom_tuple, depth + 1) for item in obj)
    if isinstance(obj, dict):
        return any(_contains_custom_serialize(value, custom_tuple, depth + 1) for value in obj.values())
    return False


def _has_custom_serialize_objects(obj: Any) -> bool:
    custom_tuple = _custom_serialize_types()
    if not custom_tuple:
        return False
    return _contains_custom_serialize(obj, custom_tuple, 0)


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
    if isinstance(obj, (dict, list, tuple, set, frozenset)):
        if _has_custom_serialize_objects(obj):
            if isinstance(obj, dict):
                return {str(k): serialize_result(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [serialize_result(v) for v in obj]
            elif isinstance(obj, set):
                return {serialize_result(v) for v in obj}
            elif isinstance(obj, frozenset):
                return frozenset(serialize_result(v) for v in obj)
            else:
                return tuple(serialize_result(v) for v in obj)
        # Short lists skip split_grid and are pickled as Python objects. A date
        # or Decimal in that list is a datetime/decimal global the host unpickler
        # rejects, which used to kill the shared worker.
        return child_pack_result(_coerce_host_pickle_tree(obj, pd_mod))
    return _coerce_host_pickle_scalar(obj, pd_mod)



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
    """Return the ``{id}:init`` companion for a cell session.

    Desktop workbooks use ``calc:…``. The compute service uses the raw Online
    session id. Both store the init executor at ``{id}:init``. Reset used to
    drop that companion only for ``calc:`` ids, so an Online reset left the
    pre-reset snapshot and the next cell seeded from it.
    """
    if session_id.endswith(":init"):
        return None
    return f"{session_id}:init"


def _cell_session_for_init(init_session_id: str) -> str | None:
    if init_session_id.endswith(":init"):
        return init_session_id[: -len(":init")]
    return None


def _clear_init_session_unlocked(init_session_id: str) -> None:
    cell_sid = _cell_session_for_init(init_session_id)
    _SESSION_EXECUTORS.pop(init_session_id, None)
    _INIT_SCRIPT_HASH.pop(init_session_id, None)
    if init_session_id.startswith("isolated:"):
        _ISOLATED_INIT_LRU.pop(init_session_id, None)
    _reset_session_duckdb(init_session_id)
    if cell_sid:
        _SESSION_EXECUTORS.pop(cell_sid, None)
        _CELL_SESSION_INIT_DIGEST.pop(cell_sid, None)
        # Init-hash change drops the workbook kernel; DuckDB tables must go too.
        _reset_session_duckdb(cell_sid)


def reset_sandbox_session(session_id: str) -> dict[str, Any]:
    """Drop any cached executor for *session_id* and reset DuckDB tables.

    Also clears the ``{id}:init`` companion when *session_id* is a cell id,
    and clears the cell session companion when *session_id* is an init id.
    """
    if not (session_id or "").strip():
        return {"status": "error", "message": "No session_id provided."}
    with _SESSION_LOCK:
        if session_id.endswith(":init"):
            _clear_init_session_unlocked(session_id)
        else:
            _SESSION_EXECUTORS.pop(session_id, None)
            _CELL_SESSION_INIT_DIGEST.pop(session_id, None)
            init_sid = _related_init_session_id(session_id)
            if init_sid:
                _clear_init_session_unlocked(init_sid)
    _reset_session_duckdb(session_id)
    return {"status": "ok"}


def clear_all_sandbox_sessions() -> None:
    """Clear every cached session executor (tests)."""
    with _SESSION_LOCK:
        _SESSION_EXECUTORS.clear()
        _INIT_SCRIPT_HASH.clear()
        _CELL_SESSION_INIT_DIGEST.clear()
        _ISOLATED_INIT_LRU.clear()
    _reset_session_duckdb(None)


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


def _copy_isolated_seed_value(value: Any) -> Any:
    """Duplicate an init binding so isolated cells cannot mutate the workbook seed.

    Cell 1 wrote ``items.append(...)`` without reassigning ``items``; that
    in one isolated cell changed what every later isolated cell on that worker
    saw. Functions and modules stay shared; deepcopy rejects them. Shared-kernel
    seeding does not use this — that workbook is one namespace.

    A lazy copy-on-demand or size guard would be too complex and prone to edge
    cases, so a simple deepcopy is used here on every cell execution for safety.
    """
    if callable(value) or isinstance(value, types.ModuleType):
        return value
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _seed_executor_from_init(executor: LocalPythonExecutor, init_session_id: str, *, copy_values: bool = False) -> None:
    bindings = _snapshot_init_bindings(init_session_id)
    if copy_values and bindings:
        bindings = {key: _copy_isolated_seed_value(value) for key, value in bindings.items()}
    if bindings:
        executor.send_variables(bindings)
    custom_tools = _snapshot_init_custom_tools(init_session_id)
    if custom_tools:
        executor.custom_tools.update(custom_tools)
        executor.state.update(custom_tools)


def _seed_shared_executor_once(
    executor: LocalPythonExecutor,
    session_id: str,
    init_session_id: str,
    init_script_hash: str | None,
) -> None:
    """Copy init bindings into a shared executor once per init digest.

    ``send_variables`` is ``state.update``. Seeding on every cell overwrote
    names the cell had rebound (init ``FACTOR = 10``, cell ``FACTOR = 99``,
    the next cell saw 10). Init-script edits already drop the cell executor
    in ``_clear_init_session_unlocked``, so a new digest seeds a fresh one.
    Isolated cells have no ``session_id`` and still seed on every run.
    """
    digest = init_script_hash or ""
    with _SESSION_LOCK:
        if _CELL_SESSION_INIT_DIGEST.get(session_id) == digest:
            return
    _seed_executor_from_init(executor, init_session_id)
    with _SESSION_LOCK:
        _CELL_SESSION_INIT_DIGEST[session_id] = digest




def _seconds_left(deadline: float) -> int | None:
    """Whole seconds still inside *deadline*, or None when the budget is spent.

    ``signal.alarm`` is one-second granularity and ``alarm(0)`` cancels, so a
    leftover fraction of a second still uses 1. The host read adds
    ``HOST_IPC_READ_GRACE_SEC`` and still wins if the child never returns.
    """
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    return max(1, math.ceil(left))


def _budget_timeout_error(timeout_sec: int) -> dict[str, Any]:
    return {
        "status": "error",
        "message": (
            f"Code execution exceeded the maximum execution time of {timeout_sec} seconds"
        ),
    }


def _ensure_init_executed(
    init_session_id: str,
    init_script: str,
    *,
    timeout_sec: int,
    deadline: float,
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
            if init_session_id.startswith("isolated:"):
                _record_isolated_init_access_unlocked(init_session_id)
            return None

    init_executor = _get_or_create_session_executor(init_session_id, timeout_sec)
    # Init and the cell used to each get a fresh alarm for the full budget, so
    # a slow init plus a slow cell outlived the single host read and killed
    # the worker (every other workbook on that process).
    left = _seconds_left(deadline)
    if left is None:
        return _budget_timeout_error(timeout_sec)
    init_executor.timeout_seconds = left
    inject_auto_imports(init_executor, script)
    result = _run_on_executor(init_executor, script)
    if result.get("status") != "ok":
        with _SESSION_LOCK:
            _clear_init_session_unlocked(init_session_id)
        return result

    with _SESSION_LOCK:
        _INIT_SCRIPT_HASH[init_session_id] = digest
        if init_session_id.startswith("isolated:"):
            _record_isolated_init_access_unlocked(init_session_id)
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
# Intentional check-then-set with no lock: LibrePy/WriterAgent venv workers are
# one thread per process (execution is serialized; ``_SESSION_LOCK`` is for
# session state, not this). ``matplotlib.use("Agg")`` twice is harmless. Do not
# add a threading.Lock here unless the worker becomes multi-threaded; then this
# flag needs a lock (or to move under ``_SESSION_LOCK``).
_MPL_AGG_SET = False
_MPL_AGG_FAILED = False


def _ensure_mpl_agg() -> None:
    global _MPL_AGG_SET, _MPL_AGG_FAILED
    if _MPL_AGG_SET or _MPL_AGG_FAILED:
        return
    mpl = optional_module("matplotlib")
    if mpl is not None and hasattr(mpl, "use"):
        try:
            mpl.use("Agg")
            _MPL_AGG_SET = True
        except Exception:
            _MPL_AGG_FAILED = True
    else:
        _MPL_AGG_FAILED = True


def _sync_custom_tools(executor: LocalPythonExecutor) -> None:
    """Sync newly bound user functions from state into custom_tools."""
    for k, v in executor.state.items():
        if callable(v) and k not in executor.custom_tools and not (isinstance(k, str) and k.startswith("_")):
            executor.custom_tools[k] = v


def _is_defined_function(obj: Any) -> bool:
    return isinstance(obj, (types.FunctionType, types.BuiltinFunctionType, types.BuiltinMethodType, types.MethodType))


def _is_mpl_artist_result(obj: Any) -> bool:
    """True for a pyplot artist or the list ``plt.plot`` returns."""
    artist_mod = optional_module("matplotlib.artist")
    if artist_mod is None:
        return False
    artist = artist_mod.Artist
    if isinstance(obj, artist):
        return True
    return isinstance(obj, (list, tuple)) and bool(obj) and all(isinstance(item, artist) for item in obj)


def _close_open_figures() -> None:
    plt_mod = optional_module("matplotlib.pyplot")
    if plt_mod is None:
        return
    try:
        if plt_mod.get_fignums():
            plt_mod.close("all")
    except Exception:
        log.debug("failed to close pyplot figures", exc_info=True)


def _run_on_executor(executor: LocalPythonExecutor, code: str) -> dict[str, Any]:
    # Bugfix (#388): shared-kernel leftover ``result`` was used as egress for later
    # last-expression cells. Popping ``result`` after every cell (or before the next)
    # stopped the hijack but also made ``result * 2`` in a later cell NameError.
    # Fix: keep ``result`` in the namespace; use it for egress only when this cell
    # rebound it (identity change). On failure, restore the pre-cell value.
    prior_result = executor.state.get("result", _RESULT_MISSING)
    try:
        from plugin.scripting.named_scripts import bind_named_scripts_executor

        bind_named_scripts_executor(executor)
        code_output = executor(code)
        _sync_custom_tools(executor)

        current = executor.state.get("result", _RESULT_MISSING)
        if current is not _RESULT_MISSING and current is not prior_result:
            result = current
        else:
            result = code_output.output

        # What was wrong: a helper-only init script evaluates to the function,
        # and plt.plot() evaluates to Line2D artists. The pickle check rejected
        # both before open figures were captured, and those figures stayed open
        # so the next script returned that SVG instead of its own value.
        if _is_defined_function(result):
            result = None

        extra_stdout = ""
        if _is_mpl_artist_result(result):
            captured, note = _capture_open_figures_payload()
            if captured is None:
                serialized = serialize_result(result)
            else:
                serialized = captured
                extra_stdout = note
        else:
            serialized = serialize_result(result)
            if not find_image_payloads(serialized):
                captured, note = _capture_open_figures_payload()
                if captured is not None:
                    serialized = captured
                    extra_stdout = note
            else:
                _close_open_figures()

        if is_split_grid(serialized):
            log.debug("venv_sandbox worker result %s", describe_wire_value(serialized))
        stdout = (code_output.logs or "") + extra_stdout
        return {
            "status": "ok",
            "result": serialized,
            "stdout": stdout,
        }
    except UserStopped as e:
        # What was wrong: exchange_tool_call turned host USER_STOPPED into
        # RuntimeError. evaluate_try catches Exception, so the script kept
        # running and issued more wa.* calls after Stop.
        # Why this works: UserStopped is BaseException, so that handler does
        # not run. End the turn with the code the host already sent.
        _restore_prior_result(executor, prior_result)
        _close_open_figures()
        return {
            "status": "error",
            "code": "USER_STOPPED",
            "message": str(e) or "Stopped by user.",
            "stdout": "",
        }
    except InterpreterError as e:
        _restore_prior_result(executor, prior_result)
        _close_open_figures()
        return {
            "status": "error",
            "message": str(e),
            "stdout": str(executor.state.get("_print_outputs", "")),
        }
    except Exception as e:
        import traceback

        _restore_prior_result(executor, prior_result)
        _close_open_figures()
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
    Isolated cell runs seed a fresh executor from a copy of that snapshot; shared kernel
    seeds the workbook session executor once, then reuses it for cell code.
    """
    if timeout_sec is None:
        timeout_sec = python_exec_timeout_default()
    deadline = time.monotonic() + float(timeout_sec)

    # Force non-interactive backend so plt.show() doesn't block in the subprocess.
    _ensure_mpl_agg()

    # Only the cell / RPS session_id is persistable. Isolated cells still have
    # init_session_id (calc:…:init); binding that would share DuckDB across cells.
    active_token = _SANDBOX_EXECUTE.set(True)
    token = _CURRENT_SANDBOX_SESSION.set(session_id)
    try:
        init_sid = init_session_id if isinstance(init_session_id, str) and init_session_id.strip() else None
        if init_sid and (init_script or "").strip():
            init_err = _ensure_init_executed(
                init_sid,
                init_script or "",
                timeout_sec=timeout_sec,
                deadline=deadline,
                init_script_hash=init_script_hash,
            )
            if init_err is not None:
                return init_err

        if session_id:
            executor = _get_or_create_session_executor(session_id, timeout_sec)
            if init_sid:
                _seed_shared_executor_once(executor, session_id, init_sid, init_script_hash)
        else:
            executor = _new_executor(timeout_sec)
            if init_sid:
                _seed_executor_from_init(executor, init_sid, copy_values=True)

        left = _seconds_left(deadline)
        if left is None:
            return _budget_timeout_error(timeout_sec)
        executor.timeout_seconds = left

        inject_auto_imports(executor, code)
        ranges = _inject_data(executor, data)
        _inject_excel_xl(executor, ranges)
        # Bugfix: a shared calc: executor is reused by =PY() and Run Python
        # Script. The cell assignment scoped_dir = "/some/dir" stayed in
        # state. The next execute that did not bind a folder (RPS injects
        # none) re-read that path as the host folder. Drop it before bindings
        # so only this execute's host value is visible.
        executor.state.pop("scoped_dir", None)
        _inject_bindings(executor, bindings)
        _inject_session_duckdb(executor)
        return _run_on_executor(executor, code)
    finally:
        _CURRENT_SANDBOX_SESSION.reset(token)
        _SANDBOX_EXECUTE.reset(active_token)
