# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar provider dispatch: LLM, Harper, LanguageTool, Vale, plus language detect and cache write.

Concurrency: proofreading runs on grammar worker threads, not on the
sidebar chat client. This module constructs its **own** ``LlmClient`` so
it does not share chat’s keep-alive HTTP connection (stdlib http.client
is not thread-safe; Stop on chat must not close grammar’s socket). How
many grammar HTTP calls may run at once is
``grammar_llm_request_gate`` in ``llm_lanes`` (local models often
handle one request). Reading or writing the Writer document still goes
through the UI thread / ``guard_uno``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from plugin.writer.locale import grammar_settings
from . import grammar_proofread_cache, grammar_proofread_json, grammar_proofread_locale, grammar_persistence
from .grammar_obs import (
    get_grammar_status,
    grammar_obs,
    slice_preview_debug,
)
from plugin.framework.grammar_rule_ids import (
    bcp47_to_uno_lang_country,
    grammar_bcp47_tags_match,
    grammar_english_name_for_bcp47,
    normalize_detected_bcp47,
)
from .grammar_work_queue import (
    GrammarWorkItem,
    GrammarWorkQueue,
    grammar_inflight_key,
    next_enqueue_seq,
)

from plugin.framework import config, llm_lanes, send_cancellation
from plugin.framework.errors import best_effort

log = logging.getLogger("writeragent.grammar")

# Mercury and other reasoning models may return null content when effort is unconstrained.
_GRAMMAR_CHAT_EXTRA = {"reasoning": {"effort": "minimal"}}

_grammar_worker_tls = threading.local()


def reset_grammar_llm_client_cache() -> None:
    """Clear thread-local cached LlmClient instance (for tests and cleanup)."""
    cached = getattr(_grammar_worker_tls, "client", None)
    if cached is not None:
        best_effort(cached.stop, "grammar LLM client stop failed")
    _grammar_worker_tls.client = None
    _grammar_worker_tls.cache_key = None


def _get_or_create_thread_llm_client(api_cfg: dict[str, Any], ctx: Any) -> Any:
    """Return a thread-local LlmClient, reused across batches unless endpoint/key/timeout change or client stopped."""
    cache_key = (
        api_cfg.get("endpoint"),
        api_cfg.get("api_key"),
        api_cfg.get("request_timeout"),
    )
    cached_client = getattr(_grammar_worker_tls, "client", None)
    cached_key = getattr(_grammar_worker_tls, "cache_key", None)

    if (
        cached_client is not None
        and cached_key == cache_key
        and not cached_client.stopped()
    ):
        return cached_client

    if cached_client is not None:
        best_effort(cached_client.stop, "grammar LLM client stop failed")

    import importlib

    llm_client_mod = importlib.import_module("plugin.framework.client.llm_client")
    new_client = llm_client_mod.LlmClient(api_cfg, ctx, register_with_send=False)
    _grammar_worker_tls.client = new_client
    _grammar_worker_tls.cache_key = cache_key
    return new_client


@dataclass
class _BatchResultSummary:
    """Accumulator for batch proofreading results and status reporting."""

    total_issues: int = 0
    chars_checked: int = 0
    n_written: int = 0
    first_text: str = ""
    second_text: str = ""

    def record(self, text: str, n_errors: int) -> None:
        self.total_issues += n_errors
        self.chars_checked += len(text)
        self.n_written += 1
        tstrip = text.strip()
        if self.n_written == 1 and tstrip:
            self.first_text = tstrip
        elif self.n_written == 2 and tstrip:
            self.second_text = tstrip

    def preview_source(self) -> str:
        return f"{self.first_text} \u00b7 {self.second_text}" if self.second_text else self.first_text


def _item_cache_identity(item: GrammarWorkItem) -> str | None:
    """Resolve cache checker_identity for a work item without persisting bare 'llm'."""
    if item.checker_identity:
        return item.checker_identity
    if grammar_settings.is_local(item.provider):
        return item.provider
    return None


def cache_lookup_text(item: GrammarWorkItem) -> str:
    """Return cache lookup text: checked prefix for LLM, full text otherwise."""
    cache_ident = _item_cache_identity(item) or ""
    if cache_ident.startswith("llm:"):
        return grammar_proofread_locale.checked_grammar_text(
            item.text, grammar_proofread_locale.grammar_max_chars()
        )
    return item.text


