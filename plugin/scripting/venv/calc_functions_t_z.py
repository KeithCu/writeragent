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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, cast

import numpy as np

from .calc_functions_util import (
    _build_holiday_set,
    _clean_paired_arrays,
    _collect_a_values,
    _extract_numeric_array,
    _find_match_index,
    _find_text_cut,
    _is_calc_error,
    _parse_weekend,
    _scipy_stats,
    _serial_to_date,
)
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
        st = _scipy_stats()
        if st is None:
            return float("nan")

        # tdist in Calc/Excel returns 1 - cdf(val) for 1 tail
        # and 2 * (1 - cdf(val)) for 2 tails
        p = st.t.sf(val, d)
        return float(p if t == 1 else 2 * p)
    except (ValueError, TypeError, OverflowError):
        # int(float("inf")) on tails raises OverflowError. The handler only
        # caught ValueError and TypeError, so TDIST(x, df, inf) raised instead
        # of the NaN already returned for every other tail count outside 1 or 2.
        return float("nan")


# Excel/Calc numeric formats, not Python format specs.
_TEXT_NUMBER_FORMATS: dict[str, tuple[int, bool]] = {
    "0": (0, False),
    "0.0": (1, False),
    "0.00": (2, False),
    "0.000": (3, False),
    "#,##0": (0, True),
    "#,##0.0": (1, True),
    "#,##0.00": (2, True),
    "#,##0.000": (3, True),
}

_PERCENT_FORMATS: dict[str, int] = {
    "0%": 0,
    "0.0%": 1,
    "0.00%": 2,
}

_DATE_FORMATS: dict[str, str] = {
    "yyyy-mm-dd": "%Y-%m-%d",
    "yyyy/mm/dd": "%Y/%m/%d",
    "dd/mm/yyyy": "%d/%m/%Y",
    "mm/dd/yyyy": "%m/%d/%Y",
    "dd-mm-yyyy": "%d-%m-%Y",
    "yyyy": "%Y",
    "yy": "%y",
    "mm": "%m",
    "dd": "%d",
    "mmm": "%b",
    "mmmm": "%B",
    "ddd": "%a",
    "dddd": "%A",
}


def _format_number_pattern(value: float, places: int, grouped: bool) -> str:
    """Round half away from zero and apply numeric TEXT specs."""
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
    if grouped:
        body = f"{abs(rounded):,.{places}f}"
    else:
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
    if fmt_str in _PERCENT_FORMATS:
        places = _PERCENT_FORMATS[fmt_str]
        try:
            formatted = _format_number_pattern(float(val) * 100, places, False)
            return f"{formatted}%"
        except (ValueError, TypeError, OverflowError, InvalidOperation):
            return str(val)
    date_fmt = _DATE_FORMATS.get(fmt_str.lower())
    if date_fmt is not None:
        try:
            return dt.date.fromordinal(int(float(val)) + 693594).strftime(date_fmt)
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
        s, _delim, _inst, idx, match_end_miss = _find_text_cut(
            text, delimiter, instance_num, match_mode, after=False
        )
        if idx is not None:
            return s[:idx]
        if match_end and match_end_miss:
            return s
        return if_not_found
    except (ValueError, TypeError, OverflowError):
        # _find_text_cut does int(float(instance_num)). An infinite instance
        # raises OverflowError, which this handler did not catch, so TEXTBEFORE
        # raised instead of the NaN used for any other invalid instance.
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


def _split_text(text: str, delimiter: str, case_insensitive: bool) -> list[str]:
    if not delimiter:
        raise ValueError("empty separator")
    if case_insensitive:
        return re.split(re.escape(delimiter), text, flags=re.IGNORECASE)
    return text.split(delimiter)


def textsplit(text: Any, col_delimiter: Any, row_delimiter: Any = None, ignore_empty: Any = False, match_mode: Any = 0, pad_with: Any = float("nan")) -> Any:
    # Case-insensitive split still returns the original text. lower() would
    # change the pieces, not just the match.
    try:
        s = str(text)
        ci = (match_mode == 1)

        if col_delimiter is None and row_delimiter is None:
            return float("nan")

        if row_delimiter is not None:
            rows = _split_text(s, str(row_delimiter), ci)
            if ignore_empty:
                rows = [r for r in rows if r]
            res = []
            for r in rows:
                if col_delimiter is not None:
                    cols = _split_text(r, str(col_delimiter), ci)
                else:
                    cols = [r]
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
            cols = _split_text(s, str(col_delimiter), ci)
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
        st = _scipy_stats()
        if st is None:
            return float("nan")

        # TINV is the 2-tailed inverse
        return float(st.t.ppf(1 - p / 2, d))
    except (ValueError, TypeError):
        return float("nan")


