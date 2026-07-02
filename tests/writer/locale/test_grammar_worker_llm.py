# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar worker LLM orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_worker_llm import (
    build_grammar_system_prompt,
    call_grammar_llm,
    detect_languages_for_chunk,
    language_detect_llm_sync,
)
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem


def _item(text: str = "They is here.") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=MagicMock(),
        text=text,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="d1",
        inflight_key="k1",
        enqueue_seq=1,
    )


def _ec(client: MagicMock | None = None, *, detect_lang_mode: str = "llm") -> MagicMock:
    ec = MagicMock()
    ec.ctx = MagicMock()
    ec.client = client or MagicMock()
    ec.model = "test-model"
    ec.max_tok = 512
    ec.detect_lang_mode = detect_lang_mode
    ec.grammar_bcp47 = "en-US"
    return ec


def test_build_grammar_system_prompt_batch_vs_single() -> None:
    batch = build_grammar_system_prompt("en-US", set(), batch=True, any_partial=False)
    single = build_grammar_system_prompt("en-US", set(), batch=False, any_partial=False)
    assert "multiple sentences" in batch
    assert "single JSON object" in single


def test_call_grammar_llm_single() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = '{"errors": []}'
    item = _item()
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker_llm.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker_llm.get_active_ignored_reasons", return_value=set()):
        results, elapsed = call_grammar_llm([(item, item.text)], "en-US", ec)
    assert len(results) == 1
    assert elapsed >= 0
    args, kwargs = client.chat_completion_sync.call_args
    assert args[0][1]["content"] == item.text


def test_call_grammar_llm_batch() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = '{"results": [{"errors": []}, {"errors": []}]}'
    a, b = _item("A."), _item("B.")
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker_llm.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker_llm.get_active_ignored_reasons", return_value=set()):
        results, _ = call_grammar_llm([(a, a.text), (b, b.text)], "en-US", ec)
    assert len(results) == 2
    args, _ = client.chat_completion_sync.call_args
    assert "1. A.\n2. B." in args[0][1]["content"]


def test_call_grammar_llm_passes_minimal_reasoning() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = '{"errors": []}'
    item = _item()
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker_llm.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker_llm.get_active_ignored_reasons", return_value=set()):
        call_grammar_llm([(item, item.text)], "en-US", ec)
    _, kwargs = client.chat_completion_sync.call_args
    assert kwargs.get("chat_extra") == {"reasoning": {"effort": "minimal"}}


def test_call_grammar_llm_empty_single_returns_clean_result() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = ""
    item = _item()
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker_llm.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker_llm.get_active_ignored_reasons", return_value=set()):
        results, _ = call_grammar_llm([(item, item.text)], "en-US", ec)
    assert len(results) == 1
    assert results[0] == []
    assert client.chat_completion_sync.call_count == 1


def test_language_detect_llm_sync_retries_on_empty() -> None:
    client = MagicMock()
    client.chat_completion_sync.side_effect = ["", '{"detected_language_bcp47": "en-US"}']
    ec = _ec(client)
    with patch("plugin.framework.queue_executor.grammar_llm_request_gate"):
        out = language_detect_llm_sync(ec, [{"role": "user", "content": "Hi"}], 64)
    assert "en-US" in out
    assert client.chat_completion_sync.call_count == 2
    assert client.chat_completion_sync.call_args_list[1].kwargs["max_tokens"] >= 256


def test_detect_languages_for_chunk_langdetect_mode() -> None:
    item = _item("Bonjour le monde.")
    ec = _ec(detect_lang_mode="langdetect")
    with patch("plugin.writer.locale.grammar_worker_llm.get_cached_language", return_value=None), \
         patch("plugin.writer.locale.grammar_worker_llm.persisted_grammar_skip_lang_detect", return_value=False), \
         patch("plugin.framework.client.langdetect_service.detect_languages", return_value=["fr-FR"]), \
         patch("plugin.writer.locale.grammar_worker_llm.emit_grammar_status"):
        detected = detect_languages_for_chunk([(item, item.text)], "", ec)
    ec.client.chat_completion_sync.assert_not_called()
    assert detected == ["fr-FR"]
