# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Fuzzy web-research cache matching: locale-aware Snowball stems + Jaccard similarity.
"""Web research cache: query fluff plus Snowball/Jaccard matching, and the SQLite/embedding lookups over the shared web_cache table."""


# crosshair: off
from __future__ import annotations

import logging
import hashlib
import json
import threading
import time
from typing import Any, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.deal_shim import deal
from plugin.framework.grammar_rule_ids import _ISO_TO_SNOWBALL

log = logging.getLogger("writeragent.web_research_cache")

_SNOWBALL_LANGS = frozenset(_ISO_TO_SNOWBALL.values())
_STEMMER_CACHE: dict[str, Any] = {}
# Pure-Python snowballstemmer.stemWord mutates cursor state on the instance.
_STEMMER_LOCK = threading.Lock()
_MIN_TOKEN_LEN = 3
# (gettext LO locale, snowball_lang) -> assembled fluff + stop words
_FLUFF_WORDS_CACHE: dict[tuple[str, str], frozenset[str]] = {}
_RESEARCH_CACHE_KIND = "research"
_DEEP_PREFIX = "deep|"
_EMBEDDING_BACKFILL_BATCH_SIZE = 32
_EMBEDDING_BACKFILL_IN_FLIGHT: set[tuple[str, str]] = set()
_EMBEDDING_BACKFILL_LOCK = threading.Lock()


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
    except ImportError:
        log.warning("No stemmer for '%s', falling back to english", snowball_lang)
        return None


def stem_word(snowball_lang: str, token: str) -> str:
    stemmer = _get_stemmer(snowball_lang)
    if stemmer is None:
        return token
    try:
        # Concurrent web_research workers share one stemmer. stemWord
        # writes cursor/limit on that object, so the lock covers the call.
        # The cache itself stays shared.
        with _STEMMER_LOCK:
            return stemmer.stemWord(token)
    except Exception:
        return token


@deal.post(lambda result: isinstance(result, str) and result in _SNOWBALL_LANGS)
def snowball_lang_from_locale_tag(tag: str) -> str:
    iso = str(tag or "").replace("-", "_").split("_")[0].lower()
    return _ISO_TO_SNOWBALL.get(iso) or "english"


def _read_doc_char_locale(doc: Any) -> tuple[str, str] | None:
    """Read first-paragraph CharLocale from *doc*. Main thread only (UNO)."""
    text = doc.getText()
    enum = text.createEnumeration()
    if not enum.hasMoreElements():
        return None
    first_para = enum.nextElement()
    char_locale = first_para.getPropertyValue("CharLocale")
    lang = str(getattr(char_locale, "Language", "") or "").strip()
    if not lang:
        return None
    country = str(getattr(char_locale, "Country", "") or "").strip()
    lo_tag = f"{lang}_{country}" if country else lang
    iso = getattr(char_locale, "Language", None)
    snowball = _ISO_TO_SNOWBALL.get(iso) if iso else None
    if lo_tag and snowball:
        return lo_tag, snowball
    return None


def _run_on_main_or_none(fn: Callable[[], Any], what: str, *, reraise_disposed: bool = False) -> Any | None:
    from plugin.framework.errors import is_disposed_exception
    from plugin.framework.queue_executor import run_on_main
    from plugin.framework.send_cancellation import SendCancelled

    try:
        return run_on_main(fn)
    except SendCancelled:
        log.debug("research cache: %s cancelled", what)
    except TimeoutError:
        log.warning("research cache: %s timed out on main thread", what)
    except Exception as e:
        if reraise_disposed and is_disposed_exception(e):
            raise
        log.debug("research cache: %s failed: %s", what, e)
    return None


def resolve_research_locale(ctx: Any, doc: Any = None) -> tuple[str, str]:
    """Return (gettext_lo_locale_tag, snowball_lang) for cache key + stemming.

    Query text is not language-detected; document CharLocale first, then LO UI locale.
    UNO reads are marshalled to the main thread because callers include async web_research.
    """
    if doc is not None:
        doc_locale = _run_on_main_or_none(
            lambda: _read_doc_char_locale(doc),
            "document language detection",
            reraise_disposed=True,
        )
        if doc_locale is not None:
            return doc_locale

    from plugin.framework.i18n import get_lo_locale

    lo_tag = _run_on_main_or_none(
        lambda: get_lo_locale(ctx),
        "LO locale detection",
    )
    if lo_tag is not None:
        return lo_tag, snowball_lang_from_locale_tag(lo_tag)
    return "en_US", "english"