def _item_is_complete(item: GrammarWorkItem, text: str) -> bool:
    """Completion for ``inflight_key``: item flag and text must agree."""
    return not item.partial_sentence and grammar_proofread_locale.looks_complete_sentence(text)


def process_grammar_results(
    chunk: list[tuple[Any, str]],
    results: list[Any],
    bcp47: str,
    original_bcp47: str,
    elapsed_ms: int,
    ec: Any,
) -> None:
    """Normalize errors, write sentence cache, emit done status."""
    from .grammar_ignore_rules import IgnoreSet

    summary = _BatchResultSummary()
    doc_id = chunk[0][0].doc_id
    ignore_set = IgnoreSet.for_document(ec.ctx, doc_id) if doc_id else None
    for (item, text), errors in zip(chunk, results):
        if ec.gq.inflight_superseded(item.inflight_key, item.enqueue_seq):
            continue
        checker_ident = _item_cache_identity(item)
        active_errors = grammar_proofread_cache.store_sentence_result(
            ec.ctx,
            item.doc_id,
            bcp47,
            text,
            errors,
            checker_ident,
            alt_locale=original_bcp47,
            ignore_set=ignore_set,
        )
        summary.record(text, len(active_errors))

    if summary.n_written:
        preview_src = summary.preview_source()
        iw = "issue" if summary.total_issues == 1 else "issues"
        sw = "sentence" if summary.n_written == 1 else "sentences"
        get_grammar_status().record_done_status(
            preview_src,
            result=f"{summary.total_issues} {iw}, {summary.n_written} {sw}",
            elapsed_ms=elapsed_ms,
            preview_source=preview_src,
            length_hint=summary.chars_checked,
        )
    else:
        get_grammar_status().record_done_status("batch", result="skipped (superseded)", elapsed_ms=elapsed_ms)


def run_single_sentence_provider(
    spec: _ProviderSpec,
    chunk: list[tuple[Any, str]],
    bcp47: str,
    original_bcp47: str,
    cfg_dir: str,
    ec: Any,
) -> None:
    """Execute a single-sentence local checker (LanguageTool, Vale), record metrics, and cache results."""
    for item, text in chunk:
        try:
            request_start = time.monotonic()
            res = spec.run(ec.ctx, text, bcp47, cfg_dir)
            elapsed_ms = int((time.monotonic() - request_start) * 1000)

            errors = res.get("errors", []) if isinstance(res, dict) else []
            process_grammar_results([(item, text)], [errors], bcp47, original_bcp47, elapsed_ms, ec)
            grammar_obs(spec.obs_event, chunk_len=1, results_len=len(errors), elapsed_ms=elapsed_ms, bcp47=bcp47)
        except Exception as ex:
            log.exception("[grammar] %s check failed", spec.name)
            get_grammar_status().failed(f"{spec.name} check", result=str(ex))


@dataclass(frozen=True)
class _ProviderSpec:
    name: str
    obs_event: str
    run: Any


def _run_languagetool(ctx: Any, text: str, bcp47: str, _cfg_dir: str) -> Any:
    from plugin.scripting.client import run_languagetool_check

    return run_languagetool_check(ctx, text, bcp47)


def _run_vale(ctx: Any, text: str, _bcp47: str, cfg_dir: str) -> Any:
    from plugin.scripting.client import run_vale_check

    return run_vale_check(ctx, text, cfg_dir, "Microsoft,Google,write-good")


_SINGLE_SENTENCE_PROVIDERS: dict[str, _ProviderSpec] = {
    "languagetool": _ProviderSpec("LanguageTool", "worker_grammar_done", _run_languagetool),
    "vale": _ProviderSpec("Vale", "worker_style_done", _run_vale),
}


def _get_paragraph_text_for_range(model: Any, rng: Any) -> str | None:
    """Return the entire paragraph string containing rng, or None."""
    try:
        text_obj = getattr(rng, "getText", lambda: None)()
        if text_obj is None and model is not None:
            text_obj = getattr(model, "getText", lambda: None)()
        if text_obj is not None:
            tc = text_obj.createTextCursorByRange(rng)
            if hasattr(tc, "gotoStartOfParagraph") and hasattr(tc, "gotoEndOfParagraph"):
                tc.gotoStartOfParagraph(False)
                tc.gotoEndOfParagraph(True)
                return tc.getString()
    except Exception as exc:
        log.debug("paragraph text for range failed (%s)", type(exc).__name__)
    return None


