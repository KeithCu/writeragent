# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for AI grammar locale-aware policy (BCP-47 registry, UNO bridging, terminators, parsing)."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.i18n import get_locales_dir
from plugin.framework.grammar_rule_ids import (
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_uno_lang_country,
    grammar_bcp47_tags_match,
    grammar_english_name_for_bcp47,
    normalize_detected_bcp47,
    normalize_uno_locale_to_bcp47,
)
from plugin.writer.locale import grammar_proofread_locale as gl

# Folder names under repo-root ``locales/`` that are translation targets (excludes en; English uses POT as msgid).
_GETTEXT_LOCALE_DIRS: frozenset[str] = frozenset(
    n
    for n in os.listdir(get_locales_dir())
    if re.match(r"^[a-z]{2,3}(_[A-Z]{2})?$", n)
)

# Each gettext dir (except that ``pt`` maps to one BCP-47) must appear as the intended primary tag
_FOLDERS_TO_TAG = {
    "bg": "bg-BG",
    "bn_IN": "bn-IN",
    "ca": "ca-ES",
    "cs": "cs-CZ",
    "da": "da-DK",
    "de": "de-DE",
    "el": "el-GR",
    "es": "es-ES",
    "et": "et-EE",
    "fi": "fi-FI",
    "fr": "fr-FR",
    "hi_IN": "hi-IN",
    "hr": "hr-HR",
    "hu": "hu-HU",
    "id": "id-ID",
    "it": "it-IT",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "lt": "lt-LT",
    "lv": "lv-LV",
    "nb_NO": "nb-NO",
    "nl": "nl-NL",
    "nn_NO": "nn-NO",
    "pl": "pl-PL",
    "pt": "pt-BR",
    "ro": "ro-RO",
    "ru": "ru-RU",
    "sk": "sk-SK",
    "sv": "sv-SE",
    "tr": "tr-TR",
    "uk": "uk-UA",
    "ur_PK": "ur-PK",
    "zh_CN": "zh-CN",
    "zh_TW": "zh-TW",
}

def _uno_locale(lang: str, country: str) -> Any:
    return SimpleNamespace(Language=lang, Country=country, Variant="")

def test_grammar_tags_match_gettext_folders() -> None:
    assert _FOLDERS_TO_TAG.keys() == _GETTEXT_LOCALE_DIRS, (
        f"Update _FOLDERS_TO_TAG or ``locales/``: missing/extra: "
        f"{_GETTEXT_LOCALE_DIRS.symmetric_difference(_FOLDERS_TO_TAG.keys())}"
    )
    for _folder, expected in _FOLDERS_TO_TAG.items():
        assert expected in GRAMMAR_REGISTRY_LOCALE_TAGS, expected

def test_grammar_tags_include_english_variants() -> None:
    assert "en-US" in GRAMMAR_REGISTRY_LOCALE_TAGS
    assert "en-GB" in GRAMMAR_REGISTRY_LOCALE_TAGS
    assert "en-AU" in GRAMMAR_REGISTRY_LOCALE_TAGS
    assert "en-CA" in GRAMMAR_REGISTRY_LOCALE_TAGS
    assert "en-IN" in GRAMMAR_REGISTRY_LOCALE_TAGS

def test_grammar_tag_count() -> None:
    assert len(GRAMMAR_REGISTRY_LOCALE_TAGS) == 5 + len(_GETTEXT_LOCALE_DIRS)

@pytest.mark.parametrize(
    "value, value_2, expected, value_3, value_4, expected_2",
    [
        pytest.param("de", "AT", "de-DE", "de", "DE", "de-DE", id="test_normalize_german_regional"),
        pytest.param("zh", "CN", "zh-CN", "zh", "TW", "zh-TW", id="test_normalize_chinese"),
        pytest.param("pt", "BR", "pt-BR", "pt", "PT", "pt-BR", id="test_normalize_pt_any_region_to_brazil"),
    ],
)
def test_normalize_german_regional(value, value_2, expected, value_3, value_4, expected_2) -> None:
    assert normalize_uno_locale_to_bcp47(_uno_locale(value, value_2)) == expected
    assert normalize_uno_locale_to_bcp47(_uno_locale(value_3, value_4)) == expected_2

def test_normalize_english_choices() -> None:
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "US")) == "en-US"
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "GB")) == "en-GB"
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "AU")) == "en-AU"
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "CA")) == "en-CA"
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "IN")) == "en-IN"

def test_english_name_is_nonempty() -> None:
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        assert len(grammar_english_name_for_bcp47(tag)) > 0

