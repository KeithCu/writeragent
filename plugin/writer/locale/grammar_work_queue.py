# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar work queue: work items, batch dedup, pure enqueue/stale helpers, sequential LLM worker.

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
import collections
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


from . import (
    grammar_proofread_cache,
    grammar_proofread_locale,
    grammar_proofread_text,
    grammar_persistence,
    grammar_fsm_state,
    grammar_proofread_json,
)

from plugin.framework import queue_executor, event_bus, config
from plugin.framework.client import model_fetcher, llm_client

import uno

log = logging.getLogger("writeragent.grammar")

_doc_locales_cache: dict[str, tuple[float, list[str]]] = {}
_lang_detect_cache: collections.OrderedDict[str, str] = collections.OrderedDict()

def _get_cached_language(text: str) -> str | None:
    if text in _lang_detect_cache:
        _lang_detect_cache.move_to_end(text)
        return _lang_detect_cache[text]
    return None

def _put_cached_language(text: str, lang: str) -> None:
    _lang_detect_cache[text] = lang
    if len(_lang_detect_cache) > 1000:
        _lang_detect_cache.popitem(last=False)

# Super simple locale detection: 1k chars around cursor should be "good enough" for most cases.
# Historical overkill (styles, first 50 paragraphs, LinguProperties) was removed to prioritize
# speed and local relevance. If precision drops, consider re-adding style-based detection
# or a smarter text portion enumeration within the visible range.
def _get_cached_document_locales(ctx: Any, doc_id: str) -> list[str]:
    now = time.time()
    cached = _doc_locales_cache.get(doc_id)
    if cached is not None and now - cached[0] < 60:
        return cached[1]

    def _query_locales() -> list[str]:
        locales = set()
        try:
            model = grammar_persistence._find_model_by_runtime_uid(ctx, doc_id)
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
        _doc_locales_cache[doc_id] = (now, locs)
        return locs
    except Exception as e:
        log.warning("Failed to get cached locales: %s", e)
        return ["en-US"]

def _apply_language_change(ctx: Any, doc_id: str, sentence_text: str, detected_bcp47: str) -> None:
    def _do_update() -> None:
        model = grammar_persistence._find_model_by_runtime_uid(ctx, doc_id)
        if not model:
            return
        
        parts = detected_bcp47.split("-")
        lang = parts[0]
        country = parts[1] if len(parts) > 1 else ""
        
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
class GrammarEffectContext:
    """Context for handling grammar FSM effects, wrapping I/O and document dependencies."""
    ctx: Any
    client: Any
    gq: GrammarWorkQueue | None
    model: str
    original_bcp47: str
    grammar_bcp47: str
    max_tok: int
    detect_lang_instruction: str = ""


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
    for item in survivors:
        if is_stale_fn(item):
            grammar_obs("queue_stale_skip", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key)
            continue
        groups[(item.doc_id, item.grammar_bcp47)].append(item)
    return dict(groups)


_ENQUEUE_SEQ_LOCK = threading.Lock()
_ENQUEUE_SEQ = 0


def next_enqueue_seq() -> int:
    """Monotonic generation stamp for ``GrammarWorkItem.enqueue_seq`` (supersede / stale detection)."""
    global _ENQUEUE_SEQ
    with _ENQUEUE_SEQ_LOCK:
        _ENQUEUE_SEQ += 1
        return _ENQUEUE_SEQ


