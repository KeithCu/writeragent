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
from dataclasses import asdict, dataclass, replace
from typing import Any

from . import grammar_proofread_cache, grammar_proofread_json, grammar_proofread_locale, grammar_persistence, grammar_proofread_text
from .grammar_obs import emit_grammar_status, grammar_obs
from plugin.framework.grammar_rule_ids import (
    bcp47_to_uno_lang_country,
    grammar_bcp47_tags_match,
    grammar_english_name_for_bcp47,
    normalize_detected_bcp47,
)
from .grammar_proofread_text import slice_preview_debug
from .grammar_work_queue import GrammarWorkItem, GrammarWorkQueue, next_enqueue_seq, grammar_queue

from plugin.framework import config, llm_lanes, send_cancellation
from plugin.framework.errors import best_effort

log = logging.getLogger("writeragent.grammar")

# Mercury and other reasoning models may return null content when effort is unconstrained.
_GRAMMAR_CHAT_EXTRA = {"reasoning": {"effort": "minimal"}}

_grammar_worker_tls = threading.local()


def is_grammar_enabled() -> bool:
    """True if the grammar checker is enabled on the Doc tab (LLM, LanguageTool, Vale, or Harper)."""
    return get_grammar_provider() != "off"


def get_grammar_provider() -> str:
    """Return the active grammar provider name ('off', 'llm', 'languagetool', 'vale', or 'harper')."""
    from plugin.framework.uno_context import is_libreharper

    if is_libreharper():
        return "harper"

    val = config.get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return "llm" if val else "off"
    val_str = str(val).strip().lower()
    if val_str == "true":
        return "llm"
    if val_str in ("llm", "languagetool", "vale", "harper"):
        return val_str

    return "off"


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
        and not getattr(cached_client, "_stopped", False)
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


def emit_done_status(
    ec: Any,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
    preview_source: str | None = None,
    length_hint: int | None = None,
) -> None:
    """Sidebar done: deferred while parallel drain batches run."""
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


def _item_cache_identity(item: Any) -> str | None:
    """Resolve cache checker_identity for a work item without persisting bare 'llm'."""
    checker_ident = getattr(item, "checker_identity", "")
    if checker_ident:
        return checker_ident
    provider = getattr(item, "provider", "")
    if provider in ("harper", "languagetool", "vale"):
        return provider
    return None


def cache_lookup_text(item: Any) -> str:
    """Return cache lookup text: checked prefix for LLM, full text otherwise."""
    cache_ident = _item_cache_identity(item) or ""
    if cache_ident.startswith("llm:"):
        return grammar_proofread_locale.checked_grammar_text(
            item.text, grammar_proofread_locale.grammar_max_chars(getattr(item, "ctx", None))
        )
    return item.text


def process_grammar_results(
    chunk: list[tuple[Any, str]],
    results: list[Any],
    bcp47: str,
    original_bcp47: str,
    elapsed_ms: int,
    ec: Any,
) -> None:
    """Normalize errors, write sentence cache, emit done status."""
    from .grammar_ignore_rules import doc_ignored_rules, is_rule_ignored
    from .grammar_proofread_cache import ignored_rules_snapshot

    summary = _BatchResultSummary()
    doc_id = chunk[0][0].doc_id if chunk else ""
    ignored = doc_ignored_rules(ec.ctx, doc_id) if doc_id else set()
    global_ignored = ignored_rules_snapshot()
    for idx, (item, text) in enumerate(chunk):
        if ec.gq and ec.gq.inflight_superseded(item.inflight_key, item.enqueue_seq):
            continue
        if idx < len(results):
            errors = results[idx]
            norm_errors = grammar_proofread_text.normalize_errors_for_text(text, 0, len(text), errors, ec.ctx, bcp47)

            active_errors = [e for e in norm_errors if not is_rule_ignored(e.rule_identifier, ignored, global_ignored)]
            payload = [asdict(e) for e in norm_errors]

            put_kwargs: dict[str, Any] = {"ctx": ec.ctx, "doc_id": item.doc_id}
            cache_ident = _item_cache_identity(item)
            if cache_ident:
                put_kwargs["checker_identity"] = cache_ident
            grammar_proofread_cache.cache_put_sentence(bcp47, text, payload, **put_kwargs)
            if original_bcp47 and not grammar_bcp47_tags_match(original_bcp47, bcp47):
                log.debug("[grammar] Double caching for %s (detected %s)", original_bcp47, bcp47)
                grammar_proofread_cache.cache_put_sentence(original_bcp47, text, payload, **put_kwargs)
            else:
                log.debug("[grammar] No double caching: original=%s, detected=%s", original_bcp47, bcp47)

            summary.record(text, len(active_errors))

    if summary.n_written:
        preview_src = summary.preview_source()
        iw = "issue" if summary.total_issues == 1 else "issues"
        sw = "sentence" if summary.n_written == 1 else "sentences"
        emit_done_status(
            ec,
            preview_src,
            result=f"{summary.total_issues} {iw}, {summary.n_written} {sw}",
            elapsed_ms=elapsed_ms,
            preview_source=preview_src,
            length_hint=summary.chars_checked,
        )
    else:
        emit_done_status(ec, "batch", result="skipped (superseded)", elapsed_ms=elapsed_ms)