def clear_research_fluff_words_cache() -> None:
    """Clear cached fluff sets (tests or after locale catalog reload)."""
    _FLUFF_WORDS_CACHE.clear()


def get_research_fluff_words(*, snowball_lang: str) -> frozenset[str]:
    """Instruction fluff from _('…') (gettext catalog locale) plus grammar stop words, normalized like query tokens."""
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
    # Stop words are stored with their accents ("até", "chère"); query tokens
    # come from _raw_tokens, which lowercases and deaccents, so union the
    # normalized form or accented stop words never filter.
    for stop_word in stop_words_for_snowball(snowball_lang):
        fluff.update(_raw_tokens(stop_word))
    result = frozenset(fluff)
    _FLUFF_WORDS_CACHE[cache_key] = result
    return result


def ordered_unique_query_words(query: str, *, snowball_lang: str) -> list[str]:  # imported by web_research.py
    """Fluff-filtered tokens, first-seen order. Storage keys sort a copy of this list."""
    if not query:
        return []
    from plugin.writer.locale.linguistic_index import _raw_tokens

    fluff = get_research_fluff_words(snowball_lang=snowball_lang)
    seen: set[str] = set()
    ordered: list[str] = []
    for token in _raw_tokens(query):
        if token in fluff or len(token) < _MIN_TOKEN_LEN or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _same_mode_and_lang(raw_key: str, *, mode: str, snowball_lang: str) -> str | None:
    """Word key when raw_key is the same mode and Snowball language, else None.
    Mode is checked before language. parse strips ``deep|``, so a deep row and
    a shallow row for the same words share a language.
    """
    key_mode = "deep" if str(raw_key or "").startswith(_DEEP_PREFIX) else ""
    if key_mode != mode:
        return None
    key_lang, word_key = parse_research_cache_key(raw_key)
    if key_lang != snowball_lang:
        return None
    return word_key


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2)
def parse_research_cache_key(raw_key: str) -> tuple[str, str]:
    """Return (snowball_lang, word_key). Legacy unprefixed keys are english.

    Deep rows are stored as ``deep|{lang}|{words}``. Strip that leading
    segment here so this stays a 2-tuple (deal post and hypothesis unpack it).
    Mode is checked via ``_DEEP_PREFIX``, not a third element.
    """
    # A shallow key never starts with deep|; stripping only that prefix keeps
    # legacy ``english|words`` and bare keys on the old parse path.
    key = raw_key[len(_DEEP_PREFIX):] if raw_key.startswith(_DEEP_PREFIX) else raw_key
    if "|" in key:
        lang, _unused, word_key = key.partition("|")
        if lang in _SNOWBALL_LANGS:
            return lang, word_key
    return "english", key


@deal.post(lambda result: isinstance(result, str))
def format_research_cache_key(snowball_lang: str, word_key: str, *, mode: str = "") -> str:
    """Storage key. ``mode='deep'`` is ``deep|{lang}|{words}`` in the same file.

    An empty word key is returned unchanged (a blank query must not become
    ``english|`` or ``deep|english|``).
    """
    if not word_key:
        return word_key
    lang = snowball_lang if snowball_lang in _SNOWBALL_LANGS else "english"
    body = f"{lang}|{word_key}"
    if mode == "deep":
        return f"{_DEEP_PREFIX}{body}"
    return body


def stem_set_from_word_key(word_key: str, snowball_lang: str) -> set[str]:
    stems: set[str] = set()
    for token in word_key.split():
        if len(token) < _MIN_TOKEN_LEN:
            continue
        stem = stem_word(snowball_lang, token)
        if stem:
            stems.add(stem)
    return stems


@deal.post(lambda result: isinstance(result, float) and 0.0 <= result <= 1.0)
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


def _research_cache_embedding_configured() -> bool:
    """True when the local embeddings venv is configured enough to try worker RPC."""
    try:
        from plugin.framework.config import get_config, get_config_str

        provider = str(get_config("embedding_provider") or "local").strip().lower() or "local"
        if provider != "local":
            return False
        return bool(get_config_str("scripting.python_venv_path").strip())
    except Exception:
        return False


