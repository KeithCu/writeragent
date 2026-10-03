# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from typing import Any

import numpy as np

from .calc_functions_util import _dollar_fraction_terms, _extract_numeric_array, _npf_result


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
    try:
        sd = dt.date.fromordinal(int(float(start_date)) + 693594)
        ed = dt.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")
    if sd > ed:
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
    return float((ed - sd).days)


def datevalue(text: Any) -> float:
    s = str(text).strip().strip('"')
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            parsed = dt.datetime.strptime(s, fmt)
            return float(parsed.toordinal() - 693594)
        except ValueError:
            continue
    return float("nan")


def daverage(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.mean(vals)) if vals else float("nan")


def days(end_date: Any, start_date: Any) -> float:
    try:
        ed = float(end_date)
        sd = float(start_date)
        return float(ed - sd)
    except (ValueError, TypeError):
        return float("nan")


def days360(start_date: Any, end_date: Any, method: Any = False) -> float:
    try:
        sd = dt.date.fromordinal(int(float(start_date)) + 693594)
        ed = dt.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")

    d1, m1, y1 = sd.day, sd.month, sd.year
    d2, m2, y2 = ed.day, ed.month, ed.year

    if bool(method):  # European: both the 31st start and the 31st end become the 30th.
        if d1 == 31:
            d1 = 30
        if d2 == 31:
            d2 = 30
    else:
        # US (NASD). A start on the 31st, or on the last day of February,
        # is day 30. The old branch only rewrote day 31, so
        # DAYS360(2023-02-28, 2023-03-01) was 3. Excel and Calc both return
        # 1 (Apache POI Days360.getStartingDate; Calc agrees, including
        # that 2024-02-28 is not month-end in a leap year and stays 3).
        # An end date in February is not rewritten; only a 31st end date
        # becomes the 30th when the adjusted start day is already 30.
        if d1 == 31 or (m1 == 2 and d1 == calendar.monthrange(y1, m1)[1]):
            d1 = 30
        if d2 == 31 and d1 == 30:
            d2 = 30

    return float((y2 - y1) * 360 + (m2 - m1) * 30 + (d2 - d1))


def db(cost: Any, salvage: Any, life: Any, period: Any, month: Any = 12) -> float:
    try:
        c = float(cost)
        s = float(salvage)
        life_val = float(life)
        p = int(float(period))
        m = int(float(month))
        if c == 0 or life_val == 0:
            return 0.0
        # Period life+1 is the partial final year (month < 12) or a zero
        # stub (month == 12). Calc returns Err:502 (#NUM!) only past that
        # stub — DB(1000000, 100000, 6, 8, 7) and DB(10000, 1000, 5, 7, 12)
        # — and still returns the stub itself (DB(..., 6, 7, 7) is the
        # partial year). Looping further kept depreciating a spent asset.
        if life_val > 0 and p > life_val + 1:
            return float("nan")
        rate = round(1.0 - math.pow(s / c, 1.0 / life_val), 3)
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
    except Exception:
        return float("nan")