def apply_language_change(
    doc_id: str,
    sentence_text: str,
    detected_bcp47: str,
    start_pos: int = 0,
    paragraph_text: str = "",
) -> bool:
    """Update CharLocale on the sentence text span inside the document when language mismatch occurs."""
    import uno

    def _do_update() -> bool:
        model = grammar_persistence.get_document_model_for_id(doc_id)
        if not model:
            return False

        lang, country = bcp47_to_uno_lang_country(
            normalize_detected_bcp47(detected_bcp47) or detected_bcp47
        )

        new_locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language=lang, Country=country)

        def _paragraph_ok(rng: Any) -> bool:
            return not paragraph_text or _get_paragraph_text_for_range(model, rng) == paragraph_text

        ctrl = getattr(model, "getCurrentController", lambda: None)()
        view_cursor = getattr(ctrl, "getViewCursor", lambda: None)() if ctrl else None

        found_range = None
        # Paragraph-relative cursor check: verify sentence text and paragraph text if provided.
        # The paragraph-relative cursor is built for every start_pos,
        # including 0. n_start is the sentence offset inside the
        # proofread paragraph, so 0 is its first sentence, not "search
        # from the caret". findNext from the view cursor retags a
        # different copy of the same sentence when 0 is treated as "no
        # offset". Offset 0 still selects from the start of that
        # paragraph. start_pos > 0 still moves, then expands, on the same
        # cursor.
        if view_cursor is not None:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            text_obj = model.getText()
            doc_cursor = text_obj.createTextCursorByRange(view_cursor.getStart())
            if hasattr(doc_cursor, "gotoStartOfParagraph"):
                doc_cursor.gotoStartOfParagraph(False)
                if start_pos > 0:
                    doc_cursor.goRight(start_pos, False)
                doc_cursor.goRight(len(sentence_text), True)
                if doc_cursor.getString() == sentence_text and _paragraph_ok(doc_cursor):
                    found_range = doc_cursor

        if found_range:
            found_range.setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s", detected_bcp47)
            return True

        # Fast path missed; fallback to findAll. Keep a hit only when that
        # range's paragraph string equals paragraph_text. One remaining hit is retagged.
        search_desc = model.createSearchDescriptor()
        search_desc.setSearchString(sentence_text)
        search_desc.SearchCaseSensitive = True

        matches = model.findAll(search_desc)
        matched_ranges = []
        if matches is not None:
            count = getattr(matches, "getCount", lambda: 0)()
            for i in range(count):
                rng = matches.getByIndex(i)
                if _paragraph_ok(rng):
                    matched_ranges.append(rng)

        if len(matched_ranges) == 1:
            matched_ranges[0].setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s (via findAll)", detected_bcp47)
            return True

        return False

    try:
        from plugin.framework.queue_executor import main

        return bool(main.run(_do_update))
    except Exception as e:
        log.warning("Failed to update language property: %s", e)
        return False


def _apply_language_retag(
    item: GrammarWorkItem,
    target_bcp47: str,
) -> None:
    applied = apply_language_change(
        item.doc_id,
        item.text,
        target_bcp47,
        start_pos=item.n_start,
        paragraph_text=item.paragraph_text,
    )
    if not applied:
        grammar_obs("apply_language_change_skip", doc_id=item.doc_id, sentence=item.text, bcp47=target_bcp47)


def run_grammar_check(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    original_bcp47: str,
    ec: Any,
) -> None:
    """Grammar check dispatcher: executes LLM, LanguageTool, or Vale, then caches results."""
    try:
        if isinstance(ec.spec, _ProviderSpec):
            cfg_dir = config.user_config_dir() or ""
            run_single_sentence_provider(
                ec.spec,
                chunk,
                bcp47,
                original_bcp47,
                cfg_dir,
                ec,
            )
            return

        # Default path: AI (LLM)
        results, elapsed_ms = call_grammar_llm(chunk, bcp47, ec)
        grammar_obs(
            "batch_stats",
            sentences_llm_requested=len(chunk),
            llm_request_duration_ms=elapsed_ms,
            bcp47=bcp47,
        )
        if len(results) != len(chunk):
            if len(results) == 0:
                log.warning(
                    "[grammar] LLM returned no parseable results for chunk of %s (model=%s)",
                    len(chunk),
                    ec.model or "",
                )
                get_grammar_status().failed("Grammar check", result="Empty LLM response")
                return
            log.warning(
                "[grammar] LLM batch result count mismatch for chunk: expected %s, got %s. Requeuing items.",
                len(chunk),
                len(results),
            )
            for item, _text in chunk:
                requeue_individual_item(item, bcp47, original_bcp47, ec)
            return
        process_grammar_results(chunk, results, bcp47, original_bcp47, elapsed_ms, ec)
        grammar_obs("worker_grammar_done", chunk_len=len(chunk), results_len=len(results), elapsed_ms=elapsed_ms, bcp47=bcp47)
        if original_bcp47 and not grammar_bcp47_tags_match(original_bcp47, bcp47):
            for item, _text in chunk:
                _apply_language_retag(item, bcp47)


    except Exception as e:
        log.exception("[grammar] Grammar check failed")
        get_grammar_status().failed("Grammar check", result=str(e))


