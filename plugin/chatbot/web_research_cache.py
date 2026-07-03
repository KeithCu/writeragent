# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Fuzzy web-research cache matching: locale-aware Snowball stems + Jaccard similarity.
"""Pure helpers for fuzzy web research cache keys (stem + Jaccard lookup)."""

from __future__ import annotations

import logging
import hashlib
import json
import threading
import time
from typing import Any

from plugin.writer.locale.linguistic_index import _ISO_TO_SNOWBALL

log = logging.getLogger("writeragent.web_research_cache")

_SNOWBALL_LANGS = frozenset(_ISO_TO_SNOWBALL.values())
_STEMMER_CACHE: dict[str, Any] = {}
_MIN_TOKEN_LEN = 3
# (gettext LO locale, snowball_lang) -> assembled fluff + stop words
_FLUFF_WORDS_CACHE: dict[tuple[str, str], frozenset[str]] = {}
_RESEARCH_CACHE_KIND = "research"
_EMBEDDING_LOOKUP_TIMEOUT_SEC = 5
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


def _read_doc_char_locale(doc: Any) -> tuple[str, str] | None:
    """Read first-paragraph CharLocale from *doc*. Main thread only (UNO)."""
    text = doc.getText()
    enum = text.createEnumeration()
    if not enum.hasMoreElements():
        return None
    first_para = enum.nextElement()
    char_locale = first_para.getPropertyValue("CharLocale")
    lo_tag = _uno_char_locale_to_tag(char_locale)
    iso = getattr(char_locale, "Language", None)
    snowball = _ISO_TO_SNOWBALL.get(iso) if iso else None
    if lo_tag and snowball:
        return lo_tag.replace("-", "_"), snowball
    return None


def _resolve_on_main(fn):
    """Run UNO work on the main thread (web_research runs on an async worker)."""
    from plugin.framework.queue_executor import execute_on_main_thread

    return execute_on_main_thread(fn, timeout=30.0)


def resolve_research_locale(ctx: Any, doc: Any = None) -> tuple[str, str]:
    """Return (gettext_lo_locale_tag, snowball_lang) for cache key + stemming.

    Query text is not language-detected; document CharLocale first, then LO UI locale.
    UNO reads are marshalled to the main thread because callers include async web_research.
    """
    from plugin.framework.queue_executor import SendCancelled, _marshal_thread_tag

    log.debug("resolve_research_locale start doc=%s %s", doc is not None, _marshal_thread_tag())

    if doc is not None:
        try:
            doc_locale = _resolve_on_main(lambda: _read_doc_char_locale(doc))
            if doc_locale is not None:
                return doc_locale
        except SendCancelled:
            log.debug("research cache: document language detection cancelled")
        except TimeoutError:
            log.warning("research cache: document language detection timed out on main thread")
        except Exception as e:
            log.debug("research cache: document language detection failed: %s", e)

    try:
        from plugin.framework.i18n import get_lo_locale

        lo_tag = _resolve_on_main(lambda: get_lo_locale(ctx))
        return lo_tag, snowball_lang_from_locale_tag(lo_tag)
    except SendCancelled:
        log.debug("research cache: LO locale detection cancelled")
    except TimeoutError:
        log.warning("research cache: LO locale detection timed out on main thread")
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


