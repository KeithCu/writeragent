# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar observability helpers and C10 batch_stats counters."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.locale import grammar_obs as go
from plugin.writer.locale.grammar_obs import slice_preview_debug
from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkItem,
    GrammarWorkQueue,
)


@pytest.fixture(autouse=True)
def _reset_status_indicator():
    go.set_grammar_status(None)
    yield
    go.set_grammar_status(None)


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


def test_filter_stale_and_group_drops_stale_items() -> None:
    items = [_item(key="a", seq=1), _item(key="b", seq=2)]
    q = GrammarWorkQueue()
    q._latest_seq["a"] = 2
    groups = q.filter_stale_and_group(items)
    assert groups == {("d1", "en-US", items[1].provider, items[1].checker_identity): [items[1]]}


def test_sidebar_grammar_status_emits_event_bus_payload() -> None:
    mock_bus = MagicMock()
    sink = go.SidebarGrammarStatus()
    with patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus):
        sink.start("Hello world.", result="queued")
    mock_bus.emit.assert_called_once_with(
        "grammar:status",
        phase="start",
        preview="Hello worl\u2026",
        length=12,
        result="queued",
        elapsed_ms=None,
    )


def test_sidebar_grammar_status_swallows_event_bus_failure() -> None:
    mock_bus = MagicMock()
    sink = go.SidebarGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus),
        patch.object(go.log, "debug") as mock_debug,
    ):
        mock_bus.emit.side_effect = RuntimeError("bus unavailable")
        sink.failed("Hi.")
    mock_debug.assert_called_once()
    assert "status emit failed" in mock_debug.call_args[0][0]


def test_sidebar_grammar_status_harper_worker_emits_request_payload() -> None:
    mock_bus = MagicMock()
    sink = go.SidebarGrammarStatus()
    with patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus):
        sink.request("They is here.", result="Downloading harper-ls v2.7.0…", preview_source="They is here.")
    mock_bus.emit.assert_called_once_with(
        "grammar:status",
        phase="request",
        preview="They is he\u2026",
        length=13,
        result="Downloading harper-ls v2.7.0…",
        elapsed_ms=None,
    )


def test_status_bar_grammar_status_done_routes_to_status_bar() -> None:
    sink = go.StatusBarGrammarStatus()
    mock_indicator = MagicMock()
    sink._indicator = mock_indicator
    mock_frame = MagicMock()
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *args: fn(*args) if callable(fn) else None),
    ):
        sink.done("Hello world.", result="clean")
    mock_indicator.setText.assert_called_with("LibreHarper: Grammar check complete")
    mock_indicator.end.assert_called_once()


def test_status_bar_grammar_status_always_posts_to_main_thread() -> None:
    """Linguistic workers can look like VCL; never paint XStatusIndicator inline."""
    sink = go.StatusBarGrammarStatus()
    mock_indicator = MagicMock()
    mock_frame = MagicMock()
    mock_frame.createStatusIndicator.return_value = mock_indicator
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *args: fn(*args) if callable(fn) else None) as mock_post,
    ):
        sink.start("Hello world.", result="Starting Harper…")
    assert mock_post.called
    mock_indicator.start.assert_called_once()


def test_status_bar_grammar_status_skips_routine_libreharper_paints() -> None:
    sink = go.StatusBarGrammarStatus()
    for result in ("steady-state", "another-routine", "enqueue-marker"):
        with (
            patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
            patch.object(sink, "_paint") as mock_paint,
            patch("plugin.framework.queue_executor.main.post") as mock_post,
        ):
            sink.start("Hello world.", result=result, routine=True)
        mock_paint.assert_not_called()
        mock_post.assert_not_called()

    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch.object(sink, "_paint") as mock_paint,
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.done("Hello world.", result="terminal routine summary", routine=True)
    mock_paint.assert_not_called()
    mock_post.assert_not_called()


def test_status_bar_grammar_status_posts_routine_done_when_indicator_active() -> None:
    sink = go.StatusBarGrammarStatus()
    mock_indicator = MagicMock()
    sink._indicator = mock_indicator
    mock_frame = MagicMock()
    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *args: fn(*args) if callable(fn) else None) as mock_post,
    ):
        sink.done("Hello world.", result="routine terminal", routine=True)
    assert mock_post.called
    mock_indicator.end.assert_called_once()
    assert sink._indicator is None


