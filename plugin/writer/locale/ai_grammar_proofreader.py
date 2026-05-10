# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Linguistic2 grammar checker (Lightproof-style): XProofreader backed by LLM + cache."""

from __future__ import annotations

import importlib
import logging
import os
import queue
import re
import sys

# LO loads this file as a standalone UNO component; set up path like panel_factory.py
# so ``import plugin...`` works (file is plugin/writer/locale/ai_grammar_proofreader.py).
_this_file = os.path.abspath(__file__)
for _i in range(4):
    _this_file = os.path.dirname(_this_file)
_ext_root = _this_file
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
_lib_dir = os.path.join(_ext_root, "plugin", "lib")
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)
import threading
import time
from dataclasses import asdict
from typing import Any, Sequence, cast

import unohelper

from com.sun.star.lang import XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XProofreader, XSupportedLocales

log = logging.getLogger("writeragent.grammar")
# Do not inherit writeragent's log level (often WARN); grammar uses INFO for diagnostics.
log.setLevel(logging.DEBUG)

IMPLEMENTATION_NAME = "org.extension.writeragent.comp.pyuno.AiGrammarProofreader"
SERVICE_NAME = "com.sun.star.linguistic2.Proofreader"

# Fixed caps (not user-configurable): JSON response budget and pathological slice ceiling.
# Normal proofread uses sentence boundaries only; this limits unterminated run-on text in the worker.
GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS = 8192
GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS = 512
GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS = 15

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

# The time (in seconds) to wait without receiving any new grammar requests
# before processing the current batch. This 1-second pause ensures we
# don't start grammar checking while the user is actively typing, thereby
# reducing unnecessary LLM calls and backend stampedes.
GRAMMAR_WORKER_PAUSE_TIMEOUT_S = 1.0

