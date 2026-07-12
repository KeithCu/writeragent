# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import datetime as dt
import builtins
import math
import re
from collections import Counter
from typing import Any, Callable, cast

import numpy as np

from plugin.scripting.calc_functions_common import HELPER_NAMES


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


def datedif(start_date: Any, end_date: Any, unit: str = "D") -> float:
    try:
        sd = dt.date.fromordinal(int(float(start_date)) + 693594)
        ed = dt.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")
    if sd > ed:
        return float("nan")
    u = str(unit).strip('"').upper()
    if u == "D":
        return float((ed - sd).days)
    if u == "M":
        return float((ed.year - sd.year) * 12 + ed.month - sd.month)
    if u == "Y":
        return float(ed.year - sd.year - ((ed.month, ed.day) < (sd.month, sd.day)))
    if u == "MD":
        return float(ed.day - sd.day)
    if u == "YM":
        return float(ed.month - sd.month - (ed.day < sd.day))
    if u == "YD":
        return float((ed - dt.date(ed.year, sd.month, sd.day)).days)
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

    if bool(method):  # European
        if d1 == 31:
            d1 = 30
        if d2 == 31:
            d2 = 30
    else:  # US
        if d1 == 31:
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
        for i in range(1, p + 1):
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
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    if not vals:
        return float("nan")
    arr = np.asarray(vals)
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
        if math.isnan(yf) or yf == 0:
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


def dollar(number: Any, decimals: Any = 2) -> str | float:
    try:
        val = float(number)
        dec = int(float(decimals))
        if math.isnan(val):
            return float("nan")
        return f"${val:,.{max(0, dec)}f}"
    except (ValueError, TypeError):
        return float("nan")


# Group B - Financial 2
def dollarde(fractional_dollar: Any, fraction: Any) -> float:
    try:
        fd = float(fractional_dollar)
        f = int(float(fraction))
    except (ValueError, TypeError):
        return float("nan")
    if f < 0:
        return float("nan")
    if f == 0:
        return float("nan")  # #DIV/0!

    sign = -1.0 if fd < 0 else 1.0
    fd = abs(fd)
    i_part = math.floor(fd)
    f_part = fd - i_part
    # The fraction part is interpreted as numerator / fraction
    # In Excel, 1.02 with fraction 16 means 1 + 2/16 = 1.125
    # Wait, 1.02 has f_part 0.02. 0.02 * 10^ceil(log10(fraction))?
    # No, it's (fd - trunc(fd)) * (10 ** ceil(log10(f))) / f
    power = math.ceil(math.log10(f)) if f > 1 else 1
    if f == 1:
        power = 1
    # Handle exact powers of 10
    if f > 1 and 10 ** (power - 1) == f:
        power -= 1
    return sign * (i_part + (f_part * (10**power)) / f)


def dollarfr(decimal_dollar: Any, fraction: Any) -> float:
    try:
        dd = float(decimal_dollar)
        f = int(float(fraction))
    except (ValueError, TypeError):
        return float("nan")
    if f < 0:
        return float("nan")
    if f == 0:
        return float("nan")
    sign = -1.0 if dd < 0 else 1.0
    dd = abs(dd)
    i_part = math.floor(dd)
    f_part = dd - i_part
    power = math.ceil(math.log10(f)) if f > 1 else 1
    if f > 1 and 10 ** (power - 1) == f:
        power -= 1
    return sign * (i_part + (f_part * f) / (10**power))


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
        return float("nan")
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
    except Exception:
        return float("nan")
    y, m = date_val.year, date_val.month
    m += int(float(months))
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
    except Exception:
        return float("nan")
    y, m = date_val.year, date_val.month
    m += int(float(months))
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

    def round_sig(x, sig):
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
    v = float(n)
    i = int(np.trunc(v))
    if i % 2 == 0:
        return float(i)
    return float(i + (1 if v >= 0 else -1))


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
        if nc:
            return f"{val:.{max(0, dec)}f}"
        return f"{val:,.{max(0, dec)}f}"
    except (ValueError, TypeError):
        return float("nan")


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
    r = float(rate)
    n = float(nper)
    pm = float(pmt_val)
    p = float(pv_val)
    t = int(float(type_val))
    if r == 0:
        return float(-(p + pm * n))
    factor = (1 + r) ** n
    if t == 1:
        return float(-(p * factor + pm * (factor - 1) * (1 + r) / r))
    return float(-(p * factor + pm * (factor - 1) / r))


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


def geomean(r: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(np.exp(np.mean(np.log(arr))))


def gestep(number: Any, step: Any = 0) -> float:
    try:
        return 1.0 if float(number) >= float(step) else 0.0
    except (ValueError, TypeError):
        return float("nan")


def growth(known_y: Any, known_x: Any = None, new_x: Any = None, const: Any = True) -> Any:
    from plugin.scripting.venv.calc_functions_n_s import slope

    try:
        y = np.asarray(known_y, dtype=float).ravel()
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


def harmean(r: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
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
    except (ValueError, TypeError, ImportError):
        return float("nan")
