# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.langdetect_rpc."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.venv import langdetect_rpc as rpc_mod


def test_detect_lang_sample_empty() -> None:
    assert rpc_mod.detect_lang_sample("") is None
    assert rpc_mod.detect_lang_sample("   ") is None


def test_detect_lang_batch_alignment() -> None:
    with patch.object(rpc_mod, "detect_lang_sample", side_effect=["fr-FR", None, "de-DE"]):
        assert rpc_mod.detect_lang_batch(["a", "b", "c"]) == ["fr-FR", None, "de-DE"]


def test_detect_lang_sample_french(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_detect = MagicMock(return_value=[SimpleNamespace(lang="fr", prob=0.99)])
    mock_exc = type("LangDetectException", (Exception,), {})
    fake_ld = SimpleNamespace(detect_langs=mock_detect, lang_detect_exception=SimpleNamespace(LangDetectException=mock_exc))
    monkeypatch.setitem(__import__("sys").modules, "langdetect", fake_ld)
    monkeypatch.setitem(__import__("sys").modules, "langdetect.lang_detect_exception", fake_ld.lang_detect_exception)
    assert rpc_mod.detect_lang_sample("Bonjour le monde.") == "fr-FR"


def test_detect_lang_sample_import_error_message() -> None:
    with patch.dict("sys.modules", {"langdetect": None}):
        with pytest.raises(ImportError, match="langdetect is not installed"):
            rpc_mod.detect_lang_sample("hello")


def test_detect_lang_batch_none_input() -> None:
    assert rpc_mod.detect_lang_batch(None) == []  # type: ignore[arg-type]


def test_bcp47_tags_isolation() -> None:
    code = (
        "import sys, plugin.framework.grammar_rule_ids; "
        "assert not any(m in sys.modules for m in ('plugin.writer', 'plugin.doc', 'plugin.embeddings', 'uno')); "
        "from plugin.embeddings.venv import langdetect_rpc; "
        "table = [('fr', 'fr-FR'), ('ja', 'ja-JP'), ('EN', 'en-US'), ('en-gb', 'en-GB'), "
        "('zh-cn', 'zh-CN'), ('zh-tw', 'zh-TW'), ('zh-hk', 'zh-TW'), ('pt', 'pt-BR'), "
        "('pt-PT', 'pt-BR'), ('no', 'nb-NO'), ('nb', 'nb-NO'), ('nn', 'nn-NO'), "
        "('de-AT', 'de-DE'), ('sr', 'sr'), ('th', 'th'), ('en-nz', 'en-US'), ('', None), (None, None)]; "
        "assert all(langdetect_rpc._raw_lang_to_bcp47(r) == exp for r, exp in table); "
        "assert 'plugin.writer' not in sys.modules"
    )
    subprocess.check_call([sys.executable, "-c", code])


def test_bcp47_constants_and_tables() -> None:
    from plugin.framework.grammar_rule_ids import (
        _ISO_TO_SNOWBALL,
        STOPWORDS_ISO_KEY,
        normalize_uno_locale_to_bcp47,
    )
    from plugin.writer.locale.harper import HARPER_LOCALE_TAGS

    assert HARPER_LOCALE_TAGS == ("en-US", "en-GB", "en-AU", "en-CA", "en-IN")
    assert STOPWORDS_ISO_KEY["serbian"] == "hr"
    assert _ISO_TO_SNOWBALL["sr"] == "serbian"
    assert "hr" not in _ISO_TO_SNOWBALL

    overrides = {
        ("en", "UK"): "en-GB",
        ("zh", "HK"): "zh-TW",
        ("zh", "MO"): "zh-TW",
        ("no", "NO"): "nb-NO",
        ("no", ""): "nb-NO",
        ("nb", ""): "nb-NO",
        ("nn", ""): "nn-NO",
    }
    for (lang, country), expected in overrides.items():
        assert normalize_uno_locale_to_bcp47(SimpleNamespace(Language=lang, Country=country)) == expected

    fallbacks = {
        "en": "en-US",
        "zh": "zh-CN",
        "pt": "pt-BR",
        "nb": "nb-NO",
        "no": "nb-NO",
        "nn": "nn-NO",
    }
    for lang, expected in fallbacks.items():
        assert normalize_uno_locale_to_bcp47(SimpleNamespace(Language=lang, Country="UNKNOWN")) == expected

    assert normalize_uno_locale_to_bcp47(SimpleNamespace(Language="no", Country="SE")) == "nb-NO"


def test_raw_lang_to_bcp47_table() -> None:
    table = [
        ("fr", "fr-FR"),
        ("ja", "ja-JP"),
        ("EN", "en-US"),
        ("en-gb", "en-GB"),
        ("zh-cn", "zh-CN"),
        ("zh-tw", "zh-TW"),
        ("zh-hk", "zh-TW"),
        ("pt", "pt-BR"),
        ("pt-PT", "pt-BR"),
        ("no", "nb-NO"),
        ("nb", "nb-NO"),
        ("nn", "nn-NO"),
        ("de-AT", "de-DE"),
        ("sr", "sr"),
        ("th", "th"),
        ("en-nz", "en-US"),
        ("", None),
        (None, None),
    ]
    for raw, expected in table:
        assert rpc_mod._raw_lang_to_bcp47(raw) == expected


def test_harper_locale_normalization_contract() -> None:
    from plugin.writer.locale.harper_proofreader import normalize_harper_locale_to_bcp47

    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="en-NZ", Country="", Variant="")) == "en-US"
    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="en", Country="NZ", Variant="")) == "en-US"
