# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locale-aware policy for the native AI grammar proofreader (DAG root for grammar code).

Includes **shipped BCP-47 tags** (aligned with gettext ``locales/`` and ``LinguisticWriterAgentGrammar.xcu``),
**UNO ``Locale`` ↔ canonical tag bridging**, English display names for prompts, **Unicode sentence-boundary**
tables, scheduling thresholds, abbreviations, Thai/Lao/Khmer whitespace chunking, hashing/fingerprinting,
LLM worker caps/prompt, and JSON wire parsing — plus pure helpers for those tables.

Must not import sibling modules under ``plugin.writer.locale``.
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
# Shipped BCP-47 registry + UNO CharLocale bridging
# ---------------------------------------------------------------------------

# One canonical tag per folder under repo-root ``locales/`` (plus en-US, en-GB; English has no ``en`` dir).
# Order: English first, then alphabetical by BCP-47. Must match ``LinguisticWriterAgentGrammar.xcu``.
#
# * ``pt`` → ``pt-BR`` (user docs may set ``pt-PT``; ``normalize_uno_locale_to_bcp47`` maps any ``pt`` to ``pt-BR``)
GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = (
    "en-US",
    "en-GB",
    "bg-BG",
    "bn-IN",
    "ca-ES",
    "cs-CZ",
    "da-DK",
    "de-DE",
    "el-GR",
    "es-ES",
    "et-EE",
    "fi-FI",
    "fr-FR",
    "hi-IN",
    "hr-HR",
    "hu-HU",
    "id-ID",
    "it-IT",
    "ja-JP",
    "ko-KR",
    "lt-LT",
    "lv-LV",
    "nb-NO",
    "nl-NL",
    "nn-NO",
    "pl-PL",
    "pt-BR",
    "ro-RO",
    "ru-RU",
    "sk-SK",
    "sv-SE",
    "tr-TR",
    "uk-UA",
    "ur-PK",
    "zh-CN",
    "zh-TW",
)


def bcp47_to_uno_lang_country(bcp47: str) -> tuple[str, str]:
    """Return (Language, Country) for ``com.sun.star.lang.Locale`` (Variant empty for our tags)."""
    parts = bcp47.split("-")
    if not parts[0]:
        return "", ""
    lang = parts[0]
    if len(parts) < 2:
        return lang, ""
    return lang, parts[1]


# Exact (lowercase language, upper country) → tag
_EXACT_PAIR_TO_TAG: dict[tuple[str, str], str] = {}
for _t in GRAMMAR_REGISTRY_LOCALE_TAGS:
    _l, c = bcp47_to_uno_lang_country(_t)
    _EXACT_PAIR_TO_TAG[(_l.lower(), c.upper())] = _t
    if c == "":
        _EXACT_PAIR_TO_TAG[(_l.lower(), "")] = _t

# One canonical BCP-47 per ISO language when the document country does not match the registry
# (e.g. ``de-AT`` / ``es-MX``) — ``en`` / ``zh`` / ``pt`` / ``nb``/``nn``/``no`` are handled in code.
_CANON_BY_LANG_NO_REGION: dict[str, str] = {}
for _t in GRAMMAR_REGISTRY_LOCALE_TAGS:
    _l, c = bcp47_to_uno_lang_country(_t)
    key = _l.lower()
    if key in ("en", "zh", "pt", "nb", "nn", "no"):
        continue
    if key not in _CANON_BY_LANG_NO_REGION:
        _CANON_BY_LANG_NO_REGION[key] = _t


def normalize_uno_locale_to_bcp47(a_locale: Any) -> str | None:
    """Map a document ``Locale`` to our canonical BCP-47, or None if the language is not supported.

    Regional variants (e.g. ``de-AT``) map to the same canonical tag as ``de-DE`` for cache/LLM.
    """
    try:
        if a_locale is None:
            return None
        lang = (getattr(a_locale, "Language", None) or "").strip()
        if not lang:
            return None
        country = (getattr(a_locale, "Country", None) or "").strip().upper()
    except Exception:
        return None

    lang_l = lang.lower()
    key = (lang_l, country)

    if key in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[key]
    if (lang_l, "") in _EXACT_PAIR_TO_TAG:
        return _EXACT_PAIR_TO_TAG[(lang_l, "")]

    if lang_l == "en":
        if country in ("GB", "UK"):
            return "en-GB"
        return "en-US"

    if lang_l == "zh":
        if country in ("TW", "HK", "MO"):
            return "zh-TW"
        return "zh-CN"

    if lang_l in ("nb", "no"):
        return "nb-NO"
    if lang_l == "nn":
        return "nn-NO"

    if lang_l == "pt":
        return "pt-BR"

    c = _CANON_BY_LANG_NO_REGION.get(lang_l)
    if c is not None:
        return c
    return None


# English names for the LLM (short addendum; JSON schema part stays in English)
_GRAMMAR_BCP47_EN_NAME: dict[str, str] = {
    "en-US": "English (United States)",
    "en-GB": "English (United Kingdom)",
    "bg-BG": "Bulgarian",
    "bn-IN": "Bengali (India)",
    "ca-ES": "Catalan",
    "cs-CZ": "Czech",
    "da-DK": "Danish",
    "de-DE": "German",
    "el-GR": "Greek",
    "es-ES": "Spanish",
    "et-EE": "Estonian",
    "fi-FI": "Finnish",
    "fr-FR": "French",
    "hi-IN": "Hindi (India)",
    "hr-HR": "Croatian",
    "hu-HU": "Hungarian",
    "id-ID": "Indonesian",
    "it-IT": "Italian",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
    "lt-LT": "Lithuanian",
    "lv-LV": "Latvian",
    "nb-NO": "Norwegian Bokmål",
    "nl-NL": "Dutch",
    "nn-NO": "Norwegian Nynorsk",
    "pl-PL": "Polish",
    "pt-BR": "Portuguese (Brazil)",
    "ro-RO": "Romanian",
    "ru-RU": "Russian",
    "sk-SK": "Slovak",
    "sv-SE": "Swedish",
    "tr-TR": "Turkish",
    "uk-UA": "Ukrainian",
    "ur-PK": "Urdu (Pakistan)",
    "zh-CN": "Chinese (Simplified, China)",
    "zh-TW": "Chinese (Traditional, Taiwan)",
}


def grammar_english_name_for_bcp47(bcp47: str) -> str:
    return _GRAMMAR_BCP47_EN_NAME.get(bcp47, bcp47)


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

_sterm_chars = "".join(sorted(GRAMMAR_SENTENCE_TERMINATORS))
_sterm_escaped = _sterm_chars.replace("\\", "\\\\").replace("]", "\\]").replace("-", "\\-").replace("^", "\\^")
_sterm_class = f"[{_sterm_escaped}]"

# Terminators used for cache key normalization (strips redundant trailing terminators).
GRAMMAR_CACHE_NORMALIZATION_RE = re.compile(rf"^(.*?{_sterm_class})({_sterm_class}*)$")


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
