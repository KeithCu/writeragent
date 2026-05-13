# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar work queue: work items, batch dedup, pure enqueue/stale helpers, sequential LLM worker."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .grammar_fsm_state import GrammarEvent

from .grammar_proofread_cache import cache_get_sentence, cache_put_sentence, ignored_rules_snapshot
from .grammar_proofread_locale import (
    GRAMMAR_BATCH_MAX_SENTENCES,
    GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE,
    GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS,
    GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS,
    GRAMMAR_SYSTEM_PROMPT_TEMPLATE,
    GRAMMAR_WORKER_PAUSE_TIMEOUT_S,
    LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT,
    LANGUAGE_DETECT_SYSTEM_PROMPT,
    looks_complete_sentence,
    parse_grammar_batch_json,
    parse_grammar_json,
    parse_language_detect_batch_json,
    parse_language_detect_json,
    normalize_uno_locale_to_bcp47,
)
from .grammar_proofread_text import grammar_inflight_key, normalize_errors_for_text, split_into_sentences
from .grammar_persistence import _find_model_by_runtime_uid
log = logging.getLogger("writeragent.grammar")

_doc_locales_cache: dict[str, tuple[float, list[str]]] = {}
_lang_detect_cache: OrderedDict[str, str] = OrderedDict()

def _get_cached_language(text: str) -> str | None:
    if text in _lang_detect_cache:
        _lang_detect_cache.move_to_end(text)
        return _lang_detect_cache[text]
    return None

def _put_cached_language(text: str, lang: str) -> None:
    _lang_detect_cache[text] = lang
    if len(_lang_detect_cache) > 1000:
        _lang_detect_cache.popitem(last=False)

def _get_cached_document_locales(ctx: Any, doc_id: str) -> list[str]:
    now = time.time()
    cached = _doc_locales_cache.get(doc_id)
    if cached is not None and now - cached[0] < 60:
        return cached[1]

    def _query_locales() -> list[str]:
        locales = set()
        try:
            smgr = getattr(ctx, "ServiceManager", getattr(ctx, "getServiceManager", lambda: None)())
            if smgr:
                lingu_props = smgr.createInstanceWithContext("com.sun.star.linguistic2.LinguProperties", ctx)
                if lingu_props:
                    def_loc = getattr(lingu_props, "DefaultLocale", None)
                    bcp = normalize_uno_locale_to_bcp47(def_loc)
                    if bcp:
                        locales.add(bcp)
        except Exception as e:
            log.warning("Failed to get LinguProperties: %s", e)

        try:
            model = _find_model_by_runtime_uid(ctx, doc_id)
            if model:
                log.debug("[grammar] Document locale detection starting")
                
                # 1. Styles
                if hasattr(model, "getStyleFamilies"):
                    families = model.getStyleFamilies()
                    for family_name in ("ParagraphStyles", "CharacterStyles"):
                        if families.hasByName(family_name):
                            family = families.getByName(family_name)
                            for style_name in family.getElementNames():
                                try:
                                    style = family.getByName(style_name)
                                    loc = getattr(style, "CharLocale", None)
                                    bcp = normalize_uno_locale_to_bcp47(loc)
                                    if bcp:
                                        locales.add(bcp)
                                except Exception:
                                    pass

                # 2. First 50 paragraphs text portions (captures direct formatting)
                if hasattr(model, "getText"):
                    enum = model.getText().createEnumeration()
                    para_count = 0
                    while enum.hasMoreElements() and para_count < 50:
                        para = enum.nextElement()
                        para_count += 1
                        if hasattr(para, "createEnumeration"):
                            portion_enum = para.createEnumeration()
                            while portion_enum.hasMoreElements():
                                portion = portion_enum.nextElement()
                                loc = getattr(portion, "CharLocale", None)
                                bcp = normalize_uno_locale_to_bcp47(loc)
                                if bcp:
                                    locales.add(bcp)
                
                # 3. 1000 characters around the view cursor
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
                            bcp = normalize_uno_locale_to_bcp47(loc)
                            if bcp:
                                locales.add(bcp)
                            tc.collapseToEnd()
                    except Exception as e:
                        log.debug("[grammar] Failed to scan near cursor for locales: %s", e)
                        
                log.debug("[grammar] Document locale detection finished. Found: %s", locales)
        except Exception as e:
            log.warning("Failed to query document styles/text for locales: %s", e)

        if not locales:
            locales.add("en-US")
        return sorted(list(locales))

    from plugin.framework.queue_executor import execute_on_main_thread
    try:
        locs = execute_on_main_thread(_query_locales)
        _doc_locales_cache[doc_id] = (now, locs)
        return locs
    except Exception as e:
        log.warning("Failed to get cached locales: %s", e)
        return ["en-US"]

