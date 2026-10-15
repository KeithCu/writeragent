# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PY() execution and return helpers (venv worker); no LLM imports."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime
import hashlib
import logging
import math
import re
import struct
import threading
import time
from typing import Any, ClassVar, Iterator, cast

import unohelper
from com.sun.star.util import XModifyListener

from plugin.calc.calc_addin_data import calc_addin_args_from_split, check_python_data_size, check_python_multi_data_size, count_cells, pack_calc_data_for_wire, pack_calc_multi_data_for_wire, split_python_addin_data_args
from plugin.calc.datetime_wire import coalesce_temporal_apply_rects, duration_serial_from_iso, match_iso_duration, match_iso_temporal, should_preserve_temporal_format
from plugin.calc.inspector import _format_category_from_type
from plugin.calc.python.formula_locator_cache import is_matching_py_formula, locate_formula_cell_in_doc
from plugin.calc.python.image_egress import insert_image_result_on_sheet
from plugin.framework.errors import format_error_message
from plugin.framework.i18n import _
from plugin.framework.thread_guard import sync_host_dispatch

from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import is_dataframe_payload, is_split_grid, find_image_payloads
from plugin.scripting.calc_range import dataframe_to_labeled_grid
from plugin.scripting.session_manager import workbook_session_id
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)

# Calc legacy add-in bridge accepts scalar double/string returns only. List results are
# emitted one scalar per formula evaluation (matrix block or repeated recalc).
# Session keys are (threading.get_ident(), session_key(...), sha256 hex from _worker_data_token).
# One evaluation passes the digest into scalar_for_list_result, and there is no cross-cell
# token cache. Two formulas with the same code but different data must stay on separate
# sessions (see tests/calc/python/test_function.py).
_MATRIX_SCALAR_SESSIONS_LOCK = threading.Lock()
_MATRIX_SCALAR_SESSIONS: dict[tuple[int, tuple[str, ...], str], WorkerResultSession] = {}


# Recalc-clump timings for DEBUG ``py_timing`` lines (not asctime deltas).
# Flip to True in this file when measuring workbook-open / recalc cost; leave False in commits.
PYTHON_TIMINGS_LOG = False
_PY_PASS_STATS = threading.local()
_PY_PASS_GAP_SEC = 2.0
_PY_HELPER_IN_SPEC_RE = re.compile(r"""["']helper["']\s*:\s*["'](\w+)["']""")


def flatten_result_values(result: Any) -> list[Any]:
    """Row-major flattening for list / nested list worker results."""
    if not isinstance(result, (list, tuple)):
        return [result]
    if not result:
        return []
    if any(isinstance(row, (list, tuple)) for row in result):
        flat: list[Any] = []
        for row in result:
            if isinstance(row, (list, tuple)):
                flat.extend(row)
            else:
                flat.append(row)
        return flat
    return list(result)


def is_scalar_index_arg(py_data: list[Any] | list[list[Any]] | None) -> bool:
    """True when arg 1 is one number (matrix index), not a data range."""
    if py_data is None:
        return False
    if count_cells(py_data) != 1:
        return False
    val = _unwrap_single_cell(py_data)
    return isinstance(val, (int, float)) and not isinstance(val, bool) and not math.isnan(val)


def _unwrap_single_cell(py_data: Any) -> Any:
    """Unwrap ``[[v]]`` / ``[v]`` / scalar to the inner value."""
    val = py_data
    while isinstance(val, list) and len(val) == 1:
        val = val[0]
    return val


def _host_ndarray_as_list(value: Any) -> list[Any] | None:
    """Turn a NumPy array into a nested list without importing NumPy on the host.

    ``tolist`` is the array method. A small numeric result that stays an
    ndarray across the pipe fails ``float()`` when the array has more than
    one cell, and ``to_calc_compatible`` then returns the array's text.
    Pandas objects are left alone (their module is not ``numpy``).
    """
    if isinstance(value, (str, bytes, bytearray, list, tuple, dict)) or value is None:
        return None
    module = getattr(type(value), "__module__", "")
    if not (isinstance(module, str) and (module == "numpy" or module.startswith("numpy."))):
        return None
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        return None
    try:
        listed = tolist()
    except Exception:
        log.debug("result_to_calc_grid: ndarray tolist failed", exc_info=True)
        return None
    if isinstance(listed, list):
        return listed
    return None


def result_to_calc_grid(result: Any, *, include_dataframe_header: bool = True) -> Any:
    """Normalize worker results for Calc consumers.

    DataFrame envelopes become a labeled 2D grid (header row + body) by default.
    Lists pass through. A leftover ndarray body is converted with ``tolist``.
    """
    if is_dataframe_payload(result):
        cols = list(result.get("columns") or [])
        data = result.get("data")
        if not isinstance(data, list):
            listed = _host_ndarray_as_list(data)
            data = listed if listed is not None else []
        return dataframe_to_labeled_grid(cols, data, include_header=include_dataframe_header)
    listed = _host_ndarray_as_list(result)
    if listed is not None:
        return listed
    return result


_ERR_NUM = 503
_ERR_VALUE = 519
_ERR_NULL = 521
_ERR_REF = 524
_ERR_NAME = 525
_ERR_DIV0 = 532
_ERR_NA = 0x7FFF

_CALC_ERROR_TOKEN_CODES: dict[str, int] = {
    "#VALUE!": 519,
    "#NUM!": 503,
    "#DIV/0!": 532,
    "#N/A": 32767,
    "#N/A N/A": 32767,
    "#REF!": 524,
    "#NAME?": 525,
    "#NULL!": 521,
}

_WORKER_EXC_CODES: dict[str, int] = {
    "ZeroDivisionError": 532,
    "OverflowError": 503,
    "FloatingPointError": 503,
    "NameError": 525,
    "UnboundLocalError": 525,
}

_DUE_TO_EXC_RE = re.compile(r" due to: ([A-Za-z_][A-Za-z0-9_]*): ")


def calc_formula_error_double(code: int) -> float:
    """Return a quiet-NaN double carrying a LibreOffice FormulaError code in its low bits.

    In LibreOffice Calc (interpr4.cxx TreatDoubleError and formula/errorcodes.hxx
    GetDoubleErrorValue), ScInterpreter::PushDouble checks for non-finite doubles.
    If the double is a quiet NaN, GetDoubleErrorValue inspects the low 32 bits
    (fraction_lo). If fraction_lo matches a valid FormulaError code, PushError is called
    with that code instead of storing a standard float. A plain NaN (fraction_lo == 0)
    evaluates to FormulaError::IllegalFPOperation (#NUM!) only; encoding the integer error
    code into fraction_lo allows returning #VALUE!, #DIV/0!, #REF!, #NAME?, #NULL!, etc.
    """
    bits = 0x7FF8000000000000 | (code & 0xFFFF)
    return struct.unpack("d", struct.pack("Q", bits))[0]


def calc_error_code_for_token(text: str) -> int | None:
    return _CALC_ERROR_TOKEN_CODES.get(text.strip())


def worker_exception_error_code(message: str) -> int:
    matches = _DUE_TO_EXC_RE.findall(message)
    if matches:
        return _WORKER_EXC_CODES.get(matches[-1], _ERR_VALUE)
    return _ERR_VALUE


_HELPER_ERROR_DICT_KEYS = frozenset({"status", "code", "message", "helper", "details"})


def _is_helper_error_dict(val: Any) -> bool:
    """True only for the coerce.error_result dict (venv/coerce.py lines 902–914).

    status, code, and message are always set. helper and details are set only
    when those arguments are truthy. Success payloads and other error dicts
    carry more keys. Do not import the venv module for this check.
    """
    if not isinstance(val, dict):
        return False
    if not set(val).issubset(_HELPER_ERROR_DICT_KEYS):
        return False
    if "code" not in val or "message" not in val:
        return False
    if val.get("status") != "error":
        return False
    code = val.get("code")
    message = val.get("message")
    if not isinstance(code, str) or not code.strip():
        return False
    if not isinstance(message, str):
        return False
    if "helper" in val:
        helper = val["helper"]
        if not isinstance(helper, str) or not helper.strip():
            return False
    if "details" in val and not isinstance(val["details"], dict):
        return False
    return True


def _calc_code_for_helper_error(code: str) -> int:
    """FormulaError integer for a helper error code.

    Tokens use calc_error_code_for_token (the _map_addin_error_tokens table).
    Exception names use _WORKER_EXC_CODES (the worker_exception_error_code table).
    Anything else is #VALUE! (519). The helper message is not scanned.
    """
    token_code = calc_error_code_for_token(code)
    if token_code is not None:
        return token_code
    exc_code = _WORKER_EXC_CODES.get(code.strip())
    if exc_code is not None:
        return exc_code
    return _ERR_VALUE


def _helper_error_log_message(err: dict[str, Any]) -> str:
    code = str(err["code"]).strip()
    message = str(err["message"])
    helper = err.get("helper")
    if isinstance(helper, str) and helper.strip():
        return f"{helper.strip()}: {code}: {message}"
    return f"{code}: {message}"


def _worker_failure_stays_text(res: dict[str, Any]) -> bool:
    if res.get("code") == "USER_STOPPED":
        return True
    message = str(res.get("message") or res.get("error") or "")
    msg_lower = message.lower()
    if "no python executable found under configured venv" in msg_lower or "venv not found" in msg_lower:
        return True
    if _DUE_TO_EXC_RE.search(message):
        return False
    if "timed out" in msg_lower or "timeout" in msg_lower:
        return True
    return False


def _map_addin_error_tokens(val: Any) -> Any:
    if isinstance(val, str):
        code = calc_error_code_for_token(val)
        if code is not None:
            return calc_formula_error_double(code)
        return val
    if isinstance(val, tuple):
        return tuple(_map_addin_error_tokens(elem) for elem in val)
    return val


def py_addin_cell_value(val: Any) -> float | str | tuple[Any, ...]:
    """Return the LibreOffice Calc add-in cell value for a Python result.

    This maps formula error tokens (e.g. #VALUE!, #DIV/0!) into quiet-NaN doubles
    carrying Calc FormulaError codes. A helper error dict is mapped the same way.
    to_calc_compatible itself remains a type coercion so quant/tabular egress
    keeps dicts and token strings as text.
    """
    if isinstance(val, dict) and _is_helper_error_dict(val):
        # Bugfix: What was wrong: a =PY() cell showed a helper error dict as text, so ISERROR was false.
        # How it happened: coerce.error_result returns {"status": "error", "code", "message"} instead of raising.
        # The worker reports status ok, and to_calc_compatible stringifies every dict. An exception from the
        # same helper already becomes a payload NaN in _addin_result_for_worker_status.
        # Why this change fixes it: this closed shape becomes calc_formula_error_double before that stringify,
        # and the message is logged with the same "=PY() worker failure" line as the exception path.
        log.error("=PY() worker failure: %s", _helper_error_log_message(val))
        return calc_formula_error_double(_calc_code_for_helper_error(val["code"]))
    return cast("float | str | tuple[Any, ...]", _map_addin_error_tokens(to_calc_compatible(val)))


