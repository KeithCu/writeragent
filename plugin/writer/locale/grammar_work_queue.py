# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar work queue: work items, batch dedup, pure enqueue/stale helpers, parallel LLM workers.

Queue dedup / stale-suppression mental model
=============================================

The grammar queue must ensure that for any given ``inflight_key``, only the
**newest** snapshot (highest ``enqueue_seq``) ever reaches the LLM.  Two
remaining layers (plus the ``_latest_seq`` generation map) enforce this
invariant.  (A third Layer 1 "tail-replace" existed historically — see below.)

**Layer 2 — Batch-drain dedup** (``_drain_loop`` dict accumulator + ``deduplicate_grammar_batch``)

    After the worker wakes on the first ``get()``, it enters a tight
    ``get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)`` loop that collects every
    pending item into a ``batch_by_key`` dict keyed by ``inflight_key``.
    For each key only the item with the highest ``enqueue_seq`` is kept.
    The result is then passed through ``deduplicate_grammar_batch`` — which
    applies the same highest-seq-wins rule — as a canonical safety net (it is
    also the standalone pure function used by tests and any external caller).

    *Why both the dict and ``deduplicate_grammar_batch``?*  The dict handles
    the fast path during draining; ``deduplicate_grammar_batch`` is defense-
    in-depth and the single source of truth for the dedup contract.

    *Blind spot*: Neither can detect items that were already consumed in a
    *previous* batch and whose ``inflight_key`` was re-enqueued while the
    worker was busy with the LLM.

**Layer 3 — Pre-execute and post-LLM stale checks** (``_latest_seq`` map)

    ``enqueue`` records the newest ``enqueue_seq`` per ``inflight_key`` in
    ``_latest_seq`` (under ``_seq_lock``).  Before sending a batch item to
    the LLM, the worker calls ``_is_stale`` — if a newer enqueue has been
    recorded since this item was drained, it is skipped.  After the LLM
    returns, ``inflight_superseded`` is checked again before writing to the
    sentence cache, catching items superseded during the (possibly slow)
    HTTP round-trip.

**Historical Layer 1 (removed)**

    An earlier O(1) "tail-replace" lived in ``enqueue()``: it acquired
    ``self._q.mutex`` and directly mutated ``self._q.queue[-1]`` when the
    tail shared the same ``inflight_key`` and the incoming item had a higher
    seq.  This was the classic "clever" bit (direct access to a ``Queue``'s
    internal deque + ``unfinished_tasks`` / ``not_empty.notify()``).

    It was removed because:
    - The worker drains so quickly that the queue is *usually empty* on the
      next enqueue during real typing bursts (the exact scenario the comment
      in the old Layer 2 section called out).
    - Layer 2 (the drain dict) + the canonical ``deduplicate_grammar_batch``
      + Layer 3 (``_latest_seq`` guards, including the language-requeue path)
      already provide complete protection.
    - Removing it eliminates the highest-cognitive-load construct while
      changing no observable behavior for squiggles, cache, or LLM calls.

**``inflight_key`` design**

    Complete sentences: ``{doc_id}|{locale}|{hash(text)[:16]}``.  Unique
    per sentence, stable if the sentence is unchanged — so two different
    sentences in the same paragraph never collide.

    Incomplete sentences: ``{doc_id}|{locale}|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING``.
    All partial drafts for the active typing spot share one key, ensuring
    every keystroke supersedes the previous draft.

**``enqueue_seq`` as generation stamp**

    A global monotonic counter (``next_enqueue_seq``), not a queue position.
    It records *when* a snapshot was created.  Queue FIFO only orders
    ``get()`` calls; ``enqueue_seq`` records supersede relationships across
    batches and stale checks (the old tail-replace path is no longer one of
    them).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


from . import (
    grammar_proofread_cache,
    grammar_proofread_locale,
    grammar_proofread_text,
    grammar_persistence,
    grammar_worker_phases,
)
from .grammar_obs import emit_grammar_status, grammar_obs
from .grammar_proofread_text import slice_preview_debug
from .grammar_worker_llm import call_grammar_llm, detect_languages_for_chunk

from plugin.framework import queue_executor, config
from plugin.framework.client import model_fetcher, llm_client
from plugin.framework.client.request_controls import LLM_MIN_REQUEST_INTERVAL_SEC

import uno

log = logging.getLogger("writeragent.grammar")

