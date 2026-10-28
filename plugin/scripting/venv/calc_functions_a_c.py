# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``calc``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import builtins
import calendar
import datetime as dt
import math
import operator
import sys
from typing import Any

import numpy as np

from .calc_functions_util import (
    _as_nonzero,
    _collect_a_values,
    _criteria_numbers,
    _date_to_serial,
    _days_between,
    _extract_numeric_array,
    _from_complex,
    _multi_criteria_mask,
    _npf_result,
    _round_half_up,
    _scipy_stats,
    _serial_to_date,
    match_criteria,
    yearfrac,
)
from .coerce import is_missing_value


__all__ = [
    "accrint",
    "accrintm",
    "acot",
    "acoth",
    "address",
    "aggregate",
    "amordegrc",
    "amorlinc",
    "arabic",
    "areas",
    "asc",
    "avedev",
    "averagea",
    "averageif",
    "averageifs",
    "base",
    "besseli",
    "besselj",
    "besselk",
    "bessely",
    "betadist",
    "betainv",
    "binomdist",
    "bitand",
    "bitlshift",
    "bitor",
    "bitrshift",
    "bitxor",
    "char",
    "chidist",
    "chiinv",
    "choose",
    "clean",
    "code",
    "combin",
    "combina",
    "complex",
    "confidence",
    "cot",
    "coth",
    "countif",
    "countifs",
    "coupdaybs",
    "coupdays",
    "coupdaysnc",
    "coupncd",
    "coupnum",
    "couppcd",
    "critbinom",
    "csc",
    "csch",
    "cumipmt",
    "cumprinc",
]


def _accrued(
    issue: Any,
    settlement: Any,
    rate: Any,
    par: Any,
    basis: Any,
    frequency: Any | None = None,
) -> float:
    """par * rate * yearfrac(issue, settlement), or NaN when the inputs are illegal.

    AnalysisAddIn::getAccrint and getAccrintm (financial.cxx) are this product.
    getAccrint also requires frequency in (1, 2, 4). Pass frequency only for that
    function. basis in (0..4), rate > 0, par > 0, issue < settlement.
    """
    # type errors are left to the worker exception path, which returns #VALUE!
    f_rate = float(rate)
    f_par = float(par) if par is not None else 1000.0
    b = int(float(basis))
    iss = float(issue)
    settle = float(settlement)
    freq = int(float(frequency)) if frequency is not None else None

    if f_rate <= 0.0 or f_par <= 0.0 or b not in (0, 1, 2, 3, 4) or iss >= settle:
        return float("nan")
    if freq is not None and freq not in (1, 2, 4):
        return float("nan")

    yf = yearfrac(iss, settle, b)
    # yearfrac NaN already propagates through the product
    return float(f_par * f_rate * yf)


def accrint(
    issue: Any,
    first_interest: Any,
    settlement: Any,
    rate: Any,
    par: Any = 1000.0,
    frequency: Any = 1,
    basis: Any = 0,
    calc_method: Any = True,
) -> float:
    # getAccrint ignores first_interest (the parameter is /*nFirstInter*/) and
    # has no calc_method argument. Both stay in the signature so Excel-shaped
    # callers still parse.
    return _accrued(issue, settlement, rate, par, basis, frequency)


def accrintm(issue: Any, settlement: Any, rate: Any, par: Any = 1000.0, basis: Any = 0) -> float:
    return _accrued(issue, settlement, rate, par, basis)


# type errors are left to the worker exception path, which returns #VALUE!
def acot(x: Any) -> float:
    xv = float(x)
    return float(math.pi / 2 - math.atan(xv))


# |x|<=1 stays NaN (#NUM!). Type errors are left to the worker exception path, which returns #VALUE!.
def acoth(x: Any) -> float:
    xv = float(x)
    if abs(xv) <= 1:
        return float("nan")
    return float(0.5 * math.log((xv + 1) / (xv - 1)))


def address(row: Any, col: Any, abs_num: Any = 1, a1: Any = True, sheet: Any = None) -> str:
    # abs_num is 1..4 (#VALUE! otherwise). A sheet name quotes ' as ''.
    try:
        r = int(float(row))
        c = int(float(col))
        abs_n = int(float(abs_num))
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"
    if r < 1 or c < 1 or abs_n not in (1, 2, 3, 4):
        return "#VALUE!"
    try:
        is_a1 = True if (a1 is None or a1 == "") else _as_nonzero(a1)
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"

    if is_a1:
        # A1 style
        col_str = ""
        temp_c = c
        while temp_c > 0:
            temp_c, rem = divmod(temp_c - 1, 26)
            col_str = chr(65 + rem) + col_str

        row_abs = "$" if abs_n in (1, 2) else ""
        col_abs = "$" if abs_n in (1, 3) else ""
        res = f"{col_abs}{col_str}{row_abs}{r}"
    else:
        # R1C1 style
        r_str = f"R{r}" if abs_n in (1, 2) else f"R[{r}]"
        c_str = f"C{c}" if abs_n in (1, 3) else f"C[{c}]"
        res = f"{r_str}{c_str}"

    if sheet is not None and str(sheet) != "":
        sheet_str = str(sheet).replace("'", "''")
        res = f"'{sheet_str}'!{res}"
    return res


