# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PY() execution and return helpers (venv worker); no LLM imports."""

from __future__ import annotations

from contextlib import contextmanager
import datetime
import logging
import math
import re
import threading
import time
from typing import Any, cast

from plugin.calc.calc_addin_data import (
    calc_addin_args_from_split,
    check_python_data_size,
    check_python_multi_data_size,
    count_cells,
    pack_calc_data_for_wire,
    pack_calc_multi_data_for_wire,
    split_python_addin_data_args,
)
from plugin.calc.datetime_wire import (
    coalesce_temporal_apply_rects,
    duration_serial_from_iso,
    match_iso_duration,
    match_iso_temporal,
    should_preserve_temporal_format,
)
from plugin.calc.inspector import _format_category_from_type
from plugin.calc.python.formula_locator_cache import (
    is_matching_py_formula,
    locate_formula_cell_in_doc,
)
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
# Keys include repr(worker_data) so the same formula with different data args
# does not share a session. repr of a large grid is expensive and a weak identity;
# a later change could use packed-payload digest + cell count. Do not key on id():
# recals would collide. Two formulas with the same code but different data must
# stay on separate sessions (see tests/calc/python/test_function.py).
_MATRIX_SCALAR_SESSIONS_LOCK = threading.Lock()
_MATRIX_SCALAR_SESSIONS: dict[tuple[int, tuple, str], WorkerResultSession] = {}


# Recalc-clump timings for DEBUG ``py_timing`` lines (not asctime deltas).
# Flip to True in this file when measuring workbook-open / recalc cost; leave False in commits.
PYTHON_TIMINGS_LOG = False
_PY_PASS_STATS = threading.local()
_PY_PASS_GAP_SEC = 2.0
_PY_HELPER_IN_SPEC_RE = re.compile(r"""["']helper["']\s*:\s*["'](\w+)["']""")


def flatten_result_values(result: Any) -> list:
    """Row-major flattening for list / nested list worker results."""
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
    return count_cells(py_data) == 1


def _unwrap_single_cell(py_data: Any) -> Any:
    """Unwrap ``[[v]]`` / ``[v]`` / scalar to the inner value."""
    val = py_data
    while isinstance(val, list) and len(val) == 1:
        val = val[0]
    return val


def result_to_calc_grid(result: Any, *, include_dataframe_header: bool = True) -> Any:
    """Normalize worker results for Calc consumers.

    DataFrame envelopes become a labeled 2D grid (header row + body) by default.
    Lists/ndarrays (already unpacked on host) pass through unchanged.
    """
    if is_dataframe_payload(result):
        cols = list(result.get("columns") or [])
        data = result.get("data")
        return dataframe_to_labeled_grid(cols, data if isinstance(data, list) else [], include_header=include_dataframe_header)
    return result


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


def to_calc_compatible(val: Any) -> float | str | tuple:
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



    if isinstance(val, int):
        return float(val)
    if isinstance(val, float):
        # Computed NaN (or NaN from a numeric grid that contained blanks) is returned as-is.
        # The Calc add-in bridge renders a raw NaN double as a cascading error (#NUM! or #VALUE!).
        # Python None is mapped to "" (empty cell). We intentionally do NOT collapse NaN here.
        # ±inf passes through (may also error in formulas). Do not collapse inf to empty.
        if math.isnan(val):
            return val
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
            if math.isnan(f):
                return f
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
        comps = desktop.getComponents()
        if comps is not None and hasattr(comps, "createEnumeration"):
            enum = comps.createEnumeration()
            while enum and enum.hasMoreElements():
                elem = enum.nextElement()
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and getattr(elem, "getController", lambda: None)():
                    ctrl = elem.getController()
                    model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
                if model and hasattr(model, "getSheets"):
                    return guard_uno(model)
    except Exception:
        pass
    return None


def session_key(ctx: Any, code: str, doc: Any | None = None) -> tuple:
    # Bugfix (#402, #411): Include workbook session_id in key so unsaved documents
    # (where doc_url="") do not collide in the in-memory formula result cache.
    # Do not use getActiveSheet(): full recalc's active sheet is not the formula cell
    # (XAddIn has no calling cell). Unique locate fills sheet+origin; otherwise
    # callers must not share WorkerResultSession.
    doc_url = ""
    sheet_name = ""
    sid = ""
    origin = ""
    try:
        target = doc
        if target is None:
            from plugin.framework.thread_guard import on_main_thread

            # AST lint only treats a bare ``if on_main_thread():`` as a guard.
            if on_main_thread():
                if hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager"):
                    target = _get_calc_doc(ctx)
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

    __slots__ = ("raw", "flat", "next_index")

    def __init__(self, raw: Any, flat: list) -> None:
        self.raw = raw
        self.flat = tuple(flat)
        self.next_index = 0


