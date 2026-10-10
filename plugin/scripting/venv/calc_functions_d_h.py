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
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any

import numpy as np

from .calc_functions_util import (
    _date_to_serial,
    _days360,
    _dollar_fraction_terms,
    _eval_d_criteria,
    _extract_numeric_array,
    _npf_result,
    _round_half_up,
    _serial_to_date,
)


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


def _round_sig(x: float, sig: int) -> float:
    if x == 0 or not math.isfinite(x):
        return 0.0
    exponent = math.floor(math.log10(abs(x)))
    factor = 10 ** (sig - 1 - exponent)
    d = Decimal(str(x * factor))
    return float(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) / factor


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
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.mean(vals)) if vals else float("nan")


def days(end_date: Any, start_date: Any) -> float:
    # DAYS truncates both serials to integers before subtracting. A fractional
    # serial is still that calendar day.
    try:
        ed = int(float(end_date))
        sd = int(float(start_date))
        return float(ed - sd)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def days360(start_date: Any, end_date: Any, method: Any = False) -> float:
    sd = _serial_to_date(start_date)
    ed = _serial_to_date(end_date)
    if sd is None or ed is None:
        return float("nan")
    return _days360(sd, ed, european=bool(method))


def db(cost: Any, salvage: Any, life: Any, period: Any, month: Any = 12) -> float:
    # 1 <= period <= life + 1, and cost, salvage, life, and month must be valid.
    # Period 0 is #NUM!, not 0.
    try:
        c = float(cost)
        s = float(salvage)
        life_val = float(life)
        p = int(float(period))
        m = int(float(month))
        if c <= 0 or s < 0 or life_val <= 0 or m < 1 or m > 12:
            return float("nan")
        if p < 1 or p > life_val + 1:
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
    vals = _eval_d_criteria(db, field, criteria)
    return float(len(vals)) if vals is not None else float("nan")


