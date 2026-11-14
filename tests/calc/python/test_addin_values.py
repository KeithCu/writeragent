# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc =PY() error code mapping and value formatting in addin_values."""

from __future__ import annotations

import datetime
import logging
import math
import struct
from typing import Any

import pytest

from plugin.calc.python import addin_values
from plugin.scripting.venv.coerce import LO_ERROR_TOKENS

_VENV_GUIDANCE = "Error: Python venv not found. Open Settings → Python, set the venv path, then Test."
_TIMEOUT_GUIDANCE = "Error: Python timed out. Open Settings → Python to raise the timeout, or Test the venv."


def _nan_low16(val: float) -> int:
    raw = struct.unpack("Q", struct.pack("d", val))[0]
    return raw & 0xFFFF


def test_calc_error_tokens_agree_with_child() -> None:
    assert LO_ERROR_TOKENS == frozenset(addin_values._CALC_ERROR_TOKEN_CODES)
    assert len(addin_values._CALC_ERROR_TOKEN_CODES) == 8
    assert set(addin_values._CALC_ERROR_TOKEN_CODES.values()) == {519, 503, 532, 32767, 524, 525, 521}


@pytest.mark.parametrize(
    ("res", "expected"),
    [
        (
            {
                "status": "error",
                "code": "VENV_NOT_FOUND",
                "message": "No python executable found under configured venv: '/missing'",
            },
            _VENV_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "VENV_NOT_FOUND",
                "message": "Could not resolve a Python interpreter (sys.executable missing, ...)",
            },
            _VENV_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "VENV_TIMEOUT",
                "message": "Python worker failed: timed out after 30 seconds Shared Python process restarted (all workbooks).",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "Code execution exceeded the maximum execution time of 30 seconds",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "Insufficient time to spawn worker.",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "python worker spawn handshake timed out",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "USER_STOPPED",
                "message": "Stopped by user.",
            },
            "Error: Stopped by user.",
        ),
        (
            {
                "status": "error",
                "code": "VENV_EXEC_ERROR",
                "message": "Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero",
            },
            532,
        ),
        (
            {
                "status": "error",
                "code": "VENV_EXEC_ERROR",
                "message": "result must not be a socket timeout object",
            },
            519,
        ),
        (
            {
                "status": "error",
                "code": "WORKER_IPC_ERROR",
                "message": "Python worker failed: host RPC response timed out after 10 seconds ...",
            },
            519,
        ),
        (
            {
                "status": "error",
                "message": "Python worker failed: timed out after 10 seconds",
            },
            519,
        ),
        (
            {
                "status": "error",
                "code": "CANCELLED",
                "message": "any",
            },
            519,
        ),
    ],
)
def test_addin_result_for_worker_status_by_code(
    res: dict[str, Any],
    expected: str | int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out = addin_values.addin_result_for_worker_status(res)

    error_records = [
        r for r in caplog.records
        if r.name == "plugin.calc.python.addin_values" and r.levelno >= logging.ERROR
    ]
    if isinstance(expected, str):
        assert out == expected
        assert error_records == []
    else:
        assert isinstance(out, float)
        assert math.isnan(out)
        assert _nan_low16(out) == expected
        assert len(error_records) == 1


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda: addin_values.format_addin_error_for_display(RuntimeError("")), "Error: RuntimeError"),
        (lambda: addin_values.format_addin_error_for_display(RuntimeError("x")), "Error: x"),
        (
            lambda: addin_values.addin_result_for_worker_status({"code": "USER_STOPPED", "message": "#SPILL!"}),
            "#SPILL!",
        ),
        (
            lambda: addin_values.addin_result_for_worker_status({"code": "USER_STOPPED", "message": ""}),
            "Error: Unknown error",
        ),
        (
            lambda: addin_values.addin_result_for_worker_status({"code": "USER_STOPPED", "message": "Error: x"}),
            "Error: x",
        ),
    ],
)
def test_error_cell_text_prefix_rules(call: Any, expected: str) -> None:
    assert call() == expected


def test_format_addin_error_for_display_keeps_host_wording() -> None:
    out = addin_values.format_addin_error_for_display(
        RuntimeError("marshal post timed out: AsyncCallback unavailable")
    )
    assert out == "Error: marshal post timed out: AsyncCallback unavailable"
    assert "Settings" not in out


