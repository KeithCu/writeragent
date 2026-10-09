# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometric Recalc Order (Experimental) — list-diff, attach, UDProp map, eval-time strip.

Provides the runtime state, UNO listeners, and document reconciliation for:
- Enforcing row-major calculation order of =PY() cells on Calc sheets.
- Maintaining the attach map in document UserDefinedProperties and memory.
- Stripping attached predecessor arguments before Python execution.
- Subscribing to Settings changes to enable or disable geometric chaining.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Mapping, final

from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.calc.python.cell_discovery import canonicalize_py_formula_for_parse, discover_python_cells_on_sheet
from plugin.calc.python.formula_edit import parse_python_formula, py_formula_has_unquoted_code_ref
from plugin.calc.python.geometric_recalc_core import (
    CONFIG_KEY,
    FEATURE_TITLE,
    GEOMETRIC_REGISTRY_PROP,
    EvalIndexKey,
    GeometricCell,
    GeometricPatch,
    GeometricRecord,
    SheetRepairResult,
    compute_eval_index,
    compute_sheet_repair,
    discovery_cap_hit,
    geometric_cap_hit_user_message,
    local_a1,
    resolved_code_for_formula,
    should_strip_eval_args,
)
from plugin.framework.errors import is_disposed_exception
from plugin.framework.i18n import _

log = logging.getLogger(__name__)


@final
class GeometricRuntime:
    """Encapsulates in-memory attach records, eval strip snapshots, and state flags."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: dict[tuple[str, str, str], GeometricRecord] = {}
        self.strip_safe: frozenset[EvalIndexKey] = frozenset()
        self.repairing: bool = False
        self.last_flag: bool | None = None
        self.config_subscribed: bool = False
        self.cap_hit_notified: set[tuple[str, str]] = set()

    def reset(self) -> None:
        with self.lock:
            self.records.clear()
            self.strip_safe = frozenset()
            self.repairing = False
            self.last_flag = None
            self.config_subscribed = False
            self.cap_hit_notified.clear()


_RUNTIME = GeometricRuntime()


def is_geometric_repairing() -> bool:
    """Re-entrancy: ``setFormula`` during repair must not schedule another pass."""
    return _RUNTIME.repairing


def reset_geometric_runtime_for_tests() -> None:
    """Drop in-memory maps. Tests only."""
    _RUNTIME.reset()
    try:
        from plugin.calc.python.sheet_modify import reset_sheet_modify_runtime_for_tests

        reset_sheet_modify_runtime_for_tests()
    except Exception:
        log.debug("reset_sheet_modify_runtime_for_tests failed", exc_info=True)


def geometric_flag_enabled() -> bool:
    """Settings flag. Default false; missing schema is off, not an exception."""
    from plugin.framework.config import get_config_bool_safe

    return get_config_bool_safe(CONFIG_KEY)


def geometric_workbook_key(doc: Any) -> str:
    """``calc:`` + ``_workbook_session_key`` — never empty (unsaved uses a persisted id)."""
    from plugin.scripting.session_manager import _workbook_session_key

    key = (_workbook_session_key(doc) or "").strip()
    if not key:
        cached = getattr(doc, "_geometric_unsaved_key", None)
        if not cached:
            cached = f"unsaved:{uuid.uuid4()}"
            try:
                setattr(doc, "_geometric_unsaved_key", cached)
            except Exception:
                pass
        key = cached
    return f"calc:{key}"


def current_geometric_strip_safe() -> frozenset[EvalIndexKey]:
    """Worker-safe read: return the current snapshot name (do not copy-mutate)."""
    return _RUNTIME.strip_safe


def replace_geometric_strip_safe(workbook_key: str, safe: frozenset[EvalIndexKey]) -> None:
    """Rebind the strip-safe snapshot for one workbook. Other workbooks stay."""
    with _RUNTIME.lock:
        kept = frozenset(k for k in _RUNTIME.strip_safe if k.workbook_key != workbook_key)
        _RUNTIME.strip_safe = kept | frozenset(safe)


def records_for_sheet(workbook_key: str, sheet_name: str) -> dict[str, GeometricRecord]:
    with _RUNTIME.lock:
        return {
            addr: rec
            for (wk, sheet, addr), rec in _RUNTIME.records.items()
            if wk == workbook_key and sheet == sheet_name
        }


def replace_records_for_sheet(workbook_key: str, sheet_name: str, records: Mapping[str, GeometricRecord]) -> None:
    with _RUNTIME.lock:
        stale = [key for key in _RUNTIME.records if key[0] == workbook_key and key[1] == sheet_name]
        for key in stale:
            _RUNTIME.records.pop(key, None)
        for addr, rec in records.items():
            _RUNTIME.records[(workbook_key, sheet_name, local_a1(addr))] = rec


def load_geometric_registry_for_doc(doc: Any) -> str:
    """Load UDProp into the in-memory map. Returns the live workbook_key."""
    workbook_key = geometric_workbook_key(doc)
    try:
        from plugin.doc.udprops import get_document_property

        raw = get_document_property(doc, GEOMETRIC_REGISTRY_PROP, None)
        if not isinstance(raw, str) or not raw.strip():
            return workbook_key
        payload = json.loads(raw)
        sheets = payload.get("sheets") if isinstance(payload, dict) else None
        if not isinstance(sheets, dict):
            return workbook_key
        with _RUNTIME.lock:
            for sheet_name, addrs in sheets.items():
                if not isinstance(addrs, dict):
                    continue
                for addr, pred in addrs.items():
                    predecessor = pred
                    if isinstance(pred, dict):
                        predecessor = pred.get("predecessor", "")
                    if not predecessor:
                        continue
                    _RUNTIME.records[(workbook_key, str(sheet_name), local_a1(str(addr)))] = (
                        GeometricRecord(predecessor=local_a1(str(predecessor)))
                    )
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.exception("Failed to load geometric registry from document property")
    return workbook_key


def save_geometric_registry_for_doc(doc: Any, workbook_key: str) -> None:
    """Persist this workbook's attach records. Sibling of save_spill_registry_for_doc."""
    try:
        from plugin.doc.udprops import get_document_property, set_document_property

        sheets: dict[str, dict[str, str]] = {}
        with _RUNTIME.lock:
            for (wk, sheet, addr), rec in _RUNTIME.records.items():
                if wk != workbook_key:
                    continue
                sheets.setdefault(sheet, {})[addr] = rec.predecessor
        payload = json.dumps({"workbook_key": workbook_key, "sheets": sheets})
        if get_document_property(doc, GEOMETRIC_REGISTRY_PROP, None) == payload:
            return
        set_document_property(doc, GEOMETRIC_REGISTRY_PROP, payload)
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.exception("Failed to save geometric registry to document property")


