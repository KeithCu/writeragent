# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometric Recalc Order — list-diff, attach, UDProp map, eval-time strip.

Phase 1 helpers stay pure (no UNO): given a row-major list of PY cells plus the
in-memory attach map, compute formula patches and unanimous-ours strip-safe
triples. Phase 2/4 add the Settings flag, UDProp / in-memory map, attach on
save and flag-on, Isolated session record, and worker-ingress strip.

See ``docs/calc/geometric-recalc-order.md`` §8 Phase 2 + Phase 4 and §9.5.
Eval identity is unanimous-ours on ``(workbook_key, resolved_code, n_args)``
only — a value fingerprint of ``args[:-1]`` was rejected (data edits miss
the key without Phase 3). The strip-safe index is a frozenset snapshot
rebound on the UI thread (§3.5); workers only read.
Cap-hit skip uses ``len(cells) >= _MAX_PYTHON_CELLS_FOUND`` (no truncated flag).
A skipped sheet must also show a user-visible error — callers use
:func:`notify_geometric_cap_hit` on the UI thread (one box per sheet).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from plugin.calc.address_utils import split_sheet_prefix
from plugin.calc.calc_addin_data import split_python_addin_data_args
from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
from plugin.calc.python.formula_edit import (
    escape_code_for_excel_formula,
    format_data_binding_display,
    format_py_data_range,
    parse_data_binding_text,
    parse_python_formula,
    py_code_arg_is_cell_ref,
    py_formula_has_unquoted_code_ref,
)
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

GEOMETRIC_DISCOVERY_CAP = _MAX_PYTHON_CELLS_FOUND
GEOMETRIC_REGISTRY_PROP = "WriterAgentGeometricRegistry"
CONFIG_KEY = "scripting.python_geometric_recalc_order"

GeometricAction = Literal["append", "replace", "remove"]


@dataclass(frozen=True)
class GeometricCell:
    """One discovered PY cell, already in sheet row-major order."""

    address: str
    formula: str
    resolved_code: str


@dataclass(frozen=True)
class GeometricRecord:
    """Attach map entry: we wrote this predecessor onto the cell."""

    predecessor: str


@dataclass(frozen=True)
class GeometricPatch:
    """One formula rewrite for a successor (or remove-field on a new first)."""

    address: str
    old_formula: str
    new_formula: str
    action: GeometricAction
    predecessor: str | None


@dataclass(frozen=True)
class EvalIndexKey:
    """Eval-time strip key. ``code`` is resolved source, not a ``$A$1`` token.

    Value fingerprint of ``args[:-1]`` was dropped: without a Phase 3
    modify-listener the index is not rebuilt on data edits, so a live-value
    hash missed after the user changed a range and strip skipped (arity
    flip). Unanimous-ours on ``(workbook_key, resolved_code, n_args)`` never
    produces wrong numbers — mixed same-triple only widens no-strip.
    """

    workbook_key: str
    resolved_code: str
    n_args: int


@dataclass(frozen=True)
class SheetRepairResult:
    """Patch list + post-repair map + unanimous-ours bools for one sheet."""

    skipped: bool
    skip_reason: str | None
    patches: tuple[GeometricPatch, ...]
    records: dict[str, GeometricRecord]
    strip_safe: frozenset[EvalIndexKey]
    user_message: str | None = None
    sheet_name: str = ""


def local_a1(address: str) -> str:
    """``Sheet1.A1`` / ``$A$1`` / ``A1`` → ``A1`` (no ``$``)."""
    _sheet, rest = split_sheet_prefix(address or "")
    return rest.replace("$", "").strip().upper()


def cell_map_key(address: str) -> str:
    """Per-sheet map key. Phase 1 lists are one sheet; callers scope the map."""
    return local_a1(address)


def same_cell_ref(left: str, right: str) -> bool:
    """True when two formula tokens name the same cell (``$`` / sheet ignored)."""
    return bool(left) and bool(right) and local_a1(left) == local_a1(right)


def is_single_cell_arg(arg: str) -> bool:
    """True for a single A1 token, not a range or Python snippet."""
    return py_code_arg_is_cell_ref(arg)


