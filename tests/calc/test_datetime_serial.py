# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for Calc date/time wire helpers (gate, preserve, elapsed, format runs)."""

from types import SimpleNamespace
import pytest

from plugin.calc.datetime_wire import (
    format_category_from_type,
    iso8601_from_serial,
    coalesce_temporal_apply_rects,
    duration_serial_from_iso,
    horizontal_apply_runs,
    is_elapsed_format_string,
    is_midnight_serial,
    iso_duration_from_serial,
    match_iso_duration,
    match_iso_temporal,
    merge_vertical_apply_rects,
    is_compatible_temporal_template,
    resolve_s25_row_empties,
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
        ("2026-08-08T08:00", "datetime"),
        ("2026-08-08 08:00", "datetime"),
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
        ("duration", 1.25, "time", True),
        ("duration", 1.25, "date", False),
        ("duration", 1.25, None, False),
        ("datetime", 46242.0, "date", True),  # midnight
        ("datetime", 46242.5, "date", False),
        ("datetime", 46242.5, "datetime", True),
        ("datetime", 46242.0, "time", False),
        ("datetime", 46242.5, None, False),  # General / non-temporal
    ],
)
def test_should_preserve_temporal_format(input_cat, serial, dest, preserve):
    assert should_preserve_temporal_format(input_cat, serial, dest) is preserve


@pytest.mark.parametrize(
    "input_cat,template,format_code,compatible",
    [
        ("date", "date", None, True),
        ("date", "datetime", None, False),  # P1 stricter than M1 preserve
        ("date", "time", None, False),
        ("date", None, None, False),
        ("time", "time", None, True),
        ("time", "time", "HH:MM:SS", True),
        ("time", "time", "[HH]:MM:SS", False),  # clock must not inherit elapsed
        ("time", "time", "[H]:MM", False),
        ("time", "date", None, False),
        ("duration", "time", None, True),
        ("duration", "time", "[HH]:MM:SS", True),  # duration may inherit elapsed
        ("duration", "date", None, False),
        ("datetime", "datetime", None, True),
        ("datetime", "date", None, True),
        ("datetime", "time", None, False),
    ],
)
def test_is_compatible_temporal_template(input_cat, template, format_code, compatible):
    assert is_compatible_temporal_template(input_cat, template, format_code) is compatible


@pytest.mark.parametrize(
    "text,ok",
    [
        ("PT30H", True),
        ("PT1H30M", True),
        ("PT45S", True),
        ("PT0S", True),
        ("PT1H30M5S", True),
        ("PT30H0M0S", True),
        ("P1D", False),
        ("PT30H0.5S", False),
        ("-PT1H", False),
        ("PT", False),
        ("30:00", False),
        ("", False),
        ("PT1Y", False),
    ],
)
def test_match_iso_duration_gate(text, ok):
    assert match_iso_duration(text) is ok


def test_duration_serial_iso_round_trip():
    assert abs(duration_serial_from_iso("PT30H") - 1.25) < 1e-12
    assert iso_duration_from_serial(1.25) == "PT30H"
    assert iso_duration_from_serial(1 / 3) == "PT8H"
    assert iso_duration_from_serial(0.0) == "PT0S"
    assert iso_duration_from_serial(8.5 / 24) == "PT8H30M"


def test_iso_duration_from_serial_rejects_negative():
    with pytest.raises(ValueError, match="negative duration serial"):
        iso_duration_from_serial(-1.25)


def test_duration_serial_from_iso_rejects_calendar_duration():
    with pytest.raises(ValueError, match="unsupported calendar duration"):
        duration_serial_from_iso("P1Y")


def test_resolve_s25_row_empties_joins_when_neighbors_agree():
    apply_k = ("apply", 10)
    assert resolve_s25_row_empties([apply_k, "empty", apply_k]) == [apply_k, apply_k, apply_k]


