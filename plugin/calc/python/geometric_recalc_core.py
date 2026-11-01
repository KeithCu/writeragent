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
from typing import Literal, Mapping

from plugin.calc.address_utils import format_address, parse_address, parse_range_string, split_sheet_prefix
from plugin.calc.python.formula_edit import (
    PythonFormulaParts,
    assemble_python_formula,
    escape_code_for_formula,
    format_data_binding_display,
    format_py_data_range,
    parse_data_binding_text,
    parse_python_formula,
    py_code_arg_is_cell_ref,
    py_formula_has_unquoted_code_ref,
)

log = logging.getLogger(__name__)

GEOMETRIC_REGISTRY_PROP = "WriterAgentGeometricRegistry"
CONFIG_KEY = "scripting.python_geometric_recalc_order"
FEATURE_TITLE = "Geometric Recalc Order (Experimental)"


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
    action: Literal["append", "replace", "remove"]


@dataclass(frozen=True)
class EvalIndexKey:
    """Eval-time strip key. ``code`` is resolved source, not a ``$A$1`` token.

    Unanimous-ours on ``(workbook_key, resolved_code, n_args)`` never produces
    wrong numbers — a mixed same-triple only widens no-strip.
    """

    workbook_key: str
    resolved_code: str
    n_args: int


def local_a1(address: str) -> str:
    """``Sheet1.A1`` / ``$A$1`` / ``A1`` → ``A1`` (no ``$``)."""
    _sheet, rest = split_sheet_prefix(address or "")
    return rest.replace("$", "").strip().upper()


def _parse_local(address: str) -> tuple[int, int] | None:
    local = local_a1(address)
    if not local:
        return None
    try:
        return parse_address(local)
    except (TypeError, ValueError):
        return None


def same_cell_ref(left: str, right: str) -> bool:
    """True when two formula tokens name the same cell (``$`` / sheet ignored)."""
    return bool(left) and bool(right) and local_a1(left) == local_a1(right)


def _arg_covers_cell(arg: str, target_a1: str, *, sheet_name: str | None = None) -> bool:
    """True when a data-arg token (cell or range) includes *target_a1*."""
    sheet, _rest = split_sheet_prefix(arg or "")
    if sheet and (sheet_name is None or sheet.casefold() != sheet_name.casefold()):
        return False
    local = local_a1(arg)
    if not local or not target_a1:
        return False
    try:
        (c0, r0), (c1, r1) = parse_range_string(local)
        tcol, trow = parse_address(target_a1)
    except (TypeError, ValueError):
        return False
    c_lo, c_hi = (c0, c1) if c0 <= c1 else (c1, c0)
    r_lo, r_hi = (r0, r1) if r0 <= r1 else (r1, r0)
    return c_lo <= tcol <= c_hi and r_lo <= trow <= r_hi


def _args_of(parts: PythonFormulaParts) -> list[str]:
    return parse_data_binding_text(format_data_binding_display(parts.data_suffix))


def formula_data_args(formula: str) -> list[str] | None:
    """Trailing ``=PY`` data args, or None if the formula does not parse."""
    parts = parse_python_formula(formula)
    if parts is None:
        return None
    return _args_of(parts)


def rebuild_formula_with_data_args(formula: str, data_args: list[str]) -> str | None:
    """Spliced formula. Existing user args stay verbatim (including ``$``).

    Quoted code is quote-escaped only (``"`` → ``""``). Code-in-cell keeps
    the unquoted ref (``$A$1``).
    """
    parts = parse_python_formula(formula)
    if parts is None:
        return None
    old_args = _args_of(parts)
    # Join existing user args verbatim; format only a new/replaced last pred.
    # ``build_data_suffix`` / ``_format_py_data_range_body`` strip ``$`` from
    # every token. Keep the parsed original spelling; only the appended/replaced
    # predecessor is formatted.
    if not data_args:
        suffix = ")"
    else:
        emitted: list[str] = []
        for i, tok in enumerate(data_args):
            if i < len(old_args) and same_cell_ref(tok, old_args[i]):
                emitted.append(old_args[i])
                continue
            if i == len(data_args) - 1:
                emitted.append(format_py_data_range(tok))
            else:
                emitted.append(tok)
        suffix = f";{';'.join(emitted)})"
    if py_formula_has_unquoted_code_ref(formula):
        return assemble_python_formula(parts.prefix, parts.code, suffix, quoted=False)
    return assemble_python_formula(parts.prefix, escape_code_for_formula(parts.code), suffix, quoted=True)


