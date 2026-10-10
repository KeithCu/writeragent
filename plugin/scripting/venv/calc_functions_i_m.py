# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import cmath
import datetime as dt
import math
from collections import Counter
from typing import Any, Callable

import numpy as np

from .calc_functions_util import (
    _collect_a_values,
    _extract_numeric_array,
    _from_complex,
    _is_calc_error,
    _npf_result,
    _to_complex,
    match_criteria,
)
from .coerce import is_blank_value, is_na_value


__all__ = [
    "iferror",
    "ifna",
    "imabs",
    "imaginary",
    "imargument",
    "imconjugate",
    "imcos",
    "imcosh",
    "imcot",
    "imcsc",
    "imcsch",
    "imdiv",
    "imexp",
    "imln",
    "imlog10",
    "imlog2",
    "impower",
    "improduct",
    "imreal",
    "imsec",
    "imsech",
    "imsin",
    "imsinh",
    "imsqrt",
    "imsub",
    "imsum",
    "imtan",
    "imtanh",
    "intercept",
    "intrate",
    "ipmt",
    "irr",
    "isblank",
    "iserr",
    "iserror",
    "iseven",
    "isformula",
    "islogical",
    "isna",
    "isnontext",
    "isnumber",
    "isodd",
    "isoweeknum",
    "ispmt",
    "isref",
    "istext",
    "jis",
    "kurt",
    "large",
    "linest",
    "logest",
    "loginv",
    "lognormdist",
    "lookup",
    "match_criteria",
    "maxa",
    "mdeterm",
    "mduration",
    "mina",
    "minverse",
    "mirr",
    "mmult",
    "mode",
    "mround",
    "mtrans",
    "multinomial",
    "munit",
    "n",
]


def iferror(f: Callable[[], Any], alt: Any) -> Any:
    # NaN and Calc error tokens (#VALUE!, #REF!, …). A float-only check misses the tokens.
    try:
        val = f()
        if _is_calc_error(val):
            return alt
        return val
    except Exception:
        return alt


def ifna(f: Callable[[], Any], alt: Any) -> Any:
    try:
        val = f()
        if is_na_value(val):
            return alt
        return val
    except Exception:
        return alt


def _im_unary(fn: Callable[[complex], complex]) -> Callable[[Any], str]:
    def wrapper(inumber: Any) -> str:
        try:
            c = _to_complex(inumber)
            res = fn(c)
            return _from_complex(res)
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            return "#VALUE!"

    return wrapper


def _im_binary(fn: Callable[[complex, complex], complex]) -> Callable[[Any, Any], str]:
    def wrapper(inumber1: Any, inumber2: Any) -> str:
        try:
            c1 = _to_complex(inumber1)
            c2 = _to_complex(inumber2)
            return _from_complex(fn(c1, c2))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            return "#VALUE!"

    return wrapper


def _im_float(fn: Callable[[complex], float]) -> Callable[[Any], float]:
    def wrapper(inumber: Any) -> float:
        try:
            return float(fn(_to_complex(inumber)))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            return float("nan")

    return wrapper


@_im_float
def imabs(c: complex) -> float:
    return abs(c)


@_im_float
def imaginary(c: complex) -> float:
    return c.imag


@_im_float
def imargument(c: complex) -> float:
    return cmath.phase(c)


@_im_unary
def imconjugate(c: complex) -> complex:
    return c.conjugate()


@_im_unary
def imcos(c: complex) -> complex:
    return cmath.cos(c)


@_im_unary
def imcosh(c: complex) -> complex:
    return cmath.cosh(c)


@_im_unary
def imcot(c: complex) -> complex:
    return 1.0 / cmath.tan(c)


@_im_unary
def imcsc(c: complex) -> complex:
    return 1.0 / cmath.sin(c)


@_im_unary
def imcsch(c: complex) -> complex:
    return 1.0 / cmath.sinh(c)


@_im_binary
def imdiv(c1: complex, c2: complex) -> complex:
    return c1 / c2


