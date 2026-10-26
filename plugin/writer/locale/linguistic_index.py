# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""IndexService — in-memory inverted index with Snowball stemming.

Ported from mcp-libre services/writer/index.py.
Language detected from UNO CharLocale. Stemming via bundled snowballstemmer.
"""

from collections import OrderedDict
from functools import lru_cache
import logging
import re
import time
from typing import Any, ClassVar
import unicodedata

from plugin.framework.errors import ToolExecutionError
from plugin.framework.service import ServiceBase
from plugin.doc.document_helpers import is_cacheable_doc_key
from plugin.writer.locale.stop_words import stop_words_for_snowball
from plugin.doc.text_helpers import get_string_without_tracked_deletions

log = logging.getLogger("writeragent.writer.index")

# ── Language mapping (ISO 639-1 -> snowballstemmer algorithm) ─────────

from plugin.framework.grammar_rule_ids import _ISO_TO_SNOWBALL

# ── Tokenisation ──────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_NOT_RE = re.compile(r"\bNOT\b", re.IGNORECASE)
_NEAR_RE = re.compile(r"(.+?)\s+NEAR/(\d+)\s+(.+)", re.IGNORECASE)
_NEAR_COUNT_RE = re.compile(r"NEAR/\d+", re.IGNORECASE)
_AND_RE = re.compile(r"\bAND\b", re.IGNORECASE)
_OR_RE = re.compile(r"\bOR\b", re.IGNORECASE)
_MIN_TOKEN_LEN = 2
_MAX_CACHED_DOCS = 4


def _deaccent(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def _raw_tokens(text: str) -> list[str]:
    cleaned = _PUNCT_RE.sub(" ", _deaccent(text.lower()))
    return [t for t in cleaned.split() if len(t) >= _MIN_TOKEN_LEN]


@lru_cache(maxsize=None)
def _normalized_stop_words(snowball_lang: str) -> frozenset[str]:
    """Stop words run through ``_raw_tokens`` so accented and punctuated
    entries match document tokens. Multi-word entries contribute only
    fragments that are standalone entries, so ``c'mon`` does not drop ``mon``."""
    raw = stop_words_for_snowball(snowball_lang)
    out: set[str] = set()
    for entry in raw:
        tokens = _raw_tokens(entry)
        if len(tokens) == 1:
            out.add(tokens[0])
        elif tokens:
            out.update(t for t in tokens if t in raw)
    return frozenset(out)


def _locale_language(loc: Any) -> str | None:
    """ISO language of a UNO Locale, lowercased; 'zxx' (no linguistic content) is ignored."""
    raw = str(getattr(loc, "Language", "") or "").strip().lower()
    iso = raw.replace("-", "_").split("_")[0]
    if len(iso) not in (2, 3) or iso == "zxx":
        return None
    return iso


def _doc_locale_language(doc: Any) -> str | None:
    for prop in ("CharLocale", "CharLocaleAsian", "CharLocaleComplex"):
        try:
            iso = _locale_language(doc.getPropertyValue(prop))
            if iso is not None:
                return iso
        except Exception:  # noqa: S110 - not every document model exposes all three
            pass
    return None


# ── Per-document index ────────────────────────────────────────────────


class _DocIndex:
    para_count: int
    build_ms: float
    language: str
    detected_language: str | None
    stem_fallback: bool
    __slots__: ClassVar[tuple[str, ...]] = (
        "terms",
        "para_texts",
        "para_count",
        "build_ms",
        "language",
        "detected_language",
        "stem_fallback",
    )

    def __init__(self) -> None:
        self.terms: dict[str, set[int]] = {}
        self.para_texts: dict[int, str] = {}
        self.para_count = 0
        self.build_ms = 0.0
        self.language = "english"
        self.detected_language = None
        self.stem_fallback = False

    def _paragraphs_for(self, stems: list[str]) -> set[int]:
        found: set[int] = set()
        for stem in stems:
            ps = self.terms.get(stem)
            if ps:
                found |= ps
        return found

    def query_and(self, stem_groups: list[list[str]]) -> set[int]:
        if not stem_groups:
            return set()
        sets: list[set[int]] = []
        for group in stem_groups:
            s = self._paragraphs_for(group)
            if not s:
                return set()
            sets.append(s)
        sets.sort(key=len)
        result = sets[0].copy()
        for s in sets[1:]:
            result &= s
            if not result:
                return result
        return result

    def query_or(self, stems: list[str]) -> set[int]:
        return self._paragraphs_for(stems)

    def query_not(self, include: set[int], exclude_stems: list[str]) -> set[int]:
        result = include.copy()
        for stem in exclude_stems:
            ps = self.terms.get(stem)
            if ps:
                result -= ps
        return result

    def query_near(self, stems_a: list[str], stems_b: list[str], distance: int) -> set[int]:
        set_a = self._paragraphs_for(stems_a)
        set_b = self._paragraphs_for(stems_b)
        if not set_a or not set_b:
            return set()
        result = set()
        sorted_b = sorted(set_b)
        for pa in set_a:
            for pb in sorted_b:
                if abs(pa - pb) <= distance:
                    result.add(pa)
                    result.add(pb)
                elif pb > pa + distance:
                    break
        return result


