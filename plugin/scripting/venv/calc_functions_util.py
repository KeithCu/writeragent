# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Internal shared utilities for calc_functions formula emulation."""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

import numpy as np

from .coerce import is_missing_value

__all__ = [
    "_bessel_iv_jv",
    "_bessel_kn_yn",
    "_build_holiday_set",
    "_collect_a_values",
    "_criteria_numbers",
    "_dollar_fraction_terms",
    "_extract_numeric_array",
    "_find_match_index",
    "_find_text_cut",
    "_fractional_dollar_digits",
    "_int_bitwise",
    "_int_shift",
    "_npf_result",
    "_parse_weekend",
    "_serial_to_date",
    "_simple_accrual",
    "_to_float_a",
    "_wildcard_fullmatch",
    "match_criteria",
]

_WEEKEND_MAPPING: dict[int, tuple[int, ...]] = {
    1: (5, 6),
    2: (6, 0),
    3: (0, 1),
    4: (1, 2),
    5: (2, 3),
    6: (3, 4),
    7: (4, 5),
    11: (6,),
    12: (0,),
    13: (1,),
    14: (2,),
    15: (3,),
    16: (4,),
    17: (5,),
}


def _serial_to_date(serial: Any) -> dt.date | None:
    """Convert an Excel serial date number to datetime.date (+693594 offset), or None."""
    try:
        return dt.date.fromordinal(int(float(serial)) + 693594)
    except (ValueError, TypeError, OverflowError):
        return None