def _flat_result_equal(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    """Compare flat worker result tuples across matrix session recalculations.

    Bugfix:
    - What was wrong: state.flat != tuple(flat) used standard inequality (!=).
      In Python, float('nan') != float('nan') is always True, so any list containing
      an error double reset next_index on every array formula cell, causing every cell
      to display the first element instead of advancing through the list.
    - How it happened: Calc error tokens (e.g. #VALUE!) are mapped into quiet-NaN
      doubles with error payload bits in flat.
    - Why this change fixes it: Comparing IEEE 754 bit representations of non-finite
      floats (NaN and inf) allows identical error doubles to compare equal, keeping
      the matrix session active and next_index advancing.
    """
    if len(left) != len(right):
        return False
    for val_left, val_right in zip(left, right):
        if isinstance(val_left, float) and isinstance(val_right, float):
            if not math.isfinite(val_left) or not math.isfinite(val_right):
                if struct.pack("d", val_left) != struct.pack("d", val_right):
                    return False
            else:
                if val_left != val_right:
                    return False
        else:
            if val_left != val_right:
                return False
    return True


def _addin_result_for_worker_status(res: dict[str, Any]) -> float | str:
    """Return add-in cell result for non-ok worker status (payload NaN error or text message).

    Bugfix:
    - What was wrong: The formula cell displayed the plain English string
      "Error: Code execution failed at line ..." from _format_python_addin_worker_error.
      Because this was returned as a string, LibreOffice Calc treated it as text rather than
      a formula error, so Calc functions like ISERROR returned FALSE.
    - How it happened: The worker caught Python exceptions and packed them into a formatted
      message string which was returned directly as cell text.
    - Why this change fixes it: Returning a quiet-NaN double carrying the LibreOffice
      FormulaError code causes ScInterpreter::PushDouble -> TreatDoubleError to store a
      real FormulaError in the cell. The descriptive error message remains accessible in
      the log and in LibrePy workbook diagnostics.
    """
    message = str(res.get("message") or res.get("error") or "")
    if _worker_failure_stays_text(res):
        return _format_python_addin_worker_error(message)
    log.error("=PY() worker failure: %s", message)
    return calc_formula_error_double(worker_exception_error_code(message))


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


def _calc_iso_datetime(dt: datetime.datetime) -> str:
    """Naive ISO-8601. Calc does not parse offset-bearing stamps as dates."""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.isoformat()


def to_calc_compatible(val: Any) -> float | str | tuple[Any, ...]:
    """Recursively convert Python values into LibreOffice Calc supported types.

    Calc cells and matrix formulas only support float (UNO double) and str (UNO string).
    Crucially, Calc matrix formulas do NOT support integer (UNO long) types and will
    throw #VALUE! if a sequence contains integers/longs. Python booleans are converted
    to 1.0 / 0.0 (UNO double) because Calc's Add-In bridge only unpacks doubles and strings.

    Host LibreOffice Python has no pandas/numpy — temporal pandas types are duck-typed
    (Timestamp subclasses datetime; NaT is NaTType). Do not import pandas here.
    """
    if val is None:
        return ""
    # pd.NaT subclasses datetime but isoformat() raises; map missing to empty cell.
    tname = type(val).__name__
    if tname in ("NaTType", "NAType"):
        return ""
    # Bugfix (#413): When Python bool (True/False) was returned directly, PyUNO wrapped it in
    # uno::Any with TypeClass_BOOLEAN. LibreOffice Calc's C++ Add-In caller (ScUnoAddInCall)
    # only unpacks double and string types, silently defaulting unhandled types (including BOOLEAN)
    # to 0.0. Mapping bool to 1.0 / 0.0 allows Calc formulas (e.g. IF, logical operators) to evaluate
    # truthiness correctly and matches _coerce_spill_value.
    if isinstance(val, bool):
        return 1.0 if val else 0.0

    # Bugfix: What was wrong: Large integers (> float64 max) raised OverflowError when converting to float.
    # How it happened: float(val) was called directly on integer values without handling OverflowError.
    # Why this change fixes it: OverflowError falls back to returning the decimal string str(val).
    if isinstance(val, int):
        try:
            return float(val)
        except OverflowError:
            return str(val)
    if isinstance(val, float):
        # Computed NaN (or NaN from a numeric grid that contained blanks) is returned as-is.
        # The Calc add-in bridge renders a raw NaN double as a cascading error (#NUM! or #VALUE!).
        # Python None is mapped to "" (empty cell). We intentionally do NOT collapse NaN here.
        # ±inf passes through (may also error in formulas). Do not collapse inf to empty.
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, datetime.datetime):
        return _calc_iso_datetime(val)
    if isinstance(val, datetime.date):
        return val.isoformat()
    if isinstance(val, datetime.time):
        return val.isoformat()
    if isinstance(val, datetime.timedelta):
        # In Calc, time intervals are represented as fractional days (e.g. 1.0 = 24 hours)
        return val.total_seconds() / 86400.0
    # np.datetime64 / timedelta64 (duck-typed; host may see these only if venv conversion was skipped)
    kind = getattr(getattr(val, "dtype", None), "kind", None)
    if kind == "M":
        text = str(val)
        return "" if text == "NaT" else text
    if kind == "m":
        to_pytd = getattr(val, "item", None)
        if callable(to_pytd):
            try:
                item = to_pytd()
                if isinstance(item, datetime.timedelta):
                    return item.total_seconds() / 86400.0
            except (ValueError, TypeError, OverflowError):
                pass
        text = str(val)
        return "" if text in ("NaT", "NaTType") else text
    to_pydt = getattr(val, "to_pydatetime", None)
    if callable(to_pydt):
        try:
            dt = to_pydt()
            if isinstance(dt, datetime.datetime):
                return _calc_iso_datetime(dt)
        except (ValueError, TypeError):
            pass
    to_pytd = getattr(val, "to_pytimedelta", None)
    if callable(to_pytd):
        try:
            td = to_pytd()
            if isinstance(td, datetime.timedelta):
                return td.total_seconds() / 86400.0
        except (ValueError, TypeError):
            pass
    if hasattr(val, "__float__") and not isinstance(val, (bytes, list, tuple, dict, set)):
        try:
            f = float(val)  # type: ignore[arg-type]
            return f
        except (ValueError, TypeError, OverflowError):
            pass
    if isinstance(val, (list, tuple)):
        if not val:
            return ()
        # Check if 2D sequence (contains nested rows)
        if any(isinstance(row, (list, tuple)) for row in val):
            # Normalize each row to a list of elements
            rows: list[list[Any]] = [list(row) if isinstance(row, (list, tuple)) else [row] for row in val]
            max_cols = max(len(row) for row in rows) if rows else 0
            # Rectangularize by padding shorter rows with "" so Calc matrix receives a valid rectangular grid
            padded_rows = []
            for row in rows:
                padded = [to_calc_compatible(cell) for cell in row]
                if len(padded) < max_cols:
                    padded.extend([""] * (max_cols - len(padded)))
                padded_rows.append(tuple(padded))
            return tuple(padded_rows)
        return tuple(to_calc_compatible(item) for item in val)
    return str(val)


def _get_calc_doc(ctx: Any) -> Any | None:
    try:
        from plugin.framework.thread_guard import guard_uno, on_main_thread

        if not on_main_thread():
            return None
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
        if doc is not None and hasattr(doc, "getSheets"):
            return guard_uno(doc)
    except Exception:
        log.debug("_get_calc_doc lookup failed", exc_info=True)
    return None


def session_key(ctx: Any, code: str, doc: Any | None = None) -> tuple[str, ...]:
    # Bugfix (#402, #411): Include workbook session_id in key so unsaved documents
    # (where doc_url="") do not collide in the in-memory formula result cache.
    # Do not use getActiveSheet(): full recalc's active sheet is not the formula cell
    # (XAddIn has no calling cell). Unique locate fills sheet+origin; otherwise
    # callers must not share WorkerResultSession.
    from plugin.framework.thread_guard import on_main_thread

    # Skip UNO off the main thread. Off-main finalize hands
    # scalar_for_list_result the cached spill model (the object a deferred
    # write posts to the UI thread). Running the guard only when doc is
    # None lets getURL and locate_formula_cell_in_doc touch that model from
    # a Yellow thread. The key stays ambiguous, and WorkerResultSession is
    # not shared, until the UI thread locates the cell.
    if not on_main_thread():
        return ("", "", "", code, "")
    doc_url = ""
    sheet_name = ""
    sid = ""
    origin = ""
    try:
        # The calling document comes only from the add-in caller argument.
        target = doc
        if target is not None:
            url_val = getattr(target, "getURL", lambda: "")()
            doc_url = url_val if isinstance(url_val, str) else ""
            from plugin.scripting.session_manager import workbook_session_id

            sid = workbook_session_id(ctx, doc=target) or ""
            located = locate_formula_cell_in_doc(ctx, target, code)
            if located is not None:
                sheet, _cell, coord = located
                name_val = getattr(sheet, "getName", lambda: "")()
                sheet_name = name_val if isinstance(name_val, str) else ""
                origin = f"{coord[0]},{coord[1]}"

    except Exception:
        log.debug("session_key inline metadata lookup exception", exc_info=True)
    return (doc_url, sheet_name, sid, code, origin)


class WorkerResultSession:
    """Caches one worker list result across multiple =PY() calls in a recalc pass."""

    __slots__: ClassVar[tuple[str, ...]] = ("raw", "flat", "next_index", "timestamp")
    raw: Any
    flat: tuple[Any, ...]
    next_index: int
    timestamp: float

    def __init__(self, raw: Any, flat: list[Any], timestamp: float | None = None) -> None:
        self.raw = raw
        self.flat = tuple(flat)
        self.next_index = 0
        self.timestamp = time.monotonic() if timestamp is None else timestamp


def _worker_data_token(worker_data: Any) -> str:
    """Stable sha256 digest of worker data to identify matrix sessions."""
    h = hashlib.sha256()
    seen: set[int] = set()

    def _walk(obj: Any) -> None:
        if obj is None:
            h.update(b"N")
            return
        if isinstance(obj, bool):
            h.update(b"B1" if obj else b"B0")
            return
        if isinstance(obj, (int, float)):
            h.update(b"F" + repr(obj).encode("ascii", "replace"))
            return
        if isinstance(obj, str):
            h.update(b"S" + obj.encode("utf-8", "surrogatepass"))
            return
        if isinstance(obj, (bytes, bytearray)):
            h.update(b"Y" + bytes(obj))
            return
        obj_id = id(obj)
        if obj_id in seen:
            h.update(b"cycle")
            return
        seen.add(obj_id)
        try:
            if isinstance(obj, dict):
                h.update(b"D")
                for k, v in sorted(obj.items(), key=lambda kv: str(kv[0])):
                    _walk(str(k))
                    _walk(v)
                return
            if isinstance(obj, (list, tuple)):
                h.update(b"L")
                for item in obj:
                    _walk(item)
                return
            h.update(b"r" + repr(obj).encode("utf-8", "replace"))
        finally:
            seen.remove(obj_id)

    _walk(worker_data)
    return h.hexdigest()