def persisted_grammar_skip_lang_detect(
    ctx: Any,
    doc_id: str,
    text: str,
    locale: str = "",
) -> bool:
    """True if persistence already stores grammar for this sentence in ``locale``.

    Heuristic to skip redundant language-detect LLM on reopen: a stored row (including
    empty errors for 'good' sentences) implies prior proofreading. The lookup is
    locale-specific so another CharLocale's clean row does not skip detection.
    """
    if not doc_id:
        return False
    fp = grammar_proofread_locale.sentence_identity_fp(text)
    p = grammar_persistence.get_persistence(ctx, doc_id)
    # Do not adopt a v2/v3 row here. That probe would retag an unknown
    # locale as this chunk's CharLocale before proofreading reads it.
    return p is not None and p.get(fp, locale, adopt_legacy=False) is not None


def build_grammar_system_prompt(
    bcp47: str,
    ignored_reasons: set[str],
    *,
    batch: bool,
    any_partial: bool,
) -> str:
    lang_name = grammar_english_name_for_bcp47(bcp47)
    if batch:
        sys_prompt = grammar_proofread_locale.GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE.format(lang_name=lang_name, bcp47=bcp47)
        if any_partial:
            sys_prompt += " The input may contain partial sentences; prefer conservative grammar suggestions and avoid broad rewrites."
    else:
        sys_prompt = grammar_proofread_locale.GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(lang_name=lang_name, bcp47=bcp47)
        if any_partial:
            sys_prompt += " The input may be a partial sentence; prefer conservative grammar suggestions and avoid broad rewrites."
    if ignored_reasons:
        sys_prompt += "\n\nIMPORTANT: The user has explicitly chosen to IGNORE the following rules/style issues in this document. DO NOT report any errors or suggestions that match or are highly similar to these:\n"
        for reason in sorted(ignored_reasons):
            sys_prompt += f"- {reason}\n"
    return sys_prompt


def language_detect_llm_sync(ec: Any, messages: list[dict[str, str]], max_tokens: int) -> str:
    """Sync language-detect LLM call with one retry when the model returns empty content."""
    model = ec.model or None
    caps = (max_tokens, max(max_tokens * 2, grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE))
    content = ""
    for idx, cap in enumerate(caps):
        content = ec.client.chat_completion_sync(
            messages,
            max_tokens=cap,
            model=model,
            response_format={"type": "json_object"},
            prepend_dev_build_system_prefix=False,
        )
        if (content or "").strip():
            return content
        if idx == 0:
            grammar_obs("lang_detect_empty_response", model=model or "", max_tokens=cap)
            log.warning(
                "[grammar] language detect returned empty content (max_tokens=%s model=%s); retrying with higher cap",
                cap,
                model,
            )
        else:
            grammar_obs("lang_detect_empty_response", model=model or "", max_tokens=cap, retry=True)
            log.warning("[grammar] language detect still empty after retry (max_tokens=%s)", cap)
    return content or ""


