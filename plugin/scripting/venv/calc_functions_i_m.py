# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""

from __future__ import annotations

import cmath
import math
import statistics
import sys
from collections import Counter
from typing import Any, Callable

import numpy as np

from .calc_functions_util import (
    _as_bool_flag,
    _clean_paired_arrays,
    _collect_a_values,
    _extract_numeric_array,
    _from_complex,
    _is_calc_error,
    _npf_result,
    _parse_complex,
    _serial_to_date,
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


def _im_suffix(first: str, second: str = "") -> str:
    """First imaginary unit wins. A real number has an empty suffix.

    ``Complex::Add`` and ``Mult`` (``analysishelper.hxx``) do
    ``if (!c) c = other.c``. ``GetString`` writes ``j`` only when the
    stored unit is ``j``; otherwise it writes ``i``.
    """
    return first or second or "i"


def _im_unary(fn: Callable[[complex], complex]) -> Callable[[Any], str]:
    def wrapper(inumber: Any) -> str:
        try:
            number, suffix = _parse_complex(inumber)
            return _from_complex(fn(number), _im_suffix(suffix))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            return "#VALUE!"

    return wrapper


def _im_binary(fn: Callable[[complex, complex], complex]) -> Callable[[Any, Any], str]:
    def wrapper(inumber1: Any, inumber2: Any) -> str:
        try:
            left, left_suffix = _parse_complex(inumber1)
            right, right_suffix = _parse_complex(inumber2)
            return _from_complex(fn(left, right), _im_suffix(left_suffix, right_suffix))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            return "#VALUE!"

    return wrapper


def _im_float(fn: Callable[[complex], float]) -> Callable[[Any], float]:
    def wrapper(inumber: Any) -> float:
        try:
            # getImabs / getImargument return finiteOrThrow(result). A
            # non-finite magnitude is NaN here. String IM functions raise
            # from _from_complex and become "#VALUE!" instead.
            result = float(fn(_to_complex(inumber)))
            if not math.isfinite(result):
                return float("nan")
            return result
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
    # Complex::Arg (analysishelper.cxx) throws when both parts are 0.
    # cmath.phase(0) is 0. The note in that function calls arg(0) undefined.
    if c.real == 0.0 and c.imag == 0.0:
        raise ValueError("arg of zero")
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
    # cmath.log(0) raises. _from_complex rejects a non-finite log.
    return cmath.log(c, 2)


def impower(inumber: Any, number: Any) -> str:
    try:
        number_c, suffix = _parse_complex(inumber)
        power = float(number)
        return _from_complex(number_c**power, _im_suffix(suffix))
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return "#VALUE!"


def _im_fold(args: tuple[Any, ...], op: Callable[[complex, complex], complex]) -> str:
    """IMSUM and IMPRODUCT.

    ``ComplexList::Append`` (``analysishelper.cxx``) skips a blank and an
    empty string. ``getImsum`` and ``getImproduct`` both return
    ``Complex(0)`` when nothing remains, so an empty product is 0, not 1.
    The fold starts at the first value so a single ``"j"`` keeps that unit.
    """
    try:
        acc: complex | None = None
        suffix = ""
        for arg in args:
            for value in np.asarray(arg, dtype=object).ravel():
                if value is None or value == "":
                    continue
                number, unit = _parse_complex(value)
                if acc is None:
                    acc = number
                    suffix = unit
                else:
                    acc = op(acc, number)
                    if not suffix:
                        suffix = unit
        if acc is None:
            acc = complex(0)
        return _from_complex(acc, _im_suffix(suffix))
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return "#VALUE!"


def improduct(*args: Any) -> str:
    return _im_fold(args, lambda left, right: left * right)


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
    return _im_fold(args, lambda left, right: left + right)


@_im_unary
def imtan(c: complex) -> complex:
    return cmath.tan(c)


@_im_unary
def imtanh(c: complex) -> complex:
    return cmath.tanh(c)


def intercept(data_y: Any, data_x: Any) -> float:
    # steyx imports intercept, so this import stays inside the function.
    # A module-level import of n_s would cycle. The absolute import failed
    # when the worker's package root was not the repo root.
    from .calc_functions_n_s import slope

    # slope drops text pairs. Re-parsing the original arrays with
    # dtype=float raised ValueError once slope had already accepted the
    # numeric pairs (intercept([1, "text", 3], [1, 2, 3])).
    cleaned = _clean_paired_arrays(data_y, data_x)
    if cleaned is None:
        return float("nan")
    y, x = cleaned
    s = slope(y, x)
    if np.isnan(s):
        return float("nan")
    return float(np.mean(y) - s * np.mean(x))


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
    # What was wrong: INTRATE with an infinite redemption returned inf.
    # How it happened: the quotient is outside the conversion try, and float
    # division does not raise. getIntrate returns finiteOrThrow.
    # Why this change fixes it: a non-finite quotient is NaN.
    res = (red - inv) / inv / yf
    return res if math.isfinite(res) else float("nan")


def ipmt(rate: Any, per: Any, nper: Any, pv_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    try:
        r = float(rate)
        # Excel/Calc truncate the period. A fractional per is a different
        # numpy-financial payment, not the truncated one.
        p = int(float(per))
        n = float(nper)
        pv_f = float(pv_val)
        fv_f = float(fv_val)
        type_f = float(type_val)
    except (ValueError, TypeError, OverflowError):
        # int(float(per)) raises OverflowError on ±inf. That used to escape
        # the helper. Excel IPMT is #NUM!, reported as NaN.
        return float("nan")
    # ScIpmt (interpr2.cxx) uses GetBool(), which is GetDouble() != 0.
    # int(type) == 1 treated 2, -1, and 0.5 as end of period. CUMIPMT is
    # the function that rejects a flag other than 0 or 1. A non-finite
    # type is still #NUM!. numpy-financial only understands 0 and 1.
    if not math.isfinite(type_f):
        return float("nan")
    t = 0 if type_f == 0.0 else 1
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
        estimated = float(guess)
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    # ScIRR (interpr2.cxx) starts at 0.1 when the guess is -1. (1+x)**i
    # is 0 there, so Newton never moves and a cashflow such as [-1, 1]
    # came back NaN instead of 0. A later step that lands on -1, or a
    # huge (1+x)**i, is #NUM!. OverflowError used to escape the helper.
    x = 0.1 if estimated == -1.0 else estimated
    # The stop used to be abs(NPV) < 1e-7 for 100 steps. ScIRR stops when
    # the rate step drops below SCdEpsilon (1e-7), and it gives up after
    # 20 steps (NoConvergence). At a 1e9 scale the NPV residual sticks
    # near 1.2e-7 while the step is already ~1e-16, so the old test
    # returned NaN for a rate Calc finds. A guess of exactly 0 that lands
    # within 1e-7 of 0 is snapped to 0 before that check.
    eps = 1.0
    try:
        for _unused in range(20):
            if not math.isfinite(x) or x == -1.0:
                return float("nan")
            f = 0.0
            df = 0.0
            for i, v in enumerate(vals):
                f += v / ((1 + x) ** i)
                if i > 0:
                    df -= i * v / ((1 + x) ** (i + 1))
            if df == 0.0:
                break
            x_new = x - f / df
            eps = abs(x_new - x)
            x = x_new
            if not eps > 1e-7:
                break
    except (OverflowError, ZeroDivisionError):
        return float("nan")
    if estimated == 0.0 and math.isfinite(x) and abs(x) < 1e-7:
        x = 0.0
    if eps < 1e-7 and math.isfinite(x):
        return float(x)
    return float("nan")


def isblank(val: Any) -> bool:
    return is_blank_value(val)


def iserr(val: Any) -> bool:
    # Real Calc error tokens, excluding #N/A. '#hashtag' is text.
    return _is_calc_error(val) and not is_na_value(val)


def iserror(val: Any) -> bool:
    # Real Calc error tokens and NaN (NA()). '#hashtag' is text.
    return _is_calc_error(val)


def _parity(val: Any) -> int | None:
    """Truncated integer, or None when the value is not a finite number.

    ``int(inf)`` raises ``OverflowError``. That used to escape ``iseven``.
    NaN is not even and not odd.
    """
    try:
        number = float(val)
        if np.isnan(number):
            return None
        return int(number)
    except (ValueError, TypeError, OverflowError):
        return None


def iseven(val: Any) -> bool:
    number = _parity(val)
    return number is not None and number % 2 == 0


def isformula(val: Any) -> bool:
    # We do not have access to formula strings in PY() by default.
    return False


def islogical(val: Any) -> bool:
    # What was wrong: islogical(np.bool_(True)) was False.
    # How it happened: np.bool_ is not a subclass of bool, and this was the
    # only logical check that tested bool alone. _is_number excludes both,
    # and n() maps both to 1.0 / 0.0.
    # Why this change fixes it: a NumPy logical is logical, matching ISLOGICAL.
    return isinstance(val, (bool, np.bool_))


def isna(val: Any) -> bool:
    return is_na_value(val)


def _is_number(val: Any) -> bool:
    # np.integer and np.floating count. bool and np.bool_ do not: they are logical.
    return isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, (bool, np.bool_))


def isnontext(val: Any) -> bool:
    # startswith("#") made isnontext("#hashtag") true while istext of that
    # string is also true. "" had the same contradiction. Error tokens stay
    # non-text because istext already excludes them.
    return not istext(val)


def isnumber(val: Any) -> bool:
    return _is_number(val)


def isodd(val: Any) -> bool:
    number = _parity(val)
    return number is not None and number % 2 != 0


def isoweeknum(serial: Any) -> float:
    # Same ordinal as every other serial helper. The inline fromordinal
    # retyped 693594 and used except Exception.
    d = _serial_to_date(serial)
    if d is None:
        return float("nan")
    return float(d.isocalendar()[1])


def ispmt(rate: Any, per: Any, nper: Any, pv_val: Any) -> float:
    try:
        r = float(rate)
        p = float(per)
        n = float(nper)
        pv_f = float(pv_val)
    except (ValueError, TypeError, OverflowError):
        # float() of a huge int raises OverflowError. The other payment
        # helpers already turn that into NaN. This one let it escape.
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


def jis(text: Any) -> str:
    # No half-width map. str(1.0) is "1.0"; an integral float is the digits.
    # None is a blank cell. str() of a cell does not raise ValueError.
    if text is None:
        return ""
    if isinstance(text, float) and math.isfinite(text) and text.is_integer():
        return str(int(text))
    return str(text)


def kurt(*args: Any) -> float:
    # ScKurt (interpr3.cxx). Fewer than 4 numbers, or a zero sample stdev,
    # is #DIV/0!. This is the unbiased excess kurtosis. scipy.stats.kurtosis
    # (bias=False) is the same expression, and a missing scipy used to make
    # every call NaN.
    arr = _extract_numeric_array(*args, ignore_text=True, ignore_bool=True)
    count = len(arr)
    if count < 4:
        return float("nan")
    mean = float(np.mean(arr))
    stdev = float(np.std(arr, ddof=1))
    if stdev == 0.0:
        return float("nan")
    k_d = (count - 2.0) * (count - 3.0)
    k_l = count * (count + 1.0) / ((count - 1.0) * k_d)
    k_t = 3.0 * (count - 1.0) * (count - 1.0) / k_d
    centered = (arr - mean) / stdev
    res = float(np.sum(centered**4) * k_l - k_t)
    return res if math.isfinite(res) else float("nan")


def large(r: Any, k: Any) -> float:
    # Numbers only. Text such as '5', bools, and NaN are ignored, not sorted in.
    arr = _extract_numeric_array(r, propagate_nan=False)
    try:
        ki = int(float(k))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    if not 0 < ki <= len(arr):
        return float("nan")
    # The kth largest sits at index -ki. The rest of the array stays unsorted.
    return float(np.partition(arr, -ki)[-ki])


def _regression_inputs(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray, bool] | str:
    """Shared known_y / known_x / const parse for LINEST and LOGEST.

    Returns ``"#VALUE!"`` when the ranges are illegal. Omitted known_x
    (the one-argument call) is ``1..n``. A present blank (``None``) is not
    that omission: ``ScLinest`` uses ``1..n`` only for ``IsMissing``.
    The fourth argument, ``stats``, is ignored. ``CalculateRGPRKP`` builds
    a 5-row statistics matrix this helper does not port.
    """
    raw_y = np.asarray(args[0], dtype=object).ravel()
    if any(isinstance(v, str) for v in raw_y):
        return "#VALUE!"
    data_y = np.asarray(args[0], dtype=float).ravel()
    if data_y.size == 0 or np.any(np.isnan(data_y)):
        return "#VALUE!"
    if len(args) <= 1:
        data_x = np.arange(1, len(data_y) + 1, dtype=float)[:, np.newaxis]
    elif args[1] is None:
        return "#VALUE!"
    else:
        raw_x = np.asarray(args[1], dtype=object).ravel()
        if any(isinstance(v, str) for v in raw_x):
            return "#VALUE!"
        data_x = np.asarray(args[1], dtype=float)
        if np.any(np.isnan(data_x)):
            return "#VALUE!"
        if data_x.ndim == 1:
            data_x = data_x[:, np.newaxis]
        elif data_x.ndim == 2 and data_x.shape[0] != len(data_y) and data_x.shape[1] == len(data_y):
            data_x = data_x.T
    if data_x.shape[0] != len(data_y):
        return "#VALUE!"
    if "const" in kwargs:
        raw_const = kwargs["const"]
    elif len(args) > 2:
        raw_const = args[2]
    else:
        raw_const = True
    return data_y, data_x, _as_bool_flag(raw_const)


def _fit_line(data_y: np.ndarray, data_x: np.ndarray, const: bool) -> list[float]:
    """Coefficients in ``[mk, …, m1, b]`` order. ``const=False`` forces ``b = 0``."""
    if const:
        coeffs = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)[0]
        slopes = coeffs[:-1]
        intercept_b = float(coeffs[-1])
    else:
        coeffs = np.linalg.lstsq(data_x, data_y, rcond=None)[0]
        slopes = coeffs
        intercept_b = 0.0
    return [float(v) for v in slopes[::-1]] + [intercept_b]