def _embedding_text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def _vector_from_json(raw: str, dim: int) -> list[float] | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list) or len(data) != dim:
        return None
    try:
        return [float(v) for v in data]
    except (TypeError, ValueError):
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for av, bv in zip(a, b):
        dot += av * bv
        norm_a += av * av
        norm_b += bv * bv
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


def store_research_cache_embeddings(
    cache_path: str,
    rows: list[tuple[str, str, list[float]]],
    *,
    embedding_model: str,
) -> None:
    """Store vectors for research cache keys. Additive table keeps old cache rows valid."""
    if not cache_path or not rows or not embedding_model:
        return
    from plugin.contrib.smolagents.default_tools import _web_cache_with_connection

    def do_store(conn: Any) -> None:
        now = time.time()
        attempted = False
        for raw_key, text, vector in rows:
            if not raw_key or not text or not vector:
                continue
            floats = [float(v) for v in vector]
            conn.execute(
                "INSERT OR REPLACE INTO web_cache_embeddings "
                "(kind, key, embedding_model, embedding_text, text_hash, dim, vector_json, created_at) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM web_cache WHERE kind = ? AND key = ?)",
                (
                    _RESEARCH_CACHE_KIND,
                    raw_key,
                    embedding_model,
                    text,
                    _embedding_text_hash(text),
                    len(floats),
                    json.dumps(floats, separators=(",", ":")),
                    now,
                    _RESEARCH_CACHE_KIND,
                    raw_key,
                ),
            )
            attempted = True
        if attempted:
            conn.commit()

    _web_cache_with_connection(cache_path, do_store)


def _cache_cutoff(max_age_days: int) -> float:
    return time.time() - (max_age_days * 86400)


def _query_web_cache_rows(cache_path: str, sql: str, params: tuple[Any, ...]) -> list[Any]:
    from plugin.contrib.smolagents.default_tools import HAS_SQLITE, _web_cache_with_connection

    if not HAS_SQLITE:
        return []
    return cast("list[Any]", _web_cache_with_connection(cache_path, lambda conn: conn.execute(sql, params).fetchall()))


def _research_cache_embedding_rows(
    cache_path: str,
    *,
    embedding_model: str,
    max_age_days: int,
    snowball_lang: str,
    mode: str = "",
) -> list[tuple[str, list[float]]]:
    cutoff = _cache_cutoff(max_age_days)
    raw_rows = _query_web_cache_rows(
        cache_path,
        "SELECT e.key, e.embedding_text, e.text_hash, e.dim, e.vector_json "
        "FROM web_cache_embeddings e "
        "JOIN web_cache w ON w.kind = e.kind AND w.key = e.key "
        "WHERE e.kind = ? AND e.embedding_model = ? AND w.created_at >= ?",
        (_RESEARCH_CACHE_KIND, embedding_model, cutoff),
    )
    out: list[tuple[str, list[float]]] = []
    for raw_key, embedding_text, text_hash, dim, vector_json in raw_rows:
        word_key = _same_mode_and_lang(str(raw_key), mode=mode, snowball_lang=snowball_lang)
        if word_key is None:
            continue
        stored_text = str(embedding_text or "").strip() or word_key
        if text_hash != _embedding_text_hash(stored_text):
            continue
        vector = _vector_from_json(str(vector_json or ""), int(dim or 0))
        if vector:
            out.append((str(raw_key), vector))
    return out


def _research_cache_missing_embedding_rows(
    cache_path: str,
    *,
    embedding_model: str,
    max_age_days: int,
    limit: int,
) -> list[tuple[str, str]]:
    cutoff = _cache_cutoff(max_age_days)
    raw_rows = _query_web_cache_rows(
        cache_path,
        "SELECT w.key, e.embedding_text, e.text_hash "
        "FROM web_cache w "
        "LEFT JOIN web_cache_embeddings e ON e.kind = w.kind AND e.key = w.key AND e.embedding_model = ? "
        "WHERE w.kind = ? AND w.created_at >= ? "
        "ORDER BY w.created_at DESC",
        (embedding_model, _RESEARCH_CACHE_KIND, cutoff),
    )
    missing: list[tuple[str, str]] = []
    for raw_key, embedding_text, stored_hash in raw_rows:
        stored_text = str(embedding_text or "").strip()
        text = stored_text or parse_research_cache_key(str(raw_key))[1].strip()
        if not text:
            continue
        if stored_hash == _embedding_text_hash(text):
            continue
        missing.append((str(raw_key), text))
        if limit > 0 and len(missing) >= limit:
            break
    return missing


