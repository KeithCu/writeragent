# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for detected-locale normalization helpers."""

from __future__ import annotations

from plugin.writer.locale.grammar_proofread_locale import (
    grammar_bcp47_tags_match,
    normalize_detected_bcp47,
)


def test_normalize_detected_bcp47_registry_and_shorthand() -> None:
    assert normalize_detected_bcp47("ja-JP") == "ja-JP"
    assert normalize_detected_bcp47("ja") == "ja-JP"
    assert normalize_detected_bcp47("en") == "en-US"


def test_grammar_bcp47_tags_match() -> None:
    assert grammar_bcp47_tags_match("ja", "ja-JP")
    assert grammar_bcp47_tags_match("ja-JP", "ja-JP")
    assert not grammar_bcp47_tags_match("ja-JP", "zh-CN")
