# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for extract_sheet_layout leaf helper."""

from __future__ import annotations

from plugin.scripting.sheet_layout import extract_sheet_layout


def test_extract_sheet_layout_copies_present_keys_raw() -> None:
    source = {"headers": False, "header_row": "abc", "helper": "describe_data"}
    assert extract_sheet_layout(source) == {"headers": False, "header_row": "abc"}

    res = extract_sheet_layout({"headers": None})
    assert res == {"headers": None}
    assert "header_row" not in res


def test_extract_sheet_layout_non_dict_is_empty() -> None:
    assert extract_sheet_layout(None) == {}
    assert extract_sheet_layout([]) == {}
    assert extract_sheet_layout("headers") == {}