def run_single_sentence_provider(
    provider_name: str,
    runner_fn: Any,
    chunk: list[tuple[Any, str]],
    bcp47: str,
    original_bcp47: str,
    ec: Any,
    *,
    emit_request_status: bool = False,
    obs_event_name: str = "worker_grammar_done",
) -> None:
    """Execute a single-sentence local checker (Harper, LanguageTool, Vale), record metrics, and cache results."""
    for item, text in chunk:
        try:
            if emit_request_status:
                emit_grammar_status("request", text, result=f"{provider_name} check")
            request_start = time.monotonic()
            res = runner_fn(text)
            elapsed_ms = int((time.monotonic() - request_start) * 1000)

            errors = res.get("errors", []) if isinstance(res, dict) else []
            process_grammar_results([(item, text)], [errors], bcp47, original_bcp47, elapsed_ms, ec)
            grammar_obs(obs_event_name, chunk_len=1, results_len=len(errors), elapsed_ms=elapsed_ms, bcp47=bcp47)
        except Exception as ex:
            log.exception("[grammar] %s check failed", provider_name)
            emit_grammar_status("failed", f"{provider_name} check", result=str(ex))


@dataclass(frozen=True)
class _ProviderSpec:
    name: str
    obs_event: str
    runner_factory: Any
    emit_request_status: bool = False


def _make_languagetool_runner(ctx: Any, bcp47: str, cfg_dir: str) -> Any:
    from plugin.scripting.client import run_languagetool_check

    return lambda t: run_languagetool_check(ctx, t, bcp47)


def _make_vale_runner(ctx: Any, bcp47: str, cfg_dir: str) -> Any:
    from plugin.scripting.client import run_vale_check

    return lambda t: run_vale_check(ctx, t, cfg_dir, "Microsoft,Google,write-good")


def _make_harper_runner(ctx: Any, bcp47: str, cfg_dir: str) -> Any:
    from plugin.writer.locale.harper import run_harper_check

    return lambda t: run_harper_check(ctx, t, cfg_dir, bcp47=bcp47)


_SINGLE_SENTENCE_PROVIDERS: dict[str, _ProviderSpec] = {
    "languagetool": _ProviderSpec("LanguageTool", "worker_grammar_done", _make_languagetool_runner),
    "vale": _ProviderSpec("Vale", "worker_style_done", _make_vale_runner),
    "harper": _ProviderSpec("Harper", "worker_harper_done", _make_harper_runner, emit_request_status=True),
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
    ctx: Any,
    doc_id: str,
    sentence_text: str,
    detected_bcp47: str,
    start_pos: int = 0,
    paragraph_text: str = "",
) -> bool:
    """Update CharLocale on the sentence text span inside the document when language mismatch occurs."""
    import uno

    def _do_update() -> bool:
        model = grammar_persistence.get_document_model_for_id(ctx, doc_id)
        if not model:
            return False

        lang, country = bcp47_to_uno_lang_country(
            normalize_detected_bcp47(detected_bcp47) or detected_bcp47
        )

        new_locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language=lang, Country=country)

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
        if view_cursor is not None and start_pos >= 0:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            text_obj = model.getText()
            doc_cursor = text_obj.createTextCursorByRange(view_cursor.getStart())
            if hasattr(doc_cursor, "gotoStartOfParagraph"):
                doc_cursor.gotoStartOfParagraph(False)
                if start_pos > 0:
                    doc_cursor.goRight(start_pos, False)
                doc_cursor.goRight(len(sentence_text), True)
                if doc_cursor.getString() == sentence_text:
                    if not paragraph_text:
                        found_range = doc_cursor
                    else:
                        para_str = _get_paragraph_text_for_range(model, doc_cursor)
                        if para_str == paragraph_text:
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
                if paragraph_text:
                    para_str = _get_paragraph_text_for_range(model, rng)
                    if para_str == paragraph_text:
                        matched_ranges.append(rng)
                else:
                    matched_ranges.append(rng)

        if len(matched_ranges) == 1:
            matched_ranges[0].setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s (via findAll)", detected_bcp47)
            return True

        return False

    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework import queue_executor

        if on_main_thread():
            return bool(_do_update())
        else:
            return bool(queue_executor.execute_on_main_thread(_do_update))
    except Exception as e:
        log.warning("Failed to update language property: %s", e)
        return False


