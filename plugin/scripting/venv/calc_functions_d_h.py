# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``calc``).

Spreadsheet import translates sheet functions to these helpers; where Calc and Excel differ they follow Calc (see calc_functions_util).
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import urllib.parse
from typing import Any, Callable

import numpy as np

from .calc_functions_t_z import vlookup
from .calc_functions_util import (
    DIV0_NAN,
    ILLEGAL_ARGUMENT_NAN,
    NESTED_ARRAY_NAN,
    RegressionError,
    VALUE_NAN,
    approx_floor,
    as_nonzero,
    coupon_schedule,
    criterion_text,
    date_to_serial,
    days360_dates,
    extract_numeric_array,
    finite_or_nan,
    first_coded_error,
    first_error,
    format_quantized,
    is_calc_error,
    is_criteria_blank,
    match_criteria,
    npf_result,
    propagate_errors,
    py_str,
    round_half_up,
    round_sig,
    scipy_stats,
    serial_to_date,
    trend_fit,
    whole_number,
    yearfrac,
)
from .coerce import header_label


__all__ = [
    "date",
    "datedif",
    "datevalue",
    "daverage",
    "days",
    "days360",
    "db",
    "dcount",
    "dcounta",
    "ddb",
    "decimal",
    "delta",
    "devsq",
    "dget",
    "disc",
    "dmax",
    "dmin",
    "dollar",
    "dollarde",
    "dollarfr",
    "dproduct",
    "dstdev",
    "dstdevp",
    "dsum",
    "duration",
    "dvar",
    "dvarp",
    "edate",
    "effect",
    "encodeurl",
    "eomonth",
    "erf",
    "erfc",
    "euroconvert",
    "even",
    "expondist",
    "fact",
    "factdouble",
    "fdist",
    "filter",
    "finv",
    "fisher",
    "fisherinv",
    "fixed",
    "forecast",
    "frequency",
    "fv",
    "fvschedule",
    "gamma",
    "gammadist",
    "gammainv",
    "gammaln",
    "gauss",
    "geomean",
    "gestep",
    "growth",
    "harmean",
    "hlookup",
    "hypgeomdist",
]


# (rate to EUR, display decimals)
_EURO: dict[str, tuple[float, int]] = {
    "EUR": (1.0, 2),
    "ATS": (13.7603, 2),
    "BEF": (40.3399, 0),
    "DEM": (1.95583, 2),
    "ESP": (166.386, 0),
    "FIM": (5.94573, 2),
    "FRF": (6.55957, 2),
    "IEP": (0.787564, 2),
    "ITL": (1936.27, 0),
    "LUF": (40.3399, 0),
    "NLG": (2.20371, 2),
    "PTE": (200.482, 0),
    "GRD": (340.750, 0),
    "SIT": (239.640, 2),
    "CYP": (0.585274, 2),
    "MTL": (0.429300, 2),
    "SKK": (30.1260, 2),
    "EEK": (15.6466, 2),
    "LVL": (0.702804, 2),
    "LTL": (3.45280, 2),
}


def _roll_ymd(year: int, month: int, day: int) -> dt.date:
    """Spill extra days into later months, matching LibreOffice ``Date::Normalize``.

    ``ScGetDateDif`` (``sc/source/core/tool/interpr2.cxx``) keeps the start day
    when it retargets the year or month, then ``Normalize()``
    (``comphelper/source/misc/date.cxx``). Feb 29 in a non-leap year becomes
    March 1. ``datetime.date`` raises ``ValueError`` instead of rolling.
    """
    dim = calendar.monthrange(year, month)[1]
    while day > dim:
        day -= dim
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        dim = calendar.monthrange(year, month)[1]
    return dt.date(year, month, day)


@propagate_errors
def date(year: Any, month: Any, day: Any) -> float:
    return date_to_serial(dt.date(whole_number(year), whole_number(month), whole_number(day)))


