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
from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
from plugin.calc.python.formula_edit import (
    assemble_python_formula,
    escape_code_for_formula,
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
    """Patch list + post-repair map for one sheet."""

    skipped: bool
    patches: tuple[GeometricPatch, ...]
    records: dict[str, GeometricRecord]
    sheet_name: str = ""


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


def _local_ref_on_sheet(token: str, sheet_name: str | None) -> str | None:
    sheet, _rest = split_sheet_prefix(token or "")
    local = local_a1(token)
    if not local:
        return None
    if sheet and (sheet_name is None or sheet.casefold() != sheet_name.casefold()):
        return None
    return local


def _arg_covers_cell(arg: str, target_a1: str, *, sheet_name: str | None = None) -> bool:
    """True when a data-arg token (cell or range) includes *target_a1*."""
    local = _local_ref_on_sheet(arg, sheet_name)
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
        return assemble_python_formula(parts.prefix, parts.code, suffix, quoted=False)
    return assemble_python_formula(parts.prefix, escape_code_for_formula(parts.code), suffix, quoted=True)


def geometric_cap_hit_user_message(sheet_name: str) -> str:
    """User-facing text when a sheet is left unchained."""
    return _(
        "Geometric Recalc Order skipped sheet '%(sheet)s': found %(cap)s or more Python cells (discovery cap). The sheet was left unchained so a partial list is not treated as complete."
    ) % {"sheet": sheet_name, "cap": GEOMETRIC_DISCOVERY_CAP}


def _last_cell_ref(args: list[str]) -> str | None:
    if not args:
        return None
    last = args[-1]
    return last if py_code_arg_is_cell_ref(last) else None


def _last_matches(args: list[str], ref: str | None) -> bool:
    if ref is None:
        return False
    last = _last_cell_ref(args)
    return last is not None and same_cell_ref(last, ref)


def _plan_action(
    *,
    desired: str | None,
    data_args: list[str],
    record: GeometricRecord | None,
) -> tuple[Literal["append", "replace", "remove", "noop"], list[str]]:
    if desired is None:
        if record is not None and _last_cell_ref(data_args) is not None:
            return "remove", data_args[:-1]
        return "noop", data_args

    if _last_matches(data_args, desired):
        return "noop", data_args

    if record is not None and _last_matches(data_args, record.predecessor):
        return "replace", data_args[:-1] + [desired]

    return "append", data_args + [desired]


def _code_ref_token(formula: str) -> str | None:
    if not py_formula_has_unquoted_code_ref(formula):
        return None
    parts = parse_python_formula(formula)
    return parts.code if parts is not None else None


def _neighbors(tokens: list[str], live_keys: set[str], sheet_name: str) -> set[str]:
    return {
        live for live in live_keys
        if any(_arg_covers_cell(token, live, sheet_name=sheet_name) for token in tokens)
    }


def _adj_for(
    formula: str,
    args: list[str] | None,
    live_keys: set[str],
    sheet_name: str,
) -> set[str]:
    tokens = list(args or [])
    ref_tok = _code_ref_token(formula)
    if ref_tok:
        tokens.append(ref_tok)
    return _neighbors(tokens, live_keys, sheet_name)


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
    cell_address: str,
) -> tuple[Literal["append", "replace", "remove", "noop", "skip"], list[str]]:
    """Determine action and arguments for a cell, guarding against Err:522 cycles."""
    if desired is not None and cycle:
        # Predecessor already reaches successor → attaching ;desired onto cell is Err:522.
        if record is not None and _last_matches(data_args, record.predecessor):
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
    parsed = _parse_local(address)
    if parsed is None:
        return None
    col, row = parsed
    ncol, nrow = col + dcol, row + drow
    if ncol < 0 or nrow < 0:
        return None
    try:
        return format_address(ncol, nrow)
    except (TypeError, ValueError):
        return None


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
    data_args_by_key: Mapping[str, list[str] | None],
) -> set[str]:
    """Incoming keys a row/col delta will move. Assigned in sheet order."""
    claimed: set[str] = set()
    for cell in cells:
        key = local_a1(cell.address)
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
    if desired is None or not _last_matches(data_args, desired):
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
    def _is_ours(addr: str, args: list[str] | None) -> bool:
        rec = records.get(addr)
        if rec is None or not args:
            return False
        return _last_matches(args, rec.predecessor)

    groups: dict[EvalIndexKey, list[tuple[str, list[str] | None]]] = {}
    for cell in cells:
        args = formula_data_args(cell.formula)
        n_args = 0 if args is None else len(args)
        key = EvalIndexKey(workbook_key, cell.resolved_code, n_args)
        groups.setdefault(key, []).append((cell.address, args))

    safe: set[EvalIndexKey] = set()
    for key, items in groups.items():
        if all(_is_ours(addr, args) for addr, args in items):
            safe.add(key)
    return frozenset(safe)


def compute_sheet_repair(
    cells: list[GeometricCell],
    records: Mapping[str, GeometricRecord] | None = None,
    *,
    sheet_name: str = "",
    truncated: bool = False,
) -> SheetRepairResult:
    """List-diff + splice for one sheet. No UNO.

    *cells* must already be row-major. Cap-hit (*truncated*) skips the whole
    sheet: skipped=True, empty patches, and the incoming records.
    An exact 100 with ``truncated=False`` is chained.
    """
    incoming = {local_a1(k): v for k, v in (records or {}).items()}
    if truncated:
        return SheetRepairResult(
            skipped=True,
            patches=(),
            records=dict(incoming),
            sheet_name=sheet_name,
        )

    working = dict(incoming)
    patches: list[GeometricPatch] = []
    live_keys = {local_a1(cell.address) for cell in cells}
    data_args_by_key = {
        local_a1(cell.address): formula_data_args(cell.formula) for cell in cells
    }
    adj: dict[str, set[str]] = {}
    for cell in cells:
        key = local_a1(cell.address)
        adj[key] = _adj_for(cell.formula, data_args_by_key.get(key), live_keys, sheet_name)

    desired_by_key: dict[str, str | None] = {}
    for i, cell in enumerate(cells):
        desired_by_key[local_a1(cell.address)] = local_a1(cells[i - 1].address) if i > 0 else None
    consumed: set[str] = set()
    evicted: set[str] = set()
    rule2_claimed = _collect_rule2_claimed(cells, incoming, live_keys, desired_by_key, data_args_by_key)

    for cell in cells:
        key = local_a1(cell.address)
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
        adj[key] = _adj_for(cell.formula, new_args, live_keys, sheet_name)
        if action == "remove":
            working.pop(key, None)
        elif desired is not None:
            working[key] = GeometricRecord(predecessor=desired)

    # Drop keys that are no longer on the sheet (moved or deleted).
    working = {addr: rec for addr, rec in working.items() if addr in live_keys}

    return SheetRepairResult(
        skipped=False,
        patches=tuple(patches),
        records=working,
        sheet_name=sheet_name,
    )