def _get_embedding_model_or_none() -> str | None:
    try:
        from plugin.embeddings.embedding_client import get_embedding_model

        return get_embedding_model()
    except Exception as e:
        log.debug("research cache embeddings: model unavailable: %s", e)
        return None


def find_embedding_research_match(
    ctx: Any,
    cache_path: str,
    word_key: str,
    *,
    snowball_lang: str,
    max_age_days: int,
    similarity_min: float,
    embedding_text: str | None = None,
    mode: str = "",
) -> tuple[str, float] | None:
    """Pick best stored-vector match. Caller falls back to Jaccard on any miss/error."""
    if not cache_path or not word_key or not _research_cache_embedding_configured():
        return None
    model = _get_embedding_model_or_none()
    if not model:
        return None
    stored = _research_cache_embedding_rows(cache_path, embedding_model=model, max_age_days=max_age_days, snowball_lang=snowball_lang, mode=mode)
    if not stored:
        return None

    from plugin.embeddings.embedding_client import embed_texts

    query_text = str(embedding_text or "").strip() or word_key
    # Omit timeout_sec so embed_texts uses embeddings_worker_timeout_sec (long trusted budget).
    batch = embed_texts(ctx, [query_text], model=model)
    query_vector = dict(zip(batch.indices, batch.vectors)).get(0)
    if not query_vector:
        return None

    best_raw_key: str | None = None
    best_score = 0.0
    for raw_key, stored_vector in stored:
        score = cosine_similarity(query_vector, stored_vector)
        if score >= similarity_min and score > best_score:
            best_raw_key = raw_key
            best_score = score
    if best_raw_key is None:
        return None
    return best_raw_key, best_score


def _embed_and_store(ctx: Any, cache_path: str, model: str, pairs: list[tuple[str, str]]) -> bool:
    from plugin.embeddings.embedding_client import embed_texts

    texts = [text for _raw_key, text in pairs]
    batch = embed_texts(ctx, texts, model=model)
    by_index = dict(zip(batch.indices, batch.vectors))
    rows: list[tuple[str, str, list[float]]] = []
    for idx, (raw_key, text) in enumerate(pairs):
        vector = by_index.get(idx)
        if vector:
            rows.append((raw_key, text, vector))
    if rows:
        store_research_cache_embeddings(cache_path, rows, embedding_model=model)
    return bool(rows)


def _research_cache_embedding_backfill_worker(ctx: Any, cache_path: str, max_age_days: int, embedding_model: str) -> None:
    try:
        while True:
            missing = _research_cache_missing_embedding_rows(cache_path, embedding_model=embedding_model, max_age_days=max_age_days, limit=_EMBEDDING_BACKFILL_BATCH_SIZE)
            if not missing:
                return
            if not _embed_and_store(ctx, cache_path, embedding_model, missing):
                return
    except Exception as e:
        log.debug("research cache embeddings backfill skipped: %s", e)
    finally:
        with _EMBEDDING_BACKFILL_LOCK:
            _EMBEDDING_BACKFILL_IN_FLIGHT.discard((cache_path, embedding_model))


def enqueue_research_cache_embedding_backfill(ctx: Any, cache_path: str, max_age_days: int) -> None:
    """Warm missing research-cache embeddings in the background; never blocks web research."""
    if not ctx or not cache_path or not _research_cache_embedding_configured():
        return
    embedding_model = _get_embedding_model_or_none()
    if not embedding_model:
        return
    key = (cache_path, embedding_model)
    with _EMBEDDING_BACKFILL_LOCK:
        if key in _EMBEDDING_BACKFILL_IN_FLIGHT:
            return
        _EMBEDDING_BACKFILL_IN_FLIGHT.add(key)
    try:
        from plugin.framework.worker_pool import run_in_background

        run_in_background(
            _research_cache_embedding_backfill_worker,
            ctx,
            cache_path,
            max_age_days,
            embedding_model,
            name="web-research-cache-embeddings",
            dedicated=True,
        )
    except Exception:
        with _EMBEDDING_BACKFILL_LOCK:
            _EMBEDDING_BACKFILL_IN_FLIGHT.discard(key)
        raise