def test_status_bar_routine_flag_ignores_result_text() -> None:
    sink = go.StatusBarGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.done("Hello world.", result="0 issues")
    assert mock_post.call_count >= 1

    with patch("plugin.framework.queue_executor.main.post") as mock_post:
        sink.done("Hello world.", result="0 issues", routine=True)
    mock_post.assert_not_called()

    mock_indicator = MagicMock()
    sink._indicator = mock_indicator
    with patch(
        "plugin.framework.queue_executor.main.post",
        side_effect=lambda fn, *args: fn(*args) if callable(fn) else None,
    ):
        sink.done("Hello world.", result="0 issues", routine=True)
    mock_indicator.end.assert_called_once()
    mock_indicator.setText.assert_not_called()
    assert sink._indicator is None

    with patch("plugin.framework.queue_executor.main.post") as mock_post:
        sink.request("Hi.", result="", routine=True)
    mock_post.assert_not_called()

    with patch("plugin.framework.queue_executor.main.post") as mock_post:
        sink.request("Hi.", result="")
    assert mock_post.call_count >= 1

    with patch(
        "plugin.framework.queue_executor.main.post",
        side_effect=lambda fn, *args: fn(*args) if callable(fn) else None,
    ) as mock_post:
        sink.failed("Hi.", result="")
    mock_post.assert_called()


def test_status_bar_grammar_status_paints_progress_and_failure() -> None:
    cases = (
        ("request", "Downloading harper-ls v2.7.0…"),
        ("request", "Starting harper-ls…"),
        ("failed", "timed out"),
    )
    for phase, result in cases:
        sink = go.StatusBarGrammarStatus()
        mock_indicator = MagicMock()
        mock_frame = MagicMock()
        mock_frame.createStatusIndicator.return_value = mock_indicator
        with (
            patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
            patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
            patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
            patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *args: fn(*args) if callable(fn) else None) as mock_post,
        ):
            if phase == "request":
                sink.request("Hello world.", result=result)
            else:
                sink.failed("Hello world.", result=result)
        assert mock_post.called
        assert mock_frame.createStatusIndicator.called


def test_status_bar_grammar_status_skips_post_on_no_vcl_helper() -> None:
    sink = go.StatusBarGrammarStatus()
    go.set_grammar_status(sink)
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch.object(go, "desktop_create_is_unsafe", return_value=True),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.request("Hello world.", result="Starting Harper…")
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
    mock_persistence.model = mock_model
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


def test_status_bar_paint_skips_paint_when_frame_missing() -> None:
    sink = go.StatusBarGrammarStatus()
    go.set_grammar_status(sink)
    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=None),
        patch("plugin.framework.uno_context.get_desktop") as mock_desktop,
        patch("plugin.framework.uno_context.get_active_document") as mock_doc,
    ):
        sink._paint("request", "Downloading harper-ls v2.7.0…")
        mock_desktop.assert_not_called()
        mock_doc.assert_not_called()
        assert sink._indicator is None


def test_status_bar_paint_ends_indicator_on_routine_done() -> None:
    sink = go.StatusBarGrammarStatus()
    go.set_grammar_status(sink)
    mock_indicator = MagicMock()
    mock_frame = MagicMock()
    mock_frame.createStatusIndicator.return_value = mock_indicator

    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
    ):
        sink._paint("start", "Downloading harper-ls v2.7.0…")
        assert sink._indicator is mock_indicator
        mock_indicator.setText.reset_mock()

        sink._paint("done", "2 issues", routine=True)
        mock_indicator.end.assert_called_once()
        mock_indicator.setText.assert_not_called()
        assert sink._indicator is None


def test_status_bar_paint_routine_done_without_indicator_does_nothing() -> None:
    sink = go.StatusBarGrammarStatus()
    mock_frame = MagicMock()
    with patch.object(go, "resolve_status_bar_frame", return_value=mock_frame) as mock_resolve:
        sink._paint("done", "0 issues", routine=True)
        mock_resolve.assert_not_called()
        mock_frame.createStatusIndicator.assert_not_called()


@pytest.mark.parametrize(
    "result_text",
    ["timed out", ""],
    ids=["with_reason", "empty_reason"],
)
def test_status_bar_paint_shows_failure_without_indicator(result_text: str) -> None:
    sink = go.StatusBarGrammarStatus()
    go.set_grammar_status(sink)
    mock_indicator = MagicMock()
    mock_frame = MagicMock()
    mock_frame.createStatusIndicator.return_value = mock_indicator

    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
    ):
        sink._paint("failed", result_text)
        mock_indicator.start.assert_called_once()
        assert sink._indicator is mock_indicator
        mock_indicator.end.assert_not_called()


def test_status_bar_paint_failure_updates_active_indicator_without_ending() -> None:
    sink = go.StatusBarGrammarStatus()
    mock_indicator = MagicMock()
    sink._indicator = mock_indicator
    go.set_grammar_status(sink)
    mock_frame = MagicMock()

    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
    ):
        sink._paint("failed", "timed out")
        mock_indicator.setText.assert_called_once()
        mock_indicator.setValue.assert_called_once_with(100)
        mock_indicator.end.assert_not_called()