def formula_data_args(formula: str) -> list[str] | None:
    """Trailing ``=PY`` data args, or None if the formula does not parse."""
    parts = parse_python_formula(formula)
    if parts is None:
        return None
    return parse_data_binding_text(format_data_binding_display(parts.data_suffix))


def repair_n_args(formula: str) -> int:
    """Arity that must match ``len(split_python_addin_data_args(data))``.

    Counts parsed data args, not semicolons in the code string. After attach,
    ``=PY("np.mean(data)"; B1:B10; A1)`` is ``n_args=2``.
    """
    args = formula_data_args(formula)
    return 0 if args is None else len(args)


def eval_n_args_from_data(data: Any) -> int:
    """Eval-time arity: ``len(split_python_addin_data_args(data))``."""
    return len(split_python_addin_data_args(data))


def discovery_cap_hit(n_found: int) -> bool:
    """Phase 1: treat ``len >= 100`` as cap-hit (over-skips an exact 100)."""
    return n_found >= GEOMETRIC_DISCOVERY_CAP


def resolved_code_for_formula(formula: str, *, code_cell_text: str | None = None) -> str:
    """Source ``execute_python_addin`` receives.

    For ``=PY($A$1; …)`` pass the contents of ``$A$1``. Do not key the token.
    """
    if py_formula_has_unquoted_code_ref(formula):
        if code_cell_text is None:
            raise ValueError("code-in-cell formula needs the referenced cell's text")
        return code_cell_text
    parts = parse_python_formula(formula)
    return parts.code if parts is not None else ""


def _geometric_data_suffix(old_args: list[str], new_args: list[str]) -> str:
    """Join existing user args verbatim; format only a new/replaced last pred.

    ``build_data_suffix`` / ``_format_py_data_range_body`` strip ``$`` from
    every token. That turned ``=PY("y"; $C$5)`` into ``=PY("y"; C5; A1)`` on
    attach — a relative copy of an absolute user data ref. Keep the parsed
    original spelling; only the appended/replaced predecessor is formatted.
    """
    if not new_args:
        return ")"
    emitted: list[str] = []
    for i, tok in enumerate(new_args):
        if i < len(old_args) and (tok == old_args[i] or same_cell_ref(tok, old_args[i])):
            emitted.append(old_args[i])
            continue
        if i == len(new_args) - 1:
            emitted.append(format_py_data_range(tok))
        else:
            emitted.append(tok)
    return f";{';'.join(emitted)})"


def rebuild_formula_with_data_args(formula: str, data_args: list[str]) -> str | None:
    """Spliced formula. Existing user args stay verbatim (including ``$``).

    Quoted code is quote-escaped only (``"`` → ``""``), same as
    ``escape_code_for_excel_formula`` — do not run the Calc sanitizer
    (``float(`` → ``(…)+0.0``) on attach. Code-in-cell keeps the unquoted
    ref (``$A$1``), not a quoted token. ``rebuild_python_formula_with_data``
    would sanitize the code, reformat every data arg (strips ``$``), and
    quote ``$A$1``. Uses the parsed prefix + first-arg spelling and
    :func:`_geometric_data_suffix`. Live Classic ``getFormula()`` stores
    ``=py(`` (lowercase), not ``=PY(`` — keep ``parts.prefix``.
    """
    parts = parse_python_formula(formula)
    if parts is None:
        return None
    old_args = formula_data_args(formula) or []
    suffix = _geometric_data_suffix(old_args, data_args)
    if py_formula_has_unquoted_code_ref(formula):
        # Keep the parsed token so =PY($A$1; …) stays $A$1, not A1 and not "$A$1".
        return f"{parts.prefix}{parts.code}{suffix}"
    # Quote-escape only. escape_code_for_formula runs sanitize_inline_py_code
    # (float( → (…)+0.0). Hand-written =PY("float(1)") must survive attach.
    return f'{parts.prefix}"{escape_code_for_excel_formula(parts.code)}"{suffix}'


def geometric_cap_hit_user_message(sheet_name: str) -> str:
    """User-facing text when a sheet is left unchained. Users do not read logfiles."""
    return _(
        "Geometric Recalc Order skipped sheet '%s': found %s or more Python "
        "cells (discovery cap). The sheet was left unchained so a partial "
        "list is not treated as complete."
    ) % (sheet_name, GEOMETRIC_DISCOVERY_CAP)


