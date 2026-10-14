# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from typing import Any

import numpy as np

from .calc_functions_util import (
    _build_holiday_set,
    _calc_sort_key,
    _clean_paired_arrays,
    _collect_a_values,
    _criteria_numbers,
    _days_between,
    _extract_numeric_array,
    _find_text_cut,
    _multi_criteria_mask,
    _npf_result,
    _parse_weekend,
    _scipy_stats,
    _serial_to_date,
)
from .coerce import is_missing_value



__all__ = [
    "na",
    "negbinomdist",
    "networkdays",
    "networkdays_intl",
    "nominal",
    "normdist",
    "norminv",
    "normsdist",
    "normsinv",
    "nper",
    "npv",
    "numbervalue",
    "odd",
    "oddfprice",
    "oddfyield",
    "oddlprice",
    "pearson",
    "percentrank",
    "permut",
    "pmt",
    "poisson",
    "prob",
    "pv",
    "py_str",
    "quartile",
    "rank",
    "regex",
    "rept",
    "rsq",
    "sec",
    "sech",
    "seriessum",
    "skew",
    "slope",
    "small",
    "sort",
    "sortby",
    "sqrtpi",
    "standardize",
    "stdeva",
    "stdevpa",
    "steyx",
    "subtotal",
    "sumif",
    "sumifs",
    "sumproduct",
    "sumsq",
    "textafter",
]


def na() -> float:
    # Usually #N/A in Calc maps to NaN in Python data array
    return float("nan")


def negbinomdist(x: Any, r: Any, p: Any) -> float:
    try:
        st = _scipy_stats()
        if st is None:
            return float("nan")
        # int(float(+inf)) and int() of an oversized count raise OverflowError.
        # The old except only named ValueError, so that input escaped. Excel
        # NEGBINOMDIST is #NUM!, which this module returns as NaN.
        k = int(float(x))
        r_val = int(float(r))
        prob = float(p)
        if k < 0 or r_val < 1 or prob <= 0 or prob > 1:
            return float("nan")
        return float(st.nbinom.pmf(k, r_val, prob))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def networkdays(start_date: Any, end_date: Any, holidays: Any | None = None) -> float:
    return networkdays_intl(start_date, end_date, weekend=1, holidays=holidays)


def networkdays_intl(start_date: Any, end_date: Any, weekend: Any = 1, holidays: Any | None = None) -> float:
    sd = _serial_to_date(start_date)
    ed = _serial_to_date(end_date)
    if sd is None or ed is None:
        return float("nan")

    if sd > ed:
        sign = -1
        sd, ed = ed, sd
    else:
        sign = 1

    wk_days = _parse_weekend(weekend)
    if not isinstance(wk_days, set):
        return float("nan")

    h_dates = _build_holiday_set(holidays)
    total_days = (ed - sd).days + 1
    weeks = total_days // 7
    workdays_per_week = 7 - len(wk_days)
    days = weeks * workdays_per_week

    curr = sd + dt.timedelta(days=weeks * 7)
    while curr <= ed:
        if curr.weekday() not in wk_days:
            days += 1
        curr += dt.timedelta(days=1)

    if h_dates:
        for h in h_dates:
            if sd <= h <= ed and h.weekday() not in wk_days:
                days -= 1

    return float(sign * max(0, days))


def nominal(effect_rate: Any, npery: Any) -> float:
    try:
        er = float(effect_rate)
        # int(float(+inf)) and float() of an oversized period count raise
        # OverflowError. The old except missed it, so NOMINAL crashed. Excel
        # is #NUM!, returned here as NaN.
        np_y = int(float(npery))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # Excel NOMINAL(0, npery) is 0. Only a negative effective rate is #NUM!.
    if er < 0 or np_y < 1:
        return float("nan")
    # What was wrong: NOMINAL(inf, n) returned inf.
    # How it happened: er < 0 is false for +inf, and the power is outside the
    # conversion try. getNominal returns finiteOrThrow. Rate 0 stays 0 (Excel);
    # LibreOffice rejects rate <= 0, and this helper does not.
    # Why this change fixes it: OverflowError and a non-finite result are NaN.
    try:
        res = np_y * ((er + 1) ** (1.0 / np_y) - 1)
    except OverflowError:
        return float("nan")
    return res if math.isfinite(res) else float("nan")


def normdist(x: Any, mean: Any, stdev: Any, c: Any = 1) -> float:
    try:
        x_val = float(x)
        m = float(mean)
        s = float(stdev)
        cum = bool(float(c))
        if s <= 0:
            return float("nan")
        # integralPhi / phi from ScNormDist. scipy.stats was imported inside
        # the call, so a missing scipy made every value NaN.
        z = (x_val - m) / s
        if cum:
            res = 0.5 * math.erfc(-z / math.sqrt(2.0))
        else:
            res = math.exp(-0.5 * z * z) / (s * math.sqrt(2.0 * math.pi))
        return res if math.isfinite(res) else float("nan")
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def norminv(prob: Any, mean: Any, stdev: Any) -> float:
    try:
        p = float(prob)
        m = float(mean)
        s = float(stdev)
        if p <= 0 or p >= 1 or s <= 0:
            return float("nan")
        # gaussinv(p) is the standard-normal inverse CDF.
        res = m + s * statistics.NormalDist().inv_cdf(p)
        return res if math.isfinite(res) else float("nan")
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def normsdist(z: Any) -> float:
    return normdist(z, 0, 1, True)