_TOP_STEMS = 20


def _index_counts(idx: _DocIndex, cached: bool) -> dict[str, Any]:
    return {
        "paragraphs": idx.para_count,
        "unique_stems": len(idx.terms),
        "build_ms": idx.build_ms,
        "cached": cached,
    }


# ── Service ───────────────────────────────────────────────────────────


class IndexService(ServiceBase):
    """Per-document inverted index with Snowball stemming."""

    name: str | None = "writer_index"

    _doc_svc: Any
    _bm_svc: Any

    def __init__(self, services: Any) -> None:
        self._doc_svc = services.document
        self._bm_svc = services.writer_bookmarks
        events = services.events
        self._cache: OrderedDict[str, tuple[int | None, _DocIndex]] = OrderedDict()  # doc_key -> (CharacterCount, index)
        self._stemmers: dict[str, Any] = {}  # lang -> StemmerInstance
        events.subscribe("document:cache_invalidated", self._on_cache_invalidated)

    def _on_cache_invalidated(self, doc: Any = None, key: Any = None, **_kw: Any) -> None:
        # key= first: close/unload emits the stored key without a live model.
        if key is not None:
            self._cache.pop(key, None)
        elif doc is None:
            self._cache.clear()
        else:
            self._cache.pop(self._doc_svc.doc_key(doc), None)

    # ── Stemmer management ────────────────────────────────────────

    def _get_stemmer(self, lang: str) -> Any:
        if lang in self._stemmers:
            return self._stemmers[lang]
        try:
            import snowballstemmer  # type: ignore[import-untyped]

            s = snowballstemmer.stemmer(lang)
        except (ImportError, KeyError):
            s = self._get_stemmer("english") if lang != "english" else None
            log.warning("No stemmer for '%s', falling back to english", lang)
        self._stemmers[lang] = s
        return s

    def _detect_language_iso(self, doc: Any) -> str | None:
        doc_lang = _doc_locale_language(doc)
        if doc_lang is not None:
            return doc_lang
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            el = enum.nextElement()
            if not el.supportsService("com.sun.star.text.Paragraph"):
                continue
            if not get_string_without_tracked_deletions(el).strip():
                continue
            try:
                loc = el.getPropertyValue("CharLocale")
            except Exception:  # noqa: S112 - missing CharLocale property falls through to next paragraph
                continue
            iso = _locale_language(loc)
            if iso is not None:
                return iso
        return None

    def _stem(self, stemmer: Any, tokens: list[str], stop_words: frozenset[str]) -> list[str]:
        return [stemmer.stemWord(t) for t in tokens if t not in stop_words]

    # ── Index build ───────────────────────────────────────────────

    def _get_index(self, doc: Any) -> tuple[_DocIndex, bool]:
        """Get or build the inverted index. Returns (index, was_cached)."""
        key = self._doc_svc.doc_key(doc)
        # A missed XModifyListener, or a mutation inside
        # DocumentService.ignore_cache_invalidation(), leaves the listener
        # cache stale; CharacterCount still moves. Same rule as
        # tree._heading_tree_fingerprint: no count -> listener-only.
        count = getattr(doc, "CharacterCount", None)
        fingerprint = int(count) if count is not None else None
        if is_cacheable_doc_key(key):
            cached = self._cache.get(key)
            if cached is not None and (fingerprint is None or fingerprint == cached[0]):
                self._cache.move_to_end(key)
                return cached[1], True

        t0 = time.perf_counter()
        detected = self._detect_language_iso(doc)
        lang = (_ISO_TO_SNOWBALL.get(detected) if detected else None) or "english"
        stemmer = self._get_stemmer(lang)
        stop_words = _normalized_stop_words(lang)

        idx = _DocIndex()
        idx.detected_language = detected
        idx.stem_fallback = detected is not None and detected not in _ISO_TO_SNOWBALL
        idx.language = lang
        text_obj = doc.getText()
        enum = text_obj.createEnumeration()
        para_i = 0

        while enum.hasMoreElements():
            el = enum.nextElement()
            if el.supportsService("com.sun.star.text.Paragraph"):
                text = get_string_without_tracked_deletions(el)
                idx.para_texts[para_i] = text
                raw = _raw_tokens(text)
                if stemmer:
                    stems = self._stem(stemmer, raw, stop_words)
                else:
                    stems = [t for t in raw if t not in stop_words]
                for stem in stems:
                    s = idx.terms.get(stem)
                    if s is None:
                        s = set()
                        idx.terms[stem] = s
                    s.add(para_i)
            else:
                idx.para_texts[para_i] = "[Table]"
            para_i += 1

        idx.para_count = para_i
        idx.build_ms = round((time.perf_counter() - t0) * 1000, 1)
        if is_cacheable_doc_key(key):
            self._cache[key] = (fingerprint, idx)
            while len(self._cache) > _MAX_CACHED_DOCS:
                self._cache.popitem(last=False)
        log.info("Index built [%s]: %d paras, %d stems, %.1fms", lang, para_i, len(idx.terms), idx.build_ms)
        return idx, False

    # ── Query parsing ─────────────────────────────────────────────

    def _stem_query_tokens(self, text: str, stemmer: Any, stop_words: frozenset[str]) -> tuple[list[str], list[str]]:
        raw = _raw_tokens(text)
        stems = []
        dropped = []
        for t in raw:
            if t in stop_words:
                dropped.append(t)
            else:
                stems.append(stemmer.stemWord(t) if stemmer else t)
        return stems, dropped

    def _parse_query(self, query: str, stemmer: Any, stop_words: frozenset[str]) -> dict[str, Any]:
        result: dict[str, Any] = {"and_stems": [], "or_stems": [], "not_stems": [], "near": [], "dropped_stops": [], "mode": "and", "error": None}

        not_split = _NOT_RE.split(query)
        main_part = not_split[0].strip()
        for part in not_split[1:]:
            stems, dropped = self._stem_query_tokens(part, stemmer, stop_words)
            result["not_stems"].extend(stems)
            result["dropped_stops"].extend(dropped)

        if len(_NEAR_COUNT_RE.findall(main_part)) > 1:
            result["error"] = "Only one NEAR/N is supported per query."
            return result

        # NEAR/N
        near_match = _NEAR_RE.search(main_part)
        if near_match:
            left, dropped_l = self._stem_query_tokens(near_match.group(1), stemmer, stop_words)
            dist = int(near_match.group(2))
            right, dropped_r = self._stem_query_tokens(near_match.group(3), stemmer, stop_words)
            result["dropped_stops"].extend(dropped_l + dropped_r)
            if left and right:
                result["near"].append((left, right, dist))
                result["mode"] = "near"
            elif not left and not right:
                result["error"] = "NEAR terms are all stop words"
            return result
 
        has_and = bool(_AND_RE.search(main_part))
        has_or = bool(_OR_RE.search(main_part))
        if has_and and has_or:
            result["error"] = "Mixed AND/OR not supported. Use one operator per query."
            return result
 
        if has_or:
            chunks = _OR_RE.split(main_part)
            for chunk in chunks:
                stems, dropped = self._stem_query_tokens(chunk, stemmer, stop_words)
                result["or_stems"].extend(stems)
                result["dropped_stops"].extend(dropped)
            result["mode"] = "or"
        else:
            if has_and:
                chunks = _AND_RE.split(main_part)
            else:
                chunks = [main_part]
            for chunk in chunks:
                stems, dropped = self._stem_query_tokens(chunk, stemmer, stop_words)
                for stem in stems:
                    result["and_stems"].append([stem])
                result["dropped_stops"].extend(dropped)
            result["mode"] = "and"

        return result

    # ── Query Execution & Formatting ──────────────────────────────

    def _execute_query(self, idx: _DocIndex, mode: str, near: Any, or_stems: Any, and_stems: Any, not_stems: Any) -> set[int]:
        if mode == "near" and near:
            left, right, dist = near[0]
            hits = idx.query_near(left, right, dist)
        elif or_stems:
            hits = idx.query_or(or_stems)
        elif and_stems:
            hits = idx.query_and(and_stems)
        else:
            raise ToolExecutionError("No search terms after stop-word filtering")

        if not_stems:
            hits = idx.query_not(hits, not_stems)

        return hits

    def _build_result_entry(self, idx: _DocIndex, para_i: int, context_paragraphs: int, all_positive: Any, bookmark_map: Any) -> dict[str, Any]:
        ctx_lo = max(0, para_i - context_paragraphs)
        ctx_hi = min(idx.para_count, para_i + context_paragraphs + 1)
        context = [{"index": j, "text": idx.para_texts.get(j, "")} for j in range(ctx_lo, ctx_hi)]

        matched = [s for s in all_positive if para_i in idx.terms.get(s, set())]

        entry = {"paragraph_index": para_i, "text": idx.para_texts.get(para_i, ""), "matched_stems": matched, "context": context}

        nearest = self._bm_svc.find_nearest_heading_bookmark(para_i, bookmark_map)
        if nearest:
            entry["nearest_heading"] = nearest

        return entry

    # ── Public API ────────────────────────────────────────────────

    def search_boolean(self, doc: Any, query: str, max_results: int = 20, context_paragraphs: int = 1) -> dict[str, Any]:
        """Boolean full-text search with Snowball stemming."""
        if type(max_results) is not int or max_results < 1:
            raise ValueError("max_results must be a positive integer.")
        if type(context_paragraphs) is not int or context_paragraphs < 0:
            raise ValueError("context_paragraphs must be a non-negative integer.")

        idx, was_cached = self._get_index(doc)

        stemmer = self._get_stemmer(idx.language)
        stop_words = _normalized_stop_words(idx.language)
        parsed = self._parse_query(query, stemmer, stop_words)

        if parsed["error"] or not (parsed["and_stems"] or parsed["or_stems"] or parsed["near"]):
            error = parsed["error"] or "No search terms after stop-word filtering"
            dropped = sorted(set(parsed["dropped_stops"]))
            if dropped:
                error += " (stop words dropped: %s)" % ", ".join(dropped)
            raise ToolExecutionError(error)

        mode = parsed["mode"]
        and_stems = parsed["and_stems"]
        or_stems = parsed["or_stems"]
        not_stems = parsed["not_stems"]
        near = parsed["near"]

        all_positive = []
        for group in and_stems:
            all_positive.extend(group)
        all_positive.extend(or_stems)
        if near:
            for left, right, _unused in near:
                all_positive.extend(left + right)

        # Execute query
        hits = self._execute_query(idx, mode, near, or_stems, and_stems, not_stems)

        total = len(hits)
        selected = sorted(hits)[:max_results]

        bookmark_map = self._bm_svc.get_mcp_bookmark_map(doc)

        results = [self._build_result_entry(idx, para_i, context_paragraphs, all_positive, bookmark_map) for para_i in selected]

        resp = {
            "query": query,
            "mode": mode,
            "language": idx.language,
            "detected_language": idx.detected_language,
            "stem_fallback": idx.stem_fallback,
            "total_found": total,
            "returned": len(results),
            "matches": results,
            "index": _index_counts(idx, was_cached),
        }
        if near:
            resp["near"] = {"left": near[0][0], "right": near[0][1], "distance": near[0][2]}
        if parsed["dropped_stops"]:
            resp["dropped_stops"] = parsed["dropped_stops"]
        return resp

    def get_index_stats(self, doc: Any) -> dict[str, Any]:
        """Index statistics + top 20 most frequent stems."""
        idx, was_cached = self._get_index(doc)

        top = sorted(idx.terms.items(), key=lambda x: len(x[1]), reverse=True)[:_TOP_STEMS]

        stats: dict[str, Any] = {
            "language": idx.language,
            "detected_language": idx.detected_language,
            "stem_fallback": idx.stem_fallback,
        }
        stats.update(_index_counts(idx, was_cached))
        stats["top_stems"] = [{"stem": t, "paragraphs": len(s)} for t, s in top]
        return stats