def notify_geometric_cap_hit(
    ctx: Any,
    sheet_name: str,
    *,
    already_notified: set[str] | None = None,
) -> bool:
    """Log the skip and show one message box per sheet. UI thread only.

    Returns True when a box was shown. A second call for the same sheet name
    in *already_notified* is a no-op so repair cannot storm the user.
    """
    if already_notified is not None and sheet_name in already_notified:
        return False
    if already_notified is not None:
        already_notified.add(sheet_name)

    message = geometric_cap_hit_user_message(sheet_name)
    log.error("Geometric Recalc Order: %s", message)

    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return False

    from plugin.chatbot.dialogs import msgbox

    msgbox(ctx, _("Geometric Recalc Order"), message, box_type=3)
    return True


def _plan_action(
    *,
    desired: str | None,
    data_args: list[str],
    record: GeometricRecord | None,
) -> tuple[Literal["append", "replace", "remove", "noop"], list[str]]:
    last = data_args[-1] if data_args else None
    last_is_cell = last is not None and is_single_cell_arg(last)

    if desired is None:
        if record is not None and last_is_cell:
            return "remove", data_args[:-1]
        return "noop", data_args

    if last_is_cell and last is not None and same_cell_ref(last, desired):
        # Already correct (ours) or user already passed the previous PY cell
        # as real data (not ours). Either way the formula is satisfied.
        return "noop", data_args

    if (
        record is not None
        and last_is_cell
        and last is not None
        and same_cell_ref(last, record.predecessor)
    ):
        return "replace", data_args[:-1] + [desired]

    return "append", data_args + [desired]


def compute_eval_index(
    cells: list[GeometricCell],
    formulas: Mapping[str, str],
    records: Mapping[str, GeometricRecord],
    workbook_key: str,
) -> frozenset[EvalIndexKey]:
    """Strip-safe iff every discovered cell with that triple is in the map."""
    groups: dict[EvalIndexKey, list[str]] = {}
    for cell in cells:
        formula = formulas.get(cell.address, cell.formula)
        key = EvalIndexKey(workbook_key, cell.resolved_code, repair_n_args(formula))
        groups.setdefault(key, []).append(cell_map_key(cell.address))

    safe: set[EvalIndexKey] = set()
    for key, addrs in groups.items():
        if addrs and all(addr in records for addr in addrs):
            safe.add(key)
    return frozenset(safe)


def should_strip_eval_args(
    *,
    workbook_key: str | None,
    resolved_code: str,
    n_args: int,
    strip_safe: Mapping[EvalIndexKey, bool] | frozenset[EvalIndexKey],
    unambiguous: bool,
) -> bool:
    """Eval gate: strip only when the session is unambiguous and the triple is ours."""
    if not unambiguous or not workbook_key:
        return False
    key = EvalIndexKey(workbook_key, resolved_code, n_args)
    if isinstance(strip_safe, frozenset):
        return key in strip_safe
    return bool(strip_safe.get(key, False))


def compute_sheet_repair(
    cells: list[GeometricCell],
    records: Mapping[str, GeometricRecord] | None = None,
    *,
    workbook_key: str,
    sheet_name: str = "",
) -> SheetRepairResult:
    """List-diff + splice + eval-index for one sheet. No UNO.

    *cells* must already be row-major. Cap-hit skips the whole sheet: no
    patches, no strip-safe marks, and a user-visible message string.
    """
    incoming = {cell_map_key(k): v for k, v in dict(records or {}).items()}
    if discovery_cap_hit(len(cells)):
        message = geometric_cap_hit_user_message(sheet_name or "?")
        return SheetRepairResult(
            skipped=True,
            skip_reason="discovery_cap",
            patches=(),
            records=dict(incoming),
            strip_safe=frozenset(),
            user_message=message,
            sheet_name=sheet_name,
        )

    working = dict(incoming)
    patches: list[GeometricPatch] = []
    new_formulas = {cell.address: cell.formula for cell in cells}

    for i, cell in enumerate(cells):
        data_args = formula_data_args(cell.formula)
        if data_args is None:
            continue
        desired = local_a1(cells[i - 1].address) if i > 0 else None
        key = cell_map_key(cell.address)
        action, new_args = _plan_action(
            desired=desired,
            data_args=data_args,
            record=working.get(key),
        )
        if action == "noop":
            continue
        new_formula = rebuild_formula_with_data_args(cell.formula, new_args)
        if new_formula is None or new_formula == cell.formula:
            continue
        patches.append(
            GeometricPatch(
                address=cell.address,
                old_formula=cell.formula,
                new_formula=new_formula,
                action=action,
                predecessor=desired,
            )
        )
        new_formulas[cell.address] = new_formula
        if action == "remove":
            working.pop(key, None)
        elif desired is not None:
            working[key] = GeometricRecord(predecessor=desired)

    strip_safe = compute_eval_index(cells, new_formulas, working, workbook_key)
    return SheetRepairResult(
        skipped=False,
        skip_reason=None,
        patches=tuple(patches),
        records=working,
        strip_safe=strip_safe,
        sheet_name=sheet_name,
    )


