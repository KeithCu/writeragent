# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for AI grammar BCP-47 registry (parity with ``plugin/locales``)."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any

from plugin.modules.writer import grammar_proofread_engine as eng
from plugin.modules.writer.grammar_locale_registry import (
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_uno_lang_country,
    grammar_english_name_for_bcp47,
    normalize_uno_locale_to_bcp47,
)

# Folder names under ``plugin/locales`` that are translation targets (excludes en; English uses POT as msgid).
_GETTEXT_LOCALE_DIRS: frozenset[str] = frozenset(
    n
    for n in os.listdir(
        os.path.join(os.path.dirname(__file__), "..", "locales")
    )
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
        f"Update _FOLDERS_TO_TAG or ``plugin/locales``: missing/extra: "
        f"{_GETTEXT_LOCALE_DIRS.symmetric_difference(_FOLDERS_TO_TAG.keys())}"
    )
    for _folder, expected in _FOLDERS_TO_TAG.items():
        assert expected in GRAMMAR_REGISTRY_LOCALE_TAGS, expected


def test_grammar_tags_include_english_variants() -> None:
    assert "en-US" in GRAMMAR_REGISTRY_LOCALE_TAGS
    assert "en-GB" in GRAMMAR_REGISTRY_LOCALE_TAGS


def test_grammar_tag_count() -> None:
    assert len(GRAMMAR_REGISTRY_LOCALE_TAGS) == 2 + len(_GETTEXT_LOCALE_DIRS)


def test_parity_grammar_engine_and_xcu() -> None:
    assert eng.GRAMMAR_REGISTRY_LOCALE_TAGS == GRAMMAR_REGISTRY_LOCALE_TAGS


def test_normalize_german_regional() -> None:
    assert normalize_uno_locale_to_bcp47(_uno_locale("de", "AT")) == "de-DE"
    assert normalize_uno_locale_to_bcp47(_uno_locale("de", "DE")) == "de-DE"


def test_normalize_english_choices() -> None:
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "US")) == "en-US"
    assert normalize_uno_locale_to_bcp47(_uno_locale("en", "GB")) == "en-GB"


def test_normalize_chinese() -> None:
    assert normalize_uno_locale_to_bcp47(_uno_locale("zh", "CN")) == "zh-CN"
    assert normalize_uno_locale_to_bcp47(_uno_locale("zh", "TW")) == "zh-TW"


def test_normalize_pt_any_region_to_brazil() -> None:
    assert normalize_uno_locale_to_bcp47(_uno_locale("pt", "BR")) == "pt-BR"
    assert normalize_uno_locale_to_bcp47(_uno_locale("pt", "PT")) == "pt-BR"


def test_english_name_is_nonempty() -> None:
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        assert len(grammar_english_name_for_bcp47(tag)) > 0


def test_bcp47_tags_have_valid_uno_language() -> None:
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        la, c = bcp47_to_uno_lang_country(tag)
        assert la and len(la) >= 2
        assert c == c.upper()
