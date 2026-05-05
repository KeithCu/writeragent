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
# so ``import plugin...`` works (file is plugin/modules/writer/ai_grammar_proofreader.py).
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
from typing import Any, no_type_check

import unohelper

from com.sun.star.lang import Locale, XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XProofreader, XSupportedLocales

log = logging.getLogger("writeragent.grammar")
# Do not inherit writeragent's log level (often WARN); grammar uses INFO for diagnostics.
log.setLevel(logging.DEBUG)

IMPLEMENTATION_NAME = "org.extension.writeragent.comp.pyuno.AiGrammarProofreader"
SERVICE_NAME = "com.sun.star.linguistic2.Proofreader"

# Fixed caps (not user-configurable): batching / LLM slice length and JSON response budget.
GRAMMAR_PROOFREAD_MAX_CHARS = 500
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

# Locale-agnostic sentence terminators used as a conservative fallback signal.
_SENTENCE_TERMINATORS = frozenset((".", "!", "?", "…", "؟", "。", "！", "？", "।"))
_TRAILING_CLOSERS = frozenset(("\"", "'", ")", "]", "}", ">", "»", "“", "‘", "」", "』", "）", "］", "〉", "》", "】", "〕", "〗", "〛"))
_NONSPACE_RE = re.compile(r"\S", re.UNICODE)

uno_mod: Any
try:
    uno_mod = importlib.import_module("uno")
except ImportError:
    uno_mod = None

# Monotonic enqueue counter for supersede detection in the work queue.
_ENQUEUE_SEQ_LOCK = threading.Lock()
_ENQUEUE_SEQ = 0


@no_type_check
def _proofreading_markup_type() -> int:
    """``com.sun.star.text.TextMarkupType.PROOFREADING`` via PyUNO constant lookup."""
    if uno_mod is None:
        return 0
    try:
        v: Any = uno_mod.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")
        return int(v)
    except Exception as e:
        log.warning(
            "[grammar] _proofreading_markup_type: falling back to 4: %s",
            e,
            exc_info=True,
        )
        return 4


def _grammar_text_preview(text: str) -> str:
    words = text.strip().split()
    return " ".join(words[:3]) if words else "(empty)"


def _emit_grammar_status(
    phase: str,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
) -> None:
    try:
        from plugin.framework.event_bus import global_event_bus

        global_event_bus.emit(
            "grammar:status",
            phase=phase,
            preview=_grammar_text_preview(text),
            length=len(text),
            result=result,
            elapsed_ms=elapsed_ms,
        )
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


from plugin.modules.writer.grammar_proofread_engine import (
    GrammarWorkItem as _GrammarWorkItem,
    deduplicate_grammar_batch as _deduplicate_grammar_batch,
)


