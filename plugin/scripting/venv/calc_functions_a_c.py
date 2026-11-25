# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``calc``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import builtins
import math
import operator
import sys
from typing import Any

import numpy as np

from .calc_functions_i_m import mode
from .calc_functions_util import (
    DIV0_NAN,
    ILLEGAL_ARGUMENT_NAN,
    approx_floor,
    argument_error,
    as_nonzero,
    collect_a_values,
    coupon_schedule,
    criteria_reduce,
    criterion_error,
    extract_numeric_array,
    finite_or_nan,
    first_error,
    from_complex,
    is_calc_error,
    match_criteria,
    multi_criteria_mask,
    npf_result,
    propagate_errors,
    py_str,
    round_half_up,
    scipy_stats,
    yearfrac,
)


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


@propagate_errors
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


@propagate_errors
def accrintm(issue: Any, settlement: Any, rate: Any, par: Any = 1000.0, basis: Any = 0) -> float:
    return _accrued(issue, settlement, rate, par, basis)


@propagate_errors
def acot(x: Any) -> float:
    xv = float(x)
    return float(math.pi / 2 - math.atan(xv))


# |x|<=1 stays NaN (#NUM!). Type errors are left to the worker exception path, which returns #VALUE!.
@propagate_errors
def acoth(x: Any) -> float:
    xv = float(x)
    if abs(xv) <= 1:
        return float("nan")
    return float(0.5 * math.log((xv + 1) / (xv - 1)))


@propagate_errors
def address(row: Any, col: Any, abs_num: Any = 1, a1: Any = True, sheet: Any = None) -> str:
    # abs_num is 1..4 (#VALUE! otherwise). A sheet name quotes ' as ''.
    try:
        r = int(float(row))
        c = int(float(col))
        abs_n = int(float(abs_num))
        if r < 1 or c < 1 or abs_n not in (1, 2, 3, 4):
            return "#VALUE!"
        is_a1 = a1 is None or a1 == "" or as_nonzero(a1)
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


def aggregate(function_num: Any, options: Any, *args: Any) -> float | str:
    # ScAggregate dispatches 1..11 like ScSubTotal, plus MEDIAN (12) and
    # MODE.SNGL (13). 14..19 need k and stay unimplemented. Bool is not a
    # number for the numeric reductions (float(True) would be 1.0). COUNTA
    # still counts a bool. Do not share this switch with subtotal(): that
    # helper counts True as 1, which test_aggregate_stubs_and_bools forbids.
    fn = int(float(function_num))
    opt = int(float(options))
    assert 1 <= fn <= 13
    flat = [np.asarray(arg, dtype=object).ravel() for arg in args]
    ignore_errors = opt in (2, 3, 6, 7)
    # COUNT (2) and COUNTA (3) never return an input error (AGGREGATE(2;4;…) counts the numbers).
    if fn not in (2, 3) and not ignore_errors:
        err = first_error(v for cells in reversed(flat) for v in cells)
        if err is not None:
            return err
    numeric: list[float] = []
    n_other = 0  # non-numeric cells COUNTA counts: text, booleans, and errors that are not ignored
    for cells in flat:
        for x in cells:
            if is_calc_error(x):
                n_other += not ignore_errors
            elif isinstance(x, (bool, np.bool_)):
                n_other += 1
            elif isinstance(x, str):
                n_other += x.strip() != ""
            else:
                try:
                    numeric.append(float(x))
                except (ValueError, TypeError, OverflowError):
                    continue
    arr = np.asarray(numeric, dtype=float)

    if fn == 1:
        # Empty after dropping text/errors is #DIV/0! in Calc; avoid the
        # RuntimeWarning from np.mean([]).
        return float(np.mean(arr)) if arr.size else float("nan")
    if fn == 2:
        return float(arr.size)
    if fn == 3:
        return float(len(arr) + n_other)
    if fn == 4:
        # ScMax pushes 0 when nothing numeric remains (text is ignored).
        # np.max([]) raises ValueError; empty is 0 here.
        return float(np.max(arr)) if arr.size else 0.0
    if fn == 5:
        # Same empty rule as ScMin / ScMax.
        return float(np.min(arr)) if arr.size else 0.0
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
        # GetMedian sets NoValue (#VALUE!) on an empty vector. np.median([])
        # is NaN plus a RuntimeWarning. Keep the NaN, skip the call.
        return float(np.median(arr)) if arr.size else float("nan")
    if fn == 13:
        # ScModalValue on the numbers already kept above. mode() would
        # count True as 1 if given the raw args. Tie-break is the lowest
        # value. No repeat is #VALUE! in Calc (ScModalValue).
        return mode(arr.tolist())
    return float("nan")