def run_grammar_check(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    original_bcp47: str,
    ec: Any,
) -> None:
    """Grammar check dispatcher: executes LLM, LanguageTool, Vale, or Harper, then caches results."""
    try:
        from plugin.framework.config import user_config_dir

        # Always use the job's explicit provider if present, even if it is not
        # the current default config, so that background retry and cache
        # identity don't desync with the UI check that enqueued it.
        provider = chunk[0][0].provider if chunk and chunk[0][0].provider else get_grammar_provider()
        spec = _SINGLE_SENTENCE_PROVIDERS.get(provider)
        if spec is not None:
            cfg_dir = user_config_dir() or ""
            runner = spec.runner_factory(ec.ctx, bcp47, cfg_dir)
            run_single_sentence_provider(
                spec.name,
                runner,
                chunk,
                bcp47,
                original_bcp47,
                ec,
                emit_request_status=spec.emit_request_status,
                obs_event_name=spec.obs_event,
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
        completion = decide_grammar_completion(len(chunk), len(results), bcp47, original_bcp47)
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
            for item, _text in chunk:
                requeue_individual_item(item, item.text, bcp47, original_bcp47, ec, cache_placeholder=False)
            return
        process_grammar_results(chunk, results, bcp47, original_bcp47, elapsed_ms, ec)
        grammar_obs("worker_grammar_done", chunk_len=len(chunk), results_len=len(results), elapsed_ms=elapsed_ms, bcp47=bcp47)
        if completion.apply_locale_after_success:
            for item, _text in chunk:
                applied = apply_language_change(
                    ec.ctx,
                    item.doc_id,
                    item.text,
                    bcp47,
                    start_pos=getattr(item, 'n_start', 0),
                    paragraph_text=getattr(item, 'paragraph_text', ""),
                )
                if not applied:
                    grammar_obs("apply_language_change_skip", doc_id=item.doc_id, sentence=item.text, bcp47=bcp47)


    except Exception as e:
        log.exception("[grammar] Grammar check failed")
        emit_grammar_status("failed", "Grammar check", result=str(e))


def persisted_grammar_skip_lang_detect(
    ctx: Any,
    doc_id: str,
    text: str,
    locale: str | None = None,
) -> bool:
    """True if persistence already stores grammar for this sentence in ``locale``.

    Heuristic to skip redundant language-detect LLM on reopen: a stored row (including
    empty errors for 'good' sentences) implies prior proofreading. The lookup is
    locale-specific so another CharLocale's clean row does not skip detection.
    """
    if not doc_id:
        return False
    fp = grammar_proofread_cache.sentence_identity_fp(text)
    p = grammar_persistence.get_persistence(ctx, doc_id)
    # Do not adopt a v2/v3 row here. That probe would retag an unknown
    # locale as this chunk's CharLocale before proofreading reads it.
    return p is not None and p.get(fp, locale, adopt_legacy=False) is not None


def get_active_ignored_reasons(ctx: Any, doc_id: str) -> set[str]:
    """Document + global ignored grammar rules, normalized for prompt filtering."""
    from .grammar_ignore_rules import collect_ignored_reasons

    return collect_ignored_reasons(ctx, doc_id)


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
    content = ec.client.chat_completion_sync(
        messages,
        max_tokens=max_tokens,
        model=model,
        response_format={"type": "json_object"},
        prepend_dev_build_system_prefix=False,
    )
    if (content or "").strip():
        return content
    grammar_obs("lang_detect_empty_response", model=model or "", max_tokens=max_tokens)
    log.warning("[grammar] language detect returned empty content (max_tokens=%s model=%s); retrying with higher cap", max_tokens, model)
    retry_cap = max(max_tokens * 2, grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE)
    content = ec.client.chat_completion_sync(
        messages,
        max_tokens=retry_cap,
        model=model,
        response_format={"type": "json_object"},
        prepend_dev_build_system_prefix=False,
    )
    if not (content or "").strip():
        grammar_obs("lang_detect_empty_response", model=model or "", max_tokens=retry_cap, retry=True)
        log.warning("[grammar] language detect still empty after retry (max_tokens=%s)", retry_cap)
    return content or ""


def grammar_llm_sync(ec: Any, messages: list[dict[str, str]], max_tokens: int) -> str:
    """Sync grammar LLM call with minimal reasoning effort (no retry on empty content)."""
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
    return content or ""


def call_grammar_llm(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    ec: Any,
) -> tuple[list[Any], int]:
    """Run grammar LLM for one sentence or a batch; return parsed results and elapsed ms."""
    from . import grammar_proofread_locale

    batch = len(chunk) > 1
    doc_id = chunk[0][0].doc_id
    ignored_reasons = get_active_ignored_reasons(ec.ctx, doc_id)
    any_partial = any(item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text) for item, text in chunk)
    sys_prompt = build_grammar_system_prompt(bcp47, ignored_reasons, batch=batch, any_partial=any_partial)

    if batch:
        user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(chunk))
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]
        grammar_obs("worker_llm_batch_request", item_count=len(chunk), total_len=len(user_content))
        emit_grammar_status("request", f"Batch of {len(chunk)}", result="LLM batch request")
        max_tokens = ec.max_tok
    else:
        item, text = chunk[0]
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]
        grammar_obs("worker_llm_request_prepare", enqueue_seq=item.enqueue_seq, llm_text_len=len(text), llm_preview=slice_preview_debug(text, 96))
        emit_grammar_status("request", text, result="LLM request")
        max_tokens = ec.max_tok

    request_start = time.monotonic()
    with llm_lanes.grammar_llm_request_gate(grammar_proofread_locale.grammar_max_in_flight(ec.ctx)):
        content = grammar_llm_sync(ec, messages, max_tokens)
    elapsed_ms = int((time.monotonic() - request_start) * 1000)

    if batch:
        return grammar_proofread_json.parse_grammar_batch_json(content or ""), elapsed_ms
    sent_results = grammar_proofread_json.parse_grammar_json(content or "")
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


