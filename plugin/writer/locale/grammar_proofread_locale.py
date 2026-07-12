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
from dataclasses import dataclass
from typing import Any, Literal

_log = logging.getLogger("writeragent.grammar")


# ---------------------------------------------------------------------------
# Centralized Grammar Checker Constants
# ---------------------------------------------------------------------------

# Persistence constants
# Schema version for user-defined properties storage (embedded in document metadata).
# Current value: 2 (uses hash keys and split clean/dirty format for 66%+ footprint reduction).
GRAMMAR_CACHE_VERSION = 2

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
# The ceiling for pathological run-on or extremely long sentences before enqueuing.
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


def grammar_max_in_flight(ctx: Any) -> int:
    """Clamp ``doc.grammar_proofreader_max_in_flight`` to [1, GRAMMAR_MAX_IN_FLIGHT].

    Local providers (Harper, LanguageTool, Vale) always use 1 worker; parallel
    drain threads and HTTP slots apply only when the grammar provider is LLM.
    """
    from plugin.framework import config

    n = config.get_config_int_safe("doc.grammar_proofreader_max_in_flight")
    n = max(1, min(GRAMMAR_MAX_IN_FLIGHT, n))
    if config.get_grammar_provider() != "llm":
        return 1
    return n


# Maximum tokens requested for a single-sentence language detection LLM call.
# Default: 256 tokens
GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE = 256

# Token allocation target per item when batching language detection checks.
# Default: 150 tokens
GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_PER_BATCH_ITEM = 150

# The idle/quiet period wait time in seconds before sequential worker drains and executes the queue.
# Rationale: Aggregates rapid edits during active typing to avoid spamming the LLM on every single keystroke.
# Default: 1.0 second
GRAMMAR_WORKER_PAUSE_TIMEOUT_S = 1.0

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


def grammar_inflight_key(a_document_identifier: str, loc_key: str, sent_text: str, is_complete: bool) -> str:
    """Queue supersede key: distinct per sentence when complete; stable per document when incomplete.

    - Complete sentences use their text hash to avoid collisions across paragraphs.
    - Incomplete sentences use a fixed 'INCOMPLETE' key to prevent typing floods.
    """
    if is_complete:
        context = fingerprint_for_text(sent_text)
    else:
        context = "INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    return f"{a_document_identifier}|{loc_key}|{context}"


def normalize_reason(reason: str) -> str:
    """Canonical/generalized representation of an error reason to group similar rules.

    - Converts to lowercase.
    - Strips punctuation and non-alphanumeric characters (keeping all word contents).
    - Collapses spaces into a single-space separated canonical string.
    """
    if not reason:
        return ""
    s = reason.lower().strip()
    # Strip non-alphanumeric characters (keeping word contents)
    s = GRAMMAR_NORMALIZE_REASON_RE.sub("", s)
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Shipped BCP-47 registry + UNO CharLocale bridging
# ---------------------------------------------------------------------------

