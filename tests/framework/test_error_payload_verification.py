# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for errors.format_error_payload."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.errors import (
    ConfigError,
    ToolError,
    ToolResult,
    ToolSuccess,
    WriterAgentException,
    format_error_message,
    format_error_payload,
)


def test_format_error_message_basic() -> None:
    msg = format_error_message(ValueError("connection failed"))
    assert isinstance(msg, str)
    assert len(msg) > 0


def test_tool_result_typeddict_status_hints_are_str() -> None:
    """CrossHair get_type_hints on Literal TypedDict fields TypeErrors; keep status as str."""
    from typing import get_type_hints

    assert get_type_hints(ToolSuccess)["status"] is str
    assert get_type_hints(ToolError)["status"] is str
    assert get_type_hints(ToolResult)["status"] is str


@given(msg=st.text(max_size=40))
@settings(max_examples=40)
def test_hypothesis_format_error_message_returns_str(msg: str) -> None:
    result = format_error_message(RuntimeError(msg))
    assert isinstance(result, str)


_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGET = "plugin.framework.errors.format_error_payload"


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def test_generic_exception_internal_error() -> None:
    payload = format_error_payload(ValueError("boom"))
    assert payload["status"] == "error"
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["message"] == "boom"
    assert payload["details"]["type"] == "ValueError"


def test_writeragent_exception_preserves_code() -> None:
    payload = format_error_payload(ConfigError("bad cfg", code="CONFIG_ERROR"))
    assert payload["status"] == "error"
    assert payload["code"] == "CONFIG_ERROR"
    assert "message" in payload


def test_writeragent_exception_with_details() -> None:
    payload = format_error_payload(WriterAgentException("x", code="X", details={"a": 1}))
    assert payload["code"] == "X"
    assert payload["details"] == {"a": 1}


@given(msg=st.text(max_size=40))
@settings(max_examples=40)
def test_hypothesis_generic_shape(msg: str) -> None:
    payload = format_error_payload(RuntimeError(msg))
    assert payload["status"] == "error"
    assert payload["code"] == "INTERNAL_ERROR"
    assert "code" in payload and "message" in payload
    assert payload["details"]["type"] == "RuntimeError"


@given(code=st.sampled_from(["INTERNAL_ERROR", "CONFIG_ERROR", "NETWORK_ERROR", "CUSTOM"]))
@settings(max_examples=20)
def test_hypothesis_wa_code_preserved(code: str) -> None:
    payload = format_error_payload(WriterAgentException("m", code=code))
    assert payload["code"] == code


@pytest.mark.slow
def test_crosshair_format_error_payload_fqn_if_available() -> None:
    pytest.skip("CrossHair concolic execution on Pygments internal exceptions is slow; run via make crosshair-check.")
