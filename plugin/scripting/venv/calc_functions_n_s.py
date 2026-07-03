# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""
from __future__ import annotations

import datetime
import builtins
import math
import re
from collections import Counter
from typing import Any, Callable, cast

import numpy as np

from plugin.scripting.calc_functions_common import HELPER_NAMES



__all__ = ["na", "negbinomdist", "networkdays", "networkdays_intl", "nominal", "normdist", "norminv", "normsdist", "normsinv", "nper", "npv", "numbervalue", "odd", "oddfprice", "oddfyield", "oddlprice", "pearson", "percentrank", "permut", "pmt", "poisson", "prob", "pv", "py_str", "quartile", "rank", "regex", "rept", "rsq", "sec", "sech", "seriessum", "skew", "slope", "small", "sort", "sortby", "sqrtpi", "standardize", "stdeva", "stdevpa", "steyx", "subtotal", "sumif", "sumifs", "sumproduct", "sumsq", "textafter"]

def na() -> float:
    # Usually #N/A in Calc maps to NaN in Python data array
    return float("nan")

def negbinomdist(x: Any, r: Any, p: Any) -> float:
    try:
        import scipy.stats as st
        k = int(float(x))
        r_val = int(float(r))
        prob = float(p)
        if k < 0 or r_val < 1 or prob <= 0 or prob > 1:
            return float("nan")
        return float(st.nbinom.pmf(k, r_val, prob))
    except (ValueError, TypeError, ImportError):
        return float("nan")