@_im_unary
def imexp(c: complex) -> complex:
    return cmath.exp(c)


@_im_unary
def imln(c: complex) -> complex:
    return cmath.log(c)


@_im_unary
def imlog10(c: complex) -> complex:
    return cmath.log10(c)


@_im_unary
def imlog2(c: complex) -> complex:
    if c == 0:
        raise ValueError("log of zero")
    logged = cmath.log(c, 2)
    if not (math.isfinite(logged.real) and math.isfinite(logged.imag)):
        raise ValueError("non-finite log2")
    return logged


def impower(inumber: Any, number: Any) -> str:
    try:
        c = _to_complex(inumber)
        p = float(number)
        result = c**p
        # macOS libm returns a non-finite complex for (1+1j)**inf and does
        # not raise. _from_complex would then print "nannani" (nan
        # concatenated, because nan > 0 is false). Linux raises
        # OverflowError, which this except already maps to #VALUE!. A
        # non-finite power is the same failure.
        if not (math.isfinite(result.real) and math.isfinite(result.imag)):
            return "#VALUE!"
        return _from_complex(result)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return "#VALUE!"


def improduct(*args: Any) -> str:
    try:
        res = complex(1, 0)
        for arg in args:
            for v in np.asarray(arg, dtype=object).ravel():
                res *= _to_complex(v)
        return _from_complex(res)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return "#VALUE!"


@_im_float
def imreal(c: complex) -> float:
    return c.real


@_im_unary
def imsec(c: complex) -> complex:
    return 1.0 / cmath.cos(c)


@_im_unary
def imsech(c: complex) -> complex:
    return 1.0 / cmath.cosh(c)


@_im_unary
def imsin(c: complex) -> complex:
    return cmath.sin(c)


@_im_unary
def imsinh(c: complex) -> complex:
    return cmath.sinh(c)


@_im_unary
def imsqrt(c: complex) -> complex:
    return cmath.sqrt(c)


@_im_binary
def imsub(c1: complex, c2: complex) -> complex:
    return c1 - c2


def imsum(*args: Any) -> str:
    try:
        res = complex(0, 0)
        for arg in args:
            for v in np.asarray(arg, dtype=object).ravel():
                res += _to_complex(v)
        return _from_complex(res)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return "#VALUE!"


@_im_unary
def imtan(c: complex) -> complex:
    return cmath.tan(c)


@_im_unary
def imtanh(c: complex) -> complex:
    return cmath.tanh(c)


def intercept(data_y: Any, data_x: Any) -> float:
    from plugin.scripting.venv.calc_functions_n_s import slope

    s = slope(data_y, data_x)
    if np.isnan(s):
        return float("nan")
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    return float(np.mean(y[mask]) - s * np.mean(x[mask]))


def intrate(settlement: Any, maturity: Any, investment: Any, redemption: Any, basis: Any = 0) -> float:
    from .calc_functions_t_z import yearfrac

    try:
        s = float(settlement)
        m = float(maturity)
        inv = float(investment)
        red = float(redemption)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        # int(float(basis)) raises OverflowError on ±inf. That used to escape
        # the helper. Excel INTRATE is #NUM!, which this module reports as NaN.
        return float("nan")
    if s >= m or inv <= 0 or red <= 0 or b < 0 or b > 4:
        return float("nan")
    yf = yearfrac(s, m, b)
    if yf == 0 or math.isnan(yf):
        return float("nan")
    return (red - inv) / inv / yf