def _apply_language_change(ctx: Any, doc_id: str, sentence_text: str, detected_bcp47: str) -> None:
    def _do_update() -> None:
        model = _find_model_by_runtime_uid(ctx, doc_id)
        if not model:
            return
        
        parts = detected_bcp47.split("-")
        lang = parts[0]
        country = parts[1] if len(parts) > 1 else ""
        
        from com.sun.star.lang import Locale
        new_locale = Locale(Language=lang, Country=country)
        
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
            
    from plugin.framework.queue_executor import execute_on_main_thread
    try:
        execute_on_main_thread(_do_update)
    except Exception as e:
        log.warning("Failed to update language property: %s", e)


@dataclass(frozen=True)
class GrammarWorkItem:
    """One queued grammar job (defined here so dedup tests avoid UNO imports)."""

    ctx: Any
    full_text: str
    n_start: int
    n_end: int
    grammar_bcp47: str
    partial_sentence: bool
    doc_id: str
    inflight_key: str
    enqueue_seq: int
    # Main-thread sentence text from doProofreading; when set, worker skips split_into_sentences
    # on the slice so substring BreakIterator cannot disagree with cache keys (see _run_llm_and_cache).
    proofread_sentence_text: str = ""


def deduplicate_grammar_batch(batch: list[GrammarWorkItem]) -> list[GrammarWorkItem]:
    """Return one queue item per ``inflight_key``, keeping the highest ``enqueue_seq``."""
    # --- Cross-sentence prefix bug (fixed): older code had a *second* pass that grouped
    # by (doc_id, locale) and dropped slice A if slice B was a string-prefix extension
    # of A (newest enqueue_seq wins). That wrongly dropped sentence 1 when sentence 2's
    # text started with sentence 1's text (e.g. "No." vs "No problem today.") — different
    # inflight_key values, unrelated timelines. One sentence while typing = one key.
    #
    # Do not add cross-key slice-text prefix logic here; tail-replace + this loop suffice.
    #
    # Alternatives if you redesign: (1) prefix-newest-wins restricted to *same*
    # inflight_key only — usually redundant after this map; (2) span-aware dedup using
    # overlapping [n_start,n_end); (3) keep distinct-key slices independent (current).
    # Regression: test_two_sentences_string_prefix_collision_both_survive.
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        # Same physical sentence / typing line: inflight_key matches → keep newer snapshot only.
        if prev is None or item.enqueue_seq > prev.enqueue_seq:
            best_by_key[item.inflight_key] = item
        elif prev is not None and item.enqueue_seq < prev.enqueue_seq:
            log.info("[grammar] queue dedup: dropped older same-key item seq=%s key=%s (newer seq=%s kept)", item.enqueue_seq, item.inflight_key, prev.enqueue_seq)
    return list(best_by_key.values())


TailEnqueueOp = Literal["replace_tail", "append", "skip_tail"]


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


def tail_enqueue_operation(tail: GrammarWorkItem | None, incoming: GrammarWorkItem) -> TailEnqueueOp:
    """O(1) tail decision: replace newest same-key, append different key, or skip stale same-key."""
    if tail is None:
        return "append"
    if tail.inflight_key != incoming.inflight_key:
        return "append"
    if incoming.enqueue_seq > tail.enqueue_seq:
        return "replace_tail"
    return "skip_tail"


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


def _grammar_text_preview(text: str) -> str:
    words = text.strip().split()
    return " ".join(words[:3]) if words else "(empty)"


