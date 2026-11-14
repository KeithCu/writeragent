# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ascii_bounded and str_bounded helpers."""

from __future__ import annotations

from plugin.framework.deal_shim import (
    DEAL_MAX_CELL_REF,
    ascii_bounded,
    str_bounded,
)


def test_ascii_bounded_valid_ascii() -> None:
    assert ascii_bounded("A1", 8) is True
    assert ascii_bounded("A0", 8) is True
    assert ascii_bounded("", 8) is True
    assert ascii_bounded("", 8, min_len=1) is False


def test_ascii_bounded_unicode_rejected() -> None:
    assert ascii_bounded("A🯰", 8) is False
    assert ascii_bounded("é", 8) is False


def test_ascii_bounded_length_limits() -> None:
    # Pytest/debug CELL_REF is 256 (sheet-qualified live ranges); not the old 32.
    assert ascii_bounded("A" * DEAL_MAX_CELL_REF, DEAL_MAX_CELL_REF) is True
    assert ascii_bounded("A" * (DEAL_MAX_CELL_REF + 1), DEAL_MAX_CELL_REF) is False
    assert ascii_bounded("abc", 5, min_len=4) is False
    assert ascii_bounded("abcd", 5, min_len=4) is True


def test_ascii_bounded_non_string_types() -> None:
    assert ascii_bounded(None, 8) is False
    assert ascii_bounded(123, 8) is False
    assert ascii_bounded(["A1"], 8) is False


def test_str_bounded_allows_unicode() -> None:
    assert str_bounded("✓ Copied!", 64) is True
    assert str_bounded("Testing…", 64) is True
    assert str_bounded("A🯰", 8) is True
    assert str_bounded("", 8) is True
    assert str_bounded("é" * 9, 8) is False
    assert str_bounded(None, 8) is False
    assert str_bounded(123, 8) is False
