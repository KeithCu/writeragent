# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Packet F (thin): HTTP 500 / 429 then hello on a live chat sidebar."""

from __future__ import annotations

import os
import time

from plugin.testing_runner import native_test, setup, teardown

from tests.chatbot.mock_llm_harness import (
    start_mock_sidebar_session,
    stop_mock_sidebar_session,
)

_session = None


def _ensure_writer_doc(ctx) -> None:
    from plugin.chatbot.sidebar_test_hooks import current_component, desktop_from_ctx
    from plugin.doc.doc_type import is_writer

    doc = current_component(ctx)
    if doc is not None and is_writer(doc):
        return
    desktop_from_ctx(ctx).loadComponentFromURL("private:factory/swriter", "_default", 0, ())
    time.sleep(1.0)


@setup
def _setup_mock(ctx):
    global _session
    import unittest

    from plugin.framework.config import init_config

    if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") != "1":
        raise unittest.SkipTest("use make test-mock-sidebar (LibreOffice user profile)")

    init_config(ctx)
    # Import hooks before later panel creates; factory WeakSet also sees panels
    # that were wired before this module loaded (debug-only).
    import plugin.chatbot.sidebar_test_hooks  # noqa: F401

    from plugin.chatbot.sidebar_test_hooks import (
        adopt_runtime_send_listeners,
        ensure_sidebar_chat_mode,
        send_listener,
        wait_for_chat_dialog_controls,
    )

    _ensure_writer_doc(ctx)
    # Point writeragent.json at the mock *before* showing the deck so the live
    # OXT send path is not still using the user's real endpoint.
    _session = start_mock_sidebar_session(delay_ms=20, offline=True)
    controls = wait_for_chat_dialog_controls(ctx, timeout=20.0)
    adopt_runtime_send_listeners()
    sl = send_listener()
    if sl is None and controls is None:
        from plugin.chatbot.sidebar_test_hooks import current_component, sidebar_deck_names

        names = []
        try:
            names = sidebar_deck_names(ctx, current_component(ctx))
        except Exception:
            names = []
        raise AssertionError(
            "WriterAgent chat sidebar not wired after showing WriterAgentDeck "
            "(View → Sidebar must be on). decks=%s" % (names,)
        )
    ensure_sidebar_chat_mode(controls)
    _session.controls = controls
    _session.listener = sl


@teardown
def _teardown_mock():
    global _session
    from plugin.chatbot.sidebar_test_hooks import press_stop, send_state

    from plugin.chatbot.sidebar_test_hooks import send_listener

    sl = send_listener()
    if sl is not None:
        try:
            if send_state(listener=sl).is_busy:
                press_stop(listener=sl)
        except Exception:
            pass
    stop_mock_sidebar_session(_session)
    _session = None


def _control_text(ctrl) -> str:
    try:
        if hasattr(ctrl, "getText"):
            return str(ctrl.getText() or "")
        model = ctrl.getModel()
        return str(getattr(model, "Text", "") or "")
    except Exception:
        return ""


def _transcript() -> str:
    sl = getattr(_session, "listener", None)
    if sl is not None:
        from plugin.chatbot.sidebar_test_hooks import transcript_text

        return transcript_text(listener=sl)
    controls = getattr(_session, "controls", None) or {}
    for name in ("response_rich", "response"):
        if name in controls:
            return _control_text(controls[name])
    return ""


def _send_and_wait(text: str, timeout: float = 60.0, *, wait_for: str | None = None):
    from plugin.chatbot.sidebar_test_hooks import (
        press_send,
        set_query_text,
        set_query_text_via_controls,
        uno_click,
        wait_controls_send_finished,
        wait_idle,
    )

    before = _transcript()
    sl = getattr(_session, "listener", None)
    if sl is not None:
        set_query_text(text, listener=sl)
        press_send(listener=sl)
        assert wait_idle(listener=sl, timeout=timeout), "send did not go idle: %r" % text
        if wait_for:
            body = _transcript()
            suffix = body[len(before) :] if body.startswith(before) else body
            assert wait_for.lower() in suffix.lower() or wait_for.lower() in body.lower(), (
                "after send %r expected %r in %r" % (text, wait_for, body[-500:])
            )
        return sl
    controls = getattr(_session, "controls", None)
    assert controls is not None, "no SendButtonListener and no chat dialog controls"
    set_query_text_via_controls(controls, text)
    time.sleep(0.2)
    uno_click(controls["send"])
    assert wait_controls_send_finished(
        controls,
        timeout=timeout,
        transcript_fn=_transcript,
        wait_for=wait_for,
        before=before,
    ), "send did not finish: %r transcript=%r" % (text, _transcript()[-500:])
    return None


def _hello_ok() -> None:
    sl = getattr(_session, "listener", None)
    before = _transcript()
    if sl is not None:
        from plugin.chatbot.sidebar_test_hooks import next_hello_ok

        assert next_hello_ok(listener=sl, timeout=60.0), "recovery hello failed"
        return
    # User line is "hello"; require mock HTML body ("mock" appears in every template).
    _send_and_wait("hello", wait_for="mock")
    body = _transcript()
    suffix = body[len(before) :] if body.startswith(before) else body
    blob = suffix if suffix else body
    assert "mock" in blob.lower() or "<p" in blob.lower() or "<ul" in blob.lower(), (
        "hello reply missing: %r" % body[-400:]
    )


@native_test
def test_f1_crash_the_stream_then_hello(ctx):
    _send_and_wait("crash the stream", wait_for="API error")
    body = _transcript()
    assert "[API error:" in body or "HTTP Error 500" in body or "500" in body, "F1 expected 500 in transcript, got %r" % body[-500:]
    _hello_ok()


@native_test
def test_f2_rate_limit_then_hello(ctx):
    _send_and_wait("crash the stream", wait_for="API error")
    before_429 = _transcript()
    _send_and_wait("rate limit", wait_for="429")
    body = _transcript()
    assert "429" in body or "Rate limited" in body, "F2 expected 429 in transcript, got %r" % body[-500:]
    if "[API error:" in before_429 or "HTTP Error 500" in before_429:
        assert "[API error:" in body or "HTTP Error 500" in body
    _hello_ok()


@native_test
def test_f14_429_then_immediate_hello(ctx):
    _send_and_wait("error 429", wait_for="429")
    _hello_ok()