def _expire_matrix_sessions_locked(now: float) -> None:
    stale = [
        k for k, session in _MATRIX_SCALAR_SESSIONS.items()
        if (now - session.timestamp) > _PY_PASS_GAP_SEC
    ]
    for k in stale:
        _MATRIX_SCALAR_SESSIONS.pop(k, None)


def scalar_for_list_result(ctx: Any, code: str, result: Any, *, worker_data: Any = None, doc: Any | None = None, data_token: str | None = None) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: list[Any] = [py_addin_cell_value(v) for v in flatten_result_values(result)]
    if not flat:
        return ""
    tid = threading.get_ident()
    sk = session_key(ctx, code, doc=doc)
    if not sk[4]:
        # Ambiguous formula identity: do not share next_index across duplicate =PY() cells.
        return flat[0] if flat else ""
    token = data_token if data_token is not None else _worker_data_token(worker_data)
    key = (tid, sk, token)
    now = time.monotonic()
    with _MATRIX_SCALAR_SESSIONS_LOCK:
        _expire_matrix_sessions_locked(now)
        state = _MATRIX_SCALAR_SESSIONS.get(key)
        if (
            not isinstance(state, WorkerResultSession)
            or not _flat_result_equal(state.flat, tuple(flat))
            or (now - state.timestamp) > _PY_PASS_GAP_SEC
        ):
            state = WorkerResultSession(result, flat, timestamp=now)
            _MATRIX_SCALAR_SESSIONS[key] = state
        state.timestamp = now
        idx = state.next_index
        state.next_index = idx + 1
        if state.next_index >= len(state.flat):
            _MATRIX_SCALAR_SESSIONS.pop(key, None)
    if 0 <= idx < len(state.flat):
        return state.flat[idx]
    return state.flat[-1] if state.flat else ""


# CellFlags bitmask: VALUE (1) | DATETIME (2) | STRING (4) | FORMULA (16) | HARDATTR (32) = 55.
# Clears values, formulas, and direct hard cell formatting/number formats without removing
# annotations, styles, or draw objects.
_SPILL_CLEAR_FLAGS = 1 | 2 | 4 | 16 | 32


@dataclass(frozen=True)
class SpillCell:
    row: int
    col: int
    kind: str  # "n" float, "s" str, "e" empty, "err" formula error code, "u" unknown (load of old JSON only)
    value: float | str | int | None


@dataclass
class SpillRecord:
    code: str
    cells: list[SpillCell]

    @property
    def spills(self) -> list[tuple[int, int]]:
        return [(cell.row, cell.col) for cell in self.cells]


# The spill registry tracks coordinates that were spilled by each formula cell.
# Key: (doc identity, sheet_name, formula_row, formula_col)
# Identity is the file URL when the workbook has one. Every unsaved book
# reports getURL()==""; those use workbook_lifecycle._lifecycle_key
# (RuntimeUID), never "". LOADED_DOCUMENTS uses the same identity.
# Value: SpillRecord tracking code and neighbor cells
SPILL_REGISTRY: dict[tuple[str, str, int, int], SpillRecord] = {}
LOADED_DOCUMENTS: set[str] = set()
_SPILL_REGISTRY_LOCK = threading.Lock()
_PENDING_SPILL_LOCK = threading.Lock()
_PENDING_SPILL_TIMERS: list[tuple[str, threading.Timer]] = []

# One listener per sheet — SheetModifyDispatcher (Phase 3) or the legacy
# CalcSpillModifyListener when a test constructs it directly.
# First element is the workbook lifecycle id (RuntimeUID). Legacy spill
# listeners still pop a file-URL key from ``disposing``.
SHEET_MODIFY_LISTENERS: dict[tuple[str, str], Any] = {}


@contextmanager
def _undo_lock(doc: Any) -> Iterator[Any]:
    """Temporarily hide or lock undo recording during background spill operations.

    If an undo action exists (e.g. user just typed =PY()), enterHiddenUndoContext()
    hides the spill mutations under the formula's undo action so the spill does not
    create a separate undo step. If the undo stack is empty, um.lock() is used.
    """
    um = None
    hidden = False
    locked = False
    try:
        raw_doc = doc
        try:
            from plugin.framework.thread_guard import _unwrap_uno

            raw_doc = _unwrap_uno(doc)
        except Exception:
            log.debug("_unwrap_uno failed in _undo_lock", exc_info=True)
        if hasattr(raw_doc, "getUndoManager"):
            um = raw_doc.getUndoManager()
            if um is not None:
                try:
                    if um.isUndoPossible():
                        um.enterHiddenUndoContext()
                        hidden = True
                    elif hasattr(um, "lock"):
                        um.lock()
                        locked = True
                except Exception:
                    log.debug("enterHiddenUndoContext/lock failed in _undo_lock", exc_info=True)
                    try:
                        if hasattr(um, "lock"):
                            um.lock()
                            locked = True
                    except Exception:
                        log.debug("fallback um.lock failed in _undo_lock", exc_info=True)
    except Exception:
        log.debug("getUndoManager lookup failed in _undo_lock", exc_info=True)
        um = None
    try:
        yield um
    finally:
        if um is not None:
            if hidden:
                try:
                    um.leaveUndoContext()
                except Exception:
                    log.debug("leaveUndoContext failed", exc_info=True)
            elif locked:
                try:
                    um.unlock()
                except Exception:
                    log.debug("UndoManager.unlock failed", exc_info=True)


def _cell_matches_stored_value(sheet: Any, row: int, col: int, cell_rec: SpillCell) -> bool:
    """Check if the cell at (col, row) matches the stored SpillCell value."""
    try:
        cell = sheet.getCellByPosition(col, row)
        if cell_rec.kind == "e":
            return cell.getType() == 0 or cell.getString() == ""
        if cell_rec.kind == "n":
            v = cell.getValue()
            if isinstance(cell_rec.value, (int, float)):
                return math.isclose(float(v), float(cell_rec.value), rel_tol=1e-9, abs_tol=1e-9)
            return False
        if cell_rec.kind == "s":
            return cell.getString() == (cell_rec.value or "")
        if cell_rec.kind == "err":
            get_err = getattr(cell, "getError", None)
            if not callable(get_err) or cell_rec.value is None:
                return False
            try:
                err_val = get_err()
                if not isinstance(err_val, (int, float, str)):
                    return False
                if not isinstance(cell_rec.value, (int, float, str)):
                    return False
                return int(err_val) == int(cell_rec.value)
            except (TypeError, ValueError):
                return False
        if cell_rec.kind == "u":
            return False
    except Exception:
        return False
    return False


def _cell_is_formula(cell: Any) -> bool:
    """True if cell contains a formula (getType() == 3 or starts with '=')."""
    try:
        if cell.getType() == 3:
            return True
        formula = str(cell.getFormula() or "")
        return formula.startswith("=")
    except Exception:
        return False


