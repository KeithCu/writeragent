# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for =PY() date and time ingress/egress helpers."""

from __future__ import annotations

import datetime
import pytest

from plugin.calc.python.function import _coerce_spill_value


def test_coerce_spill_value_dates_and_datetimes():
    null_dt = datetime.date(1899, 12, 30)

    # 1. datetime.date -> 46242.0 (2026-08-08)
    d = datetime.date(2026, 8, 8)
    val, meta = _coerce_spill_value(d, null_dt)
    assert val == 46242.0
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "date"
    assert meta["serial"] == 46242.0

    # 2. datetime.datetime -> 46242.5 (2026-08-08 12:00:00)
    dt = datetime.datetime(2026, 8, 8, 12, 0, 0)
    val, meta = _coerce_spill_value(dt, null_dt)
    assert val == 46242.5
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "datetime"
    assert meta["serial"] == 46242.5

    # 3. datetime.time -> 0.5 (12:00:00)
    t = datetime.time(12, 0, 0)
    val, meta = _coerce_spill_value(t, null_dt)
    assert val == pytest.approx(0.5)
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "time"

    # 4. datetime.timedelta -> 1.25 (30 hours)
    td = datetime.timedelta(hours=30)
    val, meta = _coerce_spill_value(td, null_dt)
    assert val == pytest.approx(1.25)
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "duration"


def test_coerce_spill_value_iso_strings():
    null_dt = datetime.date(1899, 12, 30)

    # ISO date string
    val, meta = _coerce_spill_value("2026-08-08", null_dt)
    assert val == 46242.0
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "date"

    # ISO datetime string
    val, meta = _coerce_spill_value("2026-08-08T12:00:00", null_dt)
    assert val == 46242.5
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "datetime"

    # ISO duration string
    val, meta = _coerce_spill_value("PT30H", null_dt)
    assert val == pytest.approx(1.25)
    assert meta["is_temporal"] is True
    assert meta["input_category"] == "duration"


def test_coerce_spill_value_non_temporal_pass_through():
    null_dt = datetime.date(1899, 12, 30)

    # Numbers
    val, meta = _coerce_spill_value(123.45, null_dt)
    assert val == 123.45
    assert meta["is_temporal"] is False

    # Booleans
    val, meta = _coerce_spill_value(True, null_dt)
    assert val == 1.0
    assert meta["is_temporal"] is False

    # Plain text strings
    val, meta = _coerce_spill_value("Hello World", null_dt)
    assert val == "Hello World"
    assert meta["is_temporal"] is False

    # Blanks
    val, meta = _coerce_spill_value(None, null_dt)
    assert val == ""
    assert meta["is_empty"] is True