def _round_amor(value: float) -> float:
    """Integer round, half away from zero.

    GetAmordegrc uses rtl::math::round (analysishelper.cxx). Python
    round() is half-to-even, so 0.5 became 0 and 2.5 became 2.
    """
    return float(round_half_up(value, 0))


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


@propagate_errors
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
            # n is in range(n_per), so n_per - n is at least 1. The last period is half the remaining cost.
            return _round_amor(f_cost * 0.5) if n == n_per - 1 else 0.0
        f_cost -= f_n_rate

    return float(f_n_rate)


@propagate_errors
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
        res = f_cost - f_salv - f_one_rate * num_full_periods - f0_rate
    else:
        res = 0.0

    return float(res) if res > 0.0 else 0.0


# ScArabic lcl_GetArabicValue. The bool is true for a power of ten.
_ARABIC_DIGITS: dict[str, tuple[int, bool]] = {
    "M": (1000, True),
    "D": (500, False),
    "C": (100, True),
    "L": (50, False),
    "X": (10, True),
    "V": (5, False),
    "I": (1, True),
}


@propagate_errors
def arabic(text: Any) -> float:
    # ScArabic (interpr2.cxx) walks the numeral with nValidRest starting at
    # 3999. A digit followed by exactly twice itself (VX, LC, DM) is illegal,
    # and so is a value past 3999 (MMMM). The old loop accepted those: VX
    # became 5 and MMMM became 4000. Simplified forms that ROMAN modes emit
    # stay valid (IIII is 4, IC is 99, XMIX is 999). The help page says
    # ARABIC("") is 0. "-" is not a roman digit, so "-IV" is NaN.
    roman = py_str(text).upper().strip()
    value = 0
    valid_rest = 3999
    index = 0
    length = len(roman)
    while index < length:
        first = _ARABIC_DIGITS.get(roman[index])
        if first is None:
            return float("nan")
        digit, is_ten_power = first
        next_digit = 0
        if index + 1 < length:
            second = _ARABIC_DIGITS.get(roman[index + 1])
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


# Full-width ASCII (U+FF01–U+FF5E) to ASCII, and ideographic space
# U+3000 to a normal space. Half-width katakana is not converted.
_ASC_TRANS = str.maketrans({0x3000: " ", **{cp: chr(cp - 0xFEE0) for cp in range(0xFF01, 0xFF5F)}})


@propagate_errors
def asc(text: Any) -> str:
    return py_str(text).translate(_ASC_TRANS)


@propagate_errors
def avedev(*args: Any) -> float:
    # Excel/Calc AVEDEV ignores text and logicals.
    arr = extract_numeric_array(*args, propagate_nan=False)
    if not arr.size:
        return float("nan")
    return float(np.mean(np.abs(arr - np.mean(arr))))


@propagate_errors
def averagea(*args: Any) -> float:
    # Blank strings and whitespace stay in the denominator as 0, same as text:
    # Calc AVERAGEA(10,"",20) is 10, not 15. collect_a_values counts blank
    # and text as 0 and skips None (an empty cell).
    vals = collect_a_values(*args)
    if not vals.size:
        return float("nan")
    return float(np.mean(vals))


def _mean(vals: list[float]) -> float:
    return float(np.mean(vals)) if vals else DIV0_NAN


def averageif(r: Any, crit: Any, ar: Any | None = None) -> float | str:
    res = criteria_reduce(r if ar is None else ar, [(r, crit)], _mean)
    return float("nan") if res is None else res


def _criteria_pairs(args: tuple[Any, ...]) -> list[tuple[Any, Any]] | None:
    """Criteria pairs for AVERAGEIFS and COUNTIFS. None means #VALUE!.

    Odd or empty args are None. SUMIFS uses a different empty/odd contract.
    """
    if len(args) % 2 != 0 or not args:
        return None
    return [(args[i], args[i + 1]) for i in range(0, len(args), 2)]


