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
from typing import Any, Callable

import numpy as np

from plugin.scripting.calc_functions_common import HELPER_NAMES

__all__ = ["HELPER_NAMES", *sorted(HELPER_NAMES)]


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


def pmt(rate: Any, nper: Any, pv: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    r = float(rate)
    n = float(nper)
    p = float(pv)
    f = float(fv_val)
    t = int(float(type_val))
    if r == 0:
        return float(-(p + f) / n)
    factor = (1 + r) ** n
    if t == 1:
        return float(-(p * factor + f) * r / (factor - 1) / (1 + r))
    return float(-(p * factor + f) * r / (factor - 1))


def fv(rate: Any, nper: Any, pmt_val: Any, pv_val: Any = 0, type_val: Any = 0) -> float:
    r = float(rate)
    n = float(nper)
    pm = float(pmt_val)
    p = float(pv_val)
    t = int(float(type_val))
    if r == 0:
        return float(-(p + pm * n))
    factor = (1 + r) ** n
    if t == 1:
        return float(-(p * factor + pm * (factor - 1) * (1 + r) / r))
    return float(-(p * factor + pm * (factor - 1) / r))


def pv(rate: Any, nper: Any, pmt_val: Any, fv_val: Any = 0, type_val: Any = 0) -> float:
    r = float(rate)
    n = float(nper)
    pm = float(pmt_val)
    f = float(fv_val)
    t = int(float(type_val))
    if r == 0:
        return float(-(f + pm * n))
    factor = (1 + r) ** n
    if t == 1:
        return float(-(f + pm * (factor - 1) * (1 + r) / r) / factor)
    return float(-(f + pm * (factor - 1) / r) / factor)


def mround(number: Any, multiple: Any) -> float:
    n = float(number)
    m = float(multiple)
    if m == 0:
        return 0.0
    if (n > 0 and m < 0) or (n < 0 and m > 0):
        return float("nan")
    return float(round(n / m) * m)


def sumsq(*args: Any) -> float:
    total = 0.0
    for arg in args:
        for val in np.asarray(arg).ravel():
            if val is not None and val != "":
                try:
                    total += float(val) ** 2
                except (ValueError, TypeError):
                    pass
    return float(total)


def iseven(val: Any) -> bool:
    try:
        f = float(val)
        if np.isnan(f):
            return False
        return int(f) % 2 == 0
    except (ValueError, TypeError):
        return False


def isodd(val: Any) -> bool:
    try:
        f = float(val)
        if np.isnan(f):
            return False
        return int(f) % 2 != 0
    except (ValueError, TypeError):
        return False


def days(end_date: Any, start_date: Any) -> float:
    try:
        ed = float(end_date)
        sd = float(start_date)
        return float(ed - sd)
    except (ValueError, TypeError):
        return float("nan")


def time(hour: Any, minute: Any, second: Any) -> float:
    h = int(float(hour))
    m = int(float(minute))
    s = int(float(second))
    total_seconds = h * 3600 + m * 60 + s
    return float(total_seconds / 86400.0)


def trimmean(r: Any, percent: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    if not arr.size:
        return float("nan")
    p = float(percent)
    if p < 0 or p >= 1:
        return float("nan")
    k = int(len(arr) * p / 2)
    if k == 0:
        return float(np.mean(arr))
    arr.sort()
    return float(np.mean(arr[k:-k]))


def forecast(x: Any, data_y: Any, data_x: Any) -> float:
    xv = float(x)
    y = np.asarray(data_y, dtype=float).ravel()
    x_arr = np.asarray(data_x, dtype=float).ravel()
    if y.size != x_arr.size or y.size < 2:
        return float("nan")
    avg_x = np.mean(x_arr)
    avg_y = np.mean(y)
    ss_xx = np.sum((x_arr - avg_x) ** 2)
    if ss_xx == 0:
        return float("nan")
    b = np.sum((x_arr - avg_x) * (y - avg_y)) / ss_xx
    a = avg_y - b * avg_x
    return float(a + b * xv)


def fact(n: Any) -> float:
    try:
        v = float(n)
        if v < 0 or v > 170:  # math.factorial limit
            return float("nan")
        return float(math.factorial(int(v)))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def combin(n: Any, k: Any) -> float:
    try:
        return float(math.comb(int(float(n)), int(float(k))))
    except (ValueError, TypeError):
        return float("nan")


def rept(text: Any, n: Any) -> str:
    try:
        return str(text) * int(float(n))
    except (ValueError, TypeError, OverflowError):
        return ""


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


def datevalue(text: Any) -> float:
    s = str(text).strip().strip('"')
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            return float(dt.toordinal() - 693594)
        except ValueError:
            continue
    return float("nan")


def timevalue(text: Any) -> float:
    s = str(text).strip().strip('"')
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            t = datetime.datetime.strptime(s, fmt).time()
            return float((t.hour * 3600 + t.minute * 60 + t.second) / 86400.0)
        except ValueError:
            continue
    return float("nan")


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


def type(val: Any) -> float:
    if val is None or val == "":
        return 1.0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return 1.0
    if isinstance(val, str):
        if val.startswith("#"):
            return 16.0
        return 2.0
    if isinstance(val, bool):
        return 4.0
    if isinstance(val, (list, np.ndarray)):
        return 64.0
    return 1.0


def choose(index: Any, *args: Any) -> Any:
    try:
        idx = int(float(index))
        if 1 <= idx <= len(args):
            return args[idx - 1]
    except (ValueError, TypeError):
        pass
    return None


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


def yearfrac(start_date: Any, end_date: Any, basis: Any = 0) -> float:
    try:
        sd = datetime.date.fromordinal(int(float(start_date)) + 693594)
        ed = datetime.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")

    if sd > ed:
        sd, ed = ed, sd
    diff = (ed - sd).days
    b = int(float(basis))

    if b == 0:  # US (NASD) 30/360
        return diff / 360.0
    if b == 1:  # Actual/actual
        return diff / 365.25
    if b == 2:  # Actual/360
        return diff / 360.0
    if b == 3:  # Actual/365
        return diff / 365.0
    if b == 4:  # European 30/360
        return diff / 360.0
    return diff / 365.0


def days360(start_date: Any, end_date: Any, method: Any = False) -> float:
    try:
        sd = datetime.date.fromordinal(int(float(start_date)) + 693594)
        ed = datetime.date.fromordinal(int(float(end_date)) + 693594)
    except Exception:
        return float("nan")

    d1, m1, y1 = sd.day, sd.month, sd.year
    d2, m2, y2 = ed.day, ed.month, ed.year

    if bool(method):  # European
        if d1 == 31:
            d1 = 30
        if d2 == 31:
            d2 = 30
    else:  # US
        if d1 == 31:
            d1 = 30
        if d2 == 31 and d1 == 30:
            d2 = 30

    return float((y2 - y1) * 360 + (m2 - m1) * 30 + (d2 - d1))


def networkdays_intl(start_date: Any, end_date: Any, weekend: Any = 1, holidays: Any | None = None) -> float:
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

    wk_days = set()
    if isinstance(weekend, str):
        for i, char in enumerate(weekend[:7]):
            if char == "1":
                wk_days.add(i)
    else:
        w_idx = int(float(weekend))
        mapping = {
            1: (5, 6),
            2: (6, 0),
            3: (0, 1),
            4: (1, 2),
            5: (2, 3),
            6: (3, 4),
            7: (4, 5),
            11: (6,),
            12: (0,),
            13: (1,),
            14: (2,),
            15: (3,),
            16: (4,),
            17: (5,),
        }
        wk_days.update(mapping.get(w_idx, (5, 6)))

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
        if curr.weekday() not in wk_days and curr not in h_dates:
            days += 1
        curr += datetime.timedelta(days=1)
    return float(sign * days)


def workday_intl(start_date: Any, days: Any, weekend: Any = 1, holidays: Any | None = None) -> float:
    try:
        curr = datetime.date.fromordinal(int(float(start_date)) + 693594)
    except Exception:
        return float("nan")

    wk_days = set()
    if isinstance(weekend, str):
        for i, char in enumerate(weekend[:7]):
            if char == "1":
                wk_days.add(i)
    else:
        w_idx = int(float(weekend))
        mapping = {
            1: (5, 6),
            2: (6, 0),
            3: (0, 1),
            4: (1, 2),
            5: (2, 3),
            6: (3, 4),
            7: (4, 5),
            11: (6,),
            12: (0,),
            13: (1,),
            14: (2,),
            15: (3,),
            16: (4,),
            17: (5,),
        }
        wk_days.update(mapping.get(w_idx, (5, 6)))

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
        if curr.weekday() not in wk_days and curr not in h_dates:
            remaining -= step
    return float(curr.toordinal() - 693594)


def xor(*args: Any) -> bool:
    true_count = 0
    for arg in args:
        if bool(arg):
            true_count += 1
    return true_count % 2 == 1


def areas(r: Any) -> float:
    return 1.0


def char(n: Any) -> str:
    try:
        return chr(int(float(n)))
    except (ValueError, TypeError):
        return ""


def code(s: Any) -> float:
    try:
        ss = str(s)
        return float(ord(ss[0])) if ss else float("nan")
    except (ValueError, TypeError):
        return float("nan")


def _eval_d_criteria(db: Any, field: Any, criteria: Any, as_float: bool = True) -> list[Any]:
    """Shared helper for D* functions."""
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


def daverage(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.mean(vals)) if vals else float("nan")


def dcount(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(len(vals))


def dcounta(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    return float(sum(1 for v in vals if v is not None and v != ""))


def dget(db: Any, field: Any, criteria: Any) -> Any:
    vals = _eval_d_criteria(db, field, criteria, as_float=False)
    if len(vals) == 1:
        return vals[0]
    return "#NUM!" if len(vals) > 1 else "#VALUE!"


def dmax(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.max(vals)) if vals else float("nan")


def dmin(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.min(vals)) if vals else float("nan")


def dproduct(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.prod(vals)) if vals else 0.0


def dstdev(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.std(vals, ddof=1)) if len(vals) > 1 else float("nan")


def dstdevp(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.std(vals, ddof=0)) if vals else float("nan")


def dsum(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.sum(vals))


def dvar(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.var(vals, ddof=1)) if len(vals) > 1 else float("nan")


def dvarp(db: Any, field: Any, criteria: Any) -> float:
    vals = _eval_d_criteria(db, field, criteria)
    return float(np.var(vals, ddof=0)) if vals else float("nan")


def isoweeknum(serial: Any) -> float:
    try:
        d = datetime.date.fromordinal(int(float(serial)) + 693594)
        return float(d.isocalendar()[1])
    except Exception:
        return float("nan")


def factdouble(n: Any) -> float:
    try:
        v = int(float(n))
        if v < 0:
            return float("nan")
        res = 1
        for i in range(v, 0, -2):
            res *= i
        return float(res)
    except (ValueError, TypeError, OverflowError):
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


def avedev(r: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    if not arr.size:
        return float("nan")
    return float(np.mean(np.abs(arr - np.mean(arr))))


def geomean(r: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(np.exp(np.mean(np.log(arr))))


def harmean(r: Any) -> float:
    arr = np.asarray(r, dtype=float).ravel()
    arr = arr[~np.isnan(arr)]
    if not arr.size or np.any(arr <= 0):
        return float("nan")
    return float(len(arr) / np.sum(1.0 / arr))


def npv(rate: Any, *args: Any) -> float:
    r = float(rate)
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                vals.append(0.0)
    res = 0.0
    for i, v in enumerate(vals):
        res += v / ((1 + r) ** (i + 1))
    return float(res)


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


def devsq(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    if not vals:
        return float("nan")
    arr = np.asarray(vals)
    return float(np.sum((arr - np.mean(arr)) ** 2))


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


def skew(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    n = len(vals)
    if n < 3:
        return float("nan")
    arr = np.asarray(vals)
    m = np.mean(arr)
    s = np.std(arr, ddof=1)
    if s == 0:
        return float("nan")
    # Excel/Calc skewness formula
    z = (arr - m) / s
    term1 = n / ((n - 1) * (n - 2))
    term2 = np.sum(z**3)
    return float(term1 * term2)


def slope(data_y: Any, data_x: Any) -> float:
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[mask], x[mask]
    if len(y) < 2:
        return float("nan")
    mx, my = np.mean(x), np.mean(y)
    ss_xy = np.sum((x - mx) * (y - my))
    ss_xx = np.sum((x - mx) ** 2)
    return float(ss_xy / ss_xx) if ss_xx != 0 else float("nan")


def intercept(data_y: Any, data_x: Any) -> float:
    s = slope(data_y, data_x)
    if np.isnan(s):
        return float("nan")
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    return float(np.mean(y[mask]) - s * np.mean(x[mask]))


def rsq(data_y: Any, data_x: Any) -> float:
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[mask], x[mask]
    if len(y) < 2:
        return float("nan")
    corr = np.corrcoef(x, y)[0, 1]
    return float(corr**2)


def steyx(data_y: Any, data_x: Any) -> float:
    y = np.asarray(data_y, dtype=float).ravel()
    x = np.asarray(data_x, dtype=float).ravel()
    mask = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[mask], x[mask]
    n = len(y)
    if n < 3:
        return float("nan")
    s = slope(y, x)
    i = intercept(y, x)
    y_hat = s * x + i
    ss_resid = np.sum((y - y_hat) ** 2)
    return float(math.sqrt(ss_resid / (n - 2)))


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


def sec(x: Any) -> float:
    try:
        return float(1.0 / math.cos(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


def sech(x: Any) -> float:
    try:
        return float(1.0 / math.cosh(float(x)))
    except (ValueError, TypeError, ZeroDivisionError):
        return float("nan")


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


def stdeva(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if len(vals) < 2:
        return float("nan")
    return float(np.std(vals, ddof=1))


def stdevpa(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
        return float("nan")
    return float(np.std(vals, ddof=0))


def vara(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if len(vals) < 2:
        return float("nan")
    return float(np.var(vals, ddof=1))


def varpa(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
        return float("nan")
    return float(np.var(vals, ddof=0))


def maxa(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
        return 0.0
    return float(np.max(vals))


def mina(*args: Any) -> float:
    vals = []
    for arg in args:
        for v in np.asarray(arg).ravel():
            vals.append(_to_float_a(v))
    if not vals:
        return 0.0
    return float(np.min(vals))


def erf(lower: Any, upper: Any | None = None) -> float:
    try:
        lo = float(lower)
        if upper is None:
            return float(math.erf(lo))
        u = float(upper)
        return float(math.erf(u) - math.erf(lo))
    except (ValueError, TypeError):
        return float("nan")


def erfc(x: Any) -> float:
    try:
        return float(math.erfc(float(x)))
    except (ValueError, TypeError):
        return float("nan")


def delta(n1: Any, n2: Any = 0) -> float:
    try:
        return 1.0 if float(n1) == float(n2) else 0.0
    except (ValueError, TypeError):
        return float("nan")


def gestep(number: Any, step: Any = 0) -> float:
    try:
        return 1.0 if float(number) >= float(step) else 0.0
    except (ValueError, TypeError):
        return float("nan")


def sqrtpi(number: Any) -> float:
    try:
        n = float(number)
        if n < 0:
            return float("nan")
        return float(math.sqrt(n * math.pi))
    except (ValueError, TypeError):
        return float("nan")


def bitand(n1: Any, n2: Any) -> float:
    try:
        return float(int(float(n1)) & int(float(n2)))
    except (ValueError, TypeError):
        return float("nan")


def bitor(n1: Any, n2: Any) -> float:
    try:
        return float(int(float(n1)) | int(float(n2)))
    except (ValueError, TypeError):
        return float("nan")


def bitxor(n1: Any, n2: Any) -> float:
    try:
        return float(int(float(n1)) ^ int(float(n2)))
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


def bitrshift(number: Any, shift: Any) -> float:
    try:
        n = int(float(number))
        s = int(float(shift))
        if s < 0:
            return float(n << abs(s))
        return float(n >> s)
    except (ValueError, TypeError):
        return float("nan")


def _to_complex(val: Any) -> 'builtins.complex':
    """Convert Calc complex string (e.g. '1+2i') to Python complex."""
    import builtins
    if isinstance(val, (int, float, builtins.complex)):
        return builtins.complex(val)
    s = str(val).replace("i", "j").replace("I", "j").replace(" ", "")
    try:
        return builtins.complex(s)
    except ValueError:
        raise TypeError("Invalid complex string")


def _from_complex(c: 'builtins.complex', suffix: str = "i") -> str:
    """Convert Python complex to Calc string."""
    import builtins
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


def complex(real_num: Any, imag_num: Any, suffix: Any = "i") -> str:
    try:
        import builtins
        r = float(real_num)
        i = float(imag_num)
        s = str(suffix)
        return _from_complex(builtins.complex(r, i), suffix=s)
    except (ValueError, TypeError):
        return "#VALUE!"


def imabs(inumber: Any) -> float:
    try:
        return float(abs(_to_complex(inumber)))
    except (ValueError, TypeError):
        return float("nan")


def imaginary(inumber: Any) -> float:
    try:
        return float(_to_complex(inumber).imag)
    except (ValueError, TypeError):
        return float("nan")


def imargument(inumber: Any) -> float:
    try:
        import cmath
        return float(cmath.phase(_to_complex(inumber)))
    except (ValueError, TypeError):
        return float("nan")


def imconjugate(inumber: Any) -> str:
    try:
        c = _to_complex(inumber)
        return _from_complex(c.conjugate())
    except (ValueError, TypeError):
        return "#VALUE!"


def imcos(inumber: Any) -> str:
    try:
        import cmath
        c = _to_complex(inumber)
        return _from_complex(cmath.cos(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imdiv(inumber1: Any, inumber2: Any) -> str:
    try:
        import builtins
        c1 = _to_complex(inumber1)
        c2 = _to_complex(inumber2)
        return _from_complex(c1 / c2)
    except (ValueError, TypeError, ZeroDivisionError):
        return "#VALUE!"


def imexp(inumber: Any) -> str:
    try:
        import cmath
        c = _to_complex(inumber)
        return _from_complex(cmath.exp(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imln(inumber: Any) -> str:
    try:
        import cmath
        c = _to_complex(inumber)
        return _from_complex(cmath.log(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imlog10(inumber: Any) -> str:
    try:
        import cmath
        c = _to_complex(inumber)
        return _from_complex(cmath.log10(c))
    except (ValueError, TypeError):
        return "#VALUE!"


def imlog2(inumber: Any) -> str:
    try:
        import cmath
        c = _to_complex(inumber)
        return _from_complex(cmath.log(c, 2))
    except (ValueError, TypeError):
        return "#VALUE!"


def impower(inumber: Any, number: Any) -> str:
    try:
        import builtins
        c = _to_complex(inumber)
        p = float(number)
        return _from_complex(c ** p)
    except (ValueError, TypeError):
        return "#VALUE!"


def improduct(*args: Any) -> str:
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
    try:
        return float(_to_complex(inumber).real)
    except (ValueError, TypeError):
        return float("nan")


def imsin(inumber: Any) -> str:
    try:
        import cmath
        c = _to_complex(inumber)
        return _from_complex(cmath.sin(c))
    except (ValueError, TypeError):
        return "#VALUE!"

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

def mdeterm(matrix: Any) -> float:
    try:
        import numpy as np
        m = np.asarray(matrix, dtype=float)
        if m.ndim > 2:
            m = m[0]
        return float(np.linalg.det(m))
    except Exception:
        return float("nan")

def minverse(matrix: Any) -> Any:
    try:
        import numpy as np
        m = np.asarray(matrix, dtype=float)
        if m.ndim > 2:
            m = m[0]
        return np.linalg.inv(m).tolist()
    except Exception:
        return "#VALUE!"

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

def mtrans(matrix: Any) -> Any:
    try:
        import numpy as np
        m = np.asarray(matrix)
        if m.ndim > 2:
            m = m[0]
        return np.transpose(m).tolist()
    except Exception:
        return "#VALUE!"

def munit(dimension: Any) -> Any:
    try:
        import numpy as np
        return np.eye(int(dimension)).tolist()
    except Exception:
        return "#VALUE!"

def trend(*args: Any) -> Any:
    try:
        import numpy as np
        data_y = np.asarray(args[0]).ravel()
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

        c, _, _, _ = np.linalg.lstsq(np.c_[data_x, np.ones(data_x.shape[0])], data_y, rcond=None)
        return (np.c_[new_data_x, np.ones(new_data_x.shape[0])] @ c).tolist()
    except Exception:
        return "#VALUE!"

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

def confidence(alpha: Any, stddev: Any, size: Any) -> float:
    try:
        from scipy import stats
        import math
        return float(stats.norm.ppf(1 - float(alpha)/2) * float(stddev) / math.sqrt(float(size)))
    except Exception:
        return float("nan")

def critbinom(trials: Any, prob: Any, alpha: Any) -> float:
    try:
        from scipy import stats
        return float(stats.binom.ppf(float(alpha), int(trials), float(prob)))
    except Exception:
        return float("nan")
