# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Internal shared utilities for calc_functions formula emulation."""

from __future__ import annotations

import builtins
import calendar
import datetime as dt
from decimal import Decimal, InvalidOperation, Overflow, ROUND_HALF_UP, localcontext
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
    "_parse_complex",
    "_parse_weekend",
    "_round_half_up",
    "_scipy_stats",
    "_serial_to_date",
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
    # 30/360 day count, including the 31st and end-of-month adjustments from DAYS360.
    sd = _serial_to_date(d1)
    ed = _serial_to_date(d2)
    if sd is None or ed is None:
        return float("nan")
    if basis in (0, 4):
        return _days360(sd, ed, european=(basis == 4))
    return float((ed - sd).days)


def _round_half_up(val: float, decimals: int) -> Decimal:
    """Round a float half-away-from-zero matching Excel/Calc rounding rules."""
    # Half away from zero (2.5 -> 3, 2.675 -> 2.68). Python round() is half-to-even.
    if not math.isfinite(val):
        raise ValueError("Non-finite value cannot be rounded")
    # What was wrong: DOLLAR, FIXED, and EUROCONVERT raised decimal.InvalidOperation
    # for a large place count (fixed(0.5, 40), dollar(1.5, 40)).
    # How it happened: quantize used the default 28-digit context. InvalidOperation
    # subclasses ArithmeticError, not ValueError or OverflowError, so those handlers
    # missed it. TEXT already widens the context in _format_number_pattern.
    # Why this change fixes it: the same precision bump lets a 40-place quantize
    # succeed, and a quantize that still fails becomes ValueError so the handlers
    # return NaN instead of escaping the formula.
    d = Decimal(str(val))
    try:
        if decimals >= 0:
            exp = Decimal("1") if decimals == 0 else Decimal("1e" + str(-decimals))
        else:
            exp = Decimal("1e" + str(-decimals))
        with localcontext() as ctx:
            ctx.prec = max(28, abs(d.adjusted()) + abs(decimals) + 2)
            return d.quantize(exp, rounding=ROUND_HALF_UP)
    except (InvalidOperation, Overflow):
        raise ValueError("decimal quantize failed") from None


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
    """Parse a Calc weekend argument into weekday integers, or NaN if invalid.

    GetWeekendAndHolidayMasks_MS (interpr2.cxx) accepts a string only when it
    is exactly seven characters of ``0`` or ``1``. ``"abc"`` and ``"1"`` used
    to become a mask (empty, or Monday) because any other character was
    ignored and the string was truncated to seven. The string is Monday-first,
    matching the masks workday tests already build. Numeric codes 1–7 and
    11–17 stay in ``_WEEKEND_MAPPING``.
    """
    if isinstance(weekend, str):
        if len(weekend) != 7 or any(ch not in "01" for ch in weekend):
            return float("nan")
        return {i for i, ch in enumerate(weekend) if ch == "1"}
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
    # *A functions skip blanks. Only text counts as 0. A blank is not a zero
    # and must not change the count.
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
    # Operators come from an allowlist ('><' is not one). Text is
    # case-insensitive, '*' '?' '~' are Excel wildcards, and a blank matches '=' and '<>'.
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
        # The criterion "5" is numeric, but the cell "5" is text. Equality
        # and inequality still compare them; other operators do not.
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
    # dtype=object keeps numbers as numbers. Text matches case-insensitively.
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
    When the instance is within bounds, cut_idx is an index into the original
    text and match_end_miss is False. ``after=False`` is the start of that
    delimiter. ``after=True`` is the end, where the remainder starts.
    When the instance lands on the boundary, cut_idx is None and match_end_miss is True.
    On a real miss, cut_idx is None and match_end_miss is False.
    Raises ValueError or TypeError if instance_num is invalid or zero.
    """
    s = str(text)
    delim = str(delimiter)
    inst = int(float(instance_num))
    if inst == 0:
        raise ValueError("instance_num cannot be 0")
    # "".split("") raises. re.finditer("") matches at every position, so a
    # blank delimiter would look like a hit. TEXTBEFORE / TEXTAFTER turn
    # this ValueError into NaN.
    if not delim:
        raise ValueError("empty separator")

    # ScTextBeforeOrAfter (interpr1.cxx) walks left to right and steps
    # past each delimiter. lower() of İ is i + combining dot, so an index
    # into the lowered string no longer lines up with s: textbefore of
    # "aİb" on "b" included the b. rfind also sees overlapping matches
    # ("aaa" / "aa" at 1) that the split count above it did not.
    if match_mode == 1:
        spans = [(m.start(), m.end()) for m in re.finditer(re.escape(delim), s, flags=re.IGNORECASE)]
    else:
        spans = []
        start = 0
        while True:
            found = s.find(delim, start)
            if found < 0:
                break
            spans.append((found, found + len(delim)))
            start = found + len(delim)

    n_matches = len(spans)
    abs_inst = abs(inst)
    # One past the last real match is the match_end boundary.
    if abs_inst > n_matches:
        return s, delim, inst, None, abs_inst == n_matches + 1

    # A negative instance counts from the end of that same list.
    chosen = spans[inst - 1] if inst > 0 else spans[inst]
    # after=True used to return the start for a negative instance and let
    # TEXTAFTER add len(delim). A case-folded match can be a different
    # length than the delimiter, so the cut is the span's own end.
    idx = chosen[1] if after else chosen[0]
    return s, delim, inst, idx, False


_MAX_BIT_VALUE: int = (1 << 48) - 1


def _int_bitwise(op: Any, n1: Any, n2: Any) -> float:
    """Apply a binary integer bitwise operator to n1 and n2 within [0, 2^48 - 1]."""
    # Non-negative integers below 2^48. Excel and Calc return #NUM! otherwise.
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
    # Shift at most 53 bits (Excel) and the number stays in [0, 2^48 - 1].
    # A shift of 1e9 builds an integer large enough to hang or raise MemoryError.
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
        # What was wrong: BESSELK(1, -1) returned kn(1, 1) and BESSELY(1, -1)
        # returned a finite value. Both are illegal orders.
        # How it happened: _bessel_iv_jv rejects nv < 0, but this helper only
        # rejected xv <= 0. scipy.special.kn(-n, x) equals kn(n, x), so the
        # missing check looked like a successful order-1 call.
        # Why this change fixes it: AnalysisAddIn::getBesselk and getBessely
        # throw IllegalArgumentException when nOrder < 0 or fNum <= 0. NaN is
        # that error in this runtime.
        if nv < 0 or xv <= 0:
            return float("nan")
        return float(scipy_fn(nv, xv))
    except Exception:
        return float("nan")


def _criteria_numbers(r: Any, crit: Any, val_range: Any | None = None) -> list[float]:
    """Collect matching numeric values for criteria-based functions (averageif, sumif)."""
    # dtype=object keeps cell types. A huge int raises OverflowError, which is
    # the same failure as a bad number. Text and bools are not numbers:
    # float("5") and float(True) used to count them. Calc AVERAGEIF and
    # SUMIF ignore both in the value range.
    r_flat = np.asarray(r, dtype=object).ravel()
    v_flat = np.asarray(val_range, dtype=object).ravel() if val_range is not None else r_flat
    vals: list[float] = []
    for i in range(min(len(r_flat), len(v_flat))):
        if match_criteria(r_flat[i], crit):
            cell = v_flat[i]
            if isinstance(cell, (bool, np.bool_, str)):
                continue
            try:
                val = float(cell)
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
    """Convert Python complex to Calc string.

    ``Complex::GetString`` (``analysishelper.cxx``) calls ``finiteOrThrow``
    on both parts. A non-finite value used to be printed (``"inf"``,
    ``"nannani"``). macOS libm returns a non-finite complex for
    ``(1+1j)**inf`` and does not raise. Linux raises ``OverflowError``.
    Callers map this ``ValueError`` to ``#VALUE!``.
    """
    if not (math.isfinite(c.real) and math.isfinite(c.imag)):
        raise ValueError("non-finite complex")
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


def _parse_complex(val: Any) -> tuple[builtins.complex, str]:
    """Parse a Calc complex into ``(number, suffix)``.

    ``suffix`` is ``"i"``, ``"j"``, or ``""`` when the text has no imaginary
    unit. ``Complex::Add`` / ``Mult`` (``analysishelper.hxx``) keep the first
    non-empty unit: a real number stores ``c == '\\0'`` and does not override
    a later ``j``. Rewriting every ``i`` to ``j`` before parsing used to drop
    that unit, so ``IMSUM("1+2j","3+j")`` came back as ``"4+3i"``.

    A bool is a number. It is an ``int`` subclass, and ``str(True)`` is not
    a complex literal.
    """
    if isinstance(val, (bool, np.bool_)):
        return builtins.complex(float(val)), ""
    if isinstance(val, (int, float, builtins.complex)):
        return builtins.complex(val), ""
    text = str(val).replace(" ", "")
    suffix = ""
    if text and text[-1] in "ijIJ":
        suffix = "j" if text[-1] in "jJ" else "i"
    py_text = text.replace("i", "j").replace("I", "j").replace("J", "j")
    try:
        return builtins.complex(py_text), suffix
    except ValueError:
        raise TypeError("Invalid complex string")


def _to_complex(val: Any) -> builtins.complex:
    """Convert Calc complex string (e.g. '1+2i') to Python complex."""
    number, _suffix = _parse_complex(val)
    return number


def _eval_d_criteria(db: Any, field: Any, criteria: Any, as_float: bool = True) -> list[Any] | None:
    """Shared helper for D* functions (DSUM, DAVERAGE, DMAX, DMIN, etc.)."""
    db_arr = np.asarray(db, dtype=object)
    if db_arr.ndim != 2:
        return None
    headers = [header_label(h).upper() for h in db_arr[0]]
    # First duplicate header wins, same as findFieldColumn (doubleref.cxx)
    # walking the header row left to right.
    header_index: dict[str, int] = {}
    for idx, name in enumerate(headers):
        header_index.setdefault(name, idx)

    f_idx = -1
    if isinstance(field, str) or isinstance(field, bool):
        # bool is an int subclass. int(True) - 1 would select column 0 when
        # the label "TRUE" is missing. A bool field is the header text.
        f_name = header_label(field).upper()
        if f_name in header_index:
            f_idx = header_index[f_name]
    elif isinstance(field, (int, float)):
        try:
            f_idx = int(field) - 1
        except (ValueError, TypeError, OverflowError):
            f_idx = -1
    elif field is not None and field != "":
        f_name = header_label(field).upper()
        if f_name in header_index:
            f_idx = header_index[f_name]
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

    # lcl_createExcelQuery (doubleref.cxx) requires every criteria header
    # to name a database column. A miss makes createQueryParam return
    # nullptr, and DBIterator sets IllegalParameter. Skipping the column
    # used to match every row.
    crit_cols: list[int] = []
    for c_header in crit_headers:
        col = header_index.get(c_header)
        if col is None:
            return None
        crit_cols.append(col)

    matching_vals: list[Any] = []
    for r_idx in range(1, db_arr.shape[0]):
        row = db_arr[r_idx]
        match_any_row = False
        for c_row_idx in range(1, crit_arr.shape[0]):
            match_all_cols = True
            for c_col_idx in range(crit_arr.shape[1]):
                c_val = crit_arr[c_row_idx, c_col_idx]
                if is_missing_value(c_val):
                    continue
                if not match_criteria(row[crit_cols[c_col_idx]], c_val):
                    match_all_cols = False
                    break
            if match_all_cols:
                match_any_row = True
                break

        if match_any_row:
            val = row[f_idx]
            if as_float:
                # Strings are not numbers. ScDBQueryParamBase starts with
                # mbSkipString true (queryparam.cxx). DSUM uses that iterator.
                # DCOUNTA sets the flag false and calls this with
                # as_float=False, so text still counts there. float("5") used
                # to add the text. A bool stays: Calc stores TRUE as a numeric
                # cell, so float(True) is 1. NaN is missing (is_missing_value)
                # and must not poison the aggregate.
                if isinstance(val, str):
                    continue
                try:
                    num = float(val)
                except (ValueError, TypeError, OverflowError):
                    continue
                if math.isnan(num):
                    continue
                matching_vals.append(num)
            else:
                matching_vals.append(val)
    return matching_vals


def _multi_criteria_mask(
    pairs: Sequence[tuple[Any, Any]],
    base_len: int | None = None,
    base_shape: tuple[int, ...] | None = None,
) -> np.ndarray | None:
    """Evaluate multiple (criteria_range, criterion) pairs.

    Returns a 1D boolean mask, or None when a range differs in shape or
    length. Excel COUNTIFS / AVERAGEIFS / SUMIFS need the same number of
    rows and columns. Ravelling first made a 2x3 range and a 3x2 range look
    equal. None tells the caller to return #VALUE! instead of truncating.
    """
    if not pairs:
        return np.ones(base_len, dtype=bool) if base_len is not None else np.array([], dtype=bool)

    cond_ranges: list[np.ndarray] = []
    criteria: list[Any] = []
    shapes: list[tuple[int, ...]] = []
    for r, c in pairs:
        arr = np.asarray(r, dtype=object)
        shapes.append(arr.shape)
        cond_ranges.append(arr.ravel())
        criteria.append(c)

    if base_shape is not None and any(shape != base_shape for shape in shapes):
        return None
    if any(shape != shapes[0] for shape in shapes):
        return None

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
    # Step back by calendar months from maturity. freq in (1, 2, 4), basis in
    # (0..4), settlement < maturity. A bad frequency (1e9) never terminates,
    # and a fixed day step (180, 182.5) drifts off the coupon date.
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


