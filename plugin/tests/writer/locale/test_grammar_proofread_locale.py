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

def test_parse_grammar_json_empty() -> None:
    assert gl.parse_grammar_json("") == []
    assert gl.parse_grammar_json("not json") == []

def test_parse_grammar_json_valid() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}'
    items = gl.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"

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
    assert gl.word_before_period_is_abbrev("Mr") is True
    assert gl.word_before_period_is_abbrev("Dr") is True
    assert gl.word_before_period_is_abbrev("abc") is False
    assert gl.word_before_period_is_abbrev("A") is True  # Single capital letter