def aggregate(function_num: Any, options: Any, *args: Any) -> float:
    # ScAggregate dispatches 1..11 like ScSubTotal, plus MEDIAN (12) and
    # MODE.SNGL (13). 14..19 need k and stay unimplemented. Bool is not a
    # number for the numeric reductions (float(True) would be 1.0). COUNTA
    # still counts a bool. Do not share this switch with subtotal(): that
    # helper counts True as 1, which test_aggregate_stubs_and_bools forbids.
    # non-numeric function_num/options are left to the worker exception path, which returns #VALUE!
    fn = int(float(function_num))
    opt = int(float(options))
    assert 1 <= fn <= 13
    numeric: list[float] = []
    n_text = 0
    n_bool = 0
    for arg in args:
        for x in np.asarray(arg, dtype=object).ravel():
            if isinstance(x, (bool, np.bool_)):
                # Numeric reductions ignore bools. COUNTA must still see
                # them: skipping here made AGGREGATE(3, …, TRUE) return 0.
                n_bool += 1
                continue
            if isinstance(x, str):
                if is_missing_value(x):
                    # Blank strings are ignored. Error tokens stay NaN unless
                    # the option below strips them.
                    if x.strip() != "":
                        numeric.append(float("nan"))
                    continue
                n_text += 1
                continue
            try:
                numeric.append(float(x))
            except (ValueError, TypeError, OverflowError):
                continue

    arr = np.asarray(numeric, dtype=float)
    if opt in (2, 3, 6, 7):
        arr = arr[~np.isnan(arr)]
    elif arr.size and bool(np.isnan(arr).any()):
        # Options 0, 1, 4, 5 keep errors. nanmax, nanmin, and COUNT
        # skip NaN, so an error token disappeared and the call returned
        # a number. Calc returns the error instead.
        return float("nan")

    # Simplified implementations for most common
    if fn == 1:
        # Empty after dropping text/errors is #DIV/0! in Calc; avoid the
        # RuntimeWarning from np.mean([]).
        return float(np.mean(arr)) if arr.size else float("nan")
    if fn == 2:
        return float(np.sum(~np.isnan(arr)))
    if fn == 3:
        return float(len(arr) + n_text + n_bool)
    if fn == 4:
        # ScMax pushes 0 when nothing numeric remains (text is ignored).
        # np.nanmax([]) raises ValueError. Empty is 0 here, not that exception.
        return float(np.nanmax(arr)) if arr.size else 0.0
    if fn == 5:
        # Same empty rule as ScMin / ScMax.
        return float(np.nanmin(arr)) if arr.size else 0.0
    if fn == 6:
        # np.prod([]) is 1. Calc AGGREGATE PRODUCT is 0 when ignore-errors
        # or text leaves no numbers (AGGREGATE(6,6,NA(),NA()) is 0).
        return float(np.prod(arr)) if arr.size else 0.0
    if fn == 7:
        # One sample makes np.std(ddof=1) warn and return NaN. Calc
        # STDEV.S of one value is #DIV/0!. Same guard as subtotal.
        return float(np.std(arr, ddof=1)) if arr.size > 1 else float("nan")
    if fn == 8:
        # ScStDevP: nValCount == 0 is #DIV/0!. np.std([]) warns and
        # returns NaN. One value is 0 (population, ddof=0), not #DIV/0!.
        return float(np.std(arr, ddof=0)) if arr.size else float("nan")
    if fn == 9:
        # np.sum([]) is already 0. That matches Calc AGGREGATE SUM after an
        # ignore-errors option strips every value.
        return float(np.sum(arr)) if arr.size else 0.0
    if fn == 10:
        # One sample makes np.var(ddof=1) warn and return NaN.
        return float(np.var(arr, ddof=1)) if arr.size > 1 else float("nan")
    if fn == 11:
        # ScVarP divides by the count. An empty count is #DIV/0!.
        # np.var([]) warns. One value is 0.
        return float(np.var(arr, ddof=0)) if arr.size else float("nan")
    if fn == 12:
        # GetMedian sets NoValue (#N/A) on an empty vector. np.median([])
        # is NaN plus a RuntimeWarning. Keep the NaN, skip the call.
        return float(np.median(arr)) if arr.size else float("nan")
    if fn == 13:
        # ScModalValue on the numbers already kept above. mode() would
        # count True as 1 if given the raw args. Tie-break is the lowest
        # value. No repeat is #N/A (NaN).
        from .calc_functions_i_m import mode

        return float(mode(arr.tolist()))
    return float("nan")


