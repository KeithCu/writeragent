# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for =PYTHON() (Calc types, matrix session, return coercion)."""

from __future__ import annotations

import threading
from typing import Any

from plugin.calc.calc_addin_data import count_cells
from plugin.scripting.payload_codec import host_unpack_data, is_split_grid

# Calc legacy add-in bridge accepts scalar double/string returns only. List results are
# emitted one scalar per formula evaluation (matrix block or repeated recalc).
MATRIX_SCALAR_SESSIONS = threading.local()


def worker_result_for_calc(result: Any) -> Any:
    """Expand split_grid to nested lists for matrix/session flattening; pass scalars through."""
    if is_split_grid(result):
        return host_unpack_data(result, as_nested_list=True)
    return result


def flatten_result_values(result: Any) -> list:
    """Row-major flattening for list / nested list worker results."""
    result = worker_result_for_calc(result)
    if not isinstance(result, (list, tuple)):
        return [result]
    if not result:
        return []
    if isinstance(result[0], (list, tuple)):
        flat: list = []
        for row in result:
            flat.extend(row)
        return flat
    return list(result)


def is_scalar_index_arg(py_data: list | list[list] | None) -> bool:
    """True when arg 1 is one number (matrix index), not a data range."""
    if py_data is None:
        return False
    if count_cells(py_data) != 1:
        return False
    first = py_data[0]
    return not isinstance(first, (list, tuple))


# Tests and legacy imports
_is_scalar_index_arg = is_scalar_index_arg


def coerce_index(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return int(float(value))
    raise ValueError(f"index must be numeric, got {value!r}")


def to_calc_compatible(val: Any) -> float | str | bool | tuple:
    """Recursively convert Python values into LibreOffice Calc supported types.

    Calc cells only support float (UNO double), str (UNO string), and bool (UNO boolean).
    Crucially, Calc matrix formulas do NOT support integer (UNO long) types and will
    throw #VALUE! if a sequence contains integers/longs.
    """
    if val is None:
        return ""
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return float(val)
    if isinstance(val, float):
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        inner = val[0] if val else None
        if isinstance(inner, (list, tuple)):
            return tuple(tuple(to_calc_compatible(cell) for cell in row) for row in val)
        return tuple(to_calc_compatible(item) for item in val)
    return str(val)


def session_key(ctx: Any, code: str) -> tuple:
    doc_url = ""
    sheet_name = ""
    try:
        smgr = ctx.ServiceManager
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        doc = desktop.getCurrentComponent()
        if doc is not None:
            doc_url = getattr(doc, "getURL", lambda: "")() or ""
            ctrl = getattr(doc, "getCurrentController", lambda: None)()
            if ctrl is not None:
                sheet = ctrl.getActiveSheet()
                if sheet is not None:
                    sheet_name = sheet.getName()
    except Exception:
        pass
    return (doc_url, sheet_name, code)


class WorkerResultSession:
    """Caches one worker list result across multiple =PYTHON() calls in a recalc pass."""

    __slots__ = ("raw", "flat", "next_index")

    def __init__(self, raw: Any, flat: list) -> None:
        self.raw = raw
        self.flat = tuple(flat)
        self.next_index = 0


# Legacy alias for tests
_WorkerResultSession = WorkerResultSession


def scalar_for_list_result(ctx: Any, code: str, result: Any, *, worker_data: Any = None) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: list = [to_calc_compatible(v) for v in flatten_result_values(result)]
    if not flat:
        return ""
    key = (session_key(ctx, code), repr(worker_data))
    sessions = getattr(MATRIX_SCALAR_SESSIONS, "sessions", None)
    if sessions is None:
        sessions = {}
        MATRIX_SCALAR_SESSIONS.sessions = sessions
    state = sessions.get(key)
    if not isinstance(state, WorkerResultSession) or state.flat != tuple(flat):
        state = WorkerResultSession(result, flat)
        sessions[key] = state
    idx = state.next_index
    state.next_index = idx + 1
    if state.next_index >= len(state.flat):
        sessions.pop(key, None)
    if 0 <= idx < len(state.flat):
        return state.flat[idx]
    return state.flat[-1] if state.flat else ""


def finalize_python_return(
    ctx: Any,
    code: str,
    result: Any,
    *,
    index_arg: Any = None,
    worker_data: Any = None,
) -> float | str | bool | tuple:
    """Map worker result to a single value Calc's add-in bridge accepts."""
    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = flatten_result_values(result)
            idx = coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return to_calc_compatible(flat[idx])
        return scalar_for_list_result(ctx, code, result, worker_data=worker_data)
    return to_calc_compatible(result)