@propagate_errors
def datedif(start_date: Any, end_date: Any, unit: str = "D") -> float:
    sd = serial_to_date(start_date)
    ed = serial_to_date(end_date)
    if sd is None or ed is None or sd > ed:
        return float("nan")
    u = str(unit).strip('"').upper()
    # Month and day units follow ScGetDateDif (interpr2.cxx). A plain
    # month or day subtraction ignores an incomplete month and goes
    # negative across a year boundary; YD also built ``date(end.year,
    # start.month, start.day)`` outside the try, so a Feb 29 start in a
    # non-leap end year raised ValueError.
    try:
        months = (ed.year - sd.year) * 12 + ed.month - sd.month
        if sd.day > ed.day:
            months -= 1
        if u == "D":
            return float((ed - sd).days)
        if u == "M":
            return float(months)
        if u == "Y":
            return float(ed.year - sd.year - ((ed.month, ed.day) < (sd.month, sd.day)))
        if u == "MD":
            if sd.day <= ed.day:
                return float(ed.day - sd.day)
            # Borrow the previous month, keep the start day, then roll.
            if ed.month == 1:
                anchor = _roll_ymd(ed.year - 1, 12, sd.day)
            else:
                anchor = _roll_ymd(ed.year, ed.month - 1, sd.day)
            return float((ed - anchor).days)
        if u == "YM":
            return float(months % 12)
        if u == "YD":
            year = ed.year - ((ed.month, ed.day) < (sd.month, sd.day))
            anchor = _roll_ymd(year, sd.month, sd.day)
            return float((ed - anchor).days)
    except (ValueError, OverflowError):
        return float("nan")
    # ScGetDateDif (interpr2.cxx) returns the day count when the dates are
    # equal, before it checks the unit, so an unknown unit such as "X" is 0
    # then. With different dates an unknown unit is PushIllegalArgument (NaN).
    return 0.0 if sd == ed else float("nan")


@propagate_errors
def datevalue(text: Any) -> float:
    s = str(text).strip().strip('"')
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return date_to_serial(dt.datetime.strptime(s, fmt))
        except ValueError:
            continue
    return float("nan")


def _eval_d_criteria(db: Any, field: Any, criteria: Any) -> list[Any] | None:
    """Shared row match for D* functions. Values are the raw cells."""
    db_arr = np.asarray(db, dtype=object)
    if db_arr.ndim != 2 or db_arr.shape[0] == 0 or db_arr.shape[1] == 0:
        return None
    # First duplicate header wins, same as findFieldColumn (doubleref.cxx)
    # walking the header row left to right.
    header_index: dict[str, int] = {}
    for idx, cell in enumerate(db_arr[0]):
        header_index.setdefault(header_label(cell).upper(), idx)

    f_idx = -1
    # bool is an int subclass. int(True) - 1 would select column 0 when
    # the label "TRUE" is missing. A bool field is the header text.
    if field is not None and not (isinstance(field, (int, float)) and not isinstance(field, bool)):
        f_name = header_label(field).upper()
        if f_name in header_index:
            f_idx = header_index[f_name]
    if f_idx < 0 and field is not None and not isinstance(field, (str, bool)):
        try:
            f_idx = int(float(field)) - 1
        except (ValueError, TypeError, OverflowError):
            f_idx = -1

    if f_idx < 0 or f_idx >= db_arr.shape[1]:
        return None

    crit_arr = np.asarray(criteria, dtype=object)
    if crit_arr.ndim != 2:
        return None

    # lcl_createExcelQuery (doubleref.cxx) requires every criteria header
    # to name a database column. A miss makes createQueryParam return
    # nullptr, and DBIterator sets IllegalParameter. Skipping the column
    # must not match every row.
    crit_cols: list[int] = []
    for h in crit_arr[0]:
        col = header_index.get(header_label(h).upper())
        if col is None:
            return None
        crit_cols.append(col)

    matching_vals: list[Any] = []
    for r_idx in range(1, db_arr.shape[0]):
        row = db_arr[r_idx]
        if not any(
            all(
                # lcl_createExcelQuery (doubleref.cxx): an error criterion cell has no text, so it restricts nothing.
                is_criteria_blank(c) or is_calc_error(c) or match_criteria(row[crit_cols[j]], criterion_text(c))
                for j, c in enumerate(crit_arr[i])
            )
            for i in range(1, crit_arr.shape[0])
        ):
            continue
        matching_vals.append(row[f_idx])
    return matching_vals