def _round_amor(value: float) -> float:
    """Integer round, half away from zero.

    GetAmordegrc uses rtl::math::round (analysishelper.cxx). Python
    round() is half-to-even, so 0.5 became 0 and 2.5 became 2.
    """
    return float(_round_half_up(value, 0))


def _parse_amor(
    cost: Any,
    date_purchased: Any,
    first_period: Any,
    salvage: Any,
    period: Any,
    rate: Any,
    basis: Any,
) -> tuple[float, float, float, float, int, float, int] | None:
    """Shared AMORDEGRC / AMORLINC argument check. None means #NUM!."""
    # type errors are left to the worker exception path, which returns #VALUE!
    f_cost = float(cost)
    d_purch = float(date_purchased)
    d_first = float(first_period)
    f_salv = float(salvage)
    n_per = int(float(period))
    f_rate = float(rate)
    b = int(float(basis))

    if (
        d_purch > d_first
        or f_rate <= 0.0
        or f_salv > f_cost
        or f_cost <= 0.0
        or f_salv < 0
        or n_per < 0
        or b not in (0, 1, 2, 3, 4)
    ):
        return None
    return f_cost, d_purch, d_first, f_salv, n_per, f_rate, b


def amordegrc(
    cost: Any,
    date_purchased: Any,
    first_period: Any,
    salvage: Any,
    period: Any,
    rate: Any,
    basis: Any = 0,
) -> float:
    # French declining-balance schedule from LibreOffice
    # ScInterpreter AnalysisAddIn::getAmordegrc.
    parsed = _parse_amor(cost, date_purchased, first_period, salvage, period, rate, basis)
    if parsed is None:
        return float("nan")
    f_cost, d_purch, d_first, f_salv, n_per, f_rate, b = parsed

    use_per = 1.0 / f_rate
    if use_per < 3.0:
        amor_coeff = 1.0
    elif use_per < 5.0:
        amor_coeff = 1.5
    elif use_per <= 6.0:
        amor_coeff = 2.0
    else:
        amor_coeff = 2.5

    f_rate *= amor_coeff
    yf = yearfrac(d_purch, d_first, b)
    if math.isnan(yf):
        return float("nan")
    f_n_rate = _round_amor(yf * f_rate * f_cost)
    f_cost -= f_n_rate
    f_rest = f_cost - f_salv

    for n in range(n_per):
        f_n_rate = _round_amor(f_rate * f_cost)
        f_rest -= f_n_rate
        if f_rest < 0.0:
            rem = n_per - n
            if rem in (0, 1):
                return _round_amor(f_cost * 0.5)
            else:
                return 0.0
        f_cost -= f_n_rate

    return float(f_n_rate)


def amorlinc(
    cost: Any,
    date_purchased: Any,
    first_period: Any,
    salvage: Any,
    period: Any,
    rate: Any,
    basis: Any = 0,
) -> float:
    # French linear schedule from LibreOffice
    # ScInterpreter AnalysisAddIn::getAmorlinc.
    parsed = _parse_amor(cost, date_purchased, first_period, salvage, period, rate, basis)
    if parsed is None:
        return float("nan")
    f_cost, d_purch, d_first, f_salv, n_per, f_rate, b = parsed

    f_one_rate = f_cost * f_rate
    f_cost_delta = f_cost - f_salv
    yf = yearfrac(d_purch, d_first, b)
    if math.isnan(yf):
        return float("nan")
    f0_rate = yf * f_rate * f_cost
    # Two positive floats can still underflow to 0. The division below
    # has no handler, so that product has to be rejected here.
    if f_one_rate == 0:
        return float("nan")
    num_full_periods = int((f_cost - f_salv - f0_rate) / f_one_rate)

    if n_per == 0:
        res = f0_rate
    elif n_per <= num_full_periods:
        res = f_one_rate
    elif n_per == num_full_periods + 1:
        res = f_cost_delta - f_one_rate * num_full_periods - f0_rate
    else:
        res = 0.0

    return float(res) if res > 0.0 else 0.0


_ARABIC_DIGITS: dict[str, tuple[int, bool]] = {
    "M": (1000, True),
    "D": (500, False),
    "C": (100, True),
    "L": (50, False),
    "X": (10, True),
    "V": (5, False),
    "I": (1, True),
}


def _arabic_digit(ch: str) -> tuple[int, bool] | None:
    """ScArabic's lcl_GetArabicValue. The bool is true for a power of ten."""
    return _ARABIC_DIGITS.get(ch)


