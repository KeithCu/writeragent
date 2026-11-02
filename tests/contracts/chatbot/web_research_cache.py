"""Contracts sidecar for plugin.chatbot.web_research_cache."""

from __future__ import annotations

import deal

from plugin.chatbot import web_research_cache as _prod
from plugin.chatbot.web_research_cache import _SNOWBALL_LANGS

snowball_lang_from_locale_tag = deal.post(
    lambda result: isinstance(result, str) and result in _SNOWBALL_LANGS
)(_prod.snowball_lang_from_locale_tag)

parse_research_cache_key = deal.post(
    lambda result: isinstance(result, tuple) and len(result) == 2
)(_prod.parse_research_cache_key)

format_research_cache_key = deal.post(
    lambda result: isinstance(result, str)
)(_prod.format_research_cache_key)

research_cache_similarity = deal.post(
    lambda result: isinstance(result, float) and 0.0 <= result <= 1.0
)(_prod.research_cache_similarity)

PYTEST = [
    "snowball_lang_from_locale_tag",
    "parse_research_cache_key",
    "format_research_cache_key",
    "research_cache_similarity",
]

TARGETS: list[str] = []