class CalcSpillModifyListener(unohelper.Base, XModifyListener):
    """Orphaned-spill cleanup. Walks ``SPILL_REGISTRY`` only.

    Geometric repair must not piggyback on this walk (it does not scan
    formula cells). The registered listener is ``SheetModifyDispatcher``;
    this class stays the spill job. Do not add ``CalcGeometricModifyListener``.
    """

    ctx: Any
    doc_key: str
    sheet_name: str

    def __init__(self, ctx: Any, doc_key: str, sheet_name: str) -> None:
        self.ctx = ctx
        self.doc_key = doc_key
        self.sheet_name = sheet_name

    def modified(self, aEvent: Any) -> None:
        try:
            from plugin.framework.thread_guard import on_main_thread

            if not on_main_thread():
                return
            sheet = aEvent.Source
            if sheet is None:
                return

            from plugin.calc.python.sheet_modify import _owning_calc_doc, sheet_modify_scope

            # Walk to the spreadsheet that owns the sheet. Orphan cleanup
            # locks undo and saves WriterAgentSpillRegistry on the spreadsheet
            # that owns the sheet. The cells it clears belong to the sheet
            # that fired, which may be a background file. _get_calc_doc is
            # desktop.getCurrentComponent(). A parent-less MagicMock still
            # falls back to the active model so direct tests keep their stub.
            doc = _owning_calc_doc(sheet)
            if doc is None:
                doc = _get_calc_doc(self.ctx)

            from plugin.calc.python.cell_discovery import is_py_formula_text
            from plugin.calc.python.formula_locator_cache import (
                is_matching_py_formula,
                search_sheet_for_formula,
            )

            # Bugfix: What was wrong: When rows/columns were inserted, formula cells shifted to new coordinates,
            # causing modified() to see a mismatch at (fcol, frow) and clear all spilled cells.
            # How it happened: modified() only checked if the formula at the old coordinates matched; if not,
            # it assumed the formula was deleted and immediately cleared all old spill coordinates.
            # Why this change fixes it: It first searches for the formula at its new position via
            # search_sheet_for_formula. If found and its neighbor cells moved by the same delta and match
            # stored values, the registry entry is remapped to the new coordinates without clearing.
            with sheet_modify_scope():
                to_remove: list[tuple[str, str, int, int]] = []
                to_remap: list[tuple[tuple[str, str, int, int], tuple[str, str, int, int], SpillRecord]] = []
                for key, record in list(SPILL_REGISTRY.items()):
                    reg_doc_key, sheet_name, frow, fcol = key
                    if self.doc_key and reg_doc_key == self.doc_key and sheet_name == self.sheet_name:
                        try:
                            spill_rec = record
                            cell = sheet.getCellByPosition(fcol, frow)
                            formula = cell.getFormula()
                            formula_str = str(formula) if formula else ""
                            formula_matches = (
                                is_matching_py_formula(formula_str, spill_rec.code)
                                if spill_rec.code
                                else is_py_formula_text(formula_str)
                            )
                            if formula_matches:
                                continue

                            remapped = False
                            if spill_rec.code and not any(c.kind == "u" for c in spill_rec.cells):
                                loc = search_sheet_for_formula(sheet, spill_rec.code, doc=doc)
                                if loc is not None:
                                    _loc_cell, new_row, new_col = loc
                                    d_row = new_row - frow
                                    d_col = new_col - fcol
                                    if d_row != 0 or d_col != 0:
                                        all_match = all(
                                            _cell_matches_stored_value(sheet, c.row + d_row, c.col + d_col, c)
                                            for c in spill_rec.cells
                                        )
                                        if all_match:
                                            new_cells = [
                                                SpillCell(c.row + d_row, c.col + d_col, c.kind, c.value)
                                                for c in spill_rec.cells
                                            ]
                                            new_key = (reg_doc_key, sheet_name, new_row, new_col)
                                            to_remap.append((key, new_key, SpillRecord(code=spill_rec.code, cells=new_cells)))
                                            remapped = True

                            if not remapped:
                                to_remove.append(key)
                        except Exception:
                            log.debug("Failed to inspect formula cell %r", key, exc_info=True)

                if to_remove or to_remap:
                    with _undo_lock(doc):
                        for key in to_remove:
                            removed_rec = SPILL_REGISTRY.pop(key, None)
                            if removed_rec is not None:
                                frow, fcol = key[2], key[3]
                                for c in removed_rec.cells:
                                    if (c.row, c.col) != (frow, fcol):
                                        try:
                                            spill_cell = sheet.getCellByPosition(c.col, c.row)
                                            if not _cell_is_formula(spill_cell):
                                                if c.kind == "u" or _cell_matches_stored_value(sheet, c.row, c.col, c):
                                                    spill_cell.clearContents(_SPILL_CLEAR_FLAGS)
                                        except Exception:
                                            log.debug("CalcSpillModifyListener: failed to clear cell (%d, %d)", c.row, c.col, exc_info=True)
                        for old_k, new_k, new_rec in to_remap:
                            SPILL_REGISTRY.pop(old_k, None)
                            SPILL_REGISTRY[new_k] = new_rec
                        if doc is not None:
                            save_spill_registry_for_doc(doc)
        except Exception:
            log.exception("Error in CalcSpillModifyListener.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        SHEET_MODIFY_LISTENERS.pop((self.doc_key, self.sheet_name), None)


def _spill_registry_doc_key(doc: Any) -> str:
    """Key the spill registry and LOADED_DOCUMENTS by RuntimeUID, not URL.

    Migrate existing URL keys when the uid is read.
    """
    if doc is None:
        return ""
    # Bugfix: What was wrong: Property-only reads missed builds that expose getRuntimeUID()
    # or the attribute, and str() of a truthy mock was accepted as an id.
    # How it happened: _spill_registry_doc_key only called getPropertyValue("RuntimeUID") and converted any truthy value via str().
    # Why this change fixes it: Use the shared _read_runtime_uid ladder, which tries method, attribute,
    # then property, and accepts only plain str / int, same as get_runtime_uid.
    uid = ""
    try:
        from plugin.framework.uno_context import _read_runtime_uid

        uid = _read_runtime_uid(doc)
    except Exception:
        log.debug("spill registry: RuntimeUID read failed", exc_info=True)
        uid = ""

    url = ""
    try:
        url_raw = getattr(doc, "getURL", lambda: "")()
        url = str(url_raw) if isinstance(url_raw, str) else ""
    except Exception:
        url = ""

    if uid:
        key = uid
        if url and url != key:
            if url in LOADED_DOCUMENTS:
                LOADED_DOCUMENTS.discard(url)
                LOADED_DOCUMENTS.add(key)
            with _SPILL_REGISTRY_LOCK:
                for k in list(SPILL_REGISTRY.keys()):
                    if k[0] == url:
                        SPILL_REGISTRY[(key, k[1], k[2], k[3])] = SPILL_REGISTRY.pop(k)
        return key

    if url:
        return url

    try:
        from plugin.calc.python.workbook_lifecycle import _lifecycle_key

        return str(_lifecycle_key(doc) or "")
    except Exception:
        log.debug("spill registry identity failed", exc_info=True)
        return ""


def load_spill_registry_for_doc(doc: Any) -> None:
    """Load the document's spill registry from its UserDefinedProperties."""
    try:
        from plugin.doc.udprops import get_document_property
        import json

        raw = get_document_property(doc, "WriterAgentSpillRegistry", None)
        if not isinstance(raw, str) or not raw.strip():
            return
        data = json.loads(raw)
        doc_key = _spill_registry_doc_key(doc)
        # UD JSON stays ``sheet:row,col`` inside this document. The in-memory
        # key is per workbook. Do not file those rows under "" (every untitled book).
        if not doc_key:
            return
        for key, value in data.items():
            try:
                parts = key.split(":")
                if len(parts) == 2:
                    sheet_name, coords = parts
                    row_col = coords.split(",")
                    if len(row_col) == 2:
                        frow, fcol = int(row_col[0]), int(row_col[1])
                        if isinstance(value, dict):
                            code = str(value.get("code") or "")
                            raw_cells = value.get("cells") or []
                            cells = []
                            for c in raw_cells:
                                if isinstance(c, dict):
                                    r_val = c.get("r")
                                    if r_val is None:
                                        r_val = c.get("row", 0)
                                    r = int(r_val or 0)
                                    c_val = c.get("c")
                                    if c_val is None:
                                        c_val = c.get("col", 0)
                                    col = int(c_val or 0)
                                    t = str(c.get("t", c.get("kind", "u")))
                                    v = c.get("v", c.get("value"))
                                    cells.append(SpillCell(row=r, col=col, kind=t, value=v))
                            SPILL_REGISTRY[(doc_key, sheet_name, frow, fcol)] = SpillRecord(code=code, cells=cells)
                        elif isinstance(value, list):
                            spill_cells = [SpillCell(int(r), int(c), "u", None) for r, c in value]
                            SPILL_REGISTRY[(doc_key, sheet_name, frow, fcol)] = SpillRecord(code="", cells=spill_cells)
            except Exception:
                log.warning("spill registry skipped malformed entry %r", key, exc_info=True)
                continue
    except Exception:
        log.exception("Failed to load spill registry from document property")


def save_spill_registry_for_doc(doc: Any) -> None:
    """Save the document's spill registry to its UserDefinedProperties."""
    try:
        from plugin.doc.udprops import set_document_property, get_document_property
        import json

        doc_key = _spill_registry_doc_key(doc)
        if not doc_key:
            return
        doc_spills: dict[str, Any] = {}
        for key, record in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_key:
                cells_data = []
                for c in record.cells:
                    if c.kind == "n" and isinstance(c.value, float) and not math.isfinite(c.value):
                        cells_data.append({"r": c.row, "c": c.col, "t": "u"})
                    else:
                        item: dict[str, Any] = {"r": c.row, "c": c.col, "t": c.kind}
                        if c.value is not None:
                            item["v"] = c.value
                        cells_data.append(item)
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = {
                    "code": record.code,
                    "cells": cells_data,
                }
        new_val = json.dumps(doc_spills)
        set_document_property(doc, "WriterAgentSpillRegistry", new_val)
        actual = get_document_property(doc, "WriterAgentSpillRegistry", "")
        if actual != new_val:
            log.warning("Spill registry write back mismatch: expected %r, got %r", new_val, actual)
    except Exception:
        log.exception("Failed to save spill registry to document property")


def _coerce_spill_value(val: Any, null_dt: datetime.date = datetime.date(1899, 12, 30)) -> tuple[Any, dict[str, Any]]:
    """Convert raw grid cell value to Calc-compatible primitive plus temporal metadata.

    Returns (calc_val, meta) where meta has 'is_temporal', 'input_category', 'serial'.
    """
    if val is None:
        return "", {"is_temporal": False, "is_empty": True}
    if isinstance(val, bool):
        return (1.0 if val else 0.0), {"is_temporal": False, "is_empty": False}

    tname = type(val).__name__
    if tname in ("NaTType", "NAType"):
        return "", {"is_temporal": False, "is_empty": True}

    # Bugfix: What was wrong: Large integers (> float64 max) raised OverflowError when converting to float.
    # How it happened: float(val) was called directly on integer values without handling OverflowError.
    # Why this change fixes it: OverflowError falls back to returning the decimal string str(val).
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        try:
            return float(val), {"is_temporal": False, "is_empty": False}
        except OverflowError:
            return str(val), {"is_temporal": False, "is_empty": False}

    if isinstance(val, datetime.datetime):
        dt = val.replace(tzinfo=None) if val.tzinfo is not None else val
        days = (dt.date() - null_dt).days
        fraction = (dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000.0) / 86400.0
        serial = float(days) + fraction
        return serial, {"is_temporal": True, "input_category": "datetime", "serial": serial, "is_empty": False}

    if isinstance(val, datetime.date):
        serial = float((val - null_dt).days)
        return serial, {"is_temporal": True, "input_category": "date", "serial": serial, "is_empty": False}

    if isinstance(val, datetime.time):
        serial = (val.hour * 3600 + val.minute * 60 + val.second + val.microsecond / 1_000_000.0) / 86400.0
        return serial, {"is_temporal": True, "input_category": "time", "serial": serial, "is_empty": False}

    if isinstance(val, datetime.timedelta):
        serial = val.total_seconds() / 86400.0
        return serial, {"is_temporal": True, "input_category": "duration", "serial": serial, "is_empty": False}

    # Duck-typed NumPy / Pandas types (np.datetime64, np.timedelta64)
    kind = getattr(getattr(val, "dtype", None), "kind", None)
    if kind == "M":
        to_pydt = getattr(val, "item", None)
        if callable(to_pydt):
            try:
                item = to_pydt()
                if isinstance(item, (datetime.datetime, datetime.date)):
                    return _coerce_spill_value(item, null_dt)
            except Exception:
                pass
        text = str(val)
        if text in ("NaT", "NaTType"):
            return "", {"is_temporal": False, "is_empty": True}

    if kind == "m":
        to_pytd = getattr(val, "item", None)
        if callable(to_pytd):
            try:
                item = to_pytd()
                if isinstance(item, datetime.timedelta):
                    return _coerce_spill_value(item, null_dt)
            except Exception:
                pass

    if isinstance(val, str):
        stripped = val.strip()
        if not stripped:
            return "", {"is_temporal": False, "is_empty": True}
        err = _CALC_ERROR_TOKEN_CODES.get(stripped)
        if err is not None:
            # setDataArray of None maps in PyUNO to an empty Any (UNO void), which Calc's
            # lcl_PutDataArray handles via SetError(NotAvailable) for #N/A. A NaN double written via
            # SetValue is treated as a plain numeric cell with no error code, so ISERROR would be false.
            # #ERR32767! is not a published constant, so we cannot use setFormula for #N/A.
            # For other codes, "" serves as an empty placeholder in setDataArray and perform_deferred_spill
            # assigns the formula f"=#ERR{code}!".
            if err == _ERR_NA:
                return None, {"is_temporal": False, "is_empty": False, "is_calc_error": True, "error_code": err}
            return "", {"is_temporal": False, "is_empty": False, "is_calc_error": True, "error_code": err}
        if match_iso_duration(stripped):
            try:
                serial = duration_serial_from_iso(stripped)
                return serial, {"is_temporal": True, "input_category": "duration", "serial": serial, "is_empty": False}
            except Exception:
                pass
        cat = match_iso_temporal(stripped)
        if cat is not None:
            try:
                if cat == "date":
                    d = datetime.date.fromisoformat(stripped)
                    serial = float((d - null_dt).days)
                    return serial, {"is_temporal": True, "input_category": "date", "serial": serial, "is_empty": False}
                elif cat == "datetime":
                    dt = datetime.datetime.fromisoformat(stripped.replace(" ", "T"))
                    days = (dt.date() - null_dt).days
                    fraction = (dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000.0) / 86400.0
                    serial = float(days) + fraction
                    return serial, {"is_temporal": True, "input_category": "datetime", "serial": serial, "is_empty": False}
                elif cat == "time":
                    t = datetime.time.fromisoformat(stripped)
                    serial = (t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1_000_000.0) / 86400.0
                    return serial, {"is_temporal": True, "input_category": "time", "serial": serial, "is_empty": False}
            except Exception:
                pass
        return val, {"is_temporal": False, "is_empty": False}

    return to_calc_compatible(val), {"is_temporal": False, "is_empty": False}


def perform_deferred_spill(
    ctx: Any,
    doc_url: str,
    sheet_name: str,
    formula_row: int,
    formula_col: int,
    grid: list[list[Any]],
    doc: Any | None = None,
    sheet: Any | None = None,
    *,
    code: str = "",
    lifecycle_key: str = "",
) -> None:
    """Clear old spilled cells and write new values deferred (collision check is done synchronously)."""
    try:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread():
            return
        if doc is None:
            return

        with _undo_lock(doc):
            # The scheduled token is the file URL, or the lifecycle id
            # captured when the URL was empty. current_url != doc_url is
            # false when both are empty, so two unsaved books shared one
            # spill write. A blank token is the legacy getURL() of an
            # untitled book (UNO callers). The registry still uses this
            # document's lifecycle id, and a saved book rejects the blank.
            live_key = _spill_registry_doc_key(doc)
            scheduled = doc_url or ""
            if scheduled:
                if scheduled != live_key:
                    return
            else:
                current_url = ""
                try:
                    current_url = getattr(doc, "getURL", lambda: "")() or ""
                except Exception:
                    current_url = ""
                if current_url or not live_key:
                    return
            if lifecycle_key:
                try:
                    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

                    if _lifecycle_key(doc) != lifecycle_key:
                        # Closed file reopened with the same URL is a new instance.
                        return
                except Exception:
                    log.debug("perform_deferred_spill: lifecycle key check failed", exc_info=True)
                    return

            target_sheet: Any = None
            try:
                if hasattr(doc, "getSheets"):
                    target_sheet = doc.getSheets().getByName(sheet_name)
            except Exception:
                target_sheet = None

            if target_sheet is None and sheet is not None:
                target_sheet = sheet

            if target_sheet is None:
                return

            sheet = target_sheet
            assert sheet is not None
            try:
                # If that object is disposed, return as today
                if hasattr(sheet, "getName"):
                    _ = sheet.getName()
            except Exception:
                return

            live_sheet_name = ""
            try:
                if hasattr(sheet, "getName"):
                    live_sheet_name = str(sheet.getName() or "")
            except Exception:
                live_sheet_name = ""
            if live_sheet_name and live_sheet_name != sheet_name:
                try:
                    from plugin.calc.python.sheet_modify import migrate_sheet_rename

                    migrate_sheet_rename(doc, sheet_name, live_sheet_name)
                except Exception:
                    log.debug("perform_deferred_spill: rename migration failed", exc_info=True)
                sheet_name = live_sheet_name

            if code:
                try:
                    origin_cell = sheet.getCellByPosition(formula_col, formula_row)
                    if not is_matching_py_formula(origin_cell.getFormula(), code):
                        # Formula moved or replaced; do not clear/write stale coordinates.
                        return
                except Exception:
                    log.debug("perform_deferred_spill: origin formula check failed", exc_info=True)
                    return

            reg_key = (live_key, sheet_name, formula_row, formula_col)

            from plugin.calc.python.sheet_modify import sheet_modify_scope

            with sheet_modify_scope():
                # 1. Clear previously spilled cells
                # We overwrite previously spilled cells, not #SPILL! on user edits, because the registry stores coordinates only, not the values we wrote.
                previous_record = SPILL_REGISTRY.get(reg_key)
                if previous_record is not None:
                    old_coords = previous_record.spills if isinstance(previous_record, SpillRecord) else previous_record
                    for r, c in old_coords:
                        if (r, c) != (formula_row, formula_col):
                            try:
                                cell = sheet.getCellByPosition(c, r)
                                cell.clearContents(_SPILL_CLEAR_FLAGS)
                            except Exception:
                                log.debug("perform_deferred_spill: failed to clear cell (%d, %d)", r, c, exc_info=True)

                # 2. Determine bounds
                num_rows = len(grid)
                num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
                if num_rows == 0 or num_cols == 0:
                    SPILL_REGISTRY[reg_key] = SpillRecord(code=code, cells=[])
                    save_spill_registry_for_doc(doc)
                    return

                # Extract NullDate for temporal day serial conversion
                null_dt = datetime.date(1899, 12, 30)
                try:
                    settings = doc.getNumberFormatSettings()
                    if settings is not None:
                        nd = settings.getPropertyValue("NullDate")
                        if nd is not None:
                            null_dt = datetime.date(getattr(nd, "Year", 1899), getattr(nd, "Month", 12), getattr(nd, "Day", 30))
                except Exception:
                    pass

                # 3. Coerce and pad grid values for rectangular setDataArray block write
                coerced_grid: list[list[Any]] = []
                cell_metas: list[list[dict[str, Any]]] = []
                has_any_temporal = False

                for row in grid:
                    coerced_row: list[Any] = []
                    meta_row: list[dict[str, Any]] = []
                    for col_idx in range(num_cols):
                        val = row[col_idx] if col_idx < len(row) else None
                        calc_val, meta = _coerce_spill_value(val, null_dt)
                        if meta.get("is_temporal"):
                            has_any_temporal = True
                        coerced_row.append(calc_val)
                        meta_row.append(meta)
                    coerced_grid.append(coerced_row)
                    cell_metas.append(meta_row)

                # 4. Spill new values using setDataArray to avoid O(N) individual cell writes
                # We write with setDataArray, not setFormulaArray, so result strings starting with '=' stay text.
                if num_cols > 1:
                    first_row_range = sheet.getCellRangeByPosition(formula_col + 1, formula_row, formula_col + num_cols - 1, formula_row)
                    first_row_range.setDataArray((tuple(coerced_grid[0][1:]),))

                if num_rows > 1:
                    remaining_range = sheet.getCellRangeByPosition(formula_col, formula_row + 1, formula_col + num_cols - 1, formula_row + num_rows - 1)
                    remaining_range.setDataArray(tuple(tuple(row) for row in coerced_grid[1:]))

                for r_offset in range(num_rows):
                    for c_offset in range(num_cols):
                        if (r_offset, c_offset) == (0, 0):
                            continue
                        meta = cell_metas[r_offset][c_offset]
                        if meta.get("is_calc_error") and meta.get("error_code") != _ERR_NA:
                            sheet.getCellByPosition(formula_col + c_offset, formula_row + r_offset).setFormula(
                                f"=#ERR{int(meta['error_code'])}!"
                            )

                new_cells: list[SpillCell] = []
                for r_offset in range(num_rows):
                    for c_offset in range(num_cols):
                        if (r_offset, c_offset) == (0, 0):
                            continue
                        r = formula_row + r_offset
                        c = formula_col + c_offset
                        meta = cell_metas[r_offset][c_offset]
                        calc_val = coerced_grid[r_offset][c_offset]
                        stored_val: float | str | int | None = None
                        if meta.get("is_calc_error"):
                            kind = "err"
                            stored_val = int(meta["error_code"])
                        elif meta.get("is_empty"):
                            kind = "e"
                            stored_val = None
                        elif isinstance(calc_val, float):
                            kind = "n"
                            stored_val = calc_val
                        elif isinstance(calc_val, str):
                            kind = "s"
                            stored_val = calc_val
                        else:
                            kind = "e"
                            stored_val = None
                        new_cells.append(SpillCell(row=r, col=c, kind=kind, value=stored_val))

                SPILL_REGISTRY[reg_key] = SpillRecord(code=code, cells=new_cells)
                save_spill_registry_for_doc(doc)

                # 5. Apply NumberFormats for any temporal cells (dates, datetimes, times, durations)
                if has_any_temporal:
                    try:
                        formats = doc.getNumberFormats()
                        try:
                            locale = doc.getPropertyValue("CharLocale")
                            if not getattr(locale, "Language", None):
                                import uno

                                locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")
                        except Exception:
                            import uno

                            locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")

                        # Standard format keys (2=DATE, 4=TIME, 6=DATETIME, 43=DURATION)
                        standard_keys: dict[str, int] = {}
                        try:
                            standard_keys["date"] = int(formats.getStandardFormat(2, locale))
                        except Exception:
                            standard_keys["date"] = 36
                        try:
                            standard_keys["time"] = int(formats.getStandardFormat(4, locale))
                        except Exception:
                            standard_keys["time"] = 40
                        try:
                            standard_keys["datetime"] = int(formats.getStandardFormat(6, locale))
                        except Exception:
                            standard_keys["datetime"] = 50
                        try:
                            dur_key = formats.getFormatIndex(43, locale)
                            standard_keys["duration"] = int(dur_key) if dur_key != -1 else 43
                        except Exception:
                            standard_keys["duration"] = 43

                        category_cache: dict[int, str | None] = {}
                        decisions: list[list[Any]] = []

                        for r_idx in range(num_rows):
                            row_dec: list[Any] = []
                            for c_idx in range(num_cols):
                                meta = cell_metas[r_idx][c_idx]
                                if meta.get("is_temporal"):
                                    in_cat = meta["input_category"]
                                    serial_val = meta["serial"]
                                    target_c = formula_col + c_idx
                                    target_r = formula_row + r_idx
                                    dest_cat = None
                                    try:
                                        cell = sheet.getCellByPosition(target_c, target_r)
                                        k = int(cell.getPropertyValue("NumberFormat"))
                                        if k not in category_cache:
                                            props = formats.getByKey(k)
                                            category_cache[k] = _format_category_from_type(props.getPropertyValue("Type"))
                                        dest_cat = category_cache[k]
                                    except Exception:
                                        pass
                                    if should_preserve_temporal_format(in_cat, float(serial_val), dest_cat):
                                        row_dec.append(("preserve", None))
                                    else:
                                        apply_k = standard_keys.get(in_cat, standard_keys["date"])
                                        row_dec.append(("apply", int(apply_k)))
                                elif meta.get("is_empty"):
                                    row_dec.append("empty")
                                else:
                                    row_dec.append(None)
                            decisions.append(row_dec)

                        rects = coalesce_temporal_apply_rects(decisions)
                        for r0, r1, c0, c1, k in rects:
                            trange = sheet.getCellRangeByPosition(formula_col + c0, formula_row + r0, formula_col + c1, formula_row + r1)
                            trange.setPropertyValue("NumberFormat", int(k))
                    except Exception:
                        log.exception("Error applying temporal number formats during deferred spill")

    except Exception:
        log.exception("Error in perform_deferred_spill")


def _result_as_spill_grid(result: list[Any] | tuple[Any, ...]) -> list[list[Any]]:
    """Normalize a 1D list or 2D list-of-lists into a rectangular spill grid."""
    if not result:
        return []
    if any(isinstance(row, (list, tuple)) for row in result):
        return [list(row) if isinstance(row, (list, tuple)) else [row] for row in result]
    return [[x] for x in result]


def _cell_is_matrix(sheet: Any, cell: Any) -> bool:
    """True when the located formula cell is part of a multi-cell array formula block."""
    try:
        if hasattr(sheet, "createCursorByRange") and cell is not None:
            cursor = sheet.createCursorByRange(cell)
            if hasattr(cursor, "collapseToCurrentArray"):
                cursor.collapseToCurrentArray()
                addr = cursor.getRangeAddress()
                return (addr.EndColumn > addr.StartColumn) or (addr.EndRow > addr.StartRow)
    except Exception:
        log.debug("_cell_is_matrix check failed", exc_info=True)
    return False


def _selection_is_multi_cell(target_doc: Any) -> bool:
    """True when the current UI selection spans more than one cell (matrix entry)."""
    ctrl = target_doc.getCurrentController() if target_doc is not None else None
    if ctrl is None:
        return False
    selection = ctrl.getSelection()
    if selection is None or not hasattr(selection, "getRangeAddress"):
        return False
    addr = selection.getRangeAddress()
    return (addr.EndColumn - addr.StartColumn > 0) or (addr.EndRow - addr.StartRow > 0)


@dataclass(frozen=True)
class PreparedSpill:
    doc_key: str
    sheet_name: str
    formula_row: int
    formula_col: int
    sheet: Any = None


def _prepare_auto_spill(ctx: Any, code: str, grid_to_spill: list[list[Any]], target_doc: Any) -> PreparedSpill | str | None:
    """Locate the unique formula origin and check spill collisions (UNO / UI thread).

    Returns ``"#SPILL!"`` on collision, ``PreparedSpill`` when the
    neighbor write should proceed, or ``None`` when the origin is not unique.
    The identity is the file URL, or the lifecycle id when ``getURL()`` is empty.
    """
    located = locate_formula_cell_in_doc(ctx, target_doc, code)
    if located is None:
        return None
    doc_key = _spill_registry_doc_key(target_doc)
    if not doc_key:
        # No file URL and no lifecycle id: refuse the shared "" key.
        return None
    sheet = located[0]
    cell = located[1]
    if _cell_is_matrix(sheet, cell):
        return None
    formula_coord = located[2]
    sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
    log.debug("Spill: located formula cell at %r on sheet %r for code %r", formula_coord, sheet_name, code)
    formula_row, formula_col = formula_coord

    if doc_key not in LOADED_DOCUMENTS:
        load_spill_registry_for_doc(target_doc)
        LOADED_DOCUMENTS.add(doc_key)

    try:
        from plugin.calc.python.sheet_modify import ensure_sheet_modify_listener

        ensure_sheet_modify_listener(ctx, target_doc, sheet)
    except Exception:
        log.exception("Failed to register modify listener on sheet")

    num_rows = len(grid_to_spill)
    num_cols = max(len(row) for row in grid_to_spill) if num_rows > 0 else 0
    reg_key = (doc_key, sheet_name, formula_row, formula_col)
    previous_record = SPILL_REGISTRY.get(reg_key)
    previous_spills = previous_record.spills if isinstance(previous_record, SpillRecord) else (previous_record or [])
    prev_spill_set = set(previous_spills)

    log.debug("Spill: previous spills for cell %r: %r", reg_key, previous_spills)

    try:
        from com.sun.star.table.CellContentType import EMPTY
    except ImportError:
        EMPTY = cast("Any", 0)

    max_cols = 1024
    max_rows = 1048576
    try:
        if hasattr(sheet, "getColumns"):
            cols_obj = sheet.getColumns()
            if hasattr(cols_obj, "getCount"):
                max_cols = int(cols_obj.getCount())
        if hasattr(sheet, "getRows"):
            rows_obj = sheet.getRows()
            if hasattr(rows_obj, "getCount"):
                max_rows = int(rows_obj.getCount())
    except Exception:
        pass

    for r_idx in range(num_rows):
        for c_idx in range(num_cols):
            if r_idx == 0 and c_idx == 0:
                continue
            target_r = formula_row + r_idx
            target_c = formula_col + c_idx
            if target_r >= max_rows or target_c >= max_cols:
                log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                return "#SPILL!"
            if (target_r, target_c) == (formula_row, formula_col):
                continue
            if (target_r, target_c) in prev_spill_set:
                continue
            cell = sheet.getCellByPosition(target_c, target_r)
            cell_type = cell.getType()
            if cell_type != EMPTY:
                log.debug("Spill: collision: cell at %r (type=%s, val=%r, formula=%r) is not empty", (target_r, target_c), cell_type, cell.getValue() or cell.getString(), cell.getFormula())
                return "#SPILL!"
    return PreparedSpill(doc_key, sheet_name, formula_row, formula_col, sheet=sheet)


def _queue_deferred_spill_write(ctx: Any, code: str, grid_to_spill: list[list[Any]], target_doc: Any, prepared: PreparedSpill) -> None:
    """Post neighbor writes to the UI thread after the usual 0.1s settle."""
    from plugin.framework.queue_executor import post_to_main_thread
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

    doc_url = prepared.doc_key
    sheet_name = prepared.sheet_name
    formula_row = prepared.formula_row
    formula_col = prepared.formula_col
    sheet_obj = prepared.sheet
    spill_lifecycle = _lifecycle_key(target_doc)

    def _deferred_spill_on_main() -> None:
        post_to_main_thread(lambda: perform_deferred_spill(ctx, doc_url, sheet_name, formula_row, formula_col, grid_to_spill, doc=target_doc, sheet=sheet_obj, code=code, lifecycle_key=spill_lifecycle))

    token = f"{sheet_name}:{formula_row}:{formula_col}"
    t = _new_spill_timer(0.1, _deferred_spill_on_main)
    _register_spill_timer(spill_lifecycle, t, token=token)
    t.start()


def _queue_off_main_auto_spill(ctx: Any, code: str, grid_to_spill: list[list[Any]], doc: Any) -> None:
    """Locate and write the spill on the UI thread in the calling document.

    Yellow/off-main finalize must not touch UNO: *doc* (the add-in caller) is
    only passed through to the UI-thread callback. Collision ``#SPILL!`` is too
    late to change the add-in return; the deferred path just skips the write.
    """
    from plugin.framework.queue_executor import post_to_main_thread

    log.debug("Spill: scheduling off-main deferred locate code=%r", code)

    def _on_main() -> None:
        target_doc = doc
        try:
            prepared = _prepare_auto_spill(ctx, code, grid_to_spill, target_doc)
        except Exception:
            log.exception("Error checking spill collision or locating formula cell")
            return
        if not isinstance(prepared, PreparedSpill):
            return
        from plugin.calc.python.workbook_lifecycle import _lifecycle_key

        doc_url = prepared.doc_key
        sheet_name = prepared.sheet_name
        formula_row = prepared.formula_row
        formula_col = prepared.formula_col
        sheet_obj = prepared.sheet
        perform_deferred_spill(ctx, doc_url, sheet_name, formula_row, formula_col, grid_to_spill, doc=target_doc, sheet=sheet_obj, code=code, lifecycle_key=_lifecycle_key(target_doc))

    def _deferred() -> None:
        post_to_main_thread(_on_main)

    # The key was cached on the UI thread; do not call _lifecycle_key here.
    # Registering under an empty string means unload's cancel (keyed by
    # RuntimeUID or the workbook session id) never sees this timer, so the
    # closure keeps ctx, the code, the grid, and the cached document after
    # the book closes.
    lkey = _off_main_spill_lifecycle_key(doc)
    t = _new_spill_timer(0.1, _deferred)
    if lkey:
        _register_spill_timer(lkey, t, token=f"code:{code}")
    t.start()


def finalize_python_return(ctx: Any, code: str, result: Any, *, index_arg: Any = None, worker_data: Any = None, doc: Any | None = None, data_token: str | None = None) -> float | str | bool | tuple[Any, ...]:
    """Map worker result to a single value Calc's add-in bridge accepts."""
    # Worker egress (payload_codec.child_pack_result + host_unpack_data) always yields plain
    # lists/scalars on the host — NumPy lives only in the venv subprocess, not in LO's Python.
    # DataFrame envelopes become labeled grids (header row + body) so columns survive spill.
    result = result_to_calc_grid(result)

    # Auto-spill: list/tuple, no index_arg, and not a matrix selection.
    # A matrix is only a selection we actually see covering more than one
    # cell. Off-main =PY() (Calc's multithreaded recalc, or a Yellow
    # dispatch) cannot inspect the UI selection or resolve a UNO document.
    # Treating "no document and not on main" as a matrix returns only
    # grid[0][0], so a DataFrame or a 2D list paints a single corner and
    # never logs Spill:. Off-main, locate, collision, and write are posted
    # to the UI thread with the calling document (doc is only passed
    # through there). No doc means no spill: the corner value is returned.
    is_matrix = False
    if isinstance(result, (list, tuple)) and index_arg is None and len(result) > 0:
        from plugin.framework.config import get_config_bool
        from plugin.framework.thread_guard import on_main_thread

        if get_config_bool("scripting.python_auto_spill"):
            # AST lint only treats a bare ``if on_main_thread():`` as a guard.
            if on_main_thread():
                try:
                    target_doc = doc
                    if target_doc is not None:
                        # Bugfix: What was wrong: UI selection check ran before formula locator; if user had selected multiple cells elsewhere on the sheet, single-cell =PY() was mistakenly treated as a matrix formula.
                        # How it happened: _selection_is_multi_cell was checked first to "avoid locator scan".
                        # Why this change fixes it: We locate the actual formula cell first and check _cell_is_matrix; selection is only checked as fallback when locator returns None.
                        located = locate_formula_cell_in_doc(ctx, target_doc, code)
                        if located is not None:
                            sheet, cell, _coord = located
                            is_matrix = _cell_is_matrix(sheet, cell)
                        else:
                            is_matrix = _selection_is_multi_cell(target_doc)
                except Exception:
                    log.debug("matrix check failed", exc_info=True)
        else:
            is_matrix = True

        if not is_matrix:
            grid_to_spill = _result_as_spill_grid(result)
            # Bugfix: What was wrong: [[]] became [[]], [0][0] raised IndexError after
            # _prepare_auto_spill / _queue_off_main_auto_spill had already scheduled a
            # zero-column spill, and the except Exception then fell through to the scalar path.
            # How it happened: Auto-spill assumed any non-empty result grid had a corner cell [0][0].
            # Why this change fixes it: Skipping when the corner cell does not exist writes nothing.
            if grid_to_spill and grid_to_spill[0]:
                try:
                    if not on_main_thread():
                        if doc is not None:
                            _queue_off_main_auto_spill(ctx, code, grid_to_spill, doc)
                        # We return the corner as py_addin_cell_value (ISO text for dates), not a date serial, because an add-in return cannot carry a number format.
                        return py_addin_cell_value(grid_to_spill[0][0])

                    target_doc = doc
                    if target_doc is not None:
                        prepared = _prepare_auto_spill(ctx, code, grid_to_spill, target_doc)
                        if prepared == "#SPILL!":
                            return "#SPILL!"
                        if isinstance(prepared, PreparedSpill):
                            _queue_deferred_spill_write(ctx, code, grid_to_spill, target_doc, prepared)
                            # We return the corner as py_addin_cell_value (ISO text for dates), not a date serial, because an add-in return cannot carry a number format.
                            return py_addin_cell_value(grid_to_spill[0][0])
                except Exception:
                    log.exception("Error checking spill collision or locating formula cell")

    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = flatten_result_values(result)
            idx = coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return py_addin_cell_value(flat[idx])

        from plugin.framework.thread_guard import on_main_thread

        # Cached spill model is for the deferred UI write only. Passing it
        # into session_key off-main calls getURL / locate (see session_key).
        scalar_doc = doc if on_main_thread() else None
        return scalar_for_list_result(ctx, code, result, worker_data=worker_data, doc=scalar_doc, data_token=data_token)

    return py_addin_cell_value(result)


def _format_error_for_display(exc: BaseException) -> str:
    """Cell-safe error text without importing ``plugin.framework.client.llm_client``."""
    err: Exception = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
    # Bugfix (#402): A TimeoutError during add-in execution is an internal host marshal or
    # synchronization timeout, NOT a user venv execution timeout or HTTP request timeout.
    if isinstance(exc, TimeoutError):
        return _("Error: Main-thread execution timed out ({0})").format(str(exc))
    msg = format_error_message(err)
    if msg.startswith("Error:") or msg.startswith("#"):
        return msg
    return _format_python_addin_worker_error(msg)


def _format_python_addin_worker_error(message: str) -> str:
    """Map common worker failures to short Settings → Python / Test guidance."""
    text = (message or "").strip() or _("Unknown error")
    lower = text.lower()
    if "no python executable found under configured venv" in lower or "venv not found" in lower:
        return _("Error: Python venv not found. Open Settings → Python, set the venv path, then Test.")
    if "timed out" in lower or "timeout" in lower:
        return _("Error: Python timed out. Open Settings → Python to raise the timeout, or Test the venv.")
    if text.startswith("Error:") or text.startswith("#"):
        return text
    return _("Error: {0}").format(text)


# Bugfix: What was wrong: attribute access such as obj.data[0] matched because . was outside [A-Za-z0-9_],
# so a trailing scalar was not peeled as the index.
# How it happened: The negative lookbehind (?<![A-Za-z0-9_]) did not exclude the dot attribute operator.
# Why this change fixes it: Lookbehind becomes (?<![A-Za-z0-9_.]) so preceding . attribute accesses do not match.
_INDEXED_BINDING_RE = re.compile(r"(?<![A-Za-z0-9_.])(?:data|ranges)\[")


def _code_uses_indexed_multi_data(code: str) -> bool:
    """True when inline code references bare ``data[n]`` or ``ranges[n]`` (a preceding ``.`` does not count; all PY args are data ranges)."""
    return bool(_INDEXED_BINDING_RE.search(code or ""))


def _py_scoped_dir_bindings(doc: Any | None) -> dict[str, Any]:
    """Document folder for in-cell folder SQL.

    On-main with the calling document: ``get_document_directory`` (UNO
    ``getURL()``). Off-main the doc is only passed through, so no folder is
    read. Bind ``scoped_dir`` as ``None`` when no folder is known so join
    formulas do not ``NameError`` (file joins then fail loud).
    """
    from plugin.framework.thread_guard import on_main_thread

    if doc is not None and on_main_thread():
        try:
            from plugin.doc.document_research import get_document_directory

            folder = get_document_directory(doc)
        except Exception:
            log.debug("scoped_dir binding failed", exc_info=True)
            folder = None
        if folder:
            return {"scoped_dir": folder}
    return {"scoped_dir": None}


def get_python_init_kwargs(ctx: Any, doc: Any | None = None) -> dict[str, Any]:
    """Init-script kwargs for the calling document, read on the UI thread.

    Off-main the doc is only passed through (its document scripts are UNO),
    and with no doc there is nothing to read: both return ``{}``.
    """
    try:
        from plugin.framework.thread_guard import on_main_thread

        if doc is None or not on_main_thread():
            return {}

        from plugin.scripting.document_scripts import build_python_eval_init_kwargs

        try:
            from plugin.calc.python.workbook_lifecycle import ensure_calc_workbook_unload_resets_python

            ensure_calc_workbook_unload_resets_python(ctx, doc)
        except Exception:
            log.debug("python workbook unload listener install failed", exc_info=True)
        return build_python_eval_init_kwargs(doc)
    except Exception:
        log.debug("get_python_init_kwargs failed", exc_info=True)
    return {}


def _py_timing_code_label(code: str) -> str:
    """Short greppable id: JSON helper name, else a compact code prefix."""
    src = code or ""
    matched = _PY_HELPER_IN_SPEC_RE.search(src)
    if matched:
        return matched.group(1)
    return " ".join(src.split())[:48]


def _emit_py_timing(*, code: str, total_ms: int, pack_ms: int, ipc_ms: int, image_ms: int, cached: bool, pass_start: float, n: int, pass_sum_ms: int, last_end: float) -> None:
    """Log absolute per-call ms plus recalc-clump totals (DEBUG)."""
    pass_wall_ms = int(round((last_end - pass_start) * 1000))
    pass_outside_ms = max(0, pass_wall_ms - pass_sum_ms)
    log.debug("py_timing code=%s n=%s total_ms=%s pack_ms=%s ipc_ms=%s image_ms=%s cached=%s | pass_wall_ms=%s pass_sum_ms=%s pass_outside_ms=%s", _py_timing_code_label(code), n, total_ms, pack_ms, ipc_ms, image_ms, 1 if cached else 0, pass_wall_ms, pass_sum_ms, pass_outside_ms)


def clear_python_addin_cache() -> None:
    """Clear formula result cache across all threads (e.g. on session reset or document reload)."""
    with _MATRIX_SCALAR_SESSIONS_LOCK:
        _MATRIX_SCALAR_SESSIONS.clear()


def _spill_timer_finished(timer: Any) -> bool:
    """True once a ``threading.Timer`` has fired or been cancelled.

    Test doubles omit ``finished``; those stay until fire/cancel drops them
    by identity.
    """
    finished = getattr(timer, "finished", None)
    is_set = getattr(finished, "is_set", None)
    if not callable(is_set):
        return False
    try:
        return bool(is_set())
    except Exception:
        return False


def _prune_finished_spill_timers_locked() -> None:
    """Drop fired/cancelled timers. Caller holds ``_PENDING_SPILL_LOCK``."""
    _PENDING_SPILL_TIMERS[:] = [(key, timer) for key, timer in _PENDING_SPILL_TIMERS if not _spill_timer_finished(timer)]


def _forget_spill_timer(timer: threading.Timer) -> None:
    """Remove *timer* when its callback starts so the closure can be collected."""
    with _PENDING_SPILL_LOCK:
        _PENDING_SPILL_TIMERS[:] = [(key, existing) for key, existing in _PENDING_SPILL_TIMERS if existing is not timer and not _spill_timer_finished(existing)]


def _new_spill_timer(delay_sec: float, callback: Any) -> threading.Timer:
    """Timer that leaves ``_PENDING_SPILL_TIMERS`` as soon as it fires.

    After ``run()`` the Timer, its callback, and everything that callback
    closed over (ctx, code, grid, UNO document) must drop out of the
    registry. An append-only list keeps them until process exit.
    """
    pending: list[threading.Timer] = []

    def _fire(*args: Any, **kwargs: Any) -> None:
        from plugin.framework.thread_guard import set_background_task

        set_background_task("spill_timer")
        try:
            _forget_spill_timer(pending[0])
            callback(*args, **kwargs)
        finally:
            set_background_task(None)

    timer = threading.Timer(delay_sec, _fire)
    pending.append(timer)
    return timer


def _off_main_spill_lifecycle_key(doc: Any | None) -> str:
    """Workbook key for an off-main spill timer, without UNO off the UI thread."""
    from plugin.framework.thread_guard import on_main_thread
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key, lifecycle_key_if_known

    if doc is not None and on_main_thread():
        try:
            return _lifecycle_key(doc) or ""
        except Exception:
            log.debug("off-main spill lifecycle key failed", exc_info=True)
    return lifecycle_key_if_known(doc)


def _register_spill_timer(lifecycle_key: str, timer: threading.Timer, *, token: str = "") -> None:
    setattr(timer, "_wa_spill_token", token)
    with _PENDING_SPILL_LOCK:
        _prune_finished_spill_timers_locked()
        keep: list[tuple[str, threading.Timer]] = []
        for key, existing in _PENDING_SPILL_TIMERS:
            if existing is timer:
                continue
            if token and key == lifecycle_key and getattr(existing, "_wa_spill_token", "") == token:
                try:
                    existing.cancel()
                except Exception:
                    pass
                continue
            keep.append((key, existing))
        _PENDING_SPILL_TIMERS[:] = keep
        _PENDING_SPILL_TIMERS.append((lifecycle_key, timer))


def start_deferred_sheet_timer(delay_sec: float, callback: Any, *, lifecycle_key: str = "") -> threading.Timer:
    """Shared 0.1s Timer for spill writes and geometric sheet-modify repair.

    Lives here so Layer C allows the same ``threading.Timer`` site as
    ``perform_deferred_spill``. The timer thread only ``post_to_main_thread``;
    UNO writes run on the UI thread.
    """
    timer = _new_spill_timer(delay_sec, callback)
    if lifecycle_key:
        _register_spill_timer(lifecycle_key, timer)
    timer.start()
    return timer


def cancel_pending_spill_timers(lifecycle_key: str) -> None:
    """Cancel deferred spill timers for a workbook that is unloading.

    Also drops timers that already fired or were cancelled under some other
    key. Those entries kept their closures after the callback returned.
    """
    with _PENDING_SPILL_LOCK:
        keep: list[tuple[str, threading.Timer]] = []
        for key, timer in _PENDING_SPILL_TIMERS:
            finished = _spill_timer_finished(timer)
            if key == lifecycle_key or finished:
                if key == lifecycle_key and not finished:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                continue
            keep.append((key, timer))
        _PENDING_SPILL_TIMERS[:] = keep


def clear_in_memory_spill_state(*, doc_url: str = "", lifecycle_key: str = "") -> None:
    """Drop instance-scoped spill maps. UD property is left for a later open of the same file."""
    cancel_pending_spill_timers(lifecycle_key)
    from plugin.calc.python.sheet_modify import cancel_pending_sheet_timers

    if lifecycle_key:
        cancel_pending_sheet_timers(lifecycle_key)
    if doc_url:
        cancel_pending_sheet_timers(doc_url)
    if lifecycle_key:
        # Sheet listeners are keyed by lifecycle id, not the file URL, so an
        # unload that only matched doc_url left the dispatcher registered.
        for skey in [k for k in SHEET_MODIFY_LISTENERS if k[0] == lifecycle_key]:
            SHEET_MODIFY_LISTENERS.pop(skey, None)
        # Unsaved spill rows are keyed by lifecycle id because getURL() is
        # empty. Sweep this lifecycle id only. Skipping the sweep when
        # doc_url is empty leaves the row behind, and sweeping on an empty
        # URL would drop every other untitled book.
        LOADED_DOCUMENTS.discard(lifecycle_key)
        for key in [k for k in SPILL_REGISTRY if k[0] == lifecycle_key]:
            SPILL_REGISTRY.pop(key, None)
    if doc_url:
        LOADED_DOCUMENTS.discard(doc_url)
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_url]:
            SPILL_REGISTRY.pop(key, None)
        for skey in [k for k in SHEET_MODIFY_LISTENERS if k[0] == doc_url]:
            SHEET_MODIFY_LISTENERS.pop(skey, None)
    clear_python_addin_cache()


