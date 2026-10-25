# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locale-aware policy for the native AI grammar proofreader.

Shipped BCP-47 tags and UNO ``Locale`` ↔ canonical tag bridging live in ``plugin.framework.grammar_rule_ids``.
This module holds **Unicode sentence-boundary** tables, scheduling thresholds,
Thai/Lao/Khmer whitespace chunking, hashing/fingerprinting, and the LLM worker caps/prompts.

Must not import sibling grammar-processing modules under ``plugin.writer.locale``
(leaf data modules such as ``locale_abbrev`` are fine). ``grammar_max_in_flight`` is the
one deliberate exception: it lazily imports ``grammar_worker`` because the worker imports
this module (a top-level import would cycle).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Sequence

from plugin.framework.grammar_rule_ids import bcp47_to_uno_lang_country

_log = logging.getLogger("writeragent.grammar")


# ---------------------------------------------------------------------------
# Centralized Grammar Checker Constants
# ---------------------------------------------------------------------------

# Persistence constants
# Schema version written into the document user-defined property.
# 4: per-locale good/bad maps plus the v3 checker ``model`` string.
# Loaders accept exactly 2, 3, and 4. v3 is locale-blind (adopted on first
# locale read). Any other version is rejected rather than parsed as v3.
GRAMMAR_CACHE_VERSION = 4
GRAMMAR_CACHE_MAX_CHARS = 900_000

# Name of the user-defined string property under which the document-embedded cache is stored inside the ODT file.
# Default: "WriterAgentGrammarCache"
GRAMMAR_DOC_CACHE_UDPROP = "WriterAgentGrammarCache"

# Cache sizing limits
# Maximum number of entries kept in the in-memory sentence LRU cache per session to prevent memory leaks.
# Default: 2048 sentences
MAX_CACHE_SIZE = 2048

# Maximum number of recent incomplete sentences scanned backward from the LRU tail for prefix compaction.
# Bounding this scan prevents O(N) overhead during active typing bursts.
# Default: 10 sentences
MAX_RECENT_INCOMPLETE_SCAN = 10

# Worker LLM limits & timeout thresholds
# Per-sentence ceiling applied when the LLM request / cache identity is built (checked_grammar_text);
# text past the cap is intentionally not checked.
# Rationale: Prevents sending massive chunks of text to the LLM which would inflate costs and latency.
# Default: 8192 characters (approx 1200-1600 words)
GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS = 8192

# Maximum output tokens requested from the LLM for grammar checking responses.
# Rationale: Ensures complete JSON response structures without truncation while capping cost per request.
# Default: 3072 tokens
GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS = 3072

# The hard upper limit for batching sentences from a single paragraph into one LLM request.
# Rationale: Avoids hitting LLM output token limits and prevents batch failures while optimizing throughput.
# Default: 8 sentences
GRAMMAR_BATCH_MAX_SENTENCES = 8

# Hard upper limit for simultaneous background grammar HTTP calls (doc.grammar_proofreader_max_in_flight).
GRAMMAR_MAX_IN_FLIGHT = 8


def _clamped_int_config(key: str, default: int, lo: int, hi: int, *, warn_name: str = "") -> int:
    """Read an int config key, falling back to default if <= 0 and clamping to [lo, hi]."""
    from plugin.framework import config

    n = config.get_config_int_safe(key)
    if n <= 0:
        return default
    if n < lo or n > hi:
        if warn_name:
            _log.warning("[grammar] %s %d out of range [%d, %d], clamping", warn_name, n, lo, hi)
        return max(lo, min(hi, n))
    return n


def grammar_max_in_flight(ctx: Any = None) -> int:
    """Clamp ``doc.grammar_proofreader_max_in_flight`` to [1, GRAMMAR_MAX_IN_FLIGHT].

    ``ctx`` unused; kept so call sites can pass the UNO context they already hold.
    Local providers (Harper, LanguageTool, Vale) always use 1 worker; parallel
    drain threads and HTTP slots apply only when the grammar provider is LLM.
    """
    # Lazy: grammar_worker imports this module at top level, so importing it here would cycle.
    from plugin.writer.locale.grammar_worker import get_grammar_provider

    del ctx
    n = _clamped_int_config("doc.grammar_proofreader_max_in_flight", default=1, lo=1, hi=GRAMMAR_MAX_IN_FLIGHT)
    if get_grammar_provider() != "llm":
        # Harper / LanguageTool / Vale: never parallel drain threads.
        if n > 1:
            _log.debug("[grammar] max_in_flight=%s ignored for local provider (always 1)", n)
        return 1
    return n