# ---------------------------------------------------------------------------
# Phase 2 / 4 — UDProp map, UI attach, eval-time strip
# ---------------------------------------------------------------------------
# Copy the spill pattern (function.py WriterAgentSpillRegistry / SPILL_REGISTRY).
# Eval identity is unanimous-ours + workbook_key, not a 1×1 / uniqueness heuristic.

GEOMETRIC_RECORDS: dict[tuple[str, str, str], GeometricRecord] = {}
_STRIP_SAFE: frozenset[EvalIndexKey] = frozenset()
GEOMETRIC_LOADED: set[str] = set()
_GEOMETRIC_LOCK = threading.Lock()
_GEOMETRIC_REPAIRING = False
_LAST_GEOMETRIC_FLAG: bool | None = None
_CONFIG_SUBSCRIBED = False


def reset_geometric_runtime_for_tests() -> None:
    """Drop in-memory maps. Tests only."""
    global _STRIP_SAFE, _GEOMETRIC_REPAIRING, _LAST_GEOMETRIC_FLAG
    with _GEOMETRIC_LOCK:
        GEOMETRIC_RECORDS.clear()
        GEOMETRIC_LOADED.clear()
    _STRIP_SAFE = frozenset()
    _GEOMETRIC_REPAIRING = False


def geometric_flag_enabled() -> bool:
    """Settings flag. Default false; missing schema is off, not an exception."""
    from plugin.framework.config import get_config_bool_safe

    return get_config_bool_safe(CONFIG_KEY)


def geometric_workbook_key(doc: Any) -> str:
    """``calc:`` + ``_workbook_session_key`` — never empty (unsaved uses a persisted id)."""
    from plugin.scripting.session_manager import _workbook_session_key

    key = (_workbook_session_key(doc) or "").strip()
    if not key:
        # _workbook_session_key already avoids "". This is the #402 last resort.
        key = f"unsaved:{uuid.uuid4()}"
    return f"calc:{key}"


def record_geometric_calc_session(doc: Any) -> str:
    """Isolated UI load/repair must record the same string eval reads."""
    from plugin.scripting.session_manager import record_active_calc_session

    sid = geometric_workbook_key(doc)
    record_active_calc_session(sid)
    return sid


def current_geometric_strip_safe() -> frozenset[EvalIndexKey]:
    """Worker-safe read: return the current snapshot name (do not copy-mutate)."""
    return _STRIP_SAFE


def replace_geometric_strip_safe(workbook_key: str, safe: frozenset[EvalIndexKey]) -> None:
    """Rebind the strip-safe snapshot for one workbook. Other workbooks stay.

    Written on the UI thread, read from the recalc worker. Frozenset is
    immutable; assigning ``_STRIP_SAFE = …`` is GIL-atomic (§3.5). Do not
    mutate a live set in place like ``SPILL_REGISTRY``.
    """
    global _STRIP_SAFE
    kept = frozenset(k for k in _STRIP_SAFE if k.workbook_key != workbook_key)
    _STRIP_SAFE = kept | frozenset(safe)


def records_for_sheet(workbook_key: str, sheet_name: str) -> dict[str, GeometricRecord]:
    with _GEOMETRIC_LOCK:
        return {
            addr: rec
            for (wk, sheet, addr), rec in GEOMETRIC_RECORDS.items()
            if wk == workbook_key and sheet == sheet_name
        }


