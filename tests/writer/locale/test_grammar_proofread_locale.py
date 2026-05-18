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
    # Rule: Pure numbers (any length) OR text with 1-6 alphabetic chars = abbreviation
    # Returns alpha character count if abbrev, 0 otherwise (punctuation does NOT count)
    # Basic text abbreviations
    assert gl.word_before_period_is_abbrev("Mr") == 2
    assert gl.word_before_period_is_abbrev("Dr") == 2
    assert gl.word_before_period_is_abbrev("abc") == 3  # 3 alpha chars
    assert gl.word_before_period_is_abbrev("A") == 1  # Single alpha char
    # 5-6 letter abbreviations
    assert gl.word_before_period_is_abbrev("hello") == 5  # 5 alpha chars
    assert gl.word_before_period_is_abbrev("abcde") == 5  # 5 alpha chars
    assert gl.word_before_period_is_abbrev("approx") == 6  # 6 alpha chars
    # Dots (internal punctuation does NOT count toward limit)
    assert gl.word_before_period_is_abbrev("USA") == 3  # 3 alpha chars
    assert gl.word_before_period_is_abbrev("U.S.A.") == 3  # 3 alpha chars (U,S,A)
    assert gl.word_before_period_is_abbrev("Ph.D.") == 3  # 3 alpha chars (P,h,D)
    assert gl.word_before_period_is_abbrev("e.g.") == 2  # 2 alpha chars (e,g)
    assert gl.word_before_period_is_abbrev("i.e.") == 2  # 2 alpha chars (i,e)
    assert gl.word_before_period_is_abbrev("a.m.") == 2  # 2 alpha chars (a,m)
    assert gl.word_before_period_is_abbrev("No.") == 2  # 2 alpha chars (N,o)
    # Hyphens (internal punctuation does NOT count toward limit)
    assert gl.word_before_period_is_abbrev("U-N") == 2  # 2 alpha chars (U,N)
    # Apostrophes (internal punctuation does NOT count toward limit)
    assert gl.word_before_period_is_abbrev("O'B") == 2  # 2 alpha chars (O,B)
    # Mixed alphanumeric (only alpha chars count toward limit)
    assert gl.word_before_period_is_abbrev("R2D2") == 2  # 2 alpha chars (R,D)
    assert gl.word_before_period_is_abbrev("i18n") == 2  # 2 alpha chars (i,n)
    # Numbers (pure digits with separators - 0 alpha chars, but returns 1 to indicate "treat as abbrev")
    assert gl.word_before_period_is_abbrev("123") == 1  # Pure number (returns 1)
    assert gl.word_before_period_is_abbrev("12345") == 1  # Pure number (returns 1)
    assert gl.word_before_period_is_abbrev("1234567890") == 1  # Pure number (returns 1)
    assert gl.word_before_period_is_abbrev("1,234") == 1  # Pure number with comma (returns 1)
    assert gl.word_before_period_is_abbrev("3.14") == 1  # Pure number with decimal (returns 1)
    # Not abbreviations (too many alpha chars)
    assert gl.word_before_period_is_abbrev("department") == 0  # 9 alpha chars
    assert gl.word_before_period_is_abbrev("O'Reilly") == 0  # 7 alpha chars
    # Edge cases
    assert gl.word_before_period_is_abbrev("") == 0  # Empty
    assert gl.word_before_period_is_abbrev(".") == 0  # Only dots
    assert gl.word_before_period_is_abbrev("...") == 0  # Only dots

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