def clear_in_memory_geometric_state(*, workbook_key: str = "") -> None:
    """Drop instance-scoped geometric maps. UDProp is left for a later open."""
    with _RUNTIME.lock:
        if workbook_key:
            for key in [k for k in _RUNTIME.records if k[0] == workbook_key]:
                _RUNTIME.records.pop(key, None)
            _RUNTIME.strip_safe = frozenset(k for k in _RUNTIME.strip_safe if k.workbook_key != workbook_key)
        else:
            _RUNTIME.records.clear()
            _RUNTIME.strip_safe = frozenset()


def notify_geometric_cap_hit(ctx: Any, sheet_name: str, *, workbook_key: str = "") -> bool:
    """Log the skip and show one message box per sheet. UI thread only.

    Returns True when a box was shown. A process-wide set keyed by
    ``(workbook_key, sheet_name)`` persists across reconcile / 0.1s debounce
    / save / open so one repair pass cannot storm the user.
    """
    persist_key = (workbook_key, sheet_name)
    with _RUNTIME.lock:
        if persist_key in _RUNTIME.cap_hit_notified:
            return False

    message = geometric_cap_hit_user_message(sheet_name)
    log.error("Geometric Recalc Order: %s", message)

    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        # Off-main still logs. Do not persist — a later UI-thread call
        # must still be able to show the first box.
        return False

    from plugin.chatbot.dialogs import msgbox

    msgbox(ctx, _(FEATURE_TITLE), message, box_type=3)
    with _RUNTIME.lock:
        _RUNTIME.cap_hit_notified.add(persist_key)
    return True