def _dagg(
    database: Any,
    field: Any,
    criteria: Any,
    reduce: Callable[[list[Any]], Any],
    *,
    min_n: int = 0,
    empty: float = float("nan"),
) -> float | str:
    """Shared body of the numeric D* functions.

    ``None`` from ``_eval_d_criteria`` is an illegal database, field, or
    criteria header. An empty match is not that error: DSUM of no numbers
    is 0, DMAX of no numbers is 0, DAVERAGE of no numbers is NaN. ``min_n``
    is how many numbers ``reduce`` needs. ``np.prod([])`` is 1, so DPRODUCT
    uses ``min_n`` 1 and ``empty`` 0 instead of calling prod on nothing.
    """
    matched = _eval_d_criteria(database, field, criteria)
    if matched is None:
        return float("nan")
    # A matched error cell is the result (DSUM); a plain NaN is missing.
    err = first_coded_error(matched)
    if err is not None:
        return err
    # Strings are not numbers. ScDBQueryParamBase starts with
    # mbSkipString true (queryparam.cxx). DSUM uses that iterator.
    # DCOUNTA and DGET do not come through here, so text still counts
    # there. float("5") must not add the text. A bool stays: Calc stores
    # TRUE as a numeric cell, so float(True) is 1. NaN is missing
    # (is_missing_value) and must not poison the aggregate.
    vals: list[float] = []
    for val in matched:
        if isinstance(val, str):
            continue
        try:
            num = float(val)
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isnan(num):
            continue
        vals.append(num)
    if len(vals) < min_n:
        return empty
    return float(reduce(vals))


def daverage(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, np.mean, min_n=1)


@propagate_errors
def days(end_date: Any, start_date: Any) -> float:
    # DAYS truncates both serials to integers before subtracting. A fractional
    # serial is still that calendar day.
    return float(int(float(end_date)) - int(float(start_date)))


@propagate_errors
def days360(start_date: Any, end_date: Any, method: Any = False) -> float:
    sd = serial_to_date(start_date)
    ed = serial_to_date(end_date)
    if sd is None or ed is None:
        return float("nan")
    try:
        european = as_nonzero(method)
    except (ValueError, TypeError, OverflowError):
        # Calc shows #VALUE! for text other than TRUE/FALSE; this returns NaN (#NUM!), pinned by test_days360_method_is_numeric.
        return float("nan")
    return days360_dates(sd, ed, european=european)


@propagate_errors
def db(cost: Any, salvage: Any, life: Any, period: Any, month: Any = 12) -> float:
    # ScDB (interpr2.cxx): period is a double and month uses approxFloor;
    # salvage > cost, life > 1200, and period > life + 1 are Err:502.
    c = float(cost)
    s = float(salvage)
    life_val = float(life)
    per = float(period)
    m = approx_floor(float(month))
    if m < 1 or m > 12 or life_val > 1200 or s < 0 or per > life_val + 1 or s > c or c <= 0 or life_val <= 0 or per <= 0:
        return ILLEGAL_ARGUMENT_NAN
    # LibreOffice ScDB rounds the depreciation rate half away from zero with
    # approxFloor((fOffRate * 1000.0) + 0.5) / 1000.0 (interpr2.cxx). Python round()
    # is half-to-even, which rounded ties down.
    rate = float(round_half_up(1.0 - math.pow(s / c, 1.0 / life_val), 3))
    written = c * rate * m / 12.0
    if approx_floor(per) == 1:
        return written
    dep = 0.0
    for _idx in range(2, approx_floor(min(life_val, per)) + 1):
        dep = (c - written) * rate
        written += dep
    if per > life_val:
        dep = (c - written) * rate * (12 - m) / 12.0
    return dep


def dcount(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, len)


def dcounta(db: Any, field: Any, criteria: Any) -> float | str:
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None:
        return float("nan")
    err = first_coded_error(vals)
    if err is not None:
        return err
    return float(sum(1 for v in vals if v is not None and v != ""))


@propagate_errors
def ddb(cost: Any, salvage: Any, life: Any, period: Any, factor: Any = 2) -> float:
    # Excel and Calc: 1 <= period <= life, cost >= 0, salvage >= 0, life > 0,
    # factor > 0.
    c = float(cost)
    s = float(salvage)
    life_val = float(life)
    p = int(float(period))
    f = float(factor)
    if c < 0 or s < 0 or life_val <= 0 or f <= 0 or p < 1 or p > life_val:
        return float("nan")
    rate = f / life_val
    val = c
    dep = 0.0
    for _i in range(1, p + 1):
        dep = max(min(val * rate, val - s), 0.0)
        val -= dep
    return float(dep)


@propagate_errors
def decimal(text: Any, radix: Any) -> float:
    # Every character must be a digit for this radix. Python int() accepts
    # 0x, signs, and underscores; Excel and Calc DECIMAL do not.
    # ScDecimal (interpr2.cxx) takes GetString(). An integral numeric cell
    # arrives as 111.0, and str(111.0) is "111.0", so the dot failed the
    # digit check. The cell's text has no trailing ".0".
    r = approx_floor(float(radix))
    if r < 2 or r > 36:
        return ILLEGAL_ARGUMENT_NAN
    if isinstance(text, float) and math.isfinite(text) and text.is_integer():
        s = str(int(text))
    else:
        s = py_str(text).strip()
    if not s:
        return float("nan")
    valid_chars = "0123456789abcdefghijklmnopqrstuvwxyz"[:r]
    if any(c.lower() not in valid_chars for c in s):
        return float("nan")
    return float(int(s, r))


