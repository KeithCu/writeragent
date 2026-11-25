# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for the calc_functions_* letter files. Names without a leading underscore are what the letter files import; underscore names are used only inside this module.

Where Calc and Excel differ, helpers follow Calc: it is what the sheet shows and what spreadsheet-import verification compares against.

How a helper reports a sheet error. Each function uses one of these, and its tests pin which:

1. Raise. The exception reaches the =PY() worker, which picks the cell error from its type
   (worker_exception_error_code, plugin/calc/python/addin_values.py): ZeroDivisionError is #DIV/0!,
   OverflowError is #NUM!, anything else (ValueError, TypeError) is #VALUE!. Text in a numeric
   argument takes this path in every distribution function, as Calc's GetDouble turns text it
   cannot convert into #VALUE! (ScGlobal::ConvertStringToValue, sc/source/core/data/global2.cxx).
2. Return float("nan"). The cell shows #NUM! (a plain NaN is IllegalFPOperation in
   GetDoubleErrorValue). Used for arguments outside a function's domain and for results that
   are not finite (finite_or_nan). A few helpers return NaN where Excel shows #VALUE!, #DIV/0!
   or #N/A (e.g. days360 and euroconvert text flags); the comment at that return says which.
   NA_NAN shows #N/A (na, xmatch miss, textsplit padding, textafter/textbefore miss or empty text, prob size
   mismatch), NESTED_ARRAY_NAN shows Err:533 (filter with no match and no if_empty),
   VALUE_NAN shows #VALUE! (textafter/textbefore bad flag or instance, encodeurl and numbervalue empty text),
   DIV0_NAN shows #DIV/0! (gammadist density at 0 with alpha < 1, averageif/averageifs no match), ILLEGAL_PARAMETER_NAN
   shows Err:504 (xmatch bad mode), and ILLEGAL_ARGUMENT_NAN shows Err:502 (the domain checks
   of negbinomdist, hypgeomdist, combina, fact, the bit functions, decimal radix, db,
   cumipmt/cumprinc, ttest, tdist, tinv, fdist, finv, percentrank significance, trimmean percent).
3. Return an error token string ("#VALUE!", "#N/A", "#NUM!", "#DIV/0!", "Err:502", "Err:504",
   or a propagated Err:<n> token). The add-in shows that error. Used where the cell must show
   a token other than #NUM! and the helper catches its own parse errors (lookup, text, array
   and regression helpers), and the first error of a range (@propagate_errors, criteria_reduce,
   first_error: npv, sumsq, sumproduct, subtotal, aggregate; first_coded_error: D*). "Err:502" /
   "Err:504" come from base, filter shape, index, vlookup/hlookup, xlookup bad mode or return size, growth, trend,
   linest and logest. "#VALUE!" also from mode with no repeat and fact above 170.
   @propagate_errors returns an argument's own error (token or coded NaN) before the helper runs,
   and unwraps one-cell ranges.