def arabic(text: Any) -> float:
    # ScArabic (interpr2.cxx) walks the numeral with nValidRest starting at
    # 3999. A digit followed by exactly twice itself (VX, LC, DM) is illegal,
    # and so is a value past 3999 (MMMM). The old loop accepted those: VX
    # became 5 and MMMM became 4000. Simplified forms that ROMAN modes emit
    # stay valid (IIII is 4, IC is 99, XMIX is 999). The help page says
    # ARABIC("") is 0. "-" is not a roman digit, so "-IV" is NaN.
    roman = str(text).upper().strip()
    value = 0
    valid_rest = 3999
    index = 0
    length = len(roman)
    while index < length:
        first = _arabic_digit(roman[index])
        if first is None:
            return float("nan")
        digit, is_ten_power = first
        next_digit = 0
        if index + 1 < length:
            second = _arabic_digit(roman[index + 1])
            if second is None:
                return float("nan")
            next_digit = second[0]
        if digit >= next_digit:
            value += digit
            valid_rest %= digit * (5 if is_ten_power else 2)
            if valid_rest < digit:
                return float("nan")
            valid_rest -= digit
            index += 1
        elif digit * 2 != next_digit:
            diff = next_digit - digit
            value += diff
            if valid_rest < diff:
                return float("nan")
            valid_rest = digit - 1
            index += 2
        else:
            return float("nan")
    return float(value)


def areas(r: Any) -> float:
    """Guess how many areas this Python value looks like.

    A list or tuple whose first cell is itself a list or tuple counts as
    that many areas (the length of the outer sequence). Every other value,
    including one flat range, is 1. This is a shape guess, not a Calc
    reference.
    """
    if isinstance(r, (list, tuple)) and r and isinstance(r[0], (list, tuple)) and len(r[0]) > 0 and isinstance(r[0][0], (list, tuple)):
        return float(len(r))
    return 1.0


def asc(text: Any) -> str:
    # Full-width ASCII (U+FF01–U+FF5E) to ASCII, and ideographic space
    # U+3000 to a normal space. Half-width katakana is not converted.
    # type errors are left to the worker exception path, which returns #VALUE!
    if text is None:
        return ""
    s = str(text)
    # Shift ASCII (Fullwidth is U+FF01 to U+FF5E) -> (U+0021 to U+007E)
    # Fullwidth Space U+3000 -> U+0020
    res = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            res.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            res.append(" ")
        else:
            res.append(ch)
    return "".join(res)


def avedev(*args: Any) -> float:
    # Excel/Calc AVEDEV ignores text and logicals.
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True, propagate_nan=False)
    if not arr.size:
        return float("nan")
    return float(np.mean(np.abs(arr - np.mean(arr))))


def averagea(*args: Any) -> float:
    # Blank, None, and whitespace stay in the denominator as 0, same as text.
    # Calc AVERAGEA(10,"",20) is 10. Skipping those values made this 15 and
    # failed the spreadsheet-import translate test. _collect_a_values maps
    # missing cells through _to_float_a, which returns 0 for them.
    vals = _collect_a_values(*args)
    if not vals.size:
        return float("nan")
    return float(np.mean(vals))


def averageif(r: Any, crit: Any, ar: Any | None = None) -> float:
    vals = _criteria_numbers(r, crit, ar)
    if vals is None or not vals:
        return float("nan")
    return float(np.mean(vals))


def _criteria_pairs(args: tuple[Any, ...]) -> list[tuple[Any, Any]] | None:
    """Criteria pairs for AVERAGEIFS and COUNTIFS. None means #VALUE!.

    Odd or empty args are None. SUMIFS uses a different empty/odd contract.
    """
    if len(args) % 2 != 0 or not args:
        return None
    return [(args[i], args[i + 1]) for i in range(0, len(args), 2)]


def averageifs(ar: Any, *args: Any) -> float | str:
    # Criteria ranges must match the average range in shape, not just in cell
    # count. A 2x3 range and a 3x2 range both ravel to 6 and used to be
    # averaged. Calc returns #VALUE!. No pairs used to average the whole range.
    pairs = _criteria_pairs(args)
    if pairs is None:
        return "#VALUE!"
    vals = _criteria_numbers(ar, pairs=pairs)
    if vals is None:
        return "#VALUE!"
    if not vals:
        return float("nan")
    return float(np.mean(vals))


def base(number: Any, radix: Any, min_length: Any = 0) -> str:
    try:
        n = int(float(number))
        r = int(float(radix))
        m = int(float(min_length))
        # Literal "NaN" is not a Calc error token, so bad BASE input showed up as
        # text. Excel/Calc BASE returns #NUM! for a bad number, radix, or length.
        # zfill(1e9) allocates a gigabyte string and hangs the worker.
        # Excel cells hold 32767 characters.
        if n < 0 or r < 2 or r > 36 or m < 0 or m > 32767:
            return "#NUM!"
        if n == 0:
            return "0".zfill(m)
        digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = ""
        while n > 0:
            res = digits[n % r] + res
            n //= r
        return res.zfill(m)
    except (ValueError, TypeError, OverflowError):
        return "#NUM!"


