# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.results."""

from __future__ import annotations

import inspect

import pytest

from plugin.scripting.venv.results import HelperError, catch_helper_error


def test_catch_helper_error_returns_error_result() -> None:
    @catch_helper_error
    def foo() -> dict[str, object]:
        raise HelperError("UNKNOWN_COLUMN", "Unknown columns: x")

    assert foo() == {
        "status": "error",
        "message": "Unknown columns: x",
        "code": "UNKNOWN_COLUMN",
        "helper": "foo",
    }


def test_catch_helper_error_passes_plain_errors() -> None:
    @catch_helper_error
    def raises_value() -> dict[str, object]:
        raise ValueError("plain value error")

    @catch_helper_error
    def raises_key() -> dict[str, object]:
        raise KeyError("missing")

    with pytest.raises(ValueError, match="plain value error"):
        raises_value()
    with pytest.raises(KeyError, match="missing"):
        raises_key()


def test_catch_helper_error_keeps_signature_and_doc() -> None:
    @catch_helper_error
    def sample(data: object, *, headers: bool = True) -> dict[str, object]:
        """Sample docstring."""
        return {"status": "ok", "data": data, "headers": headers}

    sig = inspect.signature(sample)
    assert "headers" in sig.parameters
    assert sig.parameters["headers"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sample.__doc__ == "Sample docstring."


def test_helper_error_is_value_error() -> None:
    err = HelperError("INVALID_PARAM", "m")
    assert isinstance(err, ValueError)
    assert err.code == "INVALID_PARAM"
    assert str(err) == "m"