def dcount(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(len(vals))


def dcounta(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    return float(sum(1 for v in vals if v is not None and v != ""))


def ddb(cost: Any, salvage: Any, life: Any, period: Any, factor: Any = 2) -> float:
    try:
        c = float(cost)
        s = float(salvage)
        life_val = float(life)
        p = int(float(period))
        f = float(factor)
        rate = f / life_val
        val = c
        dep = 0.0
        for _i in range(1, p + 1):
            dep = min(val * rate, val - s)
            if dep < 0:
                dep = 0.0
            val -= dep
        return float(dep)
    except Exception:
        return float("nan")


def decimal(text: Any, radix: Any) -> float:
    try:
        r = int(float(radix))
        if r < 2 or r > 36:
            return float("nan")
        return float(int(str(text), r))
    except Exception:
        return float("nan")


def delta(n1: Any, n2: Any = 0) -> float:
    try:
        return 1.0 if float(n1) == float(n2) else 0.0
    except (ValueError, TypeError):
        return float("nan")


def devsq(*args: Any) -> float:
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size:
        return float("nan")
    return float(np.sum((arr - np.mean(arr)) ** 2))


def dget(db: Any, field: Any, criteria: Any) -> Any:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    if len(vals) == 1:
        return vals[0]
    return "#NUM!" if len(vals) > 1 else "#VALUE!"


def disc(settlement: Any, maturity: Any, pr: Any, redemption: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_t_z import yearfrac

    try:
        p = float(pr)
        red = float(redemption)
        yf = yearfrac(settlement, maturity, basis)
        # A zero redemption is #NUM! in Calc (Err:502). Dividing by red
        # raises ZeroDivisionError today and the blanket except turns that
        # into nan, but the #NUM! result must not depend on that accident.
        if math.isnan(yf) or yf == 0 or red == 0:
            return float("nan")
        return float((red - p) / red / yf)
    except Exception:
        return float("nan")


def dmax(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.max(vals)) if vals else float("nan")


def dmin(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.min(vals)) if vals else float("nan")


def _format_rounded(val: float, decimals: int, *, commas: bool) -> str:
    """Format ``val`` after rounding to ``decimals`` places.

    Negative ``decimals`` round left of the decimal point. Clamping the
    format width with ``max(0, decimals)`` *before* rounding dropped that
    step, so DOLLAR(12345, -2) stayed ``$12,345`` instead of ``$12,300``.
    """
    places = max(0, decimals)
    rounded = round(val, decimals)
    if commas:
        return f"{rounded:,.{places}f}"
    return f"{rounded:.{places}f}"


def dollar(number: Any, decimals: Any = 2) -> str | float:
    try:
        val = float(number)
        dec = int(float(decimals))
        if math.isnan(val):
            return float("nan")
        return f"${_format_rounded(val, dec, commas=True)}"
    except (ValueError, TypeError):
        return float("nan")


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
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.prod(vals)) if vals else 0.0


def dstdev(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.std(vals, ddof=1)) if len(vals) > 1 else float("nan")


def dstdevp(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.std(vals, ddof=0)) if vals else float("nan")


def dsum(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.sum(vals))


def duration(settlement: Any, maturity: Any, coupon: Any, yld: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _year_frac

    # Macaulay Duration approximation
    try:
        s = float(settlement)
        m = float(maturity)
        c = float(coupon)
        y = float(yld)
        f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")
    if c < 0 or y < 0 or f not in (1, 2, 4) or b < 0 or b > 4 or s >= m:
        return float("nan")

    # Calculate complete coupon periods
    # duration = (1 + y/f) / (y/f) - (1 + y/f + n*(c/f - y/f)) / ((c/f)*((1+y/f)**n - 1) + y/f)
    # Actually, let's use the closed-form for Macaulay duration of a bond on coupon date:
    # Since we need exact day counting, we will use a simpler approximation if it's not a coupon date,
    # but the closed form is generally expected.
    # We will implement the standard closed form for exact periods.
    periods = _year_frac(s, m, b) * f
    n = periods  # approx number of periods
    if n <= 0:
        return float("nan")

    # Using Macaulay duration formula
    # MacD = (1 + y/f)/ (y/f) - (1 + y/f + n*(c/f - y/f)) / ( (c/f) * ((1+y/f)**n - 1) + y/f )
    # ModD = MacD / (1 + y/f)
    yf = y / f
    cf = c / f
    if yf == 0:
        # The closed form divides by y/f. At a zero yield Calc's cash-flow
        # sum (analysishelper.cxx GetDuration) discounts by (1+y/f)**t = 1,
        # so the limit is finite: n/f for a zero coupon, and
        # n*(cf*(n+1)+2)/(2*f*(cf*n+1)) when the bond pays a coupon.
        # Returning nan here dropped both, including the zero-coupon n/f.
        denom = cf * n + 1.0
        if denom == 0:
            return float("nan")
        return n * (cf * (n + 1.0) + 2.0) / (2.0 * f * denom)
    if cf == 0:
        macd = n / f
    else:
        macd = ((1 + yf) / yf - (1 + yf + n * (cf - yf)) / (cf * ((1 + yf) ** n - 1) + yf)) / f

    return macd


def dvar(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.var(vals, ddof=1)) if len(vals) > 1 else float("nan")


def dvarp(db: Any, field: Any, criteria: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _eval_d_criteria

    vals = _eval_d_criteria(db, field, criteria)
    return float(np.var(vals, ddof=0)) if vals else float("nan")


def edate(start_date: Any, months: Any) -> float:
    try:
        date_val = dt.date.fromordinal(int(float(start_date)) + 693594)
        # months sat outside this try, so a blank or text months cell raised.
        month_delta = int(float(months))
    except Exception:
        return float("nan")
    y, m = date_val.year, date_val.month
    m += month_delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = min(date_val.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return float(dt.date(y, m, d).toordinal() - 693594)


def effect(nominal_rate: Any, npery: Any) -> float:
    try:
        nr = float(nominal_rate)
        np = int(float(npery))
    except (ValueError, TypeError):
        return float("nan")
    # Excel EFFECT remarks and LibreOffice AnalysisAddIn::getEffect both
    # reject nominal_rate <= 0 with #NUM!. The algebra at rate 0 is 0, but
    # that is not the spreadsheet result.
    if nr <= 0 or np < 1:
        return float("nan")
    return (1 + nr / np) ** np - 1


def encodeurl(text: Any) -> str | float:
    try:
        import urllib.parse

        return urllib.parse.quote(str(text), safe="")
    except (ValueError, TypeError):
        return float("nan")


def eomonth(start_date: Any, months: Any) -> float:
    try:
        date_val = dt.date.fromordinal(int(float(start_date)) + 693594)
        # months sat outside this try, so a blank or text months cell raised.
        month_delta = int(float(months))
    except Exception:
        return float("nan")
    y, m = date_val.year, date_val.month
    m += month_delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    if m == 12:
        next_month = dt.date(y + 1, 1, 1)
    else:
        next_month = dt.date(y, m + 1, 1)
    last_day = next_month - dt.timedelta(days=1)
    return float(last_day.toordinal() - 693594)


def erf(lower: Any, upper: Any | None = None) -> float:
    try:
        lo = float(lower)
        if upper is None:
            return float(math.erf(lo))
        u = float(upper)
        return float(math.erf(u) - math.erf(lo))
    except (ValueError, TypeError):
        return float("nan")


def erfc(x: Any) -> float:
    try:
        return float(math.erfc(float(x)))
    except (ValueError, TypeError):
        return float("nan")


def euroconvert(value: Any, from_currency: Any, to_currency: Any, full_precision: Any = False, triangulation_precision: Any = None) -> float:
    try:
        val = float(value)
        from_curr = str(from_currency).upper().strip()
        to_curr = str(to_currency).upper().strip()
    except (ValueError, TypeError):
        return float("nan")

    rates = {
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

    if from_curr not in rates or to_curr not in rates:
        return float("nan")

    decimals = {"EUR": 2, "ATS": 2, "BEF": 0, "DEM": 2, "ESP": 0, "FIM": 2, "FRF": 2, "IEP": 2, "ITL": 0, "LUF": 0, "NLG": 2, "PTE": 0, "GRD": 0, "SIT": 2, "CYP": 2, "MTL": 2, "SKK": 2, "EEK": 2, "LVL": 2, "LTL": 2}

    if from_curr == to_curr:
        return val

    def round_sig(x: float, sig: int) -> float:
        if x == 0:
            return 0.0
        import math

        exponent = math.floor(math.log10(abs(x)))
        factor = 10 ** (sig - 1 - exponent)
        return round(x * factor) / factor

    if from_curr == "EUR":
        eur_val = val
    else:
        eur_val = val / rates[from_curr]
        if triangulation_precision is not None:
            try:
                sig = int(float(triangulation_precision))
                if sig < 3:
                    return float("nan")
                eur_val = round_sig(eur_val, sig)
            except (ValueError, TypeError):
                return float("nan")

    if to_curr == "EUR":
        res = eur_val
    else:
        res = eur_val * rates[to_curr]

    is_full = False
    if isinstance(full_precision, bool):
        is_full = full_precision
    else:
        try:
            is_full = bool(float(full_precision))
        except (ValueError, TypeError):
            is_full = False

    if not is_full:
        res = round(res, decimals[to_curr])
        if decimals[to_curr] == 0:
            res = float(int(res))
    return float(res)


def even(n: Any) -> float:
    # EVEN rounds away from zero to the next even integer. Truncating toward
    # zero first returned the truncated value whenever it was already even,
    # so EVEN(2.5) was 2 and EVEN(-2.5) was -2. Non-numeric input raised.
    try:
        v = float(n)
    except (ValueError, TypeError):
        return float("nan")
    if not math.isfinite(v):
        return float("nan")
    if v >= 0:
        return float(math.ceil(v / 2.0) * 2)
    return float(math.floor(v / 2.0) * 2)


def expondist(x: Any, lambda_: Any, c: Any = 1) -> float:
    try:
        import scipy.stats as st  # type: ignore[import-untyped]

        x_val = float(x)
        lam = float(lambda_)
        cum = bool(float(c))
        if x_val < 0 or lam <= 0:
            return float("nan")
        if cum:
            return float(st.expon.cdf(x_val, scale=1.0 / lam))
        return float(st.expon.pdf(x_val, scale=1.0 / lam))
    except (ValueError, TypeError, ImportError):
        return float("nan")


def fact(n: Any) -> float:
    try:
        v = float(n)
        if v < 0 or v > 170:  # math.factorial limit
            return float("nan")
        return float(math.factorial(int(v)))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def factdouble(n: Any) -> float:
    try:
        v = int(float(n))
        if v < 0:
            return float("nan")
        res = 1
        for i in range(v, 0, -2):
            res *= i
        return float(res)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def fdist(x: Any, r1: Any, r2: Any) -> float:
    try:
        import scipy.stats as st

        x_val = float(x)
        df1 = float(r1)
        df2 = float(r2)
        if x_val < 0 or df1 < 1 or df2 < 1:
            return float("nan")
        return float(st.f.sf(x_val, df1, df2))  # Calc returns right-tailed by default for FDIST
    except (ValueError, TypeError, ImportError):
        return float("nan")


def filter(range_arr: Any, criteria: Any, if_empty: Any | None = None) -> Any:
    # A short include was sliced down to the range, and a column of flags was
    # used as a 2-D boolean index. Both make NumPy raise IndexError
    # ("boolean index did not match"). That is a value error, not a traceback.
    try:
        arr = np.asarray(range_arr)
        crit = np.asarray(criteria)
        if arr.ndim == 1:
            mask = np.asarray([bool(x) for x in crit.ravel()[: len(arr)]])
            out = arr.ravel()[mask]
        else:
            if crit.ndim == 1:
                mask = np.asarray([bool(x) for x in crit.ravel()[: arr.shape[0]]])
                out = arr[mask]
            else:
                mask = crit.astype(bool)
                out = arr[mask]
    except (ValueError, TypeError, IndexError):
        return "#VALUE!"
    if out.size == 0:
        return if_empty
    return out.tolist() if out.ndim > 1 else out.ravel().tolist()


def finv(p: Any, r1: Any, r2: Any) -> float:
    try:
        import scipy.stats as st

        prob = float(p)
        df1 = float(r1)
        df2 = float(r2)
        if prob < 0 or prob > 1 or df1 < 1 or df2 < 1:
            return float("nan")
        return float(st.f.isf(prob, df1, df2))
    except (ValueError, TypeError, ImportError):
        return float("nan")


def fisher(x: Any) -> float:
    try:
        import math

        x_val = float(x)
        if x_val <= -1 or x_val >= 1:
            return float("nan")
        return float(math.atanh(x_val))
    except (ValueError, TypeError):
        return float("nan")


def fisherinv(y: Any) -> float:
    try:
        import math

        return float(math.tanh(float(y)))
    except (ValueError, TypeError):
        return float("nan")


def fixed(number: Any, decimals: Any = 2, no_commas: Any = False) -> str | float:
    try:
        val = float(number)
        dec = int(float(decimals))
        nc = bool(float(no_commas))
        if math.isnan(val):
            return float("nan")
        return _format_rounded(val, dec, commas=not nc)
    except (ValueError, TypeError):
        return float("nan")


def forecast(x: Any, data_y: Any, data_x: Any) -> float:
    # float() and asarray(dtype=float) raised on text. Sibling numeric
    # helpers return nan for a value error.
    try:
        xv = float(x)
        y = np.asarray(data_y, dtype=float).ravel()
        x_arr = np.asarray(data_x, dtype=float).ravel()
    except (ValueError, TypeError):
        return float("nan")
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
    try:
        data_arr = np.asarray(data).ravel()
        bins_arr = np.asarray(bins).ravel()
        # Return a list for vertical spill
        counts = np.zeros(len(bins_arr) + 1, dtype=int)
        for d in data_arr:
            for i, b in enumerate(bins_arr):
                if d <= b:
                    counts[i] += 1
                    break
            else:
                counts[-1] += 1
        return counts.tolist()
    except Exception:
        return []


def fv(rate: Any, nper: Any, pmt_val: Any, pv_val: Any = 0, type_val: Any = 0) -> float:
    # Unguarded float() raised ValueError/TypeError on text arguments.
    try:
        r = float(rate)
        n = float(nper)
        pm = float(pmt_val)
        p = float(pv_val)
        # Only type 1 is beginning-of-period. Any other type is end, matching
        # the old branch (it did not plug the raw type into the annuity).
        t = 1 if int(float(type_val)) == 1 else 0
    except (ValueError, TypeError):
        return float("nan")
    return _npf_result("fv", r, n, pm, p, t)


def fvschedule(principal: Any, schedule: Any) -> float:
    try:
        p = float(principal)
        sched = np.asarray(schedule).ravel()
    except (ValueError, TypeError):
        return float("nan")
    for rate in sched:
        try:
            p *= 1 + float(rate)
        except (ValueError, TypeError):
            return float("nan")
    return p


def gamma(x: Any) -> float:
    try:
        import math

        x_val = float(x)
        if x_val == 0 or (x_val < 0 and x_val.is_integer()):
            return float("nan")
        return float(math.gamma(x_val))
    except (ValueError, TypeError):
        return float("nan")


def gammadist(x: Any, alpha: Any, beta: Any, c: Any = 1) -> float:
    try:
        import scipy.stats as st

        x_val = float(x)
        a = float(alpha)
        b = float(beta)
        cum = bool(float(c))
        if x_val < 0 or a <= 0 or b <= 0:
            return float("nan")
        if cum:
            return float(st.gamma.cdf(x_val, a, scale=b))
        return float(st.gamma.pdf(x_val, a, scale=b))
    except (ValueError, TypeError, ImportError):
        return float("nan")


def gammainv(p: Any, alpha: Any, beta: Any) -> float:
    try:
        import scipy.stats as st

        prob = float(p)
        a = float(alpha)
        b = float(beta)
        if prob < 0 or prob > 1 or a <= 0 or b <= 0:
            return float("nan")
        return float(st.gamma.ppf(prob, a, scale=b))
    except (ValueError, TypeError, ImportError):
        return float("nan")


def gammaln(x: Any) -> float:
    try:
        import math

        x_val = float(x)
        if x_val <= 0:
            return float("nan")
        return float(math.lgamma(x_val))
    except (ValueError, TypeError):
        return float("nan")


def gauss(x: Any) -> float:
    try:
        import scipy.stats as st

        return float(st.norm.cdf(float(x)) - 0.5)
    except (ValueError, TypeError, ImportError):
        return float("nan")


def geomean(*args: Any) -> float:
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    try:
        import scipy.stats
        res = float(scipy.stats.gmean(arr))
        return res if math.isfinite(res) else float("nan")
    except Exception:
        return float(np.exp(np.mean(np.log(arr))))


def gestep(number: Any, step: Any = 0) -> float:
    try:
        return 1.0 if float(number) >= float(step) else 0.0
    except (ValueError, TypeError):
        return float("nan")


def growth(known_y: Any, known_x: Any = None, new_x: Any = None, const: Any = True) -> Any:
    try:
        y = np.asarray(known_y, dtype=float).ravel()
        # Excel/Calc GROWTH returns #NUM! when any known y is <= 0.
        # np.log of those values is -inf/nan and used to flow through
        # polyfit/exp into the result list.
        if np.any(y <= 0):
            return []
        if known_x is None:
            x = np.arange(1, len(y) + 1, dtype=float)
        else:
            x = np.asarray(known_x, dtype=float).ravel()
        if new_x is None:
            new_x_arr = x
        else:
            new_x_arr = np.asarray(new_x, dtype=float).ravel()

        y_log = np.log(y)
        if const:
            coeffs = np.polyfit(x, y_log, 1)
            res = np.exp(np.polyval(coeffs, new_x_arr))
        else:
            slope = np.sum(x * y_log) / np.sum(x * x)
            res = np.exp(slope * new_x_arr)
        return res.tolist()
    except Exception:
        return []


def harmean(*args: Any) -> float:
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    try:
        import scipy.stats
        res = float(scipy.stats.hmean(arr))
        return res if math.isfinite(res) else float("nan")
    except Exception:
        return float(len(arr) / np.sum(1.0 / arr))


def hypgeomdist(x: Any, n_sample: Any, successes: Any, n_pop: Any) -> float:
    try:
        import scipy.stats as st

        k = int(float(x))
        n = int(float(n_sample))
        K = int(float(successes))
        N = int(float(n_pop))
        if k < 0 or k > n or k > K or k < n - N + K or n < 0 or n > N or K < 0 or K > N or N < 0:
            return float("nan")
        return float(st.hypergeom.pmf(k, N, K, n))
    except (ValueError, TypeError, ImportError):
        return float("nan")
