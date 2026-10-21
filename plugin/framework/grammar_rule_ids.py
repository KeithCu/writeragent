# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure rule-id strings shared by the host ignore list and the venv LanguageTool checker, and shared BCP-47/language tag tables.

Stdlib only; must not import plugin.writer.
"""

from __future__ import annotations

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


def _build_locale_maps() -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    exact: dict[tuple[str, str], str] = {}
    canon: dict[str, str] = {}
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        lang, country = bcp47_to_uno_lang_country(tag)
        exact[(lang.lower(), country.upper())] = tag
        key = lang.lower()
        if key not in ("en", "zh", "pt", "nb", "nn", "no") and key not in canon:
            canon[key] = tag
    return exact, canon


_EXACT_PAIR_TO_TAG, _CANON_BY_LANG_NO_REGION = _build_locale_maps()

# Special country/dialect overrides for multi-region or alias languages
_LANGUAGE_DIALECT_OVERRIDES: dict[tuple[str, str], str] = {
    ("en", "UK"): "en-GB",
    ("zh", "HK"): "zh-TW",
    ("zh", "MO"): "zh-TW",
    ("no", "NO"): "nb-NO",
    ("no", ""): "nb-NO",
    ("nb", ""): "nb-NO",
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


def _normalize_lang_country(lang: str, country: str = "") -> str | None:
    """Cascade: exact pair -> dialect override -> default fallback -> canon no region."""
    lang_l = lang.strip().lower()
    if not lang_l:
        return None
    country_u = country.strip().upper()
    key = (lang_l, country_u)

    if key in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[key]
    if key in _LANGUAGE_DIALECT_OVERRIDES:
        return _LANGUAGE_DIALECT_OVERRIDES[key]
    if lang_l in _LANGUAGE_DEFAULT_FALLBACKS:
        return _LANGUAGE_DEFAULT_FALLBACKS[lang_l]
    return _CANON_BY_LANG_NO_REGION.get(lang_l)


def normalize_uno_locale_to_bcp47(a_locale: Any) -> str | None:
    """Map a document ``Locale`` to our canonical BCP-47, or None if the language is not supported.

    Regional variants (e.g. ``de-AT``) map to the same canonical tag as ``de-DE`` for cache/LLM.
    """
    if a_locale is None:
        return None
    lang = getattr(a_locale, "Language", None) or ""
    country = getattr(a_locale, "Country", None) or ""
    return _normalize_lang_country(str(lang), str(country))


def normalize_detected_bcp47(tag: str | None) -> str | None:
    """Map an LLM-detected BCP-47 tag onto a supported registry tag (same rules as UNO locales)."""
    if not tag or not str(tag).strip():
        return None
    raw = str(tag).strip().replace("_", "-")
    lang, country = bcp47_to_uno_lang_country(raw)
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

_SNOWBALL_SPECS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("arabic", "ar", ("ar",)),
    ("armenian", "hy", ("hy",)),
    ("basque", "eu", ("eu",)),
    ("catalan", "ca", ("ca",)),
    ("danish", "da", ("da",)),
    ("dutch", "nl", ("nl",)),
    ("english", "en", ("en",)),
    ("esperanto", "eo", ("eo",)),
    ("estonian", "et", ("et",)),
    ("finnish", "fi", ("fi",)),
    ("french", "fr", ("fr",)),
    ("german", "de", ("de",)),
    ("greek", "el", ("el",)),
    ("hindi", "hi", ("hi",)),
    ("hungarian", "hu", ("hu",)),
    ("indonesian", "id", ("id",)),
    ("irish", "ga", ("ga",)),
    ("italian", "it", ("it",)),
    ("lithuanian", "lt", ("lt",)),
    ("nepali", None, ("ne",)),
    ("norwegian", "no", ("no", "nb", "nn")),
    ("portuguese", "pt", ("pt",)),
    ("romanian", "ro", ("ro",)),
    ("russian", "ru", ("ru",)),
    ("serbian", "hr", ("sr",)),
    ("spanish", "es", ("es",)),
    ("swedish", "sv", ("sv",)),
    ("tamil", "ta", ("ta",)),
    ("turkish", "tr", ("tr",)),
    ("yiddish", "yi", ("yi",)),
)

STOPWORDS_ISO_KEY: dict[str, str] = {snow: iso for snow, iso, _keys in _SNOWBALL_SPECS if iso is not None}
_ISO_TO_SNOWBALL: dict[str, str] = {iso: snow for snow, _iso, keys in _SNOWBALL_SPECS for iso in keys}
