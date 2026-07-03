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



__all__ = ["_coup_days_in_period", "_days_between", "_eval_d_criteria", "_from_complex", "_get_coupon_dates", "_to_complex", "_to_float_a", "_year_frac", "accrint", "accrintm", "acot", "acoth", "address", "aggregate", "amordegrc", "amorlinc", "arabic", "areas", "asc", "avedev", "averagea", "averageif", "averageifs", "bahttext", "base", "besseli", "besselj", "besselk", "bessely", "betadist", "betainv", "binomdist", "bitand", "bitlshift", "bitor", "bitrshift", "bitxor", "char", "chidist", "chiinv", "choose", "clean", "code", "combin", "combina", "complex", "confidence", "cot", "coth", "countif", "countifs", "coupdaybs", "coupdays", "coupdaysnc", "coupncd", "coupnum", "couppcd", "critbinom", "csc", "csch", "cumipmt", "cumprinc"]

def _coup_days_in_period(frequency: Any, basis: Any) -> float:
    f = int(float(frequency))
    b = int(float(basis))
    if b in (0, 2, 4): return 360.0 / f
    if b == 3: return 365.0 / f
    return 365.25 / f
def _days_between(d1: float, d2: float, basis: int) -> float:
    # 0 = US (NASD) 30/360, 1 = Actual/Actual, 2 = Actual/360, 3 = Actual/365, 4 = EUR 30/360
    # For simplicity, we approximate basis 1 with actual days.
    # Proper financial day count is complex, we use actual days for basics.
    if basis == 1 or basis == 2 or basis == 3:
        return d2 - d1
    # 30/360 approx
    try:
        dt1 = datetime.date.fromordinal(int(d1) + 693594)
        dt2 = datetime.date.fromordinal(int(d2) + 693594)
    except (ValueError, TypeError):
        return float("nan")
    return (dt2.year - dt1.year) * 360 + (dt2.month - dt1.month) * 30 + (dt2.day - dt1.day)
def _eval_d_criteria(db: Any, field: Any, criteria: Any, as_float: bool = True) -> list[Any]:
    """Shared helper for D* functions."""
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    db_arr = np.asarray(db, dtype=object)
    if db_arr.ndim != 2:
        return []
    headers = [str(h).upper() for h in db_arr[0]]

    f_idx = -1
    if field is not None and field != "":
        try:
            f_idx = int(float(field)) - 1
        except (ValueError, TypeError):
            f_name = str(field).upper()
            if f_name in headers:
                f_idx = headers.index(f_name)

    if f_idx < 0 or f_idx >= db_arr.shape[1]:
        return []

    crit_arr = np.asarray(criteria)
    if crit_arr.ndim != 2:
        return []
    crit_headers = [str(h).upper() for h in crit_arr[0]]

    matching_vals = []
    for r_idx in range(1, db_arr.shape[0]):
        row = db_arr[r_idx]
        match_any_row = False
        for c_row_idx in range(1, crit_arr.shape[0]):
            match_all_cols = True
            for c_col_idx in range(crit_arr.shape[1]):
                c_header = crit_headers[c_col_idx]
                c_val = crit_arr[c_row_idx, c_col_idx]
                if c_val is None or str(c_val) == "":
                    continue

                if c_header in headers:
                    db_col_idx = headers.index(c_header)
                    if not match_criteria(row[db_col_idx], c_val):
                        match_all_cols = False
                        break
            if match_all_cols:
                match_any_row = True
                break

        if match_any_row:
            val = row[f_idx]
            if as_float:
                try:
                    matching_vals.append(float(val))
                except (ValueError, TypeError):
                    pass
            else:
                matching_vals.append(val)
    return matching_vals
def _from_complex(c: builtins.complex, suffix: str = "i") -> str:
    """Convert Python complex to Calc string."""
    if not isinstance(c, builtins.complex):
        return str(c)
    real = c.real
    imag = c.imag
    if imag == 0:
        return str(real)
    
    res = ""
    if real != 0:
        res += str(real)
        if imag > 0:
            res += "+"
    
    if imag == 1:
        res += suffix
    elif imag == -1:
        res += "-" + suffix
    else:
        res += str(imag) + suffix
    return res
