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

import logging
import re
import sys
from plugin.framework.utils import get_plugin_dir
import os
import time
import unicodedata

from plugin.framework.errors import ToolExecutionError
from plugin.framework.service_base import ServiceBase
from plugin.modules.writer.base import ToolWriterIndexBase

log = logging.getLogger("writeragent.writer.index")

# ── Language mapping (ISO 639-1 -> snowballstemmer algorithm) ─────────

_ISO_TO_SNOWBALL = {
    "ar": "arabic",    "hy": "armenian",  "eu": "basque",
    "ca": "catalan",   "da": "danish",    "nl": "dutch",
    "en": "english",   "eo": "esperanto", "et": "estonian",
    "fi": "finnish",   "fr": "french",    "de": "german",
    "el": "greek",     "hi": "hindi",     "hu": "hungarian",
    "id": "indonesian", "ga": "irish",    "it": "italian",
    "lt": "lithuanian", "ne": "nepali",   "no": "norwegian",
    "nb": "norwegian", "nn": "norwegian",
    "pt": "portuguese", "ro": "romanian", "ru": "russian",
    "sr": "serbian",   "es": "spanish",   "sv": "swedish",
    "ta": "tamil",     "tr": "turkish",   "yi": "yiddish",
}

# ── Stop words per language ───────────────────────────────────────────

_STOP_WORDS = {
    "french": frozenset({
        "au", "aux", "avec", "ce", "ces", "cette", "dans", "de",
        "des", "du", "elle", "en", "est", "et", "il", "ils", "je",
        "la", "le", "les", "leur", "leurs", "lui", "ma", "mais",
        "me", "mes", "mon", "ne", "ni", "nos", "notre", "nous",
        "on", "ou", "par", "pas", "pour", "qu", "que", "qui", "sa",
        "se", "ses", "si", "son", "sur", "ta", "te", "tes", "ton",
        "tu", "un", "une", "vos", "votre", "vous",
    }),
    "english": frozenset({
        "a", "an", "and", "are", "as", "at", "be", "but", "by",
        "for", "from", "had", "has", "he", "her", "his", "if", "in",
        "is", "it", "its", "my", "no", "not", "of", "on", "or",
        "our", "she", "so", "the", "to", "up", "us", "was", "we",
    }),
    "german": frozenset({
        "aber", "als", "am", "an", "auch", "auf", "aus", "bei",
        "bin", "bis", "da", "das", "dem", "den", "der", "des",
        "die", "du", "ein", "er", "es", "fur", "hat", "ich", "ihr",
        "im", "in", "ist", "ja", "mir", "mit", "nach", "nicht",
        "noch", "nun", "nur", "ob", "oder", "sie", "so", "und",
        "uns", "vom", "von", "vor", "was", "wir", "zu", "zum", "zur",
    }),
    "spanish": frozenset({
        "a", "al", "con", "de", "del", "el", "en", "es", "la",
        "las", "lo", "los", "no", "por", "que", "se", "su", "un",
        "una", "y",
    }),
    "italian": frozenset({
        "a", "al", "che", "con", "da", "del", "di", "e", "il",
        "in", "la", "le", "lo", "non", "per", "si", "su", "un", "una",
    }),
    "portuguese": frozenset({
        "a", "ao", "com", "da", "de", "do", "e", "em", "na", "no",
        "o", "os", "por", "que", "se", "um", "uma",
    }),
}

_STOP_WORDS_FALLBACK = frozenset()

# ── Tokenisation ──────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r'[^\w\s]', re.UNICODE)
_MIN_TOKEN_LEN = 2


def _deaccent(text):
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if unicodedata.category(c) != 'Mn')


def _raw_tokens(text):
    cleaned = _PUNCT_RE.sub(' ', _deaccent(text.lower()))
    return [t for t in cleaned.split() if len(t) >= _MIN_TOKEN_LEN]


# ── Per-document index ────────────────────────────────────────────────

