# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""BCP-47 labels for the AI grammar proofreader, aligned with shipped gettext ``plugin/locales``."""

from __future__ import annotations

from typing import Any

# One canonical tag per folder under ``plugin/locales/`` (plus en-US, en-GB; English has no ``en`` dir).
# Order: English first, then alphabetical by BCP-47. Must match ``LinguisticWriterAgentGrammar.xcu``.
#
# * ``pt`` → ``pt-BR`` (user docs may set ``pt-PT``; ``normalize_uno_locale_to_bcp47`` maps any ``pt`` to ``pt-BR``)
GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = (
    "en-US",
    "en-GB",
    "bg-BG",
    "bn-IN",
    "ca-ES",
    "cs-CZ",
    "da-DK",
    "de-DE",
    "el-GR",
    "es-ES",
    "et-EE",
    "fi-FI",
    "fr-FR",
    "hi-IN",
    "hr-HR",
    "hu-HU",
    "id-ID",
    "it-IT",
    "ja-JP",
    "ko-KR",
    "lt-LT",
    "lv-LV",
    "nb-NO",
    "nl-NL",
    "nn-NO",
    "pl-PL",
    "pt-BR",
    "ro-RO",
    "ru-RU",
    "sk-SK",
    "sv-SE",
    "tr-TR",
    "uk-UA",
    "ur-PK",
    "zh-CN",
    "zh-TW",
)

# --- UNO (LibreOffice) CharLocale: ``Language`` + ``Country`` (ISO region) ---


def bcp47_to_uno_lang_country(bcp47: str) -> tuple[str, str]:
    """Return (Language, Country) for ``com.sun.star.lang.Locale`` (Variant empty for our tags)."""
    parts = bcp47.split("-")
    if not parts[0]:
        return "", ""
    lang = parts[0]
    if len(parts) < 2:
        return lang, ""
    return lang, parts[1]


# Exact (lowercase language, upper country) → tag
_EXACT_PAIR_TO_TAG: dict[tuple[str, str], str] = {}
for _t in GRAMMAR_REGISTRY_LOCALE_TAGS:
    _l, c = bcp47_to_uno_lang_country(_t)
    _EXACT_PAIR_TO_TAG[(_l.lower(), c.upper())] = _t
    if c == "":
        _EXACT_PAIR_TO_TAG[(_l.lower(), "")] = _t

# One canonical BCP-47 per ISO language when the document country does not match the registry
# (e.g. ``de-AT`` / ``es-MX``) — ``en`` / ``zh`` / ``pt`` / ``nb``/``nn``/``no`` are handled in code.
_CANON_BY_LANG_NO_REGION: dict[str, str] = {}
for _t in GRAMMAR_REGISTRY_LOCALE_TAGS:
    _l, c = bcp47_to_uno_lang_country(_t)
    key = _l.lower()
    if key in ("en", "zh", "pt", "nb", "nn", "no"):
        continue
    if key not in _CANON_BY_LANG_NO_REGION:
        _CANON_BY_LANG_NO_REGION[key] = _t


def normalize_uno_locale_to_bcp47(a_locale: Any) -> str | None:
    """Map a document ``Locale`` to our canonical BCP-47, or None if the language is not supported.

    Regional variants (e.g. ``de-AT``) map to the same canonical tag as ``de-DE`` for cache/LLM.
    """
    try:
        if a_locale is None:
            return None
        lang = (getattr(a_locale, "Language", None) or "").strip()
        if not lang:
            return None
        country = (getattr(a_locale, "Country", None) or "").strip().upper()
    except Exception:
        return None

    lang_l = lang.lower()
    key = (lang_l, country)

    if key in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[key]
    if (lang_l, "") in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[(lang_l, "")]

    if lang_l == "en":
        if country in ("GB", "UK"):
            return "en-GB"
        return "en-US"

    if lang_l == "zh":
        if country in ("TW", "HK", "MO"):
            return "zh-TW"
        return "zh-CN"

    if lang_l in ("nb", "no"):
        return "nb-NO"
    if lang_l == "nn":
        return "nn-NO"

    if lang_l == "pt":
        return "pt-BR"

    c = _CANON_BY_LANG_NO_REGION.get(lang_l)
    if c is not None:
        return c
    return None


# English names for the LLM (short addendum; JSON schema part stays in English)
_GRAMMAR_BCP47_EN_NAME: dict[str, str] = {
    "en-US": "English (United States)",
    "en-GB": "English (United Kingdom)",
    "bg-BG": "Bulgarian",
    "bn-IN": "Bengali (India)",
    "ca-ES": "Catalan",
    "cs-CZ": "Czech",
    "da-DK": "Danish",
    "de-DE": "German",
    "el-GR": "Greek",
    "es-ES": "Spanish",
    "et-EE": "Estonian",
    "fi-FI": "Finnish",
    "fr-FR": "French",
    "hi-IN": "Hindi (India)",
    "hr-HR": "Croatian",
    "hu-HU": "Hungarian",
    "id-ID": "Indonesian",
    "it-IT": "Italian",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
    "lt-LT": "Lithuanian",
    "lv-LV": "Latvian",
    "nb-NO": "Norwegian Bokmål",
    "nl-NL": "Dutch",
    "nn-NO": "Norwegian Nynorsk",
    "pl-PL": "Polish",
    "pt-BR": "Portuguese (Brazil)",
    "ro-RO": "Romanian",
    "ru-RU": "Russian",
    "sk-SK": "Slovak",
    "sv-SE": "Swedish",
    "tr-TR": "Turkish",
    "uk-UA": "Ukrainian",
    "ur-PK": "Urdu (Pakistan)",
    "zh-CN": "Chinese (Simplified, China)",
    "zh-TW": "Chinese (Traditional, Taiwan)",
}


def grammar_english_name_for_bcp47(bcp47: str) -> str:
    return _GRAMMAR_BCP47_EN_NAME.get(bcp47, bcp47)
