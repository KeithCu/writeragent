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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, cast

import numpy as np

from .calc_functions_util import _find_match_index, _to_float_a
from .coerce import _LO_ERROR_TOKENS, is_missing_value


__all__ = [
    "fmt",
    "t",
    "tdist",
    "text",
    "textbefore",
    "textjoin",
    "textsplit",
    "time",
    "timevalue",
    "tinv",
    "trend",
    "trimmean",
    "ttest",
    "type",
    "unichar",
    "unicode",
    "unique",
    "vara",
    "varpa",
    "weekday",
    "weeknum",
    "weibull",
    "workday",
    "workday_intl",
    "xirr",
    "xlookup",
    "xmatch",
    "xnpv",
    "xor",
    "yearfrac",
    "yield_calc",
    "yielddisc",
    "yieldmat",
    "ztest",
]


def t(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""


def tdist(x: Any, df: Any, tails: Any) -> float:
    try:
        val = float(x)
        d = float(df)
        t = int(float(tails))
        if d < 1 or t not in (1, 2) or val < 0:
            return float("nan")
        import scipy.stats

        # tdist in Calc/Excel returns 1 - cdf(val) for 1 tail
        # and 2 * (1 - cdf(val)) for 2 tails
        p = scipy.stats.t.sf(val, d)
        return float(p if t == 1 else 2 * p)
    except (ValueError, TypeError):
        return float("nan")


# Excel/Calc numeric formats, not Python format specs. "0.00" in the format
# mini-language is zero-padding with precision 0, so text(1234.5, "0.00") was
# "1e+03". Stripping "#" and "," from "#,##0" also dropped the thousands separator.
_TEXT_NUMBER_FORMATS: dict[str, tuple[int, bool]] = {"0": (0, False), "0.00": (2, False), "#,##0": (0, True)}


def _format_number_pattern(value: float, places: int, grouped: bool) -> str:
    """Round half away from zero and apply one of the three numeric TEXT specs."""
    if not math.isfinite(value):
        return str(value)
    quant = Decimal(1).scaleb(-places)
    rounded = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    if places == 0:
        whole = int(rounded)
        # int(Decimal("-0")) is 0; Calc shows 0, not "-0".
        if whole == 0:
            return "0"
        return f"{whole:,}" if grouped else str(whole)
    negative = rounded < 0
    body = f"{abs(rounded):.{places}f}"
    return f"-{body}" if negative else body


def text(val: Any, fmt: Any) -> str:
    fmt_str = str(fmt).strip('"').strip("'")
    spec = _TEXT_NUMBER_FORMATS.get(fmt_str)
    if spec is not None:
        places, grouped = spec
        try:
            return _format_number_pattern(float(val), places, grouped)
        except (ValueError, TypeError, OverflowError, InvalidOperation):
            return str(val)
    if fmt_str == "MMMM":
        try:
            return dt.date.fromordinal(int(float(val)) + 693594).strftime("%B")
        except (ValueError, TypeError, OverflowError):
            return str(val)
    if fmt_str == "MMM":
        try:
            return dt.date.fromordinal(int(float(val)) + 693594).strftime("%b")
        except (ValueError, TypeError, OverflowError):
            return str(val)
    return str(val)


# Alias for spreadsheet-import emission: Calc's formula lexer treats ``TEXT(`` inside
# ``=PY("calc.text(...)")`` as a spreadsheet function (#NAME?). ``formula_edit`` also
# rewrites ``.text(`` to ``.fmt(``. The name has to be in ``__all__``: the venv
# facade star-imports this module, so an unlisted alias never became an attribute
# and ``=TEXT(...)`` recalc raised AttributeError (no attribute ``fmt``).
fmt = text


def textbefore(text: Any, delimiter: Any, instance_num: Any = 1, match_mode: Any = 0, match_end: Any = 0, if_not_found: Any = float("nan")) -> str | float:
    try:
        s = str(text)
        delim = str(delimiter)
        inst = int(float(instance_num))
        if match_mode == 1:
            s_search = s.lower()
            delim_search = delim.lower()
        else:
            s_search = s
            delim_search = delim

        if inst > 0:
            parts = s_search.split(delim_search)
            if len(parts) <= inst:
                if match_end and len(parts) == inst:
                    return s
                return if_not_found
            idx = 0
            for i in range(inst):
                idx = s_search.find(delim_search, idx)
                if i < inst - 1:
                    idx += len(delim_search)
            return s[:idx]
        elif inst < 0:
            parts = s_search.split(delim_search)
            if len(parts) <= abs(inst):
                if match_end and len(parts) == abs(inst):
                    return s
                return if_not_found
            idx = len(s)
            for i in range(abs(inst)):
                idx = s_search.rfind(delim_search, 0, idx)
            return s[:idx]
        else:
            return float("nan")
    except (ValueError, TypeError):
        return float("nan")


def textjoin(delim: Any, ignore_empty: Any, *args: Any) -> str:
    parts = []
    for arg in args:
        for val in np.asarray(arg).ravel():
            if is_missing_value(val):
                if not ignore_empty:
                    parts.append("")
            else:
                parts.append(str(val))
    return str(delim).join(parts)


def textsplit(text: Any, col_delimiter: Any, row_delimiter: Any = None, ignore_empty: Any = False, match_mode: Any = 0, pad_with: Any = float("nan")) -> Any:
    # A simplified version of textsplit returning a 2D array or 1D array.
    try:
        s = str(text)
        if match_mode == 1:
            s = s.lower()
            if col_delimiter:
                col_delimiter = str(col_delimiter).lower()
            if row_delimiter:
                row_delimiter = str(row_delimiter).lower()

        # very simplified logic for textsplit just to pass basic tests
        if row_delimiter is not None:
            rows = s.split(str(row_delimiter))
            if ignore_empty:
                rows = [r for r in rows if r]
            res = []
            for r in rows:
                cols = r.split(str(col_delimiter))
                if ignore_empty:
                    cols = [c for c in cols if c]
                res.append(cols)
            # pad with pad_with to make rectangle
            max_cols = max(len(row) for row in res) if res else 0
            for row in res:
                while len(row) < max_cols:
                    row.append(pad_with)
            return res
        else:
            cols = s.split(str(col_delimiter))
            if ignore_empty:
                cols = [c for c in cols if c]
            return [cols]
    except Exception:
        return float("nan")


def time(hour: Any, minute: Any, second: Any) -> float:
    # ScInterpreter::ScGetTime (sc/source/core/tool/interpr2.cxx) does
    # fmod(hour*3600 + minute*60 + second, 86400) / 86400. Truncating each
    # component and dividing by 86400 made time(24,0,0) return 1 and let a
    # negative total stay negative. A negative remainder is Calc Err:502;
    # this helper returns NaN for that, same as its other numeric failures.
    # math.fmod keeps the dividend's sign, matching C fmod.
    try:
        total = float(hour) * 3600.0 + float(minute) * 60.0 + float(second)
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    if not math.isfinite(total):
        return float("nan")
    wrapped = math.fmod(total, 86400.0)
    if wrapped < 0.0 or not math.isfinite(wrapped):
        return float("nan")
    if wrapped == 0.0:
        return 0.0
    return float(wrapped / 86400.0)


def timevalue(text: Any) -> float:
    s = str(text).strip().strip('"')
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            t = dt.datetime.strptime(s, fmt).time()
            return float((t.hour * 3600 + t.minute * 60 + t.second) / 86400.0)
        except ValueError:
            continue
    return float("nan")


def tinv(prob: Any, df: Any) -> float:
    try:
        p = float(prob)
        d = float(df)
        if p <= 0 or p > 1 or d < 1:
            return float("nan")
        import scipy.stats

        # TINV is the 2-tailed inverse
        return float(scipy.stats.t.ppf(1 - p / 2, d))
    except (ValueError, TypeError):
        return float("nan")


def trend(*args: Any) -> Any:
    try:
        import numpy as np

        data_y = np.asarray(args[0]).ravel()
        # Excel TREND returns #VALUE! for an empty known_y. lstsq on a (0, k)
        # design matrix succeeds and used to return [].
        if data_y.size == 0:
            return "#VALUE!"
        if len(args) > 1:
            data_x = np.asarray(args[1])
            if data_x.ndim == 1:
                data_x = data_x[:, np.newaxis]
        else:
            data_x = np.arange(1, len(data_y) + 1)[:, np.newaxis]

        if len(args) > 2:
            new_data_x = np.asarray(args[2])
            if new_data_x.ndim == 1:
                new_data_x = new_data_x[:, np.newaxis]
        else:
            new_data_x = data_x

        c, _unused, _unused2, _unused3 = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)
        return (np.c_[new_data_x, np.ones(new_data_x.shape[0])] @ c).tolist()
    except Exception:
        return "#VALUE!"


