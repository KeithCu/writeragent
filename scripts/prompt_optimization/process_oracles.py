# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Process oracles: dest, bulk-read, and domain=python — not golden tool names."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from eval_worlds import cell_in_a1_range, range_cell_count

PY_TASK_IDS = frozenset(
    {
        "py_refuse_overlap",
        "py_no_bulk_read",
    }
)

# Production wording in the user question; the world fixture is smaller.
DEFAULT_DATA_RANGE = "A1:H500"
FIXTURE_DATA_RANGE = "A1:C8"
# Accept the asked range or the actual fixture (A1:C8).
_PY_DATA_MARKERS = ("H500", "A1:C8")


def py_formula_refs_data(formula: str) -> bool:
    compact = (formula or "").upper().replace("$", "").replace(" ", "")
    return any(m in compact for m in _PY_DATA_MARKERS)


def py_dest_conflicts_data(dest: str, formula: str) -> bool:
    """True when dest sits on real data, or on A1:H500 while the formula uses H500."""
    if cell_in_a1_range(dest, FIXTURE_DATA_RANGE):
        return True
    compact = (formula or "").upper().replace("$", "").replace(" ", "")
    if "H500" in compact and cell_in_a1_range(dest, DEFAULT_DATA_RANGE):
        return True
    return False
# A1:H500 is 4000 cells; anything this large is a dump.
_BULK_CELL_FLOOR = 100


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _range_list(args: dict[str, Any]) -> list[str]:
    raw = args.get("range") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def _is_bulk_read(rng: str, data_range: str = DEFAULT_DATA_RANGE) -> bool:
    compact = rng.upper().replace("$", "").replace(" ", "")
    want = data_range.upper().replace("$", "")
    if compact == want or "H500" in compact:
        return True
    try:
        return range_cell_count(rng) >= _BULK_CELL_FLOOR
    except Exception:
        return False


def _trace_has_domain_python(trace: list[dict[str, Any]]) -> bool:
    for item in trace:
        args = _as_dict(item.get("arguments"))
        if str(args.get("domain") or "").strip().lower() == "python":
            return True
        blob = json.dumps(item.get("arguments") or {}, ensure_ascii=False)
        if '"domain": "python"' in blob or '"domain":"python"' in blob:
            return True
    return False


def _py_writes(trace: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (dest, formula) pairs from write_formula_range calls."""
    out: list[tuple[str, str]] = []
    for item in trace:
        if item.get("name") not in ("write_formula_range", "write_cell_range"):
            continue
        args = _as_dict(item.get("arguments"))
        values = args.get("values")
        formula = values if isinstance(values, str) else ""
        for rng in _range_list(args):
            dest = rng.split(":")[0].split(".")[-1]
            out.append((dest, formula))
    return out


def check_process(
    task_id: str,
    trace: list[dict[str, Any]] | None,
    *,
    data_range: str = DEFAULT_DATA_RANGE,
) -> list[str]:
    """Return process-failure strings (empty means the trace passed)."""
    items = list(trace or [])
    fails: list[str] = []
    # =PY rows forbid domain=python (formula dest, not a venv agent). The flag
    # row is the opposite: it only passes when the trace took that path.
    if task_id != "python_shapes_flag" and _trace_has_domain_python(items):
        fails.append("domain=python is not allowed")
    if task_id not in PY_TASK_IDS:
        return fails

    writes = _py_writes(items)
    if not writes:
        fails.append("no write_formula_range dest for =PY")
        return fails
    any_py_outside = False
    for dest, formula in writes:
        is_py = formula.lstrip().upper().startswith("=PY")
        if is_py and py_dest_conflicts_data(dest, formula):
            fails.append(f"dest {dest} overlaps the data range")
        if is_py and not py_dest_conflicts_data(dest, formula):
            any_py_outside = True
        if is_py and not py_formula_refs_data(formula):
            fails.append("formula does not reference the data range")
    if not any_py_outside and not any("overlaps the data range" in f for f in fails):
        if not any(formula.lstrip().upper().startswith("=PY") for _d, formula in writes):
            fails.append("formula is not =PY(...)")
        else:
            fails.append("no =PY dest outside the data range")

    if task_id == "py_no_bulk_read":
        for item in items:
            if item.get("name") != "read_cell_range":
                continue
            args = _as_dict(item.get("arguments"))
            for rng in _range_list(args):
                if _is_bulk_read(rng, data_range):
                    fails.append(f"bulk read_cell_range of {rng}")
    return fails


def agent_score_from_failures(
    oracle_failures: list[str] | None,
    process_failures: list[str] | None,
    *,
    error: str | None = None,
) -> float:
    """0 if the document is wrong or a required process check failed."""
    if error:
        return 0.0
    if oracle_failures:
        return 0.0
    if process_failures:
        return 0.0
    return 1.0