# One canonical tag per folder under repo-root ``locales/`` (plus en-US, en-GB; English has no ``en`` dir).
# Order: English first, then alphabetical by BCP-47. Must match ``LinguisticWriterAgentGrammar.xcu``.
#
# * ``pt`` → ``pt-BR`` (user docs may set ``pt-PT``; ``normalize_uno_locale_to_bcp47`` maps any ``pt`` to ``pt-BR``)
#
# English names for the LLM (short addendum; JSON schema part stays in English)
GRAMMAR_REGISTRY_LOCALES: dict[str, str] = {
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

GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = tuple(GRAMMAR_REGISTRY_LOCALES.keys())

GrammarDetectLanguageMode = Literal["off", "llm", "langdetect"]


def bcp47_to_icu_sentence_breaker_locale(bcp47: str) -> str:
    """Map a canonical BCP-47 tag to an ``icu4py`` ``SentenceBreaker`` locale (``@ss=standard``)."""
    tag = normalize_detected_bcp47(bcp47) or str(bcp47 or "").strip()
    if not tag:
        return "en@ss=standard"
    parts = tag.split("-")
    lang = parts[0].lower()
    if not lang:
        return "en@ss=standard"
    if lang == "en":
        return "en@ss=standard"
    country = parts[1].upper() if len(parts) > 1 else ""
    if country:
        return f"{lang}_{country}@ss=standard"
    return f"{lang}@ss=standard"


def bcp47_to_langdetect_profile(bcp47: str) -> str:
    """Map a grammar-registry BCP-47 tag to a langdetect ``profiles/`` folder name."""
    parts = bcp47.lower().split("-")
    lang = parts[0]
    if lang == "zh" and len(parts) > 1:
        return f"zh-{parts[1]}"
    if lang in ("nb", "nn"):
        return "no"
    return lang


def langdetect_profiles_for_grammar_registry() -> frozenset[str]:
    """Unique langdetect profile files required for shipped grammar locales."""
    return frozenset(bcp47_to_langdetect_profile(t) for t in GRAMMAR_REGISTRY_LOCALE_TAGS)


def get_grammar_detect_language_mode(ctx: Any) -> GrammarDetectLanguageMode:
    """Read ``doc.grammar_proofreader_detect_language`` (off / llm / langdetect; legacy bool → llm/off)."""
    from plugin.framework.config import get_config

    raw = get_config("doc.grammar_proofreader_detect_language")
    if isinstance(raw, bool):
        return "llm" if raw else "off"
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in ("off", "llm", "langdetect"):
            return v  # type: ignore[return-value]
        if v in ("1", "true", "yes", "on"):
            return "llm"
    return "off"


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


@dataclass(frozen=True)
class _LocaleTagShim:
    """Minimal Locale-like object for ``normalize_uno_locale_to_bcp47`` on LLM tags."""

    Language: str
    Country: str = ""


def normalize_detected_bcp47(tag: str | None) -> str | None:
    """Map an LLM-detected BCP-47 tag onto a supported registry tag (same rules as UNO locales)."""
    if not tag or not str(tag).strip():
        return None
    raw = str(tag).strip().replace("_", "-")
    if raw in GRAMMAR_REGISTRY_LOCALES:
        return raw
    parts = raw.split("-")
    lang = parts[0]
    if not lang:
        return None
    country = parts[1].upper() if len(parts) > 1 else ""
    return normalize_uno_locale_to_bcp47(_LocaleTagShim(lang, country))


def grammar_bcp47_tags_match(a: str | None, b: str | None) -> bool:
    """True when two tags denote the same supported grammar locale (e.g. ``ja`` vs ``ja-JP``)."""
    if not a or not b:
        return False
    na = normalize_detected_bcp47(a) or a
    nb = normalize_detected_bcp47(b) or b
    return na == nb


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


def grammar_english_name_for_bcp47(bcp47: str) -> str:
    return GRAMMAR_REGISTRY_LOCALES.get(bcp47, bcp47)


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

_COMMON_ABBREVIATIONS: frozenset[str] = frozenset({
    # English
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "co", "corp", "inc", "ltd", "etc", "eg", "ie", "vs", "ca", "al", "st", "ave", "rd", "vol", "ed", "pp", "ch", "fig", "no", "approx", "misc", "temp", "am", "pm",
    # German
    "geb", "vorm", "hr", "beisp", "bzw", "usw", "evtl", "kath", "u.a", "v.a", "z.b", "d.h", "s.o", "s.u",
    # French
    "mme", "mlle", "mlles", "pr", "c-à-d", "c.-à-d", "ex", "sup", "inf", "p",
    # Spanish
    "sra", "srta", "dra", "dña", "s.a", "s.l", "cent", "cént", "ej", "pág", "págs", "cía", "vdo", "vda", "ud", "uds",
    # Italian
    "sig", "dott", "ecc", "pag", "pagg", "cap", "succ", "s.r.l",
    # Portuguese
    "profa", "pag", "pags",
    # Dutch
    "dhr", "mw", "enz",
    # Russian
    "ул", "ст", "им", "т.е", "т.к", "и.о", "и.д", "и.т.д", "и.т.п", "кв", "корп", "д", "г", "руб", "коп", "тыс", "млн", "млрд", "доп", "см", "табл", "рис", "стр", "вып", "сер", "изд"
})


def word_before_period_is_abbrev(word: str) -> int:
    """Returns >0 if word is an abbreviation or number (not a sentence terminator), else 0.
    
    Checks pure numbers, single-letter initials, internal periods, a multilingual whitelist,
    and consonant-only words across Latin, Cyrillic, and Greek alphabets.
    """
    if not word:
        return 0

    # 1. Pure numbers (e.g. "123", "1.23")
    if all(ch.isdigit() or ch in ".,-" for ch in word) and any(ch.isdigit() for ch in word):
        return 1

    # Normalize: strip trailing periods and lowercase
    w_norm = word.lower().rstrip(".")

    # 2. Single-letter initials (e.g. "A.", "B.", "г.")
    if len(w_norm) == 1 and w_norm.isalpha():
        return len(word)

    # 3. Multilingual whitelist check
    if w_norm in _COMMON_ABBREVIATIONS:
        return len(word)

    # 4. Words with internal periods (e.g. "U.S.A.", "d.h.", "т.е.")
    if "." in w_norm:
        alpha_count = sum(1 for ch in w_norm if ch.isalpha())
        if alpha_count >= 2:
            return len(word)

    # 5. Consonant-only check (no vowels in Latin, Cyrillic, or Greek)
    vowels = set("aeiouyаеёиоуыэюяαεηιουω")
    alpha_chars = [ch for ch in w_norm if ch.isalpha()]
    if alpha_chars and not any(ch in vowels for ch in alpha_chars):
        return len(word)

    _log.debug("[grammar] obs word_before_period_is_abbrev REJECT word=%r", word)
    return 0


# ---------------------------------------------------------------------------
# Thai / Lao / Khmer: whitespace-delimited chunks (no reliable sentence BI here)
# ---------------------------------------------------------------------------

GRAMMAR_WHITESPACE_SENTENCE_LOCALE_PREFIXES: tuple[str, ...] = ("th", "lo", "km")

GRAMMAR_WHITESPACE_RUN_RE = re.compile(r"\s+")
GRAMMAR_NORMALIZE_REASON_RE = re.compile(r"[^a-z0-9\s]")


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
_sterm_escaped = re.escape(_sterm_chars)
_sterm_class = f"[{_sterm_escaped}]"

# Terminators used for cache key normalization (strips redundant trailing terminators).
GRAMMAR_CACHE_NORMALIZATION_RE = re.compile(rf"^(.*?{_sterm_class})({_sterm_class}*)$")


# ---------------------------------------------------------------------------
# Scheduling thresholds (partial sentence gating)
# ---------------------------------------------------------------------------

GRAMMAR_NONSPACE_SCHEDULE_RE = re.compile(r"\S", re.UNICODE)


def count_nonspace_chars(text: str) -> int:
    return len(GRAMMAR_NONSPACE_SCHEDULE_RE.findall(text or ""))

