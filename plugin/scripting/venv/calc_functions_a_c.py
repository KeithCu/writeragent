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
from typing import Any

import numpy as np

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
    _serial_to_date,
    _to_float_a as _to_float_a,
    match_criteria,
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
    # LibreOffice ScInterpreter AnalysisAddIn::getAccrint: frequency in
    # (1, 2, 4), basis in (0..4), rate > 0, par > 0, issue < settlement.
    from .calc_functions_t_z import yearfrac

    try:
        f_rate = float(rate)
        f_par = float(par) if par is not None else 1000.0
        freq = int(float(frequency))
        b = int(float(basis))
        iss = float(issue)
        settle = float(settlement)
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    if f_rate <= 0.0 or f_par <= 0.0 or freq not in (1, 2, 4) or b not in (0, 1, 2, 3, 4) or iss >= settle:
        return float("nan")

    yf = yearfrac(iss, settle, b)
    if math.isnan(yf):
        return float("nan")
    return float(f_par * f_rate * yf)


def accrintm(issue: Any, settlement: Any, rate: Any, par: Any = 1000.0, basis: Any = 0) -> float:
    # rate > 0, par > 0, basis in (0..4), issue < settlement. Otherwise #NUM!.
    from .calc_functions_t_z import yearfrac

    try:
        f_rate = float(rate)
        f_par = float(par) if par is not None else 1000.0
        b = int(float(basis))
        iss = float(issue)
        settle = float(settlement)
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    if f_rate <= 0.0 or f_par <= 0.0 or b not in (0, 1, 2, 3, 4) or iss >= settle:
        return float("nan")

    yf = yearfrac(iss, settle, b)
    if math.isnan(yf):
        return float("nan")
    return float(f_par * f_rate * yf)


def acot(x: Any) -> float:
    try:
        xv = float(x)
        return float(math.pi / 2 - math.atan(xv))
    except (ValueError, TypeError):
        return float("nan")