class _GrammarWorkQueue:
    """Single-worker sequential queue for grammar LLM requests.

    Solves two problems:
    1. **Stampede**: N cache misses no longer spawn N workers that all
       contend for ``llm_request_lane`` simultaneously.
    2. **Prefix waste**: when the user types, successive calls may produce
       growing or edited text. At dequeue time, same ``inflight_key`` keeps only
       the newest item; prefix-related slices in one batch also collapse to the
       newest within each ``(doc_id, locale)`` group.
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
        latest = self._latest_seq_for(item.inflight_key)
        return latest is not None and item.enqueue_seq < latest

    def enqueue(self, item: _GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call."""
        with self._seq_lock:
            prev_seq = self._latest_seq.get(item.inflight_key)
            if prev_seq is not None and item.enqueue_seq < prev_seq:
                log.error(
                    "[grammar] queue enqueue: out-of-order seq detected for key=%s: "
                    "incoming seq=%s < latest seq=%s; stale detection may be unreliable",
                    item.inflight_key,
                    item.enqueue_seq,
                    prev_seq,
                )
            self._latest_seq[item.inflight_key] = item.enqueue_seq
        log.info(
            "[grammar] queue enqueue doc_id=%s locale=%s seq=%s key=%s len=%s preview=%r",
            item.doc_id,
            item.grammar_bcp47,
            item.enqueue_seq,
            item.inflight_key,
            len(item.full_text[item.n_start : item.n_end]),
            self._slice_preview(item),
        )

        # Enqueue-time replace-in-place (bounded 10-item scan using queue.Queue internals).
        # We use the internal mutex and deque to update the best request in-flight
        # before it even reaches the drain loop.
        with self._q.mutex:
            found = False
            for i, existing in enumerate(self._q.queue):
                if i >= 10:
                    break
                if existing is not None and existing.inflight_key == item.inflight_key:
                    if item.enqueue_seq > existing.enqueue_seq:
                        log.info(
                            "[grammar] queue replace-in-place key=%s: seq=%s replacing older seq=%s at index=%s",
                            item.inflight_key,
                            item.enqueue_seq,
                            existing.enqueue_seq,
                            i,
                        )
                        self._q.queue[i] = item
                    else:
                        log.info(
                            "[grammar] queue skip-duplicate key=%s: incoming seq=%s is not newer than existing seq=%s at index=%s",
                            item.inflight_key,
                            item.enqueue_seq,
                            existing.enqueue_seq,
                            i,
                        )
                    found = True
                    break

            if not found:
                self._q.queue.append(item)
                self._q.unfinished_tasks += 1
                self._q.not_empty.notify()

        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started:
                return
            self._worker_started = True
        t = threading.Thread(
            target=self._drain_loop,
            name="writeragent-grammar-queue",
            daemon=True,
        )
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
            survivors = _deduplicate_grammar_batch(batch)
            log.info("[grammar] queue drain: survivors=%s", len(survivors))
            for item in survivors:
                latest = self._latest_seq_for(item.inflight_key)
                if self._is_stale(item):
                    log.info(
                        "[grammar] queue stale-skip doc_id=%s locale=%s seq=%s latest=%s key=%s preview=%r",
                        item.doc_id,
                        item.grammar_bcp47,
                        item.enqueue_seq,
                        latest,
                        item.inflight_key,
                        self._slice_preview(item),
                    )
                    continue
                try:
                    log.info(
                        "[grammar] queue execute doc_id=%s locale=%s seq=%s latest=%s key=%s len=%s preview=%r",
                        item.doc_id,
                        item.grammar_bcp47,
                        item.enqueue_seq,
                        latest,
                        item.inflight_key,
                        len(item.full_text[item.n_start : item.n_end]),
                        self._slice_preview(item),
                    )
                    _run_llm_and_cache(
                        item.ctx,
                        item.full_text,
                        item.n_start,
                        item.n_end,
                        item.enqueue_seq,
                        item.inflight_key,
                        item.grammar_bcp47,
                        partial_sentence=item.partial_sentence,
                    )
                except Exception as e:
                    log.error("[grammar] queue worker item failed: %s", e, exc_info=True)


_grammar_queue = _GrammarWorkQueue()