def averageifs(ar: Any, *args: Any) -> float | str:
    # Criteria ranges must match the average range in shape, not just in cell
    # count. A 2x3 range and a 3x2 range both ravel to 6 and must not be averaged. Calc returns #VALUE!. No pairs must not average the whole range.
    pairs = _criteria_pairs(args)
    if pairs is None:
        return "#VALUE!"
    res = criteria_reduce(ar, pairs, _mean)
    return "#VALUE!" if res is None else res


@propagate_errors
def base(number: Any, radix: Any, min_length: Any = 0) -> str:
    try:
        n = approx_floor(float(number))
        r = approx_floor(float(radix))
        m = approx_floor(float(min_length))
        # Literal "NaN" is not a Calc error token, so bad BASE input would show up
        # as text; returning token strings avoids that. ScBase (interpr2.cxx)
        # floors with approxFloor, caps min_length at 65534 (zfill(1e9) would hang
        # the worker), and returns Err:502 on domain failure.
        if n < 0 or r < 2 or r > 36 or m < 0 or m > 65534:
            return "Err:502"
        return np.base_repr(n, r).zfill(m)
    except OverflowError:
        return "#NUM!"
    except (ValueError, TypeError):
        return "#VALUE!"


def _bessel_call(name: str, x: Any, n: Any, *, positive_x: bool) -> float:
    """One scipy.special Bessel, or NaN when scipy is missing or the input is illegal.

    A missing install is the same NaN as npf_result, not a raised ImportError.
    """
    xv = float(x)
    nv = int(float(n))
    if nv < 0 or (positive_x and xv <= 0):
        return float("nan")
    try:
        import scipy.special  # optional and heavy: loaded on first Bessel call

        return float(getattr(scipy.special, name)(nv, xv))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


@propagate_errors
def besseli(x: Any, n: Any) -> float:
    return _bessel_call("iv", x, n, positive_x=False)


@propagate_errors
def besselj(x: Any, n: Any) -> float:
    return _bessel_call("jv", x, n, positive_x=False)


@propagate_errors
def besselk(x: Any, n: Any) -> float:
    return _bessel_call("kn", x, n, positive_x=True)


@propagate_errors
def bessely(x: Any, n: Any) -> float:
    return _bessel_call("yn", x, n, positive_x=True)


def _beta_params(args: tuple[Any, ...]) -> tuple[Any, Any] | None:
    """(scipy.stats, (first, alpha, beta, A, B)) for BETADIST and BETAINV.

    None when the arity is outside 3..5 or scipy is missing. Text raises ValueError (#VALUE!).
    """
    if len(args) < 3 or len(args) > 5:
        return None
    stats = scipy_stats()
    if stats is None:
        return None
    return stats, (float(args[0]), float(args[1]), float(args[2]),
                   float(args[3]) if len(args) > 3 else 0.0,
                   float(args[4]) if len(args) > 4 else 1.0)


@propagate_errors
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


@propagate_errors
def betainv(*args: Any) -> float:
    # p in [0, 1], alpha > 0, beta > 0, A < B. scipy's ppf does not enforce that.
    got = _beta_params(args)
    if got is None:
        return float("nan")
    stats, (p, alpha, beta, A, B) = got

    if p < 0.0 or p > 1.0 or alpha <= 0.0 or beta <= 0.0 or A >= B:
        return float("nan")
    return float(stats.beta.ppf(p, alpha, beta) * (B - A) + A)


@propagate_errors
def binomdist(*args: Any) -> float:
    # 0 <= k <= n, n >= 0, 0 <= p <= 1. scipy returns 0 for k > n; Calc wants #NUM!.
    if len(args) < 4:
        return float("nan")
    stats = scipy_stats()
    if stats is None:
        return float("nan")
    k = approx_floor(float(args[0]))
    n = approx_floor(float(args[1]))
    p = float(args[2])
    cum = as_nonzero(args[3])
    if k < 0 or k > n or n < 0 or p < 0 or p > 1:
        return float("nan")
    if cum:
        return float(stats.binom.cdf(k, n, p))
    else:
        return float(stats.binom.pmf(k, n, p))


_MAX_BIT_VALUE: int = (1 << 48) - 1


