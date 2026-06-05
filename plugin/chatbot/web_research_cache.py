# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Fuzzy web-research cache matching: locale-aware Snowball stems + Jaccard similarity.
"""Pure helpers for fuzzy web research cache keys (stem + Jaccard lookup)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.writer.locale.linguistic_index import _ISO_TO_SNOWBALL

log = logging.getLogger("writeragent.web_research_cache")

_SNOWBALL_LANGS = frozenset(_ISO_TO_SNOWBALL.values())
_STEMMER_CACHE: dict[str, Any] = {}
_MIN_TOKEN_LEN = 3
# (gettext LO locale, snowball_lang) -> assembled fluff + stop words
_FLUFF_WORDS_CACHE: dict[tuple[str, str], frozenset[str]] = {}


def _get_stemmer(snowball_lang: str) -> Any | None:
    """Lazy snowball stemmer (same algorithms as linguistic_index IndexService)."""
    cached = _STEMMER_CACHE.get(snowball_lang)
    if cached is not None:
        return cached
    try:
        import snowballstemmer  # type: ignore[import-untyped]

        stemmer = snowballstemmer.stemmer(snowball_lang)
        _STEMMER_CACHE[snowball_lang] = stemmer
        return stemmer
    except (ImportError, KeyError):
        log.warning("No stemmer for '%s', falling back to english", snowball_lang)
        if snowball_lang != "english":
            return _get_stemmer("english")
        return None


def stem_word(snowball_lang: str, token: str) -> str:
    stemmer = _get_stemmer(snowball_lang)
    if stemmer is None:
        return token
    try:
        return stemmer.stemWord(token)
    except Exception:
        return token


def snowball_lang_from_locale_tag(tag: str) -> str:
    iso = str(tag or "").replace("-", "_").split("_")[0].lower()
    return _ISO_TO_SNOWBALL.get(iso) or "english"


def _uno_char_locale_to_tag(char_locale: Any) -> str | None:
    lang = str(getattr(char_locale, "Language", "") or "").strip()
    if not lang:
        return None
    country = str(getattr(char_locale, "Country", "") or "").strip()
    if country:
        return f"{lang}_{country}"
    return lang


def resolve_research_locale(ctx: Any, doc: Any = None) -> tuple[str, str]:
    """Return (gettext_lo_locale_tag, snowball_lang) for cache key + stemming.

    Query text is not language-detected; document CharLocale first, then LO UI locale.
    """
    if doc is not None:
        try:
            text = doc.getText()
            enum = text.createEnumeration()
            if enum.hasMoreElements():
                first_para = enum.nextElement()
                char_locale = first_para.getPropertyValue("CharLocale")
                lo_tag = _uno_char_locale_to_tag(char_locale)
                iso = getattr(char_locale, "Language", None)
                snowball = _ISO_TO_SNOWBALL.get(iso) if iso else None
                if lo_tag and snowball:
                    return lo_tag.replace("-", "_"), snowball
        except Exception as e:
            log.debug("research cache: document language detection failed: %s", e)

    try:
        from plugin.framework.i18n import get_lo_locale

        lo_tag = get_lo_locale(ctx)
        return lo_tag, snowball_lang_from_locale_tag(lo_tag)
    except Exception as e:
        log.debug("research cache: LO locale detection failed: %s", e)
    return "en_US", "english"


def resolve_research_stem_language(ctx: Any, doc: Any = None) -> str:
    """Snowball language only; prefer resolve_research_locale when gettext tag is needed."""
    _lo_tag, snowball_lang = resolve_research_locale(ctx, doc)
    return snowball_lang


def tokenize_query_words(query: str) -> list[str]:
    from plugin.writer.locale.linguistic_index import _raw_tokens

    return _raw_tokens(query)


def clear_research_fluff_words_cache() -> None:
    """Clear cached fluff sets (tests or after locale catalog reload)."""
    _FLUFF_WORDS_CACHE.clear()


def get_research_fluff_words(*, snowball_lang: str) -> frozenset[str]:
    """Instruction fluff from _('…') (active LO locale) plus grammar stop words."""
    from plugin.framework.i18n import get_active_locale

    cache_key = (get_active_locale(), snowball_lang)
    cached = _FLUFF_WORDS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    from plugin.chatbot.research_cache_fluff import translated_research_cache_fluff
    from plugin.writer.locale.linguistic_index import _raw_tokens
    from plugin.writer.locale.stop_words import stop_words_for_snowball

    fluff: set[str] = set()
    for phrase in translated_research_cache_fluff():
        fluff.update(_raw_tokens(phrase))
    fluff.update(stop_words_for_snowball(snowball_lang))
    result = frozenset(fluff)
    _FLUFF_WORDS_CACHE[cache_key] = result
    return result


def parse_research_cache_key(raw_key: str) -> tuple[str, str]:
    """Return (snowball_lang, word_key). Legacy unprefixed keys are english."""
    if "|" in raw_key:
        lang, _, word_key = raw_key.partition("|")
        if lang in _SNOWBALL_LANGS:
            return lang, word_key
    return "english", raw_key


def format_research_cache_key(snowball_lang: str, word_key: str) -> str:
    if not word_key:
        return word_key
    lang = snowball_lang if snowball_lang in _SNOWBALL_LANGS else "english"
    return f"{lang}|{word_key}"


def stem_set_from_word_key(word_key: str, snowball_lang: str) -> set[str]:
    stems: set[str] = set()
    for token in word_key.split():
        if len(token) < _MIN_TOKEN_LEN:
            continue
        stem = stem_word(snowball_lang, token)
        if stem:
            stems.add(stem)
    return stems


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def research_cache_similarity(query_stems: set[str], stored_stems: set[str]) -> float:
    """Similarity for fuzzy gate: max(union Jaccard, overlap/min-size).

    Overlap/min helps when a repeat prompt adds extra words but shares the same topic stems.
    """
    if not query_stems or not stored_stems:
        return 0.0
    intersection = query_stems & stored_stems
    if not intersection:
        return 0.0
    union = query_stems | stored_stems
    jaccard_union = len(intersection) / len(union)
    overlap_min = len(intersection) / min(len(query_stems), len(stored_stems))
    return max(jaccard_union, overlap_min)


def find_fuzzy_research_match(
    query_stems: set[str],
    stored_keys: list[str],
    *,
    snowball_lang: str,
    jaccard_min: float,
    min_overlap: int,
) -> tuple[str, float] | None:
    """Pick best-scoring stored key in the same language that passes both gates."""
    if not query_stems or not stored_keys:
        return None

    best_raw_key: str | None = None
    best_score = 0.0

    for raw_key in stored_keys:
        key_lang, word_key = parse_research_cache_key(raw_key)
        if key_lang != snowball_lang:
            continue
        stored_stems = stem_set_from_word_key(word_key, snowball_lang)
        if not stored_stems:
            continue
        overlap = len(query_stems & stored_stems)
        if min_overlap > 0 and overlap < min_overlap:
            continue
        score = research_cache_similarity(query_stems, stored_stems)
        if score >= jaccard_min and score > best_score:
            best_score = score
            best_raw_key = raw_key

    if best_raw_key is None:
        return None
    return best_raw_key, best_score


def lookup_research_cache(
    cache_path: str,
    word_key: str,
    snowball_lang: str,
    max_age_days: int,
    jaccard_percent: int,
    min_overlap: int,
) -> tuple[str, str, str | None, float, str] | None:
    """Return (event, display_key, matched_raw_key, jaccard, cached_value) or None on miss."""
    from plugin.contrib.smolagents.default_tools import _web_cache_get, _web_cache_list_keys

    if not cache_path or not word_key:
        return None

    prefixed = format_research_cache_key(snowball_lang, word_key)
    for storage_key in (word_key, prefixed):
        cached = _web_cache_get(cache_path, "research", storage_key, max_age_days=max_age_days)
        if cached is not None:
            matched = storage_key if storage_key != word_key else None
            return ("hit", word_key, matched, 1.0, cached)

    jaccard_min = max(0.0, min(1.0, jaccard_percent / 100.0))
    query_stems = stem_set_from_word_key(word_key, snowball_lang)
    if not query_stems:
        return None

    stored_keys = _web_cache_list_keys(cache_path, "research", max_age_days)
    fuzzy = find_fuzzy_research_match(
        query_stems,
        stored_keys,
        snowball_lang=snowball_lang,
        jaccard_min=jaccard_min,
        min_overlap=min_overlap,
    )
    if fuzzy is None:
        return None

    matched_raw_key, score = fuzzy
    cached = _web_cache_get(cache_path, "research", matched_raw_key, max_age_days=max_age_days)
    if cached is None:
        return None
    return ("hit_fuzzy", word_key, matched_raw_key, score, cached)