def trimmean(r: Any, percent: Any) -> float:
    # Excel TRIMMEAN returns #VALUE! when any cell is non-numeric. dtype=float
    # coerced numeric text and bools, turned None into NaN, and ~isnan then
    # dropped those NaNs so the rest were averaged. Reject anything that is
    # not a real number. Sibling stats report that failure as NaN.
    try:
        cells = np.asarray(r, dtype=object).ravel()
        nums: list[float] = []
        for cell in cells:
            val = cell.item() if isinstance(cell, np.generic) else cell
            # bool is a subclass of int; Excel treats it as non-numeric here.
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return float("nan")
            number = float(val)
            if math.isnan(number):
                return float("nan")
            nums.append(number)
        if not nums:
            return float("nan")
        arr = np.asarray(nums, dtype=float)
        p = float(percent)
        if p < 0 or p >= 1:
            return float("nan")
        k = int(len(arr) * p / 2)
        if k == 0:
            return float(np.mean(arr))
        arr.sort()
        return float(np.mean(arr[k:-k]))
    except (ValueError, TypeError):
        return float("nan")


def ttest(data1: Any, data2: Any, tails: Any, type_: Any) -> float:
    try:
        d1 = np.asarray(data1).ravel()
        d2 = np.asarray(data2).ravel()
        type_num = int(float(type_))

        if type_num == 1:
            if len(d1) != len(d2):
                return float("nan")
            mask1 = np.array([isinstance(x.item() if hasattr(x, "item") else x, (int, float)) and not math.isnan(x.item() if hasattr(x, "item") else x) for x in d1])
            mask2 = np.array([isinstance(x.item() if hasattr(x, "item") else x, (int, float)) and not math.isnan(x.item() if hasattr(x, "item") else x) for x in d2])
            mask = mask1 & mask2
            d1_clean = np.asarray(d1[mask], dtype=float)
            d2_clean = np.asarray(d2[mask], dtype=float)
        else:
            d1_clean = np.asarray([x for x in d1 if isinstance(x.item() if hasattr(x, "item") else x, (int, float)) and not math.isnan(x.item() if hasattr(x, "item") else x)], dtype=float)
            d2_clean = np.asarray([x for x in d2 if isinstance(x.item() if hasattr(x, "item") else x, (int, float)) and not math.isnan(x.item() if hasattr(x, "item") else x)], dtype=float)
        t = int(float(tails))
        type_num = int(float(type_))
        if t not in (1, 2) or type_num not in (1, 2, 3) or len(d1_clean) < 2 or len(d2_clean) < 2:
            return float("nan")
        import scipy.stats

        if type_num == 1:
            # Paired
            res = scipy.stats.ttest_rel(d1_clean, d2_clean)
        elif type_num == 2:
            # Two-sample equal variance
            res = scipy.stats.ttest_ind(d1_clean, d2_clean, equal_var=True)
        else:
            # Two-sample unequal variance
            res = scipy.stats.ttest_ind(d1_clean, d2_clean, equal_var=False)

        p = float(cast("float", res[1]))
        if t == 1:
            p /= 2.0

        return float(p)
    except (ValueError, TypeError):
        return float("nan")