def replace_records_for_sheet(
    workbook_key: str,
    sheet_name: str,
    records: Mapping[str, GeometricRecord],
) -> None:
    with _GEOMETRIC_LOCK:
        stale = [
            key
            for key in GEOMETRIC_RECORDS
            if key[0] == workbook_key and key[1] == sheet_name
        ]
        for key in stale:
            GEOMETRIC_RECORDS.pop(key, None)
        for addr, rec in records.items():
            GEOMETRIC_RECORDS[(workbook_key, sheet_name, cell_map_key(addr))] = rec


def load_geometric_registry_for_doc(doc: Any) -> str:
    """Load UDProp into the in-memory map. Returns the live workbook_key."""
    workbook_key = record_geometric_calc_session(doc)
    try:
        from plugin.doc.udprops import get_document_property

        raw = get_document_property(doc, GEOMETRIC_REGISTRY_PROP, None)
        if not isinstance(raw, str) or not raw.strip():
            GEOMETRIC_LOADED.add(workbook_key)
            return workbook_key
        payload = json.loads(raw)
        sheets = payload.get("sheets") if isinstance(payload, dict) else None
        if not isinstance(sheets, dict):
            # Flat spill-like fallback: "Sheet1:A2" -> "A1"
            sheets = {}
            if isinstance(payload, dict):
                for key, pred in payload.items():
                    if key in ("workbook_key", "sheets") or not isinstance(pred, str):
                        continue
                    if ":" not in key:
                        continue
                    sheet, addr = key.split(":", 1)
                    sheets.setdefault(sheet, {})[addr] = pred
        with _GEOMETRIC_LOCK:
            for sheet_name, addrs in sheets.items():
                if not isinstance(addrs, dict):
                    continue
                for addr, pred in addrs.items():
                    predecessor = pred
                    if isinstance(pred, dict):
                        predecessor = pred.get("predecessor", "")
                    if not predecessor:
                        continue
                    GEOMETRIC_RECORDS[
                        (workbook_key, str(sheet_name), cell_map_key(str(addr)))
                    ] = GeometricRecord(predecessor=local_a1(str(predecessor)))
        GEOMETRIC_LOADED.add(workbook_key)
    except Exception:
        log.exception("Failed to load geometric registry from document property")
        GEOMETRIC_LOADED.add(workbook_key)
    return workbook_key


def save_geometric_registry_for_doc(doc: Any, workbook_key: str) -> None:
    """Persist this workbook's attach records. Sibling of save_spill_registry_for_doc."""
    try:
        from plugin.doc.udprops import set_document_property

        sheets: dict[str, dict[str, str]] = {}
        with _GEOMETRIC_LOCK:
            for (wk, sheet, addr), rec in GEOMETRIC_RECORDS.items():
                if wk != workbook_key:
                    continue
                sheets.setdefault(sheet, {})[addr] = rec.predecessor
        set_document_property(
            doc,
            GEOMETRIC_REGISTRY_PROP,
            json.dumps({"workbook_key": workbook_key, "sheets": sheets}),
        )
    except Exception:
        log.exception("Failed to save geometric registry to document property")


def clear_in_memory_geometric_state(*, workbook_key: str = "") -> None:
    """Drop instance-scoped geometric maps. UDProp is left for a later open."""
    global _STRIP_SAFE
    with _GEOMETRIC_LOCK:
        if workbook_key:
            for key in [k for k in GEOMETRIC_RECORDS if k[0] == workbook_key]:
                GEOMETRIC_RECORDS.pop(key, None)
            GEOMETRIC_LOADED.discard(workbook_key)
        else:
            GEOMETRIC_RECORDS.clear()
            GEOMETRIC_LOADED.clear()
    if workbook_key:
        _STRIP_SAFE = frozenset(k for k in _STRIP_SAFE if k.workbook_key != workbook_key)
    else:
        _STRIP_SAFE = frozenset()


