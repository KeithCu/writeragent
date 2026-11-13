# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc add-in value conversion, error mapping, and result coercion helpers.

Cell outcomes for ``=PY()`` fall into four kinds:

- **Payload NaN** (quiet NaN carrying a Calc ``FormulaError`` in its low 32 bits):
  ``py_addin_cell_value`` (error tokens and helper error dicts) and
  ``addin_result_for_worker_status`` (failed worker runs other than setup/stop codes).
  LibreOffice's ``GetDoubleErrorValue`` (``include/formula/errorcodes.hxx``) reads
  ``nan_parts.fraction_lo`` and ``ScInterpreter::PushDouble``
  (``sc/source/core/tool/interpr4.cxx``) turns it into a Calc error so ``ISERROR`` is true.
- **Spill neighbour error or void**: ``coerce_spill_value`` records error metadata
  and ``spill.perform_deferred_spill`` writes ``setDataArray`` of ``None`` (UNO void,
  which ``lcl_PutDataArray`` in ``sc/source/ui/unoobj/cellsuno.cxx`` turns into
  ``FormulaError::NotAvailable`` / ``#N/A``) or ``=#ERRn!`` formulas.
- **``"#SPILL!"`` text**: ``spill.prepare_auto_spill``, returned by
  ``function.finalize_python_return`` (``ISERROR`` is false).
- **``"Error: …"`` text**: setup and stop worker codes in
  ``addin_result_for_worker_status``, host exceptions in
  ``format_addin_error_for_display``, index/size errors in ``function.py``,
  and missing-caller in ``addin_impl.py``.

Worker failure codes in ``addin_result_for_worker_status`` map as follows:

- ``VENV_NOT_FOUND`` → venv guidance text (``Settings → Python``).
- ``VENV_TIMEOUT``, ``EXECUTION_TIMEOUT`` → timeout guidance text (``Settings → Python``).
- ``USER_STOPPED`` → ``"Error:"`` plus the message via ``_error_cell_text``.
- Any other code (including a missing code) → payload NaN selected by
  ``worker_exception_error_code``.

``LO_ERROR_TOKENS`` in ``plugin/scripting/venv/coerce.py`` mirrors
``_CALC_ERROR_TOKEN_CODES``; ``tests/calc/python/test_addin_values.py`` pins that both
agree.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import struct
from typing import Any, cast

from plugin.calc.datetime_wire import (
    datetime_serial,
    duration_serial_from_iso,
    match_iso_duration,
    match_iso_temporal,
    time_day_fraction,
)
from plugin.framework.errors import format_error_message
from plugin.framework.i18n import _
from plugin.scripting.payload_codec import wire_cell_count

log = logging.getLogger(__name__)

_CALC_ERROR_TOKEN_CODES: dict[str, int] = {
    "#VALUE!": 519,
    "#NUM!": 503,
    "#DIV/0!": 532,
    "#N/A": 0x7FFF,
    "#N/A N/A": 0x7FFF,
    "#REF!": 524,
    "#NAME?": 525,
    "#NULL!": 521,
}

_ERR_VALUE = _CALC_ERROR_TOKEN_CODES["#VALUE!"]
ERR_NA = _CALC_ERROR_TOKEN_CODES["#N/A"]

_WORKER_EXC_CODES: dict[str, int] = {
    "ZeroDivisionError": _CALC_ERROR_TOKEN_CODES["#DIV/0!"],
    "OverflowError": _CALC_ERROR_TOKEN_CODES["#NUM!"],
    "FloatingPointError": _CALC_ERROR_TOKEN_CODES["#NUM!"],
    "NameError": _CALC_ERROR_TOKEN_CODES["#NAME?"],
    "UnboundLocalError": _CALC_ERROR_TOKEN_CODES["#NAME?"],
}

_DUE_TO_EXC_RE = re.compile(r" due to: ([A-Za-z_][A-Za-z0-9_]*): ")

# Worker result codes whose run never reached a verdict on the user's code.
# These and VENV_NOT_FOUND / USER_STOPPED stay text (table in the module
# docstring); every other code is a payload NaN so ISERROR sees it.
_TIMEOUT_CODES: frozenset[str] = frozenset({"VENV_TIMEOUT", "EXECUTION_TIMEOUT"})


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


def is_helper_error_dict(val: Any) -> bool:
    """True only for the coerce.error_result dict (``plugin/scripting/venv/results.py`` ``error_result``).

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
    return _WORKER_EXC_CODES.get(code.strip(), _ERR_VALUE)


def helper_error_log_message(err: dict[str, Any]) -> str:
    code = str(err["code"]).strip()
    message = str(err["message"])
    helper = err.get("helper")
    if isinstance(helper, str) and helper.strip():
        return f"{helper.strip()}: {code}: {message}"
    return f"{code}: {message}"


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
    if isinstance(val, dict) and is_helper_error_dict(val):
        # Helper error dicts must become calc_formula_error_double, not cell text, so ISERROR sees a real formula error.
        log.error("=PY() worker failure: %s", helper_error_log_message(val))
        return calc_formula_error_double(_calc_code_for_helper_error(val["code"]))
    return cast("float | str | tuple[Any, ...]", _map_addin_error_tokens(to_calc_compatible(val)))


def flat_result_equal(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    """Compare flat worker result tuples across matrix session recalculations.

    Identical Calc error doubles (quiet NaNs) must compare equal: Python ``!=`` on NaN
    always fails, which would reset matrix next_index every recalc.
    """
    if len(left) != len(right):
        return False
    for val_left, val_right in zip(left, right):
        if isinstance(val_left, float) and isinstance(val_right, float) and not (math.isfinite(val_left) and math.isfinite(val_right)):
            if struct.pack("d", val_left) != struct.pack("d", val_right):
                return False
        elif val_left != val_right:
            return False
    return True


def addin_result_for_worker_status(res: dict[str, Any]) -> float | str:
    """Return add-in cell result for non-ok worker status (payload NaN error or text message).

    Setup and stop codes stay text so the user sees actionable guidance; every other
    failure returns a quiet-NaN with FormulaError payload so ISERROR behaves correctly.
    """
    code = res.get("code")
    message = str(res.get("message") or res.get("error") or "")
    if code == "VENV_NOT_FOUND":
        return _("Error: Python venv not found. Open Settings → Python, set the venv path, then Test.")
    if code in _TIMEOUT_CODES:
        return _("Error: Python timed out. Open Settings → Python to raise the timeout, or Test the venv.")
    if code == "USER_STOPPED":
        return _error_cell_text(message)
    log.error("=PY() worker failure: %s", message)
    return calc_formula_error_double(worker_exception_error_code(message))


def coerce_index(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
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
    # ScUnoAddInCall unpacks only double and string; map bool to 1.0/0.0 so logical formulas work.
    if isinstance(val, bool):
        return 1.0 if val else 0.0

    # Integers beyond float64 range return str(val) instead of raising OverflowError.
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
            max_cols = max(len(row) for row in rows)
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


def flatten_result_values(result: Any) -> list[Any]:
    """Row-major flattening for list / nested list worker results."""
    if not isinstance(result, (list, tuple)):
        return [result]
    return [cell for row in result_as_spill_grid(result) for cell in row]


def is_scalar_index_arg(py_data: list[Any] | list[list[Any]] | None) -> bool:
    """True when arg 1 is one number (matrix index), not a data range."""
    if py_data is None:
        return False
    if wire_cell_count(py_data) != 1:
        return False
    val = unwrap_single_cell(py_data)
    return isinstance(val, (int, float)) and not isinstance(val, bool) and not math.isnan(val)


def unwrap_single_cell(py_data: Any) -> Any:
    """Unwrap ``[[v]]`` / ``[v]`` / scalar to the inner value."""
    val = py_data
    while isinstance(val, list) and len(val) == 1:
        val = val[0]
    return val


def result_as_spill_grid(result: list[Any] | tuple[Any, ...]) -> list[list[Any]]:
    """Normalize a 1D list or 2D list-of-lists into a rectangular spill grid."""
    if not result:
        return []
    if any(isinstance(row, (list, tuple)) for row in result):
        return [list(row) if isinstance(row, (list, tuple)) else [row] for row in result]
    return [[x] for x in result]


def coerce_spill_value(val: Any, null_dt: datetime.date = datetime.date(1899, 12, 30)) -> tuple[Any, dict[str, Any]]:
    """Convert raw grid cell value to Calc-compatible primitive plus temporal metadata.

    Returns (calc_val, meta). meta always has ``is_temporal`` and ``is_empty``;
    temporal values add ``input_category`` and ``serial``; Calc error values
    add ``is_calc_error`` and ``error_code``.
    """
    if val is None or type(val).__name__ in ("NaTType", "NAType"):
        return "", {"is_temporal": False, "is_empty": True}
    if isinstance(val, int):
        # bool is an int. to_calc_compatible maps it to 1.0/0.0 and OverflowError to decimal text.
        return to_calc_compatible(val), {"is_temporal": False, "is_empty": False}
    if isinstance(val, float):
        return float(val), {"is_temporal": False, "is_empty": False}

    if isinstance(val, datetime.datetime):
        dt = val.replace(tzinfo=None) if val.tzinfo is not None else val
        serial = datetime_serial(dt, null_dt)
        return serial, {"is_temporal": True, "input_category": "datetime", "serial": serial, "is_empty": False}

    if isinstance(val, datetime.date):
        serial = datetime_serial(val, null_dt)
        return serial, {"is_temporal": True, "input_category": "date", "serial": serial, "is_empty": False}

    if isinstance(val, datetime.time):
        serial = time_day_fraction(val)
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
                    return coerce_spill_value(item, null_dt)
            except Exception:
                log.debug("numpy datetime item() failed", exc_info=True)
        text = str(val)
        if text in ("NaT", "NaTType"):
            return "", {"is_temporal": False, "is_empty": True}

    if kind == "m":
        to_pytd = getattr(val, "item", None)
        if callable(to_pytd):
            try:
                item = to_pytd()
                if isinstance(item, datetime.timedelta):
                    return coerce_spill_value(item, null_dt)
            except Exception:
                log.debug("numpy timedelta item() failed", exc_info=True)

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
            if err == ERR_NA:
                return None, {"is_temporal": False, "is_empty": False, "is_calc_error": True, "error_code": err}
            return "", {"is_temporal": False, "is_empty": False, "is_calc_error": True, "error_code": err}
        if match_iso_duration(stripped):
            try:
                serial = duration_serial_from_iso(stripped)
                return serial, {"is_temporal": True, "input_category": "duration", "serial": serial, "is_empty": False}
            except (ValueError, OverflowError):
                pass
        cat = match_iso_temporal(stripped)
        if cat is not None:
            try:
                if cat == "date":
                    parsed: datetime.date | datetime.datetime | datetime.time = datetime.date.fromisoformat(stripped)
                elif cat == "datetime":
                    parsed = datetime.datetime.fromisoformat(stripped.replace(" ", "T"))
                else:
                    parsed = datetime.time.fromisoformat(stripped)
                return coerce_spill_value(parsed, null_dt)
            except ValueError:
                pass
        return val, {"is_temporal": False, "is_empty": False}

    return to_calc_compatible(val), {"is_temporal": False, "is_empty": False}


def spill_value_from_json(kind: str, value: Any) -> Any:
    """Restore the "nan" / "inf" / "-inf" text written by save_spill_registry_for_doc (JSON has no non-finite floats)."""
    if kind != "n" or not isinstance(value, str):
        return value
    return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}.get(value.lower(), value)


def format_addin_error_for_display(exc: BaseException) -> str:
    """Cell text for an exception raised while running =PY()."""
    # A TimeoutError here is the host's main-thread marshal or sync timeout, not the user's code or an HTTP request, so it gets its own message.
    if isinstance(exc, TimeoutError):
        return _("Error: Main-thread execution timed out ({0})").format(str(exc))
    return _error_cell_text(format_error_message(exc))


def _error_cell_text(message: str) -> str:
    """Ensure an error cell message starts with ``Error:`` or ``#``."""
    text = (message or "").strip() or _("Unknown error")
    if text.startswith("Error:") or text.startswith("#"):
        return text
    return _("Error: {0}").format(text)