def test_status_bar_paint_lifecycle() -> None:
    sink = go.StatusBarGrammarStatus()
    go.set_grammar_status(sink)
    mock_indicator = MagicMock()
    mock_frame = MagicMock()
    mock_frame.createStatusIndicator.return_value = mock_indicator

    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=mock_frame),
    ):
        # 1. Start phase (progress, not the per-keystroke "queued" string)
        sink._paint("start", "Downloading harper-ls v2.7.0…")
        mock_frame.createStatusIndicator.assert_called_once()
        mock_indicator.start.assert_called_once()

        # 2. Request phase updates text/value
        sink._paint("request", "Starting harper-ls…")
        mock_indicator.setText.assert_called()
        mock_indicator.setValue.assert_called_with(50)

        # 3. Done phase ends indicator
        sink._paint("done", "done")
        mock_indicator.setText.assert_called()
        mock_indicator.end.assert_called_once()
        assert sink._indicator is None


def test_status_bar_paint_drops_dead_indicator() -> None:
    """A disposed indicator must not stick across later progress paints."""
    sink = go.StatusBarGrammarStatus()
    dead = MagicMock()
    dead.setText.side_effect = RuntimeError("disposed")
    fresh = MagicMock()
    frame = MagicMock()
    frame.createStatusIndicator.return_value = fresh
    sink._indicator = dead
    go.set_grammar_status(sink)
    with (
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=frame),
    ):
        sink._paint("request", "Starting harper-ls…")
        assert sink._indicator is None
        sink._paint("start", "Downloading harper-ls v2.7.0…")
        frame.createStatusIndicator.assert_called_once()
        assert sink._indicator is fresh


def test_routine_libreharper_status_does_not_call_slice_preview_debug() -> None:
    sink = go.StatusBarGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.writer.locale.grammar_obs.slice_preview_debug") as mock_preview,
    ):
        sink.request("Some text.", result="harper check", routine=True)
    mock_preview.assert_not_called()


def test_status_bar_grammar_status_posts_paint_and_repaint() -> None:
    sink = go.StatusBarGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
        patch("plugin.framework.queue_executor.main.call") as mock_execute,
    ):
        sink.request("Some text.", result="Downloading harper-ls v2.7.0…")

    assert mock_post.called
    mock_execute.assert_not_called()


def test_status_bar_grammar_status_skips_post_when_routine_or_unsafe() -> None:
    sink = go.StatusBarGrammarStatus()
    # Routine status with no indicator active
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.request("Some text.", result="harper check", routine=True)
    assert mock_post.call_count == 0

    # Unsafe desktop create
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch.object(go, "desktop_create_is_unsafe", return_value=True),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.request("Some text.", result="Downloading harper-ls v2.7.0…")
    assert mock_post.call_count == 0


def test_writer_agent_grammar_status_is_composite_when_not_libreharper() -> None:
    with patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=False):
        sink = go.get_grammar_status()
    assert isinstance(sink, go.WriterAgentGrammarStatus)


def test_writer_agent_failed_paints_status_bar_with_subscriber() -> None:
    mock_bus = MagicMock()
    mock_bus.has_subscribers.return_value = True
    sink = go.WriterAgentGrammarStatus()
    mock_indicator = MagicMock()
    sink._status_bar._indicator = mock_indicator
    with (
        patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus),
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=MagicMock()),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *args: fn(*args) if callable(fn) else None),
    ):
        sink.failed("Harper", result="boom")
    mock_bus.emit.assert_called_once()
    mock_indicator.setText.assert_called()


def test_writer_agent_request_skips_status_bar_when_subscriber_present() -> None:
    mock_bus = MagicMock()
    mock_bus.has_subscribers.return_value = True
    sink = go.WriterAgentGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.request("Hello.", result="checking", routine=False)
    mock_post.assert_not_called()


def test_writer_agent_request_paints_status_bar_without_subscriber() -> None:
    mock_bus = MagicMock()
    mock_bus.has_subscribers.return_value = False
    sink = go.WriterAgentGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus),
        patch("plugin.writer.locale.grammar_obs.get_ctx", return_value=MagicMock()),
        patch.object(go, "resolve_status_bar_frame", return_value=MagicMock()),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *args: fn(*args) if callable(fn) else None),
    ):
        sink.request("Hello.", result="checking", routine=False)
    mock_bus.emit.assert_called_once()


def test_writer_agent_routine_request_does_not_paint_status_bar() -> None:
    mock_bus = MagicMock()
    mock_bus.has_subscribers.return_value = False
    sink = go.WriterAgentGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.get_event_bus", return_value=mock_bus),
        patch("plugin.framework.queue_executor.main.post") as mock_post,
    ):
        sink.request("Hello.", result="checking", routine=True)
    mock_post.assert_not_called()


def test_status_bar_grammar_status_swallows_post_errors() -> None:
    sink = go.StatusBarGrammarStatus()
    with (
        patch("plugin.writer.locale.grammar_obs.is_libreharper", return_value=True),
        patch("plugin.framework.queue_executor.main.post", side_effect=RuntimeError("no AsyncCallback")),
    ):
        sink.request("Some text.", result="Downloading harper-ls v2.7.0…")  # must not raise