@propagate_errors
def delta(n1: Any, n2: Any = 0) -> float:
    return 1.0 if float(n1) == float(n2) else 0.0


@propagate_errors
def devsq(*args: Any) -> float:
    arr = extract_numeric_array(*args, propagate_nan=False)
    if not arr.size:
        return float("nan")
    # Overflow on huge inputs yields inf like LibreOffice; silence numpy's RuntimeWarning.
    with np.errstate(all="ignore"):
        return float(np.sum((arr - np.mean(arr)) ** 2))


def dget(db: Any, field: Any, criteria: Any) -> Any:
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None:
        return "#VALUE!"
    if len(vals) == 1:
        return vals[0]
    return "#NUM!" if len(vals) > 1 else "#VALUE!"


@propagate_errors
def disc(settlement: Any, maturity: Any, pr: Any, redemption: Any, basis: Any = 0) -> float:
    p = float(pr)
    red = float(redemption)
    b = int(float(basis))
    settle = float(settlement)
    mature = float(maturity)
    # getDisc throws when price or redemption is <= 0 or settlement >= maturity.
    # yearfrac swaps a reversed pair, so disc(45000, 44000, 95, 100) returned
    # the positive discount of the swapped dates. redemption == 0 was the only
    # price check, so a negative redemption also returned a number.
    if p <= 0 or red <= 0 or b < 0 or b > 4 or settle >= mature:
        return float("nan")
    yf = yearfrac(settle, mature, b)
    if math.isnan(yf) or yf <= 0:
        return float("nan")
    result = (red - p) / red / yf
    return finite_or_nan(float(result))


def dmax(db: Any, field: Any, criteria: Any) -> float | str:
    # No matching record is 0, not #NUM!. Excel and Calc DMAX do the same.
    return _dagg(db, field, criteria, np.max, min_n=1, empty=0.0)


def dmin(db: Any, field: Any, criteria: Any) -> float | str:
    # No matching record is 0, not #NUM!. Excel and Calc DMIN do the same.
    return _dagg(db, field, criteria, np.min, min_n=1, empty=0.0)


@propagate_errors
def dollar(number: Any, decimals: Any = 2) -> str | float:
    # The minus sits before the dollar sign (-$1,234.57), after half-up rounding.
    val = float(number)
    dec = int(float(decimals))
    if not math.isfinite(val):
        return float("nan")
    q = round_half_up(val, dec)
    magnitude = format_quantized(abs(q), dec, commas=True)
    return f"-${magnitude}" if q < 0 else f"${magnitude}"


def _dollar_frac(amount: Any, fraction: Any, *, to_decimal: bool) -> float:
    """DOLLARDE when ``to_decimal`` is true, otherwise DOLLARFR."""
    try:
        amt = float(amount)
        frac = float(fraction)
        if not math.isfinite(amt) or not math.isfinite(frac):
            return float("nan")
        f = int(frac)
    # float(10**400) raises OverflowError, not ValueError; overflow returns NaN
    # (#NUM!). Non-finite inputs return above, so math.floor below cannot see inf.
    except OverflowError:
        return float("nan")
    if f <= 0:
        return float("nan")
    sign = -1.0 if amt < 0 else 1.0
    amt = abs(amt)
    i_part = math.floor(amt)
    f_part = amt - i_part
    # Excel/Calc digit count is ceil(log10(f)). A fraction of 1 has scale 1.
    scale = 1 if f <= 1 else 10 ** math.ceil(math.log10(f))
    if to_decimal:
        part = (f_part * scale) / f
    else:
        part = (f_part * f) / scale
    return sign * (i_part + part)


@propagate_errors
def dollarde(fractional_dollar: Any, fraction: Any) -> float:
    return _dollar_frac(fractional_dollar, fraction, to_decimal=True)


@propagate_errors
def dollarfr(decimal_dollar: Any, fraction: Any) -> float:
    return _dollar_frac(decimal_dollar, fraction, to_decimal=False)


def dproduct(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, np.prod, min_n=1, empty=0.0)


def dstdev(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, lambda vals: np.std(vals, ddof=1), min_n=2)


def dstdevp(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, lambda vals: np.std(vals, ddof=0), min_n=1)


def dsum(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, np.sum)