@pytest.mark.parametrize(
    ("raw", "expected_calc", "expected_kind", "expected_cat", "expected_err", "expected_reg"),
    [
        (None, "", "e", None, None, None),
        ("", "", "e", None, None, None),
        ("  ", "", "e", None, None, None),
        (True, 1.0, "n", None, None, 1.0),
        (7, 7.0, "n", None, None, 7.0),
        (10**400, str(10**400), "s", None, None, str(10**400)),
        (123.45, 123.45, "n", None, None, 123.45),
        ("Hello World", "Hello World", "s", None, None, "Hello World"),
        ("#N/A", None, "err", None, 32767, 32767),
        ("#VALUE!", "", "err", None, 519, 519),
        (datetime.date(2026, 8, 8), 46242.0, "n", "date", None, 46242.0),
        (datetime.datetime(2026, 8, 8, 12), 46242.5, "n", "datetime", None, 46242.5),
        (
            datetime.datetime(2026, 8, 8, 12, tzinfo=datetime.timezone.utc),
            46242.5,
            "n",
            "datetime",
            None,
            46242.5,
        ),
        (datetime.time(12), 0.5, "n", "time", None, 0.5),
        (datetime.timedelta(hours=30), 1.25, "n", "duration", None, 1.25),
        ("2026-08-08", 46242.0, "n", "date", None, 46242.0),
        ("2026-08-08T12:00:00", 46242.5, "n", "datetime", None, 46242.5),
        ("2026-08-08 12:00:00", 46242.5, "n", "datetime", None, 46242.5),
        ("12:30:00", 45000.0 / 86400.0, "n", "time", None, 45000.0 / 86400.0),
        ("PT30H", 1.25, "n", "duration", None, 1.25),
        ("2026-02-30", "2026-02-30", "s", None, None, "2026-02-30"),
    ],
)
def test_coerce_spill_value_record(
    raw: Any,
    expected_calc: Any,
    expected_kind: str,
    expected_cat: str | None,
    expected_err: int | None,
    expected_reg: Any,
) -> None:
    rec = addin_values.coerce_spill_value(raw)
    if isinstance(expected_calc, float):
        assert rec.calc_value == pytest.approx(expected_calc, rel=1e-9, abs=1e-9)
        assert rec.registry_value == pytest.approx(expected_reg, rel=1e-9, abs=1e-9)
    else:
        assert rec.calc_value == expected_calc
        assert rec.registry_value == expected_reg
    assert rec.kind == expected_kind
    assert rec.temporal_category == expected_cat
    assert rec.error_code == expected_err


def test_to_stdlib_temporal_and_both_ladders() -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")

    assert addin_values.to_stdlib_temporal(pd.NaT) is None
    assert addin_values.to_stdlib_temporal(pd.NA) is None
    assert addin_values.to_stdlib_temporal(np.datetime64("NaT")) is None
    assert addin_values.to_stdlib_temporal(np.timedelta64("NaT")) is None

    assert addin_values.to_stdlib_temporal(np.datetime64("2026-06-25")) == datetime.date(2026, 6, 25)
    assert addin_values.to_stdlib_temporal(np.datetime64("2026-06-25T00:00:00.500", "ms")) == datetime.datetime(
        2026, 6, 25, 0, 0, 0, 500000
    )
    ns_dt = np.datetime64("2026-06-25T00:00:00.500", "ns")
    assert addin_values.to_stdlib_temporal(ns_dt) == str(ns_dt)
    assert addin_values.to_stdlib_temporal(np.timedelta64(36, "h")) == datetime.timedelta(hours=36)
    assert addin_values.to_stdlib_temporal(np.timedelta64(500, "ns")) == "500 nanoseconds"

    ts = pd.Timestamp("2026-06-25 12:00:00")
    assert addin_values.to_stdlib_temporal(ts) is ts
    assert addin_values.to_stdlib_temporal(5) == 5

    assert addin_values.to_calc_compatible(np.datetime64("2026-06-25")) == "2026-06-25"
    assert addin_values.to_calc_compatible(np.timedelta64(36, "h")) == 1.5

    date_rec = addin_values.coerce_spill_value(np.datetime64("2026-06-25"))
    assert date_rec.kind == "n"
    assert date_rec.temporal_category == "date"

    nat_rec = addin_values.coerce_spill_value(pd.NaT)
    assert nat_rec.kind == "e"
    assert nat_rec.calc_value == ""

    ns_rec = addin_values.coerce_spill_value(ns_dt)
    assert ns_rec.kind == "s"
    assert ns_rec.calc_value == str(ns_dt)