def ipmt(rate: Any, per: Any, nper: Any, pv_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    try:
        r = float(rate)
        # Excel/Calc truncate the period. A fractional per is a different
        # numpy-financial payment, not the truncated one.
        p = int(float(per))
        n = float(nper)
        pv_f = float(pv_val)
        fv_f = float(fv_val)
        t = 1 if int(float(type_val)) == 1 else 0
    except (ValueError, TypeError, OverflowError):
        # int(float(per)) and int(float(type_val)) raise OverflowError on ±inf.
        # That used to escape the helper. Excel IPMT is #NUM!, reported as NaN.
        return float("nan")
    # GetIpmt / Excel: per outside 1..nper is #NUM!. numpy-financial returns
    # 0 once per > nper (and NaN only for per < 1).
    if p < 1 or p > n:
        return float("nan")
    return _npf_result("ipmt", r, p, n, pv_f, fv_f, t)


def irr(values: Any, guess: Any = 0.1) -> float:
    # numpy-financial 1.1 irr ignores guess and returns one polynomial root
    # (smallest magnitude). Excel IRR(values, guess) is Newton's method from
    # that guess: guess 0.1 and 0.5 on [-5, 10.5, 1, -8, 1] are different
    # roots (~0.089 and ~0.71). Keep this solver.
    # dtype=float and float(guess) raise on a text cell. Sibling helpers return NaN.
    try:
        vals = np.asarray(values, dtype=float).ravel()
        x = float(guess)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # Simple Newton's method for IRR
    for _unused in range(100):
        f = 0.0
        df = 0.0
        for i, v in enumerate(vals):
            f += v / ((1 + x) ** i)
            if i > 0:
                df -= i * v / ((1 + x) ** (i + 1))
        if abs(f) < 1e-7:
            return float(x)
        if df == 0:
            break
        x = x - f / df
    return float("nan")


def isblank(val: Any) -> bool:
    return is_blank_value(val)


def iserr(val: Any) -> bool:
    # Real Calc error tokens, excluding #N/A. '#hashtag' is text.
    return _is_calc_error(val) and not is_na_value(val)


def iserror(val: Any) -> bool:
    # Real Calc error tokens and NaN (NA()). '#hashtag' is text.
    return _is_calc_error(val)


def iseven(val: Any) -> bool:
    try:
        f = float(val)
        if np.isnan(f):
            return False
        # int(inf) raises OverflowError, which the old handler let escape.
        return int(f) % 2 == 0
    except (ValueError, TypeError, OverflowError):
        return False


def isformula(val: Any) -> bool:
    # We do not have access to formula strings in PY() by default.
    return False


def islogical(val: Any) -> bool:
    return isinstance(val, bool)


def isna(val: Any) -> bool:
    return is_na_value(val)


def isnontext(val: Any) -> bool:
    return not isinstance(val, str) or val == "" or val.startswith("#")


def isnumber(val: Any) -> bool:
    # np.integer and np.floating count. bool and np.bool_ do not: they are logical.
    return isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, (bool, np.bool_))


def isodd(val: Any) -> bool:
    try:
        f = float(val)
        if np.isnan(f):
            return False
        return int(f) % 2 != 0
    except (ValueError, TypeError, OverflowError):
        return False


def isoweeknum(serial: Any) -> float:
    try:
        d = dt.date.fromordinal(int(float(serial)) + 693594)
        return float(d.isocalendar()[1])
    except Exception:
        return float("nan")


def ispmt(rate: Any, per: Any, nper: Any, pv_val: Any) -> float:
    try:
        r = float(rate)
        p = float(per)
        n = float(nper)
        pv_f = float(pv_val)
    except (ValueError, TypeError):
        return float("nan")
    # nper == 0 used to raise ZeroDivisionError (pv / nper). Excel/Calc are
    # #NUM!; this module returns NaN. Same guard as pmt.
    if n == 0:
        return float("nan")
    # ISPMT calculates interest for a loan with even principal payments
    # principal payment = pv / nper
    # balance after per periods = pv - (pv / nper) * per
    # interest for period 'per' (0-indexed in ISPMT) = balance * rate
    bal = pv_f - (pv_f / n) * p
    return -(bal * r)


def isref(val: Any) -> bool:
    # We do not have object references in PY(), only values.
    return False


def istext(val: Any) -> bool:
    # Text, except a real Calc error token. '#hashtag' is text.
    return isinstance(val, str) and not _is_calc_error(val)