def test_bcp47_tags_have_valid_uno_language() -> None:
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        la, c = bcp47_to_uno_lang_country(tag)
        assert la and len(la) >= 2
        assert c == c.upper()


def test_bcp47_to_uno_lang_country_keeps_empty_language_and_second_subtag() -> None:
    assert bcp47_to_uno_lang_country("en") == ("en", "")
    assert bcp47_to_uno_lang_country("en-US") == ("en", "US")
    assert bcp47_to_uno_lang_country("zh-Hans-CN") == ("zh", "Hans")
    assert bcp47_to_uno_lang_country("") == ("", "")
    assert bcp47_to_uno_lang_country("-US") == ("", "")


def test_looks_complete_sentence_matches_proofreader_gating() -> None:
    """Includes STerm chars beyond ASCII."""
    assert gl.looks_complete_sentence("Hello world.") is True
    assert gl.looks_complete_sentence("incomplete clause") is False
    assert gl.looks_complete_sentence("Բարև։") is True
    assert "։" in gl.GRAMMAR_SENTENCE_TERMINATORS
    assert gl.looks_complete_sentence('She said "hello."') is True
    assert gl.last_meaningful_char('She said "hello."') == "."
    assert gl.looks_complete_sentence("He said hello.\u232a") is True
    assert "\u232a" in gl.GRAMMAR_TRAILING_CLOSERS


def test_trailing_closers_cover_unicode_pe_pf() -> None:
    """A Unicode Pe/Pf that is skipped by last_meaningful_char must be in the table."""
    import sys
    import unicodedata

    missing = sorted(
        ch for ch in map(chr, range(sys.maxunicode + 1))
        if unicodedata.category(ch) in ("Pe", "Pf") and ch not in gl.GRAMMAR_TRAILING_CLOSERS
    )
    assert missing == []

def test_partial_threshold_counts_nonspace_chars() -> None:
    assert gl.count_nonspace_chars("a b c") == 3
    assert gl.count_nonspace_chars("too short") < gl.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
    assert gl.count_nonspace_chars("this is long enough") >= gl.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS


def test_tricky_terminator_regex_escaping() -> None:
    """Test that _sterm_class handles tricky chars like ] - \\ ^."""
    # Verify that the regex string is safe (contains escapes for special chars)
    # re.escape escapes almost everything non-alphanumeric.
    assert "\\]" in gl._sterm_escaped or "]" not in gl._sterm_chars
    assert "\\-" in gl._sterm_escaped or "-" not in gl._sterm_chars
    assert "\\\\" in gl._sterm_escaped or "\\" not in gl._sterm_chars
    
    # Test normalization with a simulated tricky terminator if we could, 
    # but let's just verify the compiled regex doesn't crash and matches dots.
    assert re.match(gl._sterm_class, ".")
    assert gl.GRAMMAR_CACHE_NORMALIZATION_RE.match("Hello.")


def test_fingerprint_for_text() -> None:
    text1 = "This is a sentence."
    text2 = "This is another sentence."
    fp1 = gl.fingerprint_for_text(text1)
    fp2 = gl.fingerprint_for_text(text2)
    assert len(fp1) == 24
    assert len(fp2) == 24
    assert fp1 != fp2
    assert fp1 == gl.fingerprint_for_text(text1)


@pytest.mark.parametrize("provider", ("harper", "languagetool", "vale"))
def test_grammar_max_in_flight_local_providers_always_one(provider: str) -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.config.get_config_int_safe", return_value=4),
        patch("plugin.writer.locale.grammar_worker.get_grammar_provider", return_value=provider),
    ):
        assert gl.grammar_max_in_flight(ctx) == 1


def test_grammar_max_in_flight_llm_uses_config() -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.config.get_config_int_safe", return_value=4),
        patch("plugin.writer.locale.grammar_worker.get_grammar_provider", return_value="llm"),
    ):
        assert gl.grammar_max_in_flight(ctx) == 4


def test_grammar_max_in_flight_llm_clamps_to_hard_cap() -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.config.get_config_int_safe", return_value=99),
        patch("plugin.writer.locale.grammar_worker.get_grammar_provider", return_value="llm"),
    ):
        assert gl.grammar_max_in_flight(ctx) == gl.GRAMMAR_MAX_IN_FLIGHT


def test_grammar_batch_sentences_defaults_and_clamping() -> None:
    with patch("plugin.framework.config.get_config_int_safe", return_value=0):
        assert gl.grammar_batch_sentences() == 1

    with patch("plugin.framework.config.get_config_int_safe", return_value=4):
        assert gl.grammar_batch_sentences() == 4

    with patch("plugin.framework.config.get_config_int_safe", return_value=99):
        assert gl.grammar_batch_sentences() == gl.GRAMMAR_BATCH_MAX_SENTENCES

    with patch("plugin.framework.config.get_config_int_safe", return_value=-5):
        assert gl.grammar_batch_sentences() == 1