def _last_cell_ref(args: list[str]) -> str | None:
    if not args:
        return None
    last = args[-1]
    return last if py_code_arg_is_cell_ref(last) else None


def _last_matches(args: list[str], ref: str) -> bool:
    last = _last_cell_ref(args)
    return last is not None and same_cell_ref(last, ref)


def _adj_for(
    formula: str,
    args: list[str] | None,
    live_keys: set[str],
    sheet_name: str,
) -> set[str]:
    tokens = list(args or [])
    if py_formula_has_unquoted_code_ref(formula):
        parts = parse_python_formula(formula)
        if parts is not None and parts.code:
            tokens.append(parts.code)
    return {
        live for live in live_keys
        if any(_arg_covers_cell(token, live, sheet_name=sheet_name) for token in tokens)
    }


def _reaches(start: str, target: str, adj: Mapping[str, set[str]]) -> bool:
    stack, seen = [start], set()
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur, set()))
    return False


def _plan_cell(
    *,
    desired: str | None,
    data_args: list[str],
    record: GeometricRecord | None,
    cycle: bool,
) -> tuple[Literal["append", "replace", "remove", "noop", "skip"], list[str]]:
    """Determine action and arguments for a cell, guarding against Err:522 cycles."""
    if desired is not None and cycle:
        # Predecessor already reaches successor → attaching ;desired onto cell is Err:522.
        if record is not None and _last_matches(data_args, record.predecessor):
            return "remove", data_args[:-1]
        return "skip", data_args

    if desired is None:
        if record is not None and _last_cell_ref(data_args) is not None:
            return "remove", data_args[:-1]
        return "noop", data_args
    if _last_matches(data_args, desired):
        return "noop", data_args
    if record is not None and _last_matches(data_args, record.predecessor):
        return "replace", data_args[:-1] + [desired]
    return "append", data_args + [desired]


def _rule2_candidate_score(old: str, rec: GeometricRecord, live: str, desired: str) -> tuple[int, int] | None:
    """``pred + (live − old)`` equals *desired* — the cell moved with its formula."""
    parsed_old = _parse_local(old)
    parsed_live = _parse_local(live)
    if parsed_old is None or parsed_live is None:
        return None
    ocol, orow = parsed_old
    lcol, lrow = parsed_live
    dcol, drow = lcol - ocol, lrow - orow
    if not (dcol or drow):
        return None
    parsed_pred = _parse_local(rec.predecessor)
    if parsed_pred is None:
        return None
    pcol, prow = parsed_pred
    ncol, nrow = pcol + dcol, prow + drow
    if ncol < 0 or nrow < 0:
        return None
    if same_cell_ref(format_address(ncol, nrow), desired):
        return (0, abs(dcol) + abs(drow))
    return None