def call_grammar_llm(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    ec: Any,
) -> tuple[list[Any], int]:
    """Run grammar LLM for one sentence or a batch; return parsed results and elapsed ms."""
    from .grammar_ignore_rules import collect_ignored_reasons

    batch = len(chunk) > 1
    doc_id = chunk[0][0].doc_id
    ignored_reasons = collect_ignored_reasons(ec.ctx, doc_id)
    any_partial = any(not _item_is_complete(item, text) for item, text in chunk)
    sys_prompt = build_grammar_system_prompt(bcp47, ignored_reasons, batch=batch, any_partial=any_partial)
    max_tokens = ec.max_tok

    if batch:
        user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(chunk))
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]
        grammar_obs("worker_llm_batch_request", item_count=len(chunk), total_len=len(user_content))
        get_grammar_status().request(f"Batch of {len(chunk)}", result="LLM batch request")
    else:
        item, text = chunk[0]
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]
        grammar_obs("worker_llm_request_prepare", enqueue_seq=item.enqueue_seq, llm_text_len=len(text), llm_preview=slice_preview_debug(text, 96))
        get_grammar_status().request(text, result="LLM request")

    request_start = time.monotonic()
    with llm_lanes.grammar_llm_request_gate(grammar_proofread_locale.grammar_max_in_flight()):
        model = ec.model or None
        content = ec.client.chat_completion_sync(
            messages,
            max_tokens=max_tokens,
            model=model,
            response_format={"type": "json_object"},
            prepend_dev_build_system_prefix=False,
            chat_extra=_GRAMMAR_CHAT_EXTRA,
        )
        if not (content or "").strip():
            # Some reasoning models (e.g. Inception Mercury) return content: null with stop — treated as clean in call_grammar_llm.
            grammar_obs("grammar_llm_empty_response", model=model or "", max_tokens=max_tokens)
            log.debug("[grammar] grammar LLM returned empty content (max_tokens=%s model=%s)", max_tokens, model)
        content = content or ""
    elapsed_ms = int((time.monotonic() - request_start) * 1000)

    if batch:
        return grammar_proofread_json.parse_grammar_batch_json(content), elapsed_ms
    sent_results = grammar_proofread_json.parse_grammar_json(content)
    return ([sent_results], elapsed_ms)


def _obs_lang_detect_item(idx: int, source: str, raw: str | None, canon: str | None, text: str) -> None:
    grammar_obs(
        "lang_detect_item",
        idx=idx,
        source=source,
        raw=raw,
        canon=canon,
        text_preview=slice_preview_debug(text, 48),
    )


def _remember_language(text: str, raw: str) -> str:
    canon = normalize_detected_bcp47(raw) or raw
    grammar_persistence.grammar_registry.put_cached_language(text, canon)
    return canon


def _fill_from_cache_and_persistence(
    chunk: list[tuple[Any, str]],
    ec: Any,
) -> list[str | None]:
    """Fill detected languages from LRU cache or persisted grammar heuristic; return list with None for misses."""
    detected_langs: list[str | None] = []
    for idx, (item, text) in enumerate(chunk):
        cached = grammar_persistence.grammar_registry.get_cached_language(text)
        if cached:
            canon = normalize_detected_bcp47(cached) or cached
            detected_langs.append(canon)
            _obs_lang_detect_item(idx, "cache", cached, canon, text)
        elif persisted_grammar_skip_lang_detect(ec.ctx, item.doc_id, text, ec.grammar_bcp47):
            grammar_obs("lang_detect_skip", reason="persisted_grammar_heuristic", doc_id=item.doc_id[:32] if item.doc_id else "")
            canon = _remember_language(text, ec.grammar_bcp47)
            detected_langs.append(canon)
            _obs_lang_detect_item(idx, "persisted", ec.grammar_bcp47, canon, text)
        else:
            detected_langs.append(None)
    return detected_langs


def _detect_languages_via_langdetect(
    chunk: list[tuple[Any, str]],
    detected_langs: list[str | None],
    ec: Any,
) -> None:
    """Fill pending slots via PyPI langdetect in the embeddings venv worker."""
    from plugin.writer.locale.langdetect_service import detect_languages

    pending = [idx for idx, lang in enumerate(detected_langs) if lang is None]
    if not pending:
        return

    if len(pending) == 1:
        idx = pending[0]
        text = chunk[idx][1]
        get_grammar_status().request(text, result="Detecting language")
    else:
        get_grammar_status().request(f"Batch of {len(pending)}", result="Detecting language")

    pending_texts = [chunk[idx][1] for idx in pending]
    batch_results = detect_languages(ec.ctx, pending_texts)

    for idx, canon in zip(pending, batch_results):
        text = chunk[idx][1]
        if canon:
            grammar_persistence.grammar_registry.put_cached_language(text, canon)
            detected_langs[idx] = canon
            _obs_lang_detect_item(idx, "langdetect", canon, canon, text)
        else:
            _obs_lang_detect_item(idx, "none", None, None, text)