def trend(*args: Any) -> Any:
    try:
        if not args:
            return "#VALUE!"
        data_y = np.asarray(args[0], dtype=float).ravel()
        if data_y.size == 0 or np.any(np.isnan(data_y)):
            return "#VALUE!"
        if len(args) > 1 and args[1] is not None:
            data_x = np.asarray(args[1], dtype=float)
            if data_x.ndim == 1:
                data_x = data_x[:, np.newaxis]
            elif data_x.ndim == 2 and data_x.shape[0] != data_y.shape[0] and data_x.shape[1] == data_y.shape[0]:
                data_x = data_x.T
        else:
            data_x = np.arange(1, len(data_y) + 1, dtype=float)[:, np.newaxis]

        if np.any(np.isnan(data_x)):
            return "#VALUE!"

        if len(args) > 2 and args[2] is not None:
            new_data_x = np.asarray(args[2], dtype=float)
            if new_data_x.ndim == 1:
                new_data_x = new_data_x[:, np.newaxis]
            elif new_data_x.ndim == 2 and new_data_x.shape[1] != data_x.shape[1] and new_data_x.shape[0] == data_x.shape[1]:
                new_data_x = new_data_x.T
        else:
            new_data_x = data_x

        if np.any(np.isnan(new_data_x)):
            return "#VALUE!"

        const = bool(args[3]) if len(args) > 3 else True

        if const:
            A = np.c_[data_x, np.ones(data_x.shape[0])]
            c, _unused, _unused2, _unused3 = np.linalg.lstsq(A, data_y, rcond=None)
            pred = np.c_[new_data_x, np.ones(new_data_x.shape[0])] @ c
        else:
            c, _unused, _unused2, _unused3 = np.linalg.lstsq(data_x, data_y, rcond=None)
            pred = new_data_x @ c

        return pred.tolist()
    except Exception:
        return "#VALUE!"


