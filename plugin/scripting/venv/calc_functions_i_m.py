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
from collections import Counter
from typing import Any, Callable

import numpy as np

from .coerce import is_blank_value, is_missing_value, is_na_value


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
    try:
        val = f()
        if isinstance(val, float) and np.isnan(val):
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


def imabs(inumber: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_complex

    try:
        return float(abs(_to_complex(inumber)))
    except (ValueError, TypeError):
        return float("nan")


def imaginary(inumber: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_complex

    try:
        return float(_to_complex(inumber).imag)
    except (ValueError, TypeError):
        return float("nan")


def imargument(inumber: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_complex

    try:
        import cmath

        return float(cmath.phase(_to_complex(inumber)))
    except (ValueError, TypeError):
        return float("nan")


def imconjugate(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        c = _to_complex(inumber)
        return _from_complex(c.conjugate())
    except (ValueError, TypeError):
        return "#VALUE!"


def imcos(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.cos(c))
    except (ValueError, TypeError, OverflowError):
        # cmath.cos raises OverflowError (not ValueError) for a large
        # imaginary part, e.g. IMCOS("1000i"). That used to escape the helper.
        return "#VALUE!"


def imcosh(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.cosh(c))
    except (ValueError, TypeError, OverflowError):
        # cmath.cosh(1000) raises OverflowError, which used to escape the helper.
        return "#VALUE!"


def imcot(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(1.0 / cmath.tan(c))
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imcsc(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(1.0 / cmath.sin(c))
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imcsch(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(1.0 / cmath.sinh(c))
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imdiv(inumber1: Any, inumber2: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        c1 = _to_complex(inumber1)
        c2 = _to_complex(inumber2)
        return _from_complex(c1 / c2)
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imexp(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.exp(c))
    except (ValueError, TypeError, OverflowError):
        # cmath.exp(1000) raises OverflowError (IMEXP("1000")), which used to escape the helper.
        return "#VALUE!"


def imln(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.log(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imlog10(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.log10(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imlog2(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        logged = cmath.log(c, 2)
        # cmath.log(0) and cmath.log10(0) raise ValueError, so IMLN/IMLOG10
        # return #VALUE!. cmath.log(0, 2) instead returns (-inf+nanj), and
        # _from_complex concatenates that into the string '-infnani'.
        if not (math.isfinite(logged.real) and math.isfinite(logged.imag)):
            return "#VALUE!"
        return _from_complex(logged)
    except (ValueError, TypeError):
        return "#VALUE!"


def impower(inumber: Any, number: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        c = _to_complex(inumber)
        p = float(number)
        return _from_complex(c**p)
    except (ValueError, TypeError, OverflowError):
        # complex ** raises OverflowError on a huge power (IMPOWER("2", 10000)).
        return "#VALUE!"


def improduct(*args: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import builtins

        res = builtins.complex(1, 0)
        for arg in args:
            for v in np.asarray(arg).ravel():
                res *= _to_complex(v)
        return _from_complex(res)
    except (ValueError, TypeError):
        return "#VALUE!"


def imreal(inumber: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_complex

    try:
        return float(_to_complex(inumber).real)
    except (ValueError, TypeError):
        return float("nan")


def imsec(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(1.0 / cmath.cos(c))
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imsech(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(1.0 / cmath.cosh(c))
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imsin(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.sin(c))
    except (ValueError, TypeError, OverflowError):
        # cmath.sin raises OverflowError for a large imaginary part, e.g. IMSIN("1000i").
        return "#VALUE!"


def imsinh(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.sinh(c))
    except (ValueError, TypeError, OverflowError):
        # cmath.sinh(1000) raises OverflowError, which used to escape the helper.
        return "#VALUE!"


def imsqrt(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.sqrt(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imsub(inumber1: Any, inumber2: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        c1 = _to_complex(inumber1)
        c2 = _to_complex(inumber2)
        return _from_complex(c1 - c2)
    except (ValueError, TypeError):
        return "#VALUE!"


def imsum(*args: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import builtins

        res = builtins.complex(0, 0)
        for arg in args:
            for v in np.asarray(arg).ravel():
                res += _to_complex(v)
        return _from_complex(res)
    except (ValueError, TypeError):
        return "#VALUE!"


def imtan(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.tan(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imtanh(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.tanh(c))
    except (ValueError, TypeError):
        return "#VALUE!"


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
    from plugin.scripting.venv.calc_functions_a_c import _year_frac

    try:
        s = float(settlement)
        m = float(maturity)
        inv = float(investment)
        red = float(redemption)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")
    if s >= m or inv <= 0 or red <= 0 or b < 0 or b > 4:
        return float("nan")
    yf = _year_frac(s, m, b)
    if yf == 0:
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
    except (ValueError, TypeError):
        return float("nan")
    # GetIpmt / Excel: per outside 1..nper is #NUM!. numpy-financial returns
    # 0 once per > nper (and NaN only for per < 1).
    if p < 1 or p > n:
        return float("nan")
    from plugin.scripting.venv.calc_functions_d_h import _npf_result

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
    if isinstance(val, str) and val.startswith("#"):
        return not val.upper().startswith("#N/A")
    return False


def iserror(val: Any) -> bool:
    return isinstance(val, str) and val.startswith("#")


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
    return isinstance(val, (int, float)) and not isinstance(val, bool)


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
    return isinstance(val, str) and not (isinstance(val, str) and val.startswith("#"))


def jis(text: Any) -> str | float:
    try:
        if text is None:
            return ""
        return str(text)
    except (ValueError, TypeError):
        return float("nan")


def kurt(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    n = len(vals)
    if n < 4:
        return float("nan")
    arr = np.asarray(vals)
    m = np.mean(arr)
    s = np.std(arr, ddof=1)
    if s == 0:
        return float("nan")
    # Excel/Calc kurtosis formula
    z = (arr - m) / s
    term1 = (n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3))
    term2 = np.sum(z**4)
    term3 = (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))
    return float(term1 * term2 - term3)


def large(r: Any, k: Any) -> float:
    # Text cells are not numbers. The list comprehension called float() with
    # no handler, so LARGE(["a","b"], 1) raised ValueError — and spreadsheet
    # import emits this helper for LARGE. Skip non-numeric cells (as kurt()
    # does) and return nan when k is not a number or not enough numbers remain.
    vals: list[float] = []
    for x in np.asarray(r).ravel():
        if x is None or x == "":
            continue
        try:
            vals.append(float(x))
        except (ValueError, TypeError):
            continue
    try:
        ki = int(float(k))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if not 0 < ki <= len(vals):
        return float("nan")
    vals.sort(reverse=True)
    return float(vals[ki - 1])


def linest(*args: Any) -> Any:
    # A complete implementation using numpy.polyfit or similar
    try:
        import numpy as np

        data_y = np.asarray(args[0]).ravel()
        if len(args) > 1:
            data_x = np.asarray(args[1])
            if data_x.ndim == 1:
                data_x = data_x[:, np.newaxis]
        else:
            data_x = np.arange(1, len(data_y) + 1)[:, np.newaxis]

        # Simple fallback for 1D or 2D:
        c, _unused, _unused2, _unused3 = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)
        return c.tolist()
    except Exception:
        return "#VALUE!"


def logest(*args: Any) -> Any:
    try:
        import numpy as np

        data_y = np.asarray(args[0]).ravel()
        data_y = np.log(data_y)
        if len(args) > 1:
            data_x = np.asarray(args[1])
            if data_x.ndim == 1:
                data_x = data_x[:, np.newaxis]
        else:
            data_x = np.arange(1, len(data_y) + 1)[:, np.newaxis]

        c, _unused, _unused2, _unused3 = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)
        c[:-1] = np.exp(c[:-1])
        c[-1] = np.exp(c[-1])
        return c.tolist()
    except Exception:
        return "#VALUE!"


def loginv(p: Any, mean: Any, stdev: Any) -> float:
    try:
        import scipy.stats as st
        import math

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
        import math

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
    if len(args) == 1:
        vec = np.asarray(args[0]).ravel()
        result = vec
    else:
        lookup_vec = np.asarray(args[0]).ravel()
        result = np.asarray(args[1]).ravel()
        vec = lookup_vec
    best_idx = None
    for i, v in enumerate(vec):
        try:
            if float(v) <= float(lookup_val):
                best_idx = i
        except (ValueError, TypeError):
            if str(v) <= str(lookup_val):
                best_idx = i
    if best_idx is None:
        return None
    # A result vector shorter than the lookup vector used to raise IndexError
    # (lookup(3, [1,2,3], [10,20])). Calc returns #N/A for that miss.
    if best_idx >= len(result):
        return "#N/A"
    return result[best_idx]


def match_criteria(val: Any, crit: Any) -> bool:
    if is_missing_value(crit):
        return is_missing_value(val)
    if isinstance(crit, str):
        m = re.match(r"^([<>=]+)(.*)$", crit)
        if m:
            op, val_str = m.groups()
            try:
                c_num = float(val_str)
            except (ValueError, TypeError):
                c_num = None
            try:
                v_num = float(val)
            except (ValueError, TypeError):
                v_num = None
            # A numeric criterion used to fall through to lexicographic
            # compare whenever the cell failed float(). "abc" > "5" is True,
            # so COUNTIF(["abc"], ">5") counted the text. Excel/Calc compare
            # numbers only; <> still matches because the text is not the number.
            if c_num is not None and v_num is None:
                if op == "<>":
                    return True
                if op in ("=", "==", "<", "<=", ">", ">="):
                    return False
            elif c_num is not None and v_num is not None:
                if op in ("=", "=="):
                    return v_num == c_num
                if op == "<>":
                    return v_num != c_num
                if op == "<":
                    return v_num < c_num
                if op == "<=":
                    return v_num <= c_num
                if op == ">":
                    return v_num > c_num
                if op == ">=":
                    return v_num >= c_num
            else:
                c_str = val_str
                v_str = str(val)
                if op in ("=", "=="):
                    return v_str == c_str
                if op == "<>":
                    return v_str != c_str
                if op == "<":
                    return v_str < c_str
                if op == "<=":
                    return v_str <= c_str
                if op == ">":
                    return v_str > c_str
                if op == ">=":
                    return v_str >= c_str
    try:
        if float(val) == float(crit):
            return True
    except (ValueError, TypeError):
        pass
    return str(val) == str(crit)


def maxa(*args: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_float_a

    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
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
    except (ValueError, TypeError):
        return float("nan")
    macd = duration(s, m, c, y, f, b)
    if math.isnan(macd):
        return macd
    return macd / (1 + y / f)


def mina(*args: Any) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _to_float_a

    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
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
    from plugin.scripting.venv.calc_functions_d_h import _npf_result

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
    vals = [x for x in np.asarray(r).ravel() if x is not None and x != ""]
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
    n = float(number)
    m = float(multiple)
    if m == 0:
        return 0.0
    if (n > 0 and m < 0) or (n < 0 and m > 0):
        return float("nan")
    # Python round() is banker's rounding (half to even), so MROUND(2.5, 1)
    # was 2. Excel/Calc round halves away from zero (result 3). Same-sign
    # inputs make the quotient non-negative; floor(q + 0.5) is that rounding.
    quot = n / m
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
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return 0.0
    return 0.0