def _bessel_eval(scipy_fn: Any, x: Any, n: Any, *, positive_x: bool) -> float:
    """Parse Bessel x and order. positive_x is the kn/yn rule (x > 0).

    Type and scipy errors propagate to _bessel_call, which returns NaN.
    """
    # What was wrong: BESSELK(1, -1) returned kn(1, 1) and BESSELY(1, -1)
    # returned a finite value. Both are illegal orders.
    # How it happened: iv/jv rejected a negative order, but kn/yn only
    # rejected x <= 0. scipy.special.kn(-n, x) equals kn(n, x).
    # Why this stays: AnalysisAddIn::getBesselk and getBessely throw
    # IllegalArgumentException when nOrder < 0 or fNum <= 0. NaN is that
    # error here. iv/jv still allow x <= 0 (positive_x=False).
    xv = float(x)
    nv = int(float(n))
    if nv < 0 or (positive_x and xv <= 0):
        return float("nan")
    return float(scipy_fn(nv, xv))


def _bessel_call(name: str, x: Any, n: Any, *, positive_x: bool) -> float:
    """One scipy.special Bessel, or NaN when scipy is missing or the input is illegal.

    A missing install is the same NaN as _npf_result, not a raised ImportError.
    """
    try:
        import scipy.special  # type: ignore[import-untyped]

        return _bessel_eval(getattr(scipy.special, name), x, n, positive_x=positive_x)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def besseli(x: Any, n: Any) -> float:
    return _bessel_call("iv", x, n, positive_x=False)


def besselj(x: Any, n: Any) -> float:
    return _bessel_call("jv", x, n, positive_x=False)


def besselk(x: Any, n: Any) -> float:
    return _bessel_call("kn", x, n, positive_x=True)


def bessely(x: Any, n: Any) -> float:
    return _bessel_call("yn", x, n, positive_x=True)


def _scipy_parsed(parse: Any) -> tuple[Any, Any] | None:
    """(scipy.stats, parse()) or None.

    None means scipy is missing, or parse raised ValueError, TypeError, or
    OverflowError. Callers return nan for None. Any other exception propagates.
    Call only after an arity check. The stats method stays in the caller,
    outside this try.
    """
    stats = _scipy_stats()
    if stats is None:
        return None
    try:
        return stats, parse()
    except (ValueError, TypeError, OverflowError):
        return None


def _beta_params(args: tuple[Any, ...]) -> tuple[Any, Any] | None:
    """(scipy.stats, (first, alpha, beta, A, B)) for BETADIST and BETAINV.

    Arity is 3..5 and is checked before scipy is imported. A defaults to 0,
    B to 1. Domain checks stay in the callers.
    """
    if len(args) < 3 or len(args) > 5:
        return None
    return _scipy_parsed(lambda: (
        float(args[0]),
        float(args[1]),
        float(args[2]),
        float(args[3]) if len(args) > 3 else 0.0,
        float(args[4]) if len(args) > 4 else 1.0,
    ))


def betadist(*args: Any) -> float:
    # Legacy BETADIST(x, alpha, beta, [A], [B]) is always cumulative.
    # args[3] is the lower bound A (default 0), not a cumulative flag.
    got = _beta_params(args)
    if got is None:
        return float("nan")
    stats, (x, alpha, beta, A, B) = got

    if alpha <= 0.0 or beta <= 0.0 or A >= B or x < A or x > B:
        return float("nan")
    x_norm = (x - A) / (B - A)
    return float(stats.beta.cdf(x_norm, alpha, beta))


def betainv(*args: Any) -> float:
    # p in [0, 1], alpha > 0, beta > 0, A < B. scipy's ppf does not enforce that.
    got = _beta_params(args)
    if got is None:
        return float("nan")
    stats, (p, alpha, beta, A, B) = got

    if p < 0.0 or p > 1.0 or alpha <= 0.0 or beta <= 0.0 or A >= B:
        return float("nan")
    return float(stats.beta.ppf(p, alpha, beta) * (B - A) + A)