def _cached_errors_to_uno_tuple(cached: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    from plugin.modules.writer import grammar_proofread_engine as engine

    ignored_now = engine.ignored_rules_snapshot()
    norms = [
        engine.NormalizedProofError(
            n_error_start=int(d["n_error_start"]),
            n_error_length=int(d["n_error_length"]),
            suggestions=tuple(d.get("suggestions") or ()),
            short_comment=str(d.get("short_comment", "")),
            full_comment=str(d.get("full_comment", "")),
            rule_identifier=str(d.get("rule_identifier", "")),
        )
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


@no_type_check
def _locale_tuple() -> tuple[Any, ...]:
    """Locales returned by ``getLocales`` — must match ``LinguisticWriterAgentGrammar.xcu`` ``Locales``.

    The XCU uses hyphenated BCP47-like tags in one ``oor:string-list`` value; UNO uses
    ``com.sun.star.lang.Locale`` in the same order as ``GRAMMAR_REGISTRY_LOCALE_TAGS``.

    LibreOffice merges the registry list with ``XSupportedLocales``; an extra locale here that is
    not listed under GrammarCheckers in the XCU has been observed to trigger a UNO RuntimeException
    when opening Tools → Options → Language Settings (Writing aids).
    """
    from plugin.modules.writer.grammar_locale_registry import (
        GRAMMAR_REGISTRY_LOCALE_TAGS,
        bcp47_to_uno_lang_country,
    )

    out: list[Any] = []
    try:
        for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
            la, ctry = bcp47_to_uno_lang_country(tag)
            out.append(Locale(la, ctry, ""))
        return tuple(out)
    except Exception as e:
        log.error("[grammar] _locale_tuple: Locale construction failed: %s", e, exc_info=True)
        return ()


@no_type_check
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
        log.warning(
            "[grammar] ensure_proofreader_selection: cannot read doc.grammar_proofreader_enabled: %s",
            e,
            exc_info=True,
        )
        return
    if not enabled:
        log.info(
            "[grammar] ensure_proofreader_selection: Doc-tab AI grammar off (enable on Doc tab to use the checker)"
        )
        return
    log.info(
        "[grammar] Doc-tab AI grammar on — if Writer does not underline yet, set WriterAgent as the "
        "active grammar checker under Tools → Options → Language Settings → Writing aids for the "
        "document language (same locales as the extension’s UI translation set)."
    )


@no_type_check
def _build_empty_result(
    proofreader: Any,
    a_document_identifier: Any,
    a_text: str,
    a_locale: Any,
    n_start_of_sentence_position: int,
    n_suggested_behind_end_of_sentence_position: int,
) -> Any:
    """Initialize ProofreadingResult (sentence bounds aligned with Lightproof)."""
    try:
        a_res: Any = uno_mod.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult")
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
        # ``_finalize_proofreading_sentence_positions`` for the nStart==0 grammar batch path).
        n_next = n_suggested_behind_end_of_sentence_position
        if n_next < len(a_text):
            ch = a_text[n_next : n_next + 1]
            while ch == " ":
                n_next += 1
                ch = a_text[n_next : n_next + 1] if n_next < len(a_text) else ""
            if n_next == n_suggested_behind_end_of_sentence_position and ch != "":
                n_next = n_suggested_behind_end_of_sentence_position + 1
        a_res.nStartOfNextSentencePosition = n_next
        a_res.nBehindEndOfSentencePosition = n_next
        return a_res
    except Exception as e:
        log.exception("[grammar] _build_empty_result: filling ProofreadingResult failed: %s", e)
        raise


@no_type_check
def _finalize_proofreading_sentence_positions(
    a_res: Any,
    a_text: str,
    n_suggested_behind_end: int,
    proofread_batch_end: int,
) -> None:
    """Lightproof-style batching (``lightproof/Lightproof.py`` after LO 4 patch).

    Writer grows ``nSuggestedBehindEndOfSentencePosition`` per keystroke; we treat the
    proofread window as ``a_text[0:proofread_batch_end]`` with ``proofread_batch_end`` capped
    by ``GRAMMAR_PROOFREAD_MAX_CHARS``. Then set ``nStartOfNextSentencePosition`` /
    ``nBehindEndOfSentencePosition`` from that batch end with the same space-skipping idea as
    ``_build_empty_result`` / Lightproof lines 126–132.
    """
    n_next = proofread_batch_end
    if n_next < len(a_text):
        ch = a_text[n_next : n_next + 1]
        while ch == " ":
            n_next += 1
            ch = a_text[n_next : n_next + 1] if n_next < len(a_text) else ""
        # Lightproof fallback: if space-skipping didn't advance past LO's suggested end
        # and we're not at EOF, nudge by one.  In the capped-batch path this condition
        # is nearly unreachable because proofread_batch_end < n_suggested_behind_end,
        # but it is kept for parity with Lightproof lines 126-132.
        if n_next == n_suggested_behind_end and ch != "":
            log.debug(
                "[grammar] _finalize: Lightproof fallback nudge fired "
                "n_next=%s n_suggested=%s batch_end=%s text_len=%s",
                n_next,
                n_suggested_behind_end,
                proofread_batch_end,
                len(a_text),
            )
            assert proofread_batch_end >= n_suggested_behind_end, (
                f"Lightproof fallback expected proofread_batch_end ({proofread_batch_end}) "
                f">= n_suggested_behind_end ({n_suggested_behind_end})"
            )
            n_next = n_suggested_behind_end + 1
    a_res.nStartOfNextSentencePosition = n_next
    a_res.nBehindEndOfSentencePosition = n_next


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


@no_type_check
def _errors_to_uno_tuple(
    norms: list[Any],
) -> tuple[Any, ...]:
    out: list[Any] = []
    for idx, e in enumerate(norms):
        try:
            a_err: Any = uno_mod.createUnoStruct("com.sun.star.linguistic2.SingleProofreadingError")
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
            log.warning(
                "[grammar] _errors_to_uno_tuple: skipped error index=%s rule=%r: %s",
                idx,
                getattr(e, "rule_identifier", ""),
                ex,
                exc_info=True,
            )
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
) -> None:
    try:
        from plugin.framework.config import (
            get_api_config,
            get_config_bool,
            get_config_str,
            get_text_model,
        )
        from plugin.framework.llm_concurrency import (
            is_agent_active,
            llm_request_lane,
        )
        from plugin.modules.writer import grammar_proofread_engine as engine
        from plugin.modules.writer.grammar_locale_registry import (
            grammar_english_name_for_bcp47,
        )

        try:
            if not get_config_bool(ctx, "doc.grammar_proofreader_enabled"):
                log.info("[grammar] worker skipped: doc.grammar_proofreader_enabled is false after debounce")
                return
        except Exception as e:
            log.warning("[grammar] worker: get_config_bool enabled: %s", e, exc_info=True)
            return
        try:
            pause_during_agent = get_config_bool(
                ctx, "doc.grammar_proofreader_pause_during_agent"
            )
        except Exception as e:
            log.warning(
                "[grammar] worker: get_config_bool pause_during_agent: %s",
                e,
                exc_info=True,
            )
            pause_during_agent = False
        if pause_during_agent and is_agent_active():
            log.info(
                "[grammar] worker skipped: agent active and pause_during_agent enabled"
            )
            return
        slice_txt = full_text[n_start:n_end]
        if len(slice_txt) > GRAMMAR_PROOFREAD_MAX_CHARS:
            log.info(
                "[grammar] worker skipped: slice len %s > GRAMMAR_PROOFREAD_MAX_CHARS %s",
                len(slice_txt),
                GRAMMAR_PROOFREAD_MAX_CHARS,
            )
            return
        # Check which sentences are already cached; only send uncached ones to the LLM.
        sentences = engine.split_into_sentences(ctx, grammar_bcp47, slice_txt)
        if sentences:
            uncached: list[tuple[int, str]] = [
                (off, txt)
                for off, txt in sentences
                if engine.cache_get_sentence(grammar_bcp47, txt) is None
            ]
            if not uncached:
                log.info(
                    "[grammar] worker skipped: all %s sentence(s) already cached for batch len=%s",
                    len(sentences),
                    len(slice_txt),
                )
                return
            log.info(
                "[grammar] worker: %s/%s sentence(s) uncached, sending only uncached to LLM",
                len(uncached),
                len(sentences),
            )
        else:
            # No sentence boundaries found — treat entire slice as one uncached chunk
            uncached = [(0, slice_txt)]
        # Build the text to send: concatenate uncached sentences with a space separator.
        # Track each sentence's offset within the concatenated text for error attribution.
        llm_parts: list[str] = []
        part_map: list[tuple[int, str]] = []  # (offset_in_llm_text, original_sentence)
        pos = 0
        for _orig_offset, sent_text in uncached:
            llm_parts.append(sent_text)
            part_map.append((pos, sent_text))
            pos += len(sent_text) + 1  # +1 for the space separator
        llm_text = " ".join(llm_parts)
        max_tok = GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
        try:
            model = get_config_str(ctx, "doc.grammar_proofreader_model").strip() or get_text_model(ctx)
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""
        _lang = grammar_english_name_for_bcp47(grammar_bcp47)
        sys_prompt = GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(
            lang_name=_lang, bcp47=grammar_bcp47
        )
        if partial_sentence:
            sys_prompt += (
                " The input may be a partial sentence; prefer conservative grammar suggestions and "
                "avoid broad rewrites."
            )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": llm_text},
        ]
        log.info(
            "[grammar] LLM request llm_text_len=%s (uncached %s/%s sent) max_tokens=%s model=%s",
            len(llm_text),
            len(uncached),
            len(sentences) if sentences else 1,
            max_tok,
            model or "(default text model)",
        )
        request_start = time.monotonic()
        _emit_grammar_status("request", llm_text, result="LLM request")
        from plugin.modules.http.client import LlmClient

        client = LlmClient(get_api_config(ctx), ctx)
        with llm_request_lane():
            content = client.chat_completion_sync(
                messages,
                max_tokens=max_tok,
                model=model or None,
                response_format={"type": "json_object"},
                prepend_dev_build_system_prefix=False,
            )
        elapsed_ms = int((time.monotonic() - request_start) * 1000)
        log.debug("[grammar] LLM raw response length=%s", len(content or ""))
        items = engine.parse_grammar_json(content or "")
        log.info("[grammar] parsed %s error item(s) from JSON", len(items))
        ignored = engine.ignored_rules_snapshot()
        # Normalize errors against the concatenated LLM text.
        norms = engine.normalize_errors_for_text(llm_text, 0, len(llm_text), items, ignored, ctx, grammar_bcp47)
        # Attribute errors to individual sentences and cache each.
        for llm_offset, sent_text in part_map:
            sent_end_in_llm = llm_offset + len(sent_text)
            sent_errors = [
                {**asdict(n), "n_error_start": n.n_error_start - llm_offset}
                for n in norms
                if llm_offset <= n.n_error_start < sent_end_in_llm
            ]
            engine.cache_put_sentence(grammar_bcp47, sent_text, sent_errors)
        log.info(
            "[grammar] cached errors for %s uncached sentence(s), batch len=%s",
            len(uncached),
            len(slice_txt),
        )
        issue_word = "issue" if len(norms) == 1 else "issues"
        _emit_grammar_status("complete", llm_text, result=f"{len(norms)} {issue_word}", elapsed_ms=elapsed_ms)
    except Exception as e:
        log.error("[grammar] worker failed: %s", e, exc_info=True)
        try:
            _emit_grammar_status("failed", full_text[n_start:n_end], result=type(e).__name__)
        except Exception:
            pass