4. @calc_function. A ValueError, TypeError or OverflowError inside the helper becomes NaN
   (#NUM!); any other exception still raises.
"""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal, InvalidOperation, Overflow, ROUND_HALF_UP, localcontext
from functools import wraps
import math
import operator
import re
import struct
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from plugin.scripting.calc_range import CalcRange

from .coerce import is_blank_value, is_error_token, is_missing_value

def formula_error_nan(code: int) -> float:
    """Quiet NaN carrying a LibreOffice FormulaError code in its low bits (GetDoubleErrorValue, include/formula/errorcodes.hxx)."""
    return struct.unpack("d", struct.pack("Q", 0x7FF8000000000000 | code))[0]


# 0x7FFF is FormulaError::NotAvailable, 502 is FormulaError::IllegalArgument,
# 504 is FormulaError::IllegalParameter, 519 is FormulaError::NoValue,
# 532 is FormulaError::DivisionByZero, 533 is FormulaError::NestedArray.
# They are ordinary NaNs in Python; the worker does not import plugin.calc.
NA_NAN: float = formula_error_nan(0x7FFF)
ILLEGAL_ARGUMENT_NAN: float = formula_error_nan(502)
ILLEGAL_PARAMETER_NAN: float = formula_error_nan(504)
VALUE_NAN: float = formula_error_nan(519)
NESTED_ARRAY_NAN: float = formula_error_nan(533)
DIV0_NAN: float = formula_error_nan(532)


def nan_error_code(val: float) -> int:
    """Low 32 bits of a NaN (FormulaError code), or 0 for a plain NaN (GetDoubleErrorValue, include/formula/errorcodes.hxx)."""
    return struct.unpack("Q", struct.pack("d", val))[0] & 0xFFFFFFFF


class RegressionError(Exception):
    """A regression input Calc rejects. ``exc.args[0]`` is the error the cell shows (a token or a coded NaN)."""


def require_samples(n: int, k: int, const: bool) -> None:
    """Require at least one sample per fitted coefficient: K slopes plus the intercept when const is on.

    Fewer samples have no unique fit, and Calc reports an error (LINEST(5;3), TREND(5;3),
    and GROWTH(5;3) are Err:504; CalculateRGPRKP and CalculateTrendGrowth, interpr5.cxx).
    """
    if n < k + int(const):
        raise RegressionError("Err:504")


_SERIAL_OFFSET: int = 693594
# Sentinel for an omitted optional argument: a present None is a blank cell, so it cannot mean "omitted".
OMITTED: Any = object()

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


def calc_function(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn ValueError, TypeError and OverflowError raised inside fn into NaN (#NUM!). Other exceptions propagate."""
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (ValueError, TypeError, OverflowError):
            return float("nan")

    return wrapper


def cell_value(val: Any) -> Any:
    """The cell of a one-cell CalcRange; any other value unchanged."""
    return val.values[0][0] if isinstance(val, CalcRange) and val.shape == (1, 1) else val


def argument_error(arg: Any) -> float | str | None:
    """The error of one argument: any error of a scalar, the first coded error of a range."""
    cells = np.asarray(arg, dtype=object)
    return first_error(cells.ravel()) if cells.ndim == 0 else first_coded_error(cells.ravel())


def py_str(val: Any) -> str:
    """Text of a value, "" for a blank cell. Inline =PY() code calls it to avoid the str( token."""
    val = cell_value(val)
    return "" if val is None else str(val)


def whole_number(val: Any) -> int:
    """An index argument as GetInt32 reads it (interpr4.cxx): approxFloor toward zero, a blank cell is 0."""
    val = cell_value(val)
    number = 0.0 if val is None else float(val)
    return approx_floor(number) if number >= 0 else -approx_floor(-number)


def propagate_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return the error of an argument instead of calling fn.

    A one-cell range is unwrapped to its cell. A scalar error, including a plain
    NaN, always propagates; in a range only an error with a code does. The last
    argument wins, because the interpreter pops arguments from the last (EDATE(A1;B1)
    shows B1's error).
    """
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        args = tuple(cell_value(a) for a in args)
        kwargs = {k: cell_value(v) for k, v in kwargs.items()}
        for arg in reversed((*args, *kwargs.values())):
            err = argument_error(arg)
            if err is not None:
                return err
        return fn(*args, **kwargs)

    return wrapper


def serial_to_date(serial: Any) -> dt.date | None:
    """Convert an Excel serial date number or date object to datetime.date (+693594 offset), or None."""
    if isinstance(serial, dt.datetime):
        return serial.date()
    if isinstance(serial, dt.date):
        return serial
    try:
        return dt.date.fromordinal(int(float(serial)) + _SERIAL_OFFSET)
    except (ValueError, TypeError, OverflowError):
        return None


def date_to_serial(d: dt.date) -> float:
    """Convert a datetime.date object to Excel serial date number (-693594 offset)."""
    return float(d.toordinal() - _SERIAL_OFFSET)


def days360_dates(sd: dt.date, ed: dt.date, european: bool = False) -> float:
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
    sd = serial_to_date(d1)
    ed = serial_to_date(d2)
    if sd is None or ed is None:
        return float("nan")
    # 30/360 day count, including the 31st and end-of-month adjustments from DAYS360.
    if basis in (0, 4):
        return days360_dates(sd, ed, european=(basis == 4))
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


@propagate_errors
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
    # yearfrac reaches DAYS360 through _days_between → days360_dates rather than d_h.days360, because calc_functions_d_h imports yearfrac from this module.
    sd = serial_to_date(s)
    ed = serial_to_date(e)
    if sd is None or ed is None:
        return float("nan")
    days = _days_between(sd, ed, b)
    if b == 1:
        return days / _actual_actual_year_length(sd, ed)
    return days / (365.0 if b == 3 else 360.0)


def _get_coupon_dates(
    settlement: Any, maturity: Any, frequency: Any, basis: Any = 0
) -> tuple[float, float, float, float, int]:
    """Calculate previous and next coupon dates stepping backwards from maturity in calendar months.

    Returns (prev_serial, curr_serial, days_in_period, num_coupons, basis_code).
    """
    # Step back by calendar months from maturity. freq in (1, 2, 4), basis in
    # (0..4), settlement < maturity. A bad frequency (1e9) never terminates,
    # and a fixed day step (180, 182.5) drifts off the coupon date.
    freq = int(float(frequency))
    b = int(float(basis))
    if freq not in (1, 2, 4) or b not in (0, 1, 2, 3, 4):
        raise ValueError("Invalid coupon frequency or basis")
    s_date = serial_to_date(settlement)
    m_date = serial_to_date(maturity)
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

    p_ser = date_to_serial(prev)
    c_ser = date_to_serial(curr)
    if b in (0, 2, 4):
        days_in_per = 360.0 / freq
    elif b == 3:
        days_in_per = 365.0 / freq
    else:
        days_in_per = float((curr - prev).days)

    return p_ser, c_ser, days_in_per, float(k), b


def coupon_schedule(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> tuple[float, float, float, float, float, float]:
    """Compute coupon schedule: (p_ser, c_ser, days_in_per, k, a_days, dsc_days)."""
    p_ser, c_ser, days_in_per, k, b = _get_coupon_dates(settlement, maturity, frequency, basis)
    # _days_between truncates each serial to its calendar day, so a fractional settlement counts from that day.
    a_days = float(_days_between(p_ser, settlement, b))
    dsc_days = float(_days_between(settlement, c_ser, b))
    return p_ser, c_ser, days_in_per, k, a_days, dsc_days


def round_half_up(val: float | Decimal, decimals: int) -> Decimal:
    """Round a float or Decimal half away from zero (Excel/Calc rounding). A Decimal input is used as is, so a caller can scale it exactly first."""
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    if not d.is_finite():
        raise ValueError("Non-finite value cannot be rounded")
    # Widen decimal context for large place counts so quantize failures become ValueError and formulas return NaN instead of InvalidOperation escaping.
    try:
        exp = Decimal("1e" + str(-decimals))
        with localcontext() as ctx:
            ctx.prec = max(28, abs(d.adjusted()) + abs(decimals) + 2)
            return d.quantize(exp, rounding=ROUND_HALF_UP)
    except (InvalidOperation, Overflow):
        raise ValueError("decimal quantize failed") from None


def round_sig(x: float, sig: int) -> float:
    """Round float to significant digits using half-up rounding. Keeps non-finite values unchanged."""
    if not math.isfinite(x):
        return x
    if x == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(x)))
    decimals = sig - 1 - exponent
    try:
        return float(round_half_up(x, decimals))
    except ValueError:
        return float("nan")


def approx_floor(x: float) -> int:
    """Floor with Calc's approximate-value nudge (rtl::math::approxFloor and rtl_math_approxValue, sal/rtl/math.cxx)."""
    # rtl_math_approxValue keeps integers, values with at most 11 fractional bits, and values from 2**41
    # up as they are. 15-digit rounding would change those.
    # inf raises OverflowError (#NUM!), NaN raises ValueError, like int().
    if abs(x) >= 2**41 or (x * 2048.0).is_integer():
        return math.floor(x)
    return math.floor(round_sig(x, 15))


def approx_equal(a: float, b: float) -> bool:
    """Calc's approximate float equality (rtl_math_approxEqual, sal/rtl/math.cxx)."""
    if a == b:
        return True
    if a == 0.0 or b == 0.0 or (a < 0.0) != (b < 0.0):
        return False
    diff = abs(a - b)
    if not math.isfinite(diff):
        return False
    low = min(abs(a), abs(b))
    if diff >= max(low * 2.0**-48, 10.0 ** math.floor(math.log10(low)) * 5e-15):
        return False
    # Two different integers below 2**53 are exact, so they are different numbers.
    return not (a.is_integer() and b.is_integer() and max(abs(a), abs(b)) < 2**53)


def format_quantized(q: Decimal, decimals: int, *, commas: bool) -> str:
    """Format a quantized Decimal to a fixed place count, optionally grouping thousands."""
    places = max(0, decimals)
    # Decimal('-0.00') formats as "-0.00". A rounded zero has no sign.
    if q == 0:
        q = abs(q)
    return f"{q:,.{places}f}" if commas else f"{q:.{places}f}"


_LOGICAL_TEXT: dict[str, float] = {"TRUE": 1.0, "FALSE": 0.0}


def as_nonzero(val: Any) -> bool:
    """Calc ``GetBool`` (``GetDouble() != 0``) for a scalar flag argument.

    A blank cell (``None`` or ``""``) is false. The text ``TRUE`` and ``FALSE``
    (any case, surrounding spaces ignored) are those booleans, because Calc's
    number scanner reads them (ScGlobal::ConvertStringToValue, LOCALE branch,
    sc/source/core/data/global2.cxx). Numeric text such as ``"0"`` is its
    number. Any other text raises ValueError, which the cell shows as #VALUE!.
    """
    if val is None or val == "":
        return False
    if isinstance(val, str):
        val = _LOGICAL_TEXT.get(val.strip().upper(), val)
    return bool(float(val))


def build_holiday_set(holidays: Any | None = None) -> set[dt.date]:
    """Build a set of datetime.date from a holiday argument (scalar or array)."""
    h_dates: set[dt.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                d = serial_to_date(h)
                if d is not None:
                    h_dates.add(d)
    return h_dates


def parse_weekend(weekend: Any = 1) -> set[int] | float:
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



def is_calc_error(val: Any) -> bool:
    """Return True if val is a Calc error (NaN or a Calc error token such as #VALUE! or Err:502)."""
    if isinstance(val, (bool, np.bool_)):
        return False
    if isinstance(val, (float, np.floating)):
        return math.isnan(float(val))
    if isinstance(val, str):
        return is_error_token(val)
    return False


def first_error(cells: Iterable[Any]) -> float | str | None:
    """Return the first Calc error in ``cells`` (stripped token or NaN with its code), or None."""
    for v in cells:
        if is_calc_error(v):
            return v.strip() if isinstance(v, str) else float(v)
    return None


def first_coded_error(cells: Iterable[Any]) -> float | str | None:
    """Like ``first_error``, but a plain NaN is skipped, because a blank cell of a numeric range can arrive as one (docs/calc/py-data-shapes.md)."""
    return first_error(v for v in cells if isinstance(v, str) or (is_calc_error(v) and nan_error_code(float(v))))


def is_number(val: Any) -> bool:
    """Return True if val is a numeric type (int, float, np.integer, np.floating), excluding booleans."""
    # np.integer and np.floating count. bool and np.bool_ do not: they are logical.
    return isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, (bool, np.bool_))


def finite_or_nan(x: float) -> float:
    """x, or NaN when x is inf or NaN.

    Calc turns an infinite result into #NUM! (GetDoubleErrorValue, include/formula/errorcodes.hxx).
    Here NaN is the error value; IFERROR and is_calc_error do not treat inf as an error.
    """
    return x if math.isfinite(x) else float("nan")


def number_text(val: float) -> str:
    """A number as Calc's General format shows it: 15 significant digits, "E+020" exponents.

    COUNTIF patterns and TEXTJOIN compare and join this text (ScQueryEvaluator::getCellString,
    sc/source/core/data/queryevaluator.cxx; SvNFEngine::GetInputLineString, svl/source/numbers/zforlist.cxx).
    """
    if not math.isfinite(val):
        return str(val)
    if val.is_integer() and abs(val) < 2**53:
        return str(int(val))
    mantissa, exp = f"{val:.14e}".split("e")
    power = int(exp)
    if -15 < power < 15:
        return f"{Decimal(f'{val:.15g}'):f}"
    return f"{mantissa.rstrip('0').rstrip('.')}E{power:+04d}"


def clean_paired_arrays(data1: Any, data2: Any) -> tuple[np.ndarray, np.ndarray] | None:
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


def orient_columns(arr: np.ndarray, known_y: np.ndarray, size: int, axis: int) -> np.ndarray:
    """A regression range with one variable per column.

    A scalar (0-d, which GetMatrix in interpr5.cxx stores as a 1×1 matrix) or a
    1-D range is one column. When known_y is 2-D, it fixes the orientation
    (CheckMatrix in sc/source/core/tool/interpr5.cxx): a column y keeps arr, a
    row y transposes it. When known_y is 1-D, a 2-D block whose ``axis`` length
    is not ``size`` while its other axis is ``size`` is transposed.
    """
    if arr.ndim <= 1:
        return arr.reshape(-1, 1)
    if arr.ndim > 2:
        return arr
    if known_y.ndim == 2:
        return arr if known_y.shape[1] == 1 else arr.T
    if arr.shape[axis] != size and arr.shape[1 - axis] == size:
        return arr.T
    return arr


def numeric_cells(arg: Any) -> tuple[np.ndarray, float | str | None]:
    """Floats of a regression range and its first error; error cells are NaN in the array.

    A text or blank cell raises Err:502 (CheckMatrix, interpr5.cxx). A plain NaN in a range is a
    blank, because a blank cell of a numeric range can arrive as one.
    """
    cells = np.asarray(arg, dtype=object)
    find = first_error if cells.ndim == 0 else first_coded_error
    err = find(cells.ravel())
    if err is not None and cells.size == 1:  # GetMatrix of one error cell sets the error itself
        raise RegressionError(err)
    values = []
    for v in cells.ravel():
        if find([v]) is not None:
            values.append(float("nan"))
        elif v is None or isinstance(v, str) or math.isnan(v):
            raise RegressionError("Err:502")
        else:
            values.append(float(v))
    return np.asarray(values, dtype=float).reshape(cells.shape), err


def regression_inputs(
    known_y: Any, known_x: Any, const: Any, *, log: bool
) -> tuple[np.ndarray, np.ndarray, bool]:
    """Parse and validate known_y, known_x and const for LINEST, LOGEST, TREND and GROWTH.

    Checks y <= 0 (for LOGEST, Err:502), then shape (Err:502), then sample count
    (Err:504): CheckMatrix and CalculateRGPRKP (interpr5.cxx). Same-shaped known_x
    and known_y are one x variable (CheckMatrix nCase 1), a 2-D block y accepts only
    an x of its own shape, and omitted known_x (None) is 1..n. Input cell errors are
    raised after shape and sample checks so Err:502 / Err:504 take precedence.
    """
    y, y_err = numeric_cells(known_y)
    if log:
        if np.any(y <= 0):
            raise RegressionError("Err:502")
        y = np.log(y)
    n = y.size
    if known_x is None:
        x, x_err = np.arange(1, n + 1, dtype=float).reshape(-1, 1), None
    else:
        x, x_err = numeric_cells(known_x)
        same_shape = x.shape == y.shape
        if y.ndim == 2 and min(y.shape) > 1 and not same_shape:
            raise RegressionError("Err:502")
        # CheckMatrix refuses a scalar x against a vector y, which numpy would otherwise broadcast.
        x = x.reshape(-1, 1) if same_shape else orient_columns(x, y, n, axis=0)
        if x.ndim != 2 or x.shape[0] != n:
            raise RegressionError("Err:502")
    with_intercept = as_nonzero(const)
    require_samples(n, x.shape[1], with_intercept)
    err = y_err if x_err is None else x_err
    if err is not None:
        raise RegressionError(err)
    return y, x, with_intercept


def least_squares(
    y: np.ndarray, x: np.ndarray, with_intercept: bool
) -> tuple[np.ndarray, float, np.ndarray]:
    """(center, base, slopes) of the fit base + (x - center) @ slopes."""
    # Calc fits x - mean and y - mean. Uncentred values near 1e8 make the x column and the
    # intercept column indistinguishable to lstsq.
    center = x.mean(axis=0) if with_intercept else np.zeros(x.shape[1])
    base = float(y.mean()) if with_intercept else 0.0
    # A constant column (or an all-zero one without intercept) has no slope: PushNoValue.
    spread = np.ptp(x, axis=0) if with_intercept else np.abs(x).max(axis=0)
    if np.any(spread == 0):
        raise RegressionError("#VALUE!")
    slopes = np.linalg.lstsq((x - center) / spread, y.ravel() - base, rcond=None)[0] / spread
    return center, base, slopes


def trend_fit(known_y: Any, known_x: Any, new_x: Any, const: Any, *, log: bool = False) -> list[Any]:
    """Least-squares prediction of TREND, and of GROWTH on log y (ScInterpreter::CalculateTrendGrowth, interpr5.cxx).

    With one x variable, new_x may have any shape. Shape and sample rejections raise
    RegressionError with Calc's token; other rejections raise ValueError. const is
    checked here only (LINEST ignores a const error), and a new_x error affects only
    its own result element.
    """
    const_err = argument_error(const)
    if const_err is not None:
        raise RegressionError(const_err)
    y, x, with_intercept = regression_inputs(known_y, known_x, const, log=log)
    k = x.shape[1]
    cells = x if new_x is None else np.asarray(new_x, dtype=object)
    at = numeric_cells(cells)[0]
    if new_x is not None:
        # An (m, 1) new_x would broadcast against a k-wide center instead of failing.
        cells, at = (a.reshape(-1, 1) if k == 1 else orient_columns(a, y, k, axis=1) for a in (cells, at))
    if at.ndim != 2 or at.shape[1] != k:
        raise RegressionError("Err:502")
    center, base, slopes = least_squares(y, x, with_intercept)
    fitted = (at - center) @ slopes + base
    errors = [first_error(row) for row in cells]
    return [f if e is None else e for f, e in zip((np.exp(fitted) if log else fitted).tolist(), errors)]


def scipy_stats() -> Any | None:
    """Return scipy.stats module if available, else None."""
    try:
        import scipy.stats  # optional and heavy: loaded on first use so calc imports without scipy
        return scipy.stats
    except ImportError:
        return None


def collect_a_values(*args: Any) -> np.ndarray:
    """Collect flat float array for *A functions (AVERAGEA, MAXA, etc.)."""
    # *A functions skip None (an empty cell). Callers are @propagate_errors, so
    # no error token reaches here; text and blank strings count as 0, and a plain NaN stays NaN.
    vals: list[float] = []
    for arg in args:
        for v in np.asarray(arg, dtype=object).ravel():
            if v is None:
                continue
            # A Python int bigger than the float range raises OverflowError, which
            # _to_float_or_none turns into None so the cell counts as 0 like other non-numbers.
            f = _to_float_or_none(v)
            vals.append(0.0 if f is None else f)
    return np.asarray(vals, dtype=float)




def npf_result(kind: str, *args: Any) -> float:
    """One numpy-financial scalar, or NaN where Calc/Excel are #NUM! / #DIV/0!.

    The library returns ±inf for a zero period count and for an NPER that
    never amortizes, and raises when ``when`` is not 0 or 1. Callers pass
    0 or 1. A missing install is the same NaN as a missing scipy helper.
    """
    try:
        import numpy_financial as npf  # optional and heavy: loaded on first use so calc imports without numpy_financial
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


def wildcard_fullmatch(pattern: str, text: str) -> bool:
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


_NUMERIC_TYPES = (int, float, np.integer, np.floating, np.bool_)  # bool is an int

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


def is_criteria_blank(value: Any) -> bool:
    """None, blank text, NaN, NaT or pd.NA. Error text is not blank: criteria compare an error cell by its displayed text (ScQueryEvaluator::getCellString, sc/source/core/data/queryevaluator.cxx)."""
    return is_blank_value(value) if isinstance(value, str) else is_missing_value(value)


def criterion_error(crits: Iterable[Any]) -> float | str | None:
    """Error of COUNTIF-style criteria: a NaN, or a one-cell range that holds an error. The last one wins.

    A Python string is text, because COUNTIF(A1:A5;"#N/A") counts cells in Calc.
    """
    return first_error(reversed([cell_value(c) for c in crits if not isinstance(c, str)]))


def _lookup_number(val: Any) -> float | None:
    """Numeric LOOKUP key. A bool is 0 or 1. Text and blanks are not numbers."""
    if isinstance(val, (bool, np.bool_, int, float, np.integer, np.floating)):
        try:
            number = float(val)
        except (ValueError, TypeError, OverflowError):
            return None
        # a NaN key compares false and is already a miss (#N/A)
        return number
    return None


def sorted_match(lookup_val: Any, keys: Iterable[Any]) -> int | None:
    """Position of the last key <= lookup_val of the same type (ScLookup, interpr1.cxx). Text compares case-insensitively."""
    query_num = _lookup_number(lookup_val)

    def _matches(value: Any) -> bool:
        if query_num is not None:
            cell = _lookup_number(value)
            return cell is not None and cell <= query_num
        if isinstance(lookup_val, str) and lookup_val != "" and isinstance(value, str) and value != "":
            return value.casefold() <= lookup_val.casefold()
        return False

    return max((i for i, value in enumerate(keys) if _matches(value)), default=None)


def _parse_criterion(crit: Any) -> Callable[[Any], bool]:
    """Parse an Excel/Calc condition (e.g. '>5', '<=10', '<>apple', '*item*', '=') into a matcher callable."""
    crit = cell_value(crit)
    if is_criteria_blank(crit):
        return is_criteria_blank

    if isinstance(crit, str):
        m = re.match(r"^(<=|>=|<>|<|>|==|=)(?![<>=])(.*)$", crit)
        if m:
            return value_matcher("=" if m.group(1) == "==" else m.group(1), m.group(2))
    return value_matcher("=", crit)


def value_matcher(op: str, operand: Any) -> Callable[[Any], bool]:
    """Matcher for an operator and an already-separated operand."""
    val_str = operand if isinstance(operand, str) else None
    c_num: float | None = None
    if val_str is not None:
        logical = _LOGICAL_TEXT.get(val_str.strip().upper())
        c_num = _to_float_or_none(val_str) if logical is None else logical
    elif isinstance(operand, _NUMERIC_TYPES):
        c_num = _to_float_or_none(operand)
    # Only a text criterion matches text cells; COUNTIF(A1;1) and COUNTIF(A1;TRUE()) do not match the text "1" or "TRUE".
    c_pattern = val_str if val_str is not None else str(operand)

    def matcher(val: Any) -> bool:
        val_is_blank = is_criteria_blank(val)
        if val_str == "":
            if op == "=":
                return val_is_blank
            if op == "<>":
                return not val_is_blank

        if val_is_blank:
            return op == "<>"

        v_num = _to_float_or_none(val) if isinstance(val, _NUMERIC_TYPES) else None
        if c_num is not None and v_num is not None:
            # compareByValue (queryevaluator.cxx): approxEqual counts as equal for every operator.
            return _CMP[op](v_num, v_num if approx_equal(v_num, c_num) else c_num)
        if c_num is not None:
            if isinstance(val, str) and val_str is not None and op in ("=", "<>"):
                matched = wildcard_fullmatch(c_pattern, val)
                return matched if op == "=" else not matched
            return op == "<>"
        # With a text criterion Calc compares every cell by its text for = and <>
        # (isQueryByString, queryevaluator.cxx).
        if isinstance(val, (bool, np.bool_)):
            v_str = "TRUE" if val else "FALSE"
        elif v_num is None:
            v_str = str(val)
        else:
            v_str = number_text(v_num)
        if op == "=":
            return wildcard_fullmatch(c_pattern, v_str)
        if op == "<>":
            return not wildcard_fullmatch(c_pattern, v_str)
        # Ordering criteria with text skip numbers and errors (COUNTIF(…;">a")).
        if v_num is not None or is_error_token(v_str):
            return False
        return _CMP[op](v_str.casefold(), c_pattern.casefold())

    return matcher


def match_criteria(val: Any, crit: Any) -> bool:
    """Evaluate an Excel/Calc condition (e.g. '>5', '<=10', '<>apple', '*item*', '=')."""
    return _parse_criterion(crit)(val)


def criterion_text(crit: Any) -> Any:
    """The text D* functions match. A criterion cell is read as text (doubleref.cxx passes the cell string to FillInExcelSyntax), and a boolean is "1" or "0"."""
    num = _to_float_or_none(crit) if isinstance(crit, _NUMERIC_TYPES) else None
    return crit if num is None else number_text(num)


def extract_numeric_array(
    *args: Any,
    ignore_bool: bool = True,
    propagate_nan: bool = True,
) -> np.ndarray:
    """Collect flat float array from arguments, filtering out non-numeric cells per Excel conventions."""
    vals: list[float] = []
    for arg in args:
        for x in np.asarray(arg, dtype=object).ravel():
            if ignore_bool and isinstance(x, (bool, np.bool_)):
                continue
            if isinstance(x, str):
                continue
            v = _to_float_or_none(x)
            if v is None or (not propagate_nan and math.isnan(v)):
                continue
            vals.append(v)
    return np.asarray(vals, dtype=float)


def criteria_reduce(
    values: Any,
    pairs: Sequence[tuple[Any, Any]],
    reduce: Callable[[list[float]], float],
) -> float | str | None:
    """First matched error, else ``reduce`` of the matched numbers; booleans are 1 or 0, text is skipped; None on a shape mismatch."""
    err = criterion_error(c for _r, c in pairs)
    if err is not None:
        return err
    v_arr = np.asarray(values, dtype=object)
    mask = multi_criteria_mask(pairs, base_shape=v_arr.shape)
    if mask is None:
        return None
    cells = v_arr.ravel()[mask]
    err = first_error(cells)
    return float(reduce(extract_numeric_array(cells, ignore_bool=False).tolist())) if err is None else err


def _complex_coeff(n: float) -> str:
    """Format complex coefficient without trailing .0 on integers."""
    return str(int(n)) if n.is_integer() else str(n)


def from_complex(c: complex, suffix: str = "i") -> str:
    """Convert Python complex to Calc string.

    ``Complex::GetString`` (``analysishelper.cxx``) calls ``finiteOrThrow``
    on both parts. Non-finite components raise ValueError instead of printing ``inf`` strings. macOS libm returns a non-finite complex for
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


def multi_criteria_mask(
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