def _int_bitwise(op: Any, n1: Any, n2: Any) -> float:
    """Apply a binary integer bitwise operator to n1 and n2 within [0, 2^48 - 1]."""
    # Non-negative integers below 2^48. ScBitAnd/Or/Xor (interpr1.cxx) floor with
    # approxFloor and return Err:502 outside [0, 2^48 - 1]. ValueError and TypeError
    # propagate (#VALUE! on the worker); OverflowError is #NUM!.
    try:
        v1 = approx_floor(float(n1))
        v2 = approx_floor(float(n2))
    except OverflowError:
        return float("nan")
    if v1 < 0 or v1 > _MAX_BIT_VALUE or v2 < 0 or v2 > _MAX_BIT_VALUE:
        return ILLEGAL_ARGUMENT_NAN
    return float(op(v1, v2))


def _int_shift(number: Any, shift: Any, *, left: bool) -> float:
    """Integer bit shift for number in [0, 2^48 - 1] (ScBitLshift/ScBitRshift, interpr1.cxx)."""
    # ScBitLshift multiplies by pow(2, shift) rather than capping at Excel's 53 bits,
    # so only float64 overflow limits left shifts and large right shifts become 0.
    try:
        n = approx_floor(float(number))
        s = approx_floor(float(shift))
        if n < 0 or n > _MAX_BIT_VALUE:
            return ILLEGAL_ARGUMENT_NAN
        up = s if left else -s
        if up <= 0:
            return float(n >> min(-up, 64))
        return finite_or_nan(n * 2.0**up)  # 2.0**1024 raises OverflowError
    except OverflowError:
        return float("nan")


@propagate_errors
def bitand(n1: Any, n2: Any) -> float:
    return _int_bitwise(operator.and_, n1, n2)


@propagate_errors
def bitlshift(number: Any, shift: Any) -> float:
    return _int_shift(number, shift, left=True)


@propagate_errors
def bitor(n1: Any, n2: Any) -> float:
    return _int_bitwise(operator.or_, n1, n2)


@propagate_errors
def bitrshift(number: Any, shift: Any) -> float:
    return _int_shift(number, shift, left=False)


@propagate_errors
def bitxor(n1: Any, n2: Any) -> float:
    return _int_bitwise(operator.xor, n1, n2)


@propagate_errors
def char(n: Any) -> str:
    # Non-numeric input and inf are #VALUE!, not "". chr() also accepts code
    # points above 255; Excel/Calc CHAR returns #VALUE! for those.
    try:
        code = int(float(n))
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"
    if code < 0 or code > 255:
        return "#VALUE!"
    return chr(code)


@propagate_errors
def chidist(x: Any, df: Any) -> float:
    # ScChiDist (interpr3.cxx), Excel path: x < 0 is #NUM!. ODFF returns 1
    # for x <= 0; scipy's sf(-1, df) is 1.0, and this helper follows the
    # Excel path, same as binomdist. x == 0 stays 1. df uses approx_floor
    # (float() accepts text such as "3.5").
    # df < 1 or df > 1e10 is #NUM! (Excel; LO enforces the cap on
    # ScChiSqDist_MS).
    stats = scipy_stats()
    if stats is None:
        return float("nan")
    xv, dof = float(x), approx_floor(float(df))
    if xv < 0.0 or dof < 1 or dof > 10**10:
        return float("nan")
    return float(stats.chi2.sf(xv, dof))


@propagate_errors
def chiinv(p: Any, df: Any) -> float:
    # ScChiInv: df < 1, p <= 0, or p > 1 is #NUM!. scipy isf(0, df) is inf.
    # p == 1 stays 0. df uses approx_floor (float() accepts text such as "3.5").
    # df > 1e10 matches Excel CHIINV and the chidist cap above.
    stats = scipy_stats()
    if stats is None:
        return float("nan")
    pv, dof = float(p), approx_floor(float(df))
    if dof < 1 or dof > 10**10 or pv <= 0.0 or pv > 1.0:
        return float("nan")
    return float(stats.chi2.isf(pv, dof))


