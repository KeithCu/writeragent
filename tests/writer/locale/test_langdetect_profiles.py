# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Language detection mode and venv RPC helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.locale.grammar_worker import (
    get_grammar_detect_language_mode,
)


@pytest.mark.parametrize(
    "value, expected, value_2",
    [
        pytest.param(True, "llm", False, id="test_get_grammar_detect_language_mode_legacy_bool"),
        pytest.param("langdetect", "langdetect", "off", id="test_get_grammar_detect_language_mode_strings"),
    ],
)
def test_get_grammar_detect_language_mode_legacy_bool(value, expected, value_2) -> None:
    from plugin.framework import config

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "get_config", lambda _key: value)
        assert get_grammar_detect_language_mode(object()) == expected
        mp.setattr(config, "get_config", lambda _key: value_2)
        assert get_grammar_detect_language_mode(object()) == "off"

def test_langdetect_rpc_smoke_french() -> None:
    from plugin.embeddings.venv import langdetect_rpc as rpc_mod

    mock_detect = MagicMock(return_value=[SimpleNamespace(lang="fr", prob=0.99)])
    mock_exc = type("LangDetectException", (Exception,), {})
    fake_ld = SimpleNamespace(detect_langs=mock_detect, lang_detect_exception=SimpleNamespace(LangDetectException=mock_exc))
    with patch.dict("sys.modules", {"langdetect": fake_ld, "langdetect.lang_detect_exception": fake_ld.lang_detect_exception}):
        assert rpc_mod.detect_lang_sample("Bonjour le monde.") == "fr-FR"