def normsinv(prob: Any) -> float:
    return norminv(prob, 0, 1)


def nper(rate: Any, pmt_val: Any, pv_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    try:
        r = float(rate)
        pmt_f = float(pmt_val)
        pv_f = float(pv_val)
        fv_f = float(fv_val)
        # Only type 1 is beginning-of-period, same as PMT/FV/PV. The old
        # closed form plugged any integer into (1+rate*type).
        # float() of an oversized int raises OverflowError. pmt/pv already
        # catch it; this used to escape. Excel is #VALUE!.
        t = 1 if int(float(type_val)) == 1 else 0
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # numpy-financial returns ±inf when the payment does not amortize
    # (pmt == 0, or the log argument is non-positive). log(1+rate) at
    # rate == -1 used to raise ValueError. Both are Excel #NUM!.
    return _npf_result("nper", r, pmt_f, pv_f, fv_f, t)


def npv(rate: Any, *args: Any) -> float:
    # Empty and non-numeric cells are skipped. Treating them as 0 consumes a discount period.
    try:
        r = float(rate)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if r == -1.0:
        return float("nan")
    vals = []
    for arg in args:
        for v in np.asarray(arg, dtype=object).ravel():
            if v is None or is_missing_value(v) or isinstance(v, (bool, np.bool_)):
                continue
            try:
                vf = float(v)
                if not math.isnan(vf):
                    vals.append(vf)
            except (ValueError, TypeError, OverflowError):
                continue
    res = 0.0
    try:
        for i, v in enumerate(vals):
            res += v / ((1 + r) ** (i + 1))
    except (ZeroDivisionError, OverflowError, ValueError):
        return float("nan")
    return float(res)


# Whole-string decimal that rtl_math_stringToDouble accepts. Python float()
# also accepts "inf", "nan", and "1_000", which ScNumberValue rejects.
_NUMBER_TEXT = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def numbervalue(text: Any, dec_sep: Any = ".", grp_sep: Any = ",") -> float:
    # When the group separator equals the decimal separator, do not strip it.
    # numbervalue("1,5", ",") is 1.5, not 15.
    try:
        s = str(text)
        dec = str(dec_sep)
        grp = str(grp_sep)
        # ScNumberValue requires a one-character decimal separator.
        if len(dec) != 1:
            return float("nan")
        if not s:
            return 0.0
        if grp != dec:
            # A decimal mark inside the group separator is IllegalArgument.
            if dec in grp:
                return float("nan")
            # Group marks are removed from the integer part only. Stripping
            # them from the fraction turned "1.2,3" into 1.23.
            split_at = s.find(dec)
            head, tail = (s, "") if split_at < 0 else (s[:split_at], s[split_at:])
            for ch in grp:
                head = head.replace(ch, "")
            s = head + tail
        if dec != ".":
            s = s.replace(dec, ".", 1)
        # Spaces, tabs, and line breaks are removed, then each trailing '%'
        # divides by 100. float("5%") and float("1 000") used to be NaN.
        s = "".join(ch for ch in s if ch not in " \t\r\n")
        percent = 0
        while s.endswith("%"):
            s = s[:-1]
            percent += 1
        if _NUMBER_TEXT.fullmatch(s) is None:
            return float("nan")
        value = float(s) * (10.0 ** (-2 * percent))
        return value if math.isfinite(value) else float("nan")
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def odd(n: Any) -> float:
    # ScOdd (interpr2.cxx) ceils away from zero, then steps off an even
    # integer. int(trunc(1.5)) is already odd, so odd(1.5) used to return 1.
    try:
        v = float(n)
        if not math.isfinite(v):
            return float("nan")
        # -0.0 == 0. copysign(1, -0.0) is -1, and ScOdd takes the >= 0 branch.
        if v == 0.0:
            return 1.0
        n_abs = math.ceil(abs(v))
        if n_abs % 2 == 0:
            n_abs += 1
        return math.copysign(float(n_abs), v)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def oddfprice(settlement: Any, maturity: Any, issue: Any, first_coupon: Any, rate: Any, yld: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    # GetOddfprice throws. issue and first_coupon are checked so a bad date
    # is NaN, and they do not change this regular-period approximation.
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
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # frequency 0 used to reach `r / f` and `y / f` (ZeroDivisionError). The
    # `n <= 0` check below only hides that when years is finite. Excel #NUM!.
    if f == 0:
        return float("nan")
    # basic PV approximation
    days_to_mat = _days_between(s, m, b)
    years = days_to_mat / 365.25 if b == 1 else days_to_mat / 360.0
    n = years * f
    if n <= 0 or not math.isfinite(n):
        return float("nan")

    # yld == -frequency makes (1 + y/f) zero. The coupon loop then divides by
    # zero. Excel ODDFPRICE is #NUM!.
    discount_base = 1.0 + y / f
    if abs(discount_base) < 1e-12:
        return float("nan")

    c = 100 * r / f
    price = sum(c / (discount_base ** i) for i in range(1, int(n) + 1))
    # A negative base raised to a float is a Python complex and does not raise.
    tail = discount_base ** n
    if isinstance(tail, complex) or tail == 0:
        return float("nan")
    price += red / tail
    if isinstance(price, complex) or not math.isfinite(price):
        return float("nan")
    return float(price)


def oddfyield(settlement: Any, maturity: Any, issue: Any, first_coupon: Any, rate: Any, pr: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    # GetOddfyield throws. This is Y = (C + (F-P)/n) / ((F+P)/2). Frequency
    # is validated and does not enter the approximation.
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
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if _f == 0:
        return float("nan")

    days_to_mat = _days_between(s, m, b)
    years = days_to_mat / 365.25 if b == 1 else days_to_mat / 360.0
    if years <= 0 or not math.isfinite(years):
        return float("nan")
    c = 100 * r
    # (red + price) / 2 is the denominator. Opposite signs of equal magnitude
    # used to raise ZeroDivisionError. Excel ODDFYIELD is #NUM!.
    if red + price == 0:
        return float("nan")
    approx_y = (c + (red - price) / years) / ((red + price) / 2)
    return approx_y


def oddlprice(settlement: Any, maturity: Any, last_interest: Any, rate: Any, yld: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    try:
        s = float(settlement)
        m = float(maturity)
        li = float(last_interest)
        r = float(rate)
        y = float(yld)
        red = float(redemption)
        f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # frequency 0 used to raise ZeroDivisionError on `360 / f` and `y / f`.
    # GetOddlprice cancels frequency, and a zero frequency is still #NUM!.
    if f == 0:
        return float("nan")
    # GetOddlprice (analysishelper.cxx). The old power of (1 + y/f) used
    # 365.25 or 360 for every other basis, so basis 3 was Actual/360.
    # A negative base raised to that fraction was a Python complex.
    from .calc_functions_t_z import yearfrac

    span_last_mat = yearfrac(li, m, b)
    span_set_mat = yearfrac(s, m, b)
    span_last_set = yearfrac(li, s, b)
    if (
        not math.isfinite(span_last_mat)
        or not math.isfinite(span_set_mat)
        or not math.isfinite(span_last_set)
        or span_last_mat <= 0
        or span_set_mat <= 0
    ):
        return float("nan")
    denom = 1.0 + span_set_mat * y
    if denom == 0.0:
        return float("nan")
    price = (red + span_last_mat * 100.0 * r) / denom - span_last_set * 100.0 * r
    if not math.isfinite(price):
        return float("nan")
    return float(price)


def pearson(data1: Any, data2: Any) -> float:
    try:
        cleaned = _clean_paired_arrays(data1, data2)
        if cleaned is None:
            return float("nan")
        d1_clean, d2_clean = cleaned
        if len(d1_clean) <= 1:
            return float("nan")
        # ScPearson is the correlation. np.corrcoef is the same value rsq squares.
        corr = float(np.corrcoef(d1_clean, d2_clean)[0, 1])
        return corr if math.isfinite(corr) else float("nan")
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def percentrank(data: Any, x: Any, significance: Any = 3) -> float:
    # Truncate at 10^sig (Excel/Calc), and sig must be >= 1. round() is not truncate.
    try:
        d = np.asarray(data, dtype=float).ravel()
        d = d[np.isfinite(d)]
        if len(d) == 0:
            return float("nan")
        val = float(x)
        sig = int(float(significance))
        if sig < 1:
            return float("nan")
        d_sorted = np.sort(d)
        if val < d_sorted[0] or val > d_sorted[-1]:
            return float("nan")

        n = len(d)
        if n == 1:
            return 1.0
        idx = np.searchsorted(d_sorted, val)
        if d_sorted[idx] == val:
            count_less = np.sum(d < val)
            res = count_less / (n - 1)
        else:
            idx = np.searchsorted(d_sorted, val) - 1
            x0, x1 = d_sorted[idx], d_sorted[idx + 1]
            r0, r1 = np.sum(d < x0) / (n - 1), np.sum(d < x1) / (n - 1)
            res = r0 + (r1 - r0) * (val - x0) / (x1 - x0)

        factor = 10**sig
        truncated = math.floor(round(res, sig + 4) * factor) / factor
        return float(truncated)
    except (ValueError, TypeError, IndexError, OverflowError):
        return float("nan")


def permut(n: Any, k: Any) -> float:
    try:
        n_val = int(float(n))
        k_val = int(float(k))
        if n_val < 0 or k_val < 0 or n_val < k_val:
            return float("nan")
        return float(math.perm(n_val, k_val))
    except (ValueError, TypeError, OverflowError):
        # int(float(+inf)) and float() of a huge permutation raise
        # OverflowError. The old except missed them. Excel PERMUT is #NUM!.
        return float("nan")


def pmt(rate: Any, nper: Any, pv: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    # Same guard as nper: a text cell used to raise ValueError from float().
    try:
        r = float(rate)
        n = float(nper)
        p = float(pv)
        f = float(fv_val)
        t = 1 if int(float(type_val)) == 1 else 0
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # nper == 0 used to raise ZeroDivisionError. numpy-financial returns
    # ±inf; Calc/Excel are #DIV/0!, returned here as NaN.
    return _npf_result("pmt", r, n, p, f, t)


def poisson(x: Any, mean: Any, cumulative: Any = False) -> float:
    try:
        # int(float(+inf)) raises OverflowError. The old except missed it,
        # so POISSON(+inf) crashed. Excel is #NUM!.
        k = int(float(x))
        m = float(mean)
        if k < 0 or m < 0:
            return float("nan")
        st = _scipy_stats()
        if st is None:
            return float("nan")

        if cumulative:
            return float(st.poisson.cdf(k, m))
        else:
            return float(st.poisson.pmf(k, m))
    except (ValueError, TypeError, OverflowError):
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
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def pv(rate: Any, nper: Any, pmt_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    # Same guard as nper: a text cell used to raise ValueError from float().
    try:
        r = float(rate)
        n = float(nper)
        pm = float(pmt_val)
        f = float(fv_val)
        t = 1 if int(float(type_val)) == 1 else 0
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    return _npf_result("pv", r, n, pm, f, t)


def quartile(r: Any, q: Any) -> float:
    # Non-numeric quart used to raise ValueError from float(). Out-of-range
    # quart fell through to `qi * 25` and np.percentile raised ValueError
    # (percentile outside 0..100). Excel QUARTILE returns #NUM! for both.
    try:
        qi = int(float(q))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if qi < 0 or qi > 4:
        return float("nan")
    # dtype=float raises ValueError on a text cell. The quart try above does
    # not cover the range, so that used to crash. Excel QUARTILE is #VALUE!.
    try:
        arr = np.asarray(r, dtype=float).ravel()
    except (ValueError, TypeError):
        return float("nan")
    arr = arr[~np.isnan(arr)]
    pct = (0.0, 25.0, 50.0, 75.0, 100.0)[qi]
    return float(np.percentile(arr, pct)) if len(arr) else float("nan")


def rank(val: Any, r: Any, order: int | float = 0) -> float:
    # Numbers only. Text, bools, and NaN are ignored; one text cell must not abort the call.
    try:
        target = float(val)
        if math.isnan(target):
            return float("nan")
        descending = int(float(order)) == 0
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    arr = _extract_numeric_array(r, propagate_nan=False).tolist()
    if not arr:
        return float("nan")
    if descending:
        arr.sort(reverse=True)
    else:
        arr.sort()
    try:
        return float(arr.index(target) + 1)
    except ValueError:
        return float("nan")


def _translate_calc_replacement(rep: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        s = m.group(0)
        if s == "$$":
            return "$"
        if s == "$&":
            return r"\g<0>"
        return rf"\g<{s[1:]}>"

    return re.sub(r"\$\$|\$&|\$[0-9]+", _sub, rep)


def regex(text: Any, expr: Any, replacement: Any | None = None, flags: Any = "") -> str | float:
    # Calc replacements are $1, $&, and $$. re.sub wants \g<n>, \g<0>, and a literal $.
    # ScRegex (interpr1.cxx): the 4th argument is an occurrence or a flag
    # string. Search returns the whole match. m.group(1) is None when an
    # optional group does not participate, so REGEX("b", "(a)?b") returned None.
    try:
        if text is None:
            text = ""
        text_str = str(text)
        expr_str = str(expr)
        occurrence = 1
        re_flags = 0
        global_replace = False
        if isinstance(flags, (bool, np.bool_)):
            return float("nan")
        if isinstance(flags, (int, float, np.integer, np.floating)):
            if not math.isfinite(float(flags)):
                return float("nan")
            occurrence = int(flags)
            if occurrence < 0:
                return float("nan")
        else:
            flag_text = str(flags).lower()
            if any(ch not in "gi" for ch in flag_text):
                return float("nan")
            if "i" in flag_text:
                re_flags |= re.IGNORECASE
            global_replace = "g" in flag_text
        # Occurrence 0 is the original text, even when the pattern is invalid.
        if occurrence == 0:
            return text_str
        if replacement is None:
            # "g" with no replacement does not comma-join every match.
            found = 0
            for match in re.finditer(expr_str, text_str, flags=re_flags):
                found += 1
                if found == occurrence:
                    return match.group(0)
            return "#N/A"
        rep_str = _translate_calc_replacement(str(replacement))
        seen = 0

        def _replace_nth(match: re.Match[str]) -> str:
            nonlocal seen
            seen += 1
            if global_replace or seen == occurrence:
                return match.expand(rep_str)
            return match.group(0)

        return re.sub(expr_str, _replace_nth, text_str, flags=re_flags)
    except re.error:
        return float("nan")


# ScRept rejects a result longer than kScInterpreterMaxStrLen (SAL_MAX_INT32 / 8).
_MAX_REPT_CHARS = 2**31 // 8


def rept(text: Any, n: Any) -> str | float:
    try:
        count = int(float(n))
    except (ValueError, TypeError, OverflowError):
        # A text count used to return "". GetStringPositionArgument does not.
        return float("nan")
    # `"text" * -1` is "" in Python and does not raise. Excel REPT truncates
    # first, then returns #VALUE! when the count is still negative.
    if count < 0:
        return float("nan")
    body = str(text)
    # Check before allocating. Excel's 32767 is not this limit.
    if count and body and count > _MAX_REPT_CHARS // len(body):
        return float("nan")
    return body * count


def rsq(data_y: Any, data_x: Any) -> float:
    cleaned = _clean_paired_arrays(data_y, data_x)
    if cleaned is None:
        return float("nan")
    y, x = cleaned
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
    # 1/cosh(x) goes to 0 as |x| grows. OverflowError is that limit, not an error.
    try:
        return float(1.0 / math.cosh(float(x)))
    except OverflowError:
        return 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


def seriessum(x: Any, n: Any, m: Any, coefficients: Any) -> float:
    # A complex or non-finite term is #NUM!. Python's (-x)**float is complex.
    try:
        x_val = float(x)
        n_val = float(n)
        m_val = float(m)
        coeffs = np.asarray(coefficients).ravel()
        res = 0.0
        for i, c in enumerate(coeffs):
            term = float(c) * (x_val ** (n_val + i * m_val))
            if isinstance(term, complex):
                return float("nan")
            res += term
        if isinstance(res, complex) or not math.isfinite(res):
            return float("nan")
        return float(res)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return float("nan")


def skew(*args: Any) -> float:
    # ScSkew (interpr3.cxx CalculateSkewOrSkewp, bSkewp false):
    # n * sum(((x - mean) / s)^3) / ((n - 1) * (n - 2)), s = sample stdev.
    # scipy.stats.skew(bias=False) is that formula. Text and bools stay out,
    # same as RANK.
    arr = _extract_numeric_array(*args)
    n = len(arr)
    if n < 3:
        return float("nan")
    s = float(np.std(arr, ddof=1))
    if s == 0.0 or not math.isfinite(s):
        return float("nan")
    mean = float(np.mean(arr))
    acc = float(np.sum(((arr - mean) / s) ** 3))
    res = n * acc / ((n - 1) * (n - 2))
    return float(res) if math.isfinite(res) else float("nan")


def slope(data_y: Any, data_x: Any) -> float:
    cleaned = _clean_paired_arrays(data_y, data_x)
    if cleaned is None:
        return float("nan")
    y, x = cleaned
    if len(y) < 2:
        return float("nan")
    mx, my = np.mean(x), np.mean(y)
    ss_xy = np.sum((x - mx) * (y - my))
    ss_xx = np.sum((x - mx) ** 2)
    return float(ss_xy / ss_xx) if ss_xx != 0 else float("nan")


def small(r: Any, k: Any) -> float:
    # Numbers only. Text, bools, and NaN are not part of the ranking.
    try:
        ki = int(float(k))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    arr = _extract_numeric_array(r, propagate_nan=False)
    if ki <= 0 or ki > len(arr):
        return float("nan")
    arr_sorted = np.sort(arr)
    return float(arr_sorted[ki - 1])


def sort(range_arr: Any, sort_index: int | float = 1, sort_order: int | float = 1, by_col: bool = False) -> list[Any] | float:
    # dtype=object so a mixed range stays mixed. Stable sort with reverse=
    # keeps tie order; reversing the index afterwards flips ties.
    arr = np.asarray(range_arr, dtype=object)
    if arr.size == 0:
        return []
    # Text, blank, and a bare number are 0-d. arr.shape[1] then raised
    # IndexError. Excel SORT of a non-range is #VALUE!, returned here as NaN.
    if arr.ndim == 0:
        return float("nan")
    # int(float()) on text or a blank sort_index/order used to raise ValueError.
    # max(1, ...) used to clamp a non-positive index onto the first row/column.
    # ScSort (interpr1.cxx) accepts only sort_order 1 and -1. >= 0 treated 0
    # and 2 as ascending (PushIllegalParameter).
    try:
        si = int(float(sort_index))
        order = float(sort_order)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if order != 1.0 and order != -1.0:
        return float("nan")
    asc = order > 0.0
    if arr.ndim == 1:
        indices = list(range(arr.size))
        indices.sort(key=lambda i: _calc_sort_key(arr[i]), reverse=not asc)
        return arr[indices].tolist()
    if bool(by_col):
        # Excel SORT(..., by_col=TRUE) orders columns by the sort_index-th ROW
        # (1-based; arr[si - 1, :]). The old path keyed off a column and then
        # transposed, so both the permutation and the result shape were wrong.
        # An index outside 1..nrows fell back to row 0. Excel is #VALUE!.
        if si < 1 or si > arr.shape[0]:
            return float("nan")
        col_indices = list(range(arr.shape[1]))
        col_indices.sort(key=lambda c: _calc_sort_key(arr[si - 1, c]), reverse=not asc)
        return arr[:, col_indices].tolist()
    # A column index outside 1..ncols fell back to column 0. Excel is #VALUE!.
    if si < 1 or si > arr.shape[1]:
        return float("nan")
    row_indices = list(range(arr.shape[0]))
    row_indices.sort(key=lambda r: _calc_sort_key(arr[r, si - 1]), reverse=not asc)
    return arr[row_indices].tolist()


def _sort_order_sign(val: Any) -> int | None:
    """Scalar Calc sort direction, or None when ``val`` is a by_array.

    ScSortBy accepts only 1 and -1. ``0`` is an illegal order, not another key:
    returning None made ``sortby(data, keys, 0, other)`` parse ``0`` as a key.
    Lists, tuples, ndarrays, and bools stay keys, so an omitted sort_order can
    be told apart from the next by_array.
    """
    if isinstance(val, (bool, np.bool_, np.ndarray, list, tuple)):
        return None
    try:
        num = float(val)
    except (ValueError, TypeError, OverflowError):
        return None
    if num == 1.0:
        return 1
    if num == -1.0:
        return -1
    if math.isfinite(num):
        return 0
    return None


def sortby(range_arr: Any, by_array: Any, sort_order: int | float = 1, *extra: Any) -> list[Any] | float:
    # dtype=object so a mixed range stays mixed. Sort keys from the last
    # backward so an earlier key wins ties.
    arr = np.asarray(range_arr, dtype=object)
    if arr.size == 0:
        return []
    # A scalar range is 0-d. arr[order] raised IndexError ("too many indices").
    # Excel SORTBY of a non-range is #VALUE!, returned here as NaN.
    if arr.ndim == 0:
        return float("nan")
    # *extra used to be ignored, so by_array2/sort_order2 never affected the order.
    specs: list[tuple[Any, int]] = []
    rest: list[Any] = list(extra)
    primary = _sort_order_sign(sort_order)
    if primary == 0:
        return float("nan")
    if primary is None:
        specs.append((by_array, 1))
        rest.insert(0, sort_order)
    else:
        specs.append((by_array, primary))
    idx = 0
    while idx < len(rest):
        key = rest[idx]
        sign = 1
        if idx + 1 < len(rest):
            nxt = _sort_order_sign(rest[idx + 1])
            if nxt == 0:
                return float("nan")
            if nxt is not None:
                sign = nxt
                idx += 1
        specs.append((key, sign))
        idx += 1

    # ScSortBy: a 1-D key sorts rows (length must match). A 2-D key of shape
    # (1, ncols) sorts columns; shape (nrows, 1) sorts rows. Mixed axes are
    # illegal. A 1-D range has no column axis, so a 2-D key is raveled.
    nrows = int(arr.shape[0] if arr.ndim > 1 else arr.size)
    ncols = int(arr.shape[1]) if arr.ndim > 1 else None
    axis: str | None = None
    keys: list[tuple[Any, bool]] = []
    for key, sign in specs:
        key_arr = np.asarray(key, dtype=object)
        if arr.ndim == 1 or key_arr.ndim <= 1:
            flat = key_arr.ravel()
            if int(flat.size) != nrows:
                return float("nan")
            this_axis = "row"
        elif (
            ncols is not None
            and key_arr.ndim == 2
            and tuple(key_arr.shape) == (1, ncols)
        ):
            flat = key_arr.ravel()
            this_axis = "col"
        elif key_arr.ndim == 2 and tuple(key_arr.shape) == (nrows, 1):
            flat = key_arr.ravel()
            this_axis = "row"
        else:
            return float("nan")
        if axis is None:
            axis = this_axis
        elif axis != this_axis:
            return float("nan")
        keys.append((flat, sign > 0))

    try:
        width = ncols if axis == "col" and ncols is not None else nrows
        order_list = list(range(width))
        for flat, asc in reversed(keys):
            target_flat = flat
            order_list.sort(key=lambda pos: _calc_sort_key(target_flat[pos]), reverse=not asc)
        if axis == "col":
            return arr[:, order_list].tolist()
        if arr.ndim == 1:
            return arr.ravel()[order_list].tolist()
        return arr[order_list].tolist()
    except (TypeError, ValueError, IndexError):
        return float("nan")


def sqrtpi(number: Any) -> float:
    try:
        n = float(number)
        if n < 0:
            return float("nan")
        return float(math.sqrt(n * math.pi))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def standardize(x: Any, mean: Any, stdev: Any) -> float:
    try:
        val = float(x)
        m = float(mean)
        s = float(stdev)
        if s <= 0:
            return float("nan")
        return float((val - m) / s)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def stdeva(*args: Any) -> float:
    vals = _collect_a_values(*args)
    if vals.size < 2:
        return float("nan")
    return float(np.std(vals, ddof=1))


def stdevpa(*args: Any) -> float:
    vals = _collect_a_values(*args)
    if not vals.size:
        return float("nan")
    return float(np.std(vals, ddof=0))


def steyx(data_y: Any, data_x: Any) -> float:
    # ScSTEYX is CalculatePearsonCovar. sqrt((Syy - Sxy^2/Sxx) / (n-2))
    # is that residual. The old path imported intercept, which imports slope
    # from this module, and walked the pairs three times.
    cleaned = _clean_paired_arrays(data_y, data_x)
    if cleaned is None:
        return float("nan")
    y, x = cleaned
    n = len(y)
    if n < 3:
        return float("nan")
    dx = x - np.mean(x)
    dy = y - np.mean(y)
    sxx = float(np.sum(dx * dx))
    if sxx == 0.0 or not math.isfinite(sxx):
        return float("nan")
    sxy = float(np.sum(dx * dy))
    syy = float(np.sum(dy * dy))
    resid = syy - (sxy * sxy) / sxx
    # A perfect line leaves a tiny negative from float noise.
    if resid < 0.0:
        resid = 0.0
    if not math.isfinite(resid):
        return float("nan")
    return float(math.sqrt(resid / (n - 2)))


def _cell_number(val: Any) -> float | None:
    """Calc number in a range cell, or None when the cell is blank or text.

    TRUE is 1 and FALSE is 0. Text, including ``"5"``, is not a number
    (GetDoubleArray / ScSumProduct skip ElementNaN and strings). OverflowError
    from a huge integer propagates so the caller can return NaN for the call.
    """
    if val is None or val == "":
        return None
    if isinstance(val, str):
        return None
    return float(val)


def subtotal(fn_num: Any, r: Any) -> float:
    # int(float(fn_num)) raises on text. Match small() / steyx and return NaN.
    try:
        fn = int(float(fn_num))
        # dtype=object keeps True and "5" distinct. Default asarray turned
        # [1, True, "5"] into strings, then float("True") dropped the bool
        # and float("5") added the text.
        flat = np.asarray(r, dtype=object).ravel()
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # ScSubTotal (interpr1.cxx) subtracts 100 once for the hidden-row codes
    # 101–111. This helper cannot see hidden rows, so 109 is still SUM.
    # fn % 100 mapped 201 onto AVERAGE and 211 onto VARP.
    if fn > 100:
        fn -= 100
    if fn < 1 or fn > 11:
        return float("nan")
    nums: list[float] = []
    for x in flat:
        try:
            v = _cell_number(x)
        except OverflowError:
            return float("nan")
        except (ValueError, TypeError):
            continue
        if v is None or math.isnan(v):
            continue
        nums.append(v)
    arr = np.asarray(nums, dtype=float)
    if fn == 1:
        # Empty AVERAGE is #DIV/0!.
        return float(np.mean(arr)) if len(arr) else float("nan")
    if fn == 2:
        return float(len(arr))
    if fn == 3:
        return float(sum(1 for x in flat if x is not None and x != ""))
    if fn == 4:
        return float(np.max(arr)) if len(arr) else 0.0
    if fn == 5:
        return float(np.min(arr)) if len(arr) else 0.0
    if fn == 6:
        # np.prod([]) is 1.0; the length guard keeps an empty PRODUCT at 0.
        return float(np.prod(arr)) if len(arr) else 0.0
    if fn == 7:
        # STDEV.S needs two samples. One point or none is #DIV/0!.
        return float(np.std(arr, ddof=1)) if len(arr) > 1 else float("nan")
    if fn == 8:
        # What was wrong: SUBTOTAL STDEV.P of an empty range returned 0.
        # How it happened: the empty guard was copied from PRODUCT, which
        # really is 0. np.std([]) is NaN, so the guard hid that and also
        # disagreed with ScStDevP (nValCount == 0 is #DIV/0!).
        # Why this change fixes it: no numbers is NaN; one number stays 0.
        return float(np.std(arr, ddof=0)) if len(arr) else float("nan")
    if fn == 9:
        return float(np.sum(arr))
    if fn == 10:
        # VAR.S needs two samples. One point or none is #DIV/0!.
        return float(np.var(arr, ddof=1)) if len(arr) > 1 else float("nan")
    if fn == 11:
        # What was wrong: SUBTOTAL VAR.P of an empty range returned 0.
        # How it happened: same empty-product guard as function 8.
        # ScVarP divides by the count, and an empty count is #DIV/0!.
        # Why this change fixes it: no numbers is NaN; one number stays 0.
        return float(np.var(arr, ddof=0)) if len(arr) else float("nan")
    return float("nan")


def sumif(r: Any, crit: Any, sr: Any | None = None) -> float:
    vals = _criteria_numbers(r, crit, sr)
    return float(sum(vals))


def sumifs(sr: Any, *args: Any) -> float:
    # Criteria ranges must match the sum range in shape. A shorter range, or a
    # 2x3 next to a 3x2, used to truncate or line up after ravel. That is NaN
    # here, same as an odd argument count. No pairs still sums nothing:
    # passing base_len to an empty pair list would sum the whole range.
    if len(args) % 2 != 0:
        return float("nan")
    if not args:
        return 0.0
    # A scalar string is not a range. np.asarray("text", dtype=object) is
    # shape (), so the shape check treated "text" and "" as a mismatch
    # against [1, 2] and returned NaN. Text and blanks contribute nothing,
    # same as sumif. The sum of no numbers is 0.
    if isinstance(sr, str):
        return 0.0
    sr_arr = np.asarray(sr, dtype=object)
    sr_flat = sr_arr.ravel()
    pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    mask = _multi_criteria_mask(pairs, base_len=len(sr_flat), base_shape=sr_arr.shape)
    if mask is None:
        return float("nan")
    total = 0.0
    for idx in range(len(sr_flat)):
        if mask[idx]:
            # Same rule as AVERAGEIFS: text and bools in the sum range
            # are ignored. float("5") and float(True) used to add them.
            cell = sr_flat[idx]
            if isinstance(cell, (bool, np.bool_, str)):
                continue
            try:
                val = float(cell)
                if not np.isnan(val):
                    total += val
            except (ValueError, TypeError, OverflowError):
                pass
    return float(total)


def sumproduct(*args: Any) -> float:
    if not args:
        return 0.0
    arrays = [np.asarray(a, dtype=object) for a in args]
    # Excel SUMPRODUCT returns #VALUE! when the arrays are not the same shape.
    # Ravelling and stopping at min_len used to drop the tail and return a partial sum.
    shape = arrays[0].shape
    if any(a.shape != shape for a in arrays):
        return float("nan")
    flat = [a.ravel() for a in arrays]
    total = 0.0
    for i in range(flat[0].size):
        prod = 1.0
        for arr in flat:
            try:
                val = _cell_number(arr[i])
            except OverflowError:
                # float(10**10000) used to escape the ValueError handler.
                return float("nan")
            except (ValueError, TypeError):
                prod = 0.0
                break
            # Blank and text are a zero factor. A numeric NaN stays NaN.
            if val is None:
                prod = 0.0
                break
            prod *= val
        total += prod
    return float(total)


def sumsq(*args: Any) -> float:
    total = 0.0
    for arg in args:
        for val in np.asarray(arg, dtype=object).ravel():
            try:
                num = _cell_number(val)
            except OverflowError:
                return float("nan")
            except (ValueError, TypeError):
                continue
            if num is None or math.isnan(num):
                continue
            total += num * num
    return float(total)


def py_str(val: Any) -> str:
    """Stringify for inline ``=PY()`` code without emitting the ``str(`` token."""
    return str(val)


def textafter(text: Any, delimiter: Any, instance_num: Any = 1, match_mode: Any = 0, match_end: Any = 0, if_not_found: Any = float("nan")) -> str | float:
    try:
        # bool("0") is true, so a zero flag invented a virtual delimiter.
        # ScTextBeforeOrAfter uses GetBool(). Imported here so this module
        # does not load t_z while t_z is still importing.
        from .calc_functions_t_z import _as_nonzero

        use_end = _as_nonzero(match_end)
        s, _delim, inst, idx, match_end_miss = _find_text_cut(
            text, delimiter, instance_num, match_mode, after=True
        )
        if idx is not None:
            # The cut is already the end of the delimiter, including a
            # negative instance. Adding len(delim) again skipped a second
            # copy when the matched span was longer than the delimiter.
            return s[idx:]
        if use_end and match_end_miss:
            # ScTextBeforeOrAfter inserts the virtual delimiter at 0
            # when the instance is negative. Text after that is the whole
            # string. A positive instance's virtual delimiter is the end,
            # so the remainder is empty. Both signs used to return "".
            return s if inst < 0 else ""
        return if_not_found
    except (ValueError, TypeError, OverflowError):
        # _find_text_cut does int(float(instance_num)). An infinite instance
        # raises OverflowError, which this handler did not catch, so TEXTAFTER
        # raised instead of the NaN used for any other invalid instance.
        return float("nan")