def _detect_via_llm_batch(
    texts: list[str],
    detect_lang_instruction: str,
    ec: Any,
) -> list[str | None]:
    """Run batch language detection via LLM and return canonical BCP47 or None per item."""
    user_content = "\n".join(f"{idx+1}. {text}" for idx, text in enumerate(texts))
    detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT.format(
        detect_lang_instruction=detect_lang_instruction
    )
    detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": user_content}]
    detect_max_tok = grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_PER_BATCH_ITEM * len(texts)

    get_grammar_status().request(f"Batch of {len(texts)}", result="Detecting language")
    with llm_lanes.grammar_llm_request_gate(grammar_proofread_locale.grammar_max_in_flight()):
        detect_content = language_detect_llm_sync(ec, detect_messages, detect_max_tok)

    parsed_langs = grammar_proofread_json.parse_language_detect_batch_json(detect_content or "")
    ok = len(parsed_langs) == len(texts)
    grammar_obs("lang_detect_batch_parse", chunk_len=len(texts), parsed_len=len(parsed_langs), ok=ok)

    results: list[str | None] = []
    if not ok and detect_content:
        log.warning("[grammar] language detect batch parse mismatch: chunk=%s parsed=%s", len(texts), len(parsed_langs))
    langs = parsed_langs if ok else [None] * len(texts)
    for idx, (text, d_lang) in enumerate(zip(texts, langs)):
        if d_lang:
            canon = _remember_language(text, d_lang)
            results.append(canon)
            _obs_lang_detect_item(idx, "llm", d_lang, canon, text)
        else:
            results.append(None)
            _obs_lang_detect_item(idx, "none", None, None, text)
    return results


def _detect_via_llm_single(
    text: str,
    detect_lang_instruction: str,
    ec: Any,
) -> str | None:
    """Run single-sentence language detection via LLM and return canonical BCP47 or None."""
    detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_SYSTEM_PROMPT.format(
        detect_lang_instruction=detect_lang_instruction
    )
    detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": text}]

    get_grammar_status().request(text, result="Detecting language")
    with llm_lanes.grammar_llm_request_gate(grammar_proofread_locale.grammar_max_in_flight()):
        detect_content = language_detect_llm_sync(
            ec,
            detect_messages,
            grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE,
        )

    parsed_lang = grammar_proofread_json.parse_language_detect_json(detect_content or "")
    grammar_obs("lang_detect_single_parse", parsed=bool(parsed_lang), text_preview=slice_preview_debug(text, 48))
    if parsed_lang:
        canon = _remember_language(text, parsed_lang)
        _obs_lang_detect_item(0, "llm", parsed_lang, canon, text)
        return canon
    if detect_content:
        log.warning("[grammar] language detect JSON parse failed for sentence preview=%s", slice_preview_debug(text, 48))
    _obs_lang_detect_item(0, "none", None, None, text)
    return None


def detect_languages_for_chunk(
    chunk: list[tuple[Any, str]],
    detect_lang_instruction: str,
    ec: Any,
) -> list[str | None]:
    """Resolve BCP47 per sentence (cache, optional persistence heuristic, LLM, or langdetect)."""
    detected_langs = _fill_from_cache_and_persistence(chunk, ec)
    if all(lang is not None for lang in detected_langs):
        return detected_langs

    if ec.detect_lang_mode == "langdetect":
        _detect_languages_via_langdetect(chunk, detected_langs, ec)
        return detected_langs

    pending = [(idx, text) for idx, (_item, text) in enumerate(chunk) if detected_langs[idx] is None]
    if len(pending) > 1:
        batch_results = _detect_via_llm_batch([text for _idx, text in pending], detect_lang_instruction, ec)
        for (idx, _text), canon in zip(pending, batch_results):
            detected_langs[idx] = canon
    else:
        idx, text = pending[0]
        detected_langs[idx] = _detect_via_llm_single(text, detect_lang_instruction, ec)
    return detected_langs


def _detected_target_bcp47(raw: str | None, grammar_bcp47: str) -> str:
    norm = normalize_detected_bcp47(raw) if raw else None
    return norm or normalize_detected_bcp47(grammar_bcp47) or grammar_bcp47


@dataclass(frozen=True)
class GrammarWorkerContext:
    """Shared I/O context for grammar worker phases (LLM, queue, document)."""
    ctx: Any
    client: Any
    gq: GrammarWorkQueue
    model: str
    grammar_bcp47: str
    max_tok: int
    detect_lang_mode: str = "off"
    spec: _ProviderSpec | None = None