def maybe_strip_geometric_eval_args(resolved_code: str, args: list[Any], *, doc: Any = None) -> list[Any]:
    """Drop the last split arg when the triple is strip-safe.

    Must run after ``split_python_addin_data_args`` and before
    ``calc_addin_args_from_split`` / the matrix-index heuristic.
    """
    if not args:
        return args
    from plugin.framework.thread_guard import on_main_thread

    workbook_key: str | None = None
    unambiguous = False
    if doc is not None and on_main_thread():
        workbook_key = geometric_workbook_key(doc)
        unambiguous = True
    if not should_strip_eval_args(
        workbook_key=workbook_key,
        resolved_code=resolved_code,
        n_args=len(args),
        strip_safe=current_geometric_strip_safe(),
        unambiguous=unambiguous,
    ):
        return args
    return args[:-1]


def _read_code_ref_text(doc: Any, default_sheet: Any, ref: str) -> str:
    """Cell contents of an unquoted ``=PY($A$1)`` code ref (resolved source)."""
    sheet_name, rest = split_sheet_prefix(ref)
    local = rest.replace("$", "").strip()
    sheet = default_sheet
    if sheet_name and doc is not None:
        try:
            sheet = doc.getSheets().getByName(sheet_name)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.warning("geometric_recalc: getByName(%s) failed", sheet_name, exc_info=True)
    if sheet is None or not local:
        return ""
    try:
        col, row = parse_address(local)
        cell = sheet.getCellByPosition(col, row)
        return str(cell.getString() or "")
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.warning("geometric_recalc: read code cell failed at %s", ref, exc_info=True)
        return ""


def _resolved_code_for_discovered(doc: Any, sheet: Any, formula: str) -> str:
    canon = canonicalize_py_formula_for_parse(formula)
    if py_formula_has_unquoted_code_ref(canon):
        parts = parse_python_formula(canon)
        if parts is None:
            return ""
        return _read_code_ref_text(doc, sheet, parts.code)
    return resolved_code_for_formula(canon)


def geometric_cells_on_sheet(doc: Any, sheet: Any) -> tuple[list[GeometricCell], str, bool]:
    """Discover one sheet as Phase 1 cells. Address is local A1 (per-sheet map).

    Third value is discovery ``truncated`` — cap-hit skip uses this, not
    ``len >= 100``.
    """
    discovery = discover_python_cells_on_sheet(sheet)
    try:
        name = str(sheet.getName() or "") or "Sheet"
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        name = "Sheet"
    cells = [
        GeometricCell(
            address=local_a1(info.address),
            formula=info.formula,
            resolved_code=_resolved_code_for_discovered(doc, sheet, info.formula),
        )
        for info in discovery.cells
    ]
    return cells, name, discovery.truncated


def _iter_sheets(doc: Any) -> list[Any]:
    out: list[Any] = []
    try:
        sheets = doc.getSheets()
        for i in range(int(sheets.getCount())):
            out.append(sheets.getByIndex(i))
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.warning("geometric_recalc: sheet walk failed", exc_info=True)
    return out


def _sheet_of_cell(cell: Any, doc: Any) -> Any | None:
    if cell is not None and hasattr(cell, "getSpreadsheet"):
        try:
            sheet = cell.getSpreadsheet()
            if sheet is not None:
                return sheet
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("geometric_recalc: cell.getSpreadsheet failed", exc_info=True)
    try:
        ctrl = doc.getCurrentController()
        if ctrl is not None:
            return ctrl.getActiveSheet()
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.debug("geometric_recalc: doc.getCurrentController failed", exc_info=True)
    return None


def _cell_on_sheet(sheet: Any, address: str) -> Any | None:
    local = local_a1(address)
    if not local:
        return None
    try:
        col, row = parse_address(local)
        return sheet.getCellByPosition(col, row)
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.warning("geometric_recalc: getCellByPosition failed at %s", address, exc_info=True)
        return None


def _apply_patches_to_sheet(sheet: Any, patches: tuple[GeometricPatch, ...]) -> set[str]:
    """``setFormula`` for each patch. Caller holds ``_undo_lock`` + re-entrancy."""
    applied: set[str] = set()
    for patch in patches:
        cell = _cell_on_sheet(sheet, patch.address)
        if cell is None:
            continue
        try:
            current = str(cell.getFormula() or "")
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.warning("geometric_recalc: getFormula failed at %s", patch.address, exc_info=True)
            continue
        if current != patch.old_formula:
            # Stale: user or Calc changed the cell since we computed the patch.
            continue
        try:
            cell.setFormula(patch.new_formula)
            applied.add(local_a1(patch.address))
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.warning("geometric_recalc: setFormula failed at %s", patch.address, exc_info=True)
    return applied