def scalar_for_list_result(
    ctx: Any,
    code: str,
    result: Any,
    *,
    worker_data: Any = None,
    doc: Any | None = None,
) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: list = [to_calc_compatible(v) for v in flatten_result_values(result)]
    if not flat:
        return ""
    tid = threading.get_ident()
    sk = session_key(ctx, code, doc=doc)
    if len(sk) < 5 or not sk[4]:
        # Ambiguous formula identity: do not share next_index across duplicate =PY() cells.
        return flat[0] if flat else ""
    key = (tid, sk, repr(worker_data))
    with _MATRIX_SCALAR_SESSIONS_LOCK:
        state = _MATRIX_SCALAR_SESSIONS.get(key)
        if not isinstance(state, WorkerResultSession) or state.flat != tuple(flat):
            state = WorkerResultSession(result, flat)
            _MATRIX_SCALAR_SESSIONS[key] = state
        idx = state.next_index
        state.next_index = idx + 1
        if state.next_index >= len(state.flat):
            _MATRIX_SCALAR_SESSIONS.pop(key, None)
    if 0 <= idx < len(state.flat):
        return state.flat[idx]
    return state.flat[-1] if state.flat else ""



# The spill registry tracks coordinates that were spilled by each formula cell.
# Key: (doc_url, sheet_name, formula_row, formula_col)
# Value: list of (spilled_row, spilled_col) coordinates
SPILL_REGISTRY: dict[tuple[str, str, int, int], list[tuple[int, int]]] = {}
LOADED_DOCUMENTS: set[str] = set()
_PENDING_SPILL_LOCK = threading.Lock()
_PENDING_SPILL_TIMERS: list[tuple[str, threading.Timer]] = []

import unohelper
from com.sun.star.util import XModifyListener

# One listener per sheet — SheetModifyDispatcher (Phase 3) or the legacy
# CalcSpillModifyListener when a test constructs it directly.
SHEET_MODIFY_LISTENERS: dict[tuple[str, str], Any] = {}


@contextmanager
def _undo_lock(doc: Any):
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
            pass
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
                    try:
                        if hasattr(um, "lock"):
                            um.lock()
                            locked = True
                    except Exception:
                        pass
    except Exception:
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


