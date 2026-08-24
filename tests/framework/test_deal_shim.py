# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ascii_bounded helper and deal_shim constants."""

from __future__ import annotations

from plugin.framework.deal_shim import (
    DEAL_MAX_BACKOFF,
    DEAL_MAX_BACKOFF_FACTOR,
    DEAL_MAX_CELL_REF,
    DEAL_MAX_COL_LETTERS,
    DEAL_MAX_ORIGIN,
    DEAL_MAX_PATH,
    DEAL_MAX_PLACEHOLDER_INDEX,
    DEAL_MAX_RETRY,
    DEAL_MAX_ROW_INDEX,
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SHAPE_RANK,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    DEAL_MAX_URL,
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
    assert ascii_bounded("A" * 32, DEAL_MAX_CELL_REF) is True
    assert ascii_bounded("A" * 33, DEAL_MAX_CELL_REF) is False
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


def test_deal_shim_constants() -> None:
    assert DEAL_MAX_COL_LETTERS == 3
    assert DEAL_MAX_CELL_REF == 32
    assert DEAL_MAX_TOKEN == 64
    assert DEAL_MAX_ORIGIN == 256
    assert DEAL_MAX_URL == 2048
    assert DEAL_MAX_PATH == 4096
    assert DEAL_MAX_SOURCE == 64
    assert DEAL_MAX_ROW_INDEX == 1000
    assert DEAL_MAX_PLACEHOLDER_INDEX == 1024
    assert DEAL_MAX_SHAPE_RANK == 4
    assert DEAL_MAX_SHAPE_DIM == 256
    assert DEAL_MAX_RETRY == 8
    assert DEAL_MAX_BACKOFF == 300.0
    assert DEAL_MAX_BACKOFF_FACTOR == 10.0
