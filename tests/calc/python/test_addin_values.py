# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc =PY() error code mapping and value formatting in addin_values."""

from __future__ import annotations

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
                "code": "VENV_TIMEOUT",
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
    ("raw", "expected"),
    [
        ("", "Error: Unknown error"),
        ("Error: x", "Error: x"),
        ("#SPILL!", "#SPILL!"),
        ("x", "Error: x"),
    ],
)
def test_error_cell_text_prefix_rules(raw: str, expected: str) -> None:
    assert addin_values._error_cell_text(raw) == expected


def test_format_addin_error_for_display_keeps_host_wording() -> None:
    out = addin_values.format_addin_error_for_display(
        RuntimeError("marshal post timed out: AsyncCallback unavailable")
    )
    assert out == "Error: marshal post timed out: AsyncCallback unavailable"
    assert "Settings" not in out
