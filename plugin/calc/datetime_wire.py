# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Pure helpers for Calc date/time LLM wire contract (gate, preserve, elapsed, format runs).

No UNO imports — unit-testable. See docs/calc-date-time-handling.md.
Duration parse uses vendored ``isodate``; emit is a thin hours-may-exceed-24 formatter
so the wire stays ``PT30H`` rather than ``P1DT6H``.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TypeAlias

from isodate import parse_duration

# Per-cell format decision before/after S25 empty resolve.
# ``"empty"`` is input-only; resolved rows use None for an unbridged hole.
FormatDecision: TypeAlias = tuple[str, int | None] | str | None
ApplyRun: TypeAlias = tuple[int, int, int]  # c0, c1, key (inclusive, relative)
ApplyRect: TypeAlias = tuple[int, int, int, int, int]  # r0, r1, c0, c1, key

# Bracketed alphabetic time unit: [HH], [H], [MM], [SS], localized [TT], etc.
# Used to classify elapsed formats (DURATION bit never fires).
_ELAPSED_BRACKET_RE = re.compile(r"\[[A-Za-z]+")

_DATE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")
_DATETIME_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[T ]([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")
# Strict time-only duration: PT + at least one integer H/M/S. No Y/M/W/D, fractions, or sign.
_DURATION_RE = re.compile(r"^PT(?=\d+[HMS])(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def is_elapsed_format_string(format_string: object) -> bool:
    """True when FormatString uses a bracketed elapsed time unit (any locale letters)."""
    if not isinstance(format_string, str) or not format_string:
        return False
    return _ELAPSED_BRACKET_RE.search(format_string) is not None


def match_iso_temporal(text: str) -> str | None:
    """Return ``date`` / ``time`` / ``datetime`` if *text* matches the strict ISO gate.

    Shape filter only — calendar validity is left to Calc. Evaluate datetime
    before date before time so a T/space datetime is never classified as date.
    """
    if not isinstance(text, str):
        return None
    val = text.strip()
    if not val:
        return None
    # O(1) reject for prose / plain numbers before regex.
    if not any(c in val for c in ("-", ":")):
        return None
    if _DATETIME_RE.match(val):
        return "datetime"
    if _DATE_RE.match(val):
        return "date"
    if _TIME_RE.match(val):
        return "time"
    return None


def is_midnight_serial(serial: float) -> bool:
    """True when *serial* is an exact whole day at read-path one-second precision."""
    return round(float(serial) * 86400.0) % 86400 == 0


def match_iso_duration(text: str) -> bool:
    """True when *text* matches the strict PT duration wire gate (integer H/M/S only)."""
    if not isinstance(text, str):
        return False
    val = text.strip()
    if not val or "." in val or "," in val or val.startswith("-"):
        return False
    return _DURATION_RE.match(val) is not None


def duration_serial_from_iso(text: str) -> float:
    """Parse a gated ``PT…`` string to a Calc day serial via vendored isodate."""
    td = parse_duration(text.strip())
    if not isinstance(td, timedelta):
        # Calendar Duration (Y/M) — rejected by the gate; defend against API misuse.
        raise ValueError(f"unsupported calendar duration: {text!r}")
    return float(td.total_seconds()) / 86400.0


def iso_duration_from_serial(serial: float) -> str:
    """Emit compact ``PTnHnMnS`` with hours allowed to exceed 24 (not ``P1DT6H``).

    Negative serials are out of scope for the wire contract (gate rejects ``-PT…``);
    raise rather than silently emitting a positive duration.
    """
    value = float(serial)
    if value < 0:
        raise ValueError(f"negative duration serial is out of scope: {serial!r}")
    total = round(value * 86400.0)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = ["PT"]
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if seconds or not (hours or minutes):
        parts.append(f"{seconds}S")
    return "".join(parts)


def should_preserve_temporal_format(input_category: str, serial: float, dest_category: str | None) -> bool:
    """M1 preserve predicate: keep destination NumberFormat when category-compatible."""
    if dest_category is None:
        return False
    if input_category == "date" and dest_category in ("date", "datetime"):
        return True
    if input_category == "time" and dest_category == "time":
        return True
    if input_category == "duration" and dest_category == "time":
        # Clock or elapsed TIME columns (S16); elapsed FormatString is still Type TIME.
        return True
    if input_category == "datetime" and dest_category == "datetime":
        return True
    if input_category == "datetime" and dest_category == "date" and is_midnight_serial(serial):
        return True
    return False


def resolve_s25_row_empties(row: list[FormatDecision]) -> list[FormatDecision]:
    """Join empty cells only when both adjacent non-empty coerced neighbors agree (S25)."""
    resolved: list[FormatDecision] = []
    n = len(row)
    for col_i, dec in enumerate(row):
        if dec != "empty":
            resolved.append(dec)
            continue
        left: FormatDecision = None
        right: FormatDecision = None
        for j in range(col_i - 1, -1, -1):
            if row[j] not in (None, "empty"):
                left = row[j]
                break
        for j in range(col_i + 1, n):
            if row[j] not in (None, "empty"):
                right = row[j]
                break
        if left is not None and left == right:
            resolved.append(left)
        else:
            resolved.append(None)
    return resolved


def horizontal_apply_runs(resolved_row: list[FormatDecision]) -> list[ApplyRun]:
    """Maximal contiguous ``(\"apply\", key)`` spans in a resolved row (relative cols)."""
    runs: list[ApplyRun] = []
    col_i = 0
    n = len(resolved_row)
    while col_i < n:
        dec = resolved_row[col_i]
        if not isinstance(dec, tuple) or dec[0] != "apply" or not isinstance(dec[1], int):
            col_i += 1
            continue
        key = dec[1]
        run_end = col_i
        while run_end + 1 < n and resolved_row[run_end + 1] == ("apply", key):
            run_end += 1
        runs.append((col_i, run_end, key))
        col_i = run_end + 1
    return runs


def merge_vertical_apply_rects(row_runs: list[list[ApplyRun]]) -> list[ApplyRect]:
    """Merge identical ``(c0, c1, key)`` spans across consecutive rows into 2D rects.

    A pending span extends only while the next row has that exact span. Preserve
    gaps, span mismatches, and key changes close the pending rect (S8).
    """
    if not row_runs:
        return []
    # span (c0, c1, key) -> start_row
    pending: dict[ApplyRun, int] = {}
    rects: list[ApplyRect] = []

    def _close(span: ApplyRun, end_row: int) -> None:
        r0 = pending.pop(span)
        c0, c1, key = span
        rects.append((r0, end_row, c0, c1, key))

    for r, runs in enumerate(row_runs):
        current = set(runs)
        for span in list(pending):
            if span not in current:
                _close(span, r - 1)
        for span in current:
            if span not in pending:
                pending[span] = r

    last = len(row_runs) - 1
    for span in list(pending):
        _close(span, last)
    return rects


def coalesce_temporal_apply_rects(decisions: list[list[FormatDecision]]) -> list[ApplyRect]:
    """S25-resolve each row, find horizontal apply runs, then vertically merge."""
    resolved_rows = [resolve_s25_row_empties(row) for row in decisions]
    row_runs = [horizontal_apply_runs(r) for r in resolved_rows]
    return merge_vertical_apply_rects(row_runs)
