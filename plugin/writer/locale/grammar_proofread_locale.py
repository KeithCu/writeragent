# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locale-aware text policy for the native AI grammar proofreader.

Distinct from ``grammar_locale_registry`` (supported BCP-47 tags and UNO ``Locale`` bridging):
this module holds Unicode sentence-boundary tables, scheduling thresholds, abbreviations,
Thai/Lao/Khmer whitespace chunking, hashing/fingerprinting, LLM worker caps/prompt, and
JSON wire parsing — plus pure helpers that exist only to interpret those tables.

Must not import sibling modules under ``plugin.writer.locale`` (DAG root for grammar code).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Mapping, cast

import json_repair

from plugin.framework.json_utils import safe_json_loads

_log = logging.getLogger("writeragent.grammar")

# ---------------------------------------------------------------------------
# Worker LLM policy (fixed caps, prompt template, drain debounce)
# ---------------------------------------------------------------------------

GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS = 8192
GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS = 512

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

GRAMMAR_WORKER_PAUSE_TIMEOUT_S = 1.0

# ---------------------------------------------------------------------------
# Unicode sentence terminals / trailing closers (STerm-style + Pe/Pf prose)
# ---------------------------------------------------------------------------

# Sentence-ending punctuation by script — single source for native grammar (proofreader
# gating, worker partial prompts, and cache LRU incomplete-prefix eviction).
# Matches Unicode 15.1 Sentence_Terminal (STerm); PropList.txt in Unicode UCD releases.
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
# Mostly Unicode closing punctuation (Pe/Pf); `"` `'` `>` added for prose that omits curly quotes.
# Regenerate Pe/Pf subset after a Unicode update:
#   import sys, unicodedata
#   chars = sorted(chr(cp) for cp in range(sys.maxunicode + 1)
#                  if unicodedata.category(chr(cp)) in ('Pe', 'Pf'))
#   print(frozenset(chars) | frozenset('"\'>'))

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
    "❩", "❫", "❭", "❯", "❱", "❳", "❵",
    "⟆", "⟧", "⟩", "⟫", "⟭", "⟯",
    "⦄", "⦆", "⦈", "⦊", "⦌", "⦎", "⦐", "⦒", "⦔", "⦖", "⦘",
    "⧙", "⧛", "⧽",
    "⸣", "⸥", "⸧", "⸩",
    "⹖", "⹘", "⹚", "⹜",
    # ASCII informal closers (not Pe/Pf in Unicode but common in prose)
    '"', "'", ">",
))
# fmt: on


def fingerprint_for_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


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
# Abbreviations before "." (BreakIterator sentence extension)
# ---------------------------------------------------------------------------

GRAMMAR_ABBREV_DOT_WORDS: frozenset[str] = frozenset(
    {
        "approx",
        "assoc",
        "dept",
        "prof",
        "univ",
        "ext",
        "fig",
        "vol",
        "misc",
        "vs",
        "etc",
        "mr",
        "mrs",
        "dr",
        "ms",
    }
)


def word_before_period_is_abbrev(word: str) -> bool:
    if not word:
        return False
    if word.lower() in GRAMMAR_ABBREV_DOT_WORDS:
        return True
    return 0 < len(word) <= 3 and word[0].isupper()


# ---------------------------------------------------------------------------
# Thai / Lao / Khmer: whitespace-delimited chunks (no reliable sentence BI here)
# ---------------------------------------------------------------------------

GRAMMAR_WHITESPACE_SENTENCE_LOCALE_PREFIXES: tuple[str, ...] = ("th", "lo", "km")

GRAMMAR_WHITESPACE_RUN_RE = re.compile(r"\s+")


def is_whitespace_sentence_locale(locale_key: str) -> bool:
    """True when ``locale_key`` uses whitespace runs as approximate sentence boundaries."""
    return locale_key.startswith(GRAMMAR_WHITESPACE_SENTENCE_LOCALE_PREFIXES)


def split_sentence_chunks_by_separator_regex(text: str, sep_re: re.Pattern[str]) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, chunk_text)`` pairs using regex separator matches.

    Shared by Thai/Lao/Khmer (``\\s+``) and the no-BreakIterator fallback (sentence-terminal
    lookahead split). Each chunk includes its following separator span so Writer boundaries
    stay aligned with the previous implementation.
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
    return result or [(0, text)]


# Primary clause boundary when BreakIterator is unavailable (subset of terminals).
GRAMMAR_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…؟。！？।])\s+")

# ---------------------------------------------------------------------------
# Scheduling thresholds (partial sentence gating)
# ---------------------------------------------------------------------------

GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS = 15

GRAMMAR_NONSPACE_SCHEDULE_RE = re.compile(r"\S", re.UNICODE)


def count_nonspace_chars(text: str) -> int:
    return len(GRAMMAR_NONSPACE_SCHEDULE_RE.findall(text or ""))


# ---------------------------------------------------------------------------
# LLM JSON wire format
# ---------------------------------------------------------------------------

GRAMMAR_JSON_TAIL_RE = re.compile(r"\{[\s\S]*\}\s*$")


def parse_grammar_json(content: str) -> list[dict[str, Any]]:
    """Parse assistant message into a list of error dicts (wrong, correct, type, reason)."""
    if not content or not content.strip():
        return []
    text = content.strip()
    m = GRAMMAR_JSON_TAIL_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if not isinstance(data, Mapping):
        try:
            _log.info("[grammar] parse_grammar_json: attempting json_repair")
            data = json_repair.repair_json(text, return_objects=True)
        except Exception as e:
            _log.warning("[grammar] parse_grammar_json: json_repair failed: %s", e)
            return []
    if not isinstance(data, Mapping):
        return []
    root = cast("Mapping[str, Any]", data)
    raw = root.get("errors")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        row = cast("Mapping[str, Any]", item)
        wrong = row.get("wrong")
        correct = row.get("correct")
        if wrong is None or correct is None:
            continue
        out.append({"wrong": str(wrong), "correct": str(correct), "type": str(row.get("type", "grammar")), "reason": str(row.get("reason", ""))})
    return out