@propagate_errors
def duration(settlement: Any, maturity: Any, coupon: Any, yld: Any, frequency: Any, basis: Any = 0) -> float:
    s = float(settlement)
    m = float(maturity)
    c = float(coupon)
    y = float(yld)
    f = float(frequency)
    b = int(float(basis))

    if c < 0 or y < 0 or f not in (1, 2, 4) or b < 0 or b > 4 or s >= m:
        return float("nan")

    try:
        _p_ser, _c_ser, days_in_per, k, _a_days, dsc_days = coupon_schedule(s, m, f, b)
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    n = int(k)
    if n <= 0 or days_in_per <= 0:
        return float("nan")

    frac = dsc_days / days_in_per
    yf = y / f
    cf = c * 100.0 / f

    if yf == 0:
        pv = float(n * cf + 100.0)
        if pv == 0:
            return float("nan")
        weighted_t = sum(((i - 1) + frac) * (cf + (100.0 if i == n else 0.0)) for i in range(1, n + 1))
        res = (weighted_t / pv) / f
    else:
        pv = 0.0
        weighted_t = 0.0
        for i in range(1, n + 1):
            t_i = (i - 1) + frac
            cf_i = cf + (100.0 if i == n else 0.0)
            disc = (1.0 + yf) ** t_i
            pv += cf_i / disc
            weighted_t += (t_i * cf_i) / disc
        if pv == 0:
            return float("nan")
        res = (weighted_t / pv) / f

    return finite_or_nan(float(res))


def dvar(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, lambda vals: np.var(vals, ddof=1), min_n=2)


def dvarp(db: Any, field: Any, criteria: Any) -> float | str:
    return _dagg(db, field, criteria, lambda vals: np.var(vals, ddof=0), min_n=1)


def _month_shift_serial(start_date: Any, months: Any, *, end_of_month: bool) -> float:
    sd = serial_to_date(start_date)
    if sd is None:
        return float("nan")
    month_delta = int(float(months))
    y, m = divmod(sd.year * 12 + sd.month - 1 + month_delta, 12)
    m += 1
    try:
        dim = calendar.monthrange(y, m)[1]
        d = dim if end_of_month else min(sd.day, dim)
        res_date = dt.date(y, m, d)
        return float(date_to_serial(res_date))
    except (ValueError, OverflowError):
        return float("nan")


@propagate_errors
def edate(start_date: Any, months: Any) -> float:
    # Month length comes from calendar.monthrange, including February in a leap year.
    return _month_shift_serial(start_date, months, end_of_month=False)


@propagate_errors
def effect(nominal_rate: Any, npery: Any) -> float:
    nr = float(nominal_rate)
    n_per = int(float(npery))
    # Excel EFFECT remarks and LibreOffice AnalysisAddIn::getEffect (scaddins/source/analysis/financial.cxx) both
    # reject nominal_rate <= 0 with #NUM!. The algebra at rate 0 is 0, but
    # that is not the spreadsheet result.
    if nr <= 0 or n_per < 1:
        return float("nan")
    # OverflowError is left to the worker exception path, which returns #NUM!
    res = (1 + nr / n_per) ** n_per - 1
    return finite_or_nan(res)


@propagate_errors
def encodeurl(text: Any) -> str | float:
    # ScEncodeURL (interpr7.cxx) pushes NoValue (#VALUE!) for an empty string.
    s = py_str(text)
    if not s:
        return VALUE_NAN
    return urllib.parse.quote(s, safe="")


@propagate_errors
def eomonth(start_date: Any, months: Any) -> float:
    # Last day of the month via serial_to_date and calendar.monthrange.
    return _month_shift_serial(start_date, months, end_of_month=True)


@propagate_errors
def erf(lower: Any, upper: Any | None = None) -> float:
    lo = float(lower)
    if upper is None:
        return math.erf(lo)
    return math.erf(float(upper)) - math.erf(lo)


@propagate_errors
def erfc(x: Any) -> float:
    return float(math.erfc(float(x)))