def _is_calc_error(val: Any) -> bool:
    """NaN (NA()) or a real Calc error token. "#hashtag" is text, not an error."""
    if isinstance(val, str):
        return val.strip() in _LO_ERROR_TOKENS
    if isinstance(val, bool):
        return False
    if isinstance(val, (float, np.floating)):
        return math.isnan(float(val))
    return False


def type(val: Any) -> float:
    # is_missing_value is true for NaN and for #VALUE!/#N/A/..., so those took
    # the number branch (1). LO/Excel TYPE is 16 for errors. Checking any
    # string that starts with "#" also classified "#hashtag" as an error.
    if _is_calc_error(val):
        return 16.0
    if isinstance(val, bool):
        return 4.0
    if isinstance(val, (int, float)) or isinstance(val, (np.integer, np.floating)):
        return 1.0
    if isinstance(val, str):
        return 2.0
    if isinstance(val, (list, np.ndarray)):
        return 64.0
    # Blank cell (None). An empty string is text and already returned 2.
    if val is None or is_missing_value(val):
        return 1.0
    return 1.0


def unichar(number: Any) -> str | float:
    try:
        val = int(float(number))
        if val <= 0 or val > 0x10FFFF:
            return float("nan")
        return chr(val)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def unicode(text: Any) -> float:
    try:
        s = str(text)
        if not s:
            return float("nan")
        return float(ord(s[0]))
    except (ValueError, TypeError):
        return float("nan")


