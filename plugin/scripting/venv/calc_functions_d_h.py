# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``calc``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from typing import Any, Callable

import numpy as np

from .calc_functions_a_c import _coupon_schedule
from .calc_functions_util import (
    _as_nonzero,
    _date_to_serial,
    _days360,
    _extract_numeric_array,
    _npf_result,
    _round_half_up,
    _round_sig,
    _scipy_stats,
    _serial_to_date,
    match_criteria,
    yearfrac,
)
from .coerce import header_label, is_missing_value


__all__ = [
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
    "hypgeomdist",
]


_EURO_RATES: dict[str, float] = {
    "EUR": 1.0,
    "ATS": 13.7603,
    "BEF": 40.3399,
    "DEM": 1.95583,
    "ESP": 166.386,
    "FIM": 5.94573,
    "FRF": 6.55957,
    "IEP": 0.787564,
    "ITL": 1936.27,
    "LUF": 40.3399,
    "NLG": 2.20371,
    "PTE": 200.482,
    "GRD": 340.750,
    "SIT": 239.640,
    "CYP": 0.585274,
    "MTL": 0.429300,
    "SKK": 30.1260,
    "EEK": 15.6466,
    "LVL": 0.702804,
    "LTL": 3.45280,
}

_EURO_DECIMALS: dict[str, int] = {
    "EUR": 2, "ATS": 2, "BEF": 0, "DEM": 2, "ESP": 0, "FIM": 2,
    "FRF": 2, "IEP": 2, "ITL": 0, "LUF": 0, "NLG": 2, "PTE": 0,
    "GRD": 0, "SIT": 2, "CYP": 2, "MTL": 2, "SKK": 2, "EEK": 2,
    "LVL": 2, "LTL": 2,
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


def datedif(start_date: Any, end_date: Any, unit: str = "D") -> float:
    sd = _serial_to_date(start_date)
    ed = _serial_to_date(end_date)
    if sd is None or ed is None or sd > ed:
        return float("nan")
    u = str(unit).strip('"').upper()
    # Month and day units follow ScGetDateDif (interpr2.cxx). A plain
    # month or day subtraction ignores an incomplete month and goes
    # negative across a year boundary; YD also built ``date(end.year,
    # start.month, start.day)`` outside the try, so a Feb 29 start in a
    # non-leap end year raised ValueError.
    try:
        if u == "D":
            return float((ed - sd).days)
        if u == "M":
            months = (ed.year - sd.year) * 12 + ed.month - sd.month
            if sd.day > ed.day:
                months -= 1
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
            months = (ed.year - sd.year) * 12 + ed.month - sd.month
            if sd.day > ed.day:
                months -= 1
            return float(months % 12)
        if u == "YD":
            if (ed.month, ed.day) >= (sd.month, sd.day):
                year = ed.year
            else:
                year = ed.year - 1
            anchor = _roll_ymd(year, sd.month, sd.day)
            return float((ed - anchor).days)
    except (ValueError, OverflowError):
        return float("nan")
    # ScGetDateDif (interpr2.cxx) returns the day count when the dates are
    # equal, before it checks the unit. Any other unknown unit is
    # PushIllegalArgument. The fall-through used to return the day count
    # for "X" as well.
    if sd == ed:
        return 0.0
    return float("nan")


def datevalue(text: Any) -> float:
    s = str(text).strip().strip('"')
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            parsed = dt.datetime.strptime(s, fmt)
            return _date_to_serial(parsed)
        except ValueError:
            continue
    return float("nan")


def _eval_d_criteria(db: Any, field: Any, criteria: Any, as_float: bool = True) -> list[Any] | None:
    """Shared helper for D* functions (DSUM, DAVERAGE, DMAX, DMIN, etc.)."""
    db_arr = np.asarray(db, dtype=object)
    if db_arr.ndim != 2 or db_arr.shape[0] == 0 or db_arr.shape[1] == 0:
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


def _dagg(
    database: Any,
    field: Any,
    criteria: Any,
    reduce: Callable[[list[Any]], Any],
    *,
    min_n: int = 0,
    empty: float = float("nan"),
) -> float:
    """Shared body of the numeric D* functions.

    ``None`` from ``_eval_d_criteria`` is an illegal database, field, or
    criteria header. An empty match is not that error: DSUM of no numbers
    is 0, DMAX of no numbers is 0, DAVERAGE of no numbers is NaN. ``min_n``
    is how many numbers ``reduce`` needs. ``np.prod([])`` is 1, so DPRODUCT
    uses ``min_n`` 1 and ``empty`` 0 instead of calling prod on nothing.
    """
    vals = _eval_d_criteria(database, field, criteria)
    if vals is None:
        return float("nan")
    if len(vals) < min_n:
        return empty
    return float(reduce(vals))


def daverage(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, np.mean, min_n=1)


def days(end_date: Any, start_date: Any) -> float:
    # DAYS truncates both serials to integers before subtracting. A fractional
    # serial is still that calendar day.
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    ed = int(float(end_date))
    sd = int(float(start_date))
    return float(ed - sd)


def days360(start_date: Any, end_date: Any, method: Any = False) -> float:
    sd = _serial_to_date(start_date)
    ed = _serial_to_date(end_date)
    if sd is None or ed is None:
        return float("nan")
    try:
        european = False if (method is None or method == "") else _as_nonzero(method)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    return _days360(sd, ed, european=european)


def db(cost: Any, salvage: Any, life: Any, period: Any, month: Any = 12) -> float:
    # 1 <= period <= life + 1, and cost, salvage, life, and month must be valid.
    # Period 0 is #NUM!, not 0.
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    c = float(cost)
    s = float(salvage)
    life_val = float(life)
    p = int(float(period))
    m = int(float(month))
    if c <= 0 or s < 0 or life_val <= 0 or m < 1 or m > 12:
        return float("nan")
    if p < 1 or p > life_val + 1:
        return float("nan")
    # LibreOffice ScDB rounds the depreciation rate half away from zero with
    # approxFloor((fOffRate * 1000.0) + 0.5) / 1000.0 (interpr2.cxx). Python round()
    # is half-to-even, which rounded ties down.
    rate = float(_round_half_up(1.0 - math.pow(s / c, 1.0 / life_val), 3))
    val = c
    dep = 0.0
    for i in range(1, p + 1):
        if i == 1:
            dep = val * rate * m / 12.0
        elif i == life_val + 1:
            dep = val * rate * (12 - m) / 12.0
        else:
            dep = val * rate
        val -= dep
    return float(dep)


def dcount(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, len)


def dcounta(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    return float(sum(1 for v in vals if v is not None and v != "")) if vals is not None else float("nan")


def ddb(cost: Any, salvage: Any, life: Any, period: Any, factor: Any = 2) -> float:
    # Excel and Calc: 1 <= period <= life, cost >= 0, salvage >= 0, life > 0,
    # factor > 0.
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
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
        dep = min(val * rate, val - s)
        if dep < 0:
            dep = 0.0
        val -= dep
    return float(dep)


def decimal(text: Any, radix: Any) -> float:
    # Every character must be a digit for this radix. Python int() accepts
    # 0x, signs, and underscores; Excel and Calc DECIMAL do not.
    # ScDecimal (interpr2.cxx) takes GetString(). An integral numeric cell
    # arrives as 111.0, and str(111.0) is "111.0", so the dot failed the
    # digit check. The cell's text has no trailing ".0".
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    r = int(float(radix))
    if r < 2 or r > 36:
        return float("nan")
    if isinstance(text, float) and math.isfinite(text) and text.is_integer():
        s = str(int(text))
    else:
        s = str(text).strip()
    if not s:
        return float("nan")
    valid_chars = "0123456789abcdefghijklmnopqrstuvwxyz"[:r]
    if any(c.lower() not in valid_chars for c in s):
        return float("nan")
    return float(int(s, r))


def delta(n1: Any, n2: Any = 0) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    return 1.0 if float(n1) == float(n2) else 0.0


def devsq(*args: Any) -> float:
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size:
        return float("nan")
    # Overflow on huge inputs yields inf like LibreOffice; silence numpy's RuntimeWarning.
    with np.errstate(all="ignore"):
        return float(np.sum((arr - np.mean(arr)) ** 2))


def dget(db: Any, field: Any, criteria: Any) -> Any:
    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    if vals is None:
        return "#VALUE!"
    if len(vals) == 1:
        return vals[0]
    return "#NUM!" if len(vals) > 1 else "#VALUE!"


def disc(settlement: Any, maturity: Any, pr: Any, redemption: Any, basis: Any = 0) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
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
    if not math.isfinite(result):
        return float("nan")
    return float(result)


def dmax(db: Any, field: Any, criteria: Any) -> float:
    # No matching record is 0, not #NUM!. Excel and Calc DMAX do the same.
    return _dagg(db, field, criteria, np.max, min_n=1, empty=0.0)


def dmin(db: Any, field: Any, criteria: Any) -> float:
    # No matching record is 0, not #NUM!. Excel and Calc DMIN do the same.
    return _dagg(db, field, criteria, np.min, min_n=1, empty=0.0)


def _format_rounded(val: float, decimals: int, *, commas: bool) -> str:
    """Format ``val`` after rounding half-up to ``decimals`` places."""
    # Half away from zero (2.5 -> 3, -2.5 -> -3). Python round() is half-to-even.
    places = max(0, decimals)
    q = _round_half_up(val, decimals)
    # Decimal('-0.00') formats as "-0.00". A rounded zero has no sign.
    # FIXED(-0.001, 2) was "-0.00". DOLLAR already hides it via q < 0.
    if q == 0:
        q = abs(q)
    if places == 0:
        return f"{int(q):,}" if commas else str(int(q))
    return f"{q:,.{places}f}" if commas else f"{q:.{places}f}"


def dollar(number: Any, decimals: Any = 2) -> str | float:
    # The minus sits before the dollar sign (-$1,234.57), after half-up rounding.
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf decimals) is #NUM! on that path.
    val = float(number)
    dec = int(float(decimals))
    if not math.isfinite(val):
        return float("nan")
    q = _round_half_up(val, dec)
    places = max(0, dec)
    if places == 0:
        s = f"{abs(int(q)):,}"
    else:
        s = f"{abs(q):,.{places}f}"
    return f"-${s}" if q < 0 else f"${s}"


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
        if not math.isfinite(amt) or not math.isfinite(frac):
            return None
        f = int(frac)
    # int(inf) and float(10**400) raise OverflowError, not ValueError.
    # math.floor(inf) used to sit outside this try, so dollarde(1.02, inf)
    # crashed. Overflow stays None so the caller returns NaN (#NUM!).
    # type errors are left to the worker exception path, which returns #VALUE!
    except OverflowError:
        return None
    if f <= 0 or not math.isfinite(amt) or not math.isfinite(frac):
        return None
    sign = -1.0 if amt < 0 else 1.0
    amt = abs(amt)
    i_part = math.floor(amt)
    f_part = amt - i_part
    scale = 10 ** _fractional_dollar_digits(f)
    return sign, float(i_part), f_part, f, scale


# Group B - Financial 2
def dollarde(fractional_dollar: Any, fraction: Any) -> float:
    terms = _dollar_fraction_terms(fractional_dollar, fraction)
    if terms is None:
        return float("nan")
    sign, i_part, f_part, f, scale = terms
    return sign * (i_part + (f_part * scale) / f)


def dollarfr(decimal_dollar: Any, fraction: Any) -> float:
    terms = _dollar_fraction_terms(decimal_dollar, fraction)
    if terms is None:
        return float("nan")
    sign, i_part, f_part, f, scale = terms
    return sign * (i_part + (f_part * f) / scale)


def dproduct(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, np.prod, min_n=1, empty=0.0)


def dstdev(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, lambda vals: np.std(vals, ddof=1), min_n=2)


def dstdevp(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, lambda vals: np.std(vals, ddof=0), min_n=1)


def dsum(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, np.sum)


def duration(settlement: Any, maturity: Any, coupon: Any, yld: Any, frequency: Any, basis: Any = 0) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    s = float(settlement)
    m = float(maturity)
    c = float(coupon)
    y = float(yld)
    f = float(frequency)
    b = int(float(basis))

    if c < 0 or y < 0 or f not in (1, 2, 4) or b < 0 or b > 4 or s >= m:
        return float("nan")

    try:
        _p_ser, _c_ser, days_in_per, k, _a_days, dsc_days = _coupon_schedule(s, m, f, b)
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

    return float(res) if math.isfinite(res) else float("nan")


def dvar(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, lambda vals: np.var(vals, ddof=1), min_n=2)


def dvarp(db: Any, field: Any, criteria: Any) -> float:
    return _dagg(db, field, criteria, lambda vals: np.var(vals, ddof=0), min_n=1)


def _shift_months(sd: dt.date, months: Any) -> tuple[int, int]:
    """Move ``sd`` by whole months."""
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    month_delta = int(float(months))
    y, m = sd.year, sd.month
    m += month_delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y, m


def edate(start_date: Any, months: Any) -> float:
    # Month length comes from calendar.monthrange, including February in a leap year.
    sd = _serial_to_date(start_date)
    if sd is None:
        return float("nan")
    y, m = _shift_months(sd, months)
    try:
        dim = calendar.monthrange(y, m)[1]
        d = min(sd.day, dim)
        res_date = dt.date(y, m, d)
        return float(_date_to_serial(res_date))
    except (ValueError, OverflowError):
        return float("nan")


def effect(nominal_rate: Any, npery: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    nr = float(nominal_rate)
    n_per = int(float(npery))
    # Excel EFFECT remarks and LibreOffice AnalysisAddIn::getEffect both
    # reject nominal_rate <= 0 with #NUM!. The algebra at rate 0 is 0, but
    # that is not the spreadsheet result.
    if nr <= 0 or n_per < 1:
        return float("nan")
    # OverflowError is left to the worker exception path, which returns #NUM!
    res = (1 + nr / n_per) ** n_per - 1
    return res if math.isfinite(res) else float("nan")


def encodeurl(text: Any) -> str:
    # type errors are left to the worker exception path, which returns #VALUE!
    import urllib.parse

    return urllib.parse.quote(str(text), safe="")


def eomonth(start_date: Any, months: Any) -> float:
    # Last day of the month via _serial_to_date and calendar.monthrange.
    sd = _serial_to_date(start_date)
    if sd is None:
        return float("nan")
    y, m = _shift_months(sd, months)
    try:
        dim = calendar.monthrange(y, m)[1]
        res_date = dt.date(y, m, dim)
        return float(_date_to_serial(res_date))
    except (ValueError, OverflowError):
        return float("nan")


def erf(lower: Any, upper: Any | None = None) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    lo = float(lower)
    if upper is None:
        return float(math.erf(lo))
    u = float(upper)
    return float(math.erf(u) - math.erf(lo))


def erfc(x: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    return float(math.erfc(float(x)))


def euroconvert(value: Any, from_currency: Any, to_currency: Any, full_precision: Any = False, triangulation_precision: Any = None) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    val = float(value)
    from_curr = str(from_currency).upper().strip()
    to_curr = str(to_currency).upper().strip()

    if from_curr not in _EURO_RATES or to_curr not in _EURO_RATES:
        return float("nan")

    if from_curr == to_curr:
        return val

    if from_curr == "EUR":
        eur_val = val
    else:
        eur_val = val / _EURO_RATES[from_curr]
        if triangulation_precision is not None:
            try:
                sig_f = float(triangulation_precision)
                if not math.isfinite(sig_f) or not (3 <= sig_f <= 99) or int(sig_f) != sig_f:
                    return float("nan")
                sig = int(sig_f)
            except (ValueError, TypeError, OverflowError):
                return float("nan")
            eur_val = _round_sig(eur_val, sig)

    if to_curr == "EUR":
        res = eur_val
    else:
        res = eur_val * _EURO_RATES[to_curr]

    try:
        is_full = False if (full_precision is None or full_precision == "") else _as_nonzero(full_precision)
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    if not math.isfinite(res):
        return float("nan")

    if not is_full:
        # Half away from zero, same as FIXED and DOLLAR. Python round() is half-to-even.
        res = float(_round_half_up(res, _EURO_DECIMALS[to_curr]))
    return res


def even(n: Any) -> float:
    # EVEN rounds away from zero to the next even integer. Truncating toward
    # zero first returned the truncated value whenever it was already even,
    # so EVEN(2.5) was 2 and EVEN(-2.5) was -2.
    # type errors are left to the worker exception path, which returns #VALUE!
    v = float(n)
    if not math.isfinite(v):
        return float("nan")
    if v >= 0:
        return float(math.ceil(v / 2.0) * 2)
    return float(math.floor(v / 2.0) * 2)


def expondist(x: Any, lambda_: Any, c: Any = 1) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    st = _scipy_stats()
    if st is None:
        return float("nan")

    x_val = float(x)
    lam = float(lambda_)
    try:
        cum = True if (c is None or c == "") else _as_nonzero(c)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # st.expon.pdf of a negative x is 0. scale=1/lam is ZeroDivisionError when lam is 0.
    # Excel EXPONDIST is #NUM! for both.
    if x_val < 0 or lam <= 0:
        return float("nan")
    if cum:
        return float(st.expon.cdf(x_val, scale=1.0 / lam))
    return float(st.expon.pdf(x_val, scale=1.0 / lam))


def fact(n: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    v = float(n)
    if v < 0 or v > 170:  # math.factorial limit
        return float("nan")
    return float(math.factorial(int(v)))


def factdouble(n: Any) -> float:
    # n < 0 or n > 300 is #NUM!. LibreOffice ScInterpreter caps FACTDOUBLE at 300;
    # a larger n walks down to 0 by twos.
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    v = int(float(n))
    if v < 0 or v > 300:
        return float("nan")
    res = 1
    for i in range(v, 0, -2):
        res *= i
    return float(res)


def fdist(x: Any, r1: Any, r2: Any) -> float:
    st = _scipy_stats()
    if st is None:
        return float("nan")

    x_val = float(x)
    df1 = float(r1)
    df2 = float(r2)
    # st.f.sf of a negative x is 1, and df below 1 is finite. Excel is #NUM!.
    if x_val < 0 or df1 < 1 or df2 < 1:
        return float("nan")
    return float(st.f.sf(x_val, df1, df2))  # Calc returns right-tailed by default for FDIST


def filter(range_arr: Any, criteria: Any, if_empty: Any | None = None) -> Any:
    # An (n, 1) include filters rows; a (1, m) include filters columns.
    # Treating either as an elementwise mask raises IndexError.
    try:
        arr = np.asarray(range_arr)
        crit = np.asarray(criteria)
        if arr.ndim == 1:
            crit_flat = crit.ravel()
            if len(crit_flat) != len(arr):
                return "#VALUE!"
            mask = np.asarray([_as_nonzero(x) for x in crit_flat])
            out = arr[mask]
        elif arr.ndim == 2:
            if crit.ndim == 1:
                if len(crit) == arr.shape[0]:
                    mask = np.asarray([_as_nonzero(x) for x in crit])
                    out = arr[mask]
                elif len(crit) == arr.shape[1]:
                    mask = np.asarray([_as_nonzero(x) for x in crit])
                    out = arr[:, mask]
                else:
                    return "#VALUE!"
            elif crit.ndim == 2:
                if crit.shape == (arr.shape[0], 1):
                    mask = np.asarray([_as_nonzero(x) for x in crit.ravel()])
                    out = arr[mask]
                elif crit.shape == (1, arr.shape[1]):
                    mask = np.asarray([_as_nonzero(x) for x in crit.ravel()])
                    out = arr[:, mask]
                elif crit.shape == arr.shape:
                    mask = np.asarray([_as_nonzero(x) for x in crit.ravel()]).reshape(crit.shape)
                    out = arr[mask]
                else:
                    return "#VALUE!"
            else:
                return "#VALUE!"
        else:
            return "#VALUE!"
    except (ValueError, TypeError, IndexError, OverflowError):
        return "#VALUE!"
    if out.size == 0:
        return if_empty
    return out.tolist() if out.ndim > 1 else out.ravel().tolist()


def finv(p: Any, r1: Any, r2: Any) -> float:
    st = _scipy_stats()
    if st is None:
        return float("nan")

    prob = float(p)
    df1 = float(r1)
    df2 = float(r2)
    # LibreOffice ScFInv rejects fP <= 0 and fP > 1 (sc/source/core/tool/interpr3.cxx).
    # prob == 0 returned inf instead of nan because prob < 0 was checked instead of prob <= 0.
    if prob <= 0 or prob > 1 or df1 < 1 or df2 < 1:
        return float("nan")
    result = float(st.f.isf(prob, df1, df2))
    if not math.isfinite(result):
        return float("nan")
    return result


def fisher(x: Any) -> float:
    # |x|>=1 is #NUM!. math.atanh raises ValueError (#VALUE!) at 1 and beyond.
    # type errors are left to the worker exception path, which returns #VALUE!
    x_val = float(x)
    if x_val <= -1 or x_val >= 1:
        return float("nan")
    return float(math.atanh(x_val))


def fisherinv(y: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    return float(math.tanh(float(y)))


def fixed(number: Any, decimals: Any = 2, no_commas: Any = False) -> str | float:
    val = float(number)
    dec = int(float(decimals))
    try:
        nc = False if (no_commas is None or no_commas == "") else _as_nonzero(no_commas)
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"
    # Non-finite input is #NUM!. _round_half_up(inf) raises ValueError (#VALUE!).
    if not math.isfinite(val):
        return float("nan")
    return _format_rounded(val, dec, commas=not nc)


def forecast(x: Any, data_y: Any, data_x: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
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


def frequency(data: Any, bins: Any) -> Any:
    # Numeric cells only, bins sorted, counts from searchsorted, plus the
    # overflow bin. A text cell is skipped, not an empty result.
    try:
        data_arr = _extract_numeric_array(data, ignore_text=True, ignore_bool=True, propagate_nan=False)
        bins_arr = _extract_numeric_array(bins, ignore_text=True, ignore_bool=True, propagate_nan=False)
        if bins_arr.size == 0:
            return [int(data_arr.size)]
        sorted_bins = np.sort(bins_arr)
        if data_arr.size == 0:
            return [0] * (len(sorted_bins) + 1)
        indices = np.searchsorted(sorted_bins, data_arr, side="left")
        counts = np.bincount(indices, minlength=len(sorted_bins) + 1)
        return [int(c) for c in counts[: len(sorted_bins) + 1]]
    # Broad except Exception returned [] on conversion failures instead of #VALUE!.
    # Catch conversion errors and return "#VALUE!" while letting runtime errors propagate.
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"


def fv(rate: Any, nper: Any, pmt_val: Any, pv_val: Any = 0, type_val: Any = 0) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    r = float(rate)
    n = float(nper)
    pm = float(pmt_val)
    p = float(pv_val)
    # Only type 1 is beginning-of-period. Any other type is end, matching
    # the old branch (it did not plug the raw type into the annuity).
    t = 1 if int(float(type_val)) == 1 else 0
    return _npf_result("fv", r, n, pm, p, t)


def fvschedule(principal: Any, schedule: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    p = float(principal)
    sched = np.asarray(schedule).ravel()
    for rate in sched:
        p *= 1 + float(rate)
    return p


def gamma(x: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (gamma(200)) is #NUM! on that path.
    x_val = float(x)
    if x_val == 0 or (x_val < 0 and x_val.is_integer()):
        return float("nan")
    return float(math.gamma(x_val))


def gammadist(x: Any, alpha: Any, beta: Any, c: Any = 1) -> float:
    st = _scipy_stats()
    if st is None:
        return float("nan")

    x_val = float(x)
    a = float(alpha)
    b = float(beta)
    try:
        cum = True if (c is None or c == "") else _as_nonzero(c)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # st.gamma.pdf of a negative x is 0. Excel is #NUM!.
    if x_val < 0 or a <= 0 or b <= 0:
        return float("nan")
    if cum:
        return float(st.gamma.cdf(x_val, a, scale=b))
    return float(st.gamma.pdf(x_val, a, scale=b))


def gammainv(p: Any, alpha: Any, beta: Any) -> float:
    st = _scipy_stats()
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
    if not math.isfinite(result):
        return float("nan")
    return result


def gammaln(x: Any) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!. OverflowError (lgamma(1e308)) is #NUM! on that path.
    # x <= 0 is #NUM!. math.lgamma of a negative non-integer is finite.
    x_val = float(x)
    if x_val <= 0:
        return float("nan")
    return float(math.lgamma(x_val))


def gauss(x: Any) -> float:
    st = _scipy_stats()
    if st is None:
        return float("nan")

    return float(st.norm.cdf(float(x)) - 0.5)


def geomean(*args: Any) -> float:
    # exp(mean(log(arr))). Same result as scipy.stats.gmean, without the import.
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(np.exp(np.mean(np.log(arr))))


def gestep(number: Any, step: Any = 0) -> float:
    # type errors are left to the worker exception path, which returns #VALUE!
    return 1.0 if float(number) >= float(step) else 0.0


def growth(known_y: Any, known_x: Any = None, new_x: Any = None, const: Any = True) -> Any:
    try:
        y = np.asarray(known_y, dtype=float).ravel()
        # Excel/Calc GROWTH returns #NUM! when any known y is <= 0 or non-finite.
        # Returning [] was wrong because Excel/Calc errors are string tokens, and
        # non-finite values like inf were not caught by <= 0.
        if y.size and (not np.all(np.isfinite(y)) or np.any(y <= 0)):
            return "#NUM!"
        if known_x is None:
            x = np.arange(1, len(y) + 1, dtype=float)
        else:
            x = np.asarray(known_x, dtype=float).ravel()
        if new_x is None:
            new_x_arr = x
        else:
            new_x_arr = np.asarray(new_x, dtype=float).ravel()

        y_log = np.log(y)
        # A text "0" is truthy in Python, so GROWTH fitted an intercept.
        # expondist and fixed coerce with bool(float()). "FALSE" raises
        # and the handler below returns "#VALUE!".
        use_const = True if (const is None or const == "") else _as_nonzero(const)
        if use_const:
            coeffs = np.polyfit(x, y_log, 1)
            res = np.exp(np.polyval(coeffs, new_x_arr))
        else:
            slope = np.sum(x * y_log) / np.sum(x * x)
            res = np.exp(slope * new_x_arr)
        return res.tolist()
    # Broad except Exception returned [] on failures instead of #VALUE!.
    # Narrow to expected conversion, linear algebra, and calculation exceptions.
    except (ValueError, TypeError, OverflowError, ZeroDivisionError, np.linalg.LinAlgError):
        return "#VALUE!"


def harmean(*args: Any) -> float:
    # len(arr) / sum(1/arr). Same result as scipy.stats.hmean, without the import.
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(len(arr) / np.sum(1.0 / arr))


def hypgeomdist(x: Any, n_sample: Any, successes: Any, n_pop: Any) -> float:
    st = _scipy_stats()
    if st is None:
        return float("nan")

    k = int(float(x))
    n = int(float(n_sample))
    K = int(float(successes))
    N = int(float(n_pop))
    # scipy returns 0 for an impossible draw. Excel is #NUM!.
    if k < 0 or k > n or k > K or k < n - N + K or n < 0 or n > N or K < 0 or K > N or N < 0:
        return float("nan")
    return float(st.hypergeom.pmf(k, N, K, n))