@propagate_errors
def euroconvert(value: Any, from_currency: Any, to_currency: Any, full_precision: Any = False, triangulation_precision: Any = None) -> float:
    val = float(value)
    from_curr = str(from_currency).upper().strip()
    to_curr = str(to_currency).upper().strip()

    if from_curr not in _EURO or to_curr not in _EURO:
        return float("nan")

    if from_curr == to_curr:
        return val

    if from_curr == "EUR":
        eur_val = val
    else:
        eur_val = val / _EURO[from_curr][0]
        if triangulation_precision is not None:
            try:
                sig_f = float(triangulation_precision)
                if not math.isfinite(sig_f) or not (3 <= sig_f <= 99) or int(sig_f) != sig_f:
                    return float("nan")
                sig = int(sig_f)
            except (ValueError, TypeError, OverflowError):
                return float("nan")
            eur_val = round_sig(eur_val, sig)

    if to_curr == "EUR":
        res = eur_val
    else:
        res = eur_val * _EURO[to_curr][0]

    try:
        is_full = as_nonzero(full_precision)
    except (ValueError, TypeError, OverflowError):
        # Calc shows #VALUE! for text other than TRUE/FALSE; this returns NaN (#NUM!).
        return float("nan")

    if not math.isfinite(res):
        return float("nan")

    if not is_full:
        # Half away from zero, same as FIXED and DOLLAR. Python round() is half-to-even.
        res = float(round_half_up(res, _EURO[to_curr][1]))
    return res


@propagate_errors
def even(n: Any) -> float:
    # EVEN rounds away from zero to the next even integer. Truncating toward
    # zero first returned the truncated value whenever it was already even,
    # so EVEN(2.5) was 2 and EVEN(-2.5) was -2.
    v = float(n)
    if not math.isfinite(v):
        return float("nan")
    if v >= 0:
        return float(math.ceil(v / 2.0) * 2)
    return float(math.floor(v / 2.0) * 2)


@propagate_errors
def expondist(x: Any, lambda_: Any, c: Any = 1) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    x_val = float(x)
    lam = float(lambda_)
    # ScExpDist reads the flag with GetDouble (interpr3.cxx), so a blank cell is
    # 0, the density. Calc requires the flag; the Python default 1 (cumulative)
    # is a convenience.
    cum = as_nonzero(c)
    # Lambda 0 would divide by zero, and Calc rejects lambda <= 0; a negative x is 0 in both forms,
    # as in ScExpDist (interpr3.cxx), and scipy gives that.
    if lam <= 0:
        return float("nan")
    if cum:
        return float(st.expon.cdf(x_val, scale=1.0 / lam))
    return float(st.expon.pdf(x_val, scale=1.0 / lam))


@propagate_errors
def fact(n: Any) -> float | str:
    # ScFact / Fakultaet (interpr3.cxx): < 0 is Err:502 before flooring, then approxFloor,
    # and > 170 pushes NoValue (#VALUE!).
    v = float(n)
    if v < 0:
        return ILLEGAL_ARGUMENT_NAN
    k = approx_floor(v)
    if k > 170:
        return "#VALUE!"
    return float(math.factorial(k))


@propagate_errors
def factdouble(n: Any) -> float:
    # n < 0 or n > 300 is #NUM!. LibreOffice AnalysisAddIn::getFactdouble (scaddins/source/analysis/analysis.cxx) caps FACTDOUBLE at 300;
    # a larger n walks down to 0 by twos.
    v = int(float(n))
    if v < 0 or v > 300:
        return float("nan")
    res = 1
    for i in range(v, 0, -2):
        res *= i
    return float(res)


@propagate_errors
def fdist(x: Any, r1: Any, r2: Any) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    x_val = float(x)
    # ScFDist (interpr3.cxx) floors df with approxFloor and rejects df < 1 or df >= 1e10 (Err:502).
    # st.f.sf of a negative x is 1, and df below 1 is finite; Calc returns Err:502.
    df1 = approx_floor(float(r1))
    df2 = approx_floor(float(r2))
    if x_val < 0 or df1 < 1 or df2 < 1 or df1 >= 10**10 or df2 >= 10**10:
        return ILLEGAL_ARGUMENT_NAN
    return float(st.f.sf(x_val, df1, df2))  # Calc returns right-tailed by default for FDIST


