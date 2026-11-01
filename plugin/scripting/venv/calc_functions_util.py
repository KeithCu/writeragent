# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Internal shared utilities for calc_functions formula emulation."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal, InvalidOperation, Overflow, ROUND_HALF_UP, localcontext
from functools import wraps
import math
import operator
import re
from typing import Any, Callable, Sequence

import numpy as np

from .coerce import _LO_ERROR_TOKENS, is_missing_value

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


# Whole-body handlers that return NaN for these three errors only.
# Do not add ZeroDivisionError or re.error: xirr, xnpv, and textsplit catch more.
def _nan_on_error(fn: Any) -> Any:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (ValueError, TypeError, OverflowError):
            return float("nan")

    return wrapper


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


def _actual_actual_year_length(start: dt.date, end: dt.date) -> float:
    """Year length for YEARFRAC basis 1.

    GetYearFrac (analysishelper.cxx, ODFF 1.2 §4.11.7.7). ``start`` is on
    or before ``end``. A span longer than one year uses the average length
    of each calendar year it touches. A shorter span uses 366 when it sits
    in one leap year or includes 29 February, otherwise 365. Dividing by
    a flat 365.25 made 2023-01-01 through 2024-01-01 about 0.99932.
    """
    y1, m1, d1 = start.year, start.month, start.day
    y2, m2, d2 = end.year, end.month, end.day
    years_differ = y1 != y2
    longer_than_one_year = years_differ and (
        y2 != y1 + 1 or m1 < m2 or (m1 == m2 and d1 < d2)
    )
    if longer_than_one_year:
        day_count = sum(366 if calendar.isleap(year) else 365 for year in range(y1, y2 + 1))
        return day_count / (y2 - y1 + 1)
    if not years_differ and calendar.isleap(y1):
        return 366.0
    includes_feb29 = years_differ and (
        (calendar.isleap(y1) and (m1 < 2 or (m1 == 2 and d1 <= 29)))
        or (calendar.isleap(y2) and (m2 > 2 or (m2 == 2 and d2 == 29)))
    )
    if includes_feb29:
        return 366.0
    return 365.0


def yearfrac(start_date: Any, end_date: Any, basis: Any = 0) -> float:
    # Swap when start is after end. Excel and LibreOffice GetYearFrac return
    # a non-negative fraction either way. Bond callers check order themselves.
    try:
        s = float(start_date)
        e = float(end_date)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if b < 0 or b > 4 or not math.isfinite(s) or not math.isfinite(e):
        return float("nan")
    if s > e:
        s, e = e, s
    # _days360 is what days360() calls. Importing days360 from calc_functions_d_h
    # pulled that module back while it imports yearfrac at load time.
    sd = _serial_to_date(s)
    ed = _serial_to_date(e)
    if sd is None or ed is None:
        return float("nan")
    days = _days_between(sd, ed, b)
    if b == 1:
        return days / _actual_actual_year_length(sd, ed)
    return days / (365.0 if b == 3 else 360.0)


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
        exp = Decimal("1e" + str(-decimals))
        with localcontext() as ctx:
            ctx.prec = max(28, abs(d.adjusted()) + abs(decimals) + 2)
            return d.quantize(exp, rounding=ROUND_HALF_UP)
    except (InvalidOperation, Overflow):
        raise ValueError("decimal quantize failed") from None


def _round_sig(x: float, sig: int) -> float:
    """Round float to significant digits using half-up rounding. Keeps non-finite values unchanged."""
    if not math.isfinite(x):
        return x
    if x == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(x)))
    decimals = sig - 1 - exponent
    try:
        return float(_round_half_up(x, decimals))
    except ValueError:
        return float("nan")


def _as_bool_flag(val: Any) -> bool:
    """LINEST / LOGEST ``const``.

    A blank cell is false (``GetBool`` of an empty cell). ``"TRUE"`` and
    ``"FALSE"`` are those booleans. ``"0"`` is false. ``bool("0")`` and
    ``bool("FALSE")`` used to be true, so the fit kept an intercept.
    """
    if isinstance(val, str):
        token = val.strip().upper()
        if token == "TRUE":
            return True
        if token == "FALSE":
            return False
    return _as_nonzero(val)