def choose(index: Any, *args: Any) -> Any:
    # Undecorated because an unpicked option's error must not propagate.
    # Out-of-range stays NaN (#NUM! in the cell) so a negative index does not wrap.
    # non-numeric indexes are left to the worker exception path, which returns #VALUE!. OverflowError (inf) is #NUM! on that path.
    err = argument_error(index)
    if err is not None:
        return err
    idx = int(float(index))
    if 1 <= idx <= len(args):
        return args[idx - 1]
    return float("nan")


@propagate_errors
def clean(text: Any) -> str | float:
    s = py_str(text)
    # Excel CLEAN removes C0 controls (ord < 32) and DEL (U+007F). The
    # ord < 32 filter left chr(127) in the result.
    return "".join(c for c in s if ord(c) >= 32 and ord(c) != 127)


@propagate_errors
def code(s: Any) -> float:
    # ScCode pushes 0 for an empty string. Excel CODE("") is #VALUE!.
    # This helper follows Calc.
    ss = py_str(s)
    if not ss:
        return 0.0
    return float(ord(ss[0]))


def _binom_float(n: int, k: int) -> float:
    """Binomial coefficient as a float, or NaN when the result is not finite.

    math.comb(n, k) is 0 when k > n. ScCombin (interpr3.cxx) rejects that,
    so this returns NaN.

    math.comb(1e9, 5e8) builds a giant int before float() raises. After
    folding k to min(k, n-k), C(n, k) >= C(2k, k), which no longer fits
    in float64 once k is about 515. lgamma rejects that case first.
    """
    if n < 0 or k < 0 or k > n:
        return float("nan")
    k = min(k, n - k)
    log_c = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    if not math.isfinite(log_c) or log_c > math.log(sys.float_info.max):
        return float("nan")
    return float(math.comb(n, k))


@propagate_errors
def combin(n: Any, k: Any) -> float:
    # ScCombin rejects k > n and any negative. math.comb(3, 5) is 0.
    # non-numeric arguments are left to the worker exception path. OverflowError (inf) is #NUM!; ValueError is #VALUE!.
    return _binom_float(approx_floor(float(n)), approx_floor(float(k)))


@propagate_errors
def combina(n: Any, k: Any) -> float:
    # ScCombinA (interpr3.cxx) rejects k < 0 and k > n (Err:502), and evaluates
    # BinomCoeff(n + k - 1, k). Non-numeric arguments propagate to the worker
    # (#VALUE!); OverflowError (inf) is #NUM!.
    ni = approx_floor(float(n))
    ki = approx_floor(float(k))
    if ki < 0 or ki > ni:
        return ILLEGAL_ARGUMENT_NAN
    if ni == 0:
        return 0.0  # BinomCoeff(-1, 0) is 0 in Calc
    return _binom_float(ni + ki - 1, ki)


@propagate_errors
def complex(real_num: Any, imag_num: Any = 0.0, suffix: Any = "i") -> str:
    # The Calc name shadows the builtin. The body uses builtins.complex.
    # Suffix is i or j (either case). Anything else is #VALUE!.
    try:
        r = float(real_num)
        i = float(imag_num)
        s = str(suffix).strip()
        if s.lower() not in ("i", "j"):
            return "#VALUE!"
        return from_complex(builtins.complex(r, i), suffix=s.lower())
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"


@propagate_errors
def confidence(alpha: Any, stddev: Any, size: Any) -> float:
    # alpha strictly inside (0, 1), stddev > 0, size >= 1. Otherwise #NUM!.
    stats = scipy_stats()
    if stats is None:
        return float("nan")
    a, sd, n = float(alpha), float(stddev), approx_floor(float(size))
    if a <= 0.0 or a >= 1.0 or sd <= 0.0 or n < 1:
        return float("nan")
    return float(stats.norm.ppf(1 - a / 2.0) * sd / math.sqrt(n))


@propagate_errors
def cot(x: Any) -> float:
    xv = float(x)
    if not math.isfinite(xv):
        return float("nan")
    return float(1.0 / math.tan(xv))


@propagate_errors
def coth(x: Any) -> float:
    xv = float(x)
    if not math.isfinite(xv):
        return float("nan")
    return float(1.0 / math.tanh(xv))


def countif(r: Any, crit: Any) -> float | str:
    err = criterion_error([crit])
    if err is not None:
        return err
    return float(sum(1 for v in np.asarray(r, dtype=object).ravel() if match_criteria(v, crit)))