def filter(range_arr: Any, criteria: Any, if_empty: Any = NESTED_ARRAY_NAN) -> Any:
    # An (n, 1) include filters rows; a (1, m) include filters columns.
    # On a square range a length-n vector filters rows and a (1, n)
    # include filters columns.
    try:
        arr = np.asarray(range_arr)
        # dtype=object: a mixed list such as [1, ""] would otherwise become a text array.
        crit = np.asarray(criteria, dtype=object)
        # ScFilter (interpr1.cxx) checks 2-D include first (#VALUE!), then evaluates
        # include errors and checks for an empty match before checking shape (Err:504).
        # A two-dimensional include must be refused here because an empty mask would
        # otherwise select nothing and return if_empty.
        if arr.size == 0 or crit.size == 0 or (crit.ndim == 2 and min(crit.shape) > 1):
            return "#VALUE!"
        # ScFilter (interpr1.cxx) reads include in order: an error element is the result,
        # other non-empty text is an error (Calc's internal 536, shown here as #VALUE!),
        # and an empty cell is 0.
        include: list[bool] = []
        for x in crit.ravel():
            err = first_error([x])
            if err is not None:
                return err
            if isinstance(x, str) and x != "":
                return "#VALUE!"
            include.append(x is not None and x != "" and float(x) > 0)
        if not any(include):
            return if_empty
        row = (
            (arr.ndim == 1 and crit.size == arr.size)
            or (arr.ndim == 2 and (
                (crit.ndim == 1 and crit.size == arr.shape[0])
                or (crit.ndim == 2 and crit.shape == (arr.shape[0], 1))
            ))
        )
        col = arr.ndim == 2 and (
            (crit.ndim == 1 and crit.size == arr.shape[1])
            or (crit.ndim == 2 and crit.shape == (1, arr.shape[1]))
        )
        # ScFilter needs more than one include element along the filtered axis.
        if crit.size == 1 or not (row or col):
            return "Err:504"
        mask = np.asarray(include, dtype=bool)
        out = arr[mask] if row else arr[:, mask]
    except (ValueError, TypeError, IndexError, OverflowError):
        return "#VALUE!"
    return out.tolist()


@propagate_errors
def finv(p: Any, r1: Any, r2: Any) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    prob = float(p)
    # ScFInv (interpr3.cxx) floors df with approxFloor and rejects df < 1 or df >= 1e10 (Err:502).
    # It also rejects fP <= 0 and fP > 1; prob == 0 would otherwise return inf from isf.
    df1 = approx_floor(float(r1))
    df2 = approx_floor(float(r2))
    if prob <= 0 or prob > 1 or df1 < 1 or df2 < 1 or df1 >= 10**10 or df2 >= 10**10:
        return ILLEGAL_ARGUMENT_NAN
    result = float(st.f.isf(prob, df1, df2))
    return finite_or_nan(result)


@propagate_errors
def fisher(x: Any) -> float:
    # |x|>=1 is #NUM!. math.atanh raises ValueError (#VALUE!) at 1 and beyond.
    x_val = float(x)
    if x_val <= -1 or x_val >= 1:
        return float("nan")
    return float(math.atanh(x_val))


@propagate_errors
def fisherinv(y: Any) -> float:
    return float(math.tanh(float(y)))


@propagate_errors
def fixed(number: Any, decimals: Any = 2, no_commas: Any = False) -> str | float:
    val = float(number)
    dec = int(float(decimals))
    try:
        nc = as_nonzero(no_commas)
    except (ValueError, TypeError, OverflowError):
        # A text flag is #VALUE!, as in Calc.
        return "#VALUE!"
    # Non-finite input is #NUM!. round_half_up(inf) raises ValueError (#VALUE!).
    if not math.isfinite(val):
        return float("nan")
    # Half away from zero (2.5 -> 3, -2.5 -> -3). Python round() is half-to-even.
    return format_quantized(round_half_up(val, dec), dec, commas=not nc)


@propagate_errors
def forecast(x: Any, data_y: Any, data_x: Any) -> float:
    xv = float(x)
    y = np.asarray(data_y, dtype=float).ravel()
    x_arr = np.asarray(data_x, dtype=float).ravel()
    if y.size != x_arr.size or y.size < 2:
        return float("nan")
    avg_x = np.mean(x_arr)
    avg_y = np.mean(y)
    ss_xx = np.sum((x_arr - avg_x) ** 2)
    if ss_xx == 0:
        return float("nan")
    b = np.sum((x_arr - avg_x) * (y - avg_y)) / ss_xx
    a = avg_y - b * avg_x
    return float(a + b * xv)


@propagate_errors
def frequency(data: Any, bins: Any) -> Any:
    # Numeric cells only, bins sorted, counts from searchsorted, plus the
    # overflow bin. A text cell is skipped, not an empty result.
    data_arr = extract_numeric_array(data, propagate_nan=False)
    bins_arr = extract_numeric_array(bins, propagate_nan=False)
    sorted_bins = np.sort(bins_arr)
    indices = np.searchsorted(sorted_bins, data_arr, side="left")
    counts = np.bincount(indices, minlength=len(sorted_bins) + 1)
    return counts.tolist()


@propagate_errors
def fv(rate: Any, nper: Any, pmt_val: Any, pv_val: Any = 0, type_val: Any = 0) -> float:
    r = float(rate)
    n = float(nper)
    pm = float(pmt_val)
    p = float(pv_val)
    # Only type 1 is beginning-of-period. Any other type is end; the raw type
    # is never plugged into the annuity.
    t = 1 if int(float(type_val)) == 1 else 0
    return npf_result("fv", r, n, pm, p, t)