def _rebuild_strip_safe_from_doc(
    ctx: Any,
    doc: Any,
    workbook_key: str,
    *,
    known_sheets: Mapping[str, list[GeometricCell]] | None = None,
) -> None:
    """Workbook-wide unanimous-ours. Cap-hit sheets are omitted (cannot prove)."""
    all_cells: list[GeometricCell] = []
    all_formulas: dict[str, str] = {}
    all_records: dict[str, GeometricRecord] = {}
    known = known_sheets or {}
    for sheet in _iter_sheets(doc):
        try:
            sheet_name = str(sheet.getName() or "") or "Sheet"
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            sheet_name = "Sheet"
        if sheet_name in known:
            cells = known[sheet_name]
        else:
            cells, sheet_name, truncated = geometric_cells_on_sheet(doc, sheet)
            if discovery_cap_hit(len(cells), truncated=truncated):
                notify_geometric_cap_hit(ctx, sheet_name, workbook_key=workbook_key)
                continue
        for cell in cells:
            scoped = f"{sheet_name}:{local_a1(cell.address)}"
            all_cells.append(GeometricCell(scoped, cell.formula, cell.resolved_code))
            all_formulas[scoped] = cell.formula
        for addr, rec in records_for_sheet(workbook_key, sheet_name).items():
            all_records[f"{sheet_name}:{addr}"] = rec
    replace_geometric_strip_safe(workbook_key, compute_eval_index(all_cells, all_formulas, all_records, workbook_key))


def _repair_one_sheet(
    ctx: Any,
    doc: Any,
    sheet: Any,
    workbook_key: str,
    *,
    apply_patches: bool = True,
) -> tuple[SheetRepairResult, list[GeometricCell]]:
    cells, name, truncated = geometric_cells_on_sheet(doc, sheet)
    incoming = records_for_sheet(workbook_key, name)
    result = compute_sheet_repair(
        cells,
        incoming,
        workbook_key=workbook_key,
        sheet_name=name,
        truncated=truncated,
    )
    if result.skipped:
        notify_geometric_cap_hit(ctx, name, workbook_key=workbook_key)
        return result, cells

    final_records = dict(result.records)
    actual_cells = cells
    if apply_patches and result.patches:
        applied_addrs = _apply_patches_to_sheet(sheet, result.patches)
        # An unapplied patch reverts its key to the pre-patch state: keep
        # the incoming record when there was one, otherwise leave the key
        # absent. _apply_patches_to_sheet can fail (a protected sheet throws
        # in setFormula, or the cell formula changed concurrently). Saving
        # result.records directly records those failed appends and removes,
        # so a stale record marks a group strip-safe and strips real data.
        unapplied_patches = [p for p in result.patches if local_a1(p.address) not in applied_addrs]
        if unapplied_patches:
            for patch in unapplied_patches:
                key = local_a1(patch.address)
                if key in incoming:
                    final_records[key] = incoming[key]
                else:
                    final_records.pop(key, None)

        actual_formulas = {cell.address: cell.formula for cell in cells}
        for patch in result.patches:
            if local_a1(patch.address) in applied_addrs:
                actual_formulas[patch.address] = patch.new_formula
        strip_safe = compute_eval_index(cells, actual_formulas, final_records, workbook_key)
        result = SheetRepairResult(
            skipped=result.skipped,
            skip_reason=result.skip_reason,
            patches=result.patches,
            records=final_records,
            strip_safe=strip_safe,
            user_message=result.user_message,
            sheet_name=result.sheet_name,
        )
        actual_cells = [
            GeometricCell(
                address=cell.address,
                formula=actual_formulas.get(cell.address, cell.formula),
                resolved_code=cell.resolved_code,
            )
            for cell in cells
        ]
    elif not apply_patches:
        final_records = dict(incoming)

    replace_records_for_sheet(workbook_key, name, final_records)
    return result, actual_cells


