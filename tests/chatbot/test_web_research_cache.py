# WriterAgent - fuzzy web research cache matching tests

import sys
from unittest.mock import MagicMock

import pytest

from plugin.chatbot.web_research_cache import (
    find_fuzzy_research_match,
    format_research_cache_key,
    jaccard,
    lookup_research_cache,
    parse_research_cache_key,
    research_cache_similarity,
    stem_set_from_word_key,
    stem_word,
)

SPACE_ELEVATOR_KEY_1 = (
    "challenges concept conclusion dynamics elevator energy engineering focusing including "
    "introduction materials physical physicists physics principles produce references requirements "
    "sections space strength technical urls with"
)
@pytest.fixture(autouse=True)
def _fresh_research_fluff_cache():
    from plugin.chatbot.web_research_cache import clear_research_fluff_words_cache

    clear_research_fluff_words_cache()
    yield
    clear_research_fluff_words_cache()


@pytest.fixture(autouse=True)
def _real_snowball_stemmer():
    """test_linguistic_index.py mocks sys.modules['snowballstemmer']; clear cache and restore."""
    from plugin.chatbot import web_research_cache as wrc

    sm = sys.modules.get("snowballstemmer")
    if isinstance(sm, MagicMock):
        del sys.modules["snowballstemmer"]
        import snowballstemmer  # noqa: F401
    wrc._STEMMER_CACHE.clear()
    yield
    wrc._STEMMER_CACHE.clear()


SPACE_ELEVATOR_KEY_2 = (
    "aspects authoritative candidates carbon challenges cite climber concept distribution dynamics "
    "elevator energy engineering equations focusing geostationary graphene inclusion major material "
    "mechanics nanotubes orbit orbital physicists physics power references relevant required "
    "requirements space stability strength technical tensile tension tether velocity"
)


def test_stem_collapses_material_and_requirement_variants():
    assert stem_word("english", "materials") == stem_word("english", "material")
    assert stem_word("english", "requirements") == stem_word("english", "required")


def test_space_elevator_keys_fuzzy_match_at_40_percent():
    query_stems = stem_set_from_word_key(SPACE_ELEVATOR_KEY_2, "english")
    match = find_fuzzy_research_match(
        query_stems,
        [SPACE_ELEVATOR_KEY_1],
        snowball_lang="english",
        jaccard_min=0.40,
        min_overlap=8,
    )
    assert match is not None
    matched_key, score = match
    assert matched_key == SPACE_ELEVATOR_KEY_1
    assert score >= 0.40


def test_pizza_key_does_not_fuzzy_match_space_elevator():
    pizza_key = "heights madison pizza"
    query_stems = stem_set_from_word_key(pizza_key, "english")
    match = find_fuzzy_research_match(
        query_stems,
        [SPACE_ELEVATOR_KEY_1],
        snowball_lang="english",
        jaccard_min=0.40,
        min_overlap=8,
    )
    assert match is None


def test_lang_prefixed_cache_keys_round_trip():
    raw = format_research_cache_key("french", "bonjour monde")
    assert parse_research_cache_key(raw) == ("french", "bonjour monde")
    assert parse_research_cache_key("legacy english words") == ("english", "legacy english words")


def test_research_cache_similarity_beats_union_jaccard_for_longer_repeat_query():
    a = stem_set_from_word_key(SPACE_ELEVATOR_KEY_1, "english")
    b = stem_set_from_word_key(SPACE_ELEVATOR_KEY_2, "english")
    assert jaccard(a, b) < 0.40
    assert research_cache_similarity(a, b) >= 0.40


def test_translated_research_cache_fluff_returns_gettext_strings():
    from plugin.chatbot.research_cache_fluff import translated_research_cache_fluff

    fluff = translated_research_cache_fluff()
    assert len(fluff) >= 60
    assert all(isinstance(s, str) and s for s in fluff)


def test_get_research_fluff_words_includes_gettext_tokens(monkeypatch):
    from plugin.chatbot.web_research_cache import clear_research_fluff_words_cache, get_research_fluff_words

    def mock_(message: str) -> str:
        return {"research": "recherche", "compile": "compiler"}.get(message, message)

    clear_research_fluff_words_cache()
    monkeypatch.setattr("plugin.framework.i18n._", mock_)
    fluff = get_research_fluff_words(snowball_lang="french")
    assert "recherche" in fluff
    assert "compiler" in fluff


def test_get_research_fluff_words_cached_per_locale(monkeypatch):
    from plugin.chatbot import web_research_cache as wrc
    from plugin.chatbot.web_research_cache import clear_research_fluff_words_cache

    calls: list[int] = []

    def counting_fluff() -> tuple[str, ...]:
        calls.append(1)
        return ("research",)

    clear_research_fluff_words_cache()
    monkeypatch.setattr("plugin.chatbot.research_cache_fluff.translated_research_cache_fluff", counting_fluff)
    first = wrc.get_research_fluff_words(snowball_lang="english")
    second = wrc.get_research_fluff_words(snowball_lang="english")
    assert first is second
    assert len(calls) == 1
    other = wrc.get_research_fluff_words(snowball_lang="french")
    assert other is not first
    assert len(calls) == 2


def test_lookup_research_cache_fuzzy_hit(tmp_path):
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    db_file = str(tmp_path / "writeragent_web_cache.db")
    _web_cache_set(db_file, "research", SPACE_ELEVATOR_KEY_1, "Cached elevator report", 50 * 1024 * 1024)

    hit = lookup_research_cache(
        db_file,
        SPACE_ELEVATOR_KEY_2,
        "english",
        max_age_days=30,
        jaccard_percent=40,
        min_overlap=8,
    )
    assert hit is not None
    event, display_key, matched_raw_key, score, cached = hit
    assert event == "hit_fuzzy"
    assert display_key == SPACE_ELEVATOR_KEY_2
    assert matched_raw_key == SPACE_ELEVATOR_KEY_1
    assert score >= 0.40
    assert cached == "Cached elevator report"