def linest(*args: Any, **kwargs: Any) -> Any:
    # Excel order is [mk, …, m1, b]. const=False forces b = 0. A row-oriented
    # known_x is transposed so it lines up with known_y.
    try:
        parsed = _regression_inputs(args, kwargs)
        # Equality does not narrow tuple | str for mypy or basedpyright.
        # The helper's only string is "#VALUE!".
        if isinstance(parsed, str):
            return "#VALUE!"
        data_y, data_x, const = parsed
        return _fit_line(data_y, data_x, const)
    except Exception:
        return "#VALUE!"


def logest(*args: Any, **kwargs: Any) -> Any:
    # Excel order reverses the slopes. const=False forces b = 1. A row-oriented
    # known_x is transposed so it lines up with known_y.
    try:
        parsed = _regression_inputs(args, kwargs)
        if isinstance(parsed, str):
            return "#VALUE!"
        data_y, data_x, const = parsed
        if np.any(data_y <= 0):
            return "#VALUE!"
        fitted = _fit_line(np.log(data_y), data_x, const)
        # const=False stores a log-space intercept of 0. The multiplicative
        # intercept is 1, not exp of a fitted b.
        if const:
            return [float(np.exp(v)) for v in fitted]
        return [float(np.exp(v)) for v in fitted[:-1]] + [1.0]
    except Exception:
        return "#VALUE!"