def reconcile(
    ctx: Any,
    doc: Any,
    sheets: Any = None,
    *,
    already_loaded: bool = False,
) -> None:
    """Flag-on / doc-open / save-path attach: repair sheets in one locked undo unit."""
    if doc is None or _RUNTIME.repairing:
        return
    target_sheets = [sheets] if sheets is not None and not isinstance(sheets, (list, tuple)) else sheets
    if target_sheets is not None and not target_sheets:
        return
    workbook_key = geometric_workbook_key(doc) if already_loaded else load_geometric_registry_for_doc(doc)
    _RUNTIME.repairing = True
    repaired_sheets: dict[str, list[GeometricCell]] = {}
    try:
        from plugin.calc.python.function import _undo_lock
        from plugin.calc.python.sheet_modify import ensure_sheet_modify_listener

        with _undo_lock(doc):
            sheet_list = _iter_sheets(doc) if target_sheets is None else list(target_sheets)
            for sheet in sheet_list:
                ensure_sheet_modify_listener(ctx, doc, sheet)
                res, live_cells = _repair_one_sheet(ctx, doc, sheet, workbook_key, apply_patches=True)
                if not res.skipped:
                    repaired_sheets[res.sheet_name] = live_cells
            save_geometric_registry_for_doc(doc, workbook_key)
        _rebuild_strip_safe_from_doc(ctx, doc, workbook_key, known_sheets=repaired_sheets)
    finally:
        _RUNTIME.repairing = False



def reconcile_geometric_document(ctx: Any, doc: Any, *, already_loaded: bool = False) -> None:
    """Flag-on / document-open: attach every sheet, one locked undo unit."""
    reconcile(ctx, doc, sheets=None, already_loaded=already_loaded)


def reconcile_geometric_sheet(ctx: Any, doc: Any, sheet: Any, *, already_loaded: bool = False) -> None:
    """Save-path attach for one sheet. Neighbors on this sheet may retarget."""
    reconcile(ctx, doc, sheets=sheet, already_loaded=already_loaded)


def after_py_cell_save(doc: Any, cell: Any, ctx: Any = None) -> None:
    """Primary attach path: Monaco / native Save is already outside recalc."""
    if not geometric_flag_enabled() or doc is None:
        return
    if ctx is None:
        try:
            from plugin.framework.thread_guard import on_main_thread
            from plugin.framework.uno_context import get_ctx

            if on_main_thread():
                ctx = get_ctx()
        except Exception:
            ctx = None
    sheet = _sheet_of_cell(cell, doc)
    if sheet is None:
        return
    reconcile_geometric_sheet(ctx, doc, sheet)


def maybe_geometric_on_document_open(ctx: Any, doc: Any) -> None:
    """Load UDProp; reconcile when the flag is on. Always record Isolated session."""
    if doc is None:
        return
    try:
        if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            return
    except Exception:
        return
    load_geometric_registry_for_doc(doc)
    if geometric_flag_enabled():
        reconcile_geometric_document(ctx, doc, already_loaded=True)
    else:
        workbook_key = geometric_workbook_key(doc)
        _rebuild_strip_safe_from_doc(ctx, doc, workbook_key)


def ensure_geometric_strip_index_for_eval(doc: Any, ctx: Any = None) -> None:
    """UI-thread only: rebuild ``_STRIP_SAFE`` from UDProp before packing ``data``."""
    if doc is None:
        return
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return
    workbook_key = geometric_workbook_key(doc)
    if geometric_flag_enabled() and any(key.workbook_key == workbook_key for key in current_geometric_strip_safe()):
        return
    load_geometric_registry_for_doc(doc)
    _rebuild_strip_safe_from_doc(ctx, doc, workbook_key)


def _on_geometric_config_changed(**kwargs: Any) -> None:
    """Flag-on walks all sheets. Flag-off leaves refs and the map."""
    now = geometric_flag_enabled()
    with _RUNTIME.lock:
        was = _RUNTIME.last_flag
        _RUNTIME.last_flag = now
    if not now or was is not False:
        return
    ctx = kwargs.get("ctx")
    if ctx is None:
        return
    from plugin.scripting.session_manager import _calc_document

    doc = _calc_document(ctx)
    if doc is None:
        return
    reconcile_geometric_document(ctx, doc)


def install_geometric_recalc() -> None:
    """Subscribe to Settings so flag-on can attach. Idempotent."""
    with _RUNTIME.lock:
        if _RUNTIME.config_subscribed:
            return
        _RUNTIME.last_flag = geometric_flag_enabled()
        _RUNTIME.config_subscribed = True
    from plugin.framework.event_bus import global_event_bus

    global_event_bus.subscribe("config:changed", _on_geometric_config_changed)
