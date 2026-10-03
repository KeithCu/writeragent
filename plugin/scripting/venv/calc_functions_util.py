# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Internal shared utilities for calc_functions formula emulation."""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np

from .coerce import is_missing_value

__all__ = [
    "_extract_numeric_array",
    "_find_match_index",
    "_npf_result",
    "_wildcard_fullmatch",
    "match_criteria",
]


def _npf_result(kind: str, *args: Any) -> float:
    """One numpy-financial scalar, or NaN where Calc/Excel are #NUM! / #DIV/0!.

    The library returns ±inf for a zero period count and for an NPER that
    never amortizes, and raises when ``when`` is not 0 or 1. Callers pass
    0 or 1. A missing install is the same NaN as a missing scipy helper.
    """
    try:
        import numpy_financial as npf  # type: ignore[import-untyped]
    except ImportError:
        return float("nan")
    try:
        # np.where in pmt/pv evaluates the zero-rate branch and warns on
        # divide-by-zero even when the other branch is the result.
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            result = float(np.sum(getattr(npf, kind)(*args)))
    except (AttributeError, OverflowError, ValueError, ZeroDivisionError, TypeError):
        return float("nan")
    if not math.isfinite(result):
        return float("nan")
    return result


def _wildcard_fullmatch(pattern: str, text: str) -> bool:
    """Excel-style * and ? wildcard match against the whole text."""
    escaped = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(escaped, text) is not None


def match_criteria(val: Any, crit: Any) -> bool:
    """Evaluate an Excel/Calc condition (e.g. '>5', '<=10', '<>apple', '*item*')."""
    if is_missing_value(crit):
        return is_missing_value(val)
    if isinstance(crit, str):
        m = re.match(r"^([<>=]+)(.*)$", crit)
        if m:
            op, val_str = m.groups()
            try:
                c_num = float(val_str)
            except (ValueError, TypeError):
                c_num = None
            try:
                v_num = float(val)
            except (ValueError, TypeError):
                v_num = None
            # A numeric criterion used to fall through to lexicographic
            # compare whenever the cell failed float(). "abc" > "5" is True,
            # so COUNTIF(["abc"], ">5") counted the text. Excel/Calc compare
            # numbers only; <> still matches because the text is not the number.
            if c_num is not None and v_num is None:
                if op == "<>":
                    return True
                if op in ("=", "==", "<", "<=", ">", ">="):
                    return False
            elif c_num is not None and v_num is not None:
                if op in ("=", "=="):
                    return v_num == c_num
                if op == "<>":
                    return v_num != c_num
                if op == "<":
                    return v_num < c_num
                if op == "<=":
                    return v_num <= c_num
                if op == ">":
                    return v_num > c_num
                if op == ">=":
                    return v_num >= c_num
            else:
                c_str = val_str
                v_str = str(val)
                if op in ("=", "=="):
                    return v_str == c_str
                if op == "<>":
                    return v_str != c_str
                if op == "<":
                    return v_str < c_str
                if op == "<=":
                    return v_str <= c_str
                if op == ">":
                    return v_str > c_str
                if op == ">=":
                    return v_str >= c_str
    try:
        if float(val) == float(crit):
            return True
    except (ValueError, TypeError):
        pass
    return str(val) == str(crit)


def _find_match_index(
    lookup_val: Any,
    lookup_arr: Any,
    match_mode: int | float = 0,
    search_mode: int | float = 1,
) -> int | None:
    """Find 0-based index matching Excel lookup semantics (exact, smaller, larger, wildcard)."""
    try:
        l_flat = np.asarray(lookup_arr).ravel()
        indices = list(range(len(l_flat)))
        if int(float(search_mode)) == -1:
            indices.reverse()
        mm = int(float(match_mode))
    except (TypeError, ValueError, OverflowError):
        return None

    if mm == 0:
        for idx in indices:
            if l_flat[idx] == lookup_val:
                return idx
    elif mm in (-1, 1):
        for idx in indices:
            if l_flat[idx] == lookup_val:
                return idx
        best_idx = None
        for idx in indices:
            try:
                diff = float(l_flat[idx]) - float(lookup_val)
                if mm == -1 and diff < 0:
                    if best_idx is None or diff > float(l_flat[best_idx]) - float(lookup_val):
                        best_idx = idx
                elif mm == 1 and diff > 0:
                    if best_idx is None or diff < float(l_flat[best_idx]) - float(lookup_val):
                        best_idx = idx
            except (ValueError, TypeError):
                pass
        return best_idx
    elif mm == 2:
        if isinstance(lookup_val, str):
            for idx in indices:
                cell = l_flat[idx]
                if isinstance(cell, str) and _wildcard_fullmatch(lookup_val, cell):
                    return idx
        else:
            for idx in indices:
                if l_flat[idx] == lookup_val:
                    return idx
    return None


def _extract_numeric_array(
    *args: Any,
    ignore_text: bool = True,
    ignore_bool: bool = True,
    propagate_nan: bool = True,
) -> np.ndarray:
    """Collect flat float array from arguments, filtering out non-numeric cells per Excel conventions."""
    vals: list[float] = []
    for arg in args:
        for x in np.asarray(arg, dtype=object).ravel():
            if ignore_bool and isinstance(x, (bool, np.bool_)):
                continue
            if ignore_text and isinstance(x, str):
                continue
            try:
                v = float(x)
            except (ValueError, TypeError, OverflowError):
                continue
            if not propagate_nan and math.isnan(v):
                continue
            vals.append(v)
    return np.asarray(vals, dtype=float)