def _collect_rule2_claimed(
    keys: list[str],
    incoming: Mapping[str, GeometricRecord],
    live_keys: set[str],
    desired_by_key: Mapping[str, str | None],
    data_args_by_key: Mapping[str, list[str] | None],
) -> set[str]:
    """Incoming keys a row/col delta will move. Assigned in sheet order."""
    claimed: set[str] = set()
    for key in keys:
        desired = desired_by_key.get(key)
        if desired is None:
            continue
        data_args = data_args_by_key.get(key)
        if not data_args or not _last_matches(data_args, desired):
            continue
        best: tuple[int, int] | None = None
        chosen: str | None = None
        for old, rec in incoming.items():
            if old in claimed:
                continue
            # Still on the sheet and already pointing at that cell's desired pred.
            live_desired = desired_by_key.get(old)
            if old in live_keys and live_desired is not None and same_cell_ref(rec.predecessor, live_desired):
                continue
            score = _rule2_candidate_score(old, rec, key, desired)
            if score is None:
                continue
            if best is None or score < best:
                chosen, best = old, score
        if chosen is not None:
            claimed.add(chosen)
    return claimed


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
    if desired is None or not _last_matches(data_args, desired):
        return
    chosen: str | None = None
    best: tuple[int, int] | None = None
    for old, rec in incoming.items():
        if old in consumed:
            continue
        # Gone, rule-2 moved, or overwritten. Live unclaimed keys stay for in-place.
        if old not in evicted and old in live_keys and old not in rule2_claimed:
            continue
        score = _rule2_candidate_score(old, rec, key, desired)
        # Rule 1: pred already equals desired, and only when old is gone (true orphan).
        if score is None and same_cell_ref(rec.predecessor, desired) and old not in live_keys:
            score = (1, 0)
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
    if (chosen != key and working.get(chosen) is incoming.get(chosen)) or chosen not in live_keys:
        working.pop(chosen, None)
    if key in incoming and key != chosen and key not in consumed:
        evicted.add(key)
    working[key] = GeometricRecord(predecessor=desired)
    log.debug("geometric_recalc: rehomed attach record %s -> %s (pred %s)", chosen, key, desired)


def compute_eval_index(
    cells: list[GeometricCell],
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
    seen: set[EvalIndexKey] = set()
    bad: set[EvalIndexKey] = set()
    for cell in cells:
        args = formula_data_args(cell.formula)
        n_args = 0 if args is None else len(args)
        key = EvalIndexKey(workbook_key, cell.resolved_code, n_args)
        seen.add(key)
        rec = records.get(cell.address)
        if rec is None or not args or not _last_matches(args, rec.predecessor):
            bad.add(key)
    return frozenset(seen - bad)


def compute_sheet_repair(
    cells: list[GeometricCell],
    records: Mapping[str, GeometricRecord] | None = None,
    *,
    sheet_name: str = "",
) -> tuple[tuple[GeometricPatch, ...], dict[str, GeometricRecord]]:
    """List-diff + splice for one sheet. No UNO.

    *cells* must already be row-major.
    An exact 100 is chained.
    """
    incoming = {local_a1(k): v for k, v in (records or {}).items()}
    working = dict(incoming)
    patches: list[GeometricPatch] = []
    keys = [local_a1(cell.address) for cell in cells]
    live_keys = set(keys)
    data_args_by_key = {
        key: formula_data_args(cell.formula) for key, cell in zip(keys, cells)
    }
    adj: dict[str, set[str]] = {}
    desired_by_key: dict[str, str | None] = {}
    for i, (key, cell) in enumerate(zip(keys, cells)):
        adj[key] = _adj_for(cell.formula, data_args_by_key.get(key), live_keys, sheet_name)
        desired_by_key[key] = keys[i - 1] if i > 0 else None
    consumed: set[str] = set()
    evicted: set[str] = set()
    rule2_claimed = _collect_rule2_claimed(keys, incoming, live_keys, desired_by_key, data_args_by_key)

    for key, cell in zip(keys, cells):
        data_args = data_args_by_key.get(key)
        if data_args is None:
            continue
        desired = desired_by_key[key]
        would_cycle = desired is not None and _reaches(desired, key, adj)
        action, new_args = _plan_cell(
            desired=desired,
            data_args=data_args,
            record=working.get(key),
            cycle=would_cycle,
        )
        if action == "skip":
            log.debug(
                "geometric_recalc: skip attach %s onto %s (predecessor already refs successor; Err:522)",
                desired,
                key,
            )
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
            )
        )
        adj[key] = _adj_for(cell.formula, new_args, live_keys, sheet_name)
        if action == "remove":
            working.pop(key, None)
        elif desired is not None:
            working[key] = GeometricRecord(predecessor=desired)

    # Drop keys that are no longer on the sheet (moved or deleted).
    working = {addr: rec for addr, rec in working.items() if addr in live_keys}

    return (
        tuple(patches),
        working,
    )