# Sentence-ending punctuation by script (period, question mark, ideographic stop, …).
# Used to decide whether a proofread slice is complete enough to run or counts as a
# short skip / partial clause for the prompt. Sentence splitting uses BreakIterator elsewhere.
# Matches Unicode 15.1 Sentence_Terminal (STerm); PropList.txt in Unicode UCD releases.
# fmt: off
_SENTENCE_TERMINATORS = frozenset((
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
# and similar trail the period
# Mostly Unicode closing punctuation (Pe/Pf); `"` `'` `>` added for prose that omits curly quotes.
# Regenerate Pe/Pf subset after a Unicode update:
#   import sys, unicodedata
#   chars = sorted(chr(cp) for cp in range(sys.maxunicode + 1)
#                  if unicodedata.category(chr(cp)) in ('Pe', 'Pf'))
#   print(frozenset(chars) | frozenset('"\'>'))

_TRAILING_CLOSERS: frozenset[str] = frozenset((
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

_NONSPACE_RE = re.compile(r"\S", re.UNICODE)

uno_mod: Any


def _advance_past_leading_whitespace(text: str, index: int) -> int:
    """Advance ``index`` while ``text[index]`` is Unicode whitespace (not ASCII space only)."""
    n = min(max(0, index), len(text))
    while n < len(text) and text[n].isspace():
        n += 1
    return n


try:
    uno_mod = importlib.import_module("uno")
except ImportError:
    uno_mod = None

# Monotonic enqueue counter for supersede detection in the work queue.
_ENQUEUE_SEQ_LOCK = threading.Lock()
_ENQUEUE_SEQ = 0

# INFO once when grammar is off (Writer still calls doProofreading); reset when enabled again.
_GRAMMAR_DISABLED_NOTICE_EMITTED = False


def _grammar_obs(event: str, **fields: Any) -> None:
    """DEBUG-only observability for LibreOffice ``doProofreading`` / queue behavior.

    Grep ``writeragent_debug.log`` for ``[grammar] obs`` to correlate UNO parameters,
    slice bounds, sentence splits, and cache decisions without adding INFO noise.

    Ruff caps ``line-length`` at **320** (tool maximum). Typical ``_grammar_obs`` calls fit on one line;
    a few longest calls use ``# fmt: skip`` so ``ruff format`` does not reflow them. Run ``make ruff-format-grammar`` after edits to this file.
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.debug("[grammar] obs %s %s", event, kv)


def _slice_preview_debug(text: str, max_len: int = 72) -> str:
    """Compact one-line preview for DEBUG logs (avoid dumping huge paragraphs)."""
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}…"


def _proofreading_markup_type() -> int:
    """``com.sun.star.text.TextMarkupType.PROOFREADING`` via PyUNO constant lookup."""
    if uno_mod is None:
        return 0
    try:
        v: Any = uno_mod.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")
        return int(cast("int | float | str", v))
    except Exception as e:
        log.warning("[grammar] _proofreading_markup_type: falling back to 4: %s", e, exc_info=True)
        return 4


def _grammar_text_preview(text: str) -> str:
    words = text.strip().split()
    return " ".join(words[:3]) if words else "(empty)"


def _emit_grammar_status(phase: str, text: str, *, result: str = "", elapsed_ms: int | None = None) -> None:
    try:
        from plugin.framework.event_bus import global_event_bus

        global_event_bus.emit("grammar:status", phase=phase, preview=_grammar_text_preview(text), length=len(text), result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


from .grammar_proofread_engine import GrammarWorkItem as _GrammarWorkItem, NormalizedProofError, deduplicate_grammar_batch as _deduplicate_grammar_batch
from .grammar_queue_state import (
    inflight_superseded as _grammar_queue_inflight_superseded,
    is_stale as _grammar_queue_is_stale,
    record_enqueue_latest,
    tail_enqueue_operation,
)


class _GrammarWorkQueue:
    """Single-worker sequential queue for grammar LLM requests.

    Solves two problems:
    1. **Stampede**: N cache misses no longer spawn N workers that all
       contend for ``llm_request_lane`` simultaneously.
    2. **Prefix waste**: when the user types, successive calls may produce
       growing or edited text. ``inflight_key`` includes document + locale +
       sentence start offset so (a) edits in one sentence supersede prior queued
       work for that sentence only, and (b) multiple sentences in one paragraph
       do not collapse to a single survivor. Prefix-related slices in one batch
       still collapse to the newest within each ``(doc_id, locale)`` group.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[_GrammarWorkItem | None] = queue.Queue()
        self._seq_lock = threading.Lock()
        self._latest_seq: dict[str, int] = {}
        self._worker_started = False
        self._worker_lock = threading.Lock()

    @staticmethod
    def _slice_preview(item: _GrammarWorkItem, max_len: int = 48) -> str:
        slice_txt = item.full_text[item.n_start : item.n_end]
        compact = " ".join(slice_txt.split())
        if len(compact) <= max_len:
            return compact
        return f"{compact[:max_len]}…"

    def _latest_seq_for(self, inflight_key: str) -> int | None:
        with self._seq_lock:
            return self._latest_seq.get(inflight_key)

    def _is_stale(self, item: _GrammarWorkItem) -> bool:
        with self._seq_lock:
            return _grammar_queue_is_stale(self._latest_seq, item)

    def inflight_superseded(self, inflight_key: str, enqueue_seq: int) -> bool:
        """True if a newer grammar enqueue has been recorded for this key (e.g. user kept typing)."""
        with self._seq_lock:
            return _grammar_queue_inflight_superseded(self._latest_seq, inflight_key, enqueue_seq)

    def enqueue(self, item: _GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call."""
        with self._seq_lock:
            self._latest_seq, out_of_order, superseded_prev_seq = record_enqueue_latest(self._latest_seq, item)
            if out_of_order:
                log.error("[grammar] queue enqueue: out-of-order seq detected for key=%s: incoming seq=%s < latest seq=%s; stale detection may be unreliable", item.inflight_key, item.enqueue_seq, superseded_prev_seq)
        log.info("[grammar] queue enqueue doc_id=%s locale=%s seq=%s key=%s len=%s preview=%r", item.doc_id, item.grammar_bcp47, item.enqueue_seq, item.inflight_key, len(item.full_text[item.n_start : item.n_end]), self._slice_preview(item))
        _grammar_obs("queue_enqueue", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key, n_start=item.n_start, n_end=item.n_end, slice_len=len(item.full_text[item.n_start : item.n_end]), partial_sentence=item.partial_sentence, preview=_slice_preview_debug(item.full_text[item.n_start : item.n_end]))  # fmt: skip

        # Enqueue-time replace-in-place (O(1) tail check using queue.Queue internals).
        # In a typing burst, the most recent item is almost always the one
        # we want to supersede.
        with self._q.mutex:
            tail = self._q.queue[-1] if self._q.queue else None
            op = tail_enqueue_operation(tail, item)
            if op == "replace_tail":
                assert tail is not None
                log.info("[grammar] queue replace-at-tail key=%s: seq=%s replacing older seq=%s", item.inflight_key, item.enqueue_seq, tail.enqueue_seq)
                _grammar_obs("queue_replace_tail", inflight_key=item.inflight_key, new_seq=item.enqueue_seq, old_seq=tail.enqueue_seq)
                self._q.queue[-1] = item
            elif op == "append":
                self._q.queue.append(item)
                self._q.unfinished_tasks += 1
                self._q.not_empty.notify()
            else:
                log.info("[grammar] queue skip-stale-tail key=%s: incoming seq=%s <= existing seq=%s", item.inflight_key, item.enqueue_seq, tail.enqueue_seq if tail else None)

        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started:
                return
            self._worker_started = True
        t = threading.Thread(target=self._drain_loop, name="writeragent-grammar-queue", daemon=True)
        t.start()

    def _drain_loop(self) -> None:
        """Block-dequeue, batch-drain pending items, deduplicate, process sequentially."""
        while True:
            first = self._q.get()
            if first is None:
                break
            # Drain any items that accumulated while the previous request was in flight
            batch: list[_GrammarWorkItem] = [first]
            while True:
                try:
                    # Wait for a pause of no new results coming in
                    more = self._q.get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)
                    if more is None:
                        return
                    batch.append(more)
                except queue.Empty:
                    break
            log.info("[grammar] queue drain: batch_size=%s", len(batch))
            _grammar_obs("queue_drain_batch", batch_size=len(batch), seqs=tuple(x.enqueue_seq for x in batch), keys=tuple(x.inflight_key for x in batch))
            # deduplicate_grammar_batch: comments on cross-key prefix bug live above that function.
            survivors = _deduplicate_grammar_batch(batch)
            log.info("[grammar] queue drain: survivors=%s", len(survivors))
            _grammar_obs("queue_drain_survivors", survivor_count=len(survivors), seqs=tuple(x.enqueue_seq for x in survivors))
            for item in survivors:
                latest = self._latest_seq_for(item.inflight_key)
                if self._is_stale(item):
                    log.info("[grammar] queue stale-skip doc_id=%s locale=%s seq=%s latest=%s key=%s preview=%r", item.doc_id, item.grammar_bcp47, item.enqueue_seq, latest, item.inflight_key, self._slice_preview(item))
                    _grammar_obs("queue_stale_skip", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, latest_seq=latest, inflight_key=item.inflight_key)
                    continue
                try:
                    log.info("[grammar] queue execute doc_id=%s locale=%s seq=%s latest=%s key=%s len=%s preview=%r", item.doc_id, item.grammar_bcp47, item.enqueue_seq, latest, item.inflight_key, len(item.full_text[item.n_start : item.n_end]), self._slice_preview(item))
                    _grammar_obs("queue_execute", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, latest_seq=latest, inflight_key=item.inflight_key, n_start=item.n_start, n_end=item.n_end, slice_len=len(item.full_text[item.n_start : item.n_end]), partial_sentence=item.partial_sentence)
                    _run_llm_and_cache(
                        item.ctx,
                        item.full_text,
                        item.n_start,
                        item.n_end,
                        item.enqueue_seq,
                        item.inflight_key,
                        item.grammar_bcp47,
                        partial_sentence=item.partial_sentence,
                        proofread_sentence_text=item.proofread_sentence_text,
                    )
                except Exception as e:
                    log.error("[grammar] queue worker item failed: %s", e, exc_info=True)


_grammar_queue = _GrammarWorkQueue()


def _cached_errors_to_uno_tuple(cached: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    from . import grammar_proofread_engine as engine

    ignored_now = engine.ignored_rules_snapshot()
    norms = [
        engine.NormalizedProofError(n_error_start=int(d["n_error_start"]), n_error_length=int(d["n_error_length"]), suggestions=tuple(d.get("suggestions") or ()), short_comment=str(d.get("short_comment", "")), full_comment=str(d.get("full_comment", "")), rule_identifier=str(d.get("rule_identifier", "")))
        for d in cached
        if str(d.get("rule_identifier", "")) not in ignored_now
    ]
    return _errors_to_uno_tuple(norms)


def _locale_key(loc: Any) -> str:
    try:
        return f"{loc.Language}_{loc.Country}_{loc.Variant}"
    except Exception as e:
        log.debug("[grammar] _locale_key: %s", e, exc_info=True)
        return "unknown"


def _locale_tuple() -> tuple[Any, ...]:
    """Locales returned by ``getLocales`` — must match ``LinguisticWriterAgentGrammar.xcu`` ``Locales``.

    The XCU uses hyphenated BCP47-like tags in one ``oor:string-list`` value; UNO uses
    ``com.sun.star.lang.Locale`` in the same order as ``GRAMMAR_REGISTRY_LOCALE_TAGS``.

    LibreOffice merges the registry list with ``XSupportedLocales``; an extra locale here that is
    not listed under GrammarCheckers in the XCU has been observed to trigger a UNO RuntimeException
    when opening Tools → Options → Language Settings (Writing aids).
    """
    from .grammar_locale_registry import GRAMMAR_REGISTRY_LOCALE_TAGS, bcp47_to_uno_lang_country

    if uno_mod is None:
        return ()
    out: list[Any] = []
    try:
        for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
            la, ctry = bcp47_to_uno_lang_country(tag)
            out.append(cast("Any", uno_mod.createUnoStruct("com.sun.star.lang.Locale", Language=la, Country=ctry, Variant="")))
        return tuple(out)
    except Exception as e:
        log.error("[grammar] _locale_tuple: Locale construction failed: %s", e, exc_info=True)
        return ()


def ensure_writeragent_proofreader_configured(ctx: Any) -> None:
    """Log Doc-tab grammar state only.

    We intentionally do **not** call ``XLinguServiceManager2.setConfiguredServices`` here: doing that
    during startup/sidebar init has been observed to destabilize LibreOffice (Writing aids / proofreader
    list). The Linguistic ``GrammarCheckers`` XCU is bundled in the default OXT; users still pick the
    active grammar checker under Tools → Options → Language Settings → Writing aids.
    """
    from plugin.framework.config import get_config_bool
    from plugin.framework.logging import init_logging

    try:
        init_logging(ctx)
    except Exception as e:
        log.warning("[grammar] ensure_proofreader_selection: init_logging: %s", e, exc_info=True)
    log.info("[grammar] ensure_proofreader_selection: entry")
    if uno_mod is None:
        log.warning("[grammar] ensure_proofreader_selection: uno module missing, skipping")
        return
    try:
        enabled = get_config_bool(ctx, "doc.grammar_proofreader_enabled")
    except Exception as e:
        log.warning("[grammar] ensure_proofreader_selection: cannot read doc.grammar_proofreader_enabled: %s", e, exc_info=True)
        return
    if not enabled:
        log.info("[grammar] ensure_proofreader_selection: Doc-tab AI grammar off (enable on Doc tab to use the checker)")
        return
    log.info("[grammar] Doc-tab AI grammar on — if Writer does not underline yet, set WriterAgent as the active grammar checker under Tools → Options → Language Settings → Writing aids for the document language (same locales as the extension’s UI translation set).")


def _build_empty_result(proofreader: Any, a_document_identifier: Any, a_text: str, a_locale: Any, n_start_of_sentence_position: int, n_suggested_behind_end_of_sentence_position: int) -> Any:
    """Initialize ProofreadingResult (sentence bounds aligned with Lightproof)."""
    if uno_mod is None:
        raise RuntimeError("uno not available")
    try:
        a_res = cast("Any", uno_mod.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult"))
    except Exception as e:
        log.exception("[grammar] _build_empty_result: createUnoStruct ProofreadingResult failed: %s", e)
        raise
    try:
        a_res.aDocumentIdentifier = a_document_identifier
        a_res.aText = a_text
        a_res.aLocale = a_locale
        a_res.nStartOfSentencePosition = n_start_of_sentence_position
        a_res.nStartOfNextSentencePosition = n_suggested_behind_end_of_sentence_position
        a_res.aProperties = ()
        a_res.xProofreader = proofreader
        a_res.aErrors = ()
        # Default: follow LO’s suggested end + Lightproof-style space adjustment (see
        # ``_apply_proofreading_end_positions`` when we cover a computed sentence span).
        n_next = n_suggested_behind_end_of_sentence_position
        if n_next < len(a_text):
            before = n_next
            n_next = _advance_past_leading_whitespace(a_text, n_next)
            ch = a_text[n_next : n_next + 1] if n_next < len(a_text) else ""
            if n_next == before and ch != "":
                n_next = n_suggested_behind_end_of_sentence_position + 1
        a_res.nStartOfNextSentencePosition = n_next
        a_res.nBehindEndOfSentencePosition = n_next
        return a_res
    except Exception as e:
        log.exception("[grammar] _build_empty_result: filling ProofreadingResult failed: %s", e)
        raise


def _apply_proofreading_end_positions(a_res: Any, a_text: str, covered_end: int) -> None:
    """Set traversal positions from the one-past-end of the span we actually checked (sentence-sized).

    Skips spaces after ``covered_end`` so Writer advances past inter-sentence whitespace.
    """
    n_next = min(max(0, covered_end), len(a_text))
    n_next = _advance_past_leading_whitespace(a_text, n_next)
    a_res.nStartOfNextSentencePosition = n_next
    a_res.nBehindEndOfSentencePosition = n_next


def _grammar_inflight_key(a_document_identifier: str, loc_key: str, sentence_start: int) -> str:
    """Queue supersede key: stable while editing inside one sentence; distinct per sentence in a paragraph."""
    return f"{a_document_identifier}|{loc_key}|{sentence_start}"


def _span_overlaps_range(s_start: int, s_end: int, lo: int, hi: int) -> bool:
    """Half-open ``[s_start, s_end)`` overlaps ``[lo, hi)`` (empty range yields False)."""
    return lo < hi and s_start < hi and s_end > lo


def candidate_sentence_spans_for_proofreading(
    ctx: Any,
    engine: Any,
    loc_key: str,
    a_text: str,
    n_start_lo: int,
    n_suggested_behind_end: int,
) -> list[tuple[int, int, str]]:
    """Return ``(abs_start, abs_end, sentence_text)`` for sentences Writer should check this call.

    - ``n_start_lo == 0``: paragraph-scale pass — all sentences in ``a_text``.
    - Else: incremental — sentences overlapping LibreOffice's active range.
    """
    all_sents = engine.split_into_sentences(ctx, loc_key, a_text)
    if not all_sents:
        return []
    nlen = len(a_text)
    spans: list[tuple[int, int, str]] = []
    for off, txt in all_sents:
        end = off + len(txt)
        spans.append((off, end, txt))
    if n_start_lo == 0:
        return spans
    lo = max(0, min(n_start_lo, nlen))
    hi = max(lo, min(n_suggested_behind_end, nlen))
    return [(s, e, t) for s, e, t in spans if _span_overlaps_range(s, e, lo, hi)]


def filter_sentence_spans_for_thresholds(spans: Sequence[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop incomplete sentences shorter than ``GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS`` (conservative churn avoidance)."""
    out: list[tuple[int, int, str]] = []
    for s, e, txt in spans:
        nonspace_len = _count_nonspace_chars(txt)
        complete_sentence = _looks_complete_sentence(txt)
        partial_allowed = nonspace_len >= GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
        if not complete_sentence and not partial_allowed:
            continue
        out.append((s, e, txt))
    return out


def _count_nonspace_chars(text: str) -> int:
    return len(_NONSPACE_RE.findall(text or ""))


def _last_meaningful_char(text: str) -> str:
    if not text:
        return ""
    for ch in reversed(text.rstrip()):
        if ch in _TRAILING_CLOSERS:
            continue
        return ch
    return ""


def _looks_complete_sentence(text: str) -> bool:
    return _last_meaningful_char(text) in _SENTENCE_TERMINATORS


def _errors_to_uno_tuple(norms: Sequence[NormalizedProofError]) -> tuple[Any, ...]:
    if uno_mod is None:
        return ()
    out: list[Any] = []
    for idx, e in enumerate(norms):
        try:
            a_err = cast("Any", uno_mod.createUnoStruct("com.sun.star.linguistic2.SingleProofreadingError"))
            a_err.nErrorStart = e.n_error_start
            a_err.nErrorLength = e.n_error_length
            a_err.nErrorType = _proofreading_markup_type()
            a_err.aRuleIdentifier = e.rule_identifier
            a_err.aSuggestions = tuple(e.suggestions)
            a_err.aShortComment = e.short_comment
            a_err.aFullComment = e.full_comment
            a_err.aProperties = ()
            out.append(a_err)
        except Exception as ex:
            log.warning("[grammar] _errors_to_uno_tuple: skipped error index=%s rule=%r: %s", idx, getattr(e, "rule_identifier", ""), ex, exc_info=True)
    return tuple(out)


def _run_llm_and_cache(
    ctx: Any,
    full_text: str,
    n_start: int,
    n_end: int,
    enqueue_seq: int,
    inflight_key: str,
    grammar_bcp47: str,
    partial_sentence: bool = False,
    *,
    proofread_sentence_text: str = "",
) -> None:
    try:
        from plugin.framework.config import get_api_config, get_config_bool, get_config_str, get_text_model
        from plugin.framework.queue_executor import is_agent_active, llm_request_lane
        from . import grammar_proofread_engine as engine
        from .grammar_locale_registry import grammar_english_name_for_bcp47

        try:
            if not get_config_bool(ctx, "doc.grammar_proofreader_enabled"):
                # Normal path when grammar is off: doProofreading returns before enqueue — queue stays empty.
                # This only runs if grammar was toggled off after an item was queued.
                _grammar_obs("worker_skip", reason="grammar_disabled_after_enqueue", enqueue_seq=enqueue_seq, inflight_key=inflight_key)
                return
        except Exception as e:
            log.warning("[grammar] worker: get_config_bool enabled: %s", e, exc_info=True)
            return
        try:
            pause_during_agent = get_config_bool(ctx, "doc.grammar_proofreader_pause_during_agent")
        except Exception as e:
            log.warning("[grammar] worker: get_config_bool pause_during_agent: %s", e, exc_info=True)
            pause_during_agent = False
        if pause_during_agent and is_agent_active():
            log.info("[grammar] worker skipped: agent active and pause_during_agent enabled")
            _grammar_obs("worker_skip", reason="pause_during_agent", enqueue_seq=enqueue_seq, inflight_key=inflight_key)
            return
        slice_txt = full_text[n_start:n_end]
        _grammar_obs("worker_slice", enqueue_seq=enqueue_seq, inflight_key=inflight_key, grammar_bcp47=grammar_bcp47, partial_sentence=partial_sentence, n_start=n_start, n_end=n_end, slice_len=len(slice_txt), slice_preview=_slice_preview_debug(slice_txt))
        if len(slice_txt) > GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS:
            log.info("[grammar] worker skipped: slice len %s > GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS %s", len(slice_txt), GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS)
            _grammar_obs("worker_skip", reason="slice_exceeds_safety_max_chars", slice_len=len(slice_txt), max_chars=GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS)
            return
        if proofread_sentence_text:
            # Main thread already chose this sentence; avoid BreakIterator on substring disagreeing with cache keys.
            if proofread_sentence_text != slice_txt:
                log.warning("[grammar] worker slice mismatch (using enqueue proofread_sentence_text) n_start=%s n_end=%s", n_start, n_end)
            to_process = [proofread_sentence_text]
            _grammar_obs("worker_use_enqueue_sentence_text", enqueue_seq=enqueue_seq, len_proof=len(proofread_sentence_text))
        else:
            sentences = engine.split_into_sentences(ctx, grammar_bcp47, slice_txt)
            if not sentences:
                to_process = [slice_txt]
                _grammar_obs("worker_split_fallback_whole_slice", enqueue_seq=enqueue_seq, slice_len=len(slice_txt))
            else:
                to_process = [txt for _off, txt in sentences]
                if len(to_process) > 1:
                    log.info("[grammar] worker: slice split into %s parts; processing each separately", len(to_process))
                    _grammar_obs("worker_multi_fragment_slice", enqueue_seq=enqueue_seq, fragment_count=len(to_process))

        uncached_texts: list[str] = []
        for sent_text in to_process:
            if engine.cache_get_sentence(grammar_bcp47, sent_text) is None:
                uncached_texts.append(sent_text)
        if not uncached_texts:
            log.info("[grammar] worker skipped: all sentence(s) already cached (race) len=%s", len(slice_txt))
            _grammar_obs("worker_skip", reason="all_sentences_cached_race", enqueue_seq=enqueue_seq, slice_len=len(slice_txt))
            return

        max_tok = GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
        try:
            model = get_config_str(ctx, "doc.grammar_proofreader_model").strip() or get_text_model(ctx)
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""
        _lang = grammar_english_name_for_bcp47(grammar_bcp47)
        base_sys = GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(lang_name=_lang, bcp47=grammar_bcp47)
        from plugin.framework.client.llm_client import LlmClient

        client = LlmClient(get_api_config(ctx), ctx)
        total_norms = 0
        for sent_text in uncached_texts:
            # Hard safety: still avoid megabyte runs without terminators (split_into_sentences can return one blob).
            llm_text = sent_text
            if len(llm_text) > GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS:
                llm_text = llm_text[:GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS]
                _grammar_obs("worker_sentence_truncated_to_safety", enqueue_seq=enqueue_seq, truncated_len=len(llm_text))
            use_partial = partial_sentence or not _looks_complete_sentence(llm_text)
            sys_prompt = base_sys
            if use_partial:
                sys_prompt += " The input may be a partial sentence; prefer conservative grammar suggestions and avoid broad rewrites."
            _grammar_obs("worker_llm_request_prepare", enqueue_seq=enqueue_seq, llm_text_len=len(llm_text), llm_preview=_slice_preview_debug(llm_text, 96))
            messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": llm_text}]
            log.info(
                "[grammar] LLM request (one sentence) llm_text_len=%s max_tokens=%s model=%s",
                len(llm_text),
                max_tok,
                model or "(default text model)",
            )
            request_start = time.monotonic()
            _emit_grammar_status("request", llm_text, result="LLM request")
            with llm_request_lane():
                content = client.chat_completion_sync(messages, max_tokens=max_tok, model=model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
            elapsed_ms = int((time.monotonic() - request_start) * 1000)
            log.debug("[grammar] LLM raw response length=%s", len(content or ""))
            if _grammar_queue.inflight_superseded(inflight_key, enqueue_seq):
                log.info("[grammar] worker skip cache_put: superseded during LLM seq=%s key=%s", enqueue_seq, inflight_key)
                _grammar_obs("worker_skip", reason="superseded_during_llm", enqueue_seq=enqueue_seq, inflight_key=inflight_key)
                continue
            items = engine.parse_grammar_json(content or "")
            log.info("[grammar] parsed %s error item(s) from JSON", len(items))
            ignored = engine.ignored_rules_snapshot()
            norms = engine.normalize_errors_for_text(llm_text, 0, len(llm_text), items, ignored, ctx, grammar_bcp47)
            total_norms += len(norms)
            sent_errors = [asdict(n) for n in norms]
            engine.cache_put_sentence(grammar_bcp47, llm_text, sent_errors)
            issue_word = "issue" if len(norms) == 1 else "issues"
            _emit_grammar_status("complete", llm_text, result=f"{len(norms)} {issue_word}", elapsed_ms=elapsed_ms)

        log.info("[grammar] cached errors for %s uncached sentence(s), slice len=%s", len(uncached_texts), len(slice_txt))
        _grammar_obs(
            "worker_cache_put_done",
            enqueue_seq=enqueue_seq,
            uncached_sentence_count=len(uncached_texts),
            normalized_issue_count=total_norms,
            slice_len=len(slice_txt),
        )
    except Exception as e:
        log.error("[grammar] worker failed: %s", e, exc_info=True)
        try:
            _emit_grammar_status("failed", full_text[n_start:n_end], result=type(e).__name__)
        except Exception:
            pass


class WriterAgentAiGrammarProofreader(unohelper.Base, XProofreader, XServiceInfo, XServiceName, XServiceDisplayName, XSupportedLocales):  # pyright: ignore[reportGeneralTypeIssues] — multiple UNO interface bases  # pyrefly: ignore[invalid-inheritance]
    """Grammar checker registered under Linguistic / GrammarCheckers (cf. Lightproof)."""

    def __init__(self, ctx: Any, *args: Any):
        # LibreOffice's Linguistic manager instantiates proofreaders with
        # compatibility arguments before querying XSupportedLocales.
        del args
        super().__init__()
        self.ctx = ctx
        self._implementation_name = IMPLEMENTATION_NAME
        self._supported_service_names = (SERVICE_NAME,)
        try:
            self._locales = _locale_tuple()
        except Exception as e:
            log.error("[grammar] WriterAgentAiGrammarProofreader.__init__: _locale_tuple failed: %s", e, exc_info=True)
            self._locales = ()

    # --- XServiceName / XServiceInfo ---
    def getServiceName(self) -> str:
        return self._implementation_name

    def getImplementationName(self) -> str:
        return self._implementation_name

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName in self._supported_service_names

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return self._supported_service_names

    # --- XSupportedLocales ---
    def hasLocale(self, aLocale: Any) -> bool:
        try:
            from .grammar_locale_registry import normalize_uno_locale_to_bcp47

            if aLocale is None or not self._locales:
                return False
            return normalize_uno_locale_to_bcp47(aLocale) is not None
        except Exception as e:
            log.warning("[grammar] hasLocale: %s", e, exc_info=True)
            return False

    def getLocales(self) -> tuple[Any, ...]:
        try:
            return self._locales
        except Exception as e:
            log.warning("[grammar] getLocales: %s", e, exc_info=True)
            return ()

    # --- XProofreader ---
    def isSpellChecker(self) -> bool:
        return False

    def doProofreading(self, aDocumentIdentifier: str, aText: str, aLocale: Any, nStartOfSentencePosition: int, nSuggestedBehindEndOfSentencePosition: int, aProperties: Any) -> Any:
        if uno_mod is None:
            log.warning("[grammar] doProofreading: uno_mod is None (import failed)")
            raise RuntimeError("uno not available")
        a_res: Any = None
        try:
            from plugin.framework.config import get_config_bool
            from plugin.framework.logging import init_logging
            from . import grammar_proofread_engine as engine
            from .grammar_locale_registry import normalize_uno_locale_to_bcp47

            try:
                init_logging(self.ctx)
            except Exception as e:
                log.warning("[grammar] doProofreading: init_logging: %s", e, exc_info=True)
            a_res = _build_empty_result(self, aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)
            try:
                enabled = get_config_bool(self.ctx, "doc.grammar_proofreader_enabled")
            except Exception as e:
                log.warning("[grammar] doProofreading: could not read doc.grammar_proofreader_enabled -> off: %s", e, exc_info=True)
                enabled = False
            loc_raw = _locale_key(aLocale)
            grammar_bcp47 = normalize_uno_locale_to_bcp47(aLocale)
            if not enabled:
                global _GRAMMAR_DISABLED_NOTICE_EMITTED
                if not _GRAMMAR_DISABLED_NOTICE_EMITTED:
                    _GRAMMAR_DISABLED_NOTICE_EMITTED = True
                    log.info("[grammar] doProofreading: disabled (Doc tab → Enable AI grammar checker)")
                _grammar_obs("do_proofreading_skip", reason="grammar_disabled", doc_id=aDocumentIdentifier, len_aText=len(aText), n_start_lo=nStartOfSentencePosition, n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition, locale_raw=loc_raw)
                return a_res
            _GRAMMAR_DISABLED_NOTICE_EMITTED = False
            if grammar_bcp47 is None:
                log.info("[grammar] doProofreading: locale not in WriterAgent registry: %s", loc_raw)
                _grammar_obs("do_proofreading_skip", reason="locale_not_registered", doc_id=aDocumentIdentifier, len_aText=len(aText), n_start_lo=nStartOfSentencePosition, n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition, locale_raw=loc_raw)
                return a_res
            loc_key = grammar_bcp47
            _grammar_obs("do_proofreading_entry", doc_id=aDocumentIdentifier, len_aText=len(aText), n_start_lo=nStartOfSentencePosition, n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition, grammar_bcp47=grammar_bcp47, locale_raw=loc_raw, text_preview=_slice_preview_debug(aText))

            raw_spans = candidate_sentence_spans_for_proofreading(self.ctx, engine, loc_key, aText, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)
            work_spans = filter_sentence_spans_for_thresholds(raw_spans)
            if not work_spans:
                log.info("[grammar] doProofreading: no eligible sentences (overlap/threshold) n_start=%s", nStartOfSentencePosition)
                _grammar_obs(
                    "do_proofreading_skip",
                    reason="no_eligible_sentences_or_incomplete_short",
                    doc_id=aDocumentIdentifier,
                    n_start_lo=nStartOfSentencePosition,
                    raw_candidates=len(raw_spans),
                    grammar_bcp47=grammar_bcp47,
                )
                return a_res

            covered_end = max(end for _s, end, _t in work_spans)
            _apply_proofreading_end_positions(a_res, aText, covered_end)
            _grammar_obs(
                "do_proofreading_covered_span",
                doc_id=aDocumentIdentifier,
                grammar_bcp47=grammar_bcp47,
                covered_end=covered_end,
                sentence_count=len(work_spans),
                n_start_lo=nStartOfSentencePosition,
                n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition,
                n_next=getattr(a_res, "nStartOfNextSentencePosition", None),
            )
            log.info(
                "[grammar] doProofreading doc_id=%r len_text=%s locale=%s lo_range=[%s,%s) covered_end=%s sentences=%s enabled=%s",
                aDocumentIdentifier,
                len(aText),
                loc_key,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
                covered_end,
                len(work_spans),
                enabled,
            )

            combined_errors: list[dict[str, Any]] = []
            uncached_spans: list[tuple[int, int, str]] = []
            for sent_start, _sent_end, sent_text in work_spans:
                cached = engine.cache_get_sentence(loc_key, sent_text)
                _grammar_obs(
                    "do_proofreading_sentence_cache",
                    doc_id=aDocumentIdentifier,
                    sent_start=sent_start,
                    sent_len=len(sent_text),
                    cache_hit=cached is not None,
                    sent_preview=_slice_preview_debug(sent_text, 48),
                )
                if cached is None:
                    uncached_spans.append((sent_start, _sent_end, sent_text))
                    continue
                for err_item in cached:
                    adj = dict(err_item)
                    adj["n_error_start"] = sent_start + err_item.get("n_error_start", 0)
                    combined_errors.append(adj)

            if not uncached_spans:
                try:
                    a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors))
                    log.info("[grammar] per-sentence cache ALL HIT: %s sentence(s), %s error(s)", len(work_spans), len(combined_errors))
                    _grammar_obs("do_proofreading_cache_all_hit", doc_id=aDocumentIdentifier, grammar_bcp47=grammar_bcp47, sentence_count=len(work_spans), error_count=len(combined_errors))
                except Exception as e:
                    log.exception("[grammar] doProofreading: per-sentence cache HIT path failed: %s", e)
                    try:
                        a_res.aErrors = ()
                    except Exception:
                        pass
                return a_res

            if combined_errors:
                try:
                    a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors))
                except Exception as e:
                    log.exception("[grammar] doProofreading: partial cache path failed: %s", e)
            cached_ct = len(work_spans) - len(uncached_spans)
            if cached_ct > 0:
                log.info(
                    "[grammar] per-sentence cache PARTIAL HIT: %s cached, %s uncached → enqueueing sentence-sized item(s)",
                    cached_ct,
                    len(uncached_spans),
                )
                miss_reason = "partial_miss"
            else:
                log.info(
                    "[grammar] per-sentence cache MISS (all %s sentence(s) uncached) → enqueueing",
                    len(uncached_spans),
                )
                miss_reason = "all_uncached"
            _grammar_obs(
                "do_proofreading_cache_partial_hit",
                doc_id=aDocumentIdentifier,
                grammar_bcp47=grammar_bcp47,
                cached_count=cached_ct,
                uncached_count=len(uncached_spans),
                errors_returned=len(combined_errors),
                miss_reason=miss_reason,
            )

            global _ENQUEUE_SEQ
            for sent_start, sent_end, sent_text in uncached_spans:
                with _ENQUEUE_SEQ_LOCK:
                    _ENQUEUE_SEQ += 1
                    seq = _ENQUEUE_SEQ
                inflight_key = _grammar_inflight_key(aDocumentIdentifier, loc_key, sent_start)
                complete_sentence = _looks_complete_sentence(sent_text)
                log.info("[grammar] cache MISS enqueue sentence seq=%s key=%s len=%s", seq, inflight_key, len(sent_text))
                _grammar_obs(
                    "do_proofreading_enqueue",
                    doc_id=aDocumentIdentifier,
                    grammar_bcp47=grammar_bcp47,
                    inflight_key=inflight_key,
                    enqueue_seq=seq,
                    n_start=sent_start,
                    n_end=sent_end,
                    slice_len=len(sent_text),
                    partial_sentence_arg=not complete_sentence,
                )
                _emit_grammar_status("start", sent_text, result="queued")
                _grammar_queue.enqueue(
                    _GrammarWorkItem(
                        ctx=self.ctx,
                        full_text=aText,
                        n_start=sent_start,
                        n_end=sent_end,
                        grammar_bcp47=grammar_bcp47,
                        partial_sentence=not complete_sentence,
                        doc_id=aDocumentIdentifier,
                        inflight_key=inflight_key,
                        enqueue_seq=seq,
                        proofread_sentence_text=sent_text,
                    )
                )
            log.info("[grammar] doProofreading: async miss returning partial or empty errors; sentence cache fills in background")
            return a_res
        except Exception as e:
            log.exception("[grammar] doProofreading failed (returning empty errors if possible): %s", e)
            if a_res is not None:
                try:
                    a_res.aErrors = ()
                except Exception:
                    pass
                return a_res
            try:
                from plugin.framework.logging import init_logging

                init_logging(self.ctx)
            except Exception:
                pass
            return _build_empty_result(self, aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        try:
            from . import grammar_proofread_engine as engine

            del aLocale  # locale-specific ignore not distinguished in cache yet
            engine.ignore_rule_add(str(aRuleIdentifier))
        except Exception as e:
            log.warning("[grammar] ignoreRule: %s", e, exc_info=True)

    def resetIgnoreRules(self) -> None:
        try:
            from . import grammar_proofread_engine as engine

            engine.ignore_rules_clear()
        except Exception as e:
            log.warning("[grammar] resetIgnoreRules: %s", e, exc_info=True)

    # --- XServiceDisplayName ---
    def getServiceDisplayName(self, aLocale: Any) -> str:
        try:
            _ = aLocale
            return "WriterAgent AI Grammar"
        except Exception as e:
            log.warning("[grammar] getServiceDisplayName: %s", e, exc_info=True)
            return "WriterAgent AI Grammar"


try:
    import unohelper

    g_ImplementationHelper = unohelper.ImplementationHelper()
    g_ImplementationHelper.addImplementation(WriterAgentAiGrammarProofreader, IMPLEMENTATION_NAME, (SERVICE_NAME,))
except (ImportError, AttributeError):
    g_ImplementationHelper = None  # type: ignore[assignment]
