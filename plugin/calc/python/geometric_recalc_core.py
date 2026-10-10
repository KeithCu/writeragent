# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometric Recalc Order: pure formula analysis, patch planning, and eval index.

Provides pure helpers (no UNO, no mutable process state) for:
- Splitting, inspecting, and splicing =PY formula arguments.
- Planning attach, replace, remove, or noop actions for sequential PY cells.
- Rehoming attach records across row/column moves and edits.
- Computing unanimous-ours strip-safe evaluation keys.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from plugin.calc.address_utils import index_to_column, parse_address, parse_range_string, split_sheet_prefix
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
FEATURE_TITLE = "Geometric Recalc Order (Experimental)"

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

    Unanimous-ours on ``(workbook_key, resolved_code, n_args)`` never produces
    wrong numbers — a mixed same-triple only widens no-strip.
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


def same_cell_ref(left: str, right: str) -> bool:
    """True when two formula tokens name the same cell (``$`` / sheet ignored)."""
    return bool(left) and bool(right) and local_a1(left) == local_a1(right)


def _arg_covers_cell(arg: str, target_a1: str) -> bool:
    """True when a data-arg token (cell or range) includes *target_a1*."""
    _sheet, rest = split_sheet_prefix(arg or "")
    rest = rest.replace("$", "").strip()
    if not rest or not target_a1:
        return False
    try:
        (c0, r0), (c1, r1) = parse_range_string(rest)
        tcol, trow = parse_address(target_a1)
    except (TypeError, ValueError):
        return False
    c_lo, c_hi = (c0, c1) if c0 <= c1 else (c1, c0)
    r_lo, r_hi = (r0, r1) if r0 <= r1 else (r1, r0)
    return c_lo <= tcol <= c_hi and r_lo <= trow <= r_hi


def formula_mentions_cell(formula: str, address: str) -> bool:
    """True when *formula* has a one-hop Calc ref to *address* (Err:522 risk).

    Checks trailing ``=PY`` data args (cell or range) and an unquoted
    code-in-cell token. Does not parse Python source for ``xl("A2")``.
    """
    target = local_a1(address)
    if not target or not formula:
        return False
    args = formula_data_args(formula)
    if args is None:
        return False
    if any(_arg_covers_cell(arg, target) for arg in args):
        return True
    if py_formula_has_unquoted_code_ref(formula):
        parts = parse_python_formula(formula)
        if parts is not None and same_cell_ref(parts.code, address):
            return True
    return False


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