def _research_cache_embedding_row_worker(ctx: Any, cache_path: str, raw_key: str, embedding_text: str, embedding_model: str) -> None:
    try:
        text = str(embedding_text or "").strip()
        if not text:
            return
        _embed_and_store(ctx, cache_path, embedding_model, [(raw_key, text)])
    except Exception as e:
        log.debug("research cache embedding row skipped: %s", e)


def enqueue_research_cache_embedding_for_row(ctx: Any, cache_path: str, raw_key: str, embedding_text: str) -> None:
    """Embed one new research-cache key using order-preserving query terms, off the hot path."""
    if not ctx or not cache_path or not raw_key or not embedding_text or not _research_cache_embedding_configured():
        return
    embedding_model = _get_embedding_model_or_none()
    if not embedding_model:
        return
    from plugin.framework.worker_pool import run_in_background

    run_in_background(
        _research_cache_embedding_row_worker,
        ctx,
        cache_path,
        raw_key,
        embedding_text,
        embedding_model,
        name="web-research-cache-embedding-row",
        dedicated=True,
    )


def find_fuzzy_research_match(
    query_stems: set[str],
    stored_keys: list[str],
    *,
    snowball_lang: str,
    jaccard_min: float,
    min_overlap: int,
    mode: str = "",
) -> tuple[str, float] | None:
    """Pick best-scoring stored key in the same mode and language that passes both gates."""
    if not query_stems or not stored_keys:
        return None

    best_raw_key: str | None = None
    best_score = 0.0

    for raw_key in stored_keys:
        word_key = _same_mode_and_lang(raw_key, mode=mode, snowball_lang=snowball_lang)
        if word_key is None:
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
    *,
    ctx: Any | None = None,
    embedding_percent: int | None = None,
    embedding_text: str | None = None,
    mode: str = "",
) -> tuple[str, str | None, float, str] | None:
    """Return (event, matched_raw_key, score, cached_value) or None on miss.

    English shallow lookup tries the bare word key, then the language-prefixed key.
    Other languages try only the prefixed key, so a legacy English row is not a hit.
    ``mode='deep'`` tries only ``deep|{lang}|{words}`` so a shallow row is not
    served as a deep report.
    """
    from plugin.contrib.smolagents.default_tools import _web_cache_get, _web_cache_list_keys

    if not cache_path or not word_key:
        return None

    exact_keys: list[str] = []
    if mode != "deep" and snowball_lang == "english":
        # Legacy rows were stored under the bare word key before language
        # prefixes. A French query whose words are "paris restaurants" must
        # not hit that English row (and must not refresh its created_at).
        exact_keys.append(word_key)
    # Do not fall through to the bare or shallow-prefixed key. Those rows
    # are a different report (ordinary web_research / brainstorm), and
    # returning them made Deep Research skip the live run.
    exact_keys.append(format_research_cache_key(snowball_lang, word_key, mode=mode))
    for storage_key in exact_keys:
        cached = _web_cache_get(cache_path, "research", storage_key, max_age_days=max_age_days)
        if cached is not None:
            matched = storage_key if storage_key != word_key else None
            return ("hit", matched, 1.0, cached)

    jaccard_min = max(0.0, min(1.0, jaccard_percent / 100.0))
    embedding_min = max(0.0, min(1.0, (embedding_percent if embedding_percent is not None else jaccard_percent) / 100.0))
    if ctx is not None:
        try:
            embedding = find_embedding_research_match(
                ctx,
                cache_path,
                word_key,
                snowball_lang=snowball_lang,
                max_age_days=max_age_days,
                similarity_min=embedding_min,
                embedding_text=embedding_text,
                mode=mode,
            )
            if embedding is not None:
                matched_raw_key, score = embedding
                cached = _web_cache_get(cache_path, "research", matched_raw_key, max_age_days=max_age_days)
                if cached is not None:
                    return ("hit_embedding", matched_raw_key, score, cached)
        except Exception as e:
            # Embeddings are opportunistic: cold/missing venv, model download, or worker errors must not slow or break web research.
            log.debug("research cache embeddings lookup skipped: %s", e)

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
        mode=mode,
    )
    if fuzzy is None:
        return None

    matched_raw_key, score = fuzzy
    cached = _web_cache_get(cache_path, "research", matched_raw_key, max_age_days=max_age_days)
    if cached is None:
        return None
    return ("hit_fuzzy", matched_raw_key, score, cached)