def loginv(p: Any, mean: Any, stdev: Any) -> float:
    try:
        prob = float(p)
        m = float(mean)
        s = float(stdev)
        # ScLogNormInv (interpr3.cxx): fP <= 0 or fP >= 1 is
        # IllegalArgument. gaussinv(0) is -inf and gaussinv(1) is +inf.
        if prob <= 0 or prob >= 1 or s <= 0:
            return float("nan")
        # exp(mue + sigma * gaussinv(p)). statistics.NormalDist is that
        # inverse CDF. scipy.stats used to be imported inside the call, so
        # a missing scipy made every probability NaN.
        res = math.exp(m + s * statistics.NormalDist().inv_cdf(prob))
        return res if math.isfinite(res) else float("nan")
    except (ValueError, TypeError, OverflowError):
        # exp(1e308) raises OverflowError ("math range error").
        return float("nan")


def lognormdist(x: Any, mean: Any, stdev: Any, c: Any = 1) -> float:
    try:
        x_val = float(x)
        m = float(mean)
        s = float(stdev)
        cum = bool(float(c))
        if s <= 0:
            return float("nan")
        # ScLogNormDist (interpr3.cxx): cumulative of x <= 0 is 0. The
        # density is IllegalArgument. The body is integralPhi((ln x - mue)
        # / sigma) and phi(z) / sigma / x. It does not exp(mue), so a huge
        # mean is a tail probability, not an overflow.
        if x_val <= 0:
            return 0.0 if cum else float("nan")
        z = (math.log(x_val) - m) / s
        if cum:
            res = 0.5 * math.erfc(-z / math.sqrt(2.0))
        else:
            res = math.exp(-0.5 * z * z) / (x_val * s * math.sqrt(2.0 * math.pi))
        return res if math.isfinite(res) else float("nan")
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def _lookup_number(val: Any) -> float | None:
    """Numeric LOOKUP key. A bool is 0 or 1. Text and blanks are not numbers."""
    if isinstance(val, (bool, np.bool_)):
        return float(val)
    if isinstance(val, (int, float, np.integer, np.floating)):
        try:
            number = float(val)
        except (ValueError, TypeError, OverflowError):
            return None
        if math.isnan(number):
            return None
        return number
    return None