def maybe_strip_geometric_eval_args(resolved_code: str, args: list[Any]) -> list[Any]:
    """Drop the last split arg when the triple is strip-safe. No UNO.

    Must run after ``split_python_addin_data_args`` and before
    ``calc_addin_args_from_split`` / the matrix-index heuristic. Two open
    workbooks (unambiguous false) → no strip. Does **not** consult
    ``geometric_flag_enabled`` — §9.4 flag-off leaves leftover refs, so
    leftover attached last args must still strip.
    """
    if not args:
        return args
    from plugin.scripting.session_manager import (
        get_cached_calc_session_id,
        off_main_calc_session_is_unambiguous,
    )

    unambiguous = off_main_calc_session_is_unambiguous()
    workbook_key = get_cached_calc_session_id() if unambiguous else None
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
    from plugin.calc.address_utils import parse_address

    sheet_name, rest = split_sheet_prefix(ref)
    local = rest.replace("$", "").strip()
    sheet = default_sheet
    if sheet_name and doc is not None:
        try:
            sheet = doc.getSheets().getByName(sheet_name)
        except Exception:
            pass
    if sheet is None or not local:
        return ""
    try:
        col, row = parse_address(local)
        cell = sheet.getCellByPosition(col, row)
        return str(cell.getString() or "")
    except Exception:
        return ""


def _resolved_code_for_discovered(doc: Any, sheet: Any, formula: str) -> str:
    from plugin.calc.python.cell_discovery import canonicalize_py_formula_for_parse

    canon = canonicalize_py_formula_for_parse(formula)
    if py_formula_has_unquoted_code_ref(canon):
        parts = parse_python_formula(canon)
        if parts is None:
            return ""
        return _read_code_ref_text(doc, sheet, parts.code)
    return resolved_code_for_formula(canon)


def geometric_cells_on_sheet(doc: Any, sheet: Any) -> tuple[list[GeometricCell], str]:
    """Discover one sheet as Phase 1 cells. Address is local A1 (per-sheet map)."""
    from plugin.calc.python.cell_discovery import list_python_cells_on_sheet

    infos = list_python_cells_on_sheet(sheet)
    try:
        name = str(sheet.getName() or "") or "Sheet"
    except Exception:
        name = "Sheet"
    cells = [
        GeometricCell(
            address=local_a1(info.address),
            formula=info.formula,
            resolved_code=_resolved_code_for_discovered(doc, sheet, info.formula),
        )
        for info in infos
    ]
    return cells, name


def _iter_sheets(doc: Any) -> list[Any]:
    out: list[Any] = []
    try:
        sheets = doc.getSheets()
        for i in range(int(sheets.getCount())):
            out.append(sheets.getByIndex(i))
    except Exception:
        log.debug("geometric_recalc: sheet walk failed", exc_info=True)
    return out


def _sheet_of_cell(cell: Any, doc: Any) -> Any | None:
    if cell is not None and hasattr(cell, "getSpreadsheet"):
        try:
            sheet = cell.getSpreadsheet()
            if sheet is not None:
                return sheet
        except Exception:
            pass
    try:
        ctrl = doc.getCurrentController()
        if ctrl is not None:
            return ctrl.getActiveSheet()
    except Exception:
        pass
    return None


def _cell_on_sheet(sheet: Any, address: str) -> Any | None:
    from plugin.calc.address_utils import parse_address

    local = local_a1(address)
    if not local:
        return None
    try:
        col, row = parse_address(local)
        return sheet.getCellByPosition(col, row)
    except Exception:
        return None


def _apply_patches_to_sheet(sheet: Any, patches: tuple[GeometricPatch, ...]) -> int:
    """``setFormula`` for each patch. Caller holds ``_undo_lock`` + re-entrancy."""
    applied = 0
    for patch in patches:
        cell = _cell_on_sheet(sheet, patch.address)
        if cell is None:
            continue
        try:
            current = str(cell.getFormula() or "")
        except Exception:
            continue
        if current != patch.old_formula:
            # Stale: user or Calc changed the cell since we computed the patch.
            continue
        try:
            cell.setFormula(patch.new_formula)
            applied += 1
        except Exception:
            log.debug("geometric_recalc: setFormula failed at %s", patch.address, exc_info=True)
    return applied