def test_resolve_s25_row_empties_splits_on_disagree():
    apply_k = ("apply", 10)
    preserve = ("preserve", None)
    assert resolve_s25_row_empties([apply_k, "empty", preserve]) == [apply_k, None, preserve]


def test_resolve_s25_row_empties_missing_neighbor_is_hole():
    apply_k = ("apply", 10)
    assert resolve_s25_row_empties([apply_k, "empty"]) == [apply_k, None]
    assert resolve_s25_row_empties(["empty", apply_k]) == [None, apply_k]


def test_horizontal_apply_runs_splits_on_key_and_holes():
    row = [("apply", 1), ("apply", 1), None, ("apply", 2), ("preserve", None), ("apply", 2)]
    assert horizontal_apply_runs(row) == [(0, 1, 1), (3, 3, 2), (5, 5, 2)]


def test_merge_vertical_apply_rects_homogeneous_column():
    row_runs = [[(0, 0, 7)], [(0, 0, 7)], [(0, 0, 7)]]
    assert merge_vertical_apply_rects(row_runs) == [(0, 2, 0, 0, 7)]


def test_merge_vertical_apply_rects_span_mismatch_no_merge():
    row_runs = [[(0, 2, 7)], [(0, 1, 7)]]
    assert merge_vertical_apply_rects(row_runs) == [(0, 0, 0, 2, 7), (1, 1, 0, 1, 7)]


def test_merge_vertical_apply_rects_preserve_gap_splits():
    row_runs = [[(0, 0, 7)], [], [(0, 0, 7)]]
    assert merge_vertical_apply_rects(row_runs) == [(0, 0, 0, 0, 7), (2, 2, 0, 0, 7)]


def test_coalesce_temporal_apply_rects_block_and_s25_join():
    apply_k = ("apply", 5)
    decisions = [
        [apply_k, apply_k, apply_k],
        [apply_k, "empty", apply_k],
        [apply_k, apply_k, apply_k],
    ]
    # Empty joins agreeing neighbors → full 3x3 apply → one rect.
    assert coalesce_temporal_apply_rects(decisions) == [(0, 2, 0, 2, 5)]


def test_coalesce_temporal_apply_rects_checkerboard_no_vertical_merge():
    a = ("apply", 1)
    p = ("preserve", None)
    decisions = [[a, p], [p, a]]
    assert coalesce_temporal_apply_rects(decisions) == [(0, 0, 0, 0, 1), (1, 1, 1, 1, 1)]


def test_calc_serial_iso8601_uses_document_null_date():
    null_date = SimpleNamespace(Year=1899, Month=12, Day=30)
    assert iso8601_from_serial(46237.0, "date", null_date) == "2026-08-03"
    assert iso8601_from_serial(46237.5, "datetime", null_date) == "2026-08-03T12:00:00"
    assert iso8601_from_serial(0.5, "time", null_date) == "12:00:00"
    assert format_category_from_type(2) == "date"
    assert format_category_from_type(5) == "time"  # DEFINED | TIME
    assert format_category_from_type(7) == "datetime"  # DEFINED | DATETIME
    assert format_category_from_type(16) is None


def test_calc_serial_iso8601_rounds_float_noise_to_whole_seconds():
    null_date = SimpleNamespace(Year=1899, Month=12, Day=30)
    # 0.6s past noon rounds up to 12:00:01 (use a small serial so float has room).
    assert iso8601_from_serial(0.5 + 0.6 / 86400.0, "time", null_date) == "12:00:01"
    assert iso8601_from_serial(0.5 + 0.6 / 86400.0, "datetime", null_date) == "1899-12-30T12:00:01"
    # Sub-second float dust must not leak microseconds into ISO.
    dusty = 0.5 + 1e-12
    assert iso8601_from_serial(dusty, "time", null_date) == "12:00:00"
    assert "." not in iso8601_from_serial(dusty, "datetime", null_date).split("T", 1)[1]