def jis(text: Any) -> str | float:
    try:
        if text is None:
            return ""
        return str(text)
    except (ValueError, TypeError):
        return float("nan")


def kurt(*args: Any) -> float:
    try:
        import scipy.stats
    except ImportError:
        return float("nan")
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True)
    if len(arr) < 4 or np.std(arr, ddof=1) == 0:
        return float("nan")
    try:
        res = float(scipy.stats.kurtosis(arr, bias=False))
        return res if math.isfinite(res) else float("nan")
    except Exception:
        return float("nan")


def large(r: Any, k: Any) -> float:
    # Numbers only. Text such as '5', bools, and NaN are ignored, not sorted in.
    arr = _extract_numeric_array(r, propagate_nan=False)
    try:
        ki = int(float(k))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if not 0 < ki <= len(arr):
        return float("nan")
    arr.sort()
    return float(arr[-ki])


def linest(*args: Any, **kwargs: Any) -> Any:
    # Excel order is [mk, …, m1, b]. const=False forces b = 0. A row-oriented
    # known_x is transposed so it lines up with known_y.
    try:
        raw_y = np.asarray(args[0]).ravel()
        if any(isinstance(v, str) for v in raw_y):
            return "#VALUE!"
        data_y = np.asarray(args[0], dtype=float).ravel()
        if data_y.size == 0 or np.any(np.isnan(data_y)):
            return "#VALUE!"
        if len(args) > 1:
            raw_x = np.asarray(args[1]).ravel()
            if any(isinstance(v, str) for v in raw_x):
                return "#VALUE!"
            data_x = np.asarray(args[1], dtype=float)
            if np.any(np.isnan(data_x)):
                return "#VALUE!"
            if data_x.ndim == 1:
                data_x = data_x[:, np.newaxis]
            elif data_x.ndim == 2:
                if data_x.shape[0] != len(data_y) and data_x.shape[1] == len(data_y):
                    data_x = data_x.T
        else:
            data_x = np.arange(1, len(data_y) + 1, dtype=float)[:, np.newaxis]

        if data_x.shape[0] != len(data_y):
            return "#VALUE!"

        const = bool(kwargs.get("const", args[2] if len(args) > 2 else True))

        if const:
            c, _unused, _unused2, _unused3 = np.linalg.lstsq(
                np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None
            )
            slopes = c[:-1].tolist()
            intercept = float(c[-1])
            return [*slopes[::-1], intercept]
        else:
            c, _unused, _unused2, _unused3 = np.linalg.lstsq(data_x, data_y, rcond=None)
            slopes = c.tolist()
            return [*slopes[::-1], 0.0]
    except Exception:
        return "#VALUE!"


def logest(*args: Any, **kwargs: Any) -> Any:
    # Excel order reverses the slopes. const=False forces b = 1. A row-oriented
    # known_x is transposed so it lines up with known_y.
    try:
        raw_y = np.asarray(args[0]).ravel()
        if any(isinstance(v, str) for v in raw_y):
            return "#VALUE!"
        data_y = np.asarray(args[0], dtype=float).ravel()
        if data_y.size == 0 or np.any(np.isnan(data_y)):
            return "#VALUE!"
        if np.any(data_y <= 0):
            return "#VALUE!"
        data_y = np.log(data_y)
        if len(args) > 1:
            raw_x = np.asarray(args[1]).ravel()
            if any(isinstance(v, str) for v in raw_x):
                return "#VALUE!"
            data_x = np.asarray(args[1], dtype=float)
            if np.any(np.isnan(data_x)):
                return "#VALUE!"
            if data_x.ndim == 1:
                data_x = data_x[:, np.newaxis]
            elif data_x.ndim == 2:
                if data_x.shape[0] != len(data_y) and data_x.shape[1] == len(data_y):
                    data_x = data_x.T
        else:
            data_x = np.arange(1, len(data_y) + 1, dtype=float)[:, np.newaxis]

        if data_x.shape[0] != len(data_y):
            return "#VALUE!"

        const = bool(kwargs.get("const", args[2] if len(args) > 2 else True))

        if const:
            c, _unused, _unused2, _unused3 = np.linalg.lstsq(
                np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None
            )
            slopes = np.exp(c[:-1]).tolist()
            intercept = float(np.exp(c[-1]))
            return [*slopes[::-1], intercept]
        else:
            c, _unused, _unused2, _unused3 = np.linalg.lstsq(data_x, data_y, rcond=None)
            slopes = np.exp(c).tolist()
            return [*slopes[::-1], 1.0]
    except Exception:
        return "#VALUE!"