def discovery_cap_hit(n_found: int, *, truncated: bool | None = None) -> bool:
    """True when discovery was truncated — skip the whole sheet.

    Pass *truncated* from :func:`discover_python_cells_on_sheet`. ``truncated=False``
    with 100 cells is complete. ``truncated=True`` skips even when fewer
    than 100 (50k scan cap). Omitting *truncated* falls back to ``len >= 100``.
    """
    if truncated is not None:
        return truncated
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
    every token. Keep the parsed original spelling; only the appended/replaced
    predecessor is formatted.
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

    Quoted code is quote-escaped only (``"`` → ``""``). Code-in-cell keeps
    the unquoted ref (``$A$1``).
    """
    parts = parse_python_formula(formula)
    if parts is None:
        return None
    old_args = formula_data_args(formula) or []
    suffix = _geometric_data_suffix(old_args, data_args)
    if py_formula_has_unquoted_code_ref(formula):
        return f"{parts.prefix}{parts.code}{suffix}"
    return f'{parts.prefix}"{escape_code_for_excel_formula(parts.code)}"{suffix}'


def geometric_cap_hit_user_message(sheet_name: str) -> str:
    """User-facing text when a sheet is left unchained."""
    return _(
        "Geometric Recalc Order skipped sheet '%(sheet)s': found %(cap)s or more Python cells (discovery cap). The sheet was left unchained so a partial list is not treated as complete."
    ) % {"sheet": sheet_name, "cap": GEOMETRIC_DISCOVERY_CAP}


def _plan_action(
    *,
    desired: str | None,
    data_args: list[str],
    record: GeometricRecord | None,
) -> tuple[Literal["append", "replace", "remove", "noop"], list[str]]:
    last = data_args[-1] if data_args else None
    last_is_cell = last is not None and py_code_arg_is_cell_ref(last)

    if desired is None:
        if record is not None and last_is_cell:
            return "remove", data_args[:-1]
        return "noop", data_args

    if last_is_cell and last is not None and same_cell_ref(last, desired):
        return "noop", data_args

    if record is not None and last_is_cell and last is not None and same_cell_ref(last, record.predecessor):
        return "replace", data_args[:-1] + [desired]

    return "append", data_args + [desired]


def _plan_cell(
    *,
    desired: str | None,
    data_args: list[str],
    record: GeometricRecord | None,
    pred_formula: str | None,
    cell_address: str,
) -> tuple[Literal["append", "replace", "remove", "noop", "skip"], list[str]]:
    """Determine action and arguments for a cell, guarding against Err:522 cycles."""
    if desired is not None and pred_formula is not None and formula_mentions_cell(pred_formula, cell_address):
        # A1 already names A2 → attaching ;A1 onto A2 is Err:522.
        last = data_args[-1] if data_args else None
        last_is_ours = (
            record is not None
            and last is not None
            and py_code_arg_is_cell_ref(last)
            and same_cell_ref(last, record.predecessor)
        )
        if last_is_ours:
            return "remove", data_args[:-1]
        log.debug(
            "geometric_recalc: skip attach %s onto %s (predecessor already refs successor; Err:522)",
            desired,
            local_a1(cell_address),
        )
        return "skip", data_args

    return _plan_action(desired=desired, data_args=data_args, record=record)


def _a1_shift(address: str, dcol: int, drow: int) -> str | None:
    """Apply a col/row delta to a sheet-local A1 token. ``None`` if unusable."""
    try:
        col, row = parse_address(local_a1(address))
    except (TypeError, ValueError):
        return None
    ncol, nrow = col + dcol, row + drow
    if ncol < 0 or nrow < 0:
        return None
    try:
        return f"{index_to_column(ncol)}{nrow + 1}"
    except (TypeError, ValueError):
        return None


def _rule2_candidate_score(old: str, rec: GeometricRecord, live: str, desired: str) -> tuple[int, int] | None:
    """``pred + (live − old)`` equals *desired* — the cell moved with its formula."""
    try:
        ocol, orow = parse_address(local_a1(old))
        lcol, lrow = parse_address(local_a1(live))
    except (TypeError, ValueError):
        return None
    dcol, drow = lcol - ocol, lrow - orow
    if not (dcol or drow):
        return None
    shifted = _a1_shift(rec.predecessor, dcol, drow)
    if shifted is not None and same_cell_ref(shifted, desired):
        return (0, abs(dcol) + abs(drow))
    return None


def _rehome_candidate_score(
    old: str,
    rec: GeometricRecord,
    live: str,
    desired: str,
    live_keys: set[str],
) -> tuple[int, int] | None:
    """Match a homeless incoming record to a live noop cell. Lower is better.

    Rule 2 (0, absdelta): ``pred + (live - old)`` equals *desired* — the cell
    moved and Calc already shifted the formula pred. Rule 1 (1, 0): pred
    already equals *desired*, but **only** when *old* is gone from the
    sheet (true orphan).
    """
    rule2 = _rule2_candidate_score(old, rec, live, desired)
    if rule2 is not None:
        return rule2
    if same_cell_ref(rec.predecessor, desired) and old not in live_keys:
        return (1, 0)
    return None


def _incoming_is_displaced(
    old: str,
    rec: GeometricRecord,
    live_keys: set[str],
    desired_by_key: Mapping[str, str | None],
) -> bool:
    """True when *old* is gone, first in the list, or pred ≠ that cell's desired."""
    if old not in live_keys:
        return True
    live_desired = desired_by_key.get(old)
    if live_desired is None:
        return True
    return not same_cell_ref(rec.predecessor, live_desired)


def _collect_rule2_claimed(
    cells: list[GeometricCell],
    incoming: Mapping[str, GeometricRecord],
    live_keys: set[str],
    desired_by_key: Mapping[str, str | None],
) -> set[str]:
    """Incoming keys a row/col delta will move. Assigned in sheet order."""
    claimed: set[str] = set()
    for cell in cells:
        key = local_a1(cell.address)
        desired = desired_by_key.get(key)
        if desired is None:
            continue
        data_args = formula_data_args(cell.formula)
        if not data_args:
            continue
        last = data_args[-1]
        if not py_code_arg_is_cell_ref(last) or not same_cell_ref(last, desired):
            continue
        best: tuple[int, int] | None = None
        chosen: str | None = None
        for old, rec in incoming.items():
            if old in claimed:
                continue
            if not _incoming_is_displaced(old, rec, live_keys, desired_by_key):
                continue
            score = _rule2_candidate_score(old, rec, key, desired)
            if score is None:
                continue
            if best is None or score < best:
                chosen, best = old, score
        if chosen is not None:
            claimed.add(chosen)
    return claimed


def _record_is_homeless(old: str, live_keys: set[str], evicted: set[str], rule2_claimed: set[str]) -> bool:
    """Gone, rule-2 moved, or overwritten. Live unclaimed keys stay for in-place."""
    if old in evicted or old not in live_keys:
        return True
    return old in rule2_claimed


def _rehome_or_keep_record(
    working: dict[str, GeometricRecord],
    incoming: Mapping[str, GeometricRecord],
    live_keys: set[str],
    key: str,
    desired: str | None,
    data_args: list[str],
    *,
    consumed: set[str],
    evicted: set[str],
    rule2_claimed: set[str],
) -> None:
    """Keep a true live record, or rehome a homeless one after a row/col move."""
    if desired is None:
        return
    last = data_args[-1] if data_args else None
    if last is None or not py_code_arg_is_cell_ref(last) or not same_cell_ref(last, desired):
        return
    chosen: str | None = None
    best: tuple[int, int] | None = None
    for old, rec in incoming.items():
        if old in consumed:
            continue
        if not _record_is_homeless(old, live_keys, evicted, rule2_claimed):
            continue
        score = _rehome_candidate_score(old, rec, key, desired, live_keys)
        if score is None:
            continue
        if best is None or score < best:
            chosen, best = old, score
    if chosen is None:
        incoming_here = incoming.get(key)
        if incoming_here is not None and key not in consumed and key not in evicted:
            working[key] = GeometricRecord(predecessor=desired)
        else:
            log.debug("geometric_recalc: no homeless match for %s (desired %s); not recording", key, desired)
        return
    consumed.add(chosen)
    if chosen != key and working.get(chosen) is incoming.get(chosen):
        working.pop(chosen, None)
    elif chosen not in live_keys:
        working.pop(chosen, None)
    if key in incoming and key != chosen and key not in consumed:
        evicted.add(key)
    working[key] = GeometricRecord(predecessor=desired)
    log.debug("geometric_recalc: rehomed attach record %s -> %s (pred %s)", chosen, key, desired)


def compute_eval_index(
    cells: list[GeometricCell],
    formulas: Mapping[str, str],
    records: Mapping[str, GeometricRecord],
    workbook_key: str,
) -> frozenset[EvalIndexKey]:
    """Strip-safe iff every discovered cell with that triple is attached and ours.

    The cell's trailing single-cell argument must match rec.predecessor
    via same_cell_ref. compute_eval_index used to accept any address that
    was already in records. A leftover from an edit between passes would
    then mark the group strip-safe after the user replaced the attached
    predecessor with real data, and that data would be stripped.
    """
    cell_by_addr = {cell.address: cell for cell in cells}
    groups: dict[EvalIndexKey, list[str]] = {}
    for cell in cells:
        formula = formulas.get(cell.address, cell.formula)
        key = EvalIndexKey(workbook_key, cell.resolved_code, repair_n_args(formula))
        groups.setdefault(key, []).append(cell.address)

    def _is_ours(addr: str) -> bool:
        rec = records.get(addr)
        if rec is None:
            return False
        cell = cell_by_addr.get(addr)
        formula = formulas.get(addr, cell.formula if cell else "")
        args = formula_data_args(formula)
        if not args:
            return False
        last = args[-1]
        return py_code_arg_is_cell_ref(last) and same_cell_ref(last, rec.predecessor)

    safe: set[EvalIndexKey] = set()
    for key, addrs in groups.items():
        if addrs and all(_is_ours(addr) for addr in addrs):
            safe.add(key)
    return frozenset(safe)


def should_strip_eval_args(
    *,
    workbook_key: str | None,
    resolved_code: str,
    n_args: int,
    strip_safe: frozenset[EvalIndexKey] | set[EvalIndexKey],
    unambiguous: bool,
) -> bool:
    """Eval gate: strip only when the session is unambiguous and the triple is ours."""
    if not unambiguous or not workbook_key:
        return False
    key = EvalIndexKey(workbook_key, resolved_code, n_args)
    return key in strip_safe


def compute_sheet_repair(
    cells: list[GeometricCell],
    records: Mapping[str, GeometricRecord] | None = None,
    *,
    workbook_key: str,
    sheet_name: str = "",
    truncated: bool = False,
) -> SheetRepairResult:
    """List-diff + splice + eval-index for one sheet. No UNO.

    *cells* must already be row-major. Cap-hit (*truncated*) skips the whole
    sheet: no patches, no strip-safe marks, and a user-visible message string.
    An exact 100 with ``truncated=False`` is chained.
    """
    incoming = {local_a1(k): v for k, v in dict(records or {}).items()}
    if discovery_cap_hit(len(cells), truncated=truncated):
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
    live_keys = {local_a1(cell.address) for cell in cells}
    desired_by_key: dict[str, str | None] = {}
    for i, cell in enumerate(cells):
        desired_by_key[local_a1(cell.address)] = local_a1(cells[i - 1].address) if i > 0 else None
    consumed: set[str] = set()
    evicted: set[str] = set()
    rule2_claimed = _collect_rule2_claimed(cells, incoming, live_keys, desired_by_key)

    for i, cell in enumerate(cells):
        data_args = formula_data_args(cell.formula)
        if data_args is None:
            continue
        key = local_a1(cell.address)
        desired = desired_by_key[key]
        pred_formula = new_formulas.get(cells[i - 1].address, cells[i - 1].formula) if i > 0 else None
        action, new_args = _plan_cell(
            desired=desired,
            data_args=data_args,
            record=working.get(key),
            pred_formula=pred_formula,
            cell_address=cell.address,
        )
        if action == "skip":
            continue
        if action == "noop":
            _rehome_or_keep_record(
                working,
                incoming,
                live_keys,
                key,
                desired,
                data_args,
                consumed=consumed,
                evicted=evicted,
                rule2_claimed=rule2_claimed,
            )
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

    # Drop keys that are no longer on the sheet (moved or deleted).
    working = {addr: rec for addr, rec in working.items() if addr in live_keys}

    strip_safe = compute_eval_index(cells, new_formulas, working, workbook_key)
    return SheetRepairResult(
        skipped=False,
        skip_reason=None,
        patches=tuple(patches),
        records=working,
        strip_safe=strip_safe,
        sheet_name=sheet_name,
    )
