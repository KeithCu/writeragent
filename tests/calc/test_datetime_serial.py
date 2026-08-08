# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for Calc date/time wire helpers (gate, preserve, elapsed, format runs)."""

import pytest

from plugin.calc.datetime_wire import (
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
    "input_cat,template,compatible",
    [
        ("date", "date", True),
        ("date", "datetime", True),
        ("date", "time", False),
        ("date", None, False),
        ("time", "time", True),
        ("time", "date", False),
        ("duration", "time", True),
        ("duration", "date", False),
        ("datetime", "datetime", True),
        ("datetime", "date", True),
        ("datetime", "time", False),
    ],
)
def test_is_compatible_temporal_template(input_cat, template, compatible):
    assert is_compatible_temporal_template(input_cat, template) is compatible


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