def _fill_from_cache_and_persistence(
    chunk: list[tuple[Any, str]],
    ec: Any,
    *,
    trust_persisted: bool = True,
) -> list[str | None]:
    """Fill detected languages from LRU cache or persisted grammar heuristic; return list with None for misses."""
    detected_langs: list[str | None] = []
    for idx, (item, text) in enumerate(chunk):
        cached = grammar_persistence.grammar_registry.get_cached_language(text)
        if cached:
            canon = normalize_detected_bcp47(cached) or cached
            detected_langs.append(canon)
            _obs_lang_detect_item(idx, "cache", cached, canon, text)
        elif trust_persisted and persisted_grammar_skip_lang_detect(ec.ctx, item.doc_id, text, ec.grammar_bcp47):
            grammar_obs("lang_detect_skip", reason="persisted_grammar_heuristic", doc_id=item.doc_id[:32] if item.doc_id else "")
            canon = normalize_detected_bcp47(ec.grammar_bcp47) or ec.grammar_bcp47
            grammar_persistence.grammar_registry.put_cached_language(text, canon)
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
        emit_grammar_status("request", text, result="Detecting language")
    else:
        emit_grammar_status("request", f"Batch of {len(pending)}", result="Detecting language")

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
    chunk: list[tuple[Any, str]],
    detect_lang_instruction: str,
    ec: Any,
) -> list[str | None]:
    """Run batch language detection via LLM and return canonical BCP47 or None per item."""
    user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(chunk))
    detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT.format(
        detect_lang_instruction=detect_lang_instruction
    )
    detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": user_content}]
    detect_max_tok = grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_PER_BATCH_ITEM * len(chunk)

    emit_grammar_status("request", f"Batch of {len(chunk)}", result="Detecting language")
    with llm_lanes.grammar_llm_request_gate(grammar_proofread_locale.grammar_max_in_flight(ec.ctx)):
        detect_content = language_detect_llm_sync(ec, detect_messages, detect_max_tok)

    parsed_langs = grammar_proofread_json.parse_language_detect_batch_json(detect_content or "")
    ok = len(parsed_langs) == len(chunk)
    grammar_obs("lang_detect_batch_parse", chunk_len=len(chunk), parsed_len=len(parsed_langs), ok=ok)

    results: list[str | None] = []
    if ok:
        for idx, d_lang in enumerate(parsed_langs):
            text = chunk[idx][1]
            if d_lang:
                canon = normalize_detected_bcp47(d_lang) or d_lang
                grammar_persistence.grammar_registry.put_cached_language(text, canon)
                results.append(canon)
                _obs_lang_detect_item(idx, "llm", d_lang, canon, text)
            else:
                results.append(None)
                _obs_lang_detect_item(idx, "none", None, None, text)
    else:
        if detect_content:
            log.warning("[grammar] language detect batch parse mismatch: chunk=%s parsed=%s", len(chunk), len(parsed_langs))
        for idx in range(len(chunk)):
            results.append(None)
            _obs_lang_detect_item(idx, "none", None, None, chunk[idx][1])
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

    emit_grammar_status("request", text, result="Detecting language")
    with llm_lanes.grammar_llm_request_gate(grammar_proofread_locale.grammar_max_in_flight(ec.ctx)):
        detect_content = language_detect_llm_sync(
            ec,
            detect_messages,
            grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE,
        )

    parsed_lang = grammar_proofread_json.parse_language_detect_json(detect_content or "")
    grammar_obs("lang_detect_single_parse", parsed=bool(parsed_lang), text_preview=slice_preview_debug(text, 48))
    if parsed_lang:
        canon = normalize_detected_bcp47(parsed_lang) or parsed_lang
        grammar_persistence.grammar_registry.put_cached_language(text, canon)
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
    *,
    trust_persisted_grammar_as_lang: bool = True,
) -> list[str | None]:
    """Resolve BCP47 per sentence (cache, optional persistence heuristic, LLM, or langdetect)."""
    detected_langs = _fill_from_cache_and_persistence(
        chunk, ec, trust_persisted=trust_persisted_grammar_as_lang
    )
    if all(lang is not None for lang in detected_langs):
        return detected_langs

    mode = getattr(ec, "detect_lang_mode", "llm") or "llm"
    if mode not in ("llm", "langdetect"):
        mode = "llm"

    if mode == "langdetect":
        _detect_languages_via_langdetect(chunk, detected_langs, ec)
    elif len(chunk) > 1:
        batch_results = _detect_via_llm_batch(chunk, detect_lang_instruction, ec)
        for idx, canon in enumerate(batch_results):
            if detected_langs[idx] is None:
                detected_langs[idx] = canon
    else:
        canon = _detect_via_llm_single(chunk[0][1], detect_lang_instruction, ec)
        detected_langs[0] = canon

    return detected_langs