class CalcSpillModifyListener(unohelper.Base, XModifyListener):
    """Orphaned-spill cleanup. Walks ``SPILL_REGISTRY`` only.

    Geometric repair must not piggyback on this walk (it does not scan
    formula cells). The registered listener is ``SheetModifyDispatcher``;
    this class stays the spill job. Do not add ``CalcGeometricModifyListener``.
    """
    def __init__(self, ctx: Any, doc_url: str, sheet_name: str) -> None:
        self.ctx = ctx
        self.doc_url = doc_url
        self.sheet_name = sheet_name

    def modified(self, aEvent: Any) -> None:
        try:
            from plugin.framework.thread_guard import on_main_thread

            if not on_main_thread():
                return
            sheet = aEvent.Source
            if sheet is None:
                return

            doc = _get_calc_doc(self.ctx)
            with _undo_lock(doc):
                to_remove = []
                for key, value in list(SPILL_REGISTRY.items()):
                    doc_url, sheet_name, frow, fcol = key
                    if doc_url == self.doc_url and sheet_name == self.sheet_name:
                        try:
                            cell = sheet.getCellByPosition(fcol, frow)
                            formula = cell.getFormula()
                            if not formula or not (("PYTHON" in formula or "PY" in formula)):
                                # Clear previously spilled cells
                                for r, c in value:
                                    if (r, c) != (frow, fcol):
                                        try:
                                            spill_cell = sheet.getCellByPosition(c, r)
                                            spill_cell.clearContents(23)
                                        except Exception:
                                            pass
                                to_remove.append(key)
                        except Exception:
                            log.debug("Failed to inspect formula cell %r", key, exc_info=True)

                if to_remove:
                    for key in to_remove:
                        SPILL_REGISTRY.pop(key, None)
                    if doc is not None:
                        save_spill_registry_for_doc(doc)
        except Exception:
            log.exception("Error in CalcSpillModifyListener.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        SHEET_MODIFY_LISTENERS.pop((self.doc_url, self.sheet_name), None)



def load_spill_registry_for_doc(doc: Any) -> None:
    """Load the document's spill registry from its UserDefinedProperties."""
    try:
        from plugin.doc.udprops import get_document_property
        import json
        raw = get_document_property(doc, "WriterAgentSpillRegistry", None)
        if not isinstance(raw, str) or not raw.strip():
            return
        data = json.loads(raw)
        doc_url = getattr(doc, "getURL", lambda: "")() or ""
        for key, value in data.items():
            parts = key.split(":")
            if len(parts) == 2:
                sheet_name, coords = parts
                row_col = coords.split(",")
                if len(row_col) == 2:
                    frow, fcol = int(row_col[0]), int(row_col[1])
                    spill_coords = [(int(r), int(c)) for r, c in value]
                    SPILL_REGISTRY[(doc_url, sheet_name, frow, fcol)] = spill_coords
    except Exception:
        log.exception("Failed to load spill registry from document property")


def save_spill_registry_for_doc(doc: Any) -> None:
    """Save the document's spill registry to its UserDefinedProperties."""
    try:
        from plugin.doc.udprops import set_document_property
        import json
        doc_url = getattr(doc, "getURL", lambda: "")() or ""
        doc_spills = {}
        for key, value in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_url:
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
        set_document_property(doc, "WriterAgentSpillRegistry", json.dumps(doc_spills))
    except Exception:
        log.exception("Failed to save spill registry to document property")





def _coerce_spill_value(
    val: Any,
    null_dt: datetime.date,
) -> tuple[Any, dict[str, Any]]:
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

    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val), {"is_temporal": False, "is_empty": False}

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
            if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
                return
            doc = _get_calc_doc(ctx)
        if doc is None:
            return

        with _undo_lock(doc):
            current_url = getattr(doc, "getURL", lambda: "")() or ""
            if current_url != doc_url:
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

            sheet = doc.getSheets().getByName(sheet_name)
            if sheet is None:
                return

            if code:
                try:
                    origin_cell = sheet.getCellByPosition(formula_col, formula_row)
                    if not is_matching_py_formula(origin_cell.getFormula(), code):
                        # Formula moved or replaced; do not clear/write stale coordinates.
                        return
                except Exception:
                    log.debug("perform_deferred_spill: origin formula check failed", exc_info=True)
                    return

            reg_key = (doc_url, sheet_name, formula_row, formula_col)
        
            # 1. Clear previously spilled cells
            previous_spills = SPILL_REGISTRY.get(reg_key, [])
            for r, c in previous_spills:
                if (r, c) != (formula_row, formula_col):
                    try:
                        cell = sheet.getCellByPosition(c, r)
                        # Clear contents: VALUE, DATETIME, STRING, FORMULA (23)
                        cell.clearContents(23)
                    except Exception:
                        pass

            # 2. Determine bounds
            num_rows = len(grid)
            num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
            if num_rows == 0 or num_cols == 0:
                SPILL_REGISTRY[reg_key] = []
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
            if num_cols > 1:
                first_row_range = sheet.getCellRangeByPosition(
                    formula_col + 1, formula_row, formula_col + num_cols - 1, formula_row
                )
                first_row_range.setDataArray((tuple(coerced_grid[0][1:]),))

            if num_rows > 1:
                remaining_range = sheet.getCellRangeByPosition(
                    formula_col, formula_row + 1, formula_col + num_cols - 1, formula_row + num_rows - 1
                )
                remaining_range.setDataArray(tuple(tuple(row) for row in coerced_grid[1:]))

            new_spills = []
            for r_offset in range(num_rows):
                for c_offset in range(num_cols):
                    if (r_offset, c_offset) == (0, 0):
                        continue
                    new_spills.append((formula_row + r_offset, formula_col + c_offset))

            SPILL_REGISTRY[reg_key] = new_spills
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