@no_type_check
class WriterAgentAiGrammarProofreader(
    unohelper.Base,
    XProofreader,
    XServiceInfo,
    XServiceName,
    XServiceDisplayName,
    XSupportedLocales,
):
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
            from plugin.modules.writer.grammar_locale_registry import (
                normalize_uno_locale_to_bcp47,
            )

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

    def doProofreading(
        self,
        aDocumentIdentifier: str,
        aText: str,
        aLocale: Any,
        nStartOfSentencePosition: int,
        nSuggestedBehindEndOfSentencePosition: int,
        aProperties: Any,
    ) -> Any:
        if uno_mod is None:
            log.warning("[grammar] doProofreading: uno_mod is None (import failed)")
            raise RuntimeError("uno not available")
        a_res: Any = None
        try:
            from plugin.framework.config import get_config_bool
            from plugin.framework.logging import init_logging
            from plugin.modules.writer import grammar_proofread_engine as engine
            from plugin.modules.writer.grammar_locale_registry import (
                normalize_uno_locale_to_bcp47,
            )

            try:
                init_logging(self.ctx)
            except Exception as e:
                log.warning("[grammar] doProofreading: init_logging: %s", e, exc_info=True)
            a_res = _build_empty_result(
                self,
                aDocumentIdentifier,
                aText,
                aLocale,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
            )
            try:
                enabled = get_config_bool(self.ctx, "doc.grammar_proofreader_enabled")
            except Exception as e:
                log.warning(
                    "[grammar] doProofreading: could not read doc.grammar_proofreader_enabled -> off: %s",
                    e,
                    exc_info=True,
                )
                enabled = False
            loc_raw = _locale_key(aLocale)
            grammar_bcp47 = normalize_uno_locale_to_bcp47(aLocale)
            if not enabled:
                log.info("[grammar] doProofreading: disabled (Doc tab → Enable AI grammar checker)")
                return a_res
            if grammar_bcp47 is None:
                log.info(
                    "[grammar] doProofreading: locale not in WriterAgent registry: %s",
                    loc_raw,
                )
                return a_res
            loc_key = grammar_bcp47
            # Lightproof.py "PATCH FOR LO 4": Writer issues incremental calls with nStart != 0; return
            # empty until the sentence-start pass. Otherwise we spam LLM/cache on every sub-span and
            # fight Writer's real grammar pass (same pattern as lightproof/Lightproof.py).
            if nStartOfSentencePosition != 0:
                log.debug(
                    "[grammar] doProofreading: skip incremental nStart=%s (await nStart==0 pass)",
                    nStartOfSentencePosition,
                )
                return a_res
            proofread_batch_end = min(len(aText), GRAMMAR_PROOFREAD_MAX_CHARS)
            _finalize_proofreading_sentence_positions(
                a_res,
                aText,
                nSuggestedBehindEndOfSentencePosition,
                proofread_batch_end,
            )
            log.info(
                "[grammar] doProofreading doc_id=%r len_text=%s locale=%s lo_range=[%s,%s) "
                "batch_end=%s enabled=%s",
                aDocumentIdentifier,
                len(aText),
                loc_key,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
                proofread_batch_end,
                enabled,
            )
            n_start = max(0, nStartOfSentencePosition)
            n_end = proofread_batch_end
            if n_end <= n_start:
                log.info("[grammar] doProofreading: empty span after clamp (%s,%s)", n_start, n_end)
                return a_res
            slice_txt = aText[n_start:n_end]
            # Trust LO sentence boundaries (nStart==0 batch). Cache by exact sentence text.
            # Identical sentence text anywhere in the document reuses the same errors (relative to sentence start).
            # This fulfills the requirement to cache per sentence without worrying about document offsets.
            nonspace_len = _count_nonspace_chars(slice_txt)
            complete_sentence = _looks_complete_sentence(slice_txt)
            partial_allowed = nonspace_len >= GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
            if not complete_sentence and not partial_allowed:
                log.info(
                    "[grammar] doProofreading: skip incomplete short sentence len_nonspace=%s min=%s",
                    nonspace_len,
                    GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS,
                )
                return a_res

            # Per-sentence cache lookup: split the batch into individual sentences
            # and check each. Return cached errors immediately (even partial — better
            # than empty). Enqueue only if there are uncached sentences.
            sentences = engine.split_into_sentences(self.ctx, loc_key, slice_txt)
            if sentences:
                combined_errors: list[dict[str, Any]] = []
                uncached_count = 0
                for sent_offset, sent_text in sentences:
                    cached = engine.cache_get_sentence(loc_key, sent_text)
                    if cached is None:
                        uncached_count += 1
                        continue
                    # Cached errors are relative to sentence start (0);
                    # shift to paragraph position: n_start + sent_offset + error_offset
                    for err_item in cached:
                        adj = dict(err_item)
                        adj["n_error_start"] = n_start + sent_offset + err_item.get("n_error_start", 0)
                        combined_errors.append(adj)
                if uncached_count == 0:
                    # All sentences cached — return full result, no enqueue needed
                    try:
                        a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors))
                        log.info(
                            "[grammar] per-sentence cache ALL HIT: %s sentence(s), %s error(s) for batch len=%s",
                            len(sentences),
                            len(combined_errors),
                            len(slice_txt),
                        )
                    except Exception as e:
                        log.exception("[grammar] doProofreading: per-sentence cache HIT path failed: %s", e)
                        try:
                            a_res.aErrors = ()
                        except Exception:
                            pass
                    return a_res
                # Partial miss: return cached errors now (better than empty),
                # and fall through to enqueue for the uncached sentences.
                if combined_errors:
                    try:
                        a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors))
                    except Exception as e:
                        log.exception("[grammar] doProofreading: partial cache path failed: %s", e)
                log.info(
                    "[grammar] per-sentence cache PARTIAL HIT: %s/%s cached (%s error(s) returned), "
                    "%s uncached → enqueueing",
                    len(sentences) - uncached_count,
                    len(sentences),
                    len(combined_errors),
                    uncached_count,
                )

            # inflight_key must not include slice text fingerprint: mid-sentence edits
            # change content without prefix relation, so same-key supersede + _latest_seq
            # stale checks would never fire and every keystroke would run an LLM call.
            inflight_key = f"{aDocumentIdentifier}|{loc_key}"
            global _ENQUEUE_SEQ
            with _ENQUEUE_SEQ_LOCK:
                _ENQUEUE_SEQ += 1
                seq = _ENQUEUE_SEQ
            log.info(
                "[grammar] cache MISS enqueuing slice_len=%s key=%s seq=%s",
                len(slice_txt),
                inflight_key,
                seq,
            )
            _emit_grammar_status("start", slice_txt, result="queued")
            _grammar_queue.enqueue(
                _GrammarWorkItem(
                    ctx=self.ctx,
                    full_text=aText,
                    n_start=n_start,
                    n_end=n_end,
                    grammar_bcp47=grammar_bcp47,
                    partial_sentence=not complete_sentence,
                    doc_id=aDocumentIdentifier,
                    inflight_key=inflight_key,
                    enqueue_seq=seq,
                )
            )
            # Async path: never wait or pump here — keeps menus/dialogs responsive. Squiggles on a later pass.
            log.info(
                "[grammar] doProofreading: async miss returning empty errors; sentence cache fills in background"
            )
            return a_res
        except Exception as e:
            log.exception(
                "[grammar] doProofreading failed (returning empty errors if possible): %s",
                e,
            )
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
            return _build_empty_result(
                self,
                aDocumentIdentifier,
                aText,
                aLocale,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
            )

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        try:
            from plugin.modules.writer import grammar_proofread_engine as engine

            del aLocale  # locale-specific ignore not distinguished in cache yet
            engine.ignore_rule_add(str(aRuleIdentifier))
        except Exception as e:
            log.warning("[grammar] ignoreRule: %s", e, exc_info=True)

    def resetIgnoreRules(self) -> None:
        try:
            from plugin.modules.writer import grammar_proofread_engine as engine

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


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    WriterAgentAiGrammarProofreader,
    IMPLEMENTATION_NAME,
    (SERVICE_NAME,),
)