@dataclass(frozen=True)
class LangRequeueAction:
    item: Any
    text: str
    new_bcp47: str
    original_bcp47: str


@dataclass(frozen=True)
class LanguageValidationDecision:
    """Outcome of comparing detected languages to the document target locale."""

    target_bcp47: str
    result_chunk: list[tuple[Any, str]]
    requeues: tuple[LangRequeueAction, ...] = ()


def decide_language_validation(
    chunk: list[tuple[Any, str]],
    target_bcp47: str,
    detected_langs: list[str | None],
) -> LanguageValidationDecision:
    """Map detected BCP47 tags to a filtered chunk and optional per-item requeues (pure)."""
    canon_target = normalize_detected_bcp47(target_bcp47) or target_bcp47

    if len(chunk) == 1:
        raw = detected_langs[0] if detected_langs else None
        d_lang = normalize_detected_bcp47(raw) if raw else None
        item, text = chunk[0]
        if d_lang:
            return LanguageValidationDecision(target_bcp47=d_lang, result_chunk=[(item, text)])
        return LanguageValidationDecision(target_bcp47=canon_target, result_chunk=list(chunk))

    matching: list[tuple[Any, str]] = []
    requeues: list[LangRequeueAction] = []
    for idx, raw in enumerate(detected_langs):
        item, text = chunk[idx]
        d_lang = normalize_detected_bcp47(raw) if raw else None
        if d_lang and not grammar_bcp47_tags_match(d_lang, canon_target):
            requeues.append(LangRequeueAction(item, text, d_lang, target_bcp47))
        else:
            matching.append((item, text))
    return LanguageValidationDecision(target_bcp47=canon_target, result_chunk=matching, requeues=tuple(requeues))