def test_grammar_max_tokens_and_chars_are_fixed() -> None:
    assert gl.grammar_max_tokens() == gl.GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
    assert gl.grammar_max_chars() == gl.GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS


def test_clamped_int_config_keys_exist_in_doc_module_yaml() -> None:
    """Every key read by _clamped_int_config must be a real doc-module config key."""
    import yaml
    from pathlib import Path

    doc_yaml = Path(__file__).resolve().parents[3] / "plugin" / "doc" / "module.yaml"
    config = yaml.safe_load(doc_yaml.read_text(encoding="utf-8"))["config"]
    for key in ("doc.grammar_proofreader_max_in_flight", "doc.grammar_proofreader_batch_sentences"):
        assert key.split(".", 1)[1] in config, key


def test_normalize_detected_bcp47_registry_and_shorthand() -> None:
    assert normalize_detected_bcp47("ja-JP") == "ja-JP"
    assert normalize_detected_bcp47("ja") == "ja-JP"
    assert normalize_detected_bcp47("en") == "en-US"


def test_grammar_bcp47_tags_match() -> None:
    assert grammar_bcp47_tags_match("ja", "ja-JP")
    assert grammar_bcp47_tags_match("ja-JP", "ja-JP")
    assert not grammar_bcp47_tags_match("ja-JP", "zh-CN")


def test_checked_grammar_text() -> None:
    assert gl.checked_grammar_text("short text", 20) == "short text"
    assert gl.checked_grammar_text("exact length", 12) == "exact length"
    assert gl.checked_grammar_text("word1 word2 word3 word4", 20) == "word1 word2 word3"
    text_early_space = "word1 " + "a" * 25
    assert gl.checked_grammar_text(text_early_space, 20) == text_early_space[:20]
    assert gl.checked_grammar_text("a" * 30, 20) == "a" * 20


def test_filter_sentence_spans_for_thresholds() -> None:
    spans = [
        (0, 5, "Hi."),  # Complete + Short -> Kept
        (5, 30, "This is a much longer sentence."),  # Complete + Long -> Kept
        (30, 40, "The quick"),  # Incomplete + Short -> Dropped
        (40, 70, "This is an incomplete but very long fragment"),  # Incomplete + Long -> Kept
    ]

    filtered = gl.filter_sentence_spans_for_thresholds(spans)
    assert len(filtered) == 3
    assert filtered[0][2] == "Hi."
    assert filtered[1][2] == "This is a much longer sentence."
    assert filtered[2][2] == "This is an incomplete but very long fragment"
    assert gl.filter_sentence_spans_for_thresholds([(0, 1, "."), (1, 4, "   ")]) == [(0, 1, ".")]


def test_split_sentence_chunks_by_separator_regex_whitespace() -> None:
    assert gl.split_sentence_chunks_by_separator_regex("", gl.GRAMMAR_WHITESPACE_RUN_RE) == []
    assert gl.split_sentence_chunks_by_separator_regex("   ", gl.GRAMMAR_WHITESPACE_RUN_RE) == []
    assert gl.split_sentence_chunks_by_separator_regex("ab  cd", gl.GRAMMAR_WHITESPACE_RUN_RE) == [(0, "ab  "), (4, "cd")]


@pytest.mark.parametrize(
    "locale_key, expected",
    [
        ("th-TH", True),
        ("lo-LA", True),
        ("km-KH", True),
        ("en-US", False),
        ("loz-ZM", False),
        ("kmr-TR", False),
        ("thai", False),
        ("", False),
    ],
)
def test_is_whitespace_sentence_locale(locale_key: str, expected: bool) -> None:
    assert gl.is_whitespace_sentence_locale(locale_key) is expected


def test_uno_locales_for_tags() -> None:
    mock_uno = MagicMock()
    mock_uno.createUnoStruct.side_effect = lambda t, **kwargs: (t, kwargs)
    with patch.dict("sys.modules", {"uno": mock_uno}):
        locales = gl.uno_locales_for_tags(["en-US", "de-DE"])
        assert len(locales) == 2
        assert locales[0] == ("com.sun.star.lang.Locale", {"Language": "en", "Country": "US", "Variant": ""})
        assert locales[1] == ("com.sun.star.lang.Locale", {"Language": "de", "Country": "DE", "Variant": ""})

    with patch.dict("sys.modules", {"uno": None}):
        assert gl.uno_locales_for_tags(["en-US"]) == ()