# Super simple locale detection: 1k chars around cursor should be "good enough" for most cases.
# Historical overkill (styles, first 50 paragraphs, LinguProperties) was removed to prioritize
# speed and local relevance. If precision drops, consider re-adding style-based detection
# or a smarter text portion enumeration within the visible range.
def _get_cached_document_locales(ctx: Any, doc_id: str) -> list[str]:
    now = time.time()
    with grammar_persistence.grammar_registry.lock:
        cached = grammar_persistence.grammar_registry.doc_locales_cache.get(doc_id)
    if cached is not None and now - cached[0] < 60:
        return cached[1]

    def _query_locales() -> list[str]:
        locales = set()
        try:
            model = grammar_persistence.get_document_model_for_id(ctx, doc_id)
            if model:
                # 1000 characters around the view cursor (500 behind, 500 ahead)
                ctrl = getattr(model, "getCurrentController", lambda: None)()
                view_cursor = getattr(ctrl, "getViewCursor", lambda: None)() if ctrl else None
                if view_cursor:
                    try:
                        tc = view_cursor.getText().createTextCursorByRange(view_cursor)
                        tc.goLeft(500, False)
                        for _ in range(1000):
                            if not tc.goRight(1, True):
                                break
                            loc = getattr(tc, "CharLocale", None)
                            bcp = grammar_proofread_locale.normalize_uno_locale_to_bcp47(loc)
                            if bcp:
                                locales.add(bcp)
                            tc.collapseToEnd()
                    except Exception as e:
                        log.debug("[grammar] Failed to scan near cursor for locales: %s", e)
                        
                log.debug("[grammar] Document locale detection finished (cursor scan). Found: %s", locales)
        except Exception as e:
            log.warning("Failed to query document for locales: %s", e)

        if not locales:
            locales.add("en-US")
        return sorted(list(locales))

    try:
        locs = queue_executor.execute_on_main_thread(_query_locales)
        with grammar_persistence.grammar_registry.lock:
            grammar_persistence.grammar_registry.doc_locales_cache[doc_id] = (now, locs)
        return locs
    except Exception as e:
        log.warning("Failed to get cached locales: %s", e)
        return ["en-US"]

def _apply_language_change(ctx: Any, doc_id: str, sentence_text: str, detected_bcp47: str) -> None:
    def _do_update() -> None:
        model = grammar_persistence.get_document_model_for_id(ctx, doc_id)
        if not model:
            return
        
        lang, country = grammar_proofread_locale.bcp47_to_uno_lang_country(
            grammar_proofread_locale.normalize_detected_bcp47(detected_bcp47) or detected_bcp47
        )

        new_locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language=lang, Country=country)
        
        ctrl = getattr(model, "getCurrentController", lambda: None)()
        view_cursor = getattr(ctrl, "getViewCursor", lambda: None)() if ctrl else None
        
        search_desc = model.createSearchDescriptor()
        search_desc.setSearchString(sentence_text)
        try:
            search_desc.setPropertyValue("SearchCaseSensitive", True)
        except Exception:
            # Fallback for attribute assignment
            search_desc.SearchCaseSensitive = True
        
        found_range = None
        if view_cursor:
            found_range = model.findNext(view_cursor.getStart(), search_desc)
        
        if not found_range:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            try:
                text_obj = model.getText()
                doc_start = text_obj.getStart()
                found_range = model.findNext(doc_start, search_desc)
            except Exception:
                found_range = model.findFirst(search_desc)

        if not found_range:
            found_range = model.findFirst(search_desc)

        if found_range:
            found_range.setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s", detected_bcp47)
            
    try:
        queue_executor.execute_on_main_thread(_do_update)
    except Exception as e:
        log.warning("Failed to update language property: %s", e)


@dataclass(frozen=True)
class GrammarWorkItem:
    """One queued grammar job (defined here so dedup tests avoid UNO imports)."""

    ctx: Any
    text: str
    grammar_bcp47: str
    partial_sentence: bool
    doc_id: str
    inflight_key: str
    enqueue_seq: int
    original_bcp47: str = ""



@dataclass(frozen=True)
class GrammarWorkerContext:
    """Shared I/O context for grammar worker phases (LLM, queue, document)."""
    ctx: Any
    client: Any
    gq: GrammarWorkQueue | None
    model: str
    original_bcp47: str
    grammar_bcp47: str
    max_tok: int
    detect_lang_instruction: str = ""
    detect_lang_mode: str = "off"


def deduplicate_grammar_batch(batch: list[GrammarWorkItem]) -> list[GrammarWorkItem]:
    """Return one queue item per ``inflight_key``, keeping the highest ``enqueue_seq``."""
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        if should_replace_for_key(prev, item):
            best_by_key[item.inflight_key] = item
        else:
            log.info("[grammar] queue dedup: dropped older same-key item seq=%s key=%s (newer seq=%s kept)", item.enqueue_seq, item.inflight_key, prev.enqueue_seq if prev else None)
    return list(best_by_key.values())


def record_enqueue_latest(prev: dict[str, int], item: GrammarWorkItem) -> tuple[dict[str, int], bool, int | None]:
    """Return updated ``latest_seq``, whether incoming seq was out-of-order, and prior seq for logging."""
    key = item.inflight_key
    prev_seq = prev.get(key)
    out_of_order = prev_seq is not None and item.enqueue_seq < prev_seq
    new_d = dict(prev)
    new_d[key] = item.enqueue_seq
    return new_d, out_of_order, prev_seq if out_of_order else None