def _get_coupon_dates(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> tuple[float, float, float]:
    from plugin.scripting.venv.calc_functions_a_c import _coup_days_in_period
    from datetime import datetime
    def _to_ordinal(val: Any) -> int:
        if isinstance(val, datetime): return val.toordinal()
        return int(float(val)) + 693594
    mat_ord = _to_ordinal(maturity)
    set_ord = _to_ordinal(settlement)

    # Approx based on frequency days
    days_in_period = _coup_days_in_period(frequency, basis)

    # walk backwards from maturity
    curr_ord = float(mat_ord)
    prev_ord = curr_ord - days_in_period

    while prev_ord > set_ord:
        curr_ord = prev_ord
        prev_ord -= days_in_period

    return prev_ord, curr_ord, days_in_period
def _to_complex(val: Any) -> builtins.complex:
    """Convert Calc complex string (e.g. '1+2i') to Python complex."""
    if isinstance(val, (int, float, builtins.complex)):
        return builtins.complex(val)
    s = str(val).replace("i", "j").replace("I", "j").replace(" ", "")
    try:
        return builtins.complex(s)
    except ValueError:
        raise TypeError("Invalid complex string")
def _to_float_a(val: Any) -> float:
    """Helper for *A functions (AVERAGEA, STDEVA, etc.)."""
    if val is None or val == "":
        return 0.0
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
def _year_frac(d1: float, d2: float, basis: int) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _days_between
    days = _days_between(d1, d2, basis)
    if basis == 2: return days / 360.0
    if basis == 3: return days / 365.0
    if basis == 0 or basis == 4: return days / 360.0
    # basis 1 (Actual/Actual) is complex, approx as days/365.25
    return days / 365.25
def accrint(issue: Any, first_interest: Any, settlement: Any, rate: Any, par: Any, frequency: Any, basis: Any = 0, calc_method: Any = True) -> float:
    from plugin.scripting.venv.calc_functions_t_z import yearfrac
    try:
        r = float(rate)
        p = float(par)
        yf = yearfrac(issue, settlement, basis)
        if math.isnan(yf): return float("nan")
        return float(p * r * yf)
    except Exception:
        return float("nan")
def accrintm(issue: Any, settlement: Any, rate: Any, par: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_t_z import yearfrac
    try:
        r = float(rate)
        p = float(par)
        yf = yearfrac(issue, settlement, basis)
        if math.isnan(yf): return float("nan")
        return float(p * r * yf)
    except Exception:
        return float("nan")
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
    r = int(float(row))
    c = int(float(col))
    abs_n = int(float(abs_num))
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

    if sheet:
        res = f"'{str(sheet)}'!{res}"
    return res

def aggregate(function_num: Any, options: Any, *args: Any) -> float:
    try:
        fn = int(float(function_num))
        opt = int(float(options))
        vals: list[float] = []
        for arg in args:
            vals.extend(np.asarray(arg, dtype=float).ravel().tolist())

        arr = np.array(vals, dtype=float)
        # Handle ignore options
        if opt in (4, 5, 6, 7):
            # Ignore hidden rows (cannot do here), assume same as 0,1,2,3 for now
            pass

        # Strip NaNs if options ignore errors (1, 3, 5, 7)
        if opt in (1, 3, 5, 7):
            arr = arr[~np.isnan(arr)]

        # Simplified implementations for most common
        if fn == 1: return float(np.mean(arr))
        if fn == 2: return float(np.sum(~np.isnan(arr)))
        if fn == 3: return float(len(arr))
        if fn == 4: return float(np.nanmax(arr))
        if fn == 5: return float(np.nanmin(arr))
        if fn == 6: return float(np.prod(arr))
        if fn == 7: return float(np.std(arr, ddof=1))
        if fn == 8: return float(np.std(arr, ddof=0))
        if fn == 9: return float(np.sum(arr))
        if fn == 10: return float(np.var(arr, ddof=1))
        if fn == 11: return float(np.var(arr, ddof=0))
        if fn == 12: return float(np.median(arr))
        return float("nan")
    except Exception:
        return float("nan")
def amordegrc(cost: Any, date_purchased: Any, first_period: Any, salvage: Any, period: Any, rate: Any, basis: Any = 0) -> float:
    try:
        cost_f = float(cost)
        salvage_f = float(salvage)
        per = int(float(period))
        r = float(rate)

        life = 1.0 / r if r > 0 else 0
        if life < 3: coef = 1.0
        elif life < 5: coef = 1.5
        elif life <= 6: coef = 2.0
        else: coef = 2.5

        dep_rate = r * coef
        val = cost_f
        dep = 0.0
        for i in range(per + 1):
            dep = val * dep_rate
            val -= dep
            if val < salvage_f:
                dep += (val - salvage_f)
                val = salvage_f
        return float(dep)
    except Exception:
        return float("nan")
def amorlinc(cost: Any, date_purchased: Any, first_period: Any, salvage: Any, period: Any, rate: Any, basis: Any = 0) -> float:
    try:
        return float(float(cost) * float(rate))
    except Exception:
        return float("nan")

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
                res.append(' ')
            else:
                res.append(ch)
        return "".join(res)
    except Exception:
        return "#VALUE!"

def avedev(r: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    if not arr.size:
        return float("nan")
    return float(np.mean(np.abs(arr - np.mean(arr))))

def averagea(r: Any) -> float:
    vals = []
    for x in np.asarray(r).ravel():
        if x is None or x == "":
            vals.append(0.0)
        else:
            try:
                vals.append(float(x))
            except (ValueError, TypeError):
                vals.append(0.0)
    if not vals:
        return float("nan")
    return float(np.mean(vals))
def averageif(r: Any, crit: Any, ar: Any | None = None) -> float:
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    r_flat = np.asarray(r).ravel()
    ar_flat = np.asarray(ar).ravel() if ar is not None else r_flat
    vals = []
    for i in range(min(len(r_flat), len(ar_flat))):
        if match_criteria(r_flat[i], crit):
            try:
                val = float(ar_flat[i])
                if not np.isnan(val):
                    vals.append(val)
            except (ValueError, TypeError):
                pass
    if not vals:
        return float("nan")
    return float(np.mean(vals))
def averageifs(ar: Any, *args: Any) -> float:
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    ar_flat = np.asarray(ar).ravel()
    cond_ranges = []
    criteria = []
    for i in range(0, len(args), 2):
        cond_ranges.append(np.asarray(args[i]).ravel())
        criteria.append(args[i + 1])
    vals = []
    for idx in range(len(ar_flat)):
        match = True
        for cr, crit in zip(cond_ranges, criteria):
            if idx >= len(cr) or not match_criteria(cr[idx], crit):
                match = False
                break
        if match:
            try:
                val = float(ar_flat[idx])
                if not np.isnan(val):
                    vals.append(val)
            except (ValueError, TypeError):
                pass
    if not vals:
        return float("nan")
    return float(np.mean(vals))
def bahttext(number: Any) -> str | float:
    try:
        val = float(number)
        if math.isnan(val):
            return float("nan")
        return str(val) + " Baht"  # Simplified placeholder
    except (ValueError, TypeError):
        return float("nan")
def base(number: Any, radix: Any, min_length: Any = 0) -> str:
    try:
        n = int(float(number))
        r = int(float(radix))
        m = int(float(min_length))
        if n < 0 or r < 2 or r > 36 or m < 0:
            return "NaN"
        if n == 0:
            return "0".zfill(m)
        digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = ""
        while n > 0:
            res = digits[n % r] + res
            n //= r
        return res.zfill(m)
    except Exception:
        return "NaN"

def besseli(x: Any, n: Any) -> float:
    try:
        import scipy.special  # type: ignore[import-untyped]
        v1 = float(x)
        v2 = int(float(n))
        if v2 < 0:
            return float("nan")
        return float(scipy.special.iv(v2, v1))
    except Exception:
        return float("nan")

def besselj(x: Any, n: Any) -> float:
    try:
        import scipy.special
        v1 = float(x)
        v2 = int(float(n))
        if v2 < 0:
            return float("nan")
        return float(scipy.special.jv(v2, v1))
    except Exception:
        return float("nan")

def besselk(x: Any, n: Any) -> float:
    try:
        from scipy.special import kn
        xv = float(x)
        nv = int(float(n))
        if xv <= 0:
            return float("nan")
        return float(kn(nv, xv))
    except Exception:
        return float("nan")

def bessely(x: Any, n: Any) -> float:
    try:
        from scipy.special import yn
        xv = float(x)
        nv = int(float(n))
        if xv <= 0:
            return float("nan")
        return float(yn(nv, xv))
    except Exception:
        return float("nan")

def betadist(*args: Any) -> float:
    try:
        from scipy import stats
        x = float(args[0])
        alpha = float(args[1])
        beta = float(args[2])
        cum = True
        if len(args) > 3:
            cum = bool(args[3])
        A = 0.0
        if len(args) > 4:
            A = float(args[4])
        B = 1.0
        if len(args) > 5:
            B = float(args[5])

        x_norm = (x - A) / (B - A)
        if cum:
            return float(stats.beta.cdf(x_norm, alpha, beta))
        else:
            return float(stats.beta.pdf(x_norm, alpha, beta) / (B - A))
    except Exception:
        return float("nan")

def betainv(*args: Any) -> float:
    try:
        from scipy import stats
        p = float(args[0])
        alpha = float(args[1])
        beta = float(args[2])
        A = 0.0
        if len(args) > 3:
            A = float(args[3])
        B = 1.0
        if len(args) > 4:
            B = float(args[4])

        return float(stats.beta.ppf(p, alpha, beta) * (B - A) + A)
    except Exception:
        return float("nan")

def binomdist(*args: Any) -> float:
    try:
        from scipy import stats
        k = int(args[0])
        n = int(args[1])
        p = float(args[2])
        cum = bool(args[3])
        if cum:
            return float(stats.binom.cdf(k, n, p))
        else:
            return float(stats.binom.pmf(k, n, p))
    except Exception:
        return float("nan")

def bitand(n1: Any, n2: Any) -> float:
    try:
        return float(int(float(n1)) & int(float(n2)))
    except (ValueError, TypeError):
        return float("nan")
def bitlshift(number: Any, shift: Any) -> float:
    try:
        n = int(float(number))
        s = int(float(shift))
        if s < 0:
            return float(n >> abs(s))
        return float(n << s)
    except (ValueError, TypeError):
        return float("nan")
def bitor(n1: Any, n2: Any) -> float:
    try:
        return float(int(float(n1)) | int(float(n2)))
    except (ValueError, TypeError):
        return float("nan")
def bitrshift(number: Any, shift: Any) -> float:
    try:
        n = int(float(number))
        s = int(float(shift))
        if s < 0:
            return float(n << abs(s))
        return float(n >> s)
    except (ValueError, TypeError):
        return float("nan")
def bitxor(n1: Any, n2: Any) -> float:
    try:
        return float(int(float(n1)) ^ int(float(n2)))
    except (ValueError, TypeError):
        return float("nan")
def char(n: Any) -> str:
    try:
        return chr(int(float(n)))
    except (ValueError, TypeError):
        return ""
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
    try:
        idx = int(float(index))
        if 1 <= idx <= len(args):
            return args[idx - 1]
    except (ValueError, TypeError):
        pass
    return None
def clean(text: Any) -> str | float:
    try:
        if text is None:
            return ""
        if isinstance(text, float) and math.isnan(text):
            return float("nan")
        s = str(text)
        return "".join(c for c in s if ord(c) >= 32)
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
    except (ValueError, TypeError):
        return float("nan")
def combina(n: Any, k: Any) -> float:
    try:
        ni = int(float(n))
        ki = int(float(k))
        if ni == 0 and ki == 0:
            return 1.0
        return float(math.comb(ni + ki - 1, ki))
    except (ValueError, TypeError):
        return float("nan")
def complex(real_num: Any, imag_num: Any, suffix: Any = "i") -> str:
    from plugin.scripting.venv.calc_functions_a_c import _from_complex
    try:
        import builtins
        r = float(real_num)
        i = float(imag_num)
        s = str(suffix)
        return _from_complex(builtins.complex(r, i), suffix=s)
    except (ValueError, TypeError):
        return "#VALUE!"
def confidence(alpha: Any, stddev: Any, size: Any) -> float:
    try:
        from scipy import stats
        import math
        return float(stats.norm.ppf(1 - float(alpha)/2) * float(stddev) / math.sqrt(float(size)))
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
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    r_flat = np.asarray(r).ravel()
    cnt = 0
    for val in r_flat:
        if match_criteria(val, crit):
            cnt += 1
    return float(cnt)
def countifs(*args: Any) -> float:
    from plugin.scripting.venv.calc_functions_i_m import match_criteria
    cond_ranges = []
    criteria = []
    for i in range(0, len(args), 2):
        cond_ranges.append(np.asarray(args[i]).ravel())
        criteria.append(args[i + 1])
    if not cond_ranges:
        return 0.0
    min_len = min(len(cr) for cr in cond_ranges)
    cnt = 0
    for idx in range(min_len):
        match = True
        for cr, crit in zip(cond_ranges, criteria):
            if not match_criteria(cr[idx], crit):
                match = False
                break
        if match:
            cnt += 1
    return float(cnt)
def coupdaybs(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _get_coupon_dates
    try:
        prev_ord, curr_ord, days_in_period = _get_coupon_dates(settlement, maturity, frequency, basis)
        from datetime import datetime
        def _to_ordinal(val: Any) -> int:
            if isinstance(val, datetime): return val.toordinal()
            return int(float(val)) + 693594
        set_ord = _to_ordinal(settlement)
        # return days from beginning of period to settlement
        return float(set_ord - prev_ord)
    except Exception:
        return float("nan")
def coupdays(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _coup_days_in_period
    try:
        return float(_coup_days_in_period(frequency, basis))
    except Exception:
        return float("nan")
def coupdaysnc(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _get_coupon_dates
    try:
        prev_ord, curr_ord, days_in_period = _get_coupon_dates(settlement, maturity, frequency, basis)
        from datetime import datetime
        def _to_ordinal(val: Any) -> int:
            if isinstance(val, datetime): return val.toordinal()
            return int(float(val)) + 693594
        set_ord = _to_ordinal(settlement)
        # return days from settlement to next coupon date
        return float(curr_ord - set_ord)
    except Exception:
        return float("nan")
def coupncd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _get_coupon_dates
    try:
        prev_ord, curr_ord, days_in_period = _get_coupon_dates(settlement, maturity, frequency, basis)
        # next coupon date
        return float(curr_ord - 693594)
    except Exception:
        return float("nan")
def coupnum(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_t_z import yearfrac
    try:
        yf = yearfrac(settlement, maturity, basis)
        f = int(float(frequency))
        return float(math.ceil(yf * f))
    except Exception:
        return float("nan")
def couppcd(settlement: Any, maturity: Any, frequency: Any, basis: Any = 0) -> float:
    from plugin.scripting.venv.calc_functions_a_c import _get_coupon_dates
    try:
        prev_ord, curr_ord, days_in_period = _get_coupon_dates(settlement, maturity, frequency, basis)
        # prev coupon date
        return float(prev_ord - 693594)
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
    try:
        return float(1.0 / math.sinh(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")
def cumipmt(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    try:
        r = float(rate)
        n = float(nper)
        p = float(pv)
        s = int(float(start_period))
        e = int(float(end_period))
        t = int(float(type_val))

        if r == 0:
            pmt_amt = -p / n
        else:
            factor = (1 + r) ** n
            if t == 1:
                pmt_amt = -(p * factor) * r / (factor - 1) / (1 + r)
            else:
                pmt_amt = -(p * factor) * r / (factor - 1)

        tot_i = 0.0
        rem_p = p
        for i in range(1, e + 1):
            if t == 1 and i == 1:
                ipmt = 0.0
            else:
                ipmt = rem_p * r
            ppmt = pmt_amt - (-ipmt)
            if s <= i <= e:
                tot_i += -ipmt
            rem_p -= -ppmt
        return float(tot_i)
    except Exception:
        return float("nan")
def cumprinc(rate: Any, nper: Any, pv: Any, start_period: Any, end_period: Any, type_val: Any) -> float:
    try:
        r = float(rate)
        n = float(nper)
        p = float(pv)
        s = int(float(start_period))
        e = int(float(end_period))
        t = int(float(type_val))

        if r == 0:
            pmt_amt = -p / n
        else:
            factor = (1 + r) ** n
            if t == 1:
                pmt_amt = -(p * factor) * r / (factor - 1) / (1 + r)
            else:
                pmt_amt = -(p * factor) * r / (factor - 1)

        tot_p = 0.0
        rem_p = p
        for i in range(1, e + 1):
            if t == 1 and i == 1:
                ipmt = 0.0
            else:
                ipmt = rem_p * r
            ppmt = pmt_amt - (-ipmt)
            if s <= i <= e:
                tot_p += ppmt
            rem_p -= -ppmt

        return float(tot_p)
    except Exception:
        return float("nan")