def _rebuild_strip_safe_from_doc(
    ctx: Any,
    doc: Any,
    workbook_key: str,
    *,
    already_notified: set[str] | None = None,
) -> None:
    """Workbook-wide unanimous-ours. Cap-hit sheets are omitted (cannot prove)."""
    all_cells: list[GeometricCell] = []
    all_formulas: dict[str, str] = {}
    all_records: dict[str, GeometricRecord] = {}
    notified = already_notified if already_notified is not None else set()
    for sheet in _iter_sheets(doc):
        cells, name = geometric_cells_on_sheet(doc, sheet)
        if discovery_cap_hit(len(cells)):
            notify_geometric_cap_hit(ctx, name, already_notified=notified)
            continue
        for cell in cells:
            scoped = f"{name}:{cell_map_key(cell.address)}"
            all_cells.append(
                GeometricCell(scoped, cell.formula, cell.resolved_code)
            )
            all_formulas[scoped] = cell.formula
        for addr, rec in records_for_sheet(workbook_key, name).items():
            all_records[f"{name}:{addr}"] = rec
    replace_geometric_strip_safe(
        workbook_key,
        compute_eval_index(all_cells, all_formulas, all_records, workbook_key),
    )


def _repair_one_sheet(
    ctx: Any,
    doc: Any,
    sheet: Any,
    workbook_key: str,
    *,
    apply_patches: bool,
    already_notified: set[str] | None = None,
) -> SheetRepairResult:
    cells, name = geometric_cells_on_sheet(doc, sheet)
    result = compute_sheet_repair(
        cells,
        records_for_sheet(workbook_key, name),
        workbook_key=workbook_key,
        sheet_name=name,
    )
    if result.skipped:
        notify_geometric_cap_hit(ctx, name, already_notified=already_notified)
        return result
    if apply_patches and result.patches:
        _apply_patches_to_sheet(sheet, result.patches)
    replace_records_for_sheet(workbook_key, name, result.records)
    return result


def reconcile_geometric_document(ctx: Any, doc: Any, *, already_loaded: bool = False) -> None:
    """Flag-on / document-open: attach every sheet, one locked undo unit."""
    global _GEOMETRIC_REPAIRING
    if doc is None or _GEOMETRIC_REPAIRING:
        return
    workbook_key = (
        record_geometric_calc_session(doc)
        if already_loaded
        else load_geometric_registry_for_doc(doc)
    )
    _GEOMETRIC_REPAIRING = True
    try:
        from plugin.calc.python.function import _undo_lock

        notified: set[str] = set()
        with _undo_lock(doc):
            for sheet in _iter_sheets(doc):
                _repair_one_sheet(
                    ctx,
                    doc,
                    sheet,
                    workbook_key,
                    apply_patches=True,
                    already_notified=notified,
                )
            save_geometric_registry_for_doc(doc, workbook_key)
        _rebuild_strip_safe_from_doc(
            ctx, doc, workbook_key, already_notified=notified
        )
    finally:
        _GEOMETRIC_REPAIRING = False


def reconcile_geometric_sheet(ctx: Any, doc: Any, sheet: Any) -> None:
    """Save-path attach for one sheet. Neighbors on this sheet may retarget."""
    global _GEOMETRIC_REPAIRING
    if doc is None or sheet is None or _GEOMETRIC_REPAIRING:
        return
    workbook_key = load_geometric_registry_for_doc(doc)
    _GEOMETRIC_REPAIRING = True
    try:
        from plugin.calc.python.function import _undo_lock

        notified: set[str] = set()
        with _undo_lock(doc):
            _repair_one_sheet(
                ctx,
                doc,
                sheet,
                workbook_key,
                apply_patches=True,
                already_notified=notified,
            )
            save_geometric_registry_for_doc(doc, workbook_key)
        _rebuild_strip_safe_from_doc(
            ctx, doc, workbook_key, already_notified=notified
        )
    finally:
        _GEOMETRIC_REPAIRING = False


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


def _on_geometric_config_changed(**kwargs: Any) -> None:
    """Flag-on walks all sheets. Flag-off leaves refs and the map."""
    global _LAST_GEOMETRIC_FLAG
    now = geometric_flag_enabled()
    was = _LAST_GEOMETRIC_FLAG
    _LAST_GEOMETRIC_FLAG = now
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
    global _CONFIG_SUBSCRIBED, _LAST_GEOMETRIC_FLAG
    if _CONFIG_SUBSCRIBED:
        return
    _LAST_GEOMETRIC_FLAG = geometric_flag_enabled()
    from plugin.framework.event_bus import global_event_bus

    global_event_bus.subscribe("config:changed", _on_geometric_config_changed)
    _CONFIG_SUBSCRIBED = True