def requeue_individual_item(
    item: GrammarWorkItem,
    new_bcp47: str,
    original_bcp47: str,
    ec: GrammarWorkerContext,
) -> None:
    """Requeue one item after language mismatch or grammar batch count mismatch.

    Do not store a clean sentence for a locale this call just rejected.
    """
    sent_complete = _item_is_complete(item, item.text)
    requeue_inflight_key = grammar_inflight_key(item.doc_id, new_bcp47, item.text, sent_complete)

    new_item = replace(
        item,
        grammar_bcp47=new_bcp47,
        enqueue_seq=next_enqueue_seq(),
        inflight_key=requeue_inflight_key,
        original_bcp47=original_bcp47,
    )
    ec.gq.enqueue(new_item)


def _worker_batch_gates(items: list[GrammarWorkItem], provider_name: str) -> bool:
    """Return False when the batch should not run (grammar off or agent pause)."""
    item_provider = items[0].provider or ""
    if not item_provider and provider_name == "off":
        grammar_obs("worker_batch_skip", reason="grammar_disabled", item_count=len(items))
        return False
    pause_during_agent = bool(config.get_config("doc.grammar_proofreader_pause_during_agent"))
    if pause_during_agent and send_cancellation.is_agent_active():
        # Local engines do not share the LLM request lane; only pause the LLM provider.
        active_prov = item_provider or provider_name
        if not grammar_settings.is_local(active_prov):
            grammar_obs("worker_batch_skip", reason="pause_during_agent", item_count=len(items))
            return False
    return True


def _worker_ready_items(
    chunk: list[tuple[GrammarWorkItem, str]],
    gq: Any,
    grammar_bcp47: str,
    ctx: Any,
) -> list[tuple[GrammarWorkItem, str]]:
    ready: list[tuple[GrammarWorkItem, str]] = []
    for item, text in chunk:
        if gq.inflight_superseded(item.inflight_key, item.enqueue_seq) is True:
            grammar_obs("worker_skip", reason="superseded_before_process", enqueue_seq=item.enqueue_seq, inflight_key=item.inflight_key)
            continue
        cached = grammar_proofread_cache.cache_get_sentence(
            grammar_bcp47, text, ctx=ctx, doc_id=item.doc_id, checker_identity=_item_cache_identity(item)
        )
        if cached is None:
            ready.append((item, text))
        else:
            orig_bcp47 = item.original_bcp47 or item.grammar_bcp47
            if orig_bcp47 and not grammar_bcp47_tags_match(orig_bcp47, grammar_bcp47):
                _apply_language_retag(item, grammar_bcp47)
    return ready


def _detect_lang_instruction(detect_items: list[tuple[GrammarWorkItem, str]]) -> str:
    """Prompt suffix listing the locales already recorded for the document plus the item's own."""
    doc_locales = grammar_persistence.grammar_registry.get_doc_locales(detect_items[0][0].doc_id)
    locales_in_use = sorted(doc_locales | {detect_items[0][0].grammar_bcp47})
    return f" Choose from the following locales currently used in the document, or provide a new one if none match: {', '.join(locales_in_use)}."


def _retarget_item_locale(
    item: GrammarWorkItem,
    text: str,
    target_bcp47: str,
    ec: GrammarWorkerContext,
) -> GrammarWorkItem | None:
    new_key = grammar_inflight_key(
        item.doc_id, target_bcp47, text, _item_is_complete(item, text)
    )
    new_seq = next_enqueue_seq()
    if ec.gq.note_inflight_generation(new_key, new_seq) is True:
        grammar_obs(
            "worker_skip",
            reason="superseded_before_process",
            enqueue_seq=new_seq,
            inflight_key=new_key,
        )
        return None
    return replace(
        item,
        grammar_bcp47=target_bcp47,
        enqueue_seq=new_seq,
        inflight_key=new_key,
        original_bcp47=item.original_bcp47 or ec.grammar_bcp47,
    )


def _worker_process_chunk(
    chunk: list[tuple[GrammarWorkItem, str]],
    ec: GrammarWorkerContext,
    grammar_bcp47: str,
    *,
    original_bcp47: str,
) -> None:
    """Run the grammar LLM for one chunk of ready items."""
    current_chunk = _worker_ready_items(chunk, ec.gq, grammar_bcp47, ec.ctx)
    if not current_chunk:
        grammar_obs("worker_chunk_skip", reason="empty_result_chunk", chunk_len=len(chunk))
        return
    run_grammar_check(current_chunk, grammar_bcp47, original_bcp47, ec)