def grammar_batch_sentences(ctx: Any = None) -> int:
    """Clamp ``doc.grammar_proofreader_batch_sentences`` to [1, GRAMMAR_BATCH_MAX_SENTENCES].

    ``ctx`` unused; kept so call sites can pass the UNO context they already hold.
    Logs a warning and clamps if the value is out of bounds or malformed.
    """
    del ctx
    return _clamped_int_config(
        "doc.grammar_proofreader_batch_sentences",
        default=1,
        lo=1,
        hi=GRAMMAR_BATCH_MAX_SENTENCES,
        warn_name="batch_sentences",
    )


def grammar_max_tokens(ctx: Any = None) -> int:
    """Return the fixed max output tokens for an LLM grammar response.

    Not user-configurable (there is no config key for it).
    This is the per-request cap (used for both single-sentence and batch requests,
    not multiplied by batch size).
    ``ctx`` unused; kept so call sites can pass the UNO context they already hold.
    """
    del ctx
    return GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS


def grammar_max_chars(ctx: Any = None) -> int:
    """Return the fixed max sentence character limit for LLM grammar checks.

    Not user-configurable (there is no config key for it).
    For an LLM identity, characters past the cap are not checked; the cache identity
    is the checked prefix; a tail-only edit does not miss that prefix and does not raise
    a new squiggle. Old rows stored under the full-sentence fingerprint miss once and are
    rewritten under the prefix. No migration.
    ``ctx`` unused; kept so call sites can pass the UNO context they already hold.
    """
    del ctx
    return GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS


def checked_grammar_text(text: str, limit: int) -> str:
    """Return text capped at limit for LLM grammar checks.

    If len(text) <= limit, returns text. Otherwise cut at the last whitespace in the
    first three quarters of the window, and hard-cut at limit when there is none.
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = -1
    for i in range(limit - 1, -1, -1):
        if window[i].isspace():
            cut = i
            break
    if cut >= (limit * 3) // 4:
        return window[:cut]
    return window


# Maximum tokens requested for a single-sentence language detection LLM call.
# Default: 256 tokens
GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE = 256

# Token allocation target per item when batching language detection checks.
# Default: 150 tokens
GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_PER_BATCH_ITEM = 150

# The idle/quiet period wait time in seconds before sequential worker drains and executes partial sentences.
# Complete sentences do not wait out this pause; they coalesce for GRAMMAR_COMPLETE_COALESCE_S.
# Rationale: Aggregates rapid edits during active typing to avoid spamming the LLM on every single keystroke.
# Default: 1.0 second
GRAMMAR_WORKER_PAUSE_TIMEOUT_S = 1.0

# Fixed coalesce window for complete sentences before grouping and executing.
# Complete sentences do not debounce keystroke-by-keystroke like partials.
# Default: 0.05 seconds (50ms)
GRAMMAR_COMPLETE_COALESCE_S = 0.05

# Text scheduling thresholds
# Minimum number of non-space characters required to enqueue incomplete sentence fragments during typing.
# Rationale: Drops tiny partial word fragments to prevent checking garbage/incomplete drafts.
# Default: 15 non-space characters
GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS = 15

# Worker LLM system prompts
GRAMMAR_SYSTEM_PROMPT_TEMPLATE = (
    "You are a strict grammar and style checker. Reply with a single JSON object only, "
    'no markdown, shaped exactly as: {{"errors": [{{"wrong": "exact substring from the text", '
    '"correct": "replacement", "type": "grammar|style|spelling", "reason": "brief reason"}}]}}. '
    "Use an empty errors array if there are no issues. "
    "Provide errors in the order they appear in the text. "
    "The text to check is in {lang_name} (BCP-47: {bcp47}). Apply grammar, spelling, "
    "and style rules appropriate to that language; use the same language as the text in "
    '"reason" and any comments when you give them.'
)

GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE = (
    "You are a strict grammar and style checker. The user will provide multiple sentences. "
    "Reply with a single JSON object only, no markdown, shaped exactly as: "
    '{{"results": [{{"errors": [{{"wrong": "...", "correct": "...", "type": "...", "reason": "..."}}]}}]}}. '
    "The 'results' array must have exactly the same number of elements as the input sentences, in the same order. "
    "Use an empty errors array for sentences with no issues. "
    "The text is in {lang_name} (BCP-47: {bcp47}). Apply grammar, spelling, "
    "and style rules appropriate to that language; use the same language as the text in "
    '"reason" and any comments when you give them.'
)

LANGUAGE_DETECT_SYSTEM_PROMPT = """You are a language detection engine. The user will provide a text segment.
Return a JSON object with a single key 'detected_language_bcp47' containing the BCP-47 language tag for the text.
{detect_lang_instruction}
"""

LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT = """You are a language detection engine. The user will provide multiple numbered text segments.
Return a JSON object with a 'results' array. Each element in the array must be an object with a 'detected_language_bcp47' key containing the BCP-47 language tag for the corresponding segment.
The 'results' array must have exactly the same number of elements as the input segments, in the same order.
{detect_lang_instruction}
"""


def fingerprint_for_text(text: str) -> str:
    """Truncate to 24 hex characters (96 bits) for stable collision-resistant sentence caching."""
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]


def uno_locales_for_tags(tags: Sequence[str]) -> tuple[Any, ...]:
    """Locales returned by ``getLocales`` as ``com.sun.star.lang.Locale`` structs."""
    try:
        import uno
    except ImportError:
        return ()
    out: list[Any] = []
    for tag in tags:
        la, ctry = bcp47_to_uno_lang_country(tag)
        out.append(uno.createUnoStruct("com.sun.star.lang.Locale", Language=la, Country=ctry, Variant=""))
    return tuple(out)


# ---------------------------------------------------------------------------
# Unicode sentence terminals / trailing closers (STerm-style + Pe/Pf prose)
# ---------------------------------------------------------------------------

# Sentence-ending punctuation by script — single source for native grammar (proofreader
# gating, worker partial prompts, and cache LRU incomplete-prefix eviction).
# Common prose terminals (Unicode STerm-style) plus "." and the "…" prose ellipsis;
# deliberately not the full Unicode STerm list (rare-script marks are omitted).
# fmt: off
GRAMMAR_SENTENCE_TERMINATORS: frozenset[str] = frozenset((
    "!", ".", "?",              # ASCII
    "…",                        # Horizontal ellipsis
    "։",                        # Armenian full stop
    "؟", "۔",                   # Arabic question mark / full stop
    "܀", "܁", "܂",              # Syriac
    "߹",                        # NKo exclamation mark
    "।", "॥",                   # Devanagari danda / double danda
    "၊", "။",                   # Myanmar
    "።", "፧", "፨",              # Ethiopic
    "᙮",                        # Canadian syllabics full stop
    "᠃", "᠉",                   # Mongolian full stop / Manchu full stop
    "᥄", "᥅",                   # Limbu
    "᪨", "᪩", "᪪", "᪫",        # Tai Tham
    "᭚", "᭛", "᭞", "᭟", "᭽", "᭾",  # Balinese
    "᰻",                        # Lepcha
    "᱾", "᱿",                   # Ol Chiki
    "‼", "‽", "⁇", "⁈", "⁉",   # Double/combined punctuation
    "⳹", "⳺", "⳻", "⳾",         # Coptic
    "⸮", "⸼",                   # Reversed question mark / stenographic full stop
    "。",                        # Ideographic full stop
    "꓿",                        # Lisu
    "꘎", "꘏",                   # Vai
    "꛳", "꛷",                   # Bamum
    "︑", "︒", "︕", "︖", "︙",  # Presentation forms (vertical)
    "﹒", "﹖", "﹗",             # Small forms
    "！", "．", "？",             # Fullwidth
    "｡",                        # Halfwidth ideographic full stop
    "𑅃",                        # Chakma question mark
    "𖫵",                        # Bassa Vah full stop
    "𖺘", "𖺚",                  # Medefaidrin
    "𛲟",                        # Duployan
    "𝪈",                        # Signwriting full stop
    "𞥞", "𞥟",                  # Adlam
))

# Characters skipped when scanning backward for the sentence end: brackets, closing quotes,
# and similar trail the period.
# Every Unicode Pe/Pf character, enforced by test_trailing_closers_cover_unicode_pe_pf;
# `"` `'` `>` added for prose that omits curly quotes.

GRAMMAR_TRAILING_CLOSERS: frozenset[str] = frozenset((
    # ASCII Pe
    ")", "]", "}",
    # Pf: closing quotes (», ›, curly " ', and scholarly brackets)
    "»", "’", "”", "›", "⸃", "⸅", "⸊", "⸍", "⸝", "⸡",
    # CJK / fullwidth / halfwidth Pe
    "〉", "》", "」", "』", "】", "〕", "〗", "〙", "〛", "〞", "〟",
    "﴾", "︘", "︶", "︸", "︺", "︼", "︾", "﹀", "﹂", "﹄", "﹈",
    "﹚", "﹜", "﹞", "）", "］", "｝", "｠", "｣",
    # Latin / misc Pe (Tibetan, Ogham, sub/superscript, math, ornamental)
    "༻", "༽", "᚜",
    "⁆", "⁾", "₎", "⌉", "⌋",
    "〉", "❩", "❫", "❭", "❯", "❱", "❳", "❵",
    "⟆", "⟧", "⟩", "⟫", "⟭", "⟯",
    "⦄", "⦆", "⦈", "⦊", "⦌", "⦎", "⦐", "⦒", "⦔", "⦖", "⦘",
    "⧙", "⧛", "⧽",
    "⸣", "⸥", "⸧", "⸩",
    "⹖", "⹘", "⹚", "⹜",
    # ASCII informal closers (not Pe/Pf in Unicode but common in prose)
    '"', "'", ">",
))
# fmt: on


def last_meaningful_char(text: str) -> str:
    """Return the last non-closer character (skipping quotes, brackets, etc.)."""
    if not text:
        return ""
    for ch in reversed(text.rstrip()):
        if ch in GRAMMAR_TRAILING_CLOSERS:
            continue
        return ch
    return ""


def looks_complete_sentence(text: str) -> bool:
    """True if ``text`` ends (after skipping trailing closers) with a sentence terminal."""
    return last_meaningful_char(text) in GRAMMAR_SENTENCE_TERMINATORS


# ---------------------------------------------------------------------------
# Thai / Lao / Khmer: whitespace-delimited chunks (no reliable sentence BI here)
# ---------------------------------------------------------------------------

GRAMMAR_WHITESPACE_SENTENCE_LOCALE_PREFIXES: tuple[str, ...] = ("th", "lo", "km")

GRAMMAR_WHITESPACE_RUN_RE = re.compile(r"\s+")


def is_whitespace_sentence_locale(locale_key: str) -> bool:
    """True when the primary subtag of ``locale_key`` uses whitespace sentence boundaries."""
    return locale_key.split("-", 1)[0].lower() in GRAMMAR_WHITESPACE_SENTENCE_LOCALE_PREFIXES


def split_sentence_chunks_by_separator_regex(text: str, sep_re: re.Pattern[str]) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, chunk_text)`` pairs using regex separator matches.

    Used for Thai/Lao/Khmer (``\\s+``) splitting by the grammar splitter and the embeddings
    passage splitter. Each chunk includes its following separator span so Writer boundaries
    stay aligned. Empty or whitespace-only ``text`` returns ``[]``.
    """
    result: list[tuple[int, str]] = []
    last = 0
    for m in sep_re.finditer(text):
        seg = text[last : m.start()]
        ws = text[m.start() : m.end()]
        if seg:
            result.append((last, seg + ws))
        last = m.end()
    tail = text[last:]
    if tail:
        result.append((last, tail))
    return result

_sterm_chars = "".join(sorted(GRAMMAR_SENTENCE_TERMINATORS))
_sterm_escaped = re.escape(_sterm_chars)
_sterm_class = f"[{_sterm_escaped}]"

# Terminators used for cache key normalization (strips redundant trailing terminators).
GRAMMAR_CACHE_NORMALIZATION_RE = re.compile(rf"^(.*?{_sterm_class})({_sterm_class}*)$")


# ---------------------------------------------------------------------------
# Scheduling thresholds (partial sentence gating)
# ---------------------------------------------------------------------------


def count_nonspace_chars(text: str) -> int:
    return sum(1 for c in (text or "") if not c.isspace())


def filter_sentence_spans_for_thresholds(spans: Sequence[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop incomplete sentences shorter than ``GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS`` (conservative churn avoidance)."""
    out: list[tuple[int, int, str]] = []
    for s, e, txt in spans:
        if not looks_complete_sentence(txt) and count_nonspace_chars(txt) < GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS:
            continue
        out.append((s, e, txt))
    return out