def loginv(p: Any, mean: Any, stdev: Any) -> float:
    try:
        import scipy.stats as st

        prob = float(p)
        m = float(mean)
        s = float(stdev)
        if prob < 0 or prob > 1 or s <= 0:
            return float("nan")
        return float(st.lognorm.ppf(prob, s, scale=math.exp(m)))
    except (ValueError, TypeError, ImportError):
        return float("nan")


def lognormdist(x: Any, mean: Any, stdev: Any, c: Any = 1) -> float:
    try:
        import scipy.stats as st

        x_val = float(x)
        m = float(mean)
        s = float(stdev)
        cum = bool(float(c))
        if x_val <= 0 or s <= 0:
            return float("nan")
        if cum:
            return float(st.lognorm.cdf(x_val, s, scale=math.exp(m)))
        return float(st.lognorm.pdf(x_val, s, scale=math.exp(m)))
    except (ValueError, TypeError, ImportError):
        return float("nan")


def lookup(lookup_val: Any, *args: Any) -> Any:
    # dtype=object keeps numbers as numbers. Text compares case-insensitively.
    if len(args) == 1:
        vec = np.asarray(args[0], dtype=object).ravel()
        result = vec
    else:
        lookup_vec = np.asarray(args[0], dtype=object).ravel()
        result = np.asarray(args[1], dtype=object).ravel()
        vec = lookup_vec
    best_idx = None
    for i, v in enumerate(vec):
        try:
            if float(v) <= float(lookup_val):
                best_idx = i
        except (ValueError, TypeError, OverflowError):
            if isinstance(v, str) and isinstance(lookup_val, str):
                if v.casefold() <= lookup_val.casefold():
                    best_idx = i
            elif str(v) <= str(lookup_val):
                best_idx = i
    if best_idx is None:
        return None
    if best_idx >= len(result):
        return "#N/A"
    return result[best_idx]





def maxa(*args: Any) -> float:
    vals = _collect_a_values(*args)
    if not vals.size:
        return 0.0
    return float(np.max(vals))


def mdeterm(matrix: Any) -> float:
    try:
        import numpy as np

        m = np.asarray(matrix, dtype=float)
        if m.ndim > 2:
            m = m[0]
        return float(np.linalg.det(m))
    except Exception:
        return float("nan")