@dataclass(frozen=True)
class GrammarCompletionDecision:
    requeue_all: bool
    apply_locale_after_success: bool


def decide_grammar_completion(
    chunk_len: int,
    results_len: int,
    bcp47: str,
    original_bcp47: str,
) -> GrammarCompletionDecision:
    """Whether to requeue the whole chunk or process results (and apply locale) after grammar LLM."""
    if results_len != chunk_len:
        return GrammarCompletionDecision(requeue_all=True, apply_locale_after_success=False)
    apply_locale = bool(original_bcp47 and not grammar_bcp47_tags_match(original_bcp47, bcp47))
    return GrammarCompletionDecision(requeue_all=False, apply_locale_after_success=apply_locale)


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


def _obs_language_validation_decision(
    chunk: list[tuple[GrammarWorkItem, str]],
    target_bcp47: str,
    detected: list[str | None],
    decision: LanguageValidationDecision,
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


def _run_language_validation(
    chunk: list[tuple[GrammarWorkItem, str]],
    target_bcp47: str,
    detect_lang_instruction: str,
    ec: GrammarWorkerContext,
) -> LanguageValidationDecision | None:
    """Optional phase: detect language, filter chunk, requeue mismatches. None on failure."""
    try:
        # Do not treat embedded grammar rows as proof of CharLocale — persistence is keyed by
        # sentence text only, so wrong-locale cache would skip real detection.
        detected = detect_languages_for_chunk(
            chunk, detect_lang_instruction, ec, trust_persisted_grammar_as_lang=False
        )
        decision = decide_language_validation(chunk, target_bcp47, detected)
        _obs_language_validation_decision(chunk, target_bcp47, detected, decision)
        for rq in decision.requeues:
            log.info("[grammar] Language mismatch detected: %s vs %s. Triggering locale change.", rq.new_bcp47, rq.original_bcp47)
            # The default placeholder must not store a clean row for the
            # CharLocale just rejected. The next doProofreading cache-hits
            # that empty result and never requeues, so the sentence is never
            # retagged. cache_put_sentence(original_bcp47, text, []) before
            # the detected-locale check records the rejected locale as a good
            # sentence. Requeue under the detected locale instead, and do not
            # record the rejected one.
            requeue_individual_item(
                rq.item, rq.text, rq.new_bcp47, rq.original_bcp47, ec, cache_placeholder=False
            )
        if len(chunk) == 1 and decision.target_bcp47 != target_bcp47:
            log.info("[grammar] Single item language mismatch: %s -> %s. Proceeding with new locale.", target_bcp47, decision.target_bcp47)
        return decision
    except Exception as e:
        log.exception("[grammar] Language validation error")
        emit_grammar_status("failed", "Language detection", result=str(e))
        canon_target = normalize_detected_bcp47(target_bcp47) or target_bcp47
        return LanguageValidationDecision(target_bcp47=canon_target, result_chunk=list(chunk), requeues=())


def requeue_individual_item(
    item: GrammarWorkItem,
    text: str,
    new_bcp47: str,
    original_bcp47: str,
    ec: GrammarWorkerContext,
    *,
    cache_placeholder: bool = False,
) -> None:
    """Requeue one item after language mismatch or grammar batch count mismatch.

    Do not store a clean sentence for a locale this call just rejected.
    ``cache_placeholder`` remains for an explicit caller; the default is off
    so a CharLocale cache hit cannot skip the retag.
    """
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


def _worker_batch_gates(ctx: Any, items: list[GrammarWorkItem]) -> bool:
    """Return False when the batch should not run (grammar off or agent pause)."""
    item_provider = items[0].provider if items and items[0].provider else ""
    if not item_provider and not is_grammar_enabled():
        grammar_obs("worker_batch_skip", reason="grammar_disabled", item_count=len(items))
        return False
    pause_during_agent = config.get_config_bool_safe("doc.grammar_proofreader_pause_during_agent")
    if pause_during_agent and send_cancellation.is_agent_active():
        # Local engines do not share the LLM request lane; only pause the LLM provider.
        active_prov = item_provider or get_grammar_provider()
        if active_prov not in ("harper", "languagetool", "vale"):
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
        if gq and gq.inflight_superseded(item.inflight_key, item.enqueue_seq) is True:
            grammar_obs("worker_skip", reason="superseded_before_process", enqueue_seq=item.enqueue_seq, inflight_key=item.inflight_key)
            continue
        get_kwargs: dict[str, Any] = {"ctx": ctx, "doc_id": item.doc_id}
        cache_ident = _item_cache_identity(item)
        if cache_ident:
            get_kwargs["checker_identity"] = cache_ident
        cached = grammar_proofread_cache.cache_get_sentence(grammar_bcp47, text, **get_kwargs)
        if cached is None:
            ready.append((item, text))
        else:
            orig_bcp47 = item.original_bcp47 or getattr(item, "grammar_bcp47", "")
            if orig_bcp47 and not grammar_bcp47_tags_match(orig_bcp47, grammar_bcp47):
                applied = apply_language_change(
                    ctx,
                    item.doc_id,
                    item.text,
                    grammar_bcp47,
                    start_pos=getattr(item, "n_start", 0),
                    paragraph_text=getattr(item, "paragraph_text", ""),
                )
                if not applied:
                    grammar_obs("apply_language_change_skip", doc_id=item.doc_id, sentence=item.text, bcp47=grammar_bcp47)
    return ready


def _worker_collect_valid_items(
    items: list[GrammarWorkItem],
    gq: Any,
    grammar_bcp47: str,
    ctx: Any,
) -> list[tuple[GrammarWorkItem, str]]:
    chunk = [(item, cache_lookup_text(item)) for item in items]
    return _worker_ready_items(chunk, gq, grammar_bcp47, ctx)


def _detect_lang_instruction(detect_items: list[tuple[GrammarWorkItem, str]]) -> str:
    """Prompt suffix listing the locales already recorded for the document plus the item's own."""
    doc_locales = grammar_persistence.grammar_registry.get_doc_locales(detect_items[0][0].doc_id)
    locales_in_use = sorted(doc_locales | {detect_items[0][0].grammar_bcp47})
    return f" Choose from the following locales currently used in the document, or provide a new one if none match: {', '.join(locales_in_use)}."


def _retarget_item_locale(
    item: GrammarWorkItem,
    text: str,
    target_bcp47: str,
    grammar_bcp47: str,
    ec: GrammarWorkerContext,
) -> GrammarWorkItem | None:
    new_key = grammar_proofread_locale.grammar_inflight_key(
        item.doc_id, target_bcp47, text, not item.partial_sentence
    )
    new_seq = next_enqueue_seq()
    if ec.gq is not None and ec.gq.note_inflight_generation(new_key, new_seq) is True:
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
        original_bcp47=item.original_bcp47 or grammar_bcp47,
    )


