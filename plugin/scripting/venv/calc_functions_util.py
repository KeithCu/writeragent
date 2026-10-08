# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Internal shared utilities for calc_functions formula emulation."""

from __future__ import annotations

import builtins
import calendar
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import functools
import math
import re
from typing import Any, Callable, Sequence

import numpy as np

from .coerce import _LO_ERROR_TOKENS, header_label, is_missing_value

__all__ = [
    "_SERIAL_OFFSET",
    "_bessel_iv_jv",
    "_bessel_kn_yn",
    "_build_holiday_set",
    "_calc_sort_key",
    "_clean_paired_arrays",
    "_collect_a_values",
    "_complex_coeff",
    "_coup_days_in_period",
    "_criteria_numbers",
    "_date_to_serial",
    "_days360",
    "_days_between",
    "_dollar_fraction_terms",
    "_eval_d_criteria",
    "_extract_numeric_array",
    "_find_match_index",
    "_find_text_cut",
    "_fractional_dollar_digits",
    "_from_complex",
    "_get_coupon_dates",
    "_int_bitwise",
    "_int_shift",
    "_is_calc_error",
    "_multi_criteria_mask",
    "_npf_result",
    "_parse_weekend",
    "_round_half_up",
    "_scipy_stats",
    "_serial_to_date",
    "_simple_accrual",
    "_to_complex",
    "_to_float_a",
    "_wildcard_fullmatch",
    "match_criteria",
    "nan_on_error",
]

_SERIAL_OFFSET: int = 693594

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
    """Convert an Excel serial date number or date object to datetime.date (+693594 offset), or None."""
    if isinstance(serial, dt.datetime):
        return serial.date()
    if isinstance(serial, dt.date):
        return serial
    try:
        return dt.date.fromordinal(int(float(serial)) + _SERIAL_OFFSET)
    except (ValueError, TypeError, OverflowError):
        return None


def _date_to_serial(d: dt.date) -> float:
    """Convert a datetime.date object to Excel serial date number (-693594 offset)."""
    return float(d.toordinal() - _SERIAL_OFFSET)


def _days360(sd: dt.date, ed: dt.date, european: bool = False) -> float:
    """Days between two dates using 30/360 rules (NASD US or European)."""
    d1, m1, y1 = sd.day, sd.month, sd.year
    d2, m2, y2 = ed.day, ed.month, ed.year
    if european:
        if d1 == 31:
            d1 = 30
        if d2 == 31:
            d2 = 30
    else:
        if d1 == 31 or (m1 == 2 and d1 == calendar.monthrange(y1, m1)[1]):
            d1 = 30
        if d2 == 31 and d1 == 30:
            d2 = 30
    return float((y2 - y1) * 360 + (m2 - m1) * 30 + (d2 - d1))


def _days_between(d1: Any, d2: Any, basis: int) -> float:
    """Days between two dates under financial basis (0=US 30/360, 1=Act/Act, 2=Act/360, 3=Act/365, 4=Eur 30/360)."""
    # What was wrong: _days_between used crude 30-day month arithmetic instead of true 30/360 rules.
    # How it happened: Hand-rolled (dt2.year-dt1.year)*360+(dt2.month-dt1.month)*30 ignored end-of-month and 31st adjustments.
    # Why this change fixes it: Reuses standard 30/360 date adjustment logic from days360 and exact integer date subtraction.
    sd = _serial_to_date(d1)
    ed = _serial_to_date(d2)
    if sd is None or ed is None:
        return float("nan")
    if basis in (0, 4):
        return _days360(sd, ed, european=(basis == 4))
    return float((ed - sd).days)