def countifs(*args: Any) -> float | str:
    # Every criteria range must be the same shape. A 2x3 and a 3x2 both have
    # 6 cells; comparing raveled lengths counted them. Calc returns #VALUE!.
    # No pairs must not sum an empty mask and return 0.
    pairs = _criteria_pairs(args)
    if pairs is None:
        return "#VALUE!"
    err = criterion_error(c for _r, c in pairs)
    if err is not None:
        return err
    mask = multi_criteria_mask(pairs)
    if mask is None:
        return "#VALUE!"
    return float(np.sum(mask))


def _coupon(settlement: Any, maturity: Any, frequency: Any, basis: Any, idx: int) -> float:
    # coupon_schedule order: 0 p_ser, 1 c_ser, 2 days_in_per, 3 k, 4 a_days, 5 dsc_days.
    try:
        return coupon_schedule(settlement, maturity, frequency, basis)[idx]
    except (ValueError, TypeError, OverflowError):
        return float("nan")


@propagate_errors
def coupdaybs(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    # Coupon dates step by calendar months. frequency in (1, 2, 4), basis in
    # (0..4), settlement < maturity. A fixed day step (180, 182.5) drifts.
    return _coupon(settlement, maturity, frequency, basis, 4)


@propagate_errors
def coupdays(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return _coupon(settlement, maturity, frequency, basis, 2)


@propagate_errors
def coupdaysnc(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return _coupon(settlement, maturity, frequency, basis, 5)


@propagate_errors
def coupncd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return _coupon(settlement, maturity, frequency, basis, 1)


@propagate_errors
def coupnum(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return _coupon(settlement, maturity, frequency, basis, 3)


@propagate_errors
def couppcd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    return _coupon(settlement, maturity, frequency, basis, 0)


@propagate_errors
def critbinom(trials: Any, prob: Any, alpha: Any) -> float:
    # ScCritBinom (interpr3.cxx): n < 0, alpha outside [0, 1], or p outside
    # [0, 1] is #NUM!. alpha == 0 returns 0. scipy's ppf(0, n, p) is -1.
    # alpha == 1 returns n, or 0 when p == 0. trials uses approx_floor
    # (float() accepts text such as "10.9").
    stats = scipy_stats()
    if stats is None:
        return float("nan")
    n, p, a = approx_floor(float(trials)), float(prob), float(alpha)
    if n < 0 or a < 0.0 or a > 1.0 or p < 0.0 or p > 1.0:
        return float("nan")
    if a == 0.0:
        return 0.0
    if a == 1.0:
        return 0.0 if p == 0.0 else float(n)
    return float(stats.binom.ppf(a, n, p))


@propagate_errors
def csc(x: Any) -> float:
    xv = float(x)
    if not math.isfinite(xv):
        return float("nan")
    return float(1.0 / math.sin(xv))


@propagate_errors
def csch(x: Any) -> float:
    # 1/sinh(x) goes to 0 as |x| grows, so an OverflowError from sinh is that limit (0),
    # not #NUM!. ZeroDivisionError (#DIV/0!) and type errors (#VALUE!) are left to the worker.
    xv = float(x)
    if not math.isfinite(xv):
        return float("nan")
    try:
        return float(1.0 / math.sinh(xv))
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
    # rate must be > 0. Zero is Err:502, same as a negative rate.
    r = float(rate)
    # ScCumIpmt/ScCumPrinc (interpr2.cxx) use nper unrounded, floor start and end
    # with approxFloor, and require type to be exactly 0 or 1.
    n = float(nper)
    p = float(pv)
    s = approx_floor(float(start_period))
    e = approx_floor(float(end_period))
    t = float(type_val)
    if t not in (0, 1) or r <= 0 or n <= 0 or p <= 0 or s < 1 or e < s or e > n:
        return ILLEGAL_ARGUMENT_NAN
    pers = np.arange(s, e + 1)
    return npf_result(kind, r, pers, n, p, 0, int(t))


@propagate_errors
def cumipmt(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    return _cum_payment("ipmt", rate, nper, pv, start_period, end_period, type_val)


@propagate_errors
def cumprinc(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    return _cum_payment("ppmt", rate, nper, pv, start_period, end_period, type_val)