def _embedding_text_from_raw_key(raw_key: str) -> str:
    """Embed the normalized word key, not the report body, so legacy rows can be backfilled."""
    _key_lang, word_key = parse_research_cache_key(raw_key)
    return word_key.strip()


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
    from plugin.contrib.smolagents.default_tools import HAS_SQLITE, _web_cache_with_connection

    if not HAS_SQLITE:
        return

    def do_store(conn):
        now = time.time()
        payload = []
        for raw_key, text, vector in rows:
            if not raw_key or not text or not vector:
                continue
            floats = [float(v) for v in vector]
            payload.append((
                _RESEARCH_CACHE_KIND,
                raw_key,
                embedding_model,
                _embedding_text_hash(text),
                len(floats),
                json.dumps(floats, separators=(",", ":")),
                now,
            ))
        if payload:
            conn.executemany(
                "INSERT OR REPLACE INTO web_cache_embeddings "
                "(kind, key, embedding_model, text_hash, dim, vector_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()

    _web_cache_with_connection(cache_path, do_store)


def _research_cache_embedding_rows(
    cache_path: str,
    *,
    embedding_model: str,
    max_age_days: int,
    snowball_lang: str,
) -> list[tuple[str, list[float]]]:
    if not cache_path or not embedding_model:
        return []
    from plugin.contrib.smolagents.default_tools import HAS_SQLITE, _web_cache_with_connection

    if not HAS_SQLITE:
        return []
    cutoff = time.time() - (max_age_days * 86400)

    def do_list(conn):
        return conn.execute(
            "SELECT e.key, e.text_hash, e.dim, e.vector_json "
            "FROM web_cache_embeddings e "
            "JOIN web_cache w ON w.kind = e.kind AND w.key = e.key "
            "WHERE e.kind = ? AND e.embedding_model = ? AND w.created_at >= ?",
            (_RESEARCH_CACHE_KIND, embedding_model, cutoff),
        ).fetchall()

    raw_rows = _web_cache_with_connection(cache_path, do_list)
    out: list[tuple[str, list[float]]] = []
    if not isinstance(raw_rows, list):
        return out
    for raw_key, text_hash, dim, vector_json in raw_rows:
        key_lang, word_key = parse_research_cache_key(str(raw_key))
        if key_lang != snowball_lang:
            continue
        if text_hash != _embedding_text_hash(word_key):
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
    if not cache_path or not embedding_model:
        return []
    from plugin.contrib.smolagents.default_tools import HAS_SQLITE, _web_cache_with_connection

    if not HAS_SQLITE:
        return []
    cutoff = time.time() - (max_age_days * 86400)

    def do_list(conn):
        return conn.execute(
            "SELECT w.key, e.text_hash "
            "FROM web_cache w "
            "LEFT JOIN web_cache_embeddings e ON e.kind = w.kind AND e.key = w.key AND e.embedding_model = ? "
            "WHERE w.kind = ? AND w.created_at >= ? "
            "ORDER BY w.created_at DESC",
            (embedding_model, _RESEARCH_CACHE_KIND, cutoff),
        ).fetchall()

    raw_rows = _web_cache_with_connection(cache_path, do_list)
    missing: list[tuple[str, str]] = []
    if not isinstance(raw_rows, list):
        return missing
    for raw_key, stored_hash in raw_rows:
        text = _embedding_text_from_raw_key(str(raw_key))
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
        from plugin.framework.client.embedding_client import get_embedding_model

        model = get_embedding_model().strip()
        return model or None
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
    embedding_model: str | None = None,
    timeout_sec: int = _EMBEDDING_LOOKUP_TIMEOUT_SEC,
) -> tuple[str, float] | None:
    """Pick best stored-vector match. Caller falls back to Jaccard on any miss/error."""
    if not ctx or not cache_path or not word_key or not _research_cache_embedding_configured():
        return None
    model = (embedding_model or _get_embedding_model_or_none() or "").strip()
    if not model:
        return None
    stored = _research_cache_embedding_rows(cache_path, embedding_model=model, max_age_days=max_age_days, snowball_lang=snowball_lang)
    if not stored:
        return None

    from plugin.framework.client.embedding_client import embed_texts

    batch = embed_texts(ctx, [word_key], model=model, timeout_sec=timeout_sec)
    query_vector: list[float] | None = None
    for batch_index, vector in zip(batch.indices, batch.vectors):
        if batch_index == 0:
            query_vector = vector
            break
    if not query_vector:
        return None

    best_raw_key: str | None = None
    best_score = 0.0
    threshold = max(0.0, min(1.0, similarity_min))
    for raw_key, stored_vector in stored:
        score = cosine_similarity(query_vector, stored_vector)
        if score >= threshold and score > best_score:
            best_raw_key = raw_key
            best_score = score
    if best_raw_key is None:
        return None
    return best_raw_key, best_score


def _research_cache_embedding_backfill_worker(ctx: Any, cache_path: str, max_age_days: int, embedding_model: str) -> None:
    try:
        from plugin.framework.client.embedding_client import embed_texts

        while True:
            missing = _research_cache_missing_embedding_rows(cache_path, embedding_model=embedding_model, max_age_days=max_age_days, limit=_EMBEDDING_BACKFILL_BATCH_SIZE)
            if not missing:
                return
            texts = [text for _raw_key, text in missing]
            batch = embed_texts(ctx, texts, model=embedding_model)
            by_index = {idx: vector for idx, vector in zip(batch.indices, batch.vectors)}
            rows: list[tuple[str, str, list[float]]] = []
            for idx, (raw_key, text) in enumerate(missing):
                vector = by_index.get(idx)
                if vector:
                    rows.append((raw_key, text, vector))
            store_research_cache_embeddings(cache_path, rows, embedding_model=embedding_model)
            if not rows:
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
        )
    except Exception:
        with _EMBEDDING_BACKFILL_LOCK:
            _EMBEDDING_BACKFILL_IN_FLIGHT.discard(key)
        raise


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
    *,
    ctx: Any | None = None,
    embedding_percent: int | None = None,
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
            )
            if embedding is not None:
                matched_raw_key, score = embedding
                cached = _web_cache_get(cache_path, "research", matched_raw_key, max_age_days=max_age_days)
                if cached is not None:
                    return ("hit_embedding", word_key, matched_raw_key, score, cached)
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
    )
    if fuzzy is None:
        return None

    matched_raw_key, score = fuzzy
    cached = _web_cache_get(cache_path, "research", matched_raw_key, max_age_days=max_age_days)
    if cached is None:
        return None
    return ("hit_fuzzy", word_key, matched_raw_key, score, cached)
