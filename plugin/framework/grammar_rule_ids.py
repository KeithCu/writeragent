# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure rule-id strings shared by the host ignore list and the venv LanguageTool checker, and shared BCP-47/language tag tables.

Stdlib only; must not import plugin.writer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

WA_G_RULE_PREFIX = "wa_g_rule||"
HARPER_RULE_PREFIX = "harper||"
LANGUAGETOOL_RULE_PREFIX = "languagetool||"
STABLE_RULE_PREFIXES = (HARPER_RULE_PREFIX, LANGUAGETOOL_RULE_PREFIX)


def make_rule_identifier(prefix: str, rule_code: str) -> str:
    """Build a prefixed grammar rule id for UNO ``aRuleIdentifier``."""
    return f"{prefix}{rule_code}"


def parse_prefixed_rule_identifier(rule_identifier: str, prefix: str) -> str | None:
    """Return rule code suffix for a prefixed id, or ``None`` when prefix does not match."""
    if not rule_identifier.startswith(prefix):
        return None
    code = rule_identifier[len(prefix) :].strip()
    return code or None


def bare_code_for_persistence(rule_identifier: str, prefix: str) -> str:
    """Bare rule code stored in document ``ignored_rules`` (fallback: full id)."""
    return parse_prefixed_rule_identifier(rule_identifier, prefix) or rule_identifier


def grammar_match(
    *,
    wrong: str,
    correct: str,
    n_error_start: int,
    n_error_length: int,
    short_comment: str,
    full_comment: str,
    rule_identifier: str,
    suggestions: list[str],
    reason: str,
    type: str,
) -> dict[str, Any]:
    return {
        "wrong": wrong,
        "correct": correct,
        "n_error_start": n_error_start,
        "n_error_length": n_error_length,
        "short_comment": short_comment,
        "full_comment": full_comment,
        "rule_identifier": rule_identifier,
        "suggestions": suggestions,
        "reason": reason,
        "type": type,
    }


# ---------------------------------------------------------------------------
# Shipped BCP-47 registry + UNO CharLocale bridging
# ---------------------------------------------------------------------------