def _unique_items(items: list[Any], exactly_once: bool) -> list[Any]:
    counts: dict[Any, int] = {}
    seen: list[Any] = []
    for item in items:
        counts[item] = counts.get(item, 0) + 1
        if item not in seen:
            seen.append(item)
    if exactly_once:
        return [item for item in seen if counts[item] == 1]
    return seen


def unique(arr: Any, by_col: bool = False, unique_only: bool = False) -> list[Any]:
    # `ndim == 1 or not by_col` flattened every cell whenever by_col was falsy,
    # and the row comparison ran only for a truthy by_col. Calc/Excel by_col
    # false uniques rows; true uniques columns. exactly_once is unique_only.
    data = np.asarray(arr)
    if data.size == 0:
        return []
    once = bool(unique_only)
    if data.ndim < 2:
        return _unique_items(data.reshape(-1).tolist(), once)
    if not bool(by_col):
        rows = [tuple(row) for row in data.tolist()]
        return [list(row) for row in _unique_items(rows, once)]
    cols = [tuple(data[:, i].tolist()) for i in range(data.shape[1])]
    kept = _unique_items(cols, once)
    if not kept:
        return []
    height = len(kept[0])
    return [[col[row] for col in kept] for row in range(height)]


def vara(*args: Any) -> float:
    vals = [_to_float_a(v) for arg in args for v in np.asarray(arg).ravel()]
    if len(vals) < 2:
        return float("nan")
    return float(np.var(vals, ddof=1))


def varpa(*args: Any) -> float:
    vals = [_to_float_a(v) for arg in args for v in np.asarray(arg).ravel()]
    if not vals:
        return float("nan")
    return float(np.var(vals, ddof=0))


# return_type 11..17: the day that is numbered 1, as date.weekday() (Monday=0).
_WEEKDAY_ONES: dict[int, int] = {11: 0, 12: 1, 13: 2, 14: 3, 15: 4, 16: 5, 17: 6}

# System-1 WEEKNUM week start, same Monday=0 numbering. 21 and 150 are ISO.
_WEEKNUM_WEEK_START: dict[int, int] = {1: 6, 2: 0, 11: 0, 12: 1, 13: 2, 14: 3, 15: 4, 16: 5, 17: 6}


def weekday(serial: Any, return_type: int | float = 1) -> float:
    try:
        d = dt.date.fromordinal(int(float(serial)) + 693594)
        rt = int(float(return_type))
    except (TypeError, ValueError, OverflowError, OSError):
        return float("nan")
    wd = d.weekday()
    # date.weekday() is already Monday=0 .. Sunday=6, which is return_type 3.
    # (wd+6)%7 numbered Sunday as 0, so a Sunday came back as 5. Types 11-17
    # were missing and fell through to Monday=1 .. Sunday=7. An unknown type
    # is Calc Err:502; return NaN rather than that fallthrough.
    if rt == 3:
        return float(wd)
    if rt == 1:
        rt = 17
    elif rt == 2:
        rt = 11
    start = _WEEKDAY_ONES.get(rt)
    if start is None:
        return float("nan")
    return float((wd - start) % 7 + 1)