def _enqueue_seq_superseded_by_latest(latest_seq: Mapping[str, int], inflight_key: str, enqueue_seq: int) -> bool:
    """True if ``latest_seq`` records a newer generation than ``enqueue_seq`` for ``inflight_key`` (pre-execute skip and post-LLM cache skip)."""
    latest = latest_seq.get(inflight_key)
    return latest is not None and enqueue_seq < latest


def is_stale(latest_seq: Mapping[str, int], item: GrammarWorkItem) -> bool:
    """True if a newer enqueue has been recorded for this ``inflight_key``."""
    return _enqueue_seq_superseded_by_latest(latest_seq, item.inflight_key, item.enqueue_seq)


def inflight_superseded(latest_seq: Mapping[str, int], inflight_key: str, enqueue_seq: int) -> bool:
    """True if ``enqueue_seq`` is older than the latest known generation for ``inflight_key``."""
    return _enqueue_seq_superseded_by_latest(latest_seq, inflight_key, enqueue_seq)


def should_replace_for_key(existing: GrammarWorkItem | None, incoming: GrammarWorkItem) -> bool:
    """True if ``incoming`` should replace ``existing`` in a per-key accumulator.

    Used by both the ``_drain_loop`` dict accumulator (Layer 2 fast path) and
    ``deduplicate_grammar_batch`` (canonical pure dedup).  A missing ``existing``
    (first item for this key) always returns True.
    """
    return existing is None or incoming.enqueue_seq > existing.enqueue_seq


def filter_stale_and_group(
    survivors: list[GrammarWorkItem],
    is_stale_fn: Any,
) -> dict[tuple[str, str], list[GrammarWorkItem]]:
    """Drop stale items and group the rest by ``(doc_id, grammar_bcp47)``.

    ``is_stale_fn`` is called with each item; items for which it returns True
    are skipped (with an obs log).  Returns a dict mapping
    ``(doc_id, locale)`` to the non-stale items in that group.
    """
    groups: dict[tuple[str, str], list[GrammarWorkItem]] = defaultdict(list)
    stale_count = 0
    for item in survivors:
        if is_stale_fn(item):
            grammar_obs("queue_stale_skip", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key)
            stale_count += 1
            continue
        groups[(item.doc_id, item.grammar_bcp47)].append(item)
    if stale_count > 0:
        grammar_obs("batch_stats", sentences_stale_skipped=stale_count, survivor_count=sum(len(v) for v in groups.values()))
    return dict(groups)


_ENQUEUE_SEQ_LOCK = threading.Lock()
_ENQUEUE_SEQ = 0


def next_enqueue_seq() -> int:
    """Monotonic generation stamp for ``GrammarWorkItem.enqueue_seq`` (supersede / stale detection)."""
    global _ENQUEUE_SEQ
    with _ENQUEUE_SEQ_LOCK:
        _ENQUEUE_SEQ += 1
        return _ENQUEUE_SEQ


def run_llm_and_cache(
    ctx: Any,
    text: str,
    enqueue_seq: int,
    inflight_key: str,
    grammar_bcp47: str,
    partial_sentence: bool = False,
    *,
    doc_id: str = "",
    grammar_queue: Any | None = None,
    original_bcp47: str = "",
) -> None:
    """Process one queue item: LLM request(s) + sentence cache write(s)."""
    item = GrammarWorkItem(
        ctx=ctx,
        text=text,
        grammar_bcp47=grammar_bcp47,
        partial_sentence=partial_sentence,
        doc_id=doc_id,
        inflight_key=inflight_key,
        enqueue_seq=enqueue_seq,
    )
    run_llm_and_cache_batch([item], grammar_queue=grammar_queue, original_bcp47=original_bcp47)


def _obs_language_validation_decision(
    chunk: list[tuple[GrammarWorkItem, str]],
    target_bcp47: str,
    detected: list[str | None],
    decision: grammar_worker_phases.LanguageValidationDecision,
) -> None:
    """Emit TD9 observability for language validation outcomes."""
    requeue_count = len(decision.requeues)
    result_len = len(decision.result_chunk)
    dropped_none = max(0, len(chunk) - result_len - requeue_count)
    grammar_obs(
        "lang_validation_decision",
        chunk_len=len(chunk),
        target_bcp47=target_bcp47,
        result_chunk_len=result_len,
        requeue_count=requeue_count,
        dropped_none_count=dropped_none,
    )
    if len(chunk) > 1:
        for idx, raw in enumerate(detected):
            if raw:
                continue
            item, text = chunk[idx]
            grammar_obs(
                "lang_validation_item_none",
                idx=idx,
                enqueue_seq=item.enqueue_seq,
                text_preview=slice_preview_debug(text, 48),
            )


