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
from typing import Any, ClassVar, Iterator, cast

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
# Keys include repr(worker_data) so the same formula with different data args
# does not share a session. repr of a large grid is expensive and a weak identity;
# a later change could use packed-payload digest + cell count. Do not key on id():
# recals would collide. Two formulas with the same code but different data must
# stay on separate sessions (see tests/calc/python/test_function.py).
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

    if isinstance(val, int):
        return float(val)
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


def scalar_for_list_result(ctx: Any, code: str, result: Any, *, worker_data: Any = None, doc: Any | None = None) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: list[Any] = [to_calc_compatible(v) for v in flatten_result_values(result)]
    if not flat:
        return ""
    tid = threading.get_ident()
    sk = session_key(ctx, code, doc=doc)
    if not sk[4]:
        # Ambiguous formula identity: do not share next_index across duplicate =PY() cells.
        return flat[0] if flat else ""
    key = (tid, sk, repr(worker_data))
    now = time.monotonic()
    with _MATRIX_SCALAR_SESSIONS_LOCK:
        state = _MATRIX_SCALAR_SESSIONS.get(key)
        if (
            not isinstance(state, WorkerResultSession)
            or state.flat != tuple(flat)
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


# The spill registry tracks coordinates that were spilled by each formula cell.
# Key: (doc identity, sheet_name, formula_row, formula_col)
# Identity is the file URL when the workbook has one. Every unsaved book
# reports getURL()==""; those use workbook_lifecycle._lifecycle_key
# (RuntimeUID), never "". LOADED_DOCUMENTS uses the same identity.
# Value: list of (spilled_row, spilled_col) coordinates
SPILL_REGISTRY: dict[tuple[str, str, int, int], list[tuple[int, int]]] = {}
LOADED_DOCUMENTS: set[str] = set()
_SPILL_REGISTRY_LOCK = threading.Lock()
_PENDING_SPILL_LOCK = threading.Lock()
_PENDING_SPILL_TIMERS: list[tuple[str, threading.Timer]] = []

import unohelper
from com.sun.star.util import XModifyListener

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

    ctx: Any
    doc_url: str
    sheet_name: str

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

            # Walk to the spreadsheet that owns the sheet. Orphan cleanup
            # locks undo and saves WriterAgentSpillRegistry on whichever
            # workbook is focused. The cells it clears belong to the sheet
            # that fired, which may be a background file. _get_calc_doc is
            # desktop.getCurrentComponent(). A parent-less MagicMock still
            # falls back to the active model so direct tests keep their stub.
            from plugin.calc.python.sheet_modify import _owning_calc_doc

            doc = _owning_calc_doc(sheet)
            if doc is None:
                doc = _get_calc_doc(self.ctx)
            # is_py_formula_text is the =PY( / =PYTHON( check, including a
            # qualified add-in name. "PY" in formula is true for =PYMT and
            # for any text that merely contains those letters, so replacing
            # =PY() with an unrelated formula left the spilled block.
            from plugin.calc.python.cell_discovery import is_py_formula_text

            with _undo_lock(doc):
                to_remove = []
                for key, value in list(SPILL_REGISTRY.items()):
                    doc_url, sheet_name, frow, fcol = key
                    # Callers pass the file URL or the lifecycle id. An empty
                    # string is not an identity: it matches every unsaved
                    # workbook, so a modify on one untitled book cleared the
                    # other's spill cells when the sheet names matched.
                    if self.doc_url and doc_url == self.doc_url and sheet_name == self.sheet_name:
                        try:
                            cell = sheet.getCellByPosition(fcol, frow)
                            formula = cell.getFormula()
                            if not formula or not is_py_formula_text(str(formula)):
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


def _spill_registry_doc_key(doc: Any) -> str:
    """Key the spill registry and LOADED_DOCUMENTS by RuntimeUID, not URL.

    Migrate existing URL keys when the uid is read.
    """
    if doc is None:
        return ""
    uid = ""
    try:
        if hasattr(doc, "getPropertyValue"):
            val = doc.getPropertyValue("RuntimeUID")
            if val:
                uid = str(val)
    except Exception:
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
            parts = key.split(":")
            if len(parts) == 2:
                sheet_name, coords = parts
                row_col = coords.split(",")
                if len(row_col) == 2:
                    frow, fcol = int(row_col[0]), int(row_col[1])
                    spill_coords = [(int(r), int(c)) for r, c in value]
                    SPILL_REGISTRY[(doc_key, sheet_name, frow, fcol)] = spill_coords
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
        doc_spills = {}
        for key, value in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_key:
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
        new_val = json.dumps(doc_spills)
        set_document_property(doc, "WriterAgentSpillRegistry", new_val)
        actual = get_document_property(doc, "WriterAgentSpillRegistry", "")
        if actual != new_val:
            log.warning("Spill registry write back mismatch: expected %r, got %r", new_val, actual)
    except Exception:
        log.exception("Failed to save spill registry to document property")


def _coerce_spill_value(val: Any, null_dt: datetime.date) -> tuple[Any, dict[str, Any]]:
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


def perform_deferred_spill(ctx: Any, doc_url: str, sheet_name: str, formula_row: int, formula_col: int, grid: list[list[Any]], doc: Any | None = None, *, code: str = "", lifecycle_key: str = "") -> None:
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

            reg_key = (live_key, sheet_name, formula_row, formula_col)

            # 1. Clear previously spilled cells
            # We overwrite previously spilled cells, not #SPILL! on user edits, because the registry stores coordinates only, not the values we wrote.
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
            # We write with setDataArray, not setFormulaArray, so result strings starting with '=' stay text.
            if num_cols > 1:
                first_row_range = sheet.getCellRangeByPosition(formula_col + 1, formula_row, formula_col + num_cols - 1, formula_row)
                first_row_range.setDataArray((tuple(coerced_grid[0][1:]),))

            if num_rows > 1:
                remaining_range = sheet.getCellRangeByPosition(formula_col, formula_row + 1, formula_col + num_cols - 1, formula_row + num_rows - 1)
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


def _prepare_auto_spill(ctx: Any, code: str, grid_to_spill: list[list[Any]], target_doc: Any) -> str | tuple[str, str, int, int] | None:
    """Locate the unique formula origin and check spill collisions (UNO / UI thread).

    Returns ``"#SPILL!"`` on collision, ``(doc identity, sheet_name, row, col)`` when the
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
    previous_spills = SPILL_REGISTRY.get(reg_key, [])
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
    return (doc_key, sheet_name, formula_row, formula_col)


def _queue_deferred_spill_write(ctx: Any, code: str, grid_to_spill: list[list[Any]], target_doc: Any, prepared: tuple[str, str, int, int]) -> None:
    """Post neighbor writes to the UI thread after the usual 0.1s settle."""
    from plugin.framework.queue_executor import post_to_main_thread
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

    doc_url, sheet_name, formula_row, formula_col = prepared
    spill_lifecycle = _lifecycle_key(target_doc)

    def _deferred_spill_on_main() -> None:
        post_to_main_thread(lambda: perform_deferred_spill(ctx, doc_url, sheet_name, formula_row, formula_col, grid_to_spill, doc=target_doc, code=code, lifecycle_key=spill_lifecycle))

    t = _new_spill_timer(0.1, _deferred_spill_on_main)
    _register_spill_timer(spill_lifecycle, t)
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
        if not isinstance(prepared, tuple):
            return
        from plugin.calc.python.workbook_lifecycle import _lifecycle_key

        doc_url, sheet_name, formula_row, formula_col = prepared
        perform_deferred_spill(ctx, doc_url, sheet_name, formula_row, formula_col, grid_to_spill, doc=target_doc, code=code, lifecycle_key=_lifecycle_key(target_doc))

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
        _register_spill_timer(lkey, t)
    t.start()


def finalize_python_return(ctx: Any, code: str, result: Any, *, index_arg: Any = None, worker_data: Any = None, doc: Any | None = None) -> float | str | bool | tuple[Any, ...]:
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
                        # We check the selection first, not the locator, because the locator scans every formula cell.
                        is_matrix = _selection_is_multi_cell(target_doc)
                        if not is_matrix:
                            located = locate_formula_cell_in_doc(ctx, target_doc, code)
                            if located is not None:
                                sheet, cell, _coord = located
                                is_matrix = _cell_is_matrix(sheet, cell)
                except Exception:
                    pass
        else:
            is_matrix = True

        if not is_matrix:
            grid_to_spill = _result_as_spill_grid(result)
            try:
                if not on_main_thread():
                    if doc is not None:
                        _queue_off_main_auto_spill(ctx, code, grid_to_spill, doc)
                    # We return the corner as to_calc_compatible (ISO text for dates), not a date serial, because an add-in return cannot carry a number format.
                    return to_calc_compatible(grid_to_spill[0][0])

                target_doc = doc
                if target_doc is not None:
                    prepared = _prepare_auto_spill(ctx, code, grid_to_spill, target_doc)
                    if prepared == "#SPILL!":
                        return "#SPILL!"
                    if isinstance(prepared, tuple):
                        _queue_deferred_spill_write(ctx, code, grid_to_spill, target_doc, prepared)
                        # We return the corner as to_calc_compatible (ISO text for dates), not a date serial, because an add-in return cannot carry a number format.
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

        from plugin.framework.thread_guard import on_main_thread

        # Cached spill model is for the deferred UI write only. Passing it
        # into session_key off-main calls getURL / locate (see session_key).
        scalar_doc = doc if on_main_thread() else None
        return scalar_for_list_result(ctx, code, result, worker_data=worker_data, doc=scalar_doc)

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
        return _("Error: Python venv not found. Open Settings → Python, set the venv path, then Test.")
    if "timed out" in lower or "timeout" in lower:
        return _("Error: Python timed out. Open Settings → Python to raise the timeout, or Test the venv.")
    if text.startswith("Error:") or text.startswith("#"):
        return text
    return _("Error: {0}").format(text)


def _code_uses_indexed_multi_data(code: str) -> bool:
    """True when inline code references ``data[n]`` or ``ranges[n]`` (all PY args are data ranges)."""
    src = code or ""
    return "data[" in src or "ranges[" in src


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


def _register_spill_timer(lifecycle_key: str, timer: threading.Timer) -> None:
    with _PENDING_SPILL_LOCK:
        _prune_finished_spill_timers_locked()
        _PENDING_SPILL_TIMERS[:] = [(key, existing) for key, existing in _PENDING_SPILL_TIMERS if existing is not timer]
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
        cache_key = (tid, sk, repr(worker_data))
        now = time.monotonic()
        with _MATRIX_SCALAR_SESSIONS_LOCK:
            cached = _MATRIX_SCALAR_SESSIONS.get(cache_key) if unique_origin else None
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
            _record_py_diagnostic(ctx, code, res, status="ok", doc=target_doc)
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
        _record_py_diagnostic(ctx, code, res, status="error", message=err_msg, doc=target_doc)
        log.debug("PYTHON returning worker error: %r", err_msg)
        return err_msg
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