def _lookup_keys(data: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Search vector and the default result vector for ``ScLookup``.

    A 1-D list is that vector. A 2-D table searches the first column when
    it has at least as many rows as columns, and the first row otherwise.
    The other end of that line is the result when no result vector is passed.
    ``ravel`` used to search ``[[1, 10], [2, 20]]`` as one list, so
    ``LOOKUP(2, that)`` returned 2 instead of 20.
    """
    if data.size == 0:
        return None
    if data.ndim <= 1:
        keys = data.ravel()
        return keys, keys
    if data.ndim != 2:
        return None
    if data.shape[0] >= data.shape[1]:
        return data[:, 0], data[:, -1]
    return data[0, :], data[-1, :]


def lookup(lookup_val: Any, *args: Any) -> Any:
    # dtype=object keeps numbers as numbers. Text compares case-insensitively.
    # ScLookup (interpr1.cxx) requires data <= query and the same type. A
    # number does not match text. str(None) used to be the text "None", so
    # lookup(20, ["10x"]) returned "10x". A miss is #N/A, not Python None.
    if not args:
        return "#N/A"
    parsed = _lookup_keys(np.asarray(args[0], dtype=object))
    if parsed is None:
        return "#N/A"
    vec, default_result = parsed
    if len(args) == 1:
        result = default_result
    else:
        result_arr = np.asarray(args[1], dtype=object)
        # A result range must be a vector. A block is IllegalParameter.
        if result_arr.ndim > 2 or (
            result_arr.ndim == 2 and result_arr.shape[0] > 1 and result_arr.shape[1] > 1
        ):
            return "#VALUE!"
        result = result_arr.ravel()
    query_num = _lookup_number(lookup_val)
    query_text = lookup_val.casefold() if isinstance(lookup_val, str) and lookup_val != "" else None
    best_idx = None
    for i, value in enumerate(vec):
        if query_num is not None:
            cell = _lookup_number(value)
            if cell is not None and cell <= query_num:
                best_idx = i
        elif query_text is not None and isinstance(value, str) and value != "":
            if value.casefold() <= query_text:
                best_idx = i
    if best_idx is None or best_idx >= len(result):
        return "#N/A"
    return result[best_idx]

def maxa(*args: Any) -> float:
    vals = _collect_a_values(*args)
    if not vals.size:
        return 0.0
    return float(np.max(vals))


def _trim_matrix(matrix: np.ndarray) -> np.ndarray:
    """Keep the first block of a 3-d list. det/inv/matmul used to do this inline."""
    if matrix.ndim > 2:
        return matrix[0]
    return matrix


def _as_matrix(value: Any, *, numeric: bool) -> np.ndarray:
    """2-D array. A scalar is 1×1. A flat list is a row.

    Sheet ranges are already 2-D. ``MMULT`` of two 1-D lists used to be a
    dot product; ``ScMatMult`` rejects 1×n times 1×n. ``ScMatDet`` of a
    1×1 matrix is that element, so ``MDETERM(5)`` is 5.
    """
    matrix = _trim_matrix(np.asarray(value, dtype=float if numeric else None))
    if matrix.ndim == 0:
        return matrix.reshape(1, 1)
    if matrix.ndim == 1:
        return matrix.reshape(1, -1)
    return matrix


def mdeterm(matrix: Any) -> float:
    try:
        return float(np.linalg.det(_as_matrix(matrix, numeric=True)))
    except Exception:
        return float("nan")


def mduration(settlement: Any, maturity: Any, coupon: Any, yld: Any, frequency: Any, basis: Any = 0) -> float:
    from .calc_functions_d_h import duration

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
        # ScMatInv of a singular matrix is IllegalArgument (Err:502), not
        # #NUM!. This helper reports that as #VALUE!. A scalar is 1×1, so
        # MINVERSE(5) is [[0.2]] rather than #VALUE!.
        return np.linalg.inv(_as_matrix(matrix, numeric=True)).tolist()
    except Exception:
        return "#VALUE!"


def mirr(values: Any, finance_rate: Any, reinvest_rate: Any) -> float:
    try:
        vals = [float(x) for x in np.asarray(values).ravel()]
        fr = float(finance_rate)
        rr = float(reinvest_rate)
    except (ValueError, TypeError, OverflowError):
        # float() of a huge int raises OverflowError. The other rate
        # helpers already turn that into NaN. This one let it escape.
        return float("nan")
    # rate == -1 used to divide by zero (later negative flows) or return a
    # finite number (reinvest rate -1). numpy-financial's NPV is undefined
    # there and returns NaN, which is Excel #NUM!.
    return _npf_result("mirr", vals, fr, rr)


def mmult(array1: Any, array2: Any) -> Any:
    try:
        left = _as_matrix(array1, numeric=True)
        right = _as_matrix(array2, numeric=True)
        return np.matmul(left, right).tolist()
    except Exception:
        return "#VALUE!"


def mode(*args: Any) -> Any:
    # ScModalValue (interpr3.cxx) takes one or more arguments and keeps
    # numbers only. mode(1, 2, 2) used to raise TypeError. Text is not a
    # candidate. A Calc bool is a numeric cell, so True and 1 are the same
    # value and must stay merged. dtype=object so a mixed range is not
    # stringified (1 used to come back as text).
    vals: list[float] = []
    for arg in args:
        for value in np.asarray(arg, dtype=object).ravel():
            if isinstance(value, (bool, np.bool_)):
                vals.append(float(value))
                continue
            if isinstance(value, str) or value is None:
                continue
            try:
                number = float(value)
            except (ValueError, TypeError, OverflowError):
                continue
            if math.isnan(number):
                continue
            vals.append(number)
    if not vals:
        return float("nan")
    counts = Counter(vals)
    best = max(counts.values())
    # No repeat is #N/A. na() is float nan, which isna() already treats as #N/A.
    if best < 2:
        return float("nan")
    winners = [value for value, count in counts.items() if count == best]
    # Tie-break is the lowest value, the first run after ScModalValue sorts.
    # mode([2, 2, 1, 1]) was 2 when Counter's first key won.
    return float(min(winners))


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
    # 0 / negative is signed zero, so MROUND(0, -1) was -0.0.
    if n == 0:
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
        m = _trim_matrix(np.asarray(matrix))
        # A scalar stays a scalar. np.transpose of a 1-D array does nothing,
        # so a flat row never became a column. ScMatTrans swaps the axes.
        if m.ndim == 0:
            return m.tolist()
        if m.ndim == 1:
            m = m.reshape(1, -1)
        return np.transpose(m).tolist()
    except Exception:
        return "#VALUE!"


def multinomial(*args: Any) -> float:
    try:
        vals: list[int] = []
        for arg in args:
            for value in np.asarray(arg).ravel():
                vals.append(int(float(value)))
        if any(value < 0 for value in vals):
            return float("nan")
        # What was wrong: multinomial(1e9, 1e9) did not return. It burned CPU
        # and memory until the script timeout.
        # How it happened: math.comb(sum, k) builds a Python int. comb(2e9, 1e9)
        # is hundreds of millions of bits. The switch off math.factorial(sum)
        # only made each factor smaller; it did not bound the factor.
        # getMultinomial (analysis.cxx) multiplies BinomialCoefficient in
        # double and returns finiteOrThrow. A non-finite result is #NUM!,
        # which this module reports as NaN.
        # Why this change fixes it: log-gamma rejects a coefficient that
        # cannot fit in a float before math.comb runs, and the running
        # product stops at the same float limit. k < 2 skips log-gamma:
        # lgamma(1e308) raises OverflowError even when C(n, n) is 1 and
        # C(n, 1) is n.
        acc = 1
        rest = sum(vals)
        limit = math.log(sys.float_info.max)
        for value in vals:
            k = min(value, rest - value)
            if k >= 2:
                try:
                    ln = (
                        math.lgamma(rest + 1)
                        - math.lgamma(k + 1)
                        - math.lgamma(rest - k + 1)
                    )
                except OverflowError:
                    return float("nan")
                if not math.isfinite(ln) or ln > limit:
                    return float("nan")
            acc *= math.comb(rest, value)
            if acc.bit_length() > 1024:
                return float("nan")
            rest -= value
        result = float(acc)
        return result if math.isfinite(result) else float("nan")
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def munit(dimension: Any) -> Any:
    # ScEMat (interpr5.cxx): GetUInt32 truncates, then dimension 0 is
    # IllegalArgument. np.eye(0) used to return []. Above 1024 the identity
    # is more than a million Python floats. Calc pushes MatrixSize when the
    # square is not allocatable instead of building it.
    try:
        size = int(dimension)
    except (ValueError, TypeError, OverflowError):
        return "#VALUE!"
    if size < 1 or size > 1024:
        return "#VALUE!"
    try:
        return np.eye(size).tolist()
    except Exception:
        return "#VALUE!"


def n(val: Any) -> float:
    # N() of any text is 0, including a numeric-looking string such as "5".
    if _is_number(val):
        # What was wrong: n(10**400) raised OverflowError out of the helper.
        # How it happened: float() of a Python int outside the float range
        # raises, and this return was not in a try. ispmt and mirr already
        # catch that and return NaN. A Calc cell is a double and cannot
        # reach this path.
        # Why this change fixes it: the overflow is NaN (#NUM!), not a traceback.
        try:
            return float(val)
        except OverflowError:
            return float("nan")
    if isinstance(val, (bool, np.bool_)):
        return 1.0 if val else 0.0
    return 0.0