def _requeue_individual_item(
    item: GrammarWorkItem,
    text: str,
    new_bcp47: str,
    original_bcp47: str,
    ec: GrammarWorkerContext,
    *,
    cache_placeholder: bool = True,
) -> None:
    """Requeue one item after language mismatch or grammar batch count mismatch."""
    sent_complete = (not item.partial_sentence) and grammar_proofread_locale.looks_complete_sentence(text)
    requeue_inflight_key = grammar_proofread_locale.grammar_inflight_key(item.doc_id, new_bcp47, text, sent_complete)

    if cache_placeholder:
        grammar_proofread_cache.cache_put_sentence(original_bcp47, text, [], ctx=ec.ctx, doc_id=item.doc_id)

    if ec.gq:
        new_item = replace(
            item,
            grammar_bcp47=new_bcp47,
            enqueue_seq=next_enqueue_seq(),
            inflight_key=requeue_inflight_key,
            text=text,
            original_bcp47=original_bcp47,
        )
        ec.gq.enqueue(new_item)


def _process_grammar_results(
    chunk: list[tuple[GrammarWorkItem, str]],
    results: list[Any],
    bcp47: str,
    original_bcp47: str,
    elapsed_ms: int,
    ec: GrammarWorkerContext,
) -> None:
    """Normalize LLM errors, write sentence cache, emit done status."""
    from .grammar_ignore_rules import doc_ignored_rules, is_rule_ignored
    from .grammar_proofread_cache import ignored_rules_snapshot

    total_issues = 0
    chars_checked = 0
    n_written = 0
    first_text = ""
    second_text = ""
    for idx, (item, text) in enumerate(chunk):
        if ec.gq and ec.gq.inflight_superseded(item.inflight_key, item.enqueue_seq):
            continue
        if idx < len(results):
            errors = results[idx]
            ignored = doc_ignored_rules(ec.ctx, item.doc_id)
            global_ignored = ignored_rules_snapshot()
            norm_errors = grammar_proofread_text.normalize_errors_for_text(text, 0, len(text), errors, ec.ctx, bcp47)

            filtered_errors = []
            for e in norm_errors:
                if is_rule_ignored(e.rule_identifier, ignored, global_ignored):
                    continue
                filtered_errors.append(e)

            grammar_proofread_cache.cache_put_sentence(bcp47, text, [asdict(e) for e in filtered_errors], ctx=ec.ctx, doc_id=item.doc_id)
            if original_bcp47 and not grammar_proofread_locale.grammar_bcp47_tags_match(original_bcp47, bcp47):
                log.debug("[grammar] Double caching for %s (detected %s)", original_bcp47, bcp47)
                grammar_proofread_cache.cache_put_sentence(original_bcp47, text, [asdict(e) for e in filtered_errors], ctx=ec.ctx, doc_id=item.doc_id)
            else:
                log.debug("[grammar] No double caching: original=%s, detected=%s", original_bcp47, bcp47)

            total_issues += len(filtered_errors)
            chars_checked += len(text)
            n_written += 1
            tstrip = text.strip()
            if n_written == 1 and tstrip:
                first_text = tstrip
            elif n_written == 2 and tstrip:
                second_text = tstrip

    if n_written:
        preview_src = f"{first_text} \u00b7 {second_text}" if second_text else first_text
        iw = "issue" if total_issues == 1 else "issues"
        sw = "sentence" if n_written == 1 else "sentences"
        _emit_done_status(
            ec,
            preview_src,
            result=f"{total_issues} {iw}, {n_written} {sw}",
            elapsed_ms=elapsed_ms,
            preview_source=preview_src,
            length_hint=chars_checked,
        )
    else:
        _emit_done_status(ec, "batch", result="skipped (superseded)", elapsed_ms=elapsed_ms)


def _emit_done_status(
    ec: GrammarWorkerContext,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
    preview_source: str | None = None,
    length_hint: int | None = None,
) -> None:
    """Sidebar ``done``: deferred while parallel drain batches run (see ``GrammarWorkQueue``)."""
    if ec.gq is not None:
        ec.gq.record_done_status(
            text,
            result=result,
            elapsed_ms=elapsed_ms,
            preview_source=preview_source,
            length_hint=length_hint,
        )
        return
    emit_grammar_status(
        "done",
        text,
        result=result,
        elapsed_ms=elapsed_ms,
        preview_source=preview_source,
        length_hint=length_hint,
    )


def _run_language_validation(
    chunk: list[tuple[GrammarWorkItem, str]],
    target_bcp47: str,
    detect_lang_instruction: str,
    ec: GrammarWorkerContext,
) -> grammar_worker_phases.LanguageValidationDecision | None:
    """Optional phase: detect language, filter chunk, requeue mismatches. None on failure."""
    try:
        # Do not treat embedded grammar rows as proof of CharLocale — persistence is keyed by
        # sentence text only, so wrong-locale cache would skip real detection.
        detected = detect_languages_for_chunk(chunk, detect_lang_instruction, ec, trust_persisted_grammar_as_lang=False)
        decision = grammar_worker_phases.decide_language_validation(chunk, target_bcp47, detected)
        _obs_language_validation_decision(chunk, target_bcp47, detected, decision)
        for rq in decision.requeues:
            log.info("[grammar] Language mismatch detected: %s vs %s. Triggering locale change.", rq.new_bcp47, rq.original_bcp47)
            _requeue_individual_item(rq.item, rq.text, rq.new_bcp47, rq.original_bcp47, ec)
        if len(chunk) == 1 and decision.target_bcp47 != target_bcp47:
            log.info("[grammar] Single item language mismatch: %s -> %s. Proceeding with new locale.", target_bcp47, decision.target_bcp47)
        return decision
    except Exception as e:
        log.error("[grammar] Language validation error: %s", e, exc_info=True)
        emit_grammar_status("failed", "Language detection", result=str(e))
        return None


