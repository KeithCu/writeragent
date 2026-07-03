# WriterAgent - fuzzy web research cache matching tests

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.web_research_cache import (
    _read_doc_char_locale,
    _research_cache_embedding_backfill_worker,
    find_fuzzy_research_match,
    format_research_cache_key,
    jaccard,
    lookup_research_cache,
    parse_research_cache_key,
    research_cache_similarity,
    resolve_research_locale,
    stem_set_from_word_key,
    stem_word,
    store_research_cache_embeddings,
)
from plugin.framework.client.embedding_client import EmbeddingBatch

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


def test_web_cache_schema_adds_embeddings_table_to_existing_db(tmp_path):
    import sqlite3
    from plugin.contrib.smolagents.default_tools import _web_cache_get

    db_file = str(tmp_path / "writeragent_web_cache.db")
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "CREATE TABLE web_cache "
            "(kind TEXT, key TEXT, value TEXT, size INTEGER, created_at REAL, PRIMARY KEY (kind, key))"
        )
        conn.commit()
    finally:
        conn.close()

    assert _web_cache_get(db_file, "research", "missing", max_age_days=30) is None

    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'web_cache_embeddings'"
        ).fetchone()
        columns = {col[1] for col in conn.execute("PRAGMA table_info(web_cache_embeddings)").fetchall()}
    finally:
        conn.close()
    assert row == ("web_cache_embeddings",)
    assert "embedding_text" in columns


def test_lookup_research_cache_embedding_hit(tmp_path):
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    db_file = str(tmp_path / "writeragent_web_cache.db")
    raw_key = "english|best nearby pizza"
    _web_cache_set(db_file, "research", raw_key, "Cached pizza report", 50 * 1024 * 1024)
    _web_cache_set(db_file, "research", "english|space elevator physics", "Cached space report", 50 * 1024 * 1024)
    store_research_cache_embeddings(
        db_file,
        [
            (raw_key, "best nearby pizza", [1.0, 0.0]),
            ("english|space elevator physics", "space elevator physics", [0.0, 1.0]),
        ],
        embedding_model="test-model",
    )

    with patch("plugin.chatbot.web_research_cache._research_cache_embedding_configured", return_value=True), \
         patch("plugin.chatbot.web_research_cache._get_embedding_model_or_none", return_value="test-model"), \
         patch(
             "plugin.framework.client.embedding_client.embed_texts",
             return_value=EmbeddingBatch(model="test-model", dim=2, vectors=[[1.0, 0.0]], indices=[0]),
         ) as embed_texts:
        hit = lookup_research_cache(
            db_file,
            "good pizza around",
            "english",
            max_age_days=30,
            jaccard_percent=60,
            min_overlap=8,
            ctx=object(),
            embedding_text="pizza around good",
        )

    embed_texts.assert_called_once()
    assert embed_texts.call_args.args[1] == ["pizza around good"]
    assert hit is not None
    event, display_key, matched_raw_key, score, cached = hit
    assert event == "hit_embedding"
    assert display_key == "good pizza around"
    assert matched_raw_key == raw_key
    assert score == pytest.approx(1.0)
    assert cached == "Cached pizza report"


def test_lookup_research_cache_embedding_model_mismatch_falls_back_to_jaccard(tmp_path):
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    db_file = str(tmp_path / "writeragent_web_cache.db")
    _web_cache_set(db_file, "research", SPACE_ELEVATOR_KEY_1, "Cached elevator report", 50 * 1024 * 1024)
    store_research_cache_embeddings(
        db_file,
        [(SPACE_ELEVATOR_KEY_1, SPACE_ELEVATOR_KEY_1, [1.0, 0.0])],
        embedding_model="old-model",
    )

    with patch("plugin.chatbot.web_research_cache._research_cache_embedding_configured", return_value=True), \
         patch("plugin.chatbot.web_research_cache._get_embedding_model_or_none", return_value="new-model"), \
         patch("plugin.framework.client.embedding_client.embed_texts") as embed_texts:
        hit = lookup_research_cache(
            db_file,
            SPACE_ELEVATOR_KEY_2,
            "english",
            max_age_days=30,
            jaccard_percent=40,
            min_overlap=8,
            ctx=object(),
        )

    embed_texts.assert_not_called()
    assert hit is not None
    assert hit[0] == "hit_fuzzy"
    assert hit[4] == "Cached elevator report"