def mduration(settlement: Any, maturity: Any, coupon: Any, yld: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_d_h import duration

    try:
        s = float(settlement)
        m = float(maturity)
        c = float(coupon)
        y = float(yld)
        f = float(frequency)
        b = int(float(basis))
    except (ValueError, TypeError, OverflowError):
        # int(float(basis)) raises OverflowError on ±inf. That used to escape
        # the helper. Excel MDURATION is #NUM!, which this module reports as NaN.
        return float("nan")
    macd = duration(s, m, c, y, f, b)
    if math.isnan(macd):
        return macd
    return macd / (1 + y / f)


def mina(*args: Any) -> float:
    vals = _collect_a_values(*args)
    if not vals.size:
        return 0.0
    return float(np.min(vals))


def minverse(matrix: Any) -> Any:
    try:
        import numpy as np

        m = np.asarray(matrix, dtype=float)
        if m.ndim > 2:
            m = m[0]
        return np.linalg.inv(m).tolist()
    except Exception:
        return "#VALUE!"


def mirr(values: Any, finance_rate: Any, reinvest_rate: Any) -> float:
    try:
        vals = [float(x) for x in np.asarray(values).ravel()]
        fr = float(finance_rate)
        rr = float(reinvest_rate)
    except (ValueError, TypeError):
        return float("nan")
    # rate == -1 used to divide by zero (later negative flows) or return a
    # finite number (reinvest rate -1). numpy-financial's NPV is undefined
    # there and returns NaN, which is Excel #NUM!.
    return _npf_result("mirr", vals, fr, rr)


def mmult(array1: Any, array2: Any) -> Any:
    try:
        import numpy as np

        a1 = np.asarray(array1, dtype=float)
        if a1.ndim > 2:
            a1 = a1[0]
        a2 = np.asarray(array2, dtype=float)
        if a2.ndim > 2:
            a2 = a2[0]
        return np.matmul(a1, a2).tolist()
    except Exception:
        return "#VALUE!"


def mode(r: Any) -> Any:
    # dtype=object. A bare asarray stringifies a mixed range, so 1 comes back as text.
    vals = [x for x in np.asarray(r, dtype=object).ravel() if x is not None and x != ""]
    if not vals:
        return float("nan")
    counts = Counter(vals)
    best = max(counts.values())
    # Excel/Calc MODE is #N/A when nothing repeats. na() is float nan, which
    # isna() already treats as #N/A. most_common used to return a singleton.
    if best < 2:
        return float("nan")
    winners = [v for v, c in counts.items() if c == best]
    # Tie-break is the lowest value, not the first one Counter saw
    # (mode([2, 2, 1, 1]) was 2).
    try:
        return min(winners)
    except TypeError:
        return winners[0]


def mround(number: Any, multiple: Any) -> float:
    # float() on text or a blank cell used to raise ValueError.
    try:
        n = float(number)
        m = float(multiple)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # math.floor/ceil raise OverflowError on ±inf and ValueError on NaN.
    # A huge quotient (1e308 / 1e-308) overflows the same way. Excel MROUND
    # is #NUM! for a non-finite argument; this module reports that as NaN.
    if not math.isfinite(n) or not math.isfinite(m):
        return float("nan")
    # multiple 0 returns 0. LibreOffice analysis.cxx getMround
    # (if fMult == 0.0 return fMult) and Excel agree; it is not #DIV/0!.
    if m == 0:
        return 0.0
    if (n > 0 and m < 0) or (n < 0 and m > 0):
        return float("nan")
    # Python round() is banker's rounding (half to even), so MROUND(2.5, 1)
    # was 2. Excel/Calc round halves away from zero (result 3). Same-sign
    # inputs make the quotient non-negative; floor(q + 0.5) is that rounding.
    quot = n / m
    if not math.isfinite(quot):
        return float("nan")
    rounded = math.floor(quot + 0.5) if quot >= 0 else math.ceil(quot - 0.5)
    return float(rounded * m)


def mtrans(matrix: Any) -> Any:
    try:
        import numpy as np

        m = np.asarray(matrix)
        if m.ndim > 2:
            m = m[0]
        return np.transpose(m).tolist()
    except Exception:
        return "#VALUE!"


def multinomial(*args: Any) -> float:
    try:
        vals = []
        for arg in args:
            for v in np.asarray(arg).ravel():
                vals.append(int(float(v)))
        return float(math.factorial(sum(vals)) / math.prod(math.factorial(x) for x in vals))
    except Exception:
        return float("nan")


def munit(dimension: Any) -> Any:
    try:
        import numpy as np

        return np.eye(int(dimension)).tolist()
    except Exception:
        return "#VALUE!"


def n(val: Any) -> float:
    # N() of any text is 0, including a numeric-looking string such as "5".
    if isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, (bool, np.bool_)):
        return float(val)
    if isinstance(val, (bool, np.bool_)):
        return 1.0 if val else 0.0
    if isinstance(val, str):
        return 0.0
    return 0.0
