# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc formula parity helpers for =PY() and spreadsheet import (auto-imported as ``xl``).

Semantics mirror the inline helpers formerly pasted by spreadsheet import translation.
"""
from __future__ import annotations

import datetime
import re
from collections import Counter
from typing import Any, Callable

import numpy as np

from plugin.scripting.calc_functions_common import HELPER_NAMES

__all__ = ["HELPER_NAMES"] + sorted(HELPER_NAMES)


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
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return alt
        return val
    except Exception:
        return alt


def match_criteria(val: Any, crit: Any) -> bool:
    if crit is None or crit == "":
        return val is None or val == ""
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


def sumif(r: Any, crit: Any, sr: Any | None = None) -> float:
    r_flat = np.asarray(r).ravel()
    sr_flat = np.asarray(sr).ravel() if sr is not None else r_flat
    total = 0.0
    for i in range(min(len(r_flat), len(sr_flat))):
        if match_criteria(r_flat[i], crit):
            try:
                val = float(sr_flat[i])
                if not np.isnan(val):
                    total += val
            except (ValueError, TypeError):
                pass
    return float(total)


def sumifs(sr: Any, *args: Any) -> float:
    sr_flat = np.asarray(sr).ravel()
    cond_ranges = []
    criteria = []
    for i in range(0, len(args), 2):
        cond_ranges.append(np.asarray(args[i]).ravel())
        criteria.append(args[i + 1])
    total = 0.0
    for idx in range(len(sr_flat)):
        match = True
        for cr, crit in zip(cond_ranges, criteria):
            if idx >= len(cr) or not match_criteria(cr[idx], crit):
                match = False
                break
        if match:
            try:
                val = float(sr_flat[idx])
                if not np.isnan(val):
                    total += val
            except (ValueError, TypeError):
                pass
    return float(total)


def countif(r: Any, crit: Any) -> float:
    r_flat = np.asarray(r).ravel()
    cnt = 0
    for val in r_flat:
        if match_criteria(val, crit):
            cnt += 1
    return float(cnt)


def countifs(*args: Any) -> float:
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


def averageif(r: Any, crit: Any, ar: Any | None = None) -> float:
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


def xlookup(
    lookup_val: Any,
    lookup_arr: Any,
    return_arr: Any,
    if_not_found: Any | None = None,
    match_mode: int | float = 0,
    search_mode: int | float = 1,
) -> Any:
    l_flat = np.asarray(lookup_arr).ravel()
    r_flat = np.asarray(return_arr)
    indices = list(range(len(l_flat)))
    if search_mode == -1:
        indices.reverse()
    best_idx = None
    if match_mode == 0:
        for idx in indices:
            if l_flat[idx] == lookup_val:
                best_idx = idx
                break
    elif match_mode in (-1, 1):
        for idx in indices:
            if l_flat[idx] == lookup_val:
                best_idx = idx
                break
        if best_idx is None:
            best_diff = None
            for idx in indices:
                try:
                    diff = float(l_flat[idx]) - float(lookup_val)
                    if match_mode == -1 and diff < 0:
                        if best_diff is None or diff > best_diff:
                            best_diff = diff
                            best_idx = idx
                    elif match_mode == 1 and diff > 0:
                        if best_diff is None or diff < best_diff:
                            best_diff = diff
                            best_idx = idx
                except (ValueError, TypeError):
                    pass
    elif match_mode == 2:
        if isinstance(lookup_val, str):
            pattern = re.escape(lookup_val).replace(r"\*", ".*").replace(r"\?", ".")
            regex = re.compile(f"^{pattern}$")
            for idx in indices:
                if isinstance(l_flat[idx], str) and regex.match(l_flat[idx]):
                    best_idx = idx
                    break
        else:
            for idx in indices:
                if l_flat[idx] == lookup_val:
                    best_idx = idx
                    break
    if best_idx is None:
        return if_not_found
    if r_flat.ndim == 1:
        return r_flat[best_idx]
    if r_flat.ndim == 2:
        l_shape = np.asarray(lookup_arr).shape
        if len(l_shape) == 2 and l_shape[0] > 1 and l_shape[1] == 1:
            return r_flat[best_idx].tolist()
        if best_idx < r_flat.shape[1]:
            return r_flat[:, best_idx].tolist()
        return r_flat.ravel()[best_idx]
    return r_flat.ravel()[best_idx]


def textjoin(delim: Any, ignore_empty: Any, *args: Any) -> str:
    parts = []
    for arg in args:
        for val in np.asarray(arg).ravel():
            if val is None or val == "":
                if not ignore_empty:
                    parts.append("")
            else:
                parts.append(str(val))
    return str(delim).join(parts)


def eomonth(start_date: Any, months: Any) -> float:
    try:
        date_val = datetime.date.fromordinal(int(float(start_date)) + 693594)
    except Exception:
        return float("nan")
    y, m = date_val.year, date_val.month
    m += int(float(months))
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    if m == 12:
        next_month = datetime.date(y + 1, 1, 1)
    else:
        next_month = datetime.date(y, m + 1, 1)
    last_day = next_month - datetime.timedelta(days=1)
    return float(last_day.toordinal() - 693594)


def networkdays(start_date: Any, end_date: Any, holidays: Any | None = None) -> float:
    try:
        sd = datetime.date.fromordinal(int(float(start_date)) + 693594)
        ed = datetime.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")
    if sd > ed:
        sign = -1
        sd, ed = ed, sd
    else:
        sign = 1
    h_dates: set[datetime.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                try:
                    h_dates.add(datetime.date.fromordinal(int(float(h)) + 693594))
                except Exception:
                    pass
    curr = sd
    days = 0
    while curr <= ed:
        if curr.weekday() < 5 and curr not in h_dates:
            days += 1
        curr += datetime.timedelta(days=1)
    return float(sign * days)


def regex(text: Any, expr: Any, replacement: Any | None = None, flags: str = "") -> str:
    if text is None:
        text = ""
    text_str = str(text)
    expr_str = str(expr)
    re_flags = 0
    if "i" in str(flags).lower():
        re_flags |= re.IGNORECASE
    if replacement is None:
        if "g" in str(flags).lower():
            matches = re.findall(expr_str, text_str, flags=re_flags)
            if not matches:
                return ""
            if isinstance(matches[0], tuple):
                return ", ".join("".join(m) for m in matches)
            return ", ".join(matches)
        m = re.search(expr_str, text_str, flags=re_flags)
        if m:
            return m.group(1) if m.groups() else m.group(0)
        return ""
    rep_str = str(replacement)
    if "g" in str(flags).lower():
        return re.sub(expr_str, rep_str, text_str, flags=re_flags)
    return re.sub(expr_str, rep_str, text_str, count=1, flags=re_flags)


def isblank(val: Any) -> bool:
    return val is None or val == ""


def isnumber(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def isna(val: Any) -> bool:
    if isinstance(val, str) and val.upper().startswith("#N/A"):
        return True
    return val is None or (isinstance(val, float) and np.isnan(val))


def iserror(val: Any) -> bool:
    return isinstance(val, str) and val.startswith("#")


def istext(val: Any) -> bool:
    return isinstance(val, str) and not (isinstance(val, str) and val.startswith("#"))


def islogical(val: Any) -> bool:
    return isinstance(val, bool)


def iserr(val: Any) -> bool:
    if isinstance(val, str) and val.startswith("#"):
        return not val.upper().startswith("#N/A")
    return False


def isnontext(val: Any) -> bool:
    return not isinstance(val, str) or val == "" or val.startswith("#")


def subtotal(fn_num: Any, r: Any) -> float:
    fn = int(float(fn_num)) % 100
    flat = np.asarray(r).ravel()
    nums = []
    for x in flat:
        if x is None or x == "":
            continue
        try:
            v = float(x)
            if not np.isnan(v):
                nums.append(v)
        except (ValueError, TypeError):
            pass
    arr = np.asarray(nums, dtype=float)
    if fn == 1:
        return float(np.mean(arr)) if len(arr) else 0.0
    if fn == 2:
        return float(len(arr))
    if fn == 3:
        return float(sum(1 for x in flat if x is not None and x != ""))
    if fn == 4:
        return float(np.max(arr)) if len(arr) else 0.0
    if fn == 5:
        return float(np.min(arr)) if len(arr) else 0.0
    if fn == 6:
        return float(np.prod(arr)) if len(arr) else 0.0
    if fn == 7:
        return float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    if fn == 8:
        return float(np.std(arr, ddof=0)) if len(arr) else 0.0
    if fn == 9:
        return float(np.sum(arr))
    if fn == 10:
        return float(np.var(arr, ddof=1)) if len(arr) > 1 else 0.0
    if fn == 11:
        return float(np.var(arr, ddof=0)) if len(arr) else 0.0
    return float(np.sum(arr))


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


def edate(start_date: Any, months: Any) -> float:
    try:
        date_val = datetime.date.fromordinal(int(float(start_date)) + 693594)
    except Exception:
        return float("nan")
    y, m = date_val.year, date_val.month
    m += int(float(months))
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = min(
        date_val.day,
        [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1],
    )
    return float(datetime.date(y, m, d).toordinal() - 693594)


def datedif(start_date: Any, end_date: Any, unit: str = "D") -> float:
    try:
        sd = datetime.date.fromordinal(int(float(start_date)) + 693594)
        ed = datetime.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")
    if sd > ed:
        return float("nan")
    u = str(unit).strip('"').upper()
    if u == "D":
        return float((ed - sd).days)
    if u == "M":
        return float((ed.year - sd.year) * 12 + ed.month - sd.month)
    if u == "Y":
        return float(ed.year - sd.year - ((ed.month, ed.day) < (sd.month, sd.day)))
    if u == "MD":
        return float(ed.day - sd.day)
    if u == "YM":
        return float(ed.month - sd.month - (ed.day < sd.day))
    if u == "YD":
        return float((ed - datetime.date(ed.year, sd.month, sd.day)).days)
    return float((ed - sd).days)


def sumproduct(*args: Any) -> float:
    arrays = [np.asarray(a).ravel() for a in args]
    if not arrays:
        return 0.0
    min_len = min(len(a) for a in arrays)
    total = 0.0
    for i in range(min_len):
        prod = 1.0
        for arr in arrays:
            try:
                prod *= float(arr[i])
            except (ValueError, TypeError):
                prod = 0.0
                break
        total += prod
    return float(total)


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


def quartile(r: Any, q: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    qi = int(float(q))
    pct = {0: 0.0, 1: 25.0, 2: 50.0, 3: 75.0, 4: 100.0}.get(qi, float(qi) * 25.0)
    return float(np.percentile(arr, pct)) if len(arr) else float("nan")


def rank(val: Any, r: Any, order: int | float = 0) -> float:
    arr = [float(x) for x in np.asarray(r).ravel() if x is not None and x != ""]
    try:
        target = float(val)
    except (ValueError, TypeError):
        return float("nan")
    if int(float(order)) == 0:
        arr.sort(reverse=True)
    else:
        arr.sort()
    try:
        return float(arr.index(target) + 1)
    except ValueError:
        return float("nan")


def large(r: Any, k: Any) -> float:
    arr = sorted([float(x) for x in np.asarray(r).ravel() if x is not None and x != ""], reverse=True)
    ki = int(float(k))
    return float(arr[ki - 1]) if 0 < ki <= len(arr) else float("nan")


def small(r: Any, k: Any) -> float:
    arr = sorted([float(x) for x in np.asarray(r).ravel() if x is not None and x != ""])
    ki = int(float(k))
    return float(arr[ki - 1]) if 0 < ki <= len(arr) else float("nan")


def mode(r: Any) -> Any:
    vals = [x for x in np.asarray(r).ravel() if x is not None and x != ""]
    if not vals:
        return float("nan")
    counts = Counter(vals)
    return counts.most_common(1)[0][0]


def text(val: Any, fmt: Any) -> str:
    fmt_str = str(fmt).strip('"')
    if fmt_str in ("0", "0.00", "#,##0"):
        try:
            return format(float(val), fmt_str.replace("#", "").replace(",", "") or ".0f")
        except (ValueError, TypeError):
            return str(val)
    return str(val)


def even(n: Any) -> float:
    v = float(n)
    i = int(np.trunc(v))
    if i % 2 == 0:
        return float(i)
    return float(i + (1 if v >= 0 else -1))


def odd(n: Any) -> float:
    v = float(n)
    i = int(np.trunc(v))
    if i % 2 != 0:
        return float(i)
    return float(i + (1 if v >= 0 else -1))


def xmatch(lookup_val: Any, lookup_arr: Any, match_mode: int | float = 0, search_mode: int | float = 1) -> float:
    l_flat = np.asarray(lookup_arr).ravel()
    indices = list(range(len(l_flat)))
    if int(float(search_mode)) == -1:
        indices.reverse()
    mm = int(float(match_mode))
    if mm == 0:
        for idx in indices:
            if l_flat[idx] == lookup_val:
                return float(idx + 1)
    elif mm in (-1, 1):
        for idx in indices:
            if l_flat[idx] == lookup_val:
                return float(idx + 1)
        best_idx = None
        for idx in indices:
            try:
                diff = float(l_flat[idx]) - float(lookup_val)
                if mm == -1 and diff < 0:
                    if best_idx is None or diff > float(l_flat[best_idx]) - float(lookup_val):
                        best_idx = idx
                elif mm == 1 and diff > 0:
                    if best_idx is None or diff < float(l_flat[best_idx]) - float(lookup_val):
                        best_idx = idx
            except (ValueError, TypeError):
                pass
        return float(best_idx + 1) if best_idx is not None else float("nan")
    return float("nan")


def weekday(serial: Any, return_type: int | float = 1) -> float:
    try:
        d = datetime.date.fromordinal(int(float(serial)) + 693594)
    except Exception:
        return float("nan")
    rt = int(float(return_type))
    wd = d.weekday()
    if rt == 1:
        return float(wd + 2 if wd < 6 else 1)
    if rt == 2:
        return float(wd + 1)
    if rt == 3:
        return float((wd + 6) % 7)
    return float(wd + 1)


def weeknum(serial: Any, return_type: int | float = 1) -> float:
    try:
        d = datetime.date.fromordinal(int(float(serial)) + 693594)
    except Exception:
        return float("nan")
    iso = d.isocalendar()
    return float(iso[1])


def workday(start_date: Any, days: Any, holidays: Any | None = None) -> float:
    try:
        curr = datetime.date.fromordinal(int(float(start_date)) + 693594)
    except Exception:
        return float("nan")
    h_dates: set[datetime.date] = set()
    if holidays is not None:
        for h in np.asarray(holidays).ravel():
            if h is not None and h != "":
                try:
                    h_dates.add(datetime.date.fromordinal(int(float(h)) + 693594))
                except Exception:
                    pass
    remaining = int(float(days))
    step = 1 if remaining >= 0 else -1
    while remaining != 0:
        curr += datetime.timedelta(days=step)
        if curr.weekday() < 5 and curr not in h_dates:
            remaining -= step
    return float(curr.toordinal() - 693594)


def filter(range_arr: Any, criteria: Any, if_empty: Any | None = None) -> Any:
    arr = np.asarray(range_arr)
    crit = np.asarray(criteria)
    if arr.ndim == 1:
        mask = np.asarray([bool(x) for x in crit.ravel()[: len(arr)]])
        out = arr.ravel()[mask]
    else:
        if crit.ndim == 1:
            mask = np.asarray([bool(x) for x in crit.ravel()[: arr.shape[0]]])
            out = arr[mask]
        else:
            mask = crit.astype(bool)
            out = arr[mask]
    if out.size == 0:
        return if_empty
    return out.tolist() if out.ndim > 1 else out.ravel().tolist()


def sort(range_arr: Any, sort_index: int | float = 1, sort_order: int | float = 1, by_col: bool = False) -> list:
    arr = np.asarray(range_arr)
    if arr.size == 0:
        return []
    si = max(1, int(float(sort_index))) - 1
    asc = int(float(sort_order)) >= 0
    if arr.ndim == 1:
        out = np.sort(arr) if asc else np.sort(arr)[::-1]
        return out.tolist()
    if bool(by_col):
        order = np.argsort(arr[:, si] if si < arr.shape[1] else arr[:, 0])
        if not asc:
            order = order[::-1]
        return arr[:, order].T.tolist()
    order = np.argsort(arr[:, si] if si < arr.shape[1] else arr[:, 0])
    if not asc:
        order = order[::-1]
    return arr[order].tolist()


def unique(arr: Any, by_col: bool = False, unique_only: bool = False) -> list:
    data = np.asarray(arr)
    if data.size == 0:
        return []
    if data.ndim == 1 or not bool(by_col):
        flat = data.ravel().tolist()
        seen = []
        counts: dict[Any, int] = {}
        for x in flat:
            counts[x] = counts.get(x, 0) + 1
            if x not in seen:
                seen.append(x)
        if bool(unique_only):
            return [x for x in seen if counts[x] == 1]
        return seen
    rows = [tuple(r) for r in data]
    seen_rows = []
    for row in rows:
        if row not in seen_rows:
            seen_rows.append(row)
    return [list(r) for r in seen_rows]


def sortby(range_arr: Any, by_array: Any, sort_order: int | float = 1, *extra: Any) -> list:
    arr = np.asarray(range_arr)
    by = np.asarray(by_array).ravel()
    asc = int(float(sort_order)) >= 0
    if arr.ndim == 1:
        order = np.argsort(by[: len(arr)])
        if not asc:
            order = order[::-1]
        return arr.ravel()[order].tolist()
    order = np.argsort(by[: arr.shape[0]])
    if not asc:
        order = order[::-1]
    return arr[order].tolist()
