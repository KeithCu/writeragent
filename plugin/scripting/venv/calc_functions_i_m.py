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
from .coerce import is_missing_value


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
        if is_missing_value(val):
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
    except (ValueError, TypeError):
        return "#VALUE!"


def imcosh(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.cosh(c))
    except (ValueError, TypeError):
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
    except (ValueError, TypeError):
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
        return _from_complex(cmath.log(c, 2))
    except (ValueError, TypeError):
        return "#VALUE!"


def impower(inumber: Any, number: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        c = _to_complex(inumber)
        p = float(number)
        return _from_complex(c**p)
    except (ValueError, TypeError):
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
    except (ValueError, TypeError):
        return "#VALUE!"


def imsinh(inumber: Any) -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex, _to_complex

    try:
        import cmath

        c = _to_complex(inumber)
        return _from_complex(cmath.sinh(c))
    except (ValueError, TypeError):
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
        p = int(float(per))
        n = float(nper)
        pv_f = float(pv_val)
        fv_f = float(fv_val)
        t = int(float(type_val))
    except (ValueError, TypeError):
        return float("nan")
    if p < 1 or p > n:
        return float("nan")

    if r == 0:
        return 0.0

    # PMT
    factor = (1 + r) ** n
    if t == 1:
        pmt_amt = -(pv_f * factor + fv_f) * r / ((factor - 1) * (1 + r))
    else:
        pmt_amt = -(pv_f * factor + fv_f) * r / (factor - 1)

    if t == 0:
        # End of period
        bal = pv_f * ((1 + r) ** (p - 1)) + pmt_amt * (((1 + r) ** (p - 1)) - 1) / r
        interest = -bal * r
        return interest
    else:
        # Beginning of period
        if p == 1:
            return 0.0
        bal = pv_f * ((1 + r) ** (p - 1)) + pmt_amt * (((1 + r) ** (p - 1)) - 1) / r
        # Since payment is at beginning, the interest for period p is based on balance after payment p-1
        interest = -(bal - (-pmt_amt)) * r if bal != 0 else 0.0
        # Actually standard IPMT formula:
        bal2 = pv_f * ((1 + r) ** (p - 2)) + pmt_amt * (((1 + r) ** (p - 2)) - 1) / r
        return -(bal2 + pmt_amt) * r


def irr(values: Any, guess: Any = 0.1) -> float:
    vals = np.asarray(values, dtype=float).ravel()
    # Simple Newton's method for IRR
    x = float(guess)
    for _ in range(100):
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
    return is_missing_value(val)


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
        return int(f) % 2 == 0
    except (ValueError, TypeError):
        return False


def isformula(val: Any) -> bool:
    # We do not have access to formula strings in PY() by default.
    return False


def islogical(val: Any) -> bool:
    return isinstance(val, bool)


def isna(val: Any) -> bool:
    return is_missing_value(val)


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
    except (ValueError, TypeError):
        return False


def isoweeknum(serial: Any) -> float:
    try:
        d = datetime.date.fromordinal(int(float(serial)) + 693594)
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
    arr = sorted([float(x) for x in np.asarray(r).ravel() if x is not None and x != ""], reverse=True)
    ki = int(float(k))
    return float(arr[ki - 1]) if 0 < ki <= len(arr) else float("nan")


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
        c, _, _, _ = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)
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

        c, _, _, _ = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)
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
                v_num = float(val)
            except (ValueError, TypeError):
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
            else:
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
    n = len(vals) - 1
    if n < 1:
        return float("nan")

    # NPV of negative flows at finance rate
    npv_neg = sum(v / ((1 + fr) ** i) for i, v in enumerate(vals) if v < 0)
    # FV of positive flows at reinvest rate
    fv_pos = sum(v * ((1 + rr) ** (n - i)) for i, v in enumerate(vals) if v > 0)

    if npv_neg == 0 or fv_pos == 0:
        return float("nan")

    try:
        # standard formula:
        # MIRR = (-fv_pos / npv_neg) ** (1/n) - 1
        return (-fv_pos / npv_neg) ** (1.0 / n) - 1.0
    except (ValueError, TypeError):
        return float("nan")


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
    return counts.most_common(1)[0][0]


def mround(number: Any, multiple: Any) -> float:
    n = float(number)
    m = float(multiple)
    if m == 0:
        return 0.0
    if (n > 0 and m < 0) or (n < 0 and m > 0):
        return float("nan")
    return float(round(n / m) * m)


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