def grammar_obs(event: str, **fields: Any) -> None:
    """DEBUG-only observability for queue / worker (grep ``[grammar] obs`` in logs)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.debug("[grammar] obs %s %s", event, kv)


def slice_preview_debug(text: str, max_len: int = 72) -> str:
    """Compact one-line preview for DEBUG logs (avoid dumping huge paragraphs)."""
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}…"


def emit_grammar_status(
    phase: str,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
    preview_source: str | None = None,
    length_hint: int | None = None,
) -> None:
    """Emit ``grammar:status``. Pass ``preview_source`` for a sentence snippet (sidebar, clipped to a few chars)."""
    try:
        if preview_source is not None:
            raw = preview_source.strip() or "(empty)"
            preview = slice_preview_debug(raw, 10)
            length = len(raw) if length_hint is None else length_hint
        else:
            preview = slice_preview_debug(text.strip() or "(empty)", 10)
            length = len(text)
        event_bus.global_event_bus.emit("grammar:status", phase=phase, preview=preview, length=length, result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


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


def _persisted_grammar_skip_lang_detect(ctx: Any, doc_id: str, text: str) -> bool:
    """True if persistence already stores grammar for this sentence (fingerprint).

    Heuristic to skip redundant language-detect LLM on reopen: any stored row (including
    empty errors for 'good' sentences) implies prior proofreading — good enough to treat
    as language-resolved for this session. Wrong-locale clean rows could skip redetect.
    """
    try:
        if not doc_id:
            return False
        fp = grammar_proofread_cache.sentence_identity_fp(text)
        p = grammar_persistence.get_persistence(ctx, doc_id)
        return p is not None and p.get(fp) is not None
    except Exception as e:
        log.debug("[grammar] persisted grammar heuristic lookup failed: %s", e, exc_info=True)
        return False


def _handle_lang_detect_effect(effect: Any, ec: GrammarEffectContext) -> grammar_fsm_state.GrammarEvent | None:
    """Handle language detection, including caching and LLM requests."""

    detected_langs: list[str | None] = []
    all_cached = True
    for item, text in effect.chunk:
        cached = _get_cached_language(text)
        if cached:
            detected_langs.append(cached)
        elif _persisted_grammar_skip_lang_detect(ec.ctx, item.doc_id, text):
            grammar_obs("lang_detect_skip", reason="persisted_grammar_heuristic", doc_id=item.doc_id[:32] if item.doc_id else "")
            _put_cached_language(text, ec.grammar_bcp47)
            detected_langs.append(ec.grammar_bcp47)
        else:
            detected_langs.append(None)
            all_cached = False

    if not all_cached:
        if len(effect.chunk) > 1:
            user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(effect.chunk))
            detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT.format(detect_lang_instruction=ec.detect_lang_instruction)
            detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": user_content}]

            emit_grammar_status("request", f"Batch of {len(effect.chunk)}", result="Detecting language")
            with queue_executor.llm_request_lane():
                detect_content = ec.client.chat_completion_sync(detect_messages, max_tokens=100 * len(effect.chunk), model=ec.model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)

            parsed_langs = grammar_proofread_json.parse_language_detect_batch_json(detect_content or "")
            if len(parsed_langs) == len(effect.chunk):
                for idx, d_lang in enumerate(parsed_langs):
                    if d_lang:
                        _put_cached_language(effect.chunk[idx][1], d_lang)
                        detected_langs[idx] = d_lang
        else:
            text = effect.chunk[0][1]
            detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_SYSTEM_PROMPT.format(detect_lang_instruction=ec.detect_lang_instruction)
            detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": text}]

            emit_grammar_status("request", text, result="Detecting language")
            with queue_executor.llm_request_lane():
                detect_content = ec.client.chat_completion_sync(detect_messages, max_tokens=50, model=ec.model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)

            parsed_lang = grammar_proofread_json.parse_language_detect_json(detect_content or "")
            if parsed_lang:
                _put_cached_language(text, parsed_lang)
                detected_langs[0] = parsed_lang

    return grammar_fsm_state.GrammarEvent(grammar_fsm_state.EventKind.LANG_DETECT_DONE, data={"detected_langs": detected_langs})


def _handle_grammar_check_effect(effect: Any, ec: GrammarEffectContext) -> grammar_fsm_state.GrammarEvent | None:
    """Handle grammar check, including batching and partial sentence handling."""

    lang_name = grammar_proofread_locale.grammar_english_name_for_bcp47(effect.bcp47)
    if len(effect.chunk) > 1:
        first_item, _ = effect.chunk[0]
        user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(effect.chunk))
        sys_prompt = grammar_proofread_locale.GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE.format(lang_name=lang_name, bcp47=effect.bcp47)
        any_partial = any(item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text) for item, text in effect.chunk)
        if any_partial:
            sys_prompt += " The input may contain partial sentences; prefer conservative grammar suggestions and avoid broad rewrites."

        from .grammar_persistence import get_persistence
        from .grammar_proofread_cache import normalize_reason, ignored_rules_snapshot
        p = get_persistence(ec.ctx, first_item.doc_id)
        ignored_reasons = set(p._ignored_rules) if p else set()
        for r in ignored_rules_snapshot():
            if r.startswith("wa_g_rule||"):
                ignored_reasons.add(normalize_reason(r[11:]))
            else:
                ignored_reasons.add(normalize_reason(r))
        if ignored_reasons:
            sys_prompt += "\n\nIMPORTANT: The user has explicitly chosen to IGNORE the following rules/style issues in this document. DO NOT report any errors or suggestions that match or are highly similar to these:\n"
            for reason in sorted(ignored_reasons):
                sys_prompt += f"- {reason}\n"

        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]

        grammar_obs("worker_llm_batch_request", item_count=len(effect.chunk), total_len=len(user_content))
        emit_grammar_status("request", f"Batch of {len(effect.chunk)}", result="LLM batch request")

        request_start = time.monotonic()
        with queue_executor.llm_request_lane():
            content = ec.client.chat_completion_sync(messages, max_tokens=ec.max_tok * grammar_proofread_locale.GRAMMAR_BATCH_MAX_SENTENCES, model=ec.model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
        elapsed_ms = int((time.monotonic() - request_start) * 1000)

        batch_results = grammar_proofread_json.parse_grammar_batch_json(content or "")
        return grammar_fsm_state.GrammarEvent(grammar_fsm_state.EventKind.GRAMMAR_CHECK_DONE, data={"results": batch_results, "elapsed_ms": elapsed_ms})
    else:
        item, text = effect.chunk[0]
        use_partial = item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text)
        sys_prompt = grammar_proofread_locale.GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(lang_name=lang_name, bcp47=effect.bcp47)
        if use_partial:
            sys_prompt += " The input may be a partial sentence; prefer conservative grammar suggestions and avoid broad rewrites."

        from .grammar_persistence import get_persistence
        from .grammar_proofread_cache import normalize_reason, ignored_rules_snapshot
        p = get_persistence(ec.ctx, item.doc_id)
        ignored_reasons = set(p._ignored_rules) if p else set()
        for r in ignored_rules_snapshot():
            if r.startswith("wa_g_rule||"):
                ignored_reasons.add(normalize_reason(r[11:]))
            else:
                ignored_reasons.add(normalize_reason(r))
        if ignored_reasons:
            sys_prompt += "\n\nIMPORTANT: The user has explicitly chosen to IGNORE the following rules/style issues in this document. DO NOT report any errors or suggestions that match or are highly similar to these:\n"
            for reason in sorted(ignored_reasons):
                sys_prompt += f"- {reason}\n"

        grammar_obs("worker_llm_request_prepare", enqueue_seq=item.enqueue_seq, llm_text_len=len(text), llm_preview=slice_preview_debug(text, 96))
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]

        request_start = time.monotonic()
        emit_grammar_status("request", text, result="LLM request")
        with queue_executor.llm_request_lane():
            content = ec.client.chat_completion_sync(messages, max_tokens=ec.max_tok, model=ec.model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
        elapsed_ms = int((time.monotonic() - request_start) * 1000)

        sent_results = grammar_proofread_json.parse_grammar_json(content or "")
        return grammar_fsm_state.GrammarEvent(grammar_fsm_state.EventKind.GRAMMAR_CHECK_DONE, data={"results": [sent_results] if content else [], "elapsed_ms": elapsed_ms})


def _handle_apply_lang_change_effect(effect: Any, ec: GrammarEffectContext) -> None:
    """Update CharLocale in the document."""
    _apply_language_change(ec.ctx, effect.doc_id, effect.sentence_text, effect.new_bcp47)


def _handle_requeue_individual_item_effect(effect: Any, ec: GrammarEffectContext) -> None:
    """Handle requeueing items when a language change or mismatch is detected."""
    sent_complete = (not effect.item.partial_sentence) and grammar_proofread_locale.looks_complete_sentence(effect.text)
    requeue_inflight_key = grammar_proofread_text.grammar_inflight_key(effect.item.doc_id, effect.new_bcp47, effect.text, sent_complete)

    # Cache break put to stop loop
    grammar_proofread_cache.cache_put_sentence(effect.original_bcp47, effect.text, [], ctx=ec.ctx, doc_id=effect.item.doc_id)

    if ec.gq:
        new_item = replace(
            effect.item,
            grammar_bcp47=effect.new_bcp47,
            enqueue_seq=next_enqueue_seq(),
            inflight_key=requeue_inflight_key,
            text=effect.text,
            original_bcp47=effect.original_bcp47
        )
        ec.gq.enqueue(new_item)



def _handle_process_grammar_results_effect(effect: Any, ec: GrammarEffectContext) -> None:
    """Process results from the LLM, update cache, and emit status."""
    from .grammar_persistence import get_persistence

    total_issues = 0
    chars_checked = 0
    n_written = 0
    first_text = ""
    second_text = ""
    for idx, (item, text) in enumerate(effect.chunk):
        if ec.gq and ec.gq.inflight_superseded(item.inflight_key, item.enqueue_seq):
            continue
        if idx < len(effect.results):
            errors = effect.results[idx]
            from .grammar_proofread_cache import normalize_reason, ignored_rules_snapshot
            p = get_persistence(ec.ctx, item.doc_id)
            ignored = set(p._ignored_rules) if p else set()
            global_ignored = ignored_rules_snapshot()
            norm_errors = grammar_proofread_text.normalize_errors_for_text(text, 0, len(text), errors, ec.ctx, effect.bcp47)
            
            filtered_errors = []
            for e in norm_errors:
                rule_ident = e.rule_identifier
                if rule_ident.startswith("wa_g_rule||"):
                    reason = rule_ident[11:]
                    if normalize_reason(reason) in ignored or rule_ident in global_ignored:
                        continue
                elif rule_ident in ignored or rule_ident in global_ignored:
                    continue
                filtered_errors.append(e)

            grammar_proofread_cache.cache_put_sentence(effect.bcp47, text, [asdict(e) for e in filtered_errors], ctx=ec.ctx, doc_id=item.doc_id)
            if effect.original_bcp47 and effect.original_bcp47 != effect.bcp47:
                log.debug("[grammar] Double caching for %s (detected %s)", effect.original_bcp47, effect.bcp47)
                grammar_proofread_cache.cache_put_sentence(effect.original_bcp47, text, [asdict(e) for e in filtered_errors], ctx=ec.ctx, doc_id=item.doc_id)
            else:
                log.debug("[grammar] No double caching: original=%s, detected=%s", effect.original_bcp47, effect.bcp47)

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
        emit_grammar_status(
            "done",
            preview_src,
            result=f"{total_issues} {iw}, {n_written} {sw}",
            elapsed_ms=effect.elapsed_ms,
            preview_source=preview_src,
            length_hint=chars_checked,
        )
    else:
        emit_grammar_status("done", "batch", result="skipped (superseded)", elapsed_ms=effect.elapsed_ms)


def _handle_emit_status_effect(effect: Any) -> None:
    """Emit status events."""
    emit_grammar_status(effect.phase, effect.text, result=effect.result, elapsed_ms=effect.elapsed_ms)


def _handle_log_effect(effect: Any) -> None:
    """Handle logging."""
    getattr(log, effect.level.lower())(effect.message, *effect.args)


def _handle_grammar_effect(
    effect: Any,
    *,
    client: Any,
    ctx: Any,
    gq: GrammarWorkQueue | None,
    model: str,
    original_bcp47: str,
    grammar_bcp47: str,
    max_tok: int,
    detect_lang_instruction: str = "",
) -> grammar_fsm_state.GrammarEvent | None:
    """Handle a single FSM effect by performing I/O or updating state."""
    
    ec = GrammarEffectContext(
        ctx=ctx,
        client=client,
        gq=gq,
        model=model,
        original_bcp47=original_bcp47,
        grammar_bcp47=grammar_bcp47,
        max_tok=max_tok,
        detect_lang_instruction=detect_lang_instruction
    )

    try:
        if isinstance(effect, grammar_fsm_state.ExecuteLanguageDetectEffect):
            return _handle_lang_detect_effect(effect, ec)
            
        elif isinstance(effect, grammar_fsm_state.ExecuteGrammarCheckEffect):
            return _handle_grammar_check_effect(effect, ec)
                
        elif isinstance(effect, grammar_fsm_state.ApplyLanguageChangeEffect):
            _handle_apply_lang_change_effect(effect, ec)
            
        elif isinstance(effect, grammar_fsm_state.RequeueIndividualItemEffect):
            _handle_requeue_individual_item_effect(effect, ec)
                
        elif isinstance(effect, grammar_fsm_state.ProcessGrammarResultsEffect):
            _handle_process_grammar_results_effect(effect, ec)
            
        elif isinstance(effect, grammar_fsm_state.EmitStatusEffect):
            _handle_emit_status_effect(effect)
            
        elif isinstance(effect, grammar_fsm_state.LogEffect):
            _handle_log_effect(effect)

    except Exception as e:
        log.error("[grammar] Worker logic error: %s", e, exc_info=True)
        return grammar_fsm_state.GrammarEvent(grammar_fsm_state.EventKind.ERROR, data={"error": str(e)})

    return None


def _run_fsm_stage(
    initial_state: Any,
    next_state_fn: Any,
    *,
    client: Any,
    ctx: Any,
    gq: GrammarWorkQueue | None,
    model: str,
    original_bcp47: str,
    grammar_bcp47: str,
    max_tok: int,
    detect_lang_instruction: str = "",
) -> Any:
    """Run an FSM stage until it reaches a done state, handling effects."""
    state = initial_state
    tr = next_state_fn(state, grammar_fsm_state.GrammarEvent(grammar_fsm_state.EventKind.START))
    while True:
        state = tr.state
        event = None
        
        for effect in tr.effects:
            res_event = _handle_grammar_effect(
                effect,
                client=client,
                ctx=ctx,
                gq=gq,
                model=model,
                original_bcp47=original_bcp47,
                grammar_bcp47=grammar_bcp47,
                max_tok=max_tok,
                detect_lang_instruction=detect_lang_instruction
            )
            if res_event:
                event = res_event
            
        if state.is_done:
            return state

        if event:
            tr = next_state_fn(state, event)
        else:
            break
    return state


def run_llm_and_cache_batch(
    items: list[GrammarWorkItem],
    *,
    grammar_queue: Any | None = None,
    original_bcp47: str = "",
) -> None:
    """Process a batch of items (ideally from one paragraph): FSM-driven LLM requests + multi-sentence cache writes."""
    if not items:
        return

    # All items in a batch MUST share ctx and locale (grouped by _drain_loop)
    ctx = items[0].ctx
    grammar_bcp47 = items[0].grammar_bcp47
    gq_to_use = grammar_queue or _grammar_queue_singleton
    if not original_bcp47:
        original_bcp47 = items[0].original_bcp47 or grammar_bcp47


    try:
        if not config.is_grammar_enabled(ctx):
            grammar_obs("worker_batch_skip", reason="grammar_disabled", item_count=len(items))
            return

        pause_during_agent = config.get_config_bool_safe(ctx, "doc.grammar_proofreader_pause_during_agent")
        if pause_during_agent and queue_executor.is_agent_active():
            grammar_obs("worker_batch_skip", reason="pause_during_agent", item_count=len(items))
            return

        # 1. Resolve actual sentences to process for each item (filtering hits/superseded)
        valid_items: list[tuple[GrammarWorkItem, str]] = []
        for item in items:
            if gq_to_use.inflight_superseded(item.inflight_key, item.enqueue_seq):
                grammar_obs("worker_skip", reason="superseded_before_process", enqueue_seq=item.enqueue_seq, inflight_key=item.inflight_key)
                continue

            # Only keep uncached ones
            if grammar_proofread_cache.cache_get_sentence(grammar_bcp47, item.text, ctx=ctx, doc_id=item.doc_id) is None:
                valid_items.append((item, item.text))

        if not valid_items:
            grammar_obs("worker_batch_skip", reason="all_cached_or_superseded", item_count=len(items))
            return

        # 2. Config & Preparation
        max_tok = grammar_proofread_locale.GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
        max_chars = grammar_proofread_locale.GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS
        
        try:
            model = model_fetcher.get_grammar_model(ctx)
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""

        client = llm_client.LlmClient(config.get_api_config(ctx), ctx)

        batch_size = config.get_config_int_safe(ctx, "doc.grammar_proofreader_batch_sentences", 1)
        batch_size = max(1, min(grammar_proofread_locale.GRAMMAR_BATCH_MAX_SENTENCES, batch_size))

        detect_lang_enabled = config.get_config_bool_safe(ctx, "doc.grammar_proofreader_detect_language")
        detect_lang_instruction = ""
        
        if detect_lang_enabled:
            filtered_items = []
            for item, text in valid_items:
                if item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text):
                    continue
                filtered_items.append((item, text))
            valid_items = filtered_items
            if not valid_items:
                return
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

        # 3. Execution Pipeline
        for chunk in chunks:
            # Stage 1: Language Validation
            lang_state = None
            current_chunk = chunk
            if detect_lang_enabled:
                initial_lang_state = grammar_fsm_state.LanguageValidationState(
                    chunk=current_chunk,
                    target_bcp47=grammar_bcp47,
                    instruction=detect_lang_instruction
                )
                lang_state = _run_fsm_stage(
                    initial_lang_state,
                    grammar_fsm_state.next_language_state,
                    client=client,
                    ctx=ctx,
                    gq=gq_to_use,
                    model=model,
                    original_bcp47=original_bcp47,
                    grammar_bcp47=grammar_bcp47,
                    max_tok=max_tok,
                    detect_lang_instruction=detect_lang_instruction
                )
                if not lang_state or lang_state.status == "error":
                    continue
                current_chunk = lang_state.result_chunk
                
            if not current_chunk:
                continue

            # Stage 2: Grammar Check
            current_bcp47 = grammar_bcp47
            if lang_state:
                current_bcp47 = lang_state.target_bcp47
                if current_bcp47 != grammar_bcp47:
                    updated_chunk = []
                    for item, text in current_chunk:
                        new_key = grammar_proofread_text.grammar_inflight_key(item.doc_id, current_bcp47, text, not item.partial_sentence)
                        new_item = replace(item, grammar_bcp47=current_bcp47, inflight_key=new_key)
                        updated_chunk.append((new_item, text))
                    current_chunk = updated_chunk
            
            initial_grammar_state = grammar_fsm_state.GrammarCheckState(
                chunk=current_chunk,
                bcp47=current_bcp47,
                original_bcp47=grammar_bcp47
            )
            _run_fsm_stage(
                initial_grammar_state,
                grammar_fsm_state.next_grammar_state,
                client=client,
                ctx=ctx,
                gq=gq_to_use,
                model=model,
                original_bcp47=original_bcp47,
                grammar_bcp47=grammar_bcp47,
                max_tok=max_tok
            )

    except Exception as e:
        log.error("[grammar] worker batch failed: %s", e, exc_info=True)
        try:
            emit_grammar_status("failed", "Batch processing", result=type(e).__name__)
        except Exception:
            pass

class GrammarWorkQueue:
    """Single-worker sequential queue for grammar LLM requests (stampede + per-key supersede).

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
        self._worker_started = False
        self._worker_lock = threading.Lock()

    @staticmethod
    def _slice_preview(item: GrammarWorkItem, max_len: int = 48) -> str:
        compact = " ".join(item.text.split())
        if len(compact) <= max_len:
            return compact
        return f"{compact[:max_len]}\u2026"

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
        grammar_obs("queue_enqueue", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key, slice_len=len(item.text), partial_sentence=item.partial_sentence, preview=slice_preview_debug(item.text))  # fmt: skip

        # Normal append.  (Historical Layer 1 "tail-replace" under _q.mutex was
        # removed in the TD4 simplification pass because it was ineffective
        # during the common rapid-drain burst case; see the module docstring.)
        self._q.put(item)
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
            grammar_obs("queue_drain_survivors", survivor_count=len(survivors), seqs=tuple(x.enqueue_seq for x in survivors))

            groups = filter_stale_and_group(survivors, self._is_stale)

            for (doc_id, locale), group_items in groups.items():
                try:
                    grammar_obs("queue_execute_batch", doc_id=doc_id, locale=locale, item_count=len(group_items))
                    run_llm_and_cache_batch(group_items, grammar_queue=self)
                except Exception as e:
                    log.error("[grammar] queue worker batch failed doc=%s loc=%s: %s", doc_id, locale, e, exc_info=True)


_grammar_queue_singleton = GrammarWorkQueue()

grammar_queue: GrammarWorkQueue = _grammar_queue_singleton