def execute_python_addin(ctx: Any, code: str, data: Any = None, true_strings: set[str] | None = None, false_strings: set[str] | None = None, *, doc: Any | None = None) -> Any:
    """Run *code* in the user venv and return a Calc-compatible scalar (or error string)."""
    with sync_host_dispatch():
        from plugin.calc.python.formula_locator_cache import bind_formula_locate_memo

        with bind_formula_locate_memo():
            return _execute_python_addin_impl(ctx, code, data, true_strings, false_strings, doc=doc)


def _execute_python_addin_impl(ctx: Any, code: str, data: Any = None, true_strings: set[str] | None = None, false_strings: set[str] | None = None, *, doc: Any | None = None) -> Any:
    log.debug("=== PYTHON(%r, data=%r) ===", code, data)
    timings = PYTHON_TIMINGS_LOG
    t_enter = time.perf_counter() if timings else 0.0
    if timings:
        last_end = getattr(_PY_PASS_STATS, "last_end", None)
        if last_end is None or (t_enter - last_end) > _PY_PASS_GAP_SEC:
            _PY_PASS_STATS.pass_start = t_enter
            _PY_PASS_STATS.n = 0
            _PY_PASS_STATS.sum_ms = 0
    pack_ms = 0
    ipc_ms = 0
    image_ms = 0
    used_cache = False
    target_doc: Any = doc
    try:
        t_pack = time.perf_counter() if timings else 0.0
        args = split_python_addin_data_args(data)
        # Named-range args arrive as already-evaluated values from Calc.
        # Relative names (Sheet!A4:J39) shift from the calling cell — UNO
        # Name Manager still shows the definition with the header. Absolute
        # $A$4:$J$39 is required so data[0].to_pandas() keeps Region/Channel.
        # Geometric predecessor is a Calc-only DAG token. Strip it before
        # calc_addin_args_from_split (1 vs N flips `data` to a list) and
        # before the matrix-index peel (a leftover 1×1 pred becomes index_arg).
        # *doc* is the add-in caller argument (the calling document). There is
        # no other source: no front window, open-documents search, or cache.
        from plugin.calc.python.geometric_recalc import ensure_geometric_strip_index_for_eval, maybe_strip_geometric_eval_args

        # Same-process hydrate: client/URP attach cannot fill soffice's map.
        ensure_geometric_strip_index_for_eval(target_doc, ctx)
        # UI-thread target_doc is a real workbook_key. Off-main never strips
        # (the doc is only passed through there, so no key is read).
        args = maybe_strip_geometric_eval_args(code, args, doc=target_doc)
        py_data = calc_addin_args_from_split(args, true_strings, false_strings)
        log.debug("PYTHON parsed py_data: %r", py_data)
        is_multi = len(args) > 1
        index_arg = None
        if py_data is not None:
            if is_multi and not _code_uses_indexed_multi_data(code):
                last_arg = args[-1]
                if not isinstance(last_arg, (list, tuple)) or count_cells(py_data[-1]) == 1:
                    idx_val = py_data[-1]
                    while isinstance(idx_val, list) and idx_val:
                        idx_val = idx_val[0]
                    index_arg = idx_val
                    py_data = py_data[:-1]
                    args = args[:-1]
                    is_multi = len(args) > 1
                    if py_data:
                        if not is_multi:
                            py_data = py_data[0]
                    else:
                        py_data = None
            elif is_scalar_index_arg(py_data) and not is_split_grid(py_data):
                # Single cell may be a matrix index and/or the data value itself.
                index_arg = _unwrap_single_cell(py_data)
        max_cells = configured_python_max_data_cells()
        if py_data is not None:
            if is_multi:
                size_err = check_python_multi_data_size(py_data, max_cells=max_cells)
            else:
                size_err = check_python_data_size(py_data, max_cells=max_cells)
            if size_err:
                ret = f"Error: {size_err}"
                log.debug("PYTHON returning size error: %r", ret)
                _record_py_diagnostic(ctx, code, None, status="error", message=ret, doc=target_doc)
                return ret
            worker_data = pack_calc_multi_data_for_wire(py_data) if is_multi else pack_calc_data_for_wire(py_data)
        else:
            worker_data = None
        if timings:
            pack_ms = int(round((time.perf_counter() - t_pack) * 1000))
        # Synchronous: =PY() runs during Calc recalc; UI event pumping from
        # run_blocking_in_thread can re-enter the formula engine and yield #VALUE!.
        # target_doc is the add-in caller argument (strip hydrate + worker session).

        tid = threading.get_ident()
        sk = session_key(ctx, code, doc=target_doc)
        unique_origin = bool(sk[4])
        now = time.monotonic()
        cached = None
        data_token: str | None = None
        if unique_origin:
            data_token = _worker_data_token(worker_data)
            cache_key = (tid, sk, data_token)
            with _MATRIX_SCALAR_SESSIONS_LOCK:
                _expire_matrix_sessions_locked(now)
                cached = _MATRIX_SCALAR_SESSIONS.get(cache_key)
                if cached is not None and (now - cached.timestamp) > _PY_PASS_GAP_SEC:
                    _MATRIX_SCALAR_SESSIONS.pop(cache_key, None)
                    cached = None
        if isinstance(cached, WorkerResultSession) and cached.next_index < len(cached.flat):
            used_cache = True
            res = {"status": "ok", "result": cached.raw}
        else:
            from plugin.framework.thread_guard import in_sync_host_dispatch, on_main_thread

            # Off-main the caller doc is only passed through: reading its URL or
            # init script is UNO. Such a call runs with no shared session id and
            # no init kwargs rather than borrowing another workbook's.
            on_main = on_main_thread()
            session_id = workbook_session_id(ctx, doc=target_doc) if on_main else None
            init_kwargs = get_python_init_kwargs(ctx, doc=target_doc)

            log.debug(
                "PYTHON eval: target_doc=%s session_id=%r has_init=%s on_main=%s in_sync_host=%s",
                target_doc is not None,
                session_id,
                bool(init_kwargs),
                on_main_thread(),
                in_sync_host_dispatch(),
            )
            t_ipc = time.perf_counter() if timings else 0.0
            res = run_code_in_user_venv(
                ctx,
                code,
                data=worker_data,
                bindings=_py_scoped_dir_bindings(target_doc),
                session_id=session_id,
                # Formula recalc must not mutate the document via writeragent tools.
                python_tool_domain="",
                **init_kwargs,
            )

            if timings:
                ipc_ms = int(round((time.perf_counter() - t_ipc) * 1000))
        log.debug("PYTHON res from worker: %r", res)
        if res.get("status") == "ok":
            result = res.get("result")
            if isinstance(result, dict) and _is_helper_error_dict(result):
                # Worker status is ok because the helper returned a dict instead of raising.
                # An ok diagnostic with empty stdout is dropped, so the message would never
                # reach the LibrePy sidebar. Map here, before the image scan.
                err_text = _helper_error_log_message(result)
                cell = py_addin_cell_value(result)
                _record_py_diagnostic(ctx, code, res, status="error", message=err_text, doc=target_doc)
                log.debug("PYTHON returning formula error for helper error dict")
                return cell
            _record_py_diagnostic(ctx, code, res, status="ok", doc=target_doc)
            log.debug("PYTHON raw result: %r (type: %s)", result, type(result).__name__)
            images = find_image_payloads(result)
            if images:
                t_img = time.perf_counter() if timings else 0.0
                for i, img in enumerate(images):
                    insert_image_result_on_sheet(ctx, img, code=code, doc=target_doc, image_index=i, image_count=len(images), notify_cell=True)
                if timings:
                    image_ms = int(round((time.perf_counter() - t_img) * 1000))
                return _("Image inserted") if len(images) == 1 else _("Images inserted")
            # Pass data_token: the digest was already computed for the cache key.
            final_ret = finalize_python_return(ctx, code, result, index_arg=index_arg, worker_data=worker_data, doc=target_doc, data_token=data_token)
            log.debug("PYTHON returning scalar: %r (type: %s)", final_ret, type(final_ret).__name__)
            return final_ret

        raw_message = str(res.get("message") or res.get("error") or "")
        cell = _addin_result_for_worker_status(res)
        if isinstance(cell, str):
            _record_py_diagnostic(ctx, code, res, status="error", message=cell, doc=target_doc)
            log.debug("PYTHON returning worker error: %r", cell)
        else:
            _record_py_diagnostic(ctx, code, res, status="error", message=raw_message, doc=target_doc)
            log.debug("PYTHON returning formula error for worker failure")
        return cell
    except Exception as e:
        log.exception("PYTHON unexpected error during execution")
        err_msg = _format_error_for_display(e)
        _record_py_diagnostic(ctx, code, None, status="error", message=err_msg, traceback=str(e), doc=target_doc)
        log.debug("PYTHON returning exception wrapper: %r", err_msg)
        return err_msg
    finally:
        if timings:
            total_ms = int(round((time.perf_counter() - t_enter) * 1000))
            _PY_PASS_STATS.n = getattr(_PY_PASS_STATS, "n", 0) + 1
            _PY_PASS_STATS.sum_ms = getattr(_PY_PASS_STATS, "sum_ms", 0) + total_ms
            _PY_PASS_STATS.last_end = time.perf_counter()
            _emit_py_timing(code=code, total_ms=total_ms, pack_ms=pack_ms, ipc_ms=ipc_ms, image_ms=image_ms, cached=used_cache, pass_start=getattr(_PY_PASS_STATS, "pass_start", t_enter), n=_PY_PASS_STATS.n, pass_sum_ms=int(_PY_PASS_STATS.sum_ms), last_end=_PY_PASS_STATS.last_end)