def trimmean(r: Any, percent: Any) -> float:
    # Skip blanks, text, and bools. A formula error (NaN) still propagates.
    try:
        arr = _extract_numeric_array(r, propagate_nan=True)
        if len(arr) == 0 or np.any(np.isnan(arr)):
            return float("nan")
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
        t = int(float(tails))
        type_num = int(float(type_))
        if t not in (1, 2) or type_num not in (1, 2, 3):
            return float("nan")

        if type_num == 1:
            cleaned = _clean_paired_arrays(data1, data2)
            if cleaned is None:
                return float("nan")
            d1_clean, d2_clean = cleaned
        else:
            d1_clean = _extract_numeric_array(data1, propagate_nan=False)
            d2_clean = _extract_numeric_array(data2, propagate_nan=False)

        if len(d1_clean) < 2 or len(d2_clean) < 2:
            return float("nan")

        st = _scipy_stats()
        if st is None:
            return float("nan")

        if type_num == 1:
            res = st.ttest_rel(d1_clean, d2_clean)
        elif type_num == 2:
            res = st.ttest_ind(d1_clean, d2_clean, equal_var=True)
        else:
            res = st.ttest_ind(d1_clean, d2_clean, equal_var=False)

        p = float(cast("float", res[1]))
        if t == 1:
            p /= 2.0
        return float(p)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def type(val: Any) -> float:
    # np.bool_ is logical (4), same as bool. It is a numpy integer subclass,
    # so the number check has to come after this one.
    if _is_calc_error(val):
        return 16.0
    if isinstance(val, (bool, np.bool_)):
        return 4.0
    if isinstance(val, (int, float, np.integer, np.floating)):
        return 1.0
    if isinstance(val, str):
        return 2.0
    if isinstance(val, (list, np.ndarray)):
        return 64.0
    # Blank cell (None) or unknown falls back to 1.0.
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
    # Dict insertion order is the first-seen order, so a second list is not needed.
    counts: dict[Any, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    if exactly_once:
        return [item for item, c in counts.items() if c == 1]
    return list(counts.keys())


def unique(arr: Any, by_col: bool = False, unique_only: bool = False) -> list[Any]:
    # dtype=object. A bare asarray makes 1 and '1' the same string.
    data = np.asarray(arr, dtype=object)
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
    vals = _collect_a_values(*args)
    if vals.size < 2:
        return float("nan")
    return float(np.var(vals, ddof=1))


def varpa(*args: Any) -> float:
    vals = _collect_a_values(*args)
    if not vals.size:
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
        st = _scipy_stats()
        if st is None:
            return float("nan")

        # In scipy, c=alpha (shape), scale=beta. Note: Calc calls alpha shape and beta scale.
        if cumulative:
            return float(st.weibull_min.cdf(val, a, scale=b))
        else:
            return float(st.weibull_min.pdf(val, a, scale=b))
    except (ValueError, TypeError):
        return float("nan")


def _advance_workdays(
    start: dt.date,
    remaining: int,
    weekend_days: set[int],
    holidays: set[dt.date],
) -> dt.date | None:
    """Move ``remaining`` working days from ``start`` (start itself is not counted).

    The previous loop added one calendar day per iteration, so the work was
    proportional to ``|days|`` and a large count froze the UI thread. Any 7
    consecutive days contain each weekday once, so whole weeks can be jumped.
    A holiday on a working day inside the span does not count; each one adds
    another working-day step, and that pass is bounded by the holiday set.
    A week with no working day cannot advance (Excel ``#NUM!``). A result
    outside the datetime range is the same ``#NUM!``.
    """
    if remaining == 0:
        return start
    step = 1 if remaining > 0 else -1
    work_per_week = 7 - len(weekend_days)
    if work_per_week <= 0:
        return None
    n_left = abs(remaining)

    def advance_ignoring_holidays(origin: dt.date, n_work: int) -> dt.date:
        full_weeks, rem = divmod(n_work, work_per_week)
        cursor = origin + dt.timedelta(days=step * full_weeks * 7)
        if rem == 0:
            # Landing on the same weekday overshoots when that weekday is not
            # a workday. The n-th workday is the last workday at or before
            # the landing date in the direction of travel.
            while cursor.weekday() in weekend_days:
                cursor -= dt.timedelta(days=step)
            return cursor
        left = rem
        while left:
            cursor += dt.timedelta(days=step)
            if cursor.weekday() not in weekend_days:
                left -= 1
        return cursor

    try:
        end = advance_ignoring_holidays(start, n_left)
        counted: set[dt.date] = set()
        while True:
            if step > 0:
                lo = start + dt.timedelta(days=1)
                hi = end
            else:
                lo = end
                hi = start - dt.timedelta(days=1)
            extra = 0
            for holiday in holidays:
                if holiday in counted:
                    continue
                if lo <= holiday <= hi and holiday.weekday() not in weekend_days:
                    counted.add(holiday)
                    extra += 1
            if extra == 0:
                return end
            end = advance_ignoring_holidays(end, extra)
    except (OverflowError, ValueError, OSError):
        return None


def _workday_serial(
    start_date: Any,
    days: Any,
    weekend_days: set[int],
    holidays: Any | None,
) -> float:
    curr = _serial_to_date(start_date)
    if curr is None:
        return float("nan")
    # days sat outside the start-date try, so a text days cell raised.
    try:
        remaining = int(float(days))
    except (ValueError, TypeError, OverflowError):
        return float("nan")
    end = _advance_workdays(curr, remaining, weekend_days, _build_holiday_set(holidays))
    if end is None:
        return float("nan")
    return float(end.toordinal() - 693594)


def workday(start_date: Any, days: Any, holidays: Any | None = None) -> float:
    # Excel WORKDAY weekend is Saturday and Sunday (weekday() 5 and 6).
    return _workday_serial(start_date, days, {5, 6}, holidays)


def workday_intl(start_date: Any, days: Any, weekend: Any = 1, holidays: Any | None = None) -> float:
    wk_days = _parse_weekend(weekend)
    if not isinstance(wk_days, set):
        return float("nan")
    return _workday_serial(start_date, days, wk_days, holidays)


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
    # A return array shorter than the matched index used to raise IndexError
    # (xlookup("b", ["a", "b"], [1])). The match succeeded, so this is not
    # if_not_found; it is a bad range. Report #VALUE!, same as other shape
    # mismatches, instead of letting the exception escape.
    try:
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
    except IndexError:
        return "#VALUE!"


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
    # Swap when start is after end. Excel and LibreOffice GetYearFrac return
    # a non-negative fraction either way.
    from plugin.scripting.venv.calc_functions_d_h import days360

    try:
        s = float(start_date)
        e = float(end_date)
        b = int(float(basis))
    except (ValueError, TypeError):
        return float("nan")
    if b < 0 or b > 4:
        return float("nan")
    if s > e:
        s, e = e, s
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
    """Yield of a security that pays periodic interest. Not implemented in lightweight runtime."""
    return float("nan")


def yielddisc(settlement: Any, maturity: Any, pr: Any, redemption: Any, basis: Any = 0) -> float:
    """Annual yield of a discounted security (e.g. Treasury bill)."""
    try:
        p = float(pr)
        red = float(redemption)
        b = int(float(basis))
        if p <= 0 or red <= 0 or b < 0 or b > 4:
            return float("nan")
        yf = yearfrac(settlement, maturity, b)
        if math.isnan(yf) or yf <= 0:
            return float("nan")
        return float(((red - p) / p) / yf)
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def yieldmat(settlement: Any, maturity: Any, issue: Any, rate: Any, pr: Any, basis: Any = 0) -> float:
    """Annual yield of a security that pays interest at maturity."""
    try:
        r = float(rate)
        p = float(pr)
        b = int(float(basis))
        if r < 0 or p <= 0 or b < 0 or b > 4:
            return float("nan")
        dim_b = yearfrac(issue, maturity, b)
        dis_b = yearfrac(issue, settlement, b)
        dsm_b = yearfrac(settlement, maturity, b)
        if math.isnan(dim_b) or math.isnan(dis_b) or math.isnan(dsm_b) or dsm_b <= 0:
            return float("nan")
        denom = (p / 100.0) + r * dis_b
        if denom == 0:
            return float("nan")
        num = (1.0 + r * dim_b) / denom - 1.0
        return float(num / dsm_b)
    except (ValueError, TypeError, OverflowError):
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
        st = _scipy_stats()
        if st is None:
            return float("nan")

        return float(st.norm.sf(z))
    except (ValueError, TypeError):
        return float("nan")