def _as_nonzero(val: Any) -> bool:
    """Calc ``GetDouble() != 0`` for a flag.

    ``bool("0")`` and ``bool("FALSE")`` are true. GetDouble of ``"0"`` is
    0, and a non-numeric string is ``#VALUE!``. The words TRUE and FALSE
    are not booleans here; that map is LINEST's ``_as_bool_flag``. A blank
    cell is 0.
    """
    if val is None or val == "":
        return False
    return bool(float(val))


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


def is_number(val: Any) -> bool:
    """Return True if val is a numeric type (int, float, np.integer, np.floating), excluding booleans."""
    # np.integer and np.floating count. bool and np.bool_ do not: they are logical.
    return isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, (bool, np.bool_))


def _clean_paired_arrays(data1: Any, data2: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """Extract 1D float arrays of matching length, keeping only pairs where both cells are finite numbers."""
    # conversion failures are left to the worker exception path, which returns #VALUE!
    d1 = np.asarray(data1, dtype=object).ravel()
    d2 = np.asarray(data2, dtype=object).ravel()
    if len(d1) != len(d2) or len(d1) == 0:
        return None
    c1: list[float] = []
    c2: list[float] = []
    for x, y in zip(d1, d2):
        if isinstance(x, (bool, np.bool_)) or isinstance(y, (bool, np.bool_)):
            continue
        xf = _to_float_or_none(x)
        yf = _to_float_or_none(y)
        if xf is not None and yf is not None and math.isfinite(xf) and math.isfinite(yf):
            c1.append(xf)
            c2.append(yf)
    if not c1:
        return None
    return np.asarray(c1, dtype=float), np.asarray(c2, dtype=float)


def _scipy_stats() -> Any | None:
    """Return scipy.stats module if available, else None."""
    try:
        import scipy.stats
        return scipy.stats
    except ImportError:
        return None


def _collect_a_values(*args: Any) -> np.ndarray:
    """Collect flat float array for *A functions (AVERAGEA, MAXA, etc.)."""
    # *A functions skip blanks. Text and blanks evaluate to 0, while errors propagate as NaN.
    vals: list[float] = []
    for arg in args:
        for v in np.asarray(arg, dtype=object).ravel():
            if v is None:
                continue
            if _is_calc_error(v):
                vals.append(float("nan"))
                continue
            # A Python int bigger than the float range raises OverflowError, which
            # a (ValueError, TypeError) handler missed, so AVERAGEA crashed instead
            # of treating the cell as 0 the way other non-numbers do.
            f = _to_float_or_none(v)
            vals.append(0.0 if f is None else f)
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
    """Excel-style *, ? and ~ wildcard match against the whole text (case-insensitive)."""
    # pattern is re.escape'd; re.error cannot occur
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
    regex_pat = "".join(out)
    return re.fullmatch(regex_pat, text, flags=re.IGNORECASE) is not None


_CMP = {
    "=": operator.eq,
    "<>": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def _to_float_or_none(value: Any) -> float | None:
    # NaN is a successful float. Criterion "nan" must stay NaN so a non-numeric
    # value still takes `op == "<>"` instead of a string compare.
    try:
        return float(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_criterion(crit: Any) -> Callable[[Any], bool]:
    """Parse an Excel/Calc condition (e.g. '>5', '<=10', '<>apple', '*item*', '=') into a matcher callable."""
    if is_missing_value(crit):
        return lambda val: is_missing_value(val) or val == "" or val is None

    if isinstance(crit, str):
        m = re.match(r"^(<=|>=|<>|<|>|==|=)(?![<>=])(.*)$", crit)
        if m:
            op = "=" if m.group(1) == "==" else m.group(1)
            val_str: str | None = m.group(2)
        else:
            op = "="
            val_str = crit
    else:
        op = "="
        val_str = None

    crit_bool: bool | None = None
    if isinstance(crit, (bool, np.bool_)):
        crit_bool = bool(crit)
    elif val_str is not None and val_str.upper() in ("TRUE", "FALSE"):
        crit_bool = val_str.upper() == "TRUE"

    if val_str is not None:
        c_num = _to_float_or_none(val_str)
    elif isinstance(crit, (int, float, np.integer, np.floating)):
        c_num = _to_float_or_none(crit)
    else:
        c_num = None

    c_pattern = val_str if val_str is not None else str(crit)

    def matcher(val: Any) -> bool:
        val_is_blank = val is None or val == "" or is_missing_value(val)
        if val_str == "":
            if op == "=":
                return val_is_blank
            if op == "<>":
                return not val_is_blank

        if val_is_blank:
            return op == "<>"

        if crit_bool is not None:
            if isinstance(val, (bool, np.bool_)):
                if op == "=":
                    return bool(val) == crit_bool
                if op == "<>":
                    return bool(val) != crit_bool
                return False
            return op == "<>"

        if isinstance(val, (bool, np.bool_)):
            return op == "<>"

        v_num = _to_float_or_none(val) if isinstance(val, (int, float, np.integer, np.floating)) else None
        if v_num is not None and math.isnan(v_num):
            v_num = None

        if c_num is not None and v_num is not None:
            return _CMP[op](v_num, c_num)
        if (c_num is None) != (v_num is None):
            if c_num is not None and isinstance(val, str) and op in ("=", "<>"):
                matched = _wildcard_fullmatch(c_pattern, str(val))
                return matched if op == "=" else not matched
            return op == "<>"
        v_str = str(val)
        if op == "=":
            return _wildcard_fullmatch(c_pattern, v_str)
        if op == "<>":
            return not _wildcard_fullmatch(c_pattern, v_str)
        return _CMP[op](v_str.casefold(), c_pattern.casefold())

    return matcher


def match_criteria(val: Any, crit: Any) -> bool:
    """Evaluate an Excel/Calc condition (e.g. '>5', '<=10', '<>apple', '*item*', '=')."""
    return _parse_criterion(crit)(val)


def _extract_numeric_array(
    *args: Any,
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
            if isinstance(x, str):
                continue
            v = _to_float_or_none(x)
            if v is None or (not propagate_nan and math.isnan(v)):
                continue
            vals.append(v)
    return np.asarray(vals, dtype=float)


def _criteria_numbers(values: Any, pairs: Sequence[tuple[Any, Any]]) -> list[float] | None:
    """Matching numbers for one or more (range, criterion) pairs. ``pairs`` is non-empty."""
    v_arr = np.asarray(values, dtype=object)
    mask = _multi_criteria_mask(pairs, base_shape=v_arr.shape)
    if mask is None:
        return None
    # Defaults keep error cells and NaN (SUMIFS) and skip text and bools.
    return _extract_numeric_array(v_arr.ravel()[mask]).tolist()


def _complex_coeff(n: float) -> str:
    """Format complex coefficient without trailing .0 on integers."""
    return str(int(n)) if n.is_integer() else str(n)


def _from_complex(c: complex, suffix: str = "i") -> str:
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
    imag_part = ("-" if imag < 0 else "") + ("" if abs(imag) == 1 else _complex_coeff(abs(imag))) + suffix
    if real == 0:
        return imag_part
    return _complex_coeff(real) + ("+" if imag > 0 else "") + imag_part


def _multi_criteria_mask(
    pairs: Sequence[tuple[Any, Any]],
    base_shape: tuple[int, ...] | None = None,
) -> np.ndarray | None:
    """Evaluate multiple (criteria_range, criterion) pairs.

    Returns a 1D boolean mask, or None when a range differs in shape.
    Excel COUNTIFS / AVERAGEIFS / SUMIFS need the same number of
    rows and columns. Ravelling first made a 2x3 range and a 3x2 range look
    equal. None tells the caller to return #VALUE! instead of truncating.
    """
    parsed: list[tuple[np.ndarray, Any]] = []
    target = base_shape
    for r, c in pairs:
        arr = np.asarray(r, dtype=object)
        if target is None:
            target = arr.shape
        elif arr.shape != target:
            return None
        parsed.append((arr.ravel(), c))
    width = len(parsed[0][0])
    mask = np.ones(width, dtype=bool)
    for flat, crit in parsed:
        matcher = _parse_criterion(crit)
        mask &= np.fromiter((matcher(v) for v in flat), dtype=bool, count=width)
    return mask