def run_llm_and_cache_batch(
    items: list[GrammarWorkItem],
    *,
    grammar_queue_instance: GrammarWorkQueue,
) -> None:
    """Process a batch of items (ideally from one paragraph): LLM requests + multi-sentence cache writes."""
    if not items:
        return

    item0 = items[0]
    ctx = item0.ctx
    grammar_bcp47 = item0.grammar_bcp47
    original_bcp47 = item0.original_bcp47 or grammar_bcp47

    status_cycle_started = False
    try:
        provider_name = item0.provider or grammar_settings.provider()
        spec = _SINGLE_SENTENCE_PROVIDERS.get(provider_name)
        if not _worker_batch_gates(items, provider_name):
            return

        valid_items = _worker_ready_items(
            [(item, cache_lookup_text(item)) for item in items],
            grammar_queue_instance,
            grammar_bcp47,
            ctx,
        )
        if not valid_items:
            grammar_obs("worker_batch_skip", reason="all_cached_or_superseded", item_count=len(items))
            return

        get_grammar_status().begin_status_cycle()
        status_cycle_started = True

        local_provider = spec is not None

        max_tok = grammar_proofread_locale.grammar_max_tokens()
        model = ""
        client: Any = None
        if not local_provider:
            ident = item0.checker_identity or ""
            if ident.startswith("llm:"):
                model = ident[4:]
            if not model:
                import importlib

                model_fetcher = importlib.import_module("plugin.framework.client.model_fetcher")
                model = model_fetcher.get_grammar_model()
            client = _get_or_create_thread_llm_client(config.get_api_config(), ctx)

        batch_size = 1 if local_provider else grammar_proofread_locale.grammar_batch_sentences()
        detect_lang_mode = "off" if local_provider else grammar_settings.detect_language_mode()
        detect_lang_enabled = detect_lang_mode != "off"

        ec = GrammarWorkerContext(
            ctx=ctx,
            client=client,
            gq=grammar_queue_instance,
            model=model,
            grammar_bcp47=grammar_bcp47,
            max_tok=max_tok,
            detect_lang_mode=detect_lang_mode,
            spec=spec,
        )

        def _chunkify(items_list: list[tuple[GrammarWorkItem, str]]) -> list[list[tuple[GrammarWorkItem, str]]]:
            step = max(1, batch_size)
            return [items_list[i : i + step] for i in range(0, len(items_list), step)]

        plain_items: list[tuple[GrammarWorkItem, str]] = []
        detect_items: list[tuple[GrammarWorkItem, str]] = []
        if detect_lang_enabled:
            for item, text in valid_items:
                if not _item_is_complete(item, text):
                    plain_items.append((item, text))
                else:
                    detect_items.append((item, text))
        else:
            plain_items = list(valid_items)

        for chunk in _chunkify(plain_items):
            _worker_process_chunk(chunk, ec, grammar_bcp47, original_bcp47=original_bcp47)

        if detect_items:
            detect_lang_instruction = _detect_lang_instruction(detect_items)

            items_by_target_locale: dict[str, list[tuple[GrammarWorkItem, str]]] = defaultdict(list)
            detect_chunk_size = grammar_proofread_locale.GRAMMAR_BATCH_MAX_SENTENCES
            for i in range(0, len(detect_items), detect_chunk_size):
                d_chunk = detect_items[i : i + detect_chunk_size]
                detected_langs = detect_languages_for_chunk(d_chunk, detect_lang_instruction, ec)
                for idx, (item, text) in enumerate(d_chunk):
                    target_bcp47 = _detected_target_bcp47(detected_langs[idx], grammar_bcp47)
                    if target_bcp47 != grammar_bcp47:
                        new_item = _retarget_item_locale(item, text, target_bcp47, ec)
                        if new_item is not None:
                            items_by_target_locale[target_bcp47].append((new_item, text))
                    else:
                        items_by_target_locale[target_bcp47].append((item, text))

            for loc, loc_items in items_by_target_locale.items():
                ready_items = _worker_ready_items(loc_items, ec.gq, loc, ec.ctx)
                if not ready_items:
                    continue
                for chunk in _chunkify(ready_items):
                    _worker_process_chunk(chunk, ec, loc, original_bcp47=grammar_bcp47)

    except Exception as e:
        log.exception("[grammar] worker batch failed")
        get_grammar_status().failed("Batch processing", result=type(e).__name__)
    finally:
        if status_cycle_started:
            get_grammar_status().end_status_cycle()


