# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.writer.locale.grammar_settings."""

from __future__ import annotations

import pytest

from plugin.writer.locale.grammar_settings import detect_language_mode


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(True, "llm", id="legacy_bool_true"),
        pytest.param(False, "off", id="legacy_bool_false"),
        pytest.param("langdetect", "langdetect", id="string_langdetect"),
        pytest.param("off", "off", id="string_off"),
    ],
)
def test_detect_language_mode(value: object, expected: str) -> None:
    from plugin.framework import config

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "get_config", lambda _key: value)
        assert detect_language_mode() == expected
