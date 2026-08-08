# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for Calc date/time wire helpers (gate, preserve, elapsed)."""

import pytest

from plugin.calc.datetime_wire import (
    is_elapsed_format_string,
    is_midnight_serial,
    match_iso_temporal,
    should_preserve_temporal_format,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-08-08", "date"),
        ("08:00", "time"),
        ("08:00:00", "time"),
        ("2026-08-08T08:00:00", "datetime"),
        ("2026-08-08 08:00:00", "datetime"),
        ("  2026-08-08  ", "date"),
        ("2026-8-8", None),
        ("08/05/2026", None),
        ("05.08.2026", None),
        ("08:00 AM", None),
        ("08:00:00.500", None),
        ("24:00", None),
        ("30:00", None),
        ("2026-08-08T08:00:00Z", None),
        ("2026-08-08T08:00:00-04:00", None),
        ("2026-13-45", None),
        ("Hello World", None),
        ("=SUM(A1:A10)", None),
        ("123", None),
        ("", None),
        ("2026-02-30", "date"),  # shape ok; Calc rejects later
    ],
)
def test_match_iso_temporal_gate(text, expected):
    assert match_iso_temporal(text) == expected


@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("[HH]:MM:SS", True),
        ("[H]:MM", True),
        ("[MM]:SS", True),
        ("[TT]:MM:SS", True),
        ("HH:MM:SS", False),
        ("YYYY-MM-DD", False),
        ("", False),
        (None, False),
        (4, False),
    ],
)
def test_is_elapsed_format_string(fmt, expected):
    assert is_elapsed_format_string(fmt) is expected


def test_is_midnight_serial():
    assert is_midnight_serial(46242.0) is True
    assert is_midnight_serial(46242.5) is False
    assert is_midnight_serial(0.0) is True
    assert is_midnight_serial(1 / 3) is False


@pytest.mark.parametrize(
    "input_cat,serial,dest,preserve",
    [
        ("date", 46242.0, "date", True),
        ("date", 46242.0, "datetime", True),
        ("date", 46242.0, "time", False),
        ("date", 46242.0, None, False),
        ("time", 0.333, "time", True),
        ("time", 0.333, "date", False),
        ("datetime", 46242.0, "date", True),  # midnight
        ("datetime", 46242.5, "date", False),
        ("datetime", 46242.5, "datetime", True),
        ("datetime", 46242.0, "time", False),
    ],
)
def test_should_preserve_temporal_format(input_cat, serial, dest, preserve):
    assert should_preserve_temporal_format(input_cat, serial, dest) is preserve