def _run_grammar_check(
    chunk: list[tuple[GrammarWorkItem, str]],
    bcp47: str,
    original_bcp47: str,
    ec: GrammarWorkerContext,
) -> None:
    """Grammar check dispatcher: executes LLM, LanguageTool, or Vale, then caches results."""
    try:
        from plugin.framework.config import get_grammar_provider

        provider = get_grammar_provider()

        if provider == "languagetool":
            from plugin.scripting.client import run_languagetool_check

            for item, text in chunk:
                try:
                    request_start = time.monotonic()
                    res = run_languagetool_check(ec.ctx, text, bcp47)
                    elapsed_ms = int((time.monotonic() - request_start) * 1000)

                    errors = res.get("errors", [])
                    results = [errors]

                    _process_grammar_results([(item, text)], results, bcp47, original_bcp47, elapsed_ms, ec)
                    grammar_obs("worker_grammar_done", chunk_len=1, results_len=len(errors), elapsed_ms=elapsed_ms, bcp47=bcp47)
                except Exception as ex:
                    log.error("[grammar] LanguageTool local check failed: %s", ex)
                    emit_grammar_status("failed", "LanguageTool check", result=str(ex))
            return

        if provider == "vale":
            from plugin.scripting.client import run_vale_check
            from plugin.framework.config import user_config_dir

            cfg_dir = user_config_dir() or ""
            styles = "Microsoft,Google,write-good"

            for item, text in chunk:
                try:
                    request_start = time.monotonic()
                    res = run_vale_check(ec.ctx, text, cfg_dir, styles)
                    elapsed_ms = int((time.monotonic() - request_start) * 1000)

                    errors = res.get("errors", [])
                    results = [errors]

                    _process_grammar_results([(item, text)], results, bcp47, original_bcp47, elapsed_ms, ec)
                    grammar_obs("worker_style_done", chunk_len=1, results_len=len(errors), elapsed_ms=elapsed_ms, bcp47=bcp47)
                except Exception as ex:
                    log.error("[grammar] Vale style linter failed: %s", ex)
                    emit_grammar_status("failed", "Vale style linter", result=str(ex))
            return
        if provider == "harper":
            from plugin.scripting.client import run_harper_check
            from plugin.framework.config import user_config_dir

            cfg_dir = user_config_dir() or ""

            for item, text in chunk:
                try:
                    emit_grammar_status("request", text, result="Harper check")
                    request_start = time.monotonic()
                    res = run_harper_check(ec.ctx, text, cfg_dir, bcp47=bcp47)
                    elapsed_ms = int((time.monotonic() - request_start) * 1000)

                    errors = res.get("errors", [])
                    results = [errors]

                    _process_grammar_results([(item, text)], results, bcp47, original_bcp47, elapsed_ms, ec)
                    grammar_obs("worker_harper_done", chunk_len=1, results_len=len(errors), elapsed_ms=elapsed_ms, bcp47=bcp47)
                except Exception as ex:
                    log.error("[grammar] Harper check failed: %s", ex, exc_info=True)
                    emit_grammar_status("failed", "Harper linter", result=str(ex))
            return



        # Default path: AI (LLM)
        results, elapsed_ms = call_grammar_llm(chunk, bcp47, ec)
        grammar_obs(
            "batch_stats",
            sentences_llm_requested=len(chunk),
            llm_request_duration_ms=elapsed_ms,
            bcp47=bcp47,
        )
        completion = grammar_worker_phases.decide_grammar_completion(len(chunk), len(results), bcp47, original_bcp47)
        if completion.requeue_all:
            if len(results) == 0:
                log.warning(
                    "[grammar] LLM returned no parseable results for chunk of %s (model=%s)",
                    len(chunk),
                    ec.model or "",
                )
                emit_grammar_status("failed", "Grammar check", result="Empty LLM response")
                return
            log.warning(
                "[grammar] LLM batch result count mismatch for chunk: expected %s, got %s. Requeuing items.",
                len(chunk),
                len(results),
            )
            for item, text in chunk:
                _requeue_individual_item(item, text, bcp47, original_bcp47, ec, cache_placeholder=False)
            return
        _process_grammar_results(chunk, results, bcp47, original_bcp47, elapsed_ms, ec)
        grammar_obs("worker_grammar_done", chunk_len=len(chunk), results_len=len(results), elapsed_ms=elapsed_ms, bcp47=bcp47)
        if completion.apply_locale_after_success:
            for item, text in chunk:
                _apply_language_change(ec.ctx, item.doc_id, text, bcp47)
    except Exception as e:
        log.error("[grammar] Grammar check error: %s", e, exc_info=True)
        emit_grammar_status("failed", "Grammar check", result=str(e))