def binomdist(*args: Any) -> float:
    # 0 <= k <= n, n >= 0, 0 <= p <= 1. scipy returns 0 for k > n; Calc wants #NUM!.
    if len(args) < 4:
        return float("nan")
    got = _scipy_parsed(lambda: (
        int(float(args[0])),
        int(float(args[1])),
        float(args[2]),
        False if (args[3] is None or args[3] == "") else _as_nonzero(args[3]),
    ))
    if got is None:
        return float("nan")
    stats, (k, n, p, cum) = got
    if k < 0 or k > n or n < 0 or p < 0 or p > 1:
        return float("nan")
    if cum:
        return float(stats.binom.cdf(k, n, p))
    else:
        return float(stats.binom.pmf(k, n, p))


_MAX_BIT_VALUE: int = (1 << 48) - 1


def _bit_arg(x: Any) -> int | None:
    """int(float(x)), or None on OverflowError.

    ValueError and TypeError propagate. Callers return nan on None before
    converting the next argument, matching the old single try.
    """
    try:
        return int(float(x))
    except OverflowError:
        return None


def _int_bitwise(op: Any, n1: Any, n2: Any) -> float:
    """Apply a binary integer bitwise operator to n1 and n2 within [0, 2^48 - 1]."""
    # Non-negative integers below 2^48. Excel and Calc return #NUM! otherwise.
    # type errors are left to the worker exception path, which returns #VALUE!
    v1 = _bit_arg(n1)
    if v1 is None:
        return float("nan")
    v2 = _bit_arg(n2)
    if v2 is None or v1 < 0 or v1 > _MAX_BIT_VALUE or v2 < 0 or v2 > _MAX_BIT_VALUE:
        return float("nan")
    return float(op(v1, v2))


def _int_shift(number: Any, shift: Any, *, left: bool) -> float:
    """Integer bit shift within [0, 2^48 - 1] and shift magnitude <= 53."""
    # Shift at most 53 bits (Excel) and the number stays in [0, 2^48 - 1].
    # A shift of 1e9 builds an integer large enough to hang or raise MemoryError.
    # type errors are left to the worker exception path, which returns #VALUE!
    # With those bounds the result is at most 101 bits, so MemoryError cannot happen.
    n = _bit_arg(number)
    if n is None:
        return float("nan")
    s = _bit_arg(shift)
    if s is None or n < 0 or n > _MAX_BIT_VALUE or abs(s) > 53:
        return float("nan")
    mag = abs(s)
    # bool(left) keeps today's truthiness. left == (s >= 0) differs for non-bool left.
    if (s >= 0) == bool(left):
        return float(n << mag)
    return float(n >> mag)


def bitand(n1: Any, n2: Any) -> float:
    return _int_bitwise(operator.and_, n1, n2)


def bitlshift(number: Any, shift: Any) -> float:
    return _int_shift(number, shift, left=True)


def bitor(n1: Any, n2: Any) -> float:
    return _int_bitwise(operator.or_, n1, n2)


def bitrshift(number: Any, shift: Any) -> float:
    return _int_shift(number, shift, left=False)


def bitxor(n1: Any, n2: Any) -> float:
    return _int_bitwise(operator.xor, n1, n2)


def char(n: Any) -> str:
    # Bad input used to return "" (and inf escaped as OverflowError). chr() also
    # accepts code points above 255; Excel/Calc CHAR returns #VALUE! for those.
    try:
        code = int(float(n))
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"
    if code < 0 or code > 255:
        return "#VALUE!"
    return chr(code)


def chidist(x: Any, df: Any) -> float:
    # ScChiDist (interpr3.cxx), Excel path: x < 0 is #NUM!. ODFF returns 1
    # for x <= 0; scipy's sf(-1, df) is 1.0, and this helper follows the
    # Excel path, same as binomdist. x == 0 stays 1. df is truncated.
    # df < 1 or df > 1e10 is #NUM! (Excel; LO enforces the cap on
    # ScChiSqDist_MS). int(df) rejected the string "3.5".
    got = _scipy_parsed(lambda: (float(x), int(float(df))))
    if got is None:
        return float("nan")
    stats, (xv, dof) = got
    if xv < 0.0 or dof < 1 or dof > 10**10:
        return float("nan")
    return float(stats.chi2.sf(xv, dof))


def chiinv(p: Any, df: Any) -> float:
    # ScChiInv: df < 1, p <= 0, or p > 1 is #NUM!. scipy isf(0, df) is inf.
    # p == 1 stays 0. df uses int(float()) so "3.5" truncates. df > 1e10
    # matches Excel CHIINV and the chidist cap above.
    got = _scipy_parsed(lambda: (float(p), int(float(df))))
    if got is None:
        return float("nan")
    stats, (pv, dof) = got
    if dof < 1 or dof > 10**10 or pv <= 0.0 or pv > 1.0:
        return float("nan")
    return float(stats.chi2.isf(pv, dof))


def choose(index: Any, *args: Any) -> Any:
    # Out-of-range stays NaN (#NUM! in the cell) so a negative index does not wrap.
    # non-numeric indexes are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    idx = int(float(index))
    if 1 <= idx <= len(args):
        return args[idx - 1]
    return float("nan")


