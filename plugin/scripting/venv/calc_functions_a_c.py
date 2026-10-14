# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import builtins
import math
import operator
import sys
from typing import Any

import numpy as np

from .calc_functions_t_z import yearfrac
from .calc_functions_util import (
    _bessel_iv_jv,
    _bessel_kn_yn,
    _collect_a_values,
    _criteria_numbers,
    _date_to_serial,
    _days_between,
    _extract_numeric_array,
    _from_complex,
    _get_coupon_dates,
    _int_bitwise,
    _int_shift,
    _multi_criteria_mask,
    _npf_result,
    _round_half_up,
    _serial_to_date,
    match_criteria,
    nan_on_error,
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
    "bahttext",
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
    try:
        f_rate = float(rate)
        f_par = float(par) if par is not None else 1000.0
        b = int(float(basis))
        iss = float(issue)
        settle = float(settlement)
        freq = int(float(frequency)) if frequency is not None else None
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    if f_rate <= 0.0 or f_par <= 0.0 or b not in (0, 1, 2, 3, 4) or iss >= settle:
        return float("nan")
    if freq is not None and freq not in (1, 2, 4):
        return float("nan")

    yf = yearfrac(iss, settle, b)
    if math.isnan(yf):
        return float("nan")
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


@nan_on_error(ValueError, TypeError)
def acot(x: Any) -> float:
    xv = float(x)
    return float(math.pi / 2 - math.atan(xv))


@nan_on_error(ValueError, TypeError, ZeroDivisionError)
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
    is_a1 = bool(a1)

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
    try:
        fn = int(float(function_num))
        opt = int(float(options))
        if 14 <= fn <= 19:
            raise NotImplementedError(f"AGGREGATE function {fn} is unsupported in lightweight runtime")
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
            # np.nanmax([]) raises, and the handler below turned that into NaN.
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
    except NotImplementedError:
        raise
    except Exception:
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
    try:
        f_cost = float(cost)
        d_purch = float(date_purchased)
        d_first = float(first_period)
        f_salv = float(salvage)
        n_per = int(float(period))
        f_rate = float(rate)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        return None

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


def _arabic_digit(ch: str) -> tuple[int, bool] | None:
    """ScArabic's lcl_GetArabicValue. The bool is true for a power of ten."""
    table = {
        "M": (1000, True),
        "D": (500, False),
        "C": (100, True),
        "L": (50, False),
        "X": (10, True),
        "V": (5, False),
        "I": (1, True),
    }
    return table.get(ch)


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
    try:
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
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"


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
    if not vals:
        return float("nan")
    return float(np.mean(vals))


def averageifs(ar: Any, *args: Any) -> float | str:
    # Criteria ranges must match the average range in shape, not just in cell
    # count. A 2x3 range and a 3x2 range both ravel to 6 and used to be
    # averaged. Calc returns #VALUE!. No pairs used to average the whole range.
    if len(args) % 2 != 0 or not args:
        return "#VALUE!"
    ar_arr = np.asarray(ar, dtype=object)
    ar_flat = ar_arr.ravel()
    pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    mask = _multi_criteria_mask(pairs, base_len=len(ar_flat), base_shape=ar_arr.shape)
    if mask is None:
        return "#VALUE!"
    vals: list[float] = []
    for idx in range(len(ar_flat)):
        if mask[idx]:
            # Text and bools are not numbers. float("5") and float(True)
            # used to pull them into the mean. Calc AVERAGEIFS ignores both.
            cell = ar_flat[idx]
            if isinstance(cell, (bool, np.bool_, str)):
                continue
            try:
                val = float(cell)
                if not np.isnan(val):
                    vals.append(val)
            except (ValueError, TypeError, OverflowError):
                pass
    if not vals:
        return float("nan")
    return float(np.mean(vals))


def bahttext(number: Any) -> str:
    # Thai currency spelling is not implemented in this runtime.
    raise NotImplementedError("BAHTTEXT is unsupported in lightweight runtime")


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


def _bessel_call(name: str, impl: Any, x: Any, n: Any) -> float:
    """One scipy.special Bessel, or NaN when scipy is missing or the input is illegal.

    A missing install is the same NaN as _npf_result, not a raised ImportError.
    """
    try:
        import scipy.special  # type: ignore[import-untyped]

        return impl(getattr(scipy.special, name), x, n)
    except Exception:
        return float("nan")


def besseli(x: Any, n: Any) -> float:
    return _bessel_call("iv", _bessel_iv_jv, x, n)


def besselj(x: Any, n: Any) -> float:
    return _bessel_call("jv", _bessel_iv_jv, x, n)


def besselk(x: Any, n: Any) -> float:
    return _bessel_call("kn", _bessel_kn_yn, x, n)


def bessely(x: Any, n: Any) -> float:
    return _bessel_call("yn", _bessel_kn_yn, x, n)


def betadist(*args: Any) -> float:
    # Legacy BETADIST(x, alpha, beta, [A], [B]) is always cumulative.
    # args[3] is the lower bound A (default 0), not a cumulative flag.
    if len(args) < 3 or len(args) > 5:
        return float("nan")
    try:
        from scipy import stats

        x = float(args[0])
        alpha = float(args[1])
        beta = float(args[2])
        A = float(args[3]) if len(args) > 3 else 0.0
        B = float(args[4]) if len(args) > 4 else 1.0

        if alpha <= 0.0 or beta <= 0.0 or A >= B or x < A or x > B:
            return float("nan")
        x_norm = (x - A) / (B - A)
        return float(stats.beta.cdf(x_norm, alpha, beta))
    except Exception:
        return float("nan")


def betainv(*args: Any) -> float:
    # p in [0, 1], alpha > 0, beta > 0, A < B. scipy's ppf does not enforce that.
    if len(args) < 3 or len(args) > 5:
        return float("nan")
    try:
        from scipy import stats

        p = float(args[0])
        alpha = float(args[1])
        beta = float(args[2])
        A = float(args[3]) if len(args) > 3 else 0.0
        B = float(args[4]) if len(args) > 4 else 1.0

        if p < 0.0 or p > 1.0 or alpha <= 0.0 or beta <= 0.0 or A >= B:
            return float("nan")
        return float(stats.beta.ppf(p, alpha, beta) * (B - A) + A)
    except Exception:
        return float("nan")


def binomdist(*args: Any) -> float:
    # 0 <= k <= n, n >= 0, 0 <= p <= 1. scipy returns 0 for k > n; Calc wants #NUM!.
    if len(args) < 4:
        return float("nan")
    try:
        from scipy import stats

        k = int(float(args[0]))
        n = int(float(args[1]))
        p = float(args[2])
        cum = bool(float(args[3]))
        if k < 0 or k > n or n < 0 or p < 0 or p > 1:
            return float("nan")
        if cum:
            return float(stats.binom.cdf(k, n, p))
        else:
            return float(stats.binom.pmf(k, n, p))
    except Exception:
        return float("nan")


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
    try:
        from scipy import stats

        xv = float(x)
        dof = int(float(df))
        if xv < 0.0 or dof < 1 or dof > 10**10:
            return float("nan")
        return float(stats.chi2.sf(xv, dof))
    except Exception:
        return float("nan")


def chiinv(p: Any, df: Any) -> float:
    # ScChiInv: df < 1, p <= 0, or p > 1 is #NUM!. scipy isf(0, df) is inf.
    # p == 1 stays 0. df uses int(float()) so "3.5" truncates. df > 1e10
    # matches Excel CHIINV and the chidist cap above.
    try:
        from scipy import stats

        pv = float(p)
        dof = int(float(df))
        if dof < 1 or dof > 10**10 or pv <= 0.0 or pv > 1.0:
            return float("nan")
        return float(stats.chi2.isf(pv, dof))
    except Exception:
        return float("nan")


def choose(index: Any, *args: Any) -> Any:
    # An out-of-range index is #VALUE! (NaN), not None.
    try:
        idx = int(float(index))
        if 1 <= idx <= len(args):
            return args[idx - 1]
    except (ValueError, TypeError, OverflowError):
        pass
    return float("nan")


def clean(text: Any) -> str | float:
    try:
        if text is None:
            return ""
        if isinstance(text, float) and math.isnan(text):
            return float("nan")
        s = str(text)
        # Excel CLEAN removes C0 controls (ord < 32) and DEL (U+007F). The
        # ord < 32 filter left chr(127) in the result.
        return "".join(c for c in s if ord(c) >= 32 and ord(c) != 127)
    except (ValueError, TypeError):
        return float("nan")


def code(s: Any) -> float:
    # ScCode pushes 0 for an empty string. Excel CODE("") is #VALUE!.
    # This helper follows Calc. str(None) is "None", so a missing object
    # stays the code point of "N"; do not treat None as blank here.
    try:
        ss = str(s)
        if not ss:
            return 0.0
        return float(ord(ss[0]))
    except (ValueError, TypeError):
        return float("nan")


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
    try:
        return _binom_float(int(float(n)), int(float(k)), reject_k_gt_n=True)
    except (ValueError, TypeError, OverflowError):
        # int(float(inf)) raises OverflowError, which used to escape this helper.
        return float("nan")


def combina(n: Any, k: Any) -> float:
    # ScCombinA also rejects k > n. Excel COMBINA(3, 5) is 21, so this
    # helper still evaluates C(n+k-1, k) in that case. n < 0 has to be
    # NaN: combina(-2, 6) is math.comb(3, 6), which is 0.
    try:
        ni = int(float(n))
        ki = int(float(k))
        if ni < 0 or ki < 0:
            return float("nan")
        if ni == 0 and ki == 0:
            return 1.0
        return _binom_float(ni + ki - 1, ki, reject_k_gt_n=False)
    except (ValueError, TypeError, OverflowError):
        # int(float(inf)) raises OverflowError, which used to escape this helper.
        return float("nan")


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
    try:
        from scipy import stats

        a = float(alpha)
        sd = float(stddev)
        n = int(float(size))
        if a <= 0.0 or a >= 1.0 or sd <= 0.0 or n < 1:
            return float("nan")
        return float(stats.norm.ppf(1 - a / 2.0) * sd / math.sqrt(n))
    except Exception:
        return float("nan")


@nan_on_error(ValueError, TypeError, ZeroDivisionError)
def cot(x: Any) -> float:
    return float(1.0 / math.tan(float(x)))


@nan_on_error(ValueError, TypeError, ZeroDivisionError)
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
    if len(args) % 2 != 0 or not args:
        return "#VALUE!"
    pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    mask = _multi_criteria_mask(pairs)
    if mask is None:
        return "#VALUE!"
    return float(np.sum(mask))


def coupdaybs(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    # Coupon dates step by calendar months. frequency in (1, 2, 4), basis in
    # (0..4), settlement < maturity. A fixed day step (180, 182.5) drifts.
    try:
        b = int(float(basis))
        p_ser, _c_ser, _days_in_per, _k = _get_coupon_dates(settlement, maturity, frequency, b)
        s_date = _serial_to_date(settlement)
        if s_date is None:
            return float("nan")
        s_ser = _date_to_serial(s_date)
        return float(_days_between(p_ser, s_ser, b))
    except Exception:
        return float("nan")


def coupdays(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    try:
        b = int(float(basis))
        _p_ser, _c_ser, days_in_per, _k = _get_coupon_dates(settlement, maturity, frequency, b)
        return float(days_in_per)
    except Exception:
        return float("nan")


def coupdaysnc(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    try:
        b = int(float(basis))
        _p_ser, c_ser, _days_in_per, _k = _get_coupon_dates(settlement, maturity, frequency, b)
        s_date = _serial_to_date(settlement)
        if s_date is None:
            return float("nan")
        s_ser = _date_to_serial(s_date)
        return float(_days_between(s_ser, c_ser, b))
    except Exception:
        return float("nan")


def coupncd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    try:
        _p_ser, c_ser, _days_in_per, _k = _get_coupon_dates(settlement, maturity, frequency, basis)
        return float(c_ser)
    except Exception:
        return float("nan")


def coupnum(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    try:
        _p_ser, _c_ser, _days_in_per, k = _get_coupon_dates(settlement, maturity, frequency, basis)
        return float(k)
    except Exception:
        return float("nan")


def couppcd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    try:
        p_ser, _c_ser, _days_in_per, _k = _get_coupon_dates(settlement, maturity, frequency, basis)
        return float(p_ser)
    except Exception:
        return float("nan")


def critbinom(trials: Any, prob: Any, alpha: Any) -> float:
    # ScCritBinom (interpr3.cxx): n < 0, alpha outside [0, 1], or p outside
    # [0, 1] is #NUM!. alpha == 0 returns 0. scipy's ppf(0, n, p) is -1.
    # alpha == 1 returns n, or 0 when p == 0. int(trials) rejected "10.9".
    try:
        from scipy import stats

        n = int(float(trials))
        p = float(prob)
        a = float(alpha)
        if n < 0 or a < 0.0 or a > 1.0 or p < 0.0 or p > 1.0:
            return float("nan")
        if a == 0.0:
            return 0.0
        if a == 1.0:
            return 0.0 if p == 0.0 else float(n)
        return float(stats.binom.ppf(a, n, p))
    except Exception:
        return float("nan")


@nan_on_error(ValueError, TypeError, ZeroDivisionError)
def csc(x: Any) -> float:
    return float(1.0 / math.sin(float(x)))


def csch(x: Any) -> float:
    # 1/sinh(x) goes to 0 as |x| grows. OverflowError is that limit, not #NUM!.
    try:
        return float(1.0 / math.sinh(float(x)))
    except OverflowError:
        return 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


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
    try:
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
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if r <= 0 or n <= 0 or p <= 0 or s < 1 or e < s or e > n:
        return float("nan")
    pers = np.arange(s, e + 1)
    return _npf_result(kind, r, pers, n, p, 0, t)


def cumipmt(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    return _cum_payment("ipmt", rate, nper, pv, start_period, end_period, type_val)


def cumprinc(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    return _cum_payment("ppmt", rate, nper, pv, start_period, end_period, type_val)

