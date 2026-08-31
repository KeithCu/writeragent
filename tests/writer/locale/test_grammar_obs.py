# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar observability helpers and C10 batch_stats counters."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.locale import grammar_obs as go
from plugin.writer.locale.grammar_proofread_text import slice_preview_debug
from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkItem,
    filter_stale_and_group,
)


def _item(*, doc_id: str = "d1", key: str = "k1", seq: int = 1, text: str = "Hello.") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=MagicMock(),
        text=text,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id=doc_id,
        inflight_key=key,
        enqueue_seq=seq,
    )


def test_slice_preview_debug_collapses_whitespace_and_truncates() -> None:
    assert slice_preview_debug("") == ""
    assert slice_preview_debug("  one   two  ") == "one two"
    long_text = "word " * 40
    preview = slice_preview_debug(long_text, max_len=20)
    assert len(preview) == 21
    assert preview.endswith("\u2026")


def test_grammar_obs_no_op_when_debug_disabled(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="writeragent.grammar")
    go.grammar_obs("test_event", foo=1)
    assert not any("[grammar] obs" in r.message for r in caplog.records)


def test_grammar_obs_logs_when_debug_enabled() -> None:
    with patch.object(go.log, "isEnabledFor", return_value=True), patch.object(go.log, "debug") as mock_debug:
        go.grammar_obs("test_event", counter=2)
    mock_debug.assert_called_once()
    assert mock_debug.call_args[0][0] == "[grammar] obs %s %s"
    assert mock_debug.call_args[0][1] == "test_event"
    assert "counter=2" in mock_debug.call_args[0][2]


def test_filter_stale_and_group_emits_batch_stats_for_stale_skips() -> None:
    items = [_item(key="a", seq=1), _item(key="b", seq=2)]
    with patch("plugin.writer.locale.grammar_work_queue.grammar_obs") as mock_obs:
        groups = filter_stale_and_group(items, lambda it: it.inflight_key == "a")
    assert groups == {("d1", "en-US"): [items[1]]}
    mock_obs.assert_any_call("batch_stats", sentences_stale_skipped=1, survivor_count=1)
    mock_obs.assert_any_call("queue_stale_skip", doc_id="d1", locale="en-US", seq=1, inflight_key="a")


def test_filter_stale_and_group_no_batch_stats_when_nothing_stale() -> None:
    items = [_item(key="a", seq=1)]
    with patch("plugin.writer.locale.grammar_work_queue.grammar_obs") as mock_obs:
        filter_stale_and_group(items, lambda _: False)
    assert not any(call.args and call.args[0] == "batch_stats" for call in mock_obs.call_args_list)


def test_emit_grammar_status_emits_event_bus_payload() -> None:
    with patch("plugin.writer.locale.grammar_obs.event_bus.global_event_bus") as mock_bus:
        go.emit_grammar_status("start", "Hello world.", result="queued", preview_source="Hello world.")
    mock_bus.emit.assert_called_once_with(
        "grammar:status",
        phase="start",
        preview="Hello worl\u2026",
        length=12,
        result="queued",
        elapsed_ms=None,
    )


def test_emit_grammar_status_swallows_event_bus_failure() -> None:
    with (
        patch("plugin.writer.locale.grammar_obs.event_bus.global_event_bus") as mock_bus,
        patch.object(go.log, "debug") as mock_debug,
    ):
        mock_bus.emit.side_effect = RuntimeError("bus unavailable")
        go.emit_grammar_status("failed", "Hi.")
    mock_debug.assert_called_once()
    assert "status emit failed" in mock_debug.call_args[0][0]


def test_emit_harper_worker_status_emits_request_payload() -> None:
    with patch("plugin.writer.locale.grammar_obs.event_bus.global_event_bus") as mock_bus:
        go.emit_harper_worker_status("They is here.", "Downloading harper-ls v2.7.0…")
    mock_bus.emit.assert_called_once_with(
        "grammar:status",
        phase="request",
        preview="They is he\u2026",
        length=13,
        result="Downloading harper-ls v2.7.0…",
        elapsed_ms=None,
    )


def test_emit_grammar_status_routes_to_libreoffice_status_bar_when_libreharper() -> None:
    with (
        patch("plugin.framework.uno_context.is_libreharper", return_value=True),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
    ):
        go.emit_grammar_status("complete", "Hello world.", result="clean")
    mock_post.assert_called_once_with(go.update_libreoffice_status_bar, "complete", "Hello world.", "clean")


def test_emit_grammar_status_updates_status_bar_inline_on_main_thread() -> None:
    with (
        patch("plugin.framework.uno_context.is_libreharper", return_value=True),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
        patch("plugin.writer.locale.grammar_obs.update_libreoffice_status_bar") as mock_bar,
        patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
    ):
        go.emit_grammar_status("start", "Hello world.", result="Harper")
    mock_bar.assert_called_once_with("start", "Hello world.", "Harper")
    mock_post.assert_not_called()


def test_update_libreoffice_status_bar_lifecycle() -> None:
    mock_indicator = MagicMock()
    mock_frame = MagicMock()
    mock_frame.createStatusIndicator.return_value = mock_indicator
    mock_doc = MagicMock()
    mock_doc.getCurrentController().getFrame.return_value = mock_frame

    with (
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.uno_context.get_active_document", return_value=mock_doc),
    ):
        # Reset any leftover global indicator
        go._last_status_indicator = None

        # 1. Start phase
        go.update_libreoffice_status_bar("start", "Text", "queued")
        mock_frame.createStatusIndicator.assert_called_once()
        mock_indicator.start.assert_called_once_with("LibreHarper: queued", 100)

        # 2. Request phase updates text/value
        go.update_libreoffice_status_bar("request", "Text", "Linting…")
        mock_indicator.setText.assert_called_with("LibreHarper: Linting…")
        mock_indicator.setValue.assert_called_with(50)

        # 3. Complete phase ends indicator
        go.update_libreoffice_status_bar("complete", "Text", "done")
        mock_indicator.setText.assert_called_with("LibreHarper: Grammar check complete")
        mock_indicator.end.assert_called_once()
        assert go._last_status_indicator is None

