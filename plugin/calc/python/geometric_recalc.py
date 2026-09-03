# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometric Recalc Order — Phase 1: list-diff, formula splice, eval-index bools.

Pure helpers. No UNO. Given a row-major list of PY cells plus the in-memory
attach map, compute formula patches and unanimous-ours strip-safe triples.

See ``docs/calc/geometric-recalc-order.md`` §8 Phase 1 and §9.5.
Cap-hit skip uses ``len(cells) >= _MAX_PYTHON_CELLS_FOUND`` (no truncated flag
in this phase). A skipped sheet must also show a user-visible error — callers
use :func:`notify_geometric_cap_hit` on the UI thread (one box per sheet).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from plugin.calc.address_utils import split_sheet_prefix
from plugin.calc.calc_addin_data import split_python_addin_data_args
from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
from plugin.calc.python.formula_edit import (
    CALC_PYTHON_FN,
    build_data_suffix,
    format_data_binding_display,
    parse_data_binding_text,
    parse_python_formula,
    py_code_arg_is_cell_ref,
    py_formula_has_unquoted_code_ref,
    rebuild_python_formula_with_code_ref,
    rebuild_python_formula_with_data,
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
    """Eval-time strip key. ``code`` is resolved source, not a ``$A$1`` token."""

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


def rebuild_formula_with_data_args(formula: str, data_args: list[str]) -> str | None:
    """Spliced formula. Code-in-cell keeps the unquoted ref (``$A$1``), not a quoted token.

    ``rebuild_python_formula_with_data`` would quote ``$A$1``.
    ``rebuild_python_formula_with_code_ref`` is the code-ref builder, but it
    runs the ref through ``format_py_data_range`` which strips ``$``. Emit the
    parsed token + ``build_data_suffix`` so ``=PY($A$1; …)`` stays ``$A$1``.
    """
    parts = parse_python_formula(formula)
    if parts is None:
        return None
    if py_formula_has_unquoted_code_ref(formula):
        # rebuild_python_formula_with_code_ref strips $ via format_py_data_range.
        # Keep the parsed token so =PY($A$1; …) stays $A$1, not A1 and not "$A$1".
        ref = parts.code
        if ref != ref.replace("$", ""):
            return f"={CALC_PYTHON_FN}({ref}{build_data_suffix(data_args)}"
        return rebuild_python_formula_with_code_ref(ref, data_args)
    return rebuild_python_formula_with_data(parts.code, data_args)


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