def acoth(x: Any) -> float:
    try:
        xv = float(x)
        if abs(xv) <= 1:
            return float("nan")
        return float(0.5 * math.log((xv + 1) / (xv - 1)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


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
    # Functions 1..12. 14..19 are not implemented. Bool is not a number
    # (float(True) would be 1.0).
    try:
        fn = int(float(function_num))
        opt = int(float(options))
        if 14 <= fn <= 19:
            raise NotImplementedError(f"AGGREGATE function {fn} is unsupported in lightweight runtime")
        numeric: list[float] = []
        n_text = 0
        for arg in args:
            for x in np.asarray(arg, dtype=object).ravel():
                if isinstance(x, (bool, np.bool_)):
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

        # Simplified implementations for most common
        if fn == 1:
            # Empty after dropping text/errors is #DIV/0! in Calc; avoid the
            # RuntimeWarning from np.mean([]).
            return float(np.mean(arr)) if arr.size else float("nan")
        if fn == 2:
            return float(np.sum(~np.isnan(arr)))
        if fn == 3:
            return float(len(arr) + n_text)
        if fn == 4:
            return float(np.nanmax(arr))
        if fn == 5:
            return float(np.nanmin(arr))
        if fn == 6:
            # np.prod([]) is 1. Calc AGGREGATE PRODUCT is 0 when ignore-errors
            # or text leaves no numbers (AGGREGATE(6,6,NA(),NA()) is 0).
            return float(np.prod(arr)) if arr.size else 0.0
        if fn == 7:
            return float(np.std(arr, ddof=1))
        if fn == 8:
            return float(np.std(arr, ddof=0))
        if fn == 9:
            # np.sum([]) is already 0. That matches Calc AGGREGATE SUM after an
            # ignore-errors option strips every value.
            return float(np.sum(arr)) if arr.size else 0.0
        if fn == 10:
            return float(np.var(arr, ddof=1))
        if fn == 11:
            return float(np.var(arr, ddof=0))
        if fn == 12:
            return float(np.median(arr))
        return float("nan")
    except NotImplementedError:
        raise
    except Exception:
        return float("nan")


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
    from .calc_functions_t_z import yearfrac

    try:
        f_cost = float(cost)
        d_purch = float(date_purchased)
        d_first = float(first_period)
        f_salv = float(salvage)
        n_per = int(float(period))
        f_rate = float(rate)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    if (
        d_purch > d_first
        or f_rate <= 0.0
        or f_salv > f_cost
        or f_cost <= 0.0
        or f_salv < 0
        or n_per < 0
        or b not in (0, 1, 2, 3, 4)
    ):
        return float("nan")

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
    f_n_rate = round(yf * f_rate * f_cost)
    f_cost -= f_n_rate
    f_rest = f_cost - f_salv

    for n in range(n_per):
        f_n_rate = round(f_rate * f_cost)
        f_rest -= f_n_rate
        if f_rest < 0.0:
            rem = n_per - n
            if rem in (0, 1):
                return float(round(f_cost * 0.5))
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
    from .calc_functions_t_z import yearfrac

    try:
        f_cost = float(cost)
        d_purch = float(date_purchased)
        d_first = float(first_period)
        f_salv = float(salvage)
        n_per = int(float(period))
        f_rate = float(rate)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        return float("nan")

    if (
        d_purch > d_first
        or f_rate <= 0.0
        or f_salv > f_cost
        or f_cost <= 0.0
        or f_salv < 0
        or n_per < 0
        or b not in (0, 1, 2, 3, 4)
    ):
        return float("nan")

    f_one_rate = f_cost * f_rate
    f_cost_delta = f_cost - f_salv
    yf = yearfrac(d_purch, d_first, b)
    if math.isnan(yf):
        return float("nan")
    f0_rate = yf * f_rate * f_cost
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


def arabic(text: Any) -> float:
    roman = str(text).upper().strip()
    roman_values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    res = 0
    for i in range(len(roman)):
        if roman[i] not in roman_values:
            return float("nan")
        if i + 1 < len(roman) and roman_values[roman[i]] < roman_values[roman[i + 1]]:
            res -= roman_values[roman[i]]
        else:
            res += roman_values[roman[i]]
    return float(res)


def areas(r: Any) -> float:
    """Return the number of areas in a reference.

    A sequence of ranges counts each disjoint area. One contiguous range
    or cell is 1.0, matching Excel and Calc.
    """
    if isinstance(r, (list, tuple)) and r and isinstance(r[0], (list, tuple)) and len(r[0]) > 0 and isinstance(r[0][0], (list, tuple)):
        return float(len(r))
    return 1.0


def asc(text: Any) -> str:
    # Basic full-width to half-width conversion for ASCII/Katakana characters
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
    except Exception:
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
    # Criteria ranges must match the average range. A shorter range is #VALUE!,
    # not a silent truncate.
    if len(args) % 2 != 0:
        return "#VALUE!"
    ar_flat = np.asarray(ar, dtype=object).ravel()
    pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    mask = _multi_criteria_mask(pairs, base_len=len(ar_flat))
    if mask is None:
        return "#VALUE!"
    vals: list[float] = []
    for idx in range(len(ar_flat)):
        if mask[idx]:
            try:
                val = float(ar_flat[idx])
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
        if n < 0 or r < 2 or r > 36 or m < 0:
            return "#NUM!"
        if n == 0:
            return "0".zfill(m)
        digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = ""
        while n > 0:
            res = digits[n % r] + res
            n //= r
        return res.zfill(m)
    except Exception:
        return "#NUM!"


def besseli(x: Any, n: Any) -> float:
    try:
        import scipy.special  # type: ignore[import-untyped]

        return _bessel_iv_jv(scipy.special.iv, x, n)
    except Exception:
        return float("nan")


def besselj(x: Any, n: Any) -> float:
    try:
        import scipy.special

        return _bessel_iv_jv(scipy.special.jv, x, n)
    except Exception:
        return float("nan")


def besselk(x: Any, n: Any) -> float:
    try:
        from scipy.special import kn

        return _bessel_kn_yn(kn, x, n)
    except Exception:
        return float("nan")


def bessely(x: Any, n: Any) -> float:
    try:
        from scipy.special import yn

        return _bessel_kn_yn(yn, x, n)
    except Exception:
        return float("nan")


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
    try:
        from scipy import stats

        return float(stats.chi2.sf(float(x), int(df)))
    except Exception:
        return float("nan")


def chiinv(p: Any, df: Any) -> float:
    try:
        from scipy import stats

        return float(stats.chi2.isf(float(p), int(df)))
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
    try:
        ss = str(s)
        return float(ord(ss[0])) if ss else float("nan")
    except (ValueError, TypeError):
        return float("nan")


def combin(n: Any, k: Any) -> float:
    try:
        return float(math.comb(int(float(n)), int(float(k))))
    except (ValueError, TypeError, OverflowError):
        # int(float(inf)) raises OverflowError, which used to escape this helper.
        return float("nan")


def combina(n: Any, k: Any) -> float:
    try:
        ni = int(float(n))
        ki = int(float(k))
        if ni == 0 and ki == 0:
            return 1.0
        return float(math.comb(ni + ki - 1, ki))
    except (ValueError, TypeError, OverflowError):
        # int(float(inf)) raises OverflowError, which used to escape this helper.
        return float("nan")


def complex(real_num: Any, imag_num: Any = 0.0, suffix: Any = "i") -> str:
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


def cot(x: Any) -> float:
    try:
        return float(1.0 / math.tan(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


def coth(x: Any) -> float:
    try:
        return float(1.0 / math.tanh(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


def countif(r: Any, crit: Any) -> float:
    r_flat = np.asarray(r, dtype=object).ravel()
    cnt = 0
    for val in r_flat:
        if match_criteria(val, crit):
            cnt += 1
    return float(cnt)


def countifs(*args: Any) -> float | str:
    # Every criteria range must be the same length. zip() would drop the tail
    # and return a count instead of #VALUE!.
    if len(args) % 2 != 0:
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
    try:
        from scipy import stats

        return float(stats.binom.ppf(float(alpha), int(trials), float(prob)))
    except Exception:
        return float("nan")


def csc(x: Any) -> float:
    try:
        return float(1.0 / math.sin(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


def csch(x: Any) -> float:
    # 1/sinh(x) goes to 0 as |x| grows. OverflowError is that limit, not #NUM!.
    try:
        return float(1.0 / math.sinh(float(x)))
    except OverflowError:
        return 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


def cumipmt(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    # rate must be > 0. Zero is #NUM!, same as a negative rate.
    try:
        r = float(rate)
        n = float(nper)
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
    return _npf_result("ipmt", r, pers, n, p, 0, t)


def cumprinc(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    # rate must be > 0. Zero is #NUM!, same as a negative rate.
    try:
        r = float(rate)
        n = float(nper)
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
    return _npf_result("ppmt", r, pers, n, p, 0, t)

