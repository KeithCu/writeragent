# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar registry ↔ langdetect profile mapping and venv RPC helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.locale.grammar_proofread_locale import (
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_langdetect_profile,
    get_grammar_detect_language_mode,
    langdetect_profiles_for_grammar_registry,
)


def test_bcp47_to_langdetect_profile_mapping() -> None:
    assert bcp47_to_langdetect_profile("en-US") == "en"
    assert bcp47_to_langdetect_profile("zh-CN") == "zh-cn"
    assert bcp47_to_langdetect_profile("zh-TW") == "zh-tw"
    assert bcp47_to_langdetect_profile("nb-NO") == "no"
    assert bcp47_to_langdetect_profile("nn-NO") == "no"


def test_langdetect_profiles_for_grammar_registry() -> None:
    allowed = langdetect_profiles_for_grammar_registry()
    assert len(allowed) >= 2
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        prof = bcp47_to_langdetect_profile(tag)
        assert prof in allowed, f"missing profile mapping for {tag} -> {prof}"


def test_get_grammar_detect_language_mode_legacy_bool() -> None:
    from plugin.framework import config

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "get_config", lambda _key: True)
        assert get_grammar_detect_language_mode(object()) == "llm"
        mp.setattr(config, "get_config", lambda _key: False)
        assert get_grammar_detect_language_mode(object()) == "off"


def test_get_grammar_detect_language_mode_strings() -> None:
    from plugin.framework import config

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "get_config", lambda _key: "langdetect")
        assert get_grammar_detect_language_mode(object()) == "langdetect"
        mp.setattr(config, "get_config", lambda _key: "off")
        assert get_grammar_detect_language_mode(object()) == "off"


def test_langdetect_rpc_smoke_french() -> None:
    from plugin.embeddings.venv import langdetect_rpc as rpc_mod

    mock_detect = MagicMock(return_value=[SimpleNamespace(lang="fr", prob=0.99)])
    mock_exc = type("LangDetectException", (Exception,), {})
    fake_ld = SimpleNamespace(detect_langs=mock_detect, lang_detect_exception=SimpleNamespace(LangDetectException=mock_exc))
    with patch.dict("sys.modules", {"langdetect": fake_ld, "langdetect.lang_detect_exception": fake_ld.lang_detect_exception}):
        assert rpc_mod.detect_lang_sample("Bonjour le monde.") == "fr-FR"