def _worker_batch_gates(ctx: Any, items: list[GrammarWorkItem]) -> bool:
    """Return False when the batch should not run (grammar off or agent pause)."""
    if not config.is_grammar_enabled():
        grammar_obs("worker_batch_skip", reason="grammar_disabled", item_count=len(items))
        return False
    pause_during_agent = config.get_config_bool_safe("doc.grammar_proofreader_pause_during_agent")
    if pause_during_agent and queue_executor.is_agent_active():
        grammar_obs("worker_batch_skip", reason="pause_during_agent", item_count=len(items))
        return False
    return True


def _worker_collect_valid_items(
    items: list[GrammarWorkItem],
    gq: Any,
    grammar_bcp47: str,
    ctx: Any,
) -> list[tuple[GrammarWorkItem, str]]:
    valid_items: list[tuple[GrammarWorkItem, str]] = []
    for item in items:
        if gq.inflight_superseded(item.inflight_key, item.enqueue_seq):
            grammar_obs("worker_skip", reason="superseded_before_process", enqueue_seq=item.enqueue_seq, inflight_key=item.inflight_key)
            continue
        if grammar_proofread_cache.cache_get_sentence(grammar_bcp47, item.text, ctx=ctx, doc_id=item.doc_id) is None:
            valid_items.append((item, item.text))
    return valid_items


def _worker_build_chunks(
    valid_items: list[tuple[GrammarWorkItem, str]],
    ctx: Any,
    batch_size: int,
    max_chars: int,
    detect_lang_enabled: bool,
) -> tuple[list[list[tuple[GrammarWorkItem, str]]], str]:
    """Build LLM chunks and optional language-detect instruction suffix."""
    detect_lang_instruction = ""
    if detect_lang_enabled:
        prefilter_count = len(valid_items)
        filtered_items: list[tuple[GrammarWorkItem, str]] = []
        for item, text in valid_items:
            if item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text):
                continue
            filtered_items.append((item, text))
        valid_items = filtered_items
        if not valid_items:
            grammar_obs("worker_chunk_skip", reason="detect_prefilter_empty", item_count=prefilter_count)
            return [], detect_lang_instruction
        locales_in_use = _get_cached_document_locales(ctx, valid_items[0][0].doc_id)
        detect_lang_instruction = f" Choose from the following locales currently used in the document, or provide a new one if none match: {', '.join(locales_in_use)}."

    chunks: list[list[tuple[GrammarWorkItem, str]]] = []
    if len(valid_items) > 1 and batch_size > 1:
        for i in range(0, len(valid_items), batch_size):
            chunks.append(valid_items[i : i + batch_size])
    else:
        for item, text in valid_items:
            if len(text) > max_chars:
                text = text[:max_chars]
            chunks.append([(item, text)])
    return chunks, detect_lang_instruction


def _worker_process_chunk(
    chunk: list[tuple[GrammarWorkItem, str]],
    ec: GrammarWorkerContext,
    grammar_bcp47: str,
    detect_lang_enabled: bool,
    detect_lang_instruction: str,
) -> None:
    """Run language validation (optional) then grammar LLM for one chunk."""
    current_chunk = chunk
    lang_decision = None
    if detect_lang_enabled:
        lang_decision = _run_language_validation(chunk, grammar_bcp47, detect_lang_instruction, ec)
        if lang_decision is None:
            grammar_obs("worker_chunk_skip", reason="lang_validation_failed", chunk_len=len(chunk))
            return
        current_chunk = lang_decision.result_chunk

    if not current_chunk:
        grammar_obs(
            "worker_chunk_skip",
            reason="empty_result_chunk",
            chunk_len=len(chunk),
            target_bcp47=lang_decision.target_bcp47 if lang_decision else grammar_bcp47,
            requeue_count=len(lang_decision.requeues) if lang_decision else 0,
        )
        return

    current_bcp47 = grammar_bcp47
    if lang_decision is not None:
        current_bcp47 = lang_decision.target_bcp47
        if current_bcp47 != grammar_bcp47:
            updated_chunk = []
            for item, text in current_chunk:
                new_key = grammar_proofread_locale.grammar_inflight_key(item.doc_id, current_bcp47, text, not item.partial_sentence)
                new_item = replace(item, grammar_bcp47=current_bcp47, inflight_key=new_key)
                updated_chunk.append((new_item, text))
            current_chunk = updated_chunk

    _run_grammar_check(current_chunk, current_bcp47, grammar_bcp47, ec)