def _diagnostics_workbook_key(ctx: Any, doc: Any | None = None) -> str:
    """Stable workbook key for the diagnostics store (UNO-light best effort).

    Uses the calling document only, and only reads an existing session id
    (``existing_calc_session_id`` does not mint a UDProp).
    """
    try:
        from plugin.framework.thread_guard import on_main_thread

        if doc is None or not on_main_thread():
            return "unknown"
        from plugin.scripting.session_manager import existing_calc_session_id

        existing = existing_calc_session_id(doc)
        if existing:
            return existing
    except Exception:
        log.debug("diagnostics workbook key failed", exc_info=True)
    return "unknown"


def _record_py_diagnostic(ctx: Any, code: str, res: dict[str, Any] | None, *, status: str, message: str = "", traceback: str = "", doc: Any | None = None) -> None:
    """Record stdout/errors for the LibrePy sidebar without extra UNO work.

    Skips successful evaluations with empty stdout so the log stays actionable.
    """
    try:
        from plugin.calc.python.diagnostics import record_python_eval

        stdout = ""
        tb = traceback
        msg = message
        if isinstance(res, dict):
            stdout = str(res.get("stdout") or "")
            if not msg:
                msg = str(res.get("message") or res.get("error") or "")
            if not tb:
                raw_tb = res.get("traceback")
                tb = str(raw_tb) if raw_tb else ""
        if status == "ok" and not (stdout or "").strip():
            return
        record_python_eval(workbook_key=_diagnostics_workbook_key(ctx, doc=doc), code=code or "", status=status, message=msg, stdout=stdout, traceback=tb)
    except Exception:
        # Never break formula evaluation for diagnostics UI.
        log.debug("record_python_eval failed", exc_info=True)