def clean(text: Any) -> str | float:
    # type errors are left to the worker exception path, which returns #VALUE!
    if text is None:
        return ""
    if isinstance(text, float) and math.isnan(text):
        return float("nan")
    s = str(text)
    # Excel CLEAN removes C0 controls (ord < 32) and DEL (U+007F). The
    # ord < 32 filter left chr(127) in the result.
    return "".join(c for c in s if ord(c) >= 32 and ord(c) != 127)


def code(s: Any) -> float:
    # ScCode pushes 0 for an empty string. Excel CODE("") is #VALUE!.
    # This helper follows Calc. str(None) is "None", so a missing object
    # stays the code point of "N"; do not treat None as blank here.
    # type errors are left to the worker exception path, which returns #VALUE!
    ss = str(s)
    if not ss:
        return 0.0
    return float(ord(ss[0]))


def _binom_float(n: int, k: int, *, reject_k_gt_n: bool) -> float:
    """Binomial coefficient as a float, or NaN when the result is not finite.

    math.comb(n, k) is 0 when k > n. ScCombin (interpr3.cxx) rejects that
    for COMBIN. COMBINA still wants C(n+k-1, k); that coefficient is 0
    when the bottom exceeds the top (COMBINA(0, 1)), so the caller turns
    the rejection off.

    math.comb(1e9, 5e8) builds a giant int before float() raises. After
    folding k to min(k, n-k), C(n, k) >= C(2k, k), which no longer fits
    in float64 once k is about 515. lgamma rejects that case first.
    """
    if n < 0 or k < 0:
        return float("nan")
    if k > n:
        return float("nan") if reject_k_gt_n else 0.0
    k = min(k, n - k)
    log_c = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    if not math.isfinite(log_c) or log_c > math.log(sys.float_info.max):
        return float("nan")
    return float(math.comb(n, k))


def combin(n: Any, k: Any) -> float:
    # ScCombin rejects k > n and any negative. math.comb(3, 5) is 0.
    # non-numeric arguments are left to the worker exception path. OverflowError (inf) is #NUM!; ValueError is #VALUE!.
    return _binom_float(int(float(n)), int(float(k)), reject_k_gt_n=True)


def combina(n: Any, k: Any) -> float:
    # ScCombinA also rejects k > n. Excel COMBINA(3, 5) is 21, so this
    # helper still evaluates C(n+k-1, k) in that case. n < 0 has to be
    # NaN: combina(-2, 6) is math.comb(3, 6), which is 0.
    # non-numeric arguments are left to the worker exception path. OverflowError (inf) is #NUM!; ValueError is #VALUE!.
    ni = int(float(n))
    ki = int(float(k))
    if ni < 0 or ki < 0:
        return float("nan")
    if ni == 0 and ki == 0:
        return 1.0
    return _binom_float(ni + ki - 1, ki, reject_k_gt_n=False)


def complex(real_num: Any, imag_num: Any = 0.0, suffix: Any = "i") -> str:
    # The Calc name shadows the builtin. The body uses builtins.complex.
    # Suffix is i or j (either case). Anything else is #VALUE!.
    try:
        r = float(real_num)
        i = float(imag_num)
        s = str(suffix).strip()
        if s.lower() not in ("i", "j"):
            return "#VALUE!"
        return _from_complex(builtins.complex(r, i), suffix=s.lower())
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"


def confidence(alpha: Any, stddev: Any, size: Any) -> float:
    # alpha strictly inside (0, 1), stddev > 0, size >= 1. Otherwise #NUM!.
    got = _scipy_parsed(lambda: (float(alpha), float(stddev), int(float(size))))
    if got is None:
        return float("nan")
    stats, (a, sd, n) = got
    if a <= 0.0 or a >= 1.0 or sd <= 0.0 or n < 1:
        return float("nan")
    return float(stats.norm.ppf(1 - a / 2.0) * sd / math.sqrt(n))


# ZeroDivisionError is left to the worker exception path, which returns #DIV/0!. Type errors return #VALUE!.
def cot(x: Any) -> float:
    return float(1.0 / math.tan(float(x)))


# ZeroDivisionError is left to the worker exception path, which returns #DIV/0!. Type errors return #VALUE!.
def coth(x: Any) -> float:
    return float(1.0 / math.tanh(float(x)))


def countif(r: Any, crit: Any) -> float:
    r_flat = np.asarray(r, dtype=object).ravel()
    cnt = 0
    for val in r_flat:
        if match_criteria(val, crit):
            cnt += 1
    return float(cnt)


