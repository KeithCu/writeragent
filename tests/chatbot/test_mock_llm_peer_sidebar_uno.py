# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Packet P: dual mock-LLM sidebars locking specialized-inner peer messaging (#673).

Run via ``make test-mock-sidebar FILTER=P`` (visible soffice, user profile).
Opens Calc with ``open_calc_document`` (E12 / #674: VCL-posted factory, ``_blank``).
"""

from __future__ import annotations

import os
import time
import unittest
from typing import Any

from plugin.testing_runner import native_test, setup, teardown

from tests.chatbot.mock_llm_harness import (
    calc_total_formula,
    find_open_writer,
    finish_immediately_after_peer_sends,
    open_calc_for_dual_sidebar,
    seed_budget_sheet,
    start_mock_sidebar_session,
    stop_mock_sidebar_session,
)

_session = None
_calc_doc = None
_writer_doc = None
_open_path = ""
_CALC_OPEN_SKIP = (
    "Packet P: open_calc_document did not yield a Calc WriterAgent deck "
    "(E12 follow-up: URP hang after Writer deck is farmed separately). "
    "Unit tests in tests/scripts/test_mock_llm_server.py and "
    "tests/doc/test_peer_message.py still lock finish-after-accepted, "
    "reply-via-specialized, and busy-then-queue."
)


def _require_user_profile() -> None:
    if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") != "1":
        raise unittest.SkipTest("use make test-mock-sidebar (LibreOffice user profile)")


@setup
def _setup_peer(ctx):
    global _session, _calc_doc, _writer_doc, _open_path
    from plugin.framework.config import init_config

    _require_user_profile()
    init_config(ctx)
    from plugin.chatbot.chat_sidebar_mode import mark_librarian_invoked
    from plugin.chatbot.memory import MemoryStore
    from plugin.framework.config import get_config_bool, set_config

    mark_librarian_invoked()
    try:
        MemoryStore(ctx).write("user", "# Test User Profile\n")
    except Exception:
        pass
    if get_config_bool("chatbot.prompt_for_web_research"):
        set_config("chatbot.prompt_for_web_research", False)

    import plugin.chatbot.sidebar_test_hooks  # noqa: F401

    from plugin.chatbot.sidebar_test_hooks import (
        adopt_chat_sidebar,
        desktop_from_ctx,
        ensure_sidebar_chat_mode,
    )
    from plugin.doc.doc_type import is_writer
    from plugin.framework.uno_context import get_runtime_uid

    # Point JSON at the mock before showing either deck.
    _session = start_mock_sidebar_session(delay_ms=20, offline=True)
    _session.prompt_research_cleared = True

    # #674 recipe: Writer deck first so OPEN_CALC posts onto that QueueExecutor.
    writer = find_open_writer(ctx)
    if writer is None:
        writer = desktop_from_ctx(ctx).loadComponentFromURL("private:factory/swriter", "_default", 0, ())
        time.sleep(1.0)
    _writer_doc = writer if writer is not None and is_writer(writer) else None
    writer_controls, writer_listener = adopt_chat_sidebar(ctx, _writer_doc)
    ensure_sidebar_chat_mode(writer_controls, doc_type="writer", listener=writer_listener)

    _calc_doc = open_calc_for_dual_sidebar(ctx, timeout=20.0)
    _open_path = "open_calc_document" if _calc_doc is not None else ""
    calc_controls = None
    calc_listener = None
    if _calc_doc is not None:
        seed_budget_sheet(_calc_doc)
        try:
            _calc_doc.setTitle("BudgetPeer.ods")
        except Exception:
            pass
        calc_controls, calc_listener = adopt_chat_sidebar(ctx, _calc_doc)
        ensure_sidebar_chat_mode(calc_controls, doc_type="calc", listener=calc_listener)

    _session.writer_doc = _writer_doc
    _session.calc_doc = _calc_doc
    _session.writer_controls = writer_controls
    _session.calc_controls = calc_controls
    # Cache listeners once. Re-resolving send_listener_for_doc in a wait loop
    # URP-hangs after the peer kick (getFrame during the extracted send).
    _session.writer_listener = writer_listener
    _session.calc_listener = calc_listener
    _session.hook_ctx = ctx
    _session.writer_uid = get_runtime_uid(_writer_doc) if _writer_doc is not None else ""
    _session.calc_uid = get_runtime_uid(_calc_doc) if _calc_doc is not None else ""
    _session.open_path = _open_path


@teardown
def _teardown_peer():
    global _session, _calc_doc, _writer_doc, _open_path
    from plugin.chatbot.sidebar_test_hooks import press_stop, send_listener_for_doc, send_state

    for doc in (_writer_doc, _calc_doc):
        sl = send_listener_for_doc(doc) if doc is not None else None
        if sl is None:
            continue
        try:
            if send_state(listener=sl).is_busy:
                press_stop(listener=sl)
        except Exception:
            pass
    stop_mock_sidebar_session(_session)
    from plugin.chatbot.sidebar_test_hooks import close_component

    close_component(_calc_doc)
    _session = None
    _calc_doc = None
    _writer_doc = None
    _open_path = ""


def _skip_without_dual() -> None:
    if _session is None or getattr(_session, "calc_doc", None) is None:
        raise unittest.SkipTest(_CALC_OPEN_SKIP)
    w = getattr(_session, "writer_listener", None)
    c = getattr(_session, "calc_listener", None)
    if w is None or c is None:
        if getattr(_session, "calc_controls", None) is None:
            raise unittest.SkipTest(_CALC_OPEN_SKIP)


def _listener(which: str):
    return getattr(_session, "writer_listener" if which == "writer" else "calc_listener", None)


def _controls(which: str):
    return getattr(_session, "writer_controls" if which == "writer" else "calc_controls", None)


def _transcript(which: str) -> str:
    """Read cached dialog text. Do not resolve listeners here (URP hang)."""
    controls = _controls(which) or {}
    for name in ("response_rich", "response"):
        ctrl = controls.get(name)
        if ctrl is None:
            continue
        try:
            if hasattr(ctrl, "getText"):
                text = str(ctrl.getText() or "")
                if text:
                    return text
            text = str(getattr(ctrl.getModel(), "Text", "") or "")
            if text:
                return text
        except Exception:
            continue
    sl = _listener(which)
    if sl is None:
        return ""
    from plugin.chatbot.sidebar_test_hooks import transcript_text

    try:
        return transcript_text(listener=sl)
    except Exception:
        return ""


def _send(which: str, text: str, timeout: float = 90.0) -> None:
    from plugin.chatbot.sidebar_test_hooks import (
        press_send,
        set_query_text,
        set_query_text_via_controls,
        uno_click,
        wait_controls_send_finished,
        wait_idle,
    )

    sl = _listener(which)
    controls = _controls(which)
    before = _transcript(which)
    # Prefer dialog click over listener (URP hang if we wait on listener.is_busy).
    if controls is None and sl is not None:
        set_query_text(text, listener=sl)
        press_send(listener=sl)
        assert wait_idle(listener=sl, timeout=timeout), "%s send did not go idle: %r" % (which, text)
        return
    assert controls is not None, "no listener or controls for %s" % which
    set_query_text_via_controls(controls, text)
    time.sleep(0.2)
    uno_click(controls["send"])
    finished = wait_controls_send_finished(
        controls,
        timeout=min(timeout, 25.0),
        transcript_fn=lambda: _transcript(which),
        before=before,
    )
    body = _transcript(which)
    if not finished and "[delegate" in body and ": done]" in body:
        # Wrapup HTML / Stop Enabled can lag after specialized_workflow_finished.
        return
    assert finished, "%s send did not finish: %r" % (which, body[-400:])


def _focus_doc(which: str) -> None:
    """Bring that document's frame forward so the next Send hits the right deck."""
    from plugin.chatbot.sidebar_test_hooks import desktop_from_ctx

    doc = _writer_doc if which == "writer" else _calc_doc
    if doc is None:
        return
    try:
        frame = doc.getCurrentController().getFrame()
        frame.getContainerWindow().toFront()
        ctx = getattr(_session, "hook_ctx", None)
        if ctx is not None:
            desktop_from_ctx(ctx).setActiveFrame(frame)
    except Exception:
        pass


def _click_send(which: str, text: str) -> None:
    """Fire Send without waiting (P3: start Writer ramble while Calc replies)."""
    from plugin.chatbot.sidebar_test_hooks import (
        press_send,
        set_query_text,
        set_query_text_via_controls,
        uno_click,
    )

    _focus_doc(which)
    sl = _listener(which)
    controls = _controls(which)
    if sl is not None:
        try:
            set_query_text(text, listener=sl)
            press_send(listener=sl)
            return
        except Exception:
            pass
    assert controls is not None, "no listener or controls for %s" % which
    set_query_text_via_controls(controls, text)
    time.sleep(0.15)
    uno_click(controls["send"])


def _wait_calc_envelope(timeout: float = 60.0) -> bool:
    """Writer Readys before the peer drain starts — both look idle for a beat."""
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() <= deadline:
        if "[Peer work from:" in _transcript("calc"):
            return True
        if _is_busy("calc"):
            time.sleep(0.15)
            continue
        time.sleep(0.15)
    return "[Peer work from:" in _transcript("calc")


def _wait_both_idle(timeout: float = 90.0) -> bool:
    from plugin.chatbot.sidebar_test_hooks import wait_controls_send_finished, wait_idle

    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        ok = True
        for which in ("writer", "calc"):
            sl = _listener(which)
            controls = _controls(which)
            # Prefer dialog Enabled over listener.is_busy (URP hang during peer drain).
            if controls is not None:
                if not wait_controls_send_finished(controls, timeout=0.4, transcript_fn=lambda w=which: _transcript(w)):
                    ok = False
                    break
            elif sl is not None:
                if not wait_idle(listener=sl, timeout=0.4):
                    ok = False
                    break
        if ok:
            return True
        time.sleep(0.2)
    return False


def _is_busy(which: str) -> bool:
    from plugin.chatbot.sidebar_test_hooks import control_enabled, send_state

    controls = _controls(which) or {}
    if controls.get("stop") is not None:
        en = control_enabled(controls.get("stop"))
        if en is not None:
            return en is True
    sl = _listener(which)
    if sl is not None:
        return bool(send_state(listener=sl).is_busy)
    return False


def _press_stop(which: str) -> None:
    # uno_click(Stop) URP-hangs if a drain is wedged. Packet G STOP_CLICKED posts to VCL.
    from plugin.chatbot.sidebar_test_hooks import execute_debug_sidebar_op, press_stop

    sl = _listener(which)
    if sl is not None:
        try:
            press_stop(listener=sl)
            return
        except Exception:
            pass
    ctx = getattr(_session, "hook_ctx", None)
    if ctx is not None:
        try:
            execute_debug_sidebar_op("STOP_CLICKED", ctx=ctx)
        except Exception:
            pass


def _captures() -> list[dict[str, Any]]:
    from scripts.mock_llm_server import snapshot_captures

    assert _session is not None
    return snapshot_captures(_session.config)


def _clear_captures() -> None:
    from scripts.mock_llm_server import clear_captures

    clear_captures(_session.config)


def _kick_pending_in_soffice(ctx) -> None:
    """Start queued extracted sends in soffice (test-process kick is a no-op)."""
    from plugin.chatbot.sidebar_test_hooks import execute_debug_sidebar_op

    try:
        execute_debug_sidebar_op("KICK_PEERS", ctx=ctx)
    except Exception:
        pass


def _clear_chat(which: str) -> None:
    """URP-safe Clear: click the cached control (listener.session.clear is a no-op proxy)."""
    from plugin.chatbot.sidebar_test_hooks import clear_sidebar_chat, uno_click

    controls = _controls(which) or {}
    clear_btn = controls.get("clear")
    if clear_btn is not None:
        try:
            uno_click(clear_btn)
            time.sleep(0.15)
            return
        except Exception:
            pass
    sl = _listener(which)
    if sl is not None:
        clear_sidebar_chat(listener=sl)


def _quiesce_dual(ctx) -> None:
    """Stop leftover turns (P2 hang / inject-now) so the next case starts idle."""
    _press_stop("writer")
    _press_stop("calc")
    _kick_pending_in_soffice(ctx)
    _wait_both_idle(timeout=15.0)
    _press_stop("writer")
    _press_stop("calc")
    _wait_both_idle(timeout=8.0)
    for which in ("writer", "calc"):
        _clear_chat(which)
    _clear_captures()


def _capture_tools() -> list[list[str]]:
    return [list(row.get("decided_tools") or []) for row in _captures()]


def _outer_advertised_send_peer() -> bool:
    """True if a main-chat POST (no specialized finish tool) advertised a peer send tool."""
    for row in _captures():
        advertised = set(row.get("advertised_tools") or [])
        if "send_peer_work" not in advertised and "send_peer_result" not in advertised:
            continue
        if "specialized_workflow_finished" in advertised or "final_answer" in advertised:
            continue
        return True
    return False


@native_test
def test_p1_total_row_peer_roundtrip(ctx):
    """Writer → document_research → send → finish; Calc writes Total and replies."""
    _skip_without_dual()
    from plugin.chatbot.sidebar_test_hooks import clear_sidebar_chat

    assert _session is not None
    _session.config.scenario = "none"
    _session.config.peer_wait_after_accepted = False
    _press_stop("writer")
    _press_stop("calc")
    _clear_captures()
    for which in ("writer", "calc"):
        sl = _listener(which)
        if sl is not None:
            clear_sidebar_chat(listener=sl)

    _send("writer", "Ask the budget workbook to add a Total row", timeout=90.0)
    # Inject-now / start-later: Writer is Ready before Calc's extracted send begins.
    time.sleep(0.6)
    _kick_pending_in_soffice(ctx)
    assert _wait_calc_envelope(timeout=60.0), (
        "Calc never received the envelope after Writer Ready: writer_uid=%s calc_uid=%s "
        "writer_busy=%s calc_busy=%s decided=%r writer=%r calc=%r"
        % (
            getattr(_session, "writer_uid", ""),
            getattr(_session, "calc_uid", ""),
            _is_busy("writer"),
            _is_busy("calc"),
            _capture_tools(),
            _transcript("writer")[-300:],
            _transcript("calc")[-300:],
        )
    )
    # First idle beat is Writer Ready; Calc reply + Writer follow-up start later.
    deadline = time.monotonic() + 90.0
    while time.monotonic() <= deadline:
        decided = [name for row in _capture_tools() for name in row]
        peer_sends = sum(1 for n in decided if n in ("send_peer_work", "send_peer_result"))
        if peer_sends >= 2 and (
            "write_formula_range" in decided or "delegate_to_specialized_calc_toolset" in decided
        ):
            _wait_both_idle(timeout=8.0)
            break
        time.sleep(0.25)
    _wait_both_idle(timeout=20.0)
    writer_txt = _transcript("writer")
    calc_txt = _transcript("calc")
    assert "[Peer work from:" in calc_txt, "Calc never received the envelope: %r" % calc_txt[-400:]
    formula = calc_total_formula(_session.calc_doc)
    assert "SUM" in formula.upper() or "Total" in calc_txt or "total" in writer_txt.lower(), (
        "Calc did not write a Total row: formula=%r calc=%r" % (formula, calc_txt[-300:])
    )
    assert "[Peer work from:" in writer_txt or "Total" in writer_txt or "total" in writer_txt.lower(), (
        "Writer follow-up never saw the reply: %r" % writer_txt[-400:]
    )
    snaps = _captures()
    assert finish_immediately_after_peer_sends(snaps), "specialized did not finish immediately after accepted: %r" % [
        row.get("decided_tools") for row in snaps
    ]
    assert not _outer_advertised_send_peer(), "outer main advertised peer send tools: %r" % [
        row.get("advertised_tools") for row in snaps
        if "send_peer_work" in (row.get("advertised_tools") or [])
        or "send_peer_result" in (row.get("advertised_tools") or [])
    ]
    decided = [name for row in snaps for name in (row.get("decided_tools") or [])]
    assert "send_peer_work" in decided or "send_peer_result" in decided
    assert "write_formula_range" in decided or "SUM" in formula.upper()
    assert "delegate_to_specialized_writer_toolset" in decided
    assert "delegate_to_specialized_calc_toolset" in decided


@native_test
def test_p2_wait_after_accepted_deadlocks_peer(ctx):
    """If specialized does not finish after accepted, Calc never starts (Scrolly hang)."""
    _skip_without_dual()
    from plugin.chatbot.sidebar_test_hooks import clear_sidebar_chat

    assert _session is not None
    _session.config.scenario = "peer_wait"
    _session.config.peer_wait_after_accepted = True
    if _is_busy("writer"):
        _press_stop("writer")
    if _is_busy("calc"):
        _press_stop("calc")
    time.sleep(0.5)
    _clear_captures()
    for which in ("writer", "calc"):
        sl = _listener(which)
        if sl is not None:
            clear_sidebar_chat(listener=sl)

    sl = _listener("writer")
    controls = _controls("writer")
    from plugin.chatbot.sidebar_test_hooks import press_send, set_query_text, set_query_text_via_controls, uno_click

    if sl is not None:
        set_query_text("wait after accepted then hang", listener=sl)
        press_send(listener=sl)
    else:
        assert controls is not None
        set_query_text_via_controls(controls, "wait after accepted then hang")
        time.sleep(0.2)
        uno_click(controls["send"])

    # Inject-now may paint [Peer work from:] on Calc immediately. Lock the hang:
    # while specialized keeps calling discovery (not finish), Calc must not
    # start write_formula_range. Do not wait for max_steps — that Readys Writer
    # and finally-kicks the peer.
    deadline = time.monotonic() + 4.0
    saw_send = False
    saw_wait_loop = False
    while time.monotonic() <= deadline:
        decided = _capture_tools()
        flat = [name for row in decided for name in row]
        if "send_peer_work" in flat or "send_peer_result" in flat:
            saw_send = True
        if saw_send and "list_nearby_files" in flat and "specialized_workflow_finished" not in flat:
            saw_wait_loop = True
            break
        time.sleep(0.12)
    snaps = _captures()
    decided = [row.get("decided_tools") or [] for row in snaps]
    finished_after_send = finish_immediately_after_peer_sends(snaps)
    formula_started = any("write_formula_range" in row for row in decided)
    try:
        assert saw_send, "specialized never called a peer send tool: %r" % decided
        assert saw_wait_loop or _is_busy("writer"), (
            "specialized did not stay in the wait loop after accepted: decided=%r" % decided
        )
        assert not formula_started, (
            "peer started (write_formula_range) while specialized waited: decided=%r" % decided
        )
        assert not finished_after_send
    finally:
        _session.config.scenario = "none"
        _session.config.peer_wait_after_accepted = False
        _press_stop("writer")
        _press_stop("calc")
        # Consume the inject-now envelope so P3 does not inherit a Calc reply.
        _kick_pending_in_soffice(ctx)
        _wait_both_idle(timeout=12.0)
        _press_stop("writer")
        _press_stop("calc")
        time.sleep(0.3)


@native_test
def test_p3_writer_busy_queues_calc_reply(ctx):
    """Writer ramble before Calc reply: reply queues, then starts after Ready."""
    _skip_without_dual()

    assert _session is not None
    _session.config.scenario = "none"
    _session.config.peer_wait_after_accepted = False
    # SSE ramble stays Stop-enabled; nested Calc POSTs (stream=False) need
    # sync_delay or they finish in milliseconds after Writer wrapup.
    _session.config.delay_ms = 120
    _session.config.sync_delay_ms = 3500
    _quiesce_dual(ctx)

    # Fire first ask and wait for true Ready (Stop disabled). Breaking on
    # delegate-done while wrapup still owns Stop makes the ramble Send a no-op.
    _click_send("writer", "Ask the budget workbook to add a Total row")
    deadline = time.monotonic() + 45.0
    writer_ready = False
    while time.monotonic() <= deadline:
        decided = [name for row in _capture_tools() for name in row]
        if (("send_peer_work" in decided or "send_peer_result" in decided) and not _is_busy("writer")):
            writer_ready = True
            break
        time.sleep(0.08)
    assert writer_ready, "Writer first ask never finished: decided=%r writer=%r" % (
        _capture_tools(),
        _transcript("writer")[-300:],
    )
    ready_txt = _transcript("writer")
    assert "Total row written" not in ready_txt, (
        "Calc already replied while Writer was idle after first ask: %r" % ready_txt[-300:]
    )
    sends_at_ready = sum(1 for row in _capture_tools() for name in row if name in ("send_peer_work", "send_peer_result"))

    # hang the stream half-closes the socket (Writer Readys immediately).
    # keep talking streams ~200 chunks at delay_ms so Stop stays enabled.
    _click_send("writer", "keep talking")
    ramble_started = time.monotonic()
    deadline = ramble_started + 6.0
    ramble_on = False
    clicked_again = False
    while time.monotonic() <= deadline:
        queries = [str(row.get("current_query") or "") for row in _captures()]
        if _is_busy("writer") and any("keep talking" in q.lower() for q in queries):
            ramble_on = True
            break
        if not clicked_again and time.monotonic() - ramble_started > 1.2 and not _is_busy("writer"):
            _click_send("writer", "keep talking")
            clicked_again = True
        time.sleep(0.08)
    if not (ramble_on and _is_busy("writer")):
        raise unittest.SkipTest(
            "P3 live: Writer ramble Send did not start after Ready "
            "(dual-deck URP). Units test_p3_* still lock busy-then-queue."
        )
    busy_txt = _transcript("writer")
    _kick_pending_in_soffice(ctx)

    deadline = time.monotonic() + 60.0
    calc_replied = False
    injected_mid = False
    saw_mid = False
    writer_txt = busy_txt
    while time.monotonic() <= deadline:
        writer_txt = _transcript("writer")
        finished = "word199" in writer_txt
        # Dialog getText is often a tail — do not require word0.
        mid = (not finished) and ("word" in writer_txt)
        if mid:
            saw_mid = True
            if "Total row written" in writer_txt or writer_txt.count("[Peer work from:") > busy_txt.count("[Peer work from:"):
                injected_mid = True
                break
        sends = sum(1 for row in _capture_tools() for name in row if name in ("send_peer_work", "send_peer_result"))
        if sends > sends_at_ready:
            calc_replied = True
            if injected_mid or finished or saw_mid:
                # Give execute a beat after decide, but do not wait past word199.
                time.sleep(0.35)
                writer_txt = _transcript("writer")
                if (not finished) and "word" in writer_txt and (
                    "Total row written" in writer_txt
                    or writer_txt.count("[Peer work from:") > busy_txt.count("[Peer work from:")
                ):
                    injected_mid = True
                break
        time.sleep(0.12)
    try:
        assert calc_replied, "Calc never sent the reply: decided=%r" % _capture_tools()
        assert saw_mid, "Writer ramble never painted mid-stream: %r" % writer_txt[-300:]
        assert not injected_mid, (
            "Calc reply injected while Writer ramble was still streaming: %r" % writer_txt[-300:]
        )
        decided = [name for row in _capture_tools() for name in row]
        assert "apply_document_content" not in decided
    finally:
        _session.config.delay_ms = 20
        _session.config.sync_delay_ms = None
        _press_stop("writer")
        time.sleep(0.4)
        _kick_pending_in_soffice(ctx)

    deadline = time.monotonic() + 45.0
    drained = False
    while time.monotonic() <= deadline:
        writer_txt = _transcript("writer")
        decided = [name for row in _capture_tools() for name in row]
        if (
            "Total row written" in writer_txt
            or writer_txt.count("[Peer work from:") > busy_txt.count("[Peer work from:")
            or "apply_document_content" in decided
        ):
            drained = True
            break
        time.sleep(0.25)
    _wait_both_idle(timeout=30.0)
    writer_txt = _transcript("writer")
    decided = [name for row in _capture_tools() for name in row]
    assert drained or "[Peer work from:" in writer_txt or "Total" in writer_txt, (
        "queued Calc reply never started after Writer Ready: writer=%r decided=%r"
        % (writer_txt[-300:], decided)
    )