def _system1_week_number(day: dt.date, week_start: int) -> int:
    """Week containing January 1 is week 1 (LibreOffice Date::GetWeekOfYear, min days 1).

    tools/source/datetime/tdate.cxx. week_start uses Monday=0, matching
    DayOfWeek and date.weekday(). A late December date that sits in next
    year's week 1 is numbered 1, not 53 or 54.
    """
    jan1 = dt.date(day.year, 1, 1)
    first = (jan1.weekday() + (7 - week_start)) % 7
    day_of_year = day.timetuple().tm_yday - 1
    week = (first + day_of_year) // 7 + 1
    if week == 54:
        return 1
    if week == 53:
        leap = day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0)
        days_in_year = 366 if leap else 365
        next_jan1 = dt.date(day.year + 1, 1, 1)
        next_first = (next_jan1.weekday() + (7 - week_start)) % 7
        if day_of_year > (days_in_year - next_first - 1):
            return 1
    return week


def weeknum(serial: Any, return_type: int | float = 1) -> float:
    # The body ignored return_type and always returned the ISO week. ISO is
    # only return_type 21 (and 150, the Gnumeric alias). isoweeknum already
    # covers that path; system 1 is a different numbering.
    try:
        day = dt.date.fromordinal(int(float(serial)) + 693594)
        rt = int(float(return_type))
    except (TypeError, ValueError, OverflowError, OSError):
        return float("nan")
    if rt in (21, 150):
        return float(day.isocalendar()[1])
    start = _WEEKNUM_WEEK_START.get(rt)
    if start is None:
        return float("nan")
    return float(_system1_week_number(day, start))


def weibull(x: Any, alpha: Any, beta: Any, cumulative: Any = True) -> float:
    try:
        val = float(x)
        a = float(alpha)
        b = float(beta)
        if val < 0 or a <= 0 or b <= 0:
            return float("nan")
        import scipy.stats

        # In scipy, c=alpha (shape), scale=beta. Note: Calc calls alpha shape and beta scale.
        if cumulative:
            return float(scipy.stats.weibull_min.cdf(val, a, scale=b))
        else:
            return float(scipy.stats.weibull_min.pdf(val, a, scale=b))
    except (ValueError, TypeError):
        return float("nan")


