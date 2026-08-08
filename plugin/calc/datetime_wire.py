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
"""Pure helpers for Calc date/time LLM wire contract (gate, preserve, elapsed).

No UNO imports — unit-testable. See docs/calc-date-time-handling.md.
"""

from __future__ import annotations

import re

# Bracketed alphabetic time unit: [HH], [H], [MM], [SS], localized [TT], etc.
# Used to skip ISO clock enrichment for elapsed formats (DURATION bit never fires).
_ELAPSED_BRACKET_RE = re.compile(r"\[[A-Za-z]+")

_DATE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")
_DATETIME_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[T ]([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


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


def should_preserve_temporal_format(input_category: str, serial: float, dest_category: str | None) -> bool:
    """M1 preserve predicate: keep destination NumberFormat when category-compatible."""
    if dest_category is None:
        return False
    if input_category == "date" and dest_category in ("date", "datetime"):
        return True
    if input_category == "time" and dest_category == "time":
        return True
    if input_category == "datetime" and dest_category == "datetime":
        return True
    if input_category == "datetime" and dest_category == "date" and is_midnight_serial(serial):
        return True
    return False
