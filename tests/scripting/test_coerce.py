# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grid numeric-string coercion."""

from __future__ import annotations

import pytest

from plugin.scripting.venv.coerce import _parse_numeric_string


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-5", -5.0),
        ("-1,234.50", -1234.5),
        ("$-123", -123.0),
        ("-$123", -123.0),
        ("-10%", -0.1),
        ("+7", 7.0),
        ("$1,234", 1234.0),
        ("(5)", None),
        ("+-5", None),
    ],
)
def test_parse_numeric_string_keeps_sign(text: str, expected: float | None):
    assert _parse_numeric_string(text) == expected