def _worker_process_chunk(
    chunk: list[tuple[GrammarWorkItem, str]],
    ec: GrammarWorkerContext,
    grammar_bcp47: str,
    detect_lang_enabled: bool,
    detect_lang_instruction: str,
    original_bcp47: str = "",
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
                new_item = _retarget_item_locale(item, text, current_bcp47, grammar_bcp47, ec)
                if new_item is not None:
                    updated_chunk.append((new_item, text))
            current_chunk = updated_chunk
            if not current_chunk:
                return

    current_chunk = _worker_ready_items(current_chunk, ec.gq, current_bcp47, ec.ctx)
    if not current_chunk:
        return

    orig_bcp = original_bcp47 or (current_chunk[0][0].original_bcp47 if current_chunk else "") or ec.original_bcp47 or grammar_bcp47
    run_grammar_check(current_chunk, current_bcp47, orig_bcp, ec)


def run_llm_and_cache_batch(
    items: list[GrammarWorkItem],
    *,
    grammar_queue_instance: Any | None = None,
    original_bcp47: str = "",
) -> None:
    """Process a batch of items (ideally from one paragraph): LLM requests + multi-sentence cache writes."""
    if not items:
        return

    ctx = items[0].ctx
    grammar_bcp47 = items[0].grammar_bcp47
    gq_to_use = grammar_queue_instance or grammar_queue
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

        provider = (items[0].provider if items and items[0].provider else "") or get_grammar_provider()
        # Local engines never need LlmClient / model_fetcher (and must not import framework.client package).
        local_provider = provider in ("harper", "languagetool", "vale")

        max_tok = grammar_proofread_locale.grammar_max_tokens(ctx)
        model = ""
        client: Any = None
        if not local_provider:
            ident = items[0].checker_identity if items and items[0].checker_identity else ""
            if ident.startswith("llm:"):
                model = ident[4:]
            if not model:
                import importlib

                model_fetcher = importlib.import_module("plugin.framework.client.model_fetcher")
                model = model_fetcher.get_grammar_model()
            client = _get_or_create_thread_llm_client(config.get_api_config(), ctx)

        batch_size = 1 if local_provider else grammar_proofread_locale.grammar_batch_sentences(ctx)
        detect_lang_mode = "off" if local_provider else grammar_proofread_locale.get_grammar_detect_language_mode(ctx)
        detect_lang_enabled = detect_lang_mode != "off"

        ec = GrammarWorkerContext(
            ctx=ctx,
            client=client,
            gq=gq_to_use,
            model=model,
            original_bcp47=original_bcp47,
            grammar_bcp47=grammar_bcp47,
            max_tok=max_tok,
            detect_lang_instruction="",
            detect_lang_mode=detect_lang_mode,
        )

        def _chunkify(items_list: list[tuple[GrammarWorkItem, str]]) -> list[list[tuple[GrammarWorkItem, str]]]:
            if len(items_list) > 1 and batch_size > 1:
                return [items_list[i : i + batch_size] for i in range(0, len(items_list), batch_size)]
            return [[it] for it in items_list]

        plain_items: list[tuple[GrammarWorkItem, str]] = []
        detect_items: list[tuple[GrammarWorkItem, str]] = []
        if detect_lang_enabled:
            for item, text in valid_items:
                if item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text):
                    plain_items.append((item, text))
                else:
                    detect_items.append((item, text))
        else:
            plain_items = list(valid_items)

        for chunk in _chunkify(plain_items):
            _worker_process_chunk(chunk, ec, grammar_bcp47, False, "", original_bcp47=original_bcp47)

        if detect_items:
            detect_lang_instruction = _detect_lang_instruction(detect_items)

            items_by_target_locale: dict[str, list[tuple[GrammarWorkItem, str]]] = defaultdict(list)
            detect_chunk_size = grammar_proofread_locale.GRAMMAR_BATCH_MAX_SENTENCES
            for i in range(0, len(detect_items), detect_chunk_size):
                d_chunk = detect_items[i : i + detect_chunk_size]
                detected_langs = detect_languages_for_chunk(d_chunk, detect_lang_instruction, ec)
                for idx, (item, text) in enumerate(d_chunk):
                    raw_lang = detected_langs[idx] if idx < len(detected_langs) else None
                    decision = decide_language_validation([(item, text)], grammar_bcp47, [raw_lang])
                    _obs_language_validation_decision([(item, text)], grammar_bcp47, [raw_lang], decision)
                    target_bcp47 = decision.target_bcp47
                    if target_bcp47 != grammar_bcp47:
                        new_item = _retarget_item_locale(item, text, target_bcp47, grammar_bcp47, ec)
                        if new_item is not None:
                            items_by_target_locale[target_bcp47].append((new_item, text))
                    else:
                        items_by_target_locale[target_bcp47].append((item, text))

            for loc, loc_items in items_by_target_locale.items():
                ready_items = _worker_ready_items(loc_items, ec.gq, loc, ec.ctx)
                if not ready_items:
                    continue
                for chunk in _chunkify(ready_items):
                    _worker_process_chunk(chunk, ec, loc, False, "", original_bcp47=grammar_bcp47)

    except Exception as e:
        log.exception("[grammar] worker batch failed")
        emit_grammar_status("failed", "Batch processing", result=type(e).__name__)
    finally:
        if status_cycle_started:
            gq_to_use.end_status_cycle()