def dcounta(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    return float(sum(1 for v in vals if v is not None and v != "")) if vals is not None else float("nan")


def ddb(cost: Any, salvage: Any, life: Any, period: Any, factor: Any = 2) -> float:
    # Excel and Calc: 1 <= period <= life, cost >= 0, salvage >= 0, life > 0,
    # factor > 0.
    try:
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
    except Exception:
        return float("nan")


def decimal(text: Any, radix: Any) -> float:
    # Every character must be a digit for this radix. Python int() accepts
    # 0x, signs, and underscores; Excel and Calc DECIMAL do not.
    try:
        r = int(float(radix))
        if r < 2 or r > 36:
            return float("nan")
        s = str(text).strip()
        if not s:
            return float("nan")
        valid_chars = "0123456789abcdefghijklmnopqrstuvwxyz"[:r]
        if any(c.lower() not in valid_chars for c in s):
            return float("nan")
        return float(int(s, r))
    except (ValueError, TypeError, OverflowError):
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
    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    if vals is None:
        return "#VALUE!"
    if len(vals) == 1:
        return vals[0]
    return "#NUM!" if len(vals) > 1 else "#VALUE!"


def disc(settlement: Any, maturity: Any, pr: Any, redemption: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_t_z import yearfrac

    try:
        p = float(pr)
        red = float(redemption)
        yf = yearfrac(settlement, maturity, basis)
        if math.isnan(yf) or yf == 0 or red == 0:
            return float("nan")
        return float((red - p) / red / yf)
    except Exception:
        return float("nan")


def dmax(db: Any, field: Any, criteria: Any) -> float:
    # No matching record is 0, not #NUM!. Excel and Calc DMAX do the same.
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None:
        return float("nan")
    return float(np.max(vals)) if vals else 0.0


def dmin(db: Any, field: Any, criteria: Any) -> float:
    # No matching record is 0, not #NUM!. Excel and Calc DMIN do the same.
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None:
        return float("nan")
    return float(np.min(vals)) if vals else 0.0


def _format_rounded(val: float, decimals: int, *, commas: bool) -> str:
    """Format ``val`` after rounding half-up to ``decimals`` places."""
    # Half away from zero (2.5 -> 3, -2.5 -> -3). Python round() is half-to-even.
    places = max(0, decimals)
    q = _round_half_up(val, decimals)
    if places == 0:
        return f"{int(q):,}" if commas else str(int(q))
    return f"{q:,.{places}f}" if commas else f"{q:.{places}f}"


def dollar(number: Any, decimals: Any = 2) -> str | float:
    # The minus sits before the dollar sign (-$1,234.57), after half-up rounding.
    try:
        val = float(number)
        dec = int(float(decimals))
        if math.isnan(val) or not math.isfinite(val):
            return float("nan")
        q = _round_half_up(val, dec)
        places = max(0, dec)
        if places == 0:
            s = f"{abs(int(q)):,}"
        else:
            s = f"{abs(q):,.{places}f}"
        return f"-${s}" if q < 0 else f"${s}"
    except (ValueError, TypeError, OverflowError):
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
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None:
        return float("nan")
    return float(np.prod(vals)) if vals else 0.0


def dstdev(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None or len(vals) <= 1:
        return float("nan")
    return float(np.std(vals, ddof=1))


def dstdevp(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None or not vals:
        return float("nan")
    return float(np.std(vals, ddof=0))


def dsum(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None:
        return float("nan")
    return float(np.sum(vals))


def duration(settlement: Any, maturity: Any, coupon: Any, yld: Any, frequency: Any, basis: Any = 0) -> float:
    from .calc_functions_t_z import yearfrac

    try:
        s = float(settlement)
        m = float(maturity)
        c = float(coupon)
        y = float(yld)
        f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if c < 0 or y < 0 or f not in (1, 2, 4) or b < 0 or b > 4 or s >= m:
        return float("nan")

    periods = yearfrac(s, m, b) * f
    n = periods
    if n <= 0:
        return float("nan")

    yf = y / f
    cf = c / f
    if yf == 0:
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
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None or len(vals) <= 1:
        return float("nan")
    return float(np.var(vals, ddof=1))


def dvarp(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    if vals is None or not vals:
        return float("nan")
    return float(np.var(vals, ddof=0))


def edate(start_date: Any, months: Any) -> float:
    # Month length comes from calendar.monthrange, including February in a leap year.
    sd = _serial_to_date(start_date)
    if sd is None:
        return float("nan")
    try:
        month_delta = int(float(months))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    y, m = sd.year, sd.month
    m += month_delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    try:
        dim = calendar.monthrange(y, m)[1]
        d = min(sd.day, dim)
        res_date = dt.date(y, m, d)
        return float(_date_to_serial(res_date))
    except (ValueError, OverflowError):
        return float("nan")


def effect(nominal_rate: Any, npery: Any) -> float:
    try:
        nr = float(nominal_rate)
        n_per = int(float(npery))
    except (ValueError, TypeError, OverflowError):
        # int(float("inf")) is OverflowError, so EFFECT(rate, inf) raised
        # instead of the #NUM! nan a non-positive rate already returns.
        return float("nan")
    # Excel EFFECT remarks and LibreOffice AnalysisAddIn::getEffect both
    # reject nominal_rate <= 0 with #NUM!. The algebra at rate 0 is 0, but
    # that is not the spreadsheet result.
    if nr <= 0 or n_per < 1:
        return float("nan")
    return (1 + nr / n_per) ** n_per - 1


def encodeurl(text: Any) -> str | float:
    try:
        import urllib.parse

        return urllib.parse.quote(str(text), safe="")
    except (ValueError, TypeError):
        return float("nan")


def eomonth(start_date: Any, months: Any) -> float:
    # Last day of the month via _serial_to_date and calendar.monthrange.
    sd = _serial_to_date(start_date)
    if sd is None:
        return float("nan")
    try:
        month_delta = int(float(months))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    y, m = sd.year, sd.month
    m += month_delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    try:
        dim = calendar.monthrange(y, m)[1]
        res_date = dt.date(y, m, dim)
        return float(_date_to_serial(res_date))
    except (ValueError, OverflowError):
        return float("nan")


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
                sig = int(float(triangulation_precision))
                if sig < 3:
                    return float("nan")
                eur_val = _round_sig(eur_val, sig)
            except (ValueError, TypeError, OverflowError):
                # int(float("inf")) is OverflowError, so an infinite
                # triangulation precision raised instead of #NUM!.
                return float("nan")

    if to_curr == "EUR":
        res = eur_val
    else:
        res = eur_val * _EURO_RATES[to_curr]

    is_full = False
    if isinstance(full_precision, bool):
        is_full = full_precision
    else:
        try:
            is_full = bool(float(full_precision))
        except (ValueError, TypeError):
            is_full = False

    if not math.isfinite(res):
        return float("nan")

    if not is_full:
        # Half away from zero, same as FIXED and DOLLAR. Python round() is half-to-even.
        try:
            res = float(_round_half_up(res, _EURO_DECIMALS[to_curr]))
        except (OverflowError, ValueError):
            return float("nan")
    return res


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
    # n < 0 or n > 300 is #NUM!. LibreOffice ScInterpreter caps FACTDOUBLE at 300;
    # a larger n walks down to 0 by twos.
    try:
        v = int(float(n))
        if v < 0 or v > 300:
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
    # An (n, 1) include filters rows; a (1, m) include filters columns.
    # Treating either as an elementwise mask raises IndexError.
    try:
        arr = np.asarray(range_arr)
        crit = np.asarray(criteria)
        if arr.ndim == 1:
            crit_flat = crit.ravel()
            if len(crit_flat) != len(arr):
                return "#VALUE!"
            mask = np.asarray([bool(x) for x in crit_flat])
            out = arr[mask]
        elif arr.ndim == 2:
            if crit.ndim == 1:
                if len(crit) == arr.shape[0]:
                    mask = np.asarray([bool(x) for x in crit])
                    out = arr[mask]
                elif len(crit) == arr.shape[1]:
                    mask = np.asarray([bool(x) for x in crit])
                    out = arr[:, mask]
                else:
                    return "#VALUE!"
            elif crit.ndim == 2:
                if crit.shape == (arr.shape[0], 1):
                    mask = np.asarray([bool(x) for x in crit.ravel()])
                    out = arr[mask]
                elif crit.shape == (1, arr.shape[1]):
                    mask = np.asarray([bool(x) for x in crit.ravel()])
                    out = arr[:, mask]
                elif crit.shape == arr.shape:
                    mask = crit.astype(bool)
                    out = arr[mask]
                else:
                    return "#VALUE!"
            else:
                return "#VALUE!"
        else:
            return "#VALUE!"
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
        x_val = float(x)
        if x_val <= -1 or x_val >= 1:
            return float("nan")
        return float(math.atanh(x_val))
    except (ValueError, TypeError):
        return float("nan")


def fisherinv(y: Any) -> float:
    try:
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
    except (ValueError, TypeError, OverflowError):
        # int(float("inf")) raises OverflowError, so FIXED(1, inf) left
        # this handler and traceback'd instead of the #NUM! nan text returns.
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
    except (ValueError, TypeError, OverflowError):
        # int(float("inf")) for type is OverflowError, so FV(..., inf)
        # raised instead of the #NUM! nan a text rate already returns.
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
        x_val = float(x)
        if x_val == 0 or (x_val < 0 and x_val.is_integer()):
            return float("nan")
        return float(math.gamma(x_val))
    except (ValueError, TypeError, OverflowError):
        # math.gamma(200) raises OverflowError ("math range error"). That
        # is #NUM!, same as gamma(0), but the except did not catch it.
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
    # exp(mean(log(arr))). Same result as scipy.stats.gmean, without the import.
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
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
    # len(arr) / sum(1/arr). Same result as scipy.stats.hmean, without the import.
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size or np.any(arr <= 0):
        return float("nan")
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
    except (ValueError, TypeError, OverflowError, ImportError):
        # int(float("inf")) is OverflowError, so HYPGEOMDIST with an
        # infinite argument raised instead of the #NUM! nan a bad draw returns.
        return float("nan")
