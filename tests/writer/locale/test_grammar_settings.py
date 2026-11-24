# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.writer.locale.grammar_settings."""

from __future__ import annotations

import pytest

from plugin.writer.locale.grammar_settings import detect_language_mode, engine_settings_changed


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


@pytest.mark.parametrize(
    "keys, active_provider, grammar_model, expected",
    [
        pytest.param({"doc.grammar_proofreader_enabled"}, "harper", "", True, id="engine_key_harper"),
        pytest.param({"doc.grammar_proofreader_model"}, "llm", "gpt-4o", True, id="grammar_model_key_llm"),
        pytest.param({"endpoint"}, "llm", "", True, id="endpoint_llm"),
        pytest.param({"endpoint"}, "llm", "gpt-4o", True, id="endpoint_llm_explicit_model"),
        pytest.param({"api_keys_by_endpoint"}, "llm", "", True, id="api_key_llm"),
        pytest.param({"api_keys_by_endpoint"}, "llm", "gpt-4o", True, id="api_key_llm_explicit_model"),
        pytest.param({"api_keys_by_endpoint"}, "harper", "", False, id="api_key_harper"),
        pytest.param({"api_keys_by_endpoint"}, "off", "", False, id="api_key_off"),
        pytest.param({"text_model"}, "llm", "", True, id="text_model_llm_empty_grammar_model"),
        pytest.param({"text_model"}, "llm", "   ", True, id="text_model_llm_blank_grammar_model"),
        pytest.param({"text_model"}, "llm", "gpt-4o", False, id="text_model_llm_explicit_grammar_model"),
        pytest.param({"endpoint"}, "harper", "", False, id="endpoint_harper"),
        pytest.param({"text_model"}, "harper", "", False, id="text_model_harper"),
        pytest.param({"endpoint"}, "off", "", False, id="endpoint_off"),
        pytest.param({"text_model"}, "off", "", False, id="text_model_off"),
        pytest.param(set(), "llm", "", False, id="empty_keys"),
    ],
)
def test_engine_settings_changed(
    keys: set[str], active_provider: str, grammar_model: str, expected: bool
) -> None:
    from plugin.framework import config
    from plugin.writer.locale import grammar_settings

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(grammar_settings, "provider", lambda: active_provider)
        mp.setattr(config, "get_config", lambda _key: grammar_model)
        assert engine_settings_changed(keys) is expected