def run_llm_and_cache_batch(
    items: list[GrammarWorkItem],
    *,
    grammar_queue: Any | None = None,
    original_bcp47: str = "",
) -> None:
    """Process a batch of items (ideally from one paragraph): LLM requests + multi-sentence cache writes."""
    if not items:
        return

    ctx = items[0].ctx
    grammar_bcp47 = items[0].grammar_bcp47
    gq_to_use = grammar_queue or _grammar_queue_singleton
    if not original_bcp47:
        original_bcp47 = items[0].original_bcp47 or grammar_bcp47

    status_cycle_started = False
    try:
        if not _worker_batch_gates(ctx, items):
            return

        valid_items = _worker_collect_valid_items(items, gq_to_use, grammar_bcp47, ctx)
        if not valid_items:
            grammar_obs("worker_batch_skip", reason="all_cached_or_superseded", item_count=len(items))
            return

        gq_to_use.begin_status_cycle()
        status_cycle_started = True

        max_tok = grammar_proofread_locale.GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
        max_chars = grammar_proofread_locale.GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS
        try:
            model = model_fetcher.get_grammar_model()
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""

        client = llm_client.LlmClient(config.get_api_config(), ctx)
        batch_size = config.get_config_int_safe("doc.grammar_proofreader_batch_sentences")
        batch_size = max(1, min(grammar_proofread_locale.GRAMMAR_BATCH_MAX_SENTENCES, batch_size))
        detect_lang_mode = grammar_proofread_locale.get_grammar_detect_language_mode(ctx)
        detect_lang_enabled = detect_lang_mode != "off"

        chunks, detect_lang_instruction = _worker_build_chunks(valid_items, ctx, batch_size, max_chars, detect_lang_enabled)
        if not chunks:
            return

        ec = GrammarWorkerContext(
            ctx=ctx,
            client=client,
            gq=gq_to_use,
            model=model,
            original_bcp47=original_bcp47,
            grammar_bcp47=grammar_bcp47,
            max_tok=max_tok,
            detect_lang_instruction=detect_lang_instruction,
            detect_lang_mode=detect_lang_mode,
        )

        for chunk in chunks:
            _worker_process_chunk(chunk, ec, grammar_bcp47, detect_lang_enabled, detect_lang_instruction)

    except Exception as e:
        log.error("[grammar] worker batch failed: %s", e, exc_info=True)
        try:
            emit_grammar_status("failed", "Batch processing", result=type(e).__name__)
        except Exception:
            pass
    finally:
        if status_cycle_started:
            gq_to_use.end_status_cycle()


@dataclass(frozen=True)
class _PendingGrammarDone:
    text: str
    result: str
    elapsed_ms: int | None
    preview_source: str | None
    length_hint: int | None