class _DocIndex:
    __slots__ = ('terms', 'para_texts', 'para_count',
                 'build_ms', 'language')

    def __init__(self):
        self.terms = {}        # stem -> set[int]
        self.para_texts = {}   # int -> str
        self.para_count = 0
        self.build_ms = 0.0
        self.language = "english"

    def query_and(self, stem_groups):
        if not stem_groups:
            return set()
        sets = []
        for group in stem_groups:
            s = set()
            for stem in group:
                ps = self.terms.get(stem)
                if ps:
                    s |= ps
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

    def query_or(self, stems):
        result = set()
        for stem in stems:
            ps = self.terms.get(stem)
            if ps:
                result |= ps
        return result

    def query_not(self, include, exclude_stems):
        result = include.copy()
        for stem in exclude_stems:
            ps = self.terms.get(stem)
            if ps:
                result -= ps
        return result

    def query_near(self, stems_a, stems_b, distance):
        set_a = set()
        for s in stems_a:
            ps = self.terms.get(s)
            if ps:
                set_a |= ps
        set_b = set()
        for s in stems_b:
            ps = self.terms.get(s)
            if ps:
                set_b |= ps
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


# ── Service ───────────────────────────────────────────────────────────

class IndexService(ServiceBase):
    """Per-document inverted index with Snowball stemming."""

    name = "writer_index"

    def __init__(self, services):
        self._doc_svc = services.document
        self._tree_svc = services.writer_tree
        self._bm_svc = services.writer_bookmarks
        events = services.events
        self._cache = {}       # doc_key -> _DocIndex
        self._stemmers = {}    # lang -> StemmerInstance
        events.subscribe("document:cache_invalidated",
                         self._on_cache_invalidated)

    def _on_cache_invalidated(self, doc=None, **_kw):
        if doc is None:
            self._cache.clear()
        else:
            self._cache.pop(self._doc_svc.doc_key(doc), None)

    # ── Stemmer management ────────────────────────────────────────

    def _get_stemmer(self, lang):
        cached = self._stemmers.get(lang)
        if cached is not None:
            return cached
        try:
            # Add bundled snowballstemmer to path if needed
            lib_dir = os.path.join(get_plugin_dir(), "..", "..", "lib")
            lib_dir = os.path.normpath(lib_dir)
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            import snowballstemmer # type: ignore
            s = snowballstemmer.stemmer(lang)
            self._stemmers[lang] = s
            return s
        except (ImportError, KeyError):
            log.warning("No stemmer for '%s', falling back to english", lang)
            if lang != "english":
                return self._get_stemmer("english")
            return None

    def _detect_language(self, doc):
        try:
            text = doc.getText()
            enum = text.createEnumeration()
            if enum.hasMoreElements():
                first_para = enum.nextElement()
                locale = first_para.getPropertyValue("CharLocale")
                iso = locale.Language
                lang = _ISO_TO_SNOWBALL.get(iso)
                if lang:
                    return lang
        except Exception as e:
            log.debug("Language detection failed: %s", e)
        return "english"

    def _stem(self, stemmer, tokens, stop_words):
        return [stemmer.stemWord(t) for t in tokens if t not in stop_words]

    # ── Index build ───────────────────────────────────────────────

    def _get_index(self, doc):
        """Get or build the inverted index. Returns (index, was_cached)."""
        key = self._doc_svc.doc_key(doc)
        cached = self._cache.get(key)
        if cached is not None:
            return cached, True

        t0 = time.perf_counter()
        lang = self._detect_language(doc)
        stemmer = self._get_stemmer(lang)
        stop_words = _STOP_WORDS.get(lang, _STOP_WORDS_FALLBACK)

        idx = _DocIndex()
        idx.language = lang
        text_obj = doc.getText()
        enum = text_obj.createEnumeration()
        para_i = 0

        while enum.hasMoreElements():
            el = enum.nextElement()
            if el.supportsService("com.sun.star.text.Paragraph"):
                text = el.getString()
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
        self._cache[key] = idx
        log.info("Index built [%s]: %d paras, %d stems, %.1fms",
                 lang, para_i, len(idx.terms), idx.build_ms)
        return idx, False

    # ── Query parsing ─────────────────────────────────────────────

    def _stem_query_tokens(self, text, stemmer, stop_words):
        raw = _raw_tokens(text)
        stems = []
        dropped = []
        for t in raw:
            if t in stop_words:
                dropped.append(t)
            else:
                stems.append(stemmer.stemWord(t) if stemmer else t)
        return stems, dropped

    def _parse_query(self, query, stemmer, stop_words):
        result = {
            "and_stems": [], "or_stems": [], "not_stems": [],
            "near": [], "dropped_stops": [], "mode": "and",
            "error": None,
        }

        not_split = re.split(r'\bNOT\b', query, flags=re.IGNORECASE)
        main_part = not_split[0].strip()
        for part in not_split[1:]:
            stems, dropped = self._stem_query_tokens(part, stemmer, stop_words)
            result["not_stems"].extend(stems)
            result["dropped_stops"].extend(dropped)

        # NEAR/N
        near_match = re.search(
            r'(.+?)\s+NEAR/(\d+)\s+(.+)', main_part, re.IGNORECASE)
        if near_match:
            left, dropped_l = self._stem_query_tokens(
                near_match.group(1), stemmer, stop_words)
            dist = int(near_match.group(2))
            right, dropped_r = self._stem_query_tokens(
                near_match.group(3), stemmer, stop_words)
            result["dropped_stops"].extend(dropped_l + dropped_r)
            if left and right:
                result["near"].append((left, right, dist))
                result["mode"] = "near"
            elif not left and not right:
                result["error"] = "NEAR terms are all stop words"
            return result

        has_and = bool(re.search(r'\bAND\b', main_part, re.IGNORECASE))
        has_or = bool(re.search(r'\bOR\b', main_part, re.IGNORECASE))
        if has_and and has_or:
            result["error"] = "Mixed AND/OR not supported. Use one operator per query."
            return result

        if has_or:
            chunks = re.split(r'\bOR\b', main_part, flags=re.IGNORECASE)
            for chunk in chunks:
                stems, dropped = self._stem_query_tokens(chunk, stemmer, stop_words)
                result["or_stems"].extend(stems)
                result["dropped_stops"].extend(dropped)
            result["mode"] = "or"
        else:
            if has_and:
                chunks = re.split(r'\bAND\b', main_part, flags=re.IGNORECASE)
            else:
                chunks = [main_part]
            for chunk in chunks:
                stems, dropped = self._stem_query_tokens(chunk, stemmer, stop_words)
                for stem in stems:
                    result["and_stems"].append([stem])
                result["dropped_stops"].extend(dropped)
            result["mode"] = "and"

        return result

    # ── Public API ────────────────────────────────────────────────

    def search_boolean(self, doc, query, max_results=20,
                       context_paragraphs=1):
        """Boolean full-text search with Snowball stemming."""
        idx, was_cached = self._get_index(doc)

        stemmer = self._get_stemmer(idx.language)
        stop_words = _STOP_WORDS.get(idx.language, _STOP_WORDS_FALLBACK)
        parsed = self._parse_query(query, stemmer, stop_words)

        if parsed["error"]:
            raise ToolExecutionError(parsed["error"])

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
            for left, right, _ in near:
                all_positive.extend(left + right)

        # Execute query
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

        total = len(hits)
        selected = sorted(hits)[:max_results]

        bookmark_map = self._bm_svc.get_mcp_bookmark_map(doc)

        results = []
        for para_i in selected:
            ctx_lo = max(0, para_i - context_paragraphs)
            ctx_hi = min(idx.para_count, para_i + context_paragraphs + 1)
            context = [
                {"index": j, "text": idx.para_texts.get(j, "")}
                for j in range(ctx_lo, ctx_hi)
            ]

            matched = [s for s in all_positive
                       if para_i in idx.terms.get(s, set())]

            entry = {
                "paragraph_index": para_i,
                "text": idx.para_texts.get(para_i, ""),
                "matched_stems": matched,
                "context": context,
            }

            nearest = self._bm_svc.find_nearest_heading_bookmark(
                para_i, bookmark_map)
            if nearest:
                entry["nearest_heading"] = nearest
            results.append(entry)

        resp = {
            "query": query,
            "mode": mode,
            "language": idx.language,
            "total_found": total,
            "returned": len(results),
            "matches": results,
            "index": {
                "paragraphs": idx.para_count,
                "unique_stems": len(idx.terms),
                "build_ms": idx.build_ms,
                "cached": was_cached,
            },
        }
        if near:
            resp["near"] = {"left": near[0][0], "right": near[0][1],
                            "distance": near[0][2]}
        if parsed["dropped_stops"]:
            resp["dropped_stops"] = parsed["dropped_stops"]
        return resp

    def get_index_stats(self, doc):
        """Index statistics + top 20 most frequent stems."""
        idx, was_cached = self._get_index(doc)

        top = sorted(idx.terms.items(),
                     key=lambda x: len(x[1]),
                     reverse=True)[:20]

        return {
            "language": idx.language,
            "paragraphs": idx.para_count,
            "unique_stems": len(idx.terms),
            "build_ms": idx.build_ms,
            "cached": was_cached,
            "top_stems": [{"stem": t, "paragraphs": len(s)}
                          for t, s in top],
        }