def test_research_cache_embedding_backfill_worker_stores_missing_vectors(tmp_path):
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    db_file = str(tmp_path / "writeragent_web_cache.db")
    _web_cache_set(db_file, "research", "english|best nearby pizza", "Cached pizza report", 50 * 1024 * 1024)
    _web_cache_set(db_file, "research", "english|space elevator physics", "Cached space report", 50 * 1024 * 1024)

    def fake_embed_texts(ctx, texts, *, model=None, timeout_sec=None):
        del ctx, timeout_sec
        assert model == "test-model"
        vectors = [[1.0, 0.0] if "pizza" in text else [0.0, 1.0] for text in texts]
        return EmbeddingBatch(model="test-model", dim=2, vectors=vectors, indices=list(range(len(texts))))

    with patch("plugin.framework.client.embedding_client.embed_texts", side_effect=fake_embed_texts):
        _research_cache_embedding_backfill_worker(object(), db_file, 30, "test-model")

    with patch("plugin.chatbot.web_research_cache._research_cache_embedding_configured", return_value=True), \
         patch("plugin.chatbot.web_research_cache._get_embedding_model_or_none", return_value="test-model"), \
         patch(
             "plugin.framework.client.embedding_client.embed_texts",
             return_value=EmbeddingBatch(model="test-model", dim=2, vectors=[[0.0, 1.0]], indices=[0]),
         ):
        hit = lookup_research_cache(
            db_file,
            "orbital tether mechanics",
            "english",
            max_age_days=30,
            jaccard_percent=60,
            min_overlap=8,
            ctx=object(),
        )

    assert hit is not None
    assert hit[0] == "hit_embedding"
    assert hit[2] == "english|space elevator physics"


def test_read_doc_char_locale_from_first_paragraph():
    char_locale = MagicMock(Language="fr", Country="FR")
    first_para = MagicMock()
    first_para.getPropertyValue.return_value = char_locale
    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    enum.nextElement.return_value = first_para
    text = MagicMock()
    text.createEnumeration.return_value = enum
    doc = MagicMock()
    doc.getText.return_value = text

    assert _read_doc_char_locale(doc) == ("fr_FR", "french")


def test_read_doc_char_locale_empty_doc_returns_none():
    enum = MagicMock()
    enum.hasMoreElements.return_value = False
    text = MagicMock()
    text.createEnumeration.return_value = enum
    doc = MagicMock()
    doc.getText.return_value = text

    assert _read_doc_char_locale(doc) is None


def test_resolve_research_locale_uses_main_thread_for_doc():
    doc = MagicMock()
    calls: list[str] = []

    def fake_execute(fn, *args, **kwargs):
        calls.append("main")
        return fn()

    with patch("plugin.chatbot.web_research_cache._resolve_on_main", side_effect=fake_execute), \
         patch("plugin.chatbot.web_research_cache._read_doc_char_locale", return_value=("de_DE", "german")) as read_doc:
        result_holder: list[tuple[str, str]] = []

        def worker():
            result_holder.append(resolve_research_locale(None, doc))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert result_holder == [("de_DE", "german")]
        assert calls == ["main"]
        read_doc.assert_called_once_with(doc)


def test_resolve_research_locale_send_cancelled_falls_back():
    from plugin.framework.queue_executor import SendCancelled

    with patch("plugin.chatbot.web_research_cache._resolve_on_main", side_effect=SendCancelled()):
        assert resolve_research_locale(None, MagicMock()) == ("en_US", "english")