def emit_grammar_status(phase: str, text: str, *, result: str = "", elapsed_ms: int | None = None) -> None:
    try:
        from plugin.framework.event_bus import global_event_bus

        global_event_bus.emit("grammar:status", phase=phase, preview=_grammar_text_preview(text), length=len(text), result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


def run_llm_and_cache(
    ctx: Any,
    full_text: str,
    n_start: int,
    n_end: int,
    enqueue_seq: int,
    inflight_key: str,
    grammar_bcp47: str,
    partial_sentence: bool = False,
    *,
    doc_id: str = "",
    proofread_sentence_text: str = "",
    grammar_queue: Any | None = None,
    original_bcp47: str = "",
) -> None:
    """Process one queue item: LLM request(s) + sentence cache write(s)."""
    item = GrammarWorkItem(
        ctx=ctx,
        full_text=full_text,
        n_start=n_start,
        n_end=n_end,
        grammar_bcp47=grammar_bcp47,
        partial_sentence=partial_sentence,
        doc_id=doc_id,
        inflight_key=inflight_key,
        enqueue_seq=enqueue_seq,
        proofread_sentence_text=proofread_sentence_text,
    )
    run_llm_and_cache_batch([item], grammar_queue=grammar_queue, original_bcp47=original_bcp47)


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
) -> GrammarEvent | None:
    """Handle a single FSM effect by performing I/O or updating state."""
    from plugin.framework.queue_executor import llm_request_lane
    from .grammar_proofread_locale import grammar_english_name_for_bcp47
    from .grammar_fsm_state import (
        ExecuteLanguageDetectEffect, ExecuteGrammarCheckEffect,
        ApplyLanguageChangeEffect, RequeueIndividualItemEffect,
        ProcessGrammarResultsEffect, EmitStatusEffect, LogEffect,
        EventKind, GrammarEvent
    )
    
    try:
        if isinstance(effect, ExecuteLanguageDetectEffect):
            detected_langs = []
            all_cached = True
            for _item, text in effect.chunk:
                cached = _get_cached_language(text)
                detected_langs.append(cached)
                if not cached:
                    all_cached = False
                    
            if not all_cached:
                if len(effect.chunk) > 1:
                    user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(effect.chunk))
                    detect_prompt = LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT.format(detect_lang_instruction=detect_lang_instruction)
                    detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": user_content}]
                    
                    emit_grammar_status("request", f"Batch of {len(effect.chunk)}", result="Detecting language")
                    with llm_request_lane():
                        detect_content = client.chat_completion_sync(detect_messages, max_tokens=100 * len(effect.chunk), model=model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
                    
                    parsed_langs = parse_language_detect_batch_json(detect_content or "")
                    if len(parsed_langs) == len(effect.chunk):
                        for idx, d_lang in enumerate(parsed_langs):
                            if d_lang:
                                _put_cached_language(effect.chunk[idx][1], d_lang)
                                detected_langs[idx] = d_lang
                else:
                    text = effect.chunk[0][1]
                    detect_prompt = LANGUAGE_DETECT_SYSTEM_PROMPT.format(detect_lang_instruction=detect_lang_instruction)
                    detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": text}]
                    
                    emit_grammar_status("request", text, result="Detecting language")
                    with llm_request_lane():
                        detect_content = client.chat_completion_sync(detect_messages, max_tokens=50, model=model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
                    
                    parsed_lang = parse_language_detect_json(detect_content or "")
                    if parsed_lang:
                        _put_cached_language(text, parsed_lang)
                        detected_langs[0] = parsed_lang
                        
            return GrammarEvent(EventKind.LANG_DETECT_DONE, data={"detected_langs": detected_langs})
            
        elif isinstance(effect, ExecuteGrammarCheckEffect):
            _lang = grammar_english_name_for_bcp47(effect.bcp47)
            if len(effect.chunk) > 1:
                user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(effect.chunk))
                sys_prompt = GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE.format(lang_name=_lang, bcp47=effect.bcp47)
                any_partial = any(item.partial_sentence or not looks_complete_sentence(text) for item, text in effect.chunk)
                if any_partial:
                    sys_prompt += " The input may contain partial sentences; prefer conservative grammar suggestions and avoid broad rewrites."
                
                messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]
                
                grammar_obs("worker_llm_batch_request", item_count=len(effect.chunk), total_len=len(user_content))
                emit_grammar_status("request", f"Batch of {len(effect.chunk)}", result="LLM batch request")
                
                request_start = time.monotonic()
                with llm_request_lane():
                    content = client.chat_completion_sync(messages, max_tokens=max_tok * GRAMMAR_BATCH_MAX_SENTENCES, model=model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
                elapsed_ms = int((time.monotonic() - request_start) * 1000)
                
                batch_results = parse_grammar_batch_json(content or "")
                return GrammarEvent(EventKind.GRAMMAR_CHECK_DONE, data={"results": batch_results, "elapsed_ms": elapsed_ms})
            else:
                item, text = effect.chunk[0]
                use_partial = item.partial_sentence or not looks_complete_sentence(text)
                sys_prompt = GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(lang_name=_lang, bcp47=effect.bcp47)
                if use_partial:
                    sys_prompt += " The input may be a partial sentence; prefer conservative grammar suggestions and avoid broad rewrites."
                
                grammar_obs("worker_llm_request_prepare", enqueue_seq=item.enqueue_seq, llm_text_len=len(text), llm_preview=slice_preview_debug(text, 96))
                messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]
                
                request_start = time.monotonic()
                emit_grammar_status("request", text, result="LLM request")
                with llm_request_lane():
                    content = client.chat_completion_sync(messages, max_tokens=max_tok, model=model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
                elapsed_ms = int((time.monotonic() - request_start) * 1000)
                
                sent_results = parse_grammar_json(content or "")
                return GrammarEvent(EventKind.GRAMMAR_CHECK_DONE, data={"results": [sent_results] if content else [], "elapsed_ms": elapsed_ms})
                
        elif isinstance(effect, ApplyLanguageChangeEffect):
            _apply_language_change(ctx, effect.doc_id, effect.sentence_text, effect.new_bcp47)
            
        elif isinstance(effect, RequeueIndividualItemEffect):
            sent_complete = (not effect.item.partial_sentence) and looks_complete_sentence(effect.text)
            requeue_inflight_key = grammar_inflight_key(effect.item.doc_id, effect.new_bcp47, effect.text, sent_complete)
            
            # Cache break put to stop loop
            cache_put_sentence(effect.original_bcp47, effect.text, [], ctx=ctx, doc_id=effect.item.doc_id)
            
            if gq:
                new_item = replace(
                    effect.item,
                    grammar_bcp47=effect.new_bcp47,
                    enqueue_seq=next_enqueue_seq(),
                    inflight_key=requeue_inflight_key,
                    proofread_sentence_text=effect.text
                )
                gq.enqueue(new_item)
                
        elif isinstance(effect, ProcessGrammarResultsEffect):
            ignored = ignored_rules_snapshot()
            for idx, (item, text) in enumerate(effect.chunk):
                if gq and gq.inflight_superseded(item.inflight_key, item.enqueue_seq):
                    continue
                if idx < len(effect.results):
                    errors = effect.results[idx]
                    norm_errors = normalize_errors_for_text(text, 0, len(text), errors, ignored, ctx, effect.bcp47)
                    cache_put_sentence(effect.bcp47, text, [asdict(e) for e in norm_errors], ctx=ctx, doc_id=item.doc_id)
                    if effect.original_bcp47 and effect.original_bcp47 != effect.bcp47:
                        log.debug("[grammar] Double caching for %s (detected %s)", effect.original_bcp47, effect.bcp47)
                        cache_put_sentence(effect.original_bcp47, text, [asdict(e) for e in norm_errors], ctx=ctx, doc_id=item.doc_id)
                    else:
                        log.debug("[grammar] No double caching: original=%s, detected=%s", effect.original_bcp47, effect.bcp47)
                    
            emit_grammar_status("done", f"Batch of {len(effect.chunk)}", result="Success", elapsed_ms=effect.elapsed_ms)
            
        elif isinstance(effect, EmitStatusEffect):
            emit_grammar_status(effect.phase, effect.text, result=effect.result, elapsed_ms=effect.elapsed_ms)
            
        elif isinstance(effect, LogEffect):
            getattr(log, effect.level.lower())(effect.message, *effect.args)

    except Exception as e:
        log.error("[grammar] Worker logic error: %s", e, exc_info=True)
        return GrammarEvent(EventKind.ERROR, data={"error": str(e)})

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
    from .grammar_fsm_state import EventKind, GrammarEvent
    state = initial_state
    tr = next_state_fn(state, GrammarEvent(EventKind.START))
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
        original_bcp47 = grammar_bcp47

    try:
        from plugin.framework.config import (
            get_api_config,
            get_grammar_model,
            is_grammar_enabled,
            get_config_bool_safe,
            get_config_int_safe,
        )
        from plugin.framework.queue_executor import is_agent_active, llm_request_lane
        from plugin.framework.client.llm_client import LlmClient
        from .grammar_fsm_state import (
            LanguageValidationState, GrammarCheckState,
            next_language_state, next_grammar_state,
        )
        if not is_grammar_enabled(ctx):
            grammar_obs("worker_batch_skip", reason="grammar_disabled", item_count=len(items))
            return

        pause_during_agent = get_config_bool_safe(ctx, "doc.grammar_proofreader_pause_during_agent")

        if pause_during_agent and is_agent_active():
            grammar_obs("worker_batch_skip", reason="pause_during_agent", item_count=len(items))
            return

        # 1. Resolve actual sentences to process for each item (filtering hits/superseded)
        valid_items: list[tuple[GrammarWorkItem, str]] = []
        for item in items:
            if gq_to_use.inflight_superseded(item.inflight_key, item.enqueue_seq):
                grammar_obs("worker_skip", reason="superseded_before_process", enqueue_seq=item.enqueue_seq, inflight_key=item.inflight_key)
                continue

            # Resolve text for this item
            if item.proofread_sentence_text:
                to_process = [item.proofread_sentence_text]
            else:
                slice_txt = item.full_text[item.n_start : item.n_end]
                sentences = split_into_sentences(ctx, grammar_bcp47, slice_txt)
                if not sentences:
                    to_process = [slice_txt]
                else:
                    to_process = [txt for _off, txt in sentences]

            # Only keep uncached ones
            for sent_text in to_process:
                if cache_get_sentence(grammar_bcp47, sent_text, ctx=ctx, doc_id=item.doc_id) is None:
                    valid_items.append((item, sent_text))

        if not valid_items:
            grammar_obs("worker_batch_skip", reason="all_cached_or_superseded", item_count=len(items))
            return

        # 2. Config & Preparation
        max_tok = GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
        max_chars = GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS
        
        try:
            model = get_grammar_model(ctx)
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""

        client = LlmClient(get_api_config(ctx), ctx)

        batch_size = get_config_int_safe(ctx, "doc.grammar_proofreader_batch_sentences", 1)
        batch_size = max(1, min(GRAMMAR_BATCH_MAX_SENTENCES, batch_size))

        detect_lang_enabled = get_config_bool_safe(ctx, "doc.grammar_proofreader_detect_language")
        detect_lang_instruction = ""
        
        if detect_lang_enabled:
            filtered_items = []
            for item, text in valid_items:
                if item.partial_sentence or not looks_complete_sentence(text):
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
                initial_lang_state = LanguageValidationState(
                    chunk=current_chunk,
                    target_bcp47=grammar_bcp47,
                    instruction=detect_lang_instruction
                )
                lang_state = _run_fsm_stage(
                    initial_lang_state,
                    next_language_state,
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
                        new_key = grammar_inflight_key(item.doc_id, current_bcp47, text, not item.partial_sentence)
                        new_item = replace(item, grammar_bcp47=current_bcp47, inflight_key=new_key)
                        updated_chunk.append((new_item, text))
                    current_chunk = updated_chunk
            
            initial_grammar_state = GrammarCheckState(
                chunk=current_chunk,
                bcp47=current_bcp47,
                original_bcp47=grammar_bcp47
            )
            _run_fsm_stage(
                initial_grammar_state,
                next_grammar_state,
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
    """Single-worker sequential queue for grammar LLM requests (stampede + per-key supersede)."""

    def __init__(self) -> None:
        self._q: queue.Queue[GrammarWorkItem | None] = queue.Queue()
        self._seq_lock = threading.Lock()
        self._latest_seq: dict[str, int] = {}
        self._worker_started = False
        self._worker_lock = threading.Lock()

    @staticmethod
    def _slice_preview(item: GrammarWorkItem, max_len: int = 48) -> str:
        slice_txt = item.full_text[item.n_start : item.n_end]
        compact = " ".join(slice_txt.split())
        if len(compact) <= max_len:
            return compact
        return f"{compact[:max_len]}…"

    def _latest_seq_for(self, inflight_key: str) -> int | None:
        with self._seq_lock:
            return self._latest_seq.get(inflight_key)

    def _is_stale(self, item: GrammarWorkItem) -> bool:
        with self._seq_lock:
            return is_stale(self._latest_seq, item)

    def inflight_superseded(self, inflight_key: str, enqueue_seq: int) -> bool:
        """True if a newer grammar enqueue has been recorded for this key (e.g. user kept typing)."""
        with self._seq_lock:
            return inflight_superseded(self._latest_seq, inflight_key, enqueue_seq)

    def enqueue(self, item: GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call."""
        with self._seq_lock:
            self._latest_seq, out_of_order, superseded_prev_seq = record_enqueue_latest(self._latest_seq, item)
            if out_of_order:
                log.error("[grammar] queue enqueue: out-of-order seq detected for key=%s: incoming seq=%s < latest seq=%s; stale detection may be unreliable", item.inflight_key, item.enqueue_seq, superseded_prev_seq)
        grammar_obs("queue_enqueue", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key, n_start=item.n_start, n_end=item.n_end, slice_len=len(item.full_text[item.n_start : item.n_end]), partial_sentence=item.partial_sentence, preview=slice_preview_debug(item.full_text[item.n_start : item.n_end]))  # fmt: skip

        with self._q.mutex:
            # Note on same-key bursts: tail-replace only collapses items that are
            # still in the queue. During a typing burst the drain worker pulls
            # items into its batch within microseconds, so the queue is usually
            # empty when the next enqueue arrives and same-key items still
            # accumulate. Final collapse happens in ``_drain_loop`` via
            # ``deduplicate_grammar_batch``.
            tail = self._q.queue[-1] if self._q.queue else None
            op = tail_enqueue_operation(tail, item)
            if op == "replace_tail":
                assert tail is not None
                grammar_obs("queue_replace_tail", inflight_key=item.inflight_key, new_seq=item.enqueue_seq, old_seq=tail.enqueue_seq)
                self._q.queue[-1] = item
            elif op == "append":
                self._q.queue.append(item)
                self._q.unfinished_tasks += 1
                self._q.not_empty.notify()
            else:
                grammar_obs("queue_skip_stale_tail", inflight_key=item.inflight_key, incoming_seq=item.enqueue_seq, existing_seq=tail.enqueue_seq if tail else None)

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
            # Collapse same-key items as they arrive instead of appending all of
            # them to a list and dedup-ing at the end. During typing bursts the
            # worker pulls items in microseconds, so the queue is empty between
            # keystrokes and ``enqueue``'s tail-replace path can't help — without
            # this dict the batch routinely held 20+ identical INCOMPLETE keys.
            batch_by_key: dict[str, GrammarWorkItem] = {first.inflight_key: first}
            while True:
                try:
                    more = self._q.get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)
                    if more is None:
                        return
                    prev = batch_by_key.get(more.inflight_key)
                    if prev is None or more.enqueue_seq > prev.enqueue_seq:
                        batch_by_key[more.inflight_key] = more
                except queue.Empty:
                    break
            batch = list(batch_by_key.values())
            grammar_obs("queue_drain_batch", batch_size=len(batch), seqs=tuple(x.enqueue_seq for x in batch), keys=tuple(x.inflight_key for x in batch))
            survivors = deduplicate_grammar_batch(batch)
            grammar_obs("queue_drain_survivors", survivor_count=len(survivors), seqs=tuple(x.enqueue_seq for x in survivors))

            from collections import defaultdict
            groups: dict[tuple[str, str], list[GrammarWorkItem]] = defaultdict(list)
            for item in survivors:
                if self._is_stale(item):
                    grammar_obs("queue_stale_skip", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key)
                    continue
                groups[(item.doc_id, item.grammar_bcp47)].append(item)

            for (doc_id, locale), group_items in groups.items():
                try:
                    grammar_obs("queue_execute_batch", doc_id=doc_id, locale=locale, item_count=len(group_items))
                    run_llm_and_cache_batch(group_items, grammar_queue=self)
                except Exception as e:
                    log.error("[grammar] queue worker batch failed doc=%s loc=%s: %s", doc_id, locale, e, exc_info=True)


_grammar_queue_singleton = GrammarWorkQueue()

grammar_queue: GrammarWorkQueue = _grammar_queue_singleton
