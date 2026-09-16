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


def test_emit_grammar_status_always_posts_to_main_thread() -> None:
    """Linguistic workers can look like VCL; never paint XStatusIndicator inline."""
    with (
        patch("plugin.framework.uno_context.is_libreharper", return_value=True),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
        patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
    ):
        go.emit_grammar_status("start", "Hello world.", result="Starting Harper…")
    mock_post.assert_called_once_with(go.update_libreoffice_status_bar, "start", "Hello world.", "Starting Harper…")


def test_is_routine_libreharper_status_covers_per_keystroke_strings() -> None:
    assert go.is_routine_libreharper_status("start", "Harper")
    assert go.is_routine_libreharper_status("request", "Harper check")
    assert go.is_routine_libreharper_status("start", "queued")
    assert go.is_routine_libreharper_status("done", "0 issues")
    assert go.is_routine_libreharper_status("done", "1 issue")
    assert go.is_routine_libreharper_status("request", "")
    assert not go.is_routine_libreharper_status("request", "Downloading harper-ls v2.7.0…")
    assert not go.is_routine_libreharper_status("request", "Starting harper-ls…")
    assert not go.is_routine_libreharper_status("request", "Starting Harper…")
    assert not go.is_routine_libreharper_status("failed", "Harper")
    assert not go.is_routine_libreharper_status("failed", "")


def test_emit_grammar_status_skips_routine_libreharper_paints() -> None:
    for result in ("Harper", "Harper check", "queued", "0 issues", "2 issues"):
        with (
            patch("plugin.framework.uno_context.is_libreharper", return_value=True),
            patch("plugin.writer.locale.grammar_obs.update_libreoffice_status_bar") as mock_bar,
            patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
        ):
            go.emit_grammar_status("start", "Hello world.", result=result)
        mock_bar.assert_not_called()
        mock_post.assert_not_called()


def test_emit_grammar_status_paints_progress_and_failure_when_libreharper() -> None:
    cases = (
        ("request", "Downloading harper-ls v2.7.0…"),
        ("request", "Starting harper-ls…"),
        ("failed", "timed out"),
    )
    for phase, result in cases:
        with (
            patch("plugin.framework.uno_context.is_libreharper", return_value=True),
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
            patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
        ):
            go.emit_grammar_status(phase, "Hello world.", result=result)
        mock_post.assert_called_once_with(go.update_libreoffice_status_bar, phase, "Hello world.", result)