class GrammarWorkQueue:
    """Multi-worker queue for grammar LLM requests (stampede + per-key supersede).

    Up to ``doc.grammar_proofreader_max_in_flight`` daemon drain threads share one
    ``queue.Queue``; each batch still respects ``grammar_llm_request_gate`` for HTTP.

    TD4 note: an ``InflightTracker`` wrapper around ``_seq_lock`` + ``_latest_seq``
    was evaluated and rejected — the tracker would absorb 2 fields and 3 thin methods
    but ``GrammarWorkQueue`` is already small enough that an extra indirection adds more
    cognitive load than it removes.  The pure functions (``should_replace_for_key``,
    ``filter_stale_and_group``, ``is_stale``, ``inflight_superseded``,
    ``record_enqueue_latest``) keep the logic testable without wrapping the state.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[GrammarWorkItem | None] = queue.Queue()
        self._seq_lock = threading.Lock()
        self._latest_seq: dict[str, int] = {}
        self._worker_count = 0
        self._worker_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status_inflight = 0
        self._pending_done: _PendingGrammarDone | None = None

    def begin_status_cycle(self) -> None:
        """Mark one ``run_llm_and_cache_batch`` in flight (sidebar ``done`` is deferred)."""
        with self._status_lock:
            self._status_inflight += 1

    def record_done_status(
        self,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
    ) -> None:
        """Remember the latest chunk result; emitted when the last in-flight batch finishes."""
        with self._status_lock:
            self._pending_done = _PendingGrammarDone(text, result, elapsed_ms, preview_source, length_hint)

    def end_status_cycle(self) -> None:
        """Drop in-flight count; emit a single sidebar ``done`` when all parallel batches finish."""
        pending: _PendingGrammarDone | None = None
        with self._status_lock:
            self._status_inflight = max(0, self._status_inflight - 1)
            if self._status_inflight == 0:
                pending = self._pending_done
                self._pending_done = None
        if pending is not None:
            emit_grammar_status(
                "done",
                pending.text,
                result=pending.result,
                elapsed_ms=pending.elapsed_ms,
                preview_source=pending.preview_source,
                length_hint=pending.length_hint,
            )

    def _is_stale(self, item: GrammarWorkItem) -> bool:
        with self._seq_lock:
            return is_stale(self._latest_seq, item)

    def inflight_superseded(self, inflight_key: str, enqueue_seq: int) -> bool:
        """True if a newer grammar enqueue has been recorded for this key (e.g. user kept typing)."""
        with self._seq_lock:
            return inflight_superseded(self._latest_seq, inflight_key, enqueue_seq)

    def enqueue(self, item: GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call.

        Same-key deduplication for rapid typing is handled in two places:
        - The Layer 2 ``batch_by_key`` dict inside ``_drain_loop`` (the primary
          fast path — the worker drains so quickly that the queue is usually
          empty on the next enqueue during bursts).
        - The canonical pure ``deduplicate_grammar_batch`` (defense-in-depth)
          plus the ``_latest_seq`` guards (Layer 3) for cross-batch and in-flight
          supersedes (including language-detection requeues that mint a fresh
          higher seq).
        """
        with self._seq_lock:
            self._latest_seq, out_of_order, superseded_prev_seq = record_enqueue_latest(self._latest_seq, item)
            if out_of_order:
                log.error("[grammar] queue enqueue: out-of-order seq detected for key=%s: incoming seq=%s < latest seq=%s; stale detection may be unreliable", item.inflight_key, item.enqueue_seq, superseded_prev_seq)
        grammar_obs(
            "queue_enqueue",
            sentences_queued=1,
            doc_id=item.doc_id,
            locale=item.grammar_bcp47,
            seq=item.enqueue_seq,
            inflight_key=item.inflight_key,
            slice_len=len(item.text),
            partial_sentence=item.partial_sentence,
            preview=slice_preview_debug(item.text),
        )  # fmt: skip

        # Normal append.  (Historical Layer 1 "tail-replace" under _q.mutex was
        # removed in the TD4 simplification pass because it was ineffective
        # during the common rapid-drain burst case; see the module docstring.)
        self._q.put(item)
        self._ensure_workers(item.ctx)

    def _ensure_workers(self, ctx: Any) -> None:
        desired = grammar_proofread_locale.grammar_max_in_flight(ctx)
        with self._worker_lock:
            while self._worker_count < desired:
                i = self._worker_count
                if i > 0:
                    # Stagger extra drain threads (same 50 ms pacing as LlmClient HTTP sends).
                    time.sleep(LLM_MIN_REQUEST_INTERVAL_SEC)
                self._worker_count += 1
                t = threading.Thread(target=self._drain_loop, name=f"writeragent-grammar-queue-{i}", daemon=True)
                t.start()

    def _drain_loop(self) -> None:
        """Block-dequeue, batch-drain pending items, deduplicate, process one batch."""
        while True:
            first = self._q.get()
            if first is None:
                break
            # Layer 2 fast path: collapse same-key items as they arrive instead
            # of appending all then dedup-ing.  This is now the *primary*
            # dedup point for rapid typing (the historical Layer 1 tail-replace
            # at enqueue time was removed because the worker drains so quickly
            # that the queue is usually empty between keystrokes anyway).
            batch_by_key: dict[str, GrammarWorkItem] = {first.inflight_key: first}
            while True:
                try:
                    more = self._q.get(timeout=grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S)
                    if more is None:
                        return
                    prev = batch_by_key.get(more.inflight_key)
                    if should_replace_for_key(prev, more):
                        batch_by_key[more.inflight_key] = more
                except queue.Empty:
                    break
            batch = list(batch_by_key.values())
            grammar_obs("queue_drain_batch", batch_size=len(batch), seqs=tuple(x.enqueue_seq for x in batch), keys=tuple(x.inflight_key for x in batch))
            # Canonical dedup (defense-in-depth — the drain dict already did
            # same-key newest-wins for this batch, but deduplicate_grammar_batch
            # is the single source of truth for the dedup contract and is also
            # used by unit tests and any external caller).
            survivors = deduplicate_grammar_batch(batch)
            deduped_count = len(batch) - len(survivors)
            grammar_obs(
                "queue_drain_survivors",
                survivor_count=len(survivors),
                seqs=tuple(x.enqueue_seq for x in survivors),
            )
            if deduped_count > 0:
                grammar_obs("batch_stats", sentences_deduped=deduped_count, batch_size=len(batch))

            groups = filter_stale_and_group(survivors, self._is_stale)

            for (doc_id, locale), group_items in groups.items():
                try:
                    grammar_obs("queue_execute_batch", doc_id=doc_id, locale=locale, item_count=len(group_items))
                    run_llm_and_cache_batch(group_items, grammar_queue=self)
                except Exception as e:
                    log.error("[grammar] queue worker batch failed doc=%s loc=%s: %s", doc_id, locale, e, exc_info=True)


_grammar_queue_singleton = GrammarWorkQueue()

grammar_queue: GrammarWorkQueue = _grammar_queue_singleton

_lang_detect_cache = grammar_persistence.grammar_registry.lang_detect_cache

# Test / legacy aliases (TD2 seam tests patch these names on grammar_work_queue).
_detect_languages = detect_languages_for_chunk

_call_grammar_llm = call_grammar_llm