def fvschedule(principal: Any, schedule: Any) -> float:
    p = float(principal)
    sched = np.asarray(schedule).ravel()
    for rate in sched:
        p *= 1 + float(rate)
    return p


@propagate_errors
def gamma(x: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (gamma(200)) is #NUM! on that path.
    x_val = float(x)
    if x_val == 0 or (x_val < 0 and x_val.is_integer()):
        return float("nan")
    return float(math.gamma(x_val))


@propagate_errors
def gammadist(x: Any, alpha: Any, beta: Any, c: Any = 1) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    x_val = float(x)
    a = float(alpha)
    b = float(beta)
    # ScGammaDist reads a fourth argument with GetBool (interpr3.cxx), so a blank
    # cell is 0, the density. Only an omitted flag is cumulative.
    cum = as_nonzero(c)
    # A negative x is 0 in both forms (GetGammaDist, GetGammaDistPDF, interpr6.cxx);
    # only the Excel-named GAMMA.DIST rejects it.
    if a <= 0 or b <= 0:
        return float("nan")
    if cum:
        return float(st.gamma.cdf(x_val, a, scale=b))
    # GetGammaDistPDF (interpr6.cxx) sets DivisionByZero here.
    if x_val == 0 and a < 1:
        return DIV0_NAN
    return float(st.gamma.pdf(x_val, a, scale=b))


@propagate_errors
def gammainv(p: Any, alpha: Any, beta: Any) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    prob = float(p)
    a = float(alpha)
    b = float(beta)
    # LibreOffice ScGammaInv rejects fP < 0 or fP >= 1 (fP == 0 is 0; fP == 1 is
    # illegal). prob == 1 returned inf instead of nan because prob > 1 was checked.
    if prob < 0 or prob >= 1 or a <= 0 or b <= 0:
        return float("nan")
    result = float(st.gamma.ppf(prob, a, scale=b))
    return finite_or_nan(result)


@propagate_errors
def gammaln(x: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (lgamma(1e308)) is #NUM! on that path.
    # x <= 0 is #NUM!. math.lgamma of a negative non-integer is finite.
    x_val = float(x)
    if x_val <= 0:
        return float("nan")
    return float(math.lgamma(x_val))


@propagate_errors
def gauss(x: Any) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    return float(st.norm.cdf(float(x)) - 0.5)


@propagate_errors
def geomean(*args: Any) -> float:
    # exp(mean(log(arr))). Same result as scipy.stats.gmean, without the import.
    arr = extract_numeric_array(*args, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(np.exp(np.mean(np.log(arr))))


@propagate_errors
def gestep(number: Any, step: Any = 0) -> float:
    return 1.0 if float(number) >= float(step) else 0.0


def growth(known_y: Any, known_x: Any = None, new_x: Any = None, const: Any = True) -> Any:
    try:
        return trend_fit(known_y, known_x, new_x, const, log=True)
    except RegressionError as exc:
        return exc.args[0]
    except (ValueError, TypeError, OverflowError, ZeroDivisionError, np.linalg.LinAlgError):
        return "#VALUE!"


@propagate_errors
def harmean(*args: Any) -> float:
    # len(arr) / sum(1/arr). Same result as scipy.stats.hmean, without the import.
    arr = extract_numeric_array(*args, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(len(arr) / np.sum(1.0 / arr))


def hlookup(lookup_val: Any, table: Any, row_index: Any, is_sorted: Any = True) -> Any:
    return vlookup(lookup_val, np.atleast_2d(np.asarray(table, dtype=object)).T, row_index, is_sorted)


@propagate_errors
def hypgeomdist(x: Any, n_sample: Any, successes: Any, n_pop: Any) -> float:
    st = scipy_stats()
    if st is None:
        return float("nan")

    k = approx_floor(float(x))
    n = approx_floor(float(n_sample))
    K = approx_floor(float(successes))
    N = approx_floor(float(n_pop))
    # ScHypGeomDist (interpr3.cxx) rejects x < 0, n < x, N < n, N < M, M < 0 (Err:502);
    # an impossible draw is 0.0, and an empty population (N == 0) is 1.0 (scipy returns NaN).
    if k < 0 or n < k or N < n or N < K or K < 0:
        return ILLEGAL_ARGUMENT_NAN
    if n - k > N - K or k > K:
        return 0.0
    if N == 0:
        return 1.0
    return float(st.hypergeom.pmf(k, N, K, n))