# One canonical tag per folder under repo-root ``locales/`` (plus en-US, en-GB; English has no ``en`` dir).
# Order: English first, then alphabetical by BCP-47. Must match ``LinguisticWriterAgentGrammar.xcu``.
#
# * ``pt`` → ``pt-BR`` (user docs may set ``pt-PT``; ``normalize_uno_locale_to_bcp47`` maps any ``pt`` to ``pt-BR``)
#
# English names for the LLM (short addendum; JSON schema part stays in English)
GRAMMAR_REGISTRY_LOCALES: dict[str, str] = {
    "en-US": "English (United States)",
    "en-GB": "English (United Kingdom)",
    "en-AU": "English (Australia)",
    "en-CA": "English (Canada)",
    "en-IN": "English (India)",
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

GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = tuple(GRAMMAR_REGISTRY_LOCALES.keys())


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
    _l, _c = bcp47_to_uno_lang_country(_t)
    _EXACT_PAIR_TO_TAG[(_l.lower(), _c.upper())] = _t
    if _c == "":
        _EXACT_PAIR_TO_TAG[(_l.lower(), "")] = _t

# One canonical BCP-47 per ISO language when the document country does not match the registry
# (e.g. ``de-AT`` / ``es-MX``) — ``en`` / ``zh`` / ``pt`` / ``nb``/``nn``/``no`` are handled in code.
_CANON_BY_LANG_NO_REGION: dict[str, str] = {}
for _t in GRAMMAR_REGISTRY_LOCALE_TAGS:
    _l, _c = bcp47_to_uno_lang_country(_t)
    _key = _l.lower()
    if _key in ("en", "zh", "pt", "nb", "nn", "no"):
        continue
    if _key not in _CANON_BY_LANG_NO_REGION:
        _CANON_BY_LANG_NO_REGION[_key] = _t

# Special country/dialect overrides for multi-region or alias languages
_LANGUAGE_DIALECT_OVERRIDES: dict[tuple[str, str], str] = {
    # English regional variants
    ("en", "GB"): "en-GB",
    ("en", "UK"): "en-GB",
    ("en", "AU"): "en-AU",
    ("en", "CA"): "en-CA",
    ("en", "IN"): "en-IN",
    # Chinese variants
    ("zh", "TW"): "zh-TW",
    ("zh", "HK"): "zh-TW",
    ("zh", "MO"): "zh-TW",
    ("zh", "CN"): "zh-CN",
    # Norwegian variants
    ("no", "NO"): "nb-NO",
    ("no", ""): "nb-NO",
    ("nb", "NO"): "nb-NO",
    ("nb", ""): "nb-NO",
    ("nn", "NO"): "nn-NO",
    ("nn", ""): "nn-NO",
}

_LANGUAGE_DEFAULT_FALLBACKS: dict[str, str] = {
    "en": "en-US",
    "zh": "zh-CN",
    "pt": "pt-BR",
    "nb": "nb-NO",
    "no": "nb-NO",
    "nn": "nn-NO",
}


def normalize_uno_locale_to_bcp47(a_locale: Any) -> str | None:
    """Map a document ``Locale`` to our canonical BCP-47, or None if the language is not supported.

    Regional variants (e.g. ``de-AT``) map to the same canonical tag as ``de-DE`` for cache/LLM.
    """
    if a_locale is None:
        return None
    lang = (getattr(a_locale, "Language", None) or "").strip()
    if not lang:
        return None
    country = (getattr(a_locale, "Country", None) or "").strip().upper()

    lang_l = lang.lower()
    key = (lang_l, country)

    # 1. Exact (lang, country) or (lang, "") matches
    if key in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[key]
    if (lang_l, "") in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[(lang_l, "")]

    # 2. Dialect / territory overrides
    if key in _LANGUAGE_DIALECT_OVERRIDES:
        return _LANGUAGE_DIALECT_OVERRIDES[key]

    # 3. Base language default fallbacks
    if lang_l in _LANGUAGE_DEFAULT_FALLBACKS:
        return _LANGUAGE_DEFAULT_FALLBACKS[lang_l]

    # 4. Standard regional fallback (e.g. de-AT -> de-DE, es-MX -> es-ES)
    return _CANON_BY_LANG_NO_REGION.get(lang_l)


def _normalize_lang_country(lang: str, country: str = "") -> str | None:
    """Same registry mapping as a UNO Locale with Language/Country fields."""
    return normalize_uno_locale_to_bcp47(SimpleNamespace(Language=lang, Country=country))


def normalize_detected_bcp47(tag: str | None) -> str | None:
    """Map an LLM-detected BCP-47 tag onto a supported registry tag (same rules as UNO locales)."""
    if not tag or not str(tag).strip():
        return None
    raw = str(tag).strip().replace("_", "-")
    if raw in GRAMMAR_REGISTRY_LOCALES:
        return raw
    parts = raw.split("-")
    lang = parts[0]
    if not lang:
        return None
    country = parts[1].upper() if len(parts) > 1 else ""
    return _normalize_lang_country(lang, country)


def grammar_bcp47_tags_match(a: str | None, b: str | None) -> bool:
    """True when two tags denote the same supported grammar locale (e.g. ``ja`` vs ``ja-JP``)."""
    if not a or not b:
        return False
    na = normalize_detected_bcp47(a) or a
    nb = normalize_detected_bcp47(b) or b
    return na == nb


def grammar_english_name_for_bcp47(bcp47: str) -> str:
    return GRAMMAR_REGISTRY_LOCALES.get(bcp47, bcp47)


# ---------------------------------------------------------------------------
# Snowball / ISO language mappings
# ---------------------------------------------------------------------------

_ISO_TO_SNOWBALL: dict[str, str] = {
    "ar": "arabic",
    "hy": "armenian",
    "eu": "basque",
    "ca": "catalan",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "eo": "esperanto",
    "et": "estonian",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "el": "greek",
    "hi": "hindi",
    "hu": "hungarian",
    "id": "indonesian",
    "ga": "irish",
    "it": "italian",
    "lt": "lithuanian",
    "ne": "nepali",
    "no": "norwegian",
    "nb": "norwegian",
    "nn": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sr": "serbian",
    "es": "spanish",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
    "yi": "yiddish",
}

STOPWORDS_ISO_KEY: dict[str, str] = {
    "arabic": "ar",
    "armenian": "hy",
    "basque": "eu",
    "catalan": "ca",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "esperanto": "eo",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hindi": "hi",
    "hungarian": "hu",
    "indonesian": "id",
    "irish": "ga",
    "italian": "it",
    "lithuanian": "lt",
    "norwegian": "no",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "serbian": "hr",
    "spanish": "es",
    "swedish": "sv",
    "tamil": "ta",
    "turkish": "tr",
    "yiddish": "yi",
}