def _build_holiday_set(holidays: Any | None = None) -> set[dt.date]:
    """Build a set of datetime.date from a holiday argument (scalar or array)."""
    h_dates: set[dt.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                d = _serial_to_date(h)
                if d is not None:
                    h_dates.add(d)
    return h_dates


def _parse_weekend(weekend: Any = 1) -> set[int] | float:
    """Parse Excel weekend parameter into a set of weekday integers, or NaN if invalid."""
    if isinstance(weekend, str):
        wk_days: set[int] = set()
        for i, char in enumerate(weekend[:7]):
            if char == "1":
                wk_days.add(i)
        return wk_days
    try:
        w_idx = int(float(weekend))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # Excel weekend codes. An unknown code returns NaN (Excel returns #NUM!).
    if w_idx not in _WEEKEND_MAPPING:
        return float("nan")
    return set(_WEEKEND_MAPPING[w_idx])



def _to_float_a(val: Any) -> float:
    """Helper for *A functions (AVERAGEA, STDEVA, etc.)."""
    if is_missing_value(val):
        return 0.0
    if isinstance(val, (bool, np.bool_)):
        return 1.0 if val else 0.0
    try:
        return float(val)
    # A Python int bigger than the float range raises OverflowError, which
    # the (ValueError, TypeError) handler missed, so AVERAGEA-style callers
    # crashed instead of treating the cell as 0 the way other non-numbers do.
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _collect_a_values(*args: Any) -> np.ndarray:
    """Collect flat float array for *A functions (AVERAGEA, MAXA, etc.)."""
    vals = [_to_float_a(v) for arg in args for v in np.asarray(arg).ravel()]
    return np.asarray(vals, dtype=float)




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
            except (ValueError, TypeError, OverflowError):
                c_num = None
            try:
                v_num = float(val)
            # float() of an int past the float range is OverflowError, not
            # ValueError. That used to escape and crash COUNTIF/SUMIF.
            except (ValueError, TypeError, OverflowError):
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
    # Same OverflowError hole as the operator branch above: a huge int is
    # not a float, so fall through to the string compare.
    except (ValueError, TypeError, OverflowError):
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


def _find_text_cut(
    text: Any,
    delimiter: Any,
    instance_num: Any = 1,
    match_mode: Any = 0,
    after: bool = False,
) -> tuple[str, str, int, int | None, bool]:
    """Shared delimiter search for textbefore and textafter.

    Returns (text_str, delim_str, instance_int, cut_idx, match_end_miss).
    When the instance is within bounds, cut_idx is an integer index and match_end_miss is False.
    When the instance lands on the boundary, cut_idx is None and match_end_miss is True.
    On a real miss, cut_idx is None and match_end_miss is False.
    Raises ValueError or TypeError if instance_num is invalid or zero.
    """
    s = str(text)
    delim = str(delimiter)
    inst = int(float(instance_num))
    if inst == 0:
        raise ValueError("instance_num cannot be 0")

    if match_mode == 1:
        s_search = s.lower()
        delim_search = delim.lower()
    else:
        s_search = s
        delim_search = delim

    abs_inst = abs(inst)
    parts = s_search.split(delim_search)
    if len(parts) <= abs_inst:
        match_end_miss = len(parts) == abs_inst
        return s, delim, inst, None, match_end_miss

    if inst > 0:
        idx = 0
        if after:
            for _unused in range(inst):
                idx = s_search.find(delim_search, idx) + len(delim_search)
        else:
            for i in range(inst):
                idx = s_search.find(delim_search, idx)
                if i < inst - 1:
                    idx += len(delim_search)
        return s, delim, inst, idx, False
    else:
        idx = len(s)
        for _unused in range(abs_inst):
            idx = s_search.rfind(delim_search, 0, idx)
        return s, delim, inst, idx, False


def _int_bitwise(op: Any, n1: Any, n2: Any) -> float:
    """Apply a binary integer bitwise operator to n1 and n2."""
    try:
        return float(op(int(float(n1)), int(float(n2))))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def _int_shift(number: Any, shift: Any, *, left: bool) -> float:
    """Integer bit shift. A negative shift swaps direction (Calc/ODFF semantics)."""
    try:
        n = int(float(number))
        s = int(float(shift))
        if s < 0:
            return float(n >> abs(s)) if left else float(n << abs(s))
        return float(n << s) if left else float(n >> s)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def _bessel_iv_jv(scipy_fn: Any, x: Any, n: Any) -> float:
    """Evaluate modified/regular Bessel function of the first kind (iv or jv)."""
    try:
        v1 = float(x)
        v2 = int(float(n))
        if v2 < 0:
            return float("nan")
        return float(scipy_fn(v2, v1))
    except Exception:
        return float("nan")


def _bessel_kn_yn(scipy_fn: Any, x: Any, n: Any) -> float:
    """Evaluate modified/regular Bessel function of the second kind (kn or yn)."""
    try:
        xv = float(x)
        nv = int(float(n))
        if xv <= 0:
            return float("nan")
        return float(scipy_fn(nv, xv))
    except Exception:
        return float("nan")


def _simple_accrual(
    issue: Any, settlement: Any, rate: Any, par: Any, basis: Any = 0
) -> float:
    """Simple accrual interest calculation for accrint and accrintm."""
    from .calc_functions_t_z import yearfrac

    try:
        r = float(rate)
        p = float(par)
        yf = yearfrac(issue, settlement, basis)
        if math.isnan(yf):
            return float("nan")
        return float(p * r * yf)
    except Exception:
        return float("nan")


def _criteria_numbers(r: Any, crit: Any, val_range: Any | None = None) -> list[float]:
    """Collect matching numeric values for criteria-based functions (averageif, sumif)."""
    r_flat = np.asarray(r).ravel()
    v_flat = np.asarray(val_range).ravel() if val_range is not None else r_flat
    vals: list[float] = []
    for i in range(min(len(r_flat), len(v_flat))):
        if match_criteria(r_flat[i], crit):
            try:
                val = float(v_flat[i])
                if not np.isnan(val):
                    vals.append(val)
            except (ValueError, TypeError):
                pass
    return vals


def _fractional_dollar_digits(fraction: int) -> int:
    """Digits Excel/Calc use when reading a fractional dollar price."""
    if fraction <= 1:
        return 0
    digits = math.ceil(math.log10(fraction))
    if 10 ** (digits - 1) == fraction:
        digits -= 1
    return digits


def _dollar_fraction_terms(
    amount: Any, fraction: Any
) -> tuple[float, float, float, int, int] | None:
    """Parse and compute terms for dollarde and dollarfr: (sign, i_part, f_part, f, scale)."""
    try:
        amt = float(amount)
        frac = float(fraction)
        f = int(frac)
    # int(inf) and float(10**400) raise OverflowError, not ValueError.
    # math.floor(inf) does too, and that call sat outside this try, so
    # dollarde(1.02, inf) and dollarfr(inf, 4) crashed the formula.
    except (ValueError, TypeError, OverflowError):
        return None
    if f <= 0 or not math.isfinite(amt) or not math.isfinite(frac):
        return None
    sign = -1.0 if amt < 0 else 1.0
    amt = abs(amt)
    i_part = math.floor(amt)
    f_part = amt - i_part
    scale = 10 ** _fractional_dollar_digits(f)
    return sign, float(i_part), f_part, f, scale