def countifs(*args: Any) -> float | str:
    # Every criteria range must be the same shape. A 2x3 and a 3x2 both have
    # 6 cells; comparing raveled lengths counted them. Calc returns #VALUE!.
    # No pairs used to sum an empty mask and return 0.
    pairs = _criteria_pairs(args)
    if pairs is None:
        return "#VALUE!"
    mask = _multi_criteria_mask(pairs)
    if mask is None:
        return "#VALUE!"
    return float(np.sum(mask))


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


def _coupon_schedule(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> tuple[float, float, float, float, float, float]:
    """Compute coupon schedule: (p_ser, c_ser, days_in_per, k, a_days, dsc_days)."""
    b = int(float(basis))
    p_ser, c_ser, days_in_per, k = _get_coupon_dates(settlement, maturity, frequency, b)
    s_date = _serial_to_date(settlement)
    assert s_date is not None
    s_ser = _date_to_serial(s_date)
    a_days = float(_days_between(p_ser, s_ser, b))
    dsc_days = float(_days_between(s_ser, c_ser, b))
    return p_ser, c_ser, days_in_per, k, a_days, dsc_days


def _coupon(settlement: Any, maturity: Any, frequency: Any, basis: Any, idx: int) -> float:
    # _coupon_schedule order: 0 p_ser, 1 c_ser, 2 days_in_per, 3 k, 4 a_days, 5 dsc_days.
    try:
        return _coupon_schedule(settlement, maturity, frequency, basis)[idx]
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def coupdaybs(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    # Coupon dates step by calendar months. frequency in (1, 2, 4), basis in
    # (0..4), settlement < maturity. A fixed day step (180, 182.5) drifts.
    return _coupon(settlement, maturity, frequency, basis, 4)


def coupdays(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return float(_coupon(settlement, maturity, frequency, basis, 2))


def coupdaysnc(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return _coupon(settlement, maturity, frequency, basis, 5)


def coupncd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return float(_coupon(settlement, maturity, frequency, basis, 1))


def coupnum(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return float(_coupon(settlement, maturity, frequency, basis, 3))


def couppcd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return float(_coupon(settlement, maturity, frequency, basis, 0))


def critbinom(trials: Any, prob: Any, alpha: Any) -> float:
    # ScCritBinom (interpr3.cxx): n < 0, alpha outside [0, 1], or p outside
    # [0, 1] is #NUM!. alpha == 0 returns 0. scipy's ppf(0, n, p) is -1.
    # alpha == 1 returns n, or 0 when p == 0. int(trials) rejected "10.9".
    got = _scipy_parsed(lambda: (int(float(trials)), float(prob), float(alpha)))
    if got is None:
        return float("nan")
    stats, (n, p, a) = got
    if n < 0 or a < 0.0 or a > 1.0 or p < 0.0 or p > 1.0:
        return float("nan")
    if a == 0.0:
        return 0.0
    if a == 1.0:
        return 0.0 if p == 0.0 else float(n)
    return float(stats.binom.ppf(a, n, p))


# ZeroDivisionError is left to the worker exception path, which returns #DIV/0!. Type errors return #VALUE!.
def csc(x: Any) -> float:
    return float(1.0 / math.sin(float(x)))


def csch(x: Any) -> float:
    # 1/sinh(x) goes to 0 as |x| grows. OverflowError is that limit, not #NUM!.
    # OverflowError is the finite limit 0. ZeroDivisionError (#DIV/0!) and type errors (#VALUE!) are left to the worker.
    try:
        return float(1.0 / math.sinh(float(x)))
    except OverflowError:
        return 0.0


def _cum_payment(
    kind: str,
    rate: Any,
    nper: Any,
    pv: Any,
    start_period: Any,
    end_period: Any,
    type_val: Any,
) -> float:
    """Shared body of CUMIPMT and CUMPRINC. kind is "ipmt" or "ppmt"."""
    # rate must be > 0. Zero is #NUM!, same as a negative rate.
    # type errors are left to the worker exception path, which returns #VALUE!
    r = float(rate)
    # getCumipmt takes sal_Int32 nNumPeriods (financial.cxx). Leaving nper
    # as a float made numpy-financial price a different loan: nper 12.2
    # is not the 12-period schedule. start and end were already truncated.
    n = int(float(nper))
    p = float(pv)
    s = int(float(start_period))
    e = int(float(end_period))
    t = int(float(type_val))
    if t not in (0, 1):
        return float("nan")
    if r <= 0 or n <= 0 or p <= 0 or s < 1 or e < s or e > n:
        return float("nan")
    pers = np.arange(s, e + 1)
    return _npf_result(kind, r, pers, n, p, 0, t)


def cumipmt(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    return _cum_payment("ipmt", rate, nper, pv, start_period, end_period, type_val)


def cumprinc(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    return _cum_payment("ppmt", rate, nper, pv, start_period, end_period, type_val)