def test_desktop_create_is_unsafe_for_uno_bin_helper() -> None:
    import sys

    with patch.object(sys, "argv", ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"]):
        assert go.desktop_create_is_unsafe()
    with (
        patch.object(sys, "argv", ["soffice"]),
        patch("plugin.framework.uno_context._linux_process_tokens", return_value=["/usr/lib64/libreoffice/program/soffice.bin"]),
    ):
        assert not go.desktop_create_is_unsafe()


def test_desktop_create_is_unsafe_when_pythonloader_rewrites_argv() -> None:
    """pythonloader inside uno.bin often leaves argv as '' or a .py path (#768)."""
    import sys

    proc = ["/usr/lib64/libreoffice/program/uno.bin", "--quiet", "--singleaccept"]
    with (
        patch.object(sys, "argv", [""]),
        patch("plugin.framework.uno_context._linux_process_tokens", return_value=proc),
    ):
        assert go.desktop_create_is_unsafe()


def test_emit_grammar_status_skips_post_on_no_vcl_helper() -> None:
    with (
        patch("plugin.framework.uno_context.is_libreharper", return_value=True),
        patch.object(go, "desktop_create_is_unsafe", return_value=True),
        patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
    ):
        go.emit_grammar_status("request", "Hello world.", result="Starting Harper…")
    mock_post.assert_not_called()


def test_resolve_status_bar_frame_skips_desktop_create_when_helper_or_missing() -> None:
    mock_ctx = MagicMock()
    mock_ctx.getValueByName.return_value = None
    with (
        patch.object(go, "_frame_from_bound_grammar_docs", return_value=None),
        patch.object(go, "desktop_create_is_unsafe", return_value=True),
        patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
        patch("plugin.framework.uno_context.get_active_document") as mock_doc,
    ):
        assert go.resolve_status_bar_frame(mock_ctx) is None
        mock_ctx.getValueByName.assert_not_called()
        mock_desktop.assert_not_called()
        mock_doc.assert_not_called()

    mock_ctx.reset_mock()
    with (
        patch.object(go, "_frame_from_bound_grammar_docs", return_value=None),
        patch.object(go, "desktop_create_is_unsafe", return_value=False),
        patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
        patch("plugin.framework.uno_context.get_active_document") as mock_doc,
    ):
        assert go.resolve_status_bar_frame(mock_ctx) is None
        mock_ctx.getValueByName.assert_called_once_with("/singletons/com.sun.star.frame.theDesktop")
        mock_desktop.assert_not_called()
        mock_doc.assert_not_called()


def test_frame_from_bound_grammar_docs_reads_registry_model() -> None:
    mock_frame = MagicMock()
    mock_model = MagicMock()
    mock_model.getCurrentController().getFrame.return_value = mock_frame
    mock_persistence = MagicMock()
    mock_persistence._model = mock_model
    mock_registry = MagicMock()
    mock_registry.doc_persistence_instances = {"d1": mock_persistence}
    mock_registry.lock = MagicMock()
    mock_registry.lock.__enter__.return_value = None
    mock_registry.lock.__exit__.return_value = False
    with patch("plugin.writer.locale.grammar_persistence.grammar_registry", mock_registry):
        assert go._frame_from_bound_grammar_docs() is mock_frame


def test_resolve_status_bar_frame_uses_bound_document_without_desktop() -> None:
    mock_frame = MagicMock(name="bound_frame")
    mock_ctx = MagicMock()
    with (
        patch.object(go, "_frame_from_bound_grammar_docs", return_value=mock_frame),
        patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
        patch("plugin.framework.uno_context.get_active_document") as mock_doc,
    ):
        assert go.resolve_status_bar_frame(mock_ctx) is mock_frame
        mock_ctx.getValueByName.assert_not_called()
        mock_desktop.assert_not_called()
        mock_doc.assert_not_called()


def test_resolve_status_bar_frame_uses_existing_desktop_singleton() -> None:
    mock_frame = MagicMock(name="singleton_frame")
    mock_desktop = MagicMock()
    mock_desktop.getCurrentFrame.return_value = mock_frame
    mock_ctx = MagicMock()
    mock_ctx.getValueByName.return_value = mock_desktop
    with (
        patch.object(go, "_frame_from_bound_grammar_docs", return_value=None),
        patch.object(go, "desktop_create_is_unsafe", return_value=False),
        patch("plugin.framework.uno_context.get_desktop") as mock_create,
        patch("plugin.framework.uno_context.get_active_document") as mock_doc,
    ):
        assert go.resolve_status_bar_frame(mock_ctx) is mock_frame
        mock_ctx.getValueByName.assert_called_once_with("/singletons/com.sun.star.frame.theDesktop")
        mock_create.assert_not_called()
        mock_doc.assert_not_called()


def test_update_libreoffice_status_bar_skips_paint_when_frame_missing() -> None:
    with (
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=None),
        patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
        patch("plugin.framework.uno_context.get_active_document") as mock_doc,
    ):
        go._last_status_indicator = None
        go.update_libreoffice_status_bar("request", "Text", "Downloading harper-ls v2.7.0…")
        mock_desktop.assert_not_called()
        mock_doc.assert_not_called()
        assert go._last_status_indicator is None


def test_update_libreoffice_status_bar_skips_routine_result_without_resolving_frame() -> None:
    with patch.object(go, "resolve_status_bar_frame") as mock_resolve:
        go._last_status_indicator = None
        go.update_libreoffice_status_bar("start", "Text", "Harper")
        mock_resolve.assert_not_called()


def test_update_libreoffice_status_bar_lifecycle() -> None:
    mock_indicator = MagicMock()
    mock_frame = MagicMock()
    mock_frame.createStatusIndicator.return_value = mock_indicator

    with (
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
    ):
        # Reset any leftover global indicator
        go._last_status_indicator = None

        # 1. Start phase (progress, not the per-keystroke "queued" string)
        go.update_libreoffice_status_bar("start", "Text", "Downloading harper-ls v2.7.0…")
        mock_frame.createStatusIndicator.assert_called_once()
        mock_indicator.start.assert_called_once_with("LibreHarper: Downloading harper-ls v2.7.0…", 100)

        # 2. Request phase updates text/value
        go.update_libreoffice_status_bar("request", "Text", "Starting harper-ls…")
        mock_indicator.setText.assert_called_with("LibreHarper: Starting harper-ls…")
        mock_indicator.setValue.assert_called_with(50)

        # 3. Complete phase ends indicator
        go.update_libreoffice_status_bar("complete", "Text", "done")
        mock_indicator.setText.assert_called_with("LibreHarper: Grammar check complete")
        mock_indicator.end.assert_called_once()
        assert go._last_status_indicator is None

