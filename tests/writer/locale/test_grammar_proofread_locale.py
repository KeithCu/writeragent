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

from plugin.framework.constants import get_locales_dir
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
        assert expected in gl.GRAMMAR_REGISTRY_LOCALE_TAGS, expected

def test_grammar_tags_include_english_variants() -> None:
    assert "en-US" in gl.GRAMMAR_REGISTRY_LOCALE_TAGS
    assert "en-GB" in gl.GRAMMAR_REGISTRY_LOCALE_TAGS

def test_grammar_tag_count() -> None:
    assert len(gl.GRAMMAR_REGISTRY_LOCALE_TAGS) == 2 + len(_GETTEXT_LOCALE_DIRS)

def test_normalize_german_regional() -> None:
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("de", "AT")) == "de-DE"
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("de", "DE")) == "de-DE"

def test_bcp47_to_icu_sentence_breaker_locale() -> None:
    assert gl.bcp47_to_icu_sentence_breaker_locale("en-US") == "en@ss=standard"
    assert gl.bcp47_to_icu_sentence_breaker_locale("en-GB") == "en@ss=standard"
    assert gl.bcp47_to_icu_sentence_breaker_locale("de-DE") == "de_DE@ss=standard"
    assert gl.bcp47_to_icu_sentence_breaker_locale("ja-JP") == "ja_JP@ss=standard"
    assert gl.bcp47_to_icu_sentence_breaker_locale("") == "en@ss=standard"

def test_normalize_english_choices() -> None:
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("en", "US")) == "en-US"
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("en", "GB")) == "en-GB"

def test_normalize_chinese() -> None:
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("zh", "CN")) == "zh-CN"
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("zh", "TW")) == "zh-TW"

def test_normalize_pt_any_region_to_brazil() -> None:
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("pt", "BR")) == "pt-BR"
    assert gl.normalize_uno_locale_to_bcp47(_uno_locale("pt", "PT")) == "pt-BR"

def test_english_name_is_nonempty() -> None:
    for tag in gl.GRAMMAR_REGISTRY_LOCALE_TAGS:
        assert len(gl.grammar_english_name_for_bcp47(tag)) > 0

def test_bcp47_tags_have_valid_uno_language() -> None:
    for tag in gl.GRAMMAR_REGISTRY_LOCALE_TAGS:
        la, c = gl.bcp47_to_uno_lang_country(tag)
        assert la and len(la) >= 2
        assert c == c.upper()


def test_looks_complete_sentence_matches_proofreader_gating() -> None:
    """Includes STerm chars beyond ASCII."""
    assert gl.looks_complete_sentence("Hello world.") is True
    assert gl.looks_complete_sentence("incomplete clause") is False
    assert gl.looks_complete_sentence("Բարև։") is True
    assert "։" in gl.GRAMMAR_SENTENCE_TERMINATORS
    assert gl.looks_complete_sentence('She said "hello."') is True
    assert gl.last_meaningful_char('She said "hello."') == "."

def test_partial_threshold_counts_nonspace_chars() -> None:
    assert gl.count_nonspace_chars("a b c") == 3
    assert gl.count_nonspace_chars("too short") < gl.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
    assert gl.count_nonspace_chars("this is long enough") >= gl.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS

def test_word_before_period_is_abbrev() -> None:
    # Basic CLDR-whitelisted abbreviations
    assert gl.word_before_period_is_abbrev("Mr") == 2
    assert gl.word_before_period_is_abbrev("Dr") == 2
    assert gl.word_before_period_is_abbrev("approx") == 6
    assert gl.word_before_period_is_abbrev("Inc.") > 0

    # Normal words are NOT abbreviations (should return 0)
    assert gl.word_before_period_is_abbrev("abc") == 0
    assert gl.word_before_period_is_abbrev("hello") == 0
    assert gl.word_before_period_is_abbrev("abcde") == 0
    assert gl.word_before_period_is_abbrev("USA") == 0  # No dots, has vowel A

    # Single-letter initials (return non-zero)
    assert gl.word_before_period_is_abbrev("A") == 1
    assert gl.word_before_period_is_abbrev("г") == 1

    # Dots (internal punctuation/dots return non-zero)
    assert gl.word_before_period_is_abbrev("U.S.A.") == 6
    assert gl.word_before_period_is_abbrev("Ph.D.") == 5
    assert gl.word_before_period_is_abbrev("e.g.") == 4
    assert gl.word_before_period_is_abbrev("i.e.") == 4
    assert gl.word_before_period_is_abbrev("a.m.") == 4

    # Consonant-only checks (return non-zero)
    assert gl.word_before_period_is_abbrev("ltd") == 3
    assert gl.word_before_period_is_abbrev("vs") == 2
    assert gl.word_before_period_is_abbrev("ст") == 2

    # Numbers (pure digits with separators - 0 alpha chars, but returns 1 to indicate "treat as abbrev")
    assert gl.word_before_period_is_abbrev("123") == 1
    assert gl.word_before_period_is_abbrev("12345") == 1
    assert gl.word_before_period_is_abbrev("1234567890") == 1
    assert gl.word_before_period_is_abbrev("1,234") == 1
    assert gl.word_before_period_is_abbrev("3.14") == 1

    # Not abbreviations
    assert gl.word_before_period_is_abbrev("department") == 0
    assert gl.word_before_period_is_abbrev("O'Reilly") == 0
    assert gl.word_before_period_is_abbrev("") == 0
    assert gl.word_before_period_is_abbrev(".") == 0
    assert gl.word_before_period_is_abbrev("...") == 0


def test_word_before_period_is_abbrev_cldr_whitelist() -> None:
    """Single whitelist is filtered CLDR; heuristics cover gaps (not LLM hand lists)."""
    # CLDR-derived (must survive denylist filtering)
    assert gl.word_before_period_is_abbrev("Prof.") > 0
    assert gl.word_before_period_is_abbrev("Jan") > 0
    assert gl.word_before_period_is_abbrev("ул") > 0
    assert gl.word_before_period_is_abbrev("руб") > 0
    # Consonant-only heuristic (not in CLDR whitelist)
    assert gl.word_before_period_is_abbrev("bzw") > 0

    # Ambiguous English sentence-enders from raw CLDR must NOT match
    assert gl.word_before_period_is_abbrev("To") == 0
    assert gl.word_before_period_is_abbrev("By") == 0
    assert gl.word_before_period_is_abbrev("On") == 0
    assert gl.word_before_period_is_abbrev("Go") == 0
    assert gl.word_before_period_is_abbrev("All") == 0


def test_common_abbreviations_are_cldr_only() -> None:
    from pathlib import Path

    from plugin.writer.locale import locale_abbrev
    from plugin.writer.locale.locale_abbrev import CLDR_ABBREVS

    assert gl._COMMON_ABBREVIATIONS is CLDR_ABBREVS or gl._COMMON_ABBREVIATIONS == CLDR_ABBREVS
    assert not hasattr(gl, "_HAND_ABBREVIATIONS")
    assert "LANG_ABBREVS" not in Path(locale_abbrev.__file__).read_text(encoding="utf-8")

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


def test_grammar_inflight_key_complete_uses_full_fingerprint() -> None:
    text = "Hello world."
    assert gl.grammar_inflight_key("doc1", "en-US", text, is_complete=True) == f"doc1|en-US|{gl.fingerprint_for_text(text)}"


def test_grammar_inflight_key_incomplete_stable() -> None:
    key = gl.grammar_inflight_key("doc1", "en-US", "H", is_complete=False)
    assert key == "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    assert key == gl.grammar_inflight_key("doc1", "en-US", "Hello world", is_complete=False)


@pytest.mark.parametrize("provider", ("harper", "languagetool", "vale"))
def test_grammar_max_in_flight_local_providers_always_one(provider: str) -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.config.get_config_int_safe", return_value=4),
        patch("plugin.framework.config.get_grammar_provider", return_value=provider),
    ):
        assert gl.grammar_max_in_flight(ctx) == 1


def test_grammar_max_in_flight_llm_uses_config() -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.config.get_config_int_safe", return_value=4),
        patch("plugin.framework.config.get_grammar_provider", return_value="llm"),
    ):
        assert gl.grammar_max_in_flight(ctx) == 4


def test_grammar_max_in_flight_llm_clamps_to_hard_cap() -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.config.get_config_int_safe", return_value=99),
        patch("plugin.framework.config.get_grammar_provider", return_value="llm"),
    ):
        assert gl.grammar_max_in_flight(ctx) == gl.GRAMMAR_MAX_IN_FLIGHT