def _round_half_up(val: float, decimals: int) -> Decimal:
    """Round a float half-away-from-zero matching Excel/Calc rounding rules."""
    # What was wrong: fixed, dollar, and euroconvert used Python's round() which performs round-half-to-even (banker's rounding).
    # How it happened: standard round(val, decimals) rounds 2.5 to 2 and 2.675 to 2.67.
    # Why this change fixes it: Decimal with ROUND_HALF_UP rounds half-way values away from zero (2.5 -> 3, 2.675 -> 2.68).
    if not math.isfinite(val):
        raise ValueError("Non-finite value cannot be rounded")
    d = Decimal(str(val))
    if decimals >= 0:
        exp = Decimal("1") if decimals == 0 else Decimal("1e" + str(-decimals))
    else:
        exp = Decimal("1e" + str(-decimals))
    return d.quantize(exp, rounding=ROUND_HALF_UP)


def nan_on_error(*exceptions: type[BaseException]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator catching given exceptions (default ValueError, TypeError, OverflowError) and returning NaN."""
    exc_types = exceptions or (ValueError, TypeError, OverflowError)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except exc_types:
                return float("nan")

        return wrapper

    return decorator


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



def _is_calc_error(val: Any) -> bool:
    """Return True if val is a Calc error (NaN or a recognized error token string like #VALUE!)."""
    if isinstance(val, (bool, np.bool_)):
        return False
    if isinstance(val, (float, np.floating)):
        return math.isnan(float(val))
    if isinstance(val, str):
        return val.strip() in _LO_ERROR_TOKENS
    return False


def _calc_sort_key(val: Any) -> tuple[int, Any]:
    """Sort key matching Calc/Excel ordering: numbers < text (case-insensitive) < bools < blanks/errors."""
    if val is None or val == "" or _is_calc_error(val):
        return (3, "")
    if isinstance(val, (bool, np.bool_)):
        return (2, int(val))
    if isinstance(val, (int, float, np.integer, np.floating)):
        try:
            fval = float(val)
            if math.isnan(fval):
                return (3, "")
            return (0, fval)
        except (ValueError, TypeError, OverflowError):
            return (3, "")
    return (1, str(val).casefold())


def _clean_paired_arrays(data1: Any, data2: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """Extract 1D float arrays of matching length, keeping only pairs where both cells are finite numbers."""
    try:
        d1 = np.asarray(data1, dtype=object).ravel()
        d2 = np.asarray(data2, dtype=object).ravel()
        if len(d1) != len(d2) or len(d1) == 0:
            return None
        c1: list[float] = []
        c2: list[float] = []
        for x, y in zip(d1, d2):
            if isinstance(x, (bool, np.bool_)) or isinstance(y, (bool, np.bool_)):
                continue
            try:
                xf = float(x)
                yf = float(y)
                if math.isfinite(xf) and math.isfinite(yf):
                    c1.append(xf)
                    c2.append(yf)
            except (ValueError, TypeError, OverflowError):
                continue
        if not c1:
            return None
        return np.asarray(c1, dtype=float), np.asarray(c2, dtype=float)
    except Exception:
        return None


def _scipy_stats() -> Any | None:
    """Return scipy.stats module if available, else None."""
    try:
        import scipy.stats
        return scipy.stats
    except ImportError:
        return None


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
    # What was wrong: *A functions treated empty cells (None) as 0.0 and coerced mixed ranges to strings.
    # How it happened: _to_float_a mapped missing values to 0.0 instead of skipping them, and np.asarray had no dtype=object.
    # Why this change fixes it: Excel and Calc ignore blank cells in ranges for *A functions (only text counts as 0).
    vals: list[float] = []
    for arg in args:
        for v in np.asarray(arg, dtype=object).ravel():
            if v is None or (isinstance(v, (float, np.floating)) and math.isnan(float(v))):
                continue
            vals.append(_to_float_a(v))
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


def _wildcard_to_regex(pattern: str) -> str:
    """Convert Excel wildcard pattern (*, ?, ~*, ~?, ~~) to a regex pattern string."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "~" and i + 1 < n and pattern[i + 1] in ("*", "?", "~"):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "*":
            out.append(".*")
            i += 1
        elif c == "?":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _wildcard_fullmatch(pattern: str, text: str) -> bool:
    """Excel-style *, ? and ~ wildcard match against the whole text (case-insensitive)."""
    try:
        regex_pat = _wildcard_to_regex(pattern)
        return re.fullmatch(regex_pat, text, flags=re.IGNORECASE) is not None
    except re.error:
        return False


def match_criteria(val: Any, crit: Any) -> bool:
    """Evaluate an Excel/Calc condition (e.g. '>5', '<=10', '<>apple', '*item*', '=')."""
    # What was wrong: match_criteria was case-sensitive, lacked wildcard (*, ?, ~) support,
    # did not match blanks on '=', and accepted invalid operator prefixes like '><'.
    # How it happened: regex r"^([<>=]+)(.*)$" matched invalid operators without an allowlist
    # and fell back to exact string equality without invoking wildcard regex or casefold.
    # Why this change fixes it: validates against an operator allowlist, matches blanks for
    # '=' and '<>', and performs case-insensitive comparisons with Excel wildcard escapes.
    if is_missing_value(crit):
        return is_missing_value(val) or val == "" or val is None

    if isinstance(crit, str):
        m = re.match(r"^(<=|>=|<>|<|>|==|=)(?![<>=])(.*)$", crit)
        if m:
            op = "=" if m.group(1) == "==" else m.group(1)
            val_str = m.group(2)
        else:
            op = "="
            val_str = crit
    else:
        op = "="
        val_str = None

    val_is_blank = val is None or val == "" or is_missing_value(val)
    if val_str == "":
        if op == "=":
            return val_is_blank
        if op == "<>":
            return not val_is_blank

    if val_is_blank:
        return op == "<>"

    val_is_bool = isinstance(val, (bool, np.bool_))
    crit_is_bool = False
    bool_val = False
    if isinstance(crit, (bool, np.bool_)):
        crit_is_bool = True
        bool_val = bool(crit)
    elif val_str is not None and val_str.upper() in ("TRUE", "FALSE"):
        crit_is_bool = True
        bool_val = val_str.upper() == "TRUE"

    if crit_is_bool:
        if val_is_bool:
            v_b = bool(val)
            if op == "=":
                return v_b == bool_val
            if op == "<>":
                return v_b != bool_val
            return False
        return op == "<>"

    if val_is_bool:
        return op == "<>"

    c_num = None
    if val_str is not None:
        try:
            c_num = float(val_str)
        except (ValueError, TypeError, OverflowError):
            c_num = None
    elif isinstance(crit, (int, float, np.integer, np.floating)):
        try:
            c_num = float(crit)
        except (ValueError, TypeError, OverflowError):
            c_num = None

    v_num = None
    if isinstance(val, (int, float, np.integer, np.floating)):
        try:
            v_num = float(val)
            if math.isnan(v_num):
                v_num = None
        except (ValueError, TypeError, OverflowError):
            v_num = None

    if c_num is not None and v_num is not None:
        if op == "=":
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

    v_str = str(val)
    c_pattern = val_str if val_str is not None else str(crit)

    if c_num is not None and v_num is None:
        # What was wrong: match_criteria('5', '5') returned False because c_num was float 5.0 but string val had v_num None.
        # How it happened: early exit returned op == '<>' when c_num was numeric and v_num was None.
        # Why this change fixes it: allows string val to match string-represented number criteria under equality/inequality.
        if isinstance(val, str) and op in ("=", "<>"):
            matched = _wildcard_fullmatch(c_pattern, v_str)
            return matched if op == "=" else not matched
        return op == "<>"

    if c_num is None and v_num is not None:
        return op == "<>"

    if op == "=":
        return _wildcard_fullmatch(c_pattern, v_str)
    if op == "<>":
        return not _wildcard_fullmatch(c_pattern, v_str)

    v_fold = v_str.casefold()
    c_fold = c_pattern.casefold()
    if op == "<":
        return v_fold < c_fold
    if op == "<=":
        return v_fold <= c_fold
    if op == ">":
        return v_fold > c_fold
    if op == ">=":
        return v_fold >= c_fold

    return False


def _find_match_index(
    lookup_val: Any,
    lookup_arr: Any,
    match_mode: int | float = 0,
    search_mode: int | float = 1,
) -> int | None:
    """Find 0-based index matching Excel lookup semantics (exact, smaller, larger, wildcard)."""
    # What was wrong: mixed lookup_arr was coerced to string array by bare np.asarray,
    # corrupting numbers and case matching.
    # How it happened: np.asarray lacked dtype=object, and exact match used identity/case-sensitive equality.
    # Why this change fixes it: preserves object types with dtype=object and matches text case-insensitively.
    try:
        l_flat = np.asarray(lookup_arr, dtype=object).ravel()
        indices = list(range(len(l_flat)))
        if int(float(search_mode)) == -1:
            indices.reverse()
        mm = int(float(match_mode))
    except (TypeError, ValueError, OverflowError):
        return None

    def _is_exact(cell: Any, target: Any) -> bool:
        if isinstance(cell, str) and isinstance(target, str):
            return cell.casefold() == target.casefold()
        if isinstance(cell, (bool, np.bool_)) or isinstance(target, (bool, np.bool_)):
            return bool(cell) is bool(target) if (isinstance(cell, (bool, np.bool_)) and isinstance(target, (bool, np.bool_))) else False
        try:
            return float(cell) == float(target)
        except (ValueError, TypeError, OverflowError):
            return cell == target

    if mm == 0:
        for idx in indices:
            if _is_exact(l_flat[idx], lookup_val):
                return idx
    elif mm in (-1, 1):
        for idx in indices:
            if _is_exact(l_flat[idx], lookup_val):
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
            except (ValueError, TypeError, OverflowError):
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
                if _is_exact(l_flat[idx], lookup_val):
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
            if _is_calc_error(x):
                if propagate_nan:
                    vals.append(float("nan"))
                continue
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


_MAX_BIT_VALUE: int = (1 << 48) - 1


def _int_bitwise(op: Any, n1: Any, n2: Any) -> float:
    """Apply a binary integer bitwise operator to n1 and n2 within [0, 2^48 - 1]."""
    # What was wrong: bitand, bitor, and bitxor accepted negative numbers and numbers >= 2^48.
    # How it happened: inputs were cast to Python ints without non-negative or 48-bit upper limit checks.
    # Why this change fixes it: Excel and Calc require non-negative integers < 2^48 (returning #NUM! / NaN otherwise).
    try:
        v1 = int(float(n1))
        v2 = int(float(n2))
        if v1 < 0 or v1 > _MAX_BIT_VALUE or v2 < 0 or v2 > _MAX_BIT_VALUE:
            return float("nan")
        return float(op(v1, v2))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def _int_shift(number: Any, shift: Any, *, left: bool) -> float:
    """Integer bit shift within [0, 2^48 - 1] and shift magnitude <= 53."""
    # What was wrong: unbounded shift amounts (e.g. 1e9) allocated giant integers causing uncaught MemoryError / hangs.
    # How it happened: Python int << shift executed without checking shift magnitude or catching MemoryError.
    # Why this change fixes it: caps shift magnitude at 53 (Excel limit), validates number in [0, 2^48 - 1],
    # and catches MemoryError.
    try:
        n = int(float(number))
        s = int(float(shift))
        if n < 0 or n > _MAX_BIT_VALUE or abs(s) > 53:
            return float("nan")
        if s < 0:
            return float(n >> abs(s)) if left else float(n << abs(s))
        return float(n << s) if left else float(n >> s)
    except (ValueError, TypeError, OverflowError, MemoryError):
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
    # What was wrong: criteria ranges were stringified by np.asarray, and OverflowError on huge ints escaped.
    # How it happened: np.asarray lacked dtype=object, and except block only caught ValueError/TypeError.
    # Why this change fixes it: dtype=object keeps types intact, and OverflowError is caught.
    r_flat = np.asarray(r, dtype=object).ravel()
    v_flat = np.asarray(val_range, dtype=object).ravel() if val_range is not None else r_flat
    vals: list[float] = []
    for i in range(min(len(r_flat), len(v_flat))):
        if match_criteria(r_flat[i], crit):
            try:
                val = float(v_flat[i])
                if not np.isnan(val):
                    vals.append(val)
            except (ValueError, TypeError, OverflowError):
                pass
    return vals


def _fractional_dollar_digits(fraction: int) -> int:
    """Digits Excel/Calc use when reading a fractional dollar price."""
    if fraction <= 1:
        return 0
    return math.ceil(math.log10(fraction))


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


def _complex_coeff(n: float) -> str:
    """Format complex coefficient without trailing .0 on integers."""
    if math.isfinite(n) and n.is_integer():
        return str(int(n))
    return str(n)


def _from_complex(c: builtins.complex, suffix: str = "i") -> str:
    """Convert Python complex to Calc string."""
    real = c.real
    imag = c.imag
    if imag == 0:
        return _complex_coeff(real)
    if real == 0:
        if imag == 1:
            return suffix
        if imag == -1:
            return "-" + suffix
        return _complex_coeff(imag) + suffix
    res = _complex_coeff(real)
    if imag > 0:
        res += "+"
    if imag == 1:
        res += suffix
    elif imag == -1:
        res += "-" + suffix
    else:
        res += _complex_coeff(imag) + suffix
    return res


def _to_complex(val: Any) -> builtins.complex:
    """Convert Calc complex string (e.g. '1+2i') to Python complex."""
    if isinstance(val, (int, float, builtins.complex)):
        return builtins.complex(val)
    s = str(val).replace("i", "j").replace("I", "j").replace(" ", "")
    try:
        return builtins.complex(s)
    except ValueError:
        raise TypeError("Invalid complex string")


def _eval_d_criteria(db: Any, field: Any, criteria: Any, as_float: bool = True) -> list[Any] | None:
    """Shared helper for D* functions (DSUM, DAVERAGE, DMAX, DMIN, etc.)."""
    db_arr = np.asarray(db, dtype=object)
    if db_arr.ndim != 2:
        return None
    headers = [header_label(h).upper() for h in db_arr[0]]

    f_idx = -1
    if isinstance(field, str) or isinstance(field, bool):
        f_name = header_label(field).upper()
        if f_name in headers:
            f_idx = headers.index(f_name)
    elif isinstance(field, (int, float)):
        try:
            f_idx = int(field) - 1
        except (ValueError, TypeError, OverflowError):
            f_idx = -1
    elif field is not None and field != "":
        f_name = header_label(field).upper()
        if f_name in headers:
            f_idx = headers.index(f_name)
        else:
            try:
                f_idx = int(float(field)) - 1
            except (ValueError, TypeError, OverflowError):
                f_idx = -1

    if f_idx < 0 or f_idx >= db_arr.shape[1]:
        return None

    crit_arr = np.asarray(criteria, dtype=object)
    if crit_arr.ndim != 2:
        return None
    crit_headers = [header_label(h).upper() for h in crit_arr[0]]

    matching_vals = []
    for r_idx in range(1, db_arr.shape[0]):
        row = db_arr[r_idx]
        match_any_row = False
        for c_row_idx in range(1, crit_arr.shape[0]):
            match_all_cols = True
            for c_col_idx in range(crit_arr.shape[1]):
                c_header = crit_headers[c_col_idx]
                c_val = crit_arr[c_row_idx, c_col_idx]
                if is_missing_value(c_val):
                    continue

                if c_header in headers:
                    db_col_idx = headers.index(c_header)
                    if not match_criteria(row[db_col_idx], c_val):
                        match_all_cols = False
                        break
            if match_all_cols:
                match_any_row = True
                break

        if match_any_row:
            val = row[f_idx]
            if as_float:
                try:
                    matching_vals.append(float(val))
                except (ValueError, TypeError, OverflowError):
                    pass
            else:
                matching_vals.append(val)
    return matching_vals


def _multi_criteria_mask(pairs: Sequence[tuple[Any, Any]], base_len: int | None = None) -> np.ndarray | None:
    """Evaluate multiple (criteria_range, criterion) pairs, ensuring all ranges have matching lengths.

    Returns a 1D boolean numpy array mask, or None if lengths mismatch or pairs are empty.
    """
    # What was wrong: countifs and averageifs truncated mismatched range lengths to the shortest range.
    # How it happened: zip() and min(len...) silently truncated ranges instead of validating equal dimensions.
    # Why this change fixes it: validates that all criteria ranges match base_len (and each other) exactly,
    # returning None so callers can return #VALUE! per Excel and Calc semantics.
    if not pairs:
        return np.ones(base_len, dtype=bool) if base_len is not None else np.array([], dtype=bool)

    cond_ranges: list[np.ndarray] = []
    criteria: list[Any] = []
    for r, c in pairs:
        r_flat = np.asarray(r, dtype=object).ravel()
        cond_ranges.append(r_flat)
        criteria.append(c)

    expected_len = base_len if base_len is not None else len(cond_ranges[0])
    for cr in cond_ranges:
        if len(cr) != expected_len:
            return None

    mask = np.ones(expected_len, dtype=bool)
    for cr, crit in zip(cond_ranges, criteria):
        for idx in range(expected_len):
            if mask[idx] and not match_criteria(cr[idx], crit):
                mask[idx] = False

    return mask


def _get_coupon_dates(
    settlement: Any, maturity: Any, frequency: Any, basis: Any = 0
) -> tuple[float, float, float, float]:
    """Calculate previous and next coupon dates stepping backwards from maturity in calendar months.

    Returns (prev_serial, curr_serial, days_in_period, num_coupons).
    """
    # What was wrong: Coupon calculations stepped back in fixed day counts (180, 182.5) instead of calendar months,
    # accepted invalid frequencies (e.g. 1e9 which caused an infinite loop), and accepted settlement >= maturity.
    # How it happened: _get_coupon_dates used fixed day division and had no check that freq in (1, 2, 4) or settlement < maturity.
    # Why this change fixes it: Validates freq in (1, 2, 4), basis in (0..4), settlement < maturity, and steps backwards
    # by calendar months from maturity, matching Excel and LibreOffice ScInterpreter coupon scheduling.
    freq = int(float(frequency))
    b = int(float(basis))
    if freq not in (1, 2, 4) or b not in (0, 1, 2, 3, 4):
        raise ValueError("Invalid coupon frequency or basis")
    s_date = _serial_to_date(settlement)
    m_date = _serial_to_date(maturity)
    if s_date is None or m_date is None or s_date >= m_date:
        raise ValueError("Invalid settlement or maturity date")

    m_step = 12 // freq
    d_mat = m_date.day
    mat_is_eom = d_mat == calendar.monthrange(m_date.year, m_date.month)[1]
    mat_month_idx = m_date.year * 12 + m_date.month - 1
    k = 0
    curr = m_date
    prev = m_date
    while curr > s_date:
        k += 1
        m_idx = mat_month_idx - k * m_step
        y = m_idx // 12
        m = (m_idx % 12) + 1
        dim = calendar.monthrange(y, m)[1]
        d = dim if (mat_is_eom or d_mat > dim) else d_mat
        prev = dt.date(y, m, d)
        if prev <= s_date:
            break
        curr = prev

    p_ser = _date_to_serial(prev)
    c_ser = _date_to_serial(curr)
    if b in (0, 2, 4):
        days_in_per = 360.0 / freq
    elif b == 3:
        days_in_per = 365.0 / freq
    else:
        days_in_per = float((curr - prev).days)

    return p_ser, c_ser, days_in_per, float(k)


def _coup_days_in_period(frequency: Any, basis: Any = 0) -> float:
    """Days in coupon period for given frequency and basis."""
    try:
        freq = int(float(frequency))
        b = int(float(basis))
        if freq not in (1, 2, 4) or b not in (0, 1, 2, 3, 4):
            return float("nan")
        if b in (0, 2, 4):
            return 360.0 / freq
        if b == 3:
            return 365.0 / freq
        return 365.25 / freq
    except (ValueError, TypeError, OverflowError):
        return float("nan")