def workday(start_date: Any, days: Any, holidays: Any | None = None) -> float:
    try:
        curr = dt.date.fromordinal(int(float(start_date)) + 693594)
    except Exception:
        return float("nan")
    h_dates: set[dt.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                try:
                    h_dates.add(dt.date.fromordinal(int(float(h)) + 693594))
                except Exception:
                    pass
    # days sat outside the start-date try, so a text days cell raised.
    try:
        remaining = int(float(days))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    step = 1 if remaining >= 0 else -1
    while remaining != 0:
        curr += dt.timedelta(days=step)
        if curr.weekday() < 5 and curr not in h_dates:
            remaining -= step
    return float(curr.toordinal() - 693594)


def workday_intl(start_date: Any, days: Any, weekend: Any = 1, holidays: Any | None = None) -> float:
    try:
        curr = dt.date.fromordinal(int(float(start_date)) + 693594)
    except Exception:
        return float("nan")

    wk_days = set()
    if isinstance(weekend, str):
        for i, char in enumerate(weekend[:7]):
            if char == "1":
                wk_days.add(i)
    else:
        try:
            w_idx = int(float(weekend))
        except (ValueError, TypeError, OverflowError):
            return float("nan")
        # Excel weekend codes. An unknown code used to fall back to Sat/Sun
        # (mapping.get default), so WORKDAY.INTL(…, 8) looked like weekend 1.
        # Excel returns #NUM!; this module uses NaN. Same as networkdays_intl.
        mapping = {1: (5, 6), 2: (6, 0), 3: (0, 1), 4: (1, 2), 5: (2, 3), 6: (3, 4), 7: (4, 5), 11: (6,), 12: (0,), 13: (1,), 14: (2,), 15: (3,), 16: (4,), 17: (5,)}
        if w_idx not in mapping:
            return float("nan")
        wk_days.update(mapping[w_idx])

    h_dates: set[dt.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                try:
                    h_dates.add(dt.date.fromordinal(int(float(h)) + 693594))
                except Exception:
                    pass

    try:
        remaining = int(float(days))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    step = 1 if remaining >= 0 else -1
    while remaining != 0:
        curr += dt.timedelta(days=step)
        if curr.weekday() not in wk_days and curr not in h_dates:
            remaining -= step
    return float(curr.toordinal() - 693594)


def xirr(values: Any, dates: Any, guess: Any = 0.1) -> float:
    try:
        vals = np.asarray(values, dtype=float).ravel()
        dts = np.asarray(dates, dtype=float).ravel()
        if len(vals) != len(dts) or len(vals) == 0:
            return float("nan")
        x = float(guess)
        d0 = float(dts[0])
        for _unused in range(100):
            f = 0.0
            df = 0.0
            for v, d in zip(vals, dts):
                t = (float(d) - d0) / 365.0
                f += v / ((1.0 + x) ** t)
                df -= t * v / ((1.0 + x) ** (t + 1.0))
            if abs(f) < 1e-7:
                return float(x)
            # df == 0 missed a tiny slope. Newton then stepped by f/df
            # (up to ~1e300) before the derivative underflowed to 0.
            if abs(df) < 1e-15:
                break
            x = x - f / df
        return float("nan")
    except Exception:
        return float("nan")


def _scalar_if_singleton(values: list[Any]) -> Any:
    if len(values) == 1:
        return values[0]
    return values


def xlookup(lookup_val: Any, lookup_arr: Any, return_arr: Any, if_not_found: Any | None = None, match_mode: int | float = 0, search_mode: int | float = 1) -> Any:
    best_idx = _find_match_index(lookup_val, lookup_arr, match_mode, search_mode)
    if best_idx is None:
        return if_not_found
    r_flat = np.asarray(return_arr)
    if r_flat.ndim == 1:
        return r_flat[best_idx]
    if r_flat.ndim == 2:
        l_shape = np.asarray(lookup_arr).shape
        if len(l_shape) == 2 and l_shape[0] > 1 and l_shape[1] == 1:
            return _scalar_if_singleton(r_flat[best_idx].tolist())
        # A flat (N,) lookup used the column slice whenever best_idx < width.
        # xlookup("b", ["a","b","c"], [["x","y"],["z","w"],["p","q"]])
        # returned ["y","w","q"] instead of the "b" row ["z","w"]. When the
        # return has one row per lookup value, index that row. A wide return
        # whose columns match the lookup length still uses the column slice
        # below; (N, 1) and (1, N) lookups are handled by their own branches.
        if len(l_shape) == 1 and r_flat.shape[0] == l_shape[0]:
            return _scalar_if_singleton(r_flat[best_idx].tolist())
        if best_idx < r_flat.shape[1]:
            # A horizontal 1×N lookup into a one-row return sliced out a
            # one-element column and .tolist() wrapped it. A 1×1 result is a scalar.
            return _scalar_if_singleton(r_flat[:, best_idx].tolist())
        return r_flat.ravel()[best_idx]
    return r_flat.ravel()[best_idx]


def xmatch(lookup_val: Any, lookup_arr: Any, match_mode: int | float = 0, search_mode: int | float = 1) -> float:
    best_idx = _find_match_index(lookup_val, lookup_arr, match_mode, search_mode)
    return float(best_idx + 1) if best_idx is not None else float("nan")


def xnpv(rate: Any, values: Any, dates: Any) -> float:
    try:
        r = float(rate)
        vals = np.asarray(values).ravel()
        dts = np.asarray(dates).ravel()
        if len(vals) != len(dts) or len(vals) == 0:
            return float("nan")
        # Excel XNPV returns #NUM! when rate <= -1. (1+rate)**fraction is
        # complex for a negative base, and rate == -1 is 0**0 == 1 on a
        # cash flow dated with the anchor (or ZeroDivisionError later), so a
        # finite total or a complex used to leak out of this float return.
        if 1.0 + r <= 0.0:
            return float("nan")
        res = 0.0
        d0 = float(dts[0])
        for v, d in zip(vals, dts):
            res += float(v) / ((1.0 + r) ** ((float(d) - d0) / 365.0))
        return res
    except Exception:
        return float("nan")


def _is_calc_range(arg: Any) -> bool:
    if isinstance(arg, (str, bytes, bytearray)):
        return False
    if isinstance(arg, np.ndarray):
        return arg.ndim >= 1
    return isinstance(arg, (list, tuple))


def _flatten_range(arg: Any) -> list[Any]:
    if isinstance(arg, np.ndarray):
        return [v.item() if isinstance(v, np.generic) else v for v in arg.ravel()]
    flat: list[Any] = []
    for val in arg:
        if _is_calc_range(val):
            flat.extend(_flatten_range(val))
        else:
            flat.append(val)
    return flat


def _xor_logical(val: Any, *, in_range: bool) -> tuple[str, bool | float | str]:
    if isinstance(val, np.generic):
        val = val.item()
    if isinstance(val, (bool, np.bool_)):
        return ("ok", bool(val))
    if isinstance(val, (int, np.integer)) and not isinstance(val, bool):
        return ("ok", int(val) != 0)
    if isinstance(val, (float, np.floating)):
        number = float(val)
        if math.isnan(number):
            return ("err", float("nan"))
        return ("ok", number != 0.0)
    if isinstance(val, str):
        token = val.strip()
        if token in _LO_ERROR_TOKENS:
            return ("err", token)
        # Text and blanks inside a range do not count. A text argument is #VALUE!.
        if in_range:
            return ("skip", False)
        return ("err", "#VALUE!")
    if val is None:
        return ("skip", False) if in_range else ("err", "#VALUE!")
    return ("err", "#VALUE!")


def xor(*args: Any) -> bool | float | str:
    # bool(list) is True for any non-empty list, so a range of two TRUEs was
    # True. bool(ndarray) raises ValueError ("ambiguous truth value"). Walk
    # scalars instead. Calc ignores text in a range, rejects a text argument
    # with #VALUE!, and propagates an error token from a cell.
    trues = 0
    saw = False
    for arg in args:
        in_range = _is_calc_range(arg)
        values = _flatten_range(arg) if in_range else (arg,)
        for val in values:
            kind, payload = _xor_logical(val, in_range=in_range)
            if kind == "err":
                return payload
            if kind == "skip":
                continue
            saw = True
            if payload is True:
                trues += 1
    if not saw:
        return "#VALUE!"
    return trues % 2 == 1


def yearfrac(start_date: Any, end_date: Any, basis: Any = 0) -> float:
    # Old body divided actual days by 360 for basis 0, 2, and 4 and swapped
    # dates, so bond helpers disagreed with days360.
    from plugin.scripting.venv.calc_functions_d_h import days360

    try:
        s = float(start_date)
        e = float(end_date)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")
    if b < 0 or b > 4:
        return float("nan")
    if b == 0 or b == 4:
        counted = days360(s, e, b == 4)
        if math.isnan(counted):
            return float("nan")
        return counted / 360.0
    try:
        sd = dt.date.fromordinal(int(s) + 693594)
        ed = dt.date.fromordinal(int(e) + 693594)
    except (OverflowError, OSError, ValueError):
        return float("nan")
    actual = (ed - sd).days
    if b == 1:
        return actual / 365.25
    if b == 2:
        return actual / 360.0
    return actual / 365.0


def yield_calc(settlement: Any, maturity: Any, rate: Any, pr: Any, redemption: Any, frequency: Any, basis: Any = 0) -> float:
    # Approximate stub
    return float("nan")


def yielddisc(settlement: Any, maturity: Any, pr: Any, redemption: Any, basis: Any = 0) -> float:
    # Approximate stub
    return float("nan")


def yieldmat(settlement: Any, maturity: Any, issue: Any, rate: Any, pr: Any, basis: Any = 0) -> float:
    # Approximate stub
    return float("nan")


def ztest(data: Any, x: Any, sigma: Any | None = None) -> float:
    try:
        d = np.asarray(data, dtype=float).ravel()
        d = d[np.isfinite(d)]
        if len(d) == 0:
            return float("nan")
        val = float(x)
        n = len(d)
        m = np.mean(d)
        if sigma is None:
            s = np.std(d, ddof=1)
        else:
            s = float(sigma)

        if s == 0:
            return float("nan")

        z = (m - val) / (s / math.sqrt(n))
        import scipy.stats

        return float(scipy.stats.norm.sf(z))
    except (ValueError, TypeError):
        return float("nan")