def finalize_python_return(
    ctx: Any,
    code: str,
    result: Any,
    *,
    index_arg: Any = None,
    worker_data: Any = None,
    doc: Any | None = None,
) -> float | str | bool | tuple:
    """Map worker result to a single value Calc's add-in bridge accepts."""
    # Worker egress (payload_codec.child_pack_result + host_unpack_data) always yields plain
    # lists/scalars on the host — NumPy lives only in the venv subprocess, not in LO's Python.
    # DataFrame envelopes become labeled grids (header row + body) so columns survive spill.
    result = result_to_calc_grid(result)

    # Auto-spill check: If it's a list/tuple, index_arg is not provided, and it's not a matrix selection
    is_matrix = False
    if isinstance(result, (list, tuple)) and index_arg is None and len(result) > 0:
        from plugin.framework.config import get_config_bool
        if get_config_bool("scripting.python_auto_spill"):
            try:
                target_doc = doc
                from plugin.framework.thread_guard import on_main_thread

                if target_doc is None and (not on_main_thread() or not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager"))):
                    is_matrix = True
                elif target_doc is None:
                    target_doc = _get_calc_doc(ctx)
                if target_doc is not None:
                    ctrl = target_doc.getCurrentController()
                    if ctrl is not None:
                         selection = ctrl.getSelection()
                         if selection is not None and hasattr(selection, "getRangeAddress"):
                             addr = selection.getRangeAddress()
                             is_matrix = (addr.EndColumn - addr.StartColumn > 0) or (addr.EndRow - addr.StartRow > 0)
            except Exception:
                pass
        else:
            is_matrix = True

        if not is_matrix:
            grid_to_spill = []
            first_elem = result[0]
            if isinstance(first_elem, (list, tuple)):
                grid_to_spill = [list(row) for row in result]
            else:
                grid_to_spill = [[x] for x in result]

            # Get document and sheet to locate formula cell
            try:
                from plugin.framework.thread_guard import on_main_thread

                target_doc = doc
                if target_doc is None and (not on_main_thread() or not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager"))):
                    return to_calc_compatible(grid_to_spill[0][0])
                if target_doc is None:
                    target_doc = _get_calc_doc(ctx)
                if target_doc is not None:
                    doc_url = getattr(target_doc, "getURL", lambda: "")() or ""
                    located = locate_formula_cell_in_doc(ctx, target_doc, code)
                    if located is not None:
                        sheet, _, formula_coord = located
                        sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
                        log.debug("Spill: located formula cell at %r on sheet %r for code %r", formula_coord, sheet_name, code)
                        formula_row, formula_col = formula_coord
                                
                        # Check for collisions synchronously
                        if doc_url not in LOADED_DOCUMENTS:
                            load_spill_registry_for_doc(target_doc)
                            LOADED_DOCUMENTS.add(doc_url)

                        # One dispatcher per sheet (spill cleanup + geometric repair).
                        try:
                            from plugin.calc.python.sheet_modify import (
                                ensure_sheet_modify_listener,
                            )

                            ensure_sheet_modify_listener(ctx, target_doc, sheet)
                        except Exception:
                            log.exception("Failed to register modify listener on sheet")

                        num_rows = len(grid_to_spill)
                        num_cols = max(len(row) for row in grid_to_spill) if num_rows > 0 else 0
                        reg_key = (doc_url, sheet_name, formula_row, formula_col)
                        previous_spills = SPILL_REGISTRY.get(reg_key, [])
                        prev_spill_set = set(previous_spills)

                        log.debug("Spill: previous spills for cell %r: %r", reg_key, previous_spills)

                        try:
                            from com.sun.star.table.CellContentType import EMPTY
                        except ImportError:
                            EMPTY = cast("Any", 0)

                        collides = False
                        for r_idx in range(num_rows):
                            for c_idx in range(num_cols):
                                if r_idx == 0 and c_idx == 0:
                                    continue
                                target_r = formula_row + r_idx
                                target_c = formula_col + c_idx
                                if target_r >= 1048576 or target_c >= 1024:
                                    log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                                    collides = True
                                    break
                                if (target_r, target_c) == (formula_row, formula_col):
                                    continue
                                if (target_r, target_c) in prev_spill_set:
                                    continue
                                cell = sheet.getCellByPosition(target_c, target_r)
                                cell_type = cell.getType()
                                if cell_type != EMPTY:
                                    log.debug(
                                        "Spill: collision: cell at %r (type=%s, val=%r, formula=%r) is not empty",
                                        (target_r, target_c),
                                        cell_type,
                                        cell.getValue() or cell.getString(),
                                        cell.getFormula(),
                                    )
                                    collides = True
                                    break
                            if collides:
                                break

                        if collides:
                            return "#SPILL!"

                        from plugin.framework.queue_executor import post_to_main_thread
                        from plugin.calc.python.workbook_lifecycle import _lifecycle_key

                        spill_lifecycle = _lifecycle_key(target_doc)

                        def _deferred_spill_on_main() -> None:
                            post_to_main_thread(
                                lambda: perform_deferred_spill(
                                    ctx,
                                    doc_url,
                                    sheet_name,
                                    formula_row,
                                    formula_col,
                                    grid_to_spill,
                                    doc=target_doc,
                                    code=code,
                                    lifecycle_key=spill_lifecycle,
                                )
                            )

                        t = threading.Timer(0.1, _deferred_spill_on_main)
                        _register_spill_timer(spill_lifecycle, t)
                        t.start()

                        return to_calc_compatible(grid_to_spill[0][0])
            except Exception:
                log.exception("Error checking spill collision or locating formula cell")

    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = flatten_result_values(result)
            idx = coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return to_calc_compatible(flat[idx])
        
        return scalar_for_list_result(ctx, code, result, worker_data=worker_data, doc=doc)

    return to_calc_compatible(result)



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
        return _(
            "Error: Python venv not found. Open Settings → Python, set the venv path, then Test."
        )
    if "timed out" in lower or "timeout" in lower:
        return _(
            "Error: Python timed out. Open Settings → Python to raise the timeout, or Test the venv."
        )
    if text.startswith("Error:") or text.startswith("#"):
        return text
    return _("Error: {0}").format(text)



def _code_uses_indexed_multi_data(code: str) -> bool:
    """True when inline code references ``data[n]`` or ``ranges[n]`` (all PY args are data ranges)."""
    src = code or ""
    return "data[" in src or "ranges[" in src


def get_python_init_kwargs(ctx: Any, doc: Any | None = None) -> dict[str, Any]:
    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.scripting.document_scripts import build_python_eval_init_kwargs, get_calc_document_from_ctx
        from plugin.scripting.session_manager import get_cached_calc_init_kwargs, record_active_calc_session

        target = doc
        if target is None:
            if on_main_thread():
                target = get_calc_document_from_ctx(ctx)
            else:
                from plugin.scripting.session_manager import off_main_calc_session_is_unambiguous

                # Two open workbooks: cached init belongs to the last focused file, not
                # the one Calc is recalculating (add-in has no calling document).
                if not off_main_calc_session_is_unambiguous():
                    return {}
                return get_cached_calc_init_kwargs()
        if target is not None:
            try:
                from plugin.calc.python.workbook_lifecycle import (
                    ensure_calc_workbook_unload_resets_python,
                )

                ensure_calc_workbook_unload_resets_python(ctx, target)
            except Exception:
                log.debug("python workbook unload listener install failed", exc_info=True)
            kwargs = build_python_eval_init_kwargs(target)
            if kwargs and on_main_thread():
                record_active_calc_session(None, kwargs)
            return kwargs
        return get_cached_calc_init_kwargs()
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


def _emit_py_timing(
    *,
    code: str,
    total_ms: int,
    pack_ms: int,
    ipc_ms: int,
    image_ms: int,
    cached: bool,
    pass_start: float,
    n: int,
    pass_sum_ms: int,
    last_end: float,
) -> None:
    """Log absolute per-call ms plus recalc-clump totals (DEBUG)."""
    pass_wall_ms = int(round((last_end - pass_start) * 1000))
    pass_outside_ms = max(0, pass_wall_ms - pass_sum_ms)
    log.debug(
        "py_timing code=%s n=%s total_ms=%s pack_ms=%s ipc_ms=%s image_ms=%s cached=%s | "
        "pass_wall_ms=%s pass_sum_ms=%s pass_outside_ms=%s",
        _py_timing_code_label(code),
        n,
        total_ms,
        pack_ms,
        ipc_ms,
        image_ms,
        1 if cached else 0,
        pass_wall_ms,
        pass_sum_ms,
        pass_outside_ms,
    )


def clear_python_addin_cache() -> None:
    """Clear formula result cache across all threads (e.g. on session reset or document reload)."""
    with _MATRIX_SCALAR_SESSIONS_LOCK:
        _MATRIX_SCALAR_SESSIONS.clear()


def _register_spill_timer(lifecycle_key: str, timer: threading.Timer) -> None:
    with _PENDING_SPILL_LOCK:
        _PENDING_SPILL_TIMERS.append((lifecycle_key, timer))


def start_deferred_sheet_timer(
    delay_sec: float,
    callback: Any,
    *,
    lifecycle_key: str = "",
) -> threading.Timer:
    """Shared 0.1s Timer for spill writes and geometric sheet-modify repair.

    Lives here so Layer C allows the same ``threading.Timer`` site as
    ``perform_deferred_spill``. The timer thread only ``post_to_main_thread``;
    UNO writes run on the UI thread.
    """
    timer = threading.Timer(delay_sec, callback)
    if lifecycle_key:
        _register_spill_timer(lifecycle_key, timer)
    timer.start()
    return timer


def cancel_pending_spill_timers(lifecycle_key: str) -> None:
    """Cancel deferred spill timers for a workbook that is unloading."""
    with _PENDING_SPILL_LOCK:
        keep: list[tuple[str, threading.Timer]] = []
        for key, timer in _PENDING_SPILL_TIMERS:
            if key == lifecycle_key:
                try:
                    timer.cancel()
                except Exception:
                    pass
            else:
                keep.append((key, timer))
        _PENDING_SPILL_TIMERS[:] = keep


def clear_in_memory_spill_state(*, doc_url: str = "", lifecycle_key: str = "") -> None:
    """Drop instance-scoped spill maps. UD property is left for a later open of the same file."""
    if lifecycle_key:
        cancel_pending_spill_timers(lifecycle_key)
    if doc_url:
        LOADED_DOCUMENTS.discard(doc_url)
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_url]:
            SPILL_REGISTRY.pop(key, None)
        for skey in [k for k in SHEET_MODIFY_LISTENERS if k[0] == doc_url]:
            SHEET_MODIFY_LISTENERS.pop(skey, None)
    clear_python_addin_cache()


def execute_python_addin(
    ctx: Any,
    code: str,
    data: Any = None,
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
    *,
    doc: Any | None = None,
) -> Any:
    """Run *code* in the user venv and return a Calc-compatible scalar (or error string)."""
    with sync_host_dispatch():
        return _execute_python_addin_impl(ctx, code, data, true_strings, false_strings, doc=doc)


def _execute_python_addin_impl(
    ctx: Any,
    code: str,
    data: Any = None,
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
    *,
    doc: Any | None = None,
) -> Any:
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
    try:
        t_pack = time.perf_counter() if timings else 0.0
        args = split_python_addin_data_args(data)
        # Geometric predecessor is a Calc-only DAG token. Strip it before
        # calc_addin_args_from_split (1 vs N flips `data` to a list) and
        # before the matrix-index peel (a leftover 1×1 pred becomes index_arg).
        from plugin.calc.python.geometric_recalc import maybe_strip_geometric_eval_args

        args = maybe_strip_geometric_eval_args(code, args)
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
        max_cells = configured_python_max_data_cells(ctx)
        if py_data is not None:
            if is_multi:
                size_err = check_python_multi_data_size(py_data, max_cells=max_cells)
            else:
                size_err = check_python_data_size(py_data, max_cells=max_cells)
            if size_err:
                ret = f"Error: {size_err}"
                log.debug("PYTHON returning size error: %r", ret)
                _record_py_diagnostic(ctx, code, None, status="error", message=ret)
                return ret
            worker_data = pack_calc_multi_data_for_wire(py_data) if is_multi else pack_calc_data_for_wire(py_data)
        else:
            worker_data = None
        if timings:
            pack_ms = int(round((time.perf_counter() - t_pack) * 1000))
        # Synchronous: =PY() runs during Calc recalc; UI event pumping from
        # run_blocking_in_thread can re-enter the formula engine and yield #VALUE!.
        target_doc = doc
        if target_doc is None:
            from plugin.framework.thread_guard import on_main_thread

            if on_main_thread():
                from plugin.scripting.session_manager import _calc_document

                target_doc = _calc_document(ctx)

        tid = threading.get_ident()
        sk = session_key(ctx, code, doc=target_doc)
        unique_origin = bool(sk[4]) if len(sk) > 4 else False
        cache_key = (tid, sk, repr(worker_data))
        with _MATRIX_SCALAR_SESSIONS_LOCK:
            cached = _MATRIX_SCALAR_SESSIONS.get(cache_key) if unique_origin else None
        if isinstance(cached, WorkerResultSession) and cached.next_index < len(cached.flat):
            used_cache = True
            res = {"status": "ok", "result": cached.raw}
        else:
            session_id = workbook_session_id(ctx, doc=target_doc)
            init_kwargs = get_python_init_kwargs(ctx, doc=target_doc)

            from plugin.framework.thread_guard import in_sync_host_dispatch, on_main_thread

            log.debug(
                "PYTHON eval: target_doc=%r, session_id=%r, has_init=%s, on_main=%s, in_sync_host=%s",
                target_doc,
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
                session_id=session_id,
                # Formula recalc must not mutate the document via writeragent tools.
                python_tool_domain="",
                **init_kwargs,
            )

            if timings:
                ipc_ms = int(round((time.perf_counter() - t_ipc) * 1000))
        log.debug("PYTHON res from worker: %r", res)
        if res.get("status") == "ok":
            _record_py_diagnostic(ctx, code, res, status="ok")
            result = res.get("result")
            log.debug("PYTHON raw result: %r (type: %s)", result, type(result).__name__)
            images = find_image_payloads(result)
            if images:
                t_img = time.perf_counter() if timings else 0.0
                for img in images:
                    insert_image_result_on_sheet(ctx, img, code=code, doc=target_doc)
                if timings:
                    image_ms = int(round((time.perf_counter() - t_img) * 1000))
                return _("Image inserted") if len(images) == 1 else _("Images inserted")
            final_ret = finalize_python_return(ctx, code, result, index_arg=index_arg, worker_data=worker_data, doc=target_doc)
            log.debug("PYTHON returning scalar: %r (type: %s)", final_ret, type(final_ret).__name__)
            return final_ret


        err_msg = _format_python_addin_worker_error(str(res.get("message") or res.get("error") or ""))
        _record_py_diagnostic(ctx, code, res, status="error", message=err_msg)
        log.debug("PYTHON returning worker error: %r", err_msg)
        return err_msg
    except Exception as e:
        log.exception("PYTHON unexpected error during execution")
        err_msg = _format_error_for_display(e)
        _record_py_diagnostic(ctx, code, None, status="error", message=err_msg, traceback=str(e))
        log.debug("PYTHON returning exception wrapper: %r", err_msg)
        return err_msg
    finally:
        if timings:
            total_ms = int(round((time.perf_counter() - t_enter) * 1000))
            _PY_PASS_STATS.n = getattr(_PY_PASS_STATS, "n", 0) + 1
            _PY_PASS_STATS.sum_ms = getattr(_PY_PASS_STATS, "sum_ms", 0) + total_ms
            _PY_PASS_STATS.last_end = time.perf_counter()
            _emit_py_timing(
                code=code,
                total_ms=total_ms,
                pack_ms=pack_ms,
                ipc_ms=ipc_ms,
                image_ms=image_ms,
                cached=used_cache,
                pass_start=getattr(_PY_PASS_STATS, "pass_start", t_enter),
                n=_PY_PASS_STATS.n,
                pass_sum_ms=int(_PY_PASS_STATS.sum_ms),
                last_end=_PY_PASS_STATS.last_end,
            )


def _diagnostics_workbook_key(ctx: Any) -> str:
    """Stable workbook key for the diagnostics store (UNO-light best effort)."""
    try:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread():
            return "unknown"
        from plugin.scripting.document_scripts import get_calc_document_from_ctx
        from plugin.scripting.session_manager import calc_workbook_base_session_id

        doc = get_calc_document_from_ctx(ctx)
        if doc is not None:
            return calc_workbook_base_session_id(doc)
    except Exception:
        log.debug("diagnostics workbook key failed", exc_info=True)
    return "unknown"


def _record_py_diagnostic(
    ctx: Any,
    code: str,
    res: dict[str, Any] | None,
    *,
    status: str,
    message: str = "",
    traceback: str = "",
) -> None:
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
        record_python_eval(
            workbook_key=_diagnostics_workbook_key(ctx),
            code=code or "",
            status=status,
            message=msg,
            stdout=stdout,
            traceback=tb,
        )
    except Exception:
        # Never break formula evaluation for diagnostics UI.
        log.debug("record_python_eval failed", exc_info=True)