def networkdays(start_date: Any, end_date: Any, holidays: Any | None = None) -> float:
    try:
        sd = datetime.date.fromordinal(int(float(start_date)) + 693594)
        ed = datetime.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")
    if sd > ed:
        sign = -1
        sd, ed = ed, sd
    else:
        sign = 1
    h_dates: set[datetime.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                try:
                    h_dates.add(datetime.date.fromordinal(int(float(h)) + 693594))
                except Exception:
                    pass
    curr = sd
    days = 0
    while curr <= ed:
        if curr.weekday() < 5 and curr not in h_dates:
            days += 1
        curr += datetime.timedelta(days=1)
    return float(sign * days)
def networkdays_intl(start_date: Any, end_date: Any, weekend: Any = 1, holidays: Any | None = None) -> float:
    try:
        sd = datetime.date.fromordinal(int(float(start_date)) + 693594)
        ed = datetime.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")

    if sd > ed:
        sign = -1
        sd, ed = ed, sd
    else:
        sign = 1

    wk_days = set()
    if isinstance(weekend, str):
        for i, char in enumerate(weekend[:7]):
            if char == "1":
                wk_days.add(i)
    else:
        w_idx = int(float(weekend))
        mapping = {
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
        wk_days.update(mapping.get(w_idx, (5, 6)))

    h_dates: set[datetime.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                try:
                    h_dates.add(datetime.date.fromordinal(int(float(h)) + 693594))
                except Exception:
                    pass
    curr = sd
    days = 0
    while curr <= ed:
        if curr.weekday() not in wk_days and curr not in h_dates:
            days += 1
        curr += datetime.timedelta(days=1)
    return float(sign * days)
def nominal(effect_rate: Any, npery: Any) -> float:
    try:
        er = float(effect_rate)
        np_y = int(float(npery))
    except (ValueError, TypeError):
        return float("nan")
    if er <= 0 or np_y < 1:
        return float("nan")
    return np_y * ((er + 1) ** (1.0 / np_y) - 1)
def normdist(x: Any, mean: Any, stdev: Any, c: Any = 1) -> float:
    try:
        import scipy.stats as st
        x_val = float(x)
        m = float(mean)
        s = float(stdev)
        cum = bool(float(c))
        if s <= 0:
            return float("nan")
        if cum:
            return float(st.norm.cdf(x_val, loc=m, scale=s))
        return float(st.norm.pdf(x_val, loc=m, scale=s))
    except (ValueError, TypeError, ImportError):
        return float("nan")
def norminv(prob: Any, mean: Any, stdev: Any) -> float:
    try:
        p = float(prob)
        m = float(mean)
        s = float(stdev)
        if p <= 0 or p >= 1 or s <= 0:
            return float("nan")
        import scipy.stats
        return float(scipy.stats.norm.ppf(p, loc=m, scale=s))
    except (ValueError, TypeError):
        return float("nan")
def normsdist(z: Any) -> float:
    try:
        import scipy.stats
        return float(scipy.stats.norm.cdf(float(z)))
    except (ValueError, TypeError):
        return float("nan")
def normsinv(prob: Any) -> float:
    try:
        p = float(prob)
        if p <= 0 or p >= 1:
            return float("nan")
        import scipy.stats
        return float(scipy.stats.norm.ppf(p))
    except (ValueError, TypeError):
        return float("nan")
def nper(rate: Any, pmt_val: Any, pv_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    try:
        r = float(rate)
        pmt_f = float(pmt_val)
        pv_f = float(pv_val)
        fv_f = float(fv_val)
        t = int(float(type_val))
    except (ValueError, TypeError):
        return float("nan")
    if r == 0:
        if pmt_f == 0: return float("nan")
        return -(pv_f + fv_f) / pmt_f

    # PV * (1+r)^n + PMT*(1+r*t)*(((1+r)^n - 1)/r) + FV = 0
    # Let A = PMT*(1+r*t)/r
    # PV*(1+r)^n + A*(1+r)^n - A + FV = 0
    # (1+r)^n * (PV + A) = A - FV
    # n * ln(1+r) = ln((A - FV)/(PV + A))
    A = pmt_f * (1 + r * t) / r
    num = A - fv_f
    den = pv_f + A
    if den == 0:
        return float("nan")
    val = num / den
    if val <= 0:
        return float("nan")
    return math.log(val) / math.log(1 + r)
def npv(rate: Any, *args: Any) -> float:
    r = float(rate)
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                vals.append(0.0)
    res = 0.0
    for i, v in enumerate(vals):
        res += v / ((1 + r) ** (i + 1))
    return float(res)
def numbervalue(text: Any, dec_sep: Any = ".", grp_sep: Any = ",") -> float:
    try:
        s = str(text).strip()
        if not s:
            return 0.0
        s = s.replace(str(grp_sep), "")
        if str(dec_sep) != ".":
            s = s.replace(str(dec_sep), ".")
        return float(s)
    except (ValueError, TypeError):
        return float("nan")
def odd(n: Any) -> float:
    v = float(n)
    i = int(np.trunc(v))
    if i % 2 != 0:
        return float(i)
    return float(i + (1 if v >= 0 else -1))

def oddfprice(settlement: Any, maturity: Any, issue: Any, first_coupon: Any, rate: Any, yld: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _days_between
    try:
        s = float(settlement)
        m = float(maturity)
        _iss = float(issue)
        _fc = float(first_coupon)
        r = float(rate)
        y = float(yld)
        red = float(redemption)
        f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")
    # basic PV approximation
    days_to_mat = _days_between(s, m, b)
    years = days_to_mat / 365.25 if b == 1 else days_to_mat / 360.0
    n = years * f
    if n <= 0: return float("nan")

    c = 100 * r / f
    price = sum(c / ((1 + y / f) ** i) for i in range(1, int(n) + 1))
    price += red / ((1 + y / f) ** n)
    return price
def oddfyield(settlement: Any, maturity: Any, issue: Any, first_coupon: Any, rate: Any, pr: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _days_between
    try:
        s = float(settlement)
        m = float(maturity)
        _iss = float(issue)
        _fc = float(first_coupon)
        r = float(rate)
        price = float(pr)
        red = float(redemption)
        _f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")

    # Approx yield using simple formula: Y = (C + (F-P)/n) / ((F+P)/2)
    days_to_mat = _days_between(s, m, b)
    years = days_to_mat / 365.25 if b == 1 else days_to_mat / 360.0
    if years <= 0: return float("nan")
    c = 100 * r
    approx_y = (c + (red - price) / years) / ((red + price) / 2)
    return approx_y
def oddlprice(settlement: Any, maturity: Any, last_interest: Any, rate: Any, yld: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _days_between
    try:
        s = float(settlement)
        m = float(maturity)
        li = float(last_interest)
        r = float(rate)
        y = float(yld)
        red = float(redemption)
        f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")

    days_in_reg_period = 365.25 / f if b == 1 else 360.0 / f
    days_li_to_m = _days_between(li, m, b)
    last_period_frac = days_li_to_m / days_in_reg_period

    days_s_to_m = _days_between(s, m, b)
    settle_frac = days_s_to_m / days_in_reg_period

    if last_period_frac <= 0 or settle_frac <= 0: return float("nan")

    c = 100 * r / f
    last_c = c * last_period_frac

    price = (red + last_c) / ((1 + y / f) ** settle_frac)

    days_li_to_s = _days_between(li, s, b)
    accrued_frac = days_li_to_s / days_in_reg_period
    accrued_interest = c * accrued_frac

    return price - accrued_interest
def pearson(data1: Any, data2: Any) -> float:
    try:
        d1 = np.asarray(data1).ravel()
        d2 = np.asarray(data2).ravel()
        if len(d1) != len(d2):
            return float("nan")
        mask1 = np.array([isinstance(x.item() if hasattr(x, 'item') else x, (int, float)) and not math.isnan(x.item() if hasattr(x, 'item') else x) for x in d1])
        mask2 = np.array([isinstance(x.item() if hasattr(x, 'item') else x, (int, float)) and not math.isnan(x.item() if hasattr(x, 'item') else x) for x in d2])
        mask = mask1 & mask2
        d1_clean = np.asarray(d1[mask], dtype=float)
        d2_clean = np.asarray(d2[mask], dtype=float)
        if len(d1_clean) <= 1:
            return float("nan")
        import scipy.stats  # type: ignore[import-untyped]
        corr, _p = scipy.stats.pearsonr(d1_clean, d2_clean)
        return float(cast("float", corr))
    except (ValueError, TypeError):
        return float("nan")
def percentrank(data: Any, x: Any, significance: Any = 3) -> float:
    try:
        d = np.asarray(data, dtype=float).ravel()
        d = d[np.isfinite(d)]
        if len(d) == 0:
            return float("nan")
        val = float(x)
        sig = int(significance)
        d_sorted = np.sort(d)
        if val < d_sorted[0] or val > d_sorted[-1]:
            return float("nan")
        # Calc PERCENTRANK acts differently if x is in data vs interpolation
        # Using percentileofscore with weak for rank approximation, then dividing
        # But wait, scipy.stats.percentileofscore returns a percentage (0-100).
        # We need a value between 0 and 1.

        # A more exact match to Excel/Calc PERCENTRANK.INC
        # It's better to implement manually to match Calc.
        # find index
        n = len(d)
        if n == 1:
            return 1.0
        # Check if x is in array
        idx = np.searchsorted(d_sorted, val)
        if d_sorted[idx] == val:
            # count occurrences less than val
            count_less = np.sum(d < val)
            res = count_less / (n - 1)
        else:
            idx = np.searchsorted(d_sorted, val) - 1
            x0, x1 = d_sorted[idx], d_sorted[idx+1]
            r0, r1 = np.sum(d < x0) / (n - 1), np.sum(d < x1) / (n - 1)
            res = r0 + (r1 - r0) * (val - x0) / (x1 - x0)

        return float(np.round(res, sig))
    except (ValueError, TypeError, IndexError):
        return float("nan")
def permut(n: Any, k: Any) -> float:
    try:
        n_val = int(float(n))
        k_val = int(float(k))
        if n_val < 0 or k_val < 0 or n_val < k_val:
            return float("nan")
        return float(math.perm(n_val, k_val))
    except (ValueError, TypeError):
        return float("nan")
def pmt(rate: Any, nper: Any, pv: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    r = float(rate)
    n = float(nper)
    p = float(pv)
    f = float(fv_val)
    t = int(float(type_val))
    if r == 0:
        return float(-(p + f) / n)
    factor = (1 + r) ** n
    if t == 1:
        return float(-(p * factor + f) * r / (factor - 1) / (1 + r))
    return float(-(p * factor + f) * r / (factor - 1))

def poisson(x: Any, mean: Any, cumulative: Any = False) -> float:
    try:
        k = int(float(x))
        m = float(mean)
        if k < 0 or m < 0:
            return float("nan")
        import scipy.stats
        if cumulative:
            return float(scipy.stats.poisson.cdf(k, m))
        else:
            return float(scipy.stats.poisson.pmf(k, m))
    except (ValueError, TypeError):
        return float("nan")
def prob(data: Any, probs: Any, x_start: Any, x_end: Any | None = None) -> float:
    try:
        d = np.asarray(data, dtype=float).ravel()
        p = np.asarray(probs, dtype=float).ravel()
        if len(d) != len(p) or len(d) == 0:
            return float("nan")
        if not np.isclose(np.sum(p), 1.0) or np.any(p < 0) or np.any(p > 1):
            return float("nan")
        start = float(x_start)
        end = float(x_end) if x_end is not None else start
        mask = (d >= start) & (d <= end)
        return float(np.sum(p[mask]))
    except (ValueError, TypeError):
        return float("nan")
def pv(rate: Any, nper: Any, pmt_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    r = float(rate)
    n = float(nper)
    pm = float(pmt_val)
    f = float(fv_val)
    t = int(float(type_val))
    if r == 0:
        return float(-(f + pm * n))
    factor = (1 + r) ** n
    if t == 1:
        return float(-(f + pm * (factor - 1) * (1 + r) / r) / factor)
    return float(-(f + pm * (factor - 1) / r) / factor)

def quartile(r: Any, q: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    qi = int(float(q))
    pct = {0: 0.0, 1: 25.0, 2: 50.0, 3: 75.0, 4: 100.0}.get(qi, float(qi) * 25.0)
    return float(np.percentile(arr, pct)) if len(arr) else float("nan")

def rank(val: Any, r: Any, order: int | float = 0) -> float:
    arr = [float(x) for x in np.asarray(r).ravel() if x is not None and x != ""]
    try:
        target = float(val)
    except (ValueError, TypeError):
        return float("nan")
    if int(float(order)) == 0:
        arr.sort(reverse=True)
    else:
        arr.sort()
    try:
        return float(arr.index(target) + 1)
    except ValueError:
        return float("nan")
def regex(text: Any, expr: Any, replacement: Any | None = None, flags: str = "") -> str:
    if text is None:
        text = ""
    text_str = str(text)
    expr_str = str(expr)
    re_flags = 0
    if "i" in str(flags).lower():
        re_flags |= re.IGNORECASE
    if replacement is None:
        if "g" in str(flags).lower():
            matches = re.findall(expr_str, text_str, flags=re_flags)
            if not matches:
                return ""
            if isinstance(matches[0], tuple):
                return ", ".join("".join(m) for m in matches)
            return ", ".join(matches)
        m = re.search(expr_str, text_str, flags=re_flags)
        if m:
            return m.group(1) if m.groups() else m.group(0)
        return ""
    rep_str = str(replacement)
    if "g" in str(flags).lower():
        return re.sub(expr_str, rep_str, text_str, flags=re_flags)
    return re.sub(expr_str, rep_str, text_str, count=1, flags=re_flags)
def rept(text: Any, n: Any) -> str:
    try:
        return str(text) * int(float(n))
    except (ValueError, TypeError, OverflowError):
        return ""
def rsq(data_y: Any, data_x: Any) -> float:
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[mask], x[mask]
    if len(y) < 2:
        return float("nan")
    corr = np.corrcoef(x, y)[0, 1]
    return float(corr**2)

def sec(x: Any) -> float:
    try:
        return float(1.0 / math.cos(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")
def sech(x: Any) -> float:
    try:
        return float(1.0 / math.cosh(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")
def seriessum(x: Any, n: Any, m: Any, coefficients: Any) -> float:
    try:
        x_val = float(x)
        n_val = float(n)
        m_val = float(m)
        coeffs = np.asarray(coefficients).ravel()
        res = 0.0
        for i, c in enumerate(coeffs):
            res += float(c) * (x_val ** (n_val + i * m_val))
        return res
    except Exception:
        return float("nan")
def skew(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    n = len(vals)
    if n < 3:
        return float("nan")
    arr = np.asarray(vals)
    m = np.mean(arr)
    s = np.std(arr, ddof=1)
    if s == 0:
        return float("nan")
    # Excel/Calc skewness formula
    z = (arr - m) / s
    term1 = n / ((n - 1) * (n - 2))
    term2 = np.sum(z**3)
    return float(term1 * term2)
def slope(data_y: Any, data_x: Any) -> float:
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[mask], x[mask]
    if len(y) < 2:
        return float("nan")
    mx, my = np.mean(x), np.mean(y)
    ss_xy = np.sum((x - mx) * (y - my))
    ss_xx = np.sum((x - mx) ** 2)
    return float(ss_xy / ss_xx) if ss_xx != 0 else float("nan")

def small(r: Any, k: Any) -> float:
    arr = sorted([float(x) for x in np.asarray(r).ravel() if x is not None and x != ""])
    ki = int(float(k))
    return float(arr[ki - 1]) if 0 < ki <= len(arr) else float("nan")

def sort(range_arr: Any, sort_index: int | float = 1, sort_order: int | float = 1, by_col: bool = False) -> list:
    arr = np.asarray(range_arr)
    if arr.size == 0:
        return []
    si = max(1, int(float(sort_index))) - 1
    asc = int(float(sort_order)) >= 0
    if arr.ndim == 1:
        out = np.sort(arr) if asc else np.sort(arr)[::-1]
        return out.tolist()
    if bool(by_col):
        order = np.argsort(arr[:, si] if si < arr.shape[1] else arr[:, 0])
        if not asc:
            order = order[::-1]
        return arr[:, order].T.tolist()
    order = np.argsort(arr[:, si] if si < arr.shape[1] else arr[:, 0])
    if not asc:
        order = order[::-1]
    return arr[order].tolist()
def sortby(range_arr: Any, by_array: Any, sort_order: int | float = 1, *extra: Any) -> list:
    arr = np.asarray(range_arr)
    by = np.asarray(by_array).ravel()
    asc = int(float(sort_order)) >= 0
    if arr.ndim == 1:
        order = np.argsort(by[: len(arr)])
        if not asc:
            order = order[::-1]
        return arr.ravel()[order].tolist()
    order = np.argsort(by[: arr.shape[0]])
    if not asc:
        order = order[::-1]
    return arr[order].tolist()

def sqrtpi(number: Any) -> float:
    try:
        n = float(number)
        if n < 0:
            return float("nan")
        return float(math.sqrt(n * math.pi))
    except (ValueError, TypeError):
        return float("nan")
def standardize(x: Any, mean: Any, stdev: Any) -> float:
    try:
        val = float(x)
        m = float(mean)
        s = float(stdev)
        if s <= 0:
            return float("nan")
        return float((val - m) / s)
    except (ValueError, TypeError):
        return float("nan")
def stdeva(*args: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_float_a
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if len(vals) < 2:
        return float("nan")
    return float(np.std(vals, ddof=1))
def stdevpa(*args: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_float_a
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
        return float("nan")
    return float(np.std(vals, ddof=0))
def steyx(data_y: Any, data_x: Any) -> float:
    from plugin.scripting.venv.calc_functions_i_m import intercept
    from plugin.scripting.venv.calc_functions_n_s import slope
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[mask], x[mask]
    n = len(y)
    if n < 3:
        return float("nan")
    s = slope(y, x)
    i = intercept(y, x)
    y_hat = s * x + i
    ss_resid = np.sum((y - y_hat) ** 2)
    return float(math.sqrt(ss_resid / (n - 2)))
def subtotal(fn_num: Any, r: Any) -> float:
    fn = int(float(fn_num)) % 100
    flat = np.asarray(r).ravel()
    nums = []
    for x in flat:
        if x is None or x == "":
            continue
        try:
            v = float(x)
            if not np.isnan(v):
                nums.append(v)
        except (ValueError, TypeError):
            pass
    arr = np.asarray(nums, dtype=float)
    if fn == 1:
        return float(np.mean(arr)) if len(arr) else 0.0
    if fn == 2:
        return float(len(arr))
    if fn == 3:
        return float(sum(1 for x in flat if x is not None and x != ""))
    if fn == 4:
        return float(np.max(arr)) if len(arr) else 0.0
    if fn == 5:
        return float(np.min(arr)) if len(arr) else 0.0
    if fn == 6:
        return float(np.prod(arr)) if len(arr) else 0.0
    if fn == 7:
        return float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    if fn == 8:
        return float(np.std(arr, ddof=0)) if len(arr) else 0.0
    if fn == 9:
        return float(np.sum(arr))
    if fn == 10:
        return float(np.var(arr, ddof=1)) if len(arr) > 1 else 0.0
    if fn == 11:
        return float(np.var(arr, ddof=0)) if len(arr) else 0.0
    return float(np.sum(arr))
def sumif(r: Any, crit: Any, sr: Any | None = None) -> float:
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    r_flat = np.asarray(r).ravel()
    sr_flat = np.asarray(sr).ravel() if sr is not None else r_flat
    total = 0.0
    for i in range(min(len(r_flat), len(sr_flat))):
        if match_criteria(r_flat[i], crit):
            try:
                val = float(sr_flat[i])
                if not np.isnan(val):
                    total += val
            except (ValueError, TypeError):
                pass
    return float(total)
def sumifs(sr: Any, *args: Any) -> float:
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    sr_flat = np.asarray(sr).ravel()
    cond_ranges = []
    criteria = []
    for i in range(0, len(args), 2):
        cond_ranges.append(np.asarray(args[i]).ravel())
        criteria.append(args[i + 1])
    total = 0.0
    for idx in range(len(sr_flat)):
        match = True
        for cr, crit in zip(cond_ranges, criteria):
            if idx >= len(cr) or not match_criteria(cr[idx], crit):
                match = False
                break
        if match:
            try:
                val = float(sr_flat[idx])
                if not np.isnan(val):
                    total += val
            except (ValueError, TypeError):
                pass
    return float(total)
def sumproduct(*args: Any) -> float:
    arrays = [np.asarray(a).ravel() for a in args]
    if not arrays:
        return 0.0
    min_len = min(len(a) for a in arrays)
    total = 0.0
    for i in range(min_len):
        prod = 1.0
        for arr in arrays:
            try:
                prod *= float(arr[i])
            except (ValueError, TypeError):
                prod = 0.0
                break
        total += prod
    return float(total)
def sumsq(*args: Any) -> float:
    total = 0.0
    for arg in args:
        for val in np.asarray(arg).ravel():
            if val is not None and val != "":
                try:
                    total += float(val) ** 2
                except (ValueError, TypeError):
                    pass
    return float(total)
def py_str(val: Any) -> str:
    """Stringify for inline ``=PY()`` code without emitting the ``str(`` token."""
    return str(val)

def textafter(text: Any, delimiter: Any, instance_num: Any = 1, match_mode: Any = 0, match_end: Any = 0, if_not_found: Any = float("nan")) -> str | float:
    try:
        s = str(text)
        delim = str(delimiter)
        inst = int(float(instance_num))
        if match_mode == 1:
            s_search = s.lower()
            delim_search = delim.lower()
        else:
            s_search = s
            delim_search = delim

        if inst > 0:
            parts = s_search.split(delim_search)
            if len(parts) <= inst:
                if match_end and len(parts) == inst:
                    return ""
                return if_not_found
            # Find the actual split point in the original string
            idx = 0
            for i in range(inst):
                idx = s_search.find(delim_search, idx) + len(delim_search)
            return s[idx:]
        elif inst < 0:
            parts = s_search.split(delim_search)
            if len(parts) <= abs(inst):
                if match_end and len(parts) == abs(inst):
                    return ""
                return if_not_found
            idx = len(s)
            for i in range(abs(inst)):
                idx = s_search.rfind(delim_search, 0, idx)
            return s[idx + len(delim_search):]
        else:
            return float("nan")
    except (ValueError, TypeError):
        return float("nan")
