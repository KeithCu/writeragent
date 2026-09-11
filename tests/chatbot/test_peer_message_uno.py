"""UNO tests for A1 peer messaging: addressing, missing deck, inject/queue."""

from __future__ import annotations

import sys

from plugin.doc.live_panels import register_live_panel, reset_live_panels, unregister_live_panel
from plugin.doc.peer_message import (
    SendPeerMessage,
    drop_listener_queue,
    kick_pending_peer_starts,
    listener_queue_len,
    reset_peer_queues,
    resolve_peer_target,
)
from plugin.framework.async_drain_guard import drain_owner_scope, reset_sentry_state
from plugin.framework.tool import ToolContext
from plugin.framework.uno_context import get_desktop, get_runtime_uid
from plugin.testing_runner import _progress, native_test
from plugin.tests.testing_utils import (
    TestingFactory,
    _draw_family_raw_close,
    close_draw_family_doc,
    settle_after_draw_family_close,
)


class _Listener:
    def __init__(self):
        self.session = _Session()
        self.appended = []
        self.started = []
        self.sidebar_state = _SendState()
        self._active_q = None
        self._send_cancellation = None
        self.chat_mode_selector = None

    def _append_response(self, text, role="assistant"):
        self.appended.append((text, role))

    def start_extracted_peer_send(self, query_text, *, already_appended):
        self.started.append((query_text, already_appended))
        return True


class _Session:
    def __init__(self):
        self.messages = []

    def add_user_message(self, content):
        self.messages.append({"role": "user", "content": content})


class _SendState:
    def __init__(self):
        self.send = type("S", (), {"is_busy": False})()


def _hidden_prop():
    import uno

    return uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)


def _load(ctx, factory_url):
    # Name the factory on stderr so a 30s faulthandler dump shows swriter vs
    # simpress. GHA 34419828920 hung at line 109 — Writer after a raw Impress
    # close, not the Impress load itself (that would be the next _load line).
    _progress("peer_message_uno: load start %s" % factory_url)
    desktop = get_desktop(ctx)
    doc = desktop.loadComponentFromURL(factory_url, "_blank", 0, (_hidden_prop(),))
    _progress("peer_message_uno: load done %s" % factory_url)
    return doc


# After the first Windows Impress raw close in this file. A second
# ``close(True)`` killed soffice (GHA 34547869791). Reset is not required
# — the suite ends and the runner recycles office.
_windows_impress_raw_closed = False


def _windows_skip_impress_close() -> bool:
    """True after this suite already raw-closed one Impress on Windows.

    GHA 34547869791: skip-close unblocked Writer+Writer. First Impress
    raw close returned (~21 ms). Second Impress raw close raised
    ``DisposedException`` after ~7 s and soffice exited 0 — fail-closed
    the catalog test and skipped remaining suites. Leftover Impress from
    a skipped *last* close is OK: this is the last test in the file and
    recycle kills soffice. Do not skip the first close — leftover
    Impress before a later Writer load hangs (34537826720).
    """
    return _windows_impress_raw_closed


def _windows_skip_doc_close() -> bool:
    """True when this suite must not call Writer/Calc ``close_doc``.

    GHA 34544965319: ``test_peer_missing_deck_is_clear_error`` ran *before*
    any Impress in this file. First ``close_doc`` (``other``, uid=30)
    returned; the second (``writer``, uid=29) hung 30s at ``doc.close``.
    Dual hidden-Writer ``close_doc`` is the hang — leftover Impress from
    *this* suite is not required. Drop the proxy on win32. POSIX still
    ``close_doc``.
    """
    return sys.platform == "win32"


def _doc_uid(doc) -> str:
    try:
        return str(getattr(doc, "RuntimeUID", None) or "?")
    except Exception:
        return "?"


def _close(doc):
    """Close Writer/Calc via harness ``close_doc`` (GC + 50 ms, then ``doc.close``).

    POSIX: same path as ``@with_native_doc``. Windows: drop the proxy —
    GHA 34544965319 hung 30s on the *second* Writer ``close_doc`` in
    ``test_peer_missing_deck_is_clear_error`` before any Impress in this
    file. Logs uid and start/done/skip so the next red dump names which
    close hung. Impress teardown is ``_teardown_peer_pair``. Do not fold
    Draw-family settle into ``close_doc``.
    """
    if doc is None:
        return None
    uid = _doc_uid(doc)
    if _windows_skip_doc_close():
        _progress("peer_message_uno: skip close (windows) uid=%s" % uid)
        # Leftover hidden docs poison later suites' close_doc (34544965319).
        from plugin.testing_runner import request_office_recycle_after_suite

        request_office_recycle_after_suite()
        return None
    _progress("peer_message_uno: close_doc start uid=%s" % uid)
    TestingFactory.close_doc(doc)
    _progress("peer_message_uno: close_doc done uid=%s" % uid)
    return None


def _reactivate_writer_after_impress(ctx, writer):
    """Point the desktop back at Writer after Impress close.

    Closing another app can leave ``getCurrentComponent()`` empty even
    while Writer remains open (box UNO proof; ``_restore_writer_after_calc``
    / #707). Re-activate before the next ``private:factory/swriter`` load
    so the factory is not racing a wedged Impress current component.
    Hidden docs may have no container window; ``setActiveFrame`` is the
    important call.
    """
    if ctx is None or writer is None:
        return
    try:
        frame = writer.getCurrentController().getFrame()
        try:
            win = frame.getContainerWindow()
            if win is not None:
                win.toFront()
        except Exception:
            pass
        get_desktop(ctx).setActiveFrame(frame)
        _progress("peer_message_uno: writer reactivated")
    except Exception as exc:
        _progress(
            "peer_message_uno: writer reactivate skipped err=%s" % type(exc).__name__
        )


def _teardown_peer_pair(writer, impress, ctx=None):
    """Close the Writer+Impress peer pair without poisoning the next load.

    What was wrong: GHA 34518091151 hung 30s in ``TestingFactory.close_doc``
    at ``doc.close(True)`` while this suite closed Impress with Writer still
    open. #710's ``settle_after_draw_family_close`` never ran. Later
    Writer-first + GC/sleep then close/dispose also hung (34532953982 /
    34535868114). Skipping close (34537826720) unblocked the first test
    then hung the next ``private:factory/swriter`` load — leftover Impress
    wedges later Writer factory loads (same family as #710).

    Why this: Windows keeps Writer open and uses a bare Impress
    ``close(True)`` (the only close that returned, #710 / 34419828920),
    drops the proxy, post-close settles, and re-activates Writer (#707).
    Do **not** then ``close_doc`` the sibling Writer — GHA 34540353452
    raw-closed Impress in ~25 ms, settle + ``setActiveFrame`` returned,
    then ``TestingFactory.close_doc`` hung 30s at Writer ``close(True)``.
    Leftover Writer is not the poison (keeper Writer already exists);
    leftover Impress is. GHA 34544965319: dual hidden-Writer
    ``close_doc`` hung *before* any Impress in this file — ``_close``
    skips all Writer/Calc ``close_doc`` on win32. GHA 34547869791:
    first Impress raw close returned; the second killed soffice
    (exited 0). Skip that second close — leftover last Impress dies
    with recycle. This path still asks the runner to recycle office
    so later suites are not poisoned. POSIX still closes Writer
    first, then the Draw-family path (setModified + pre-close settle
    + ``close(True)``) and the same post-close settle. Not a product
    fix.
    """
    had_impress = impress is not None
    if had_impress and _draw_family_raw_close():
        from plugin.testing_runner import request_office_recycle_after_suite

        if _windows_skip_impress_close():
            # Second raw close exited soffice 0 (34547869791). Last test
            # in this file — leftover Impress dies with recycle.
            _progress("peer_message_uno: skip second impress close (windows)")
            request_office_recycle_after_suite()
            return None, None
        _progress("peer_message_uno: close impress start")
        close_draw_family_doc(impress)
        impress = None
        _progress("peer_message_uno: close impress done")
        settle_after_draw_family_close()
        _progress("peer_message_uno: impress post-close settle done")
        _reactivate_writer_after_impress(ctx, writer)
        # Drop the proxy only. close_doc hung 30s here (34540353452).
        # Writer+Writer close_doc also hung before any Impress
        # (34544965319). Recycle so later suites are not poisoned.
        global _windows_impress_raw_closed
        _windows_impress_raw_closed = True
        request_office_recycle_after_suite()
        _progress("peer_message_uno: skip writer close after impress")
        return None, None
    _progress("peer_message_uno: close writer start")
    writer = _close(writer)
    _progress("peer_message_uno: close writer done")
    if had_impress:
        _progress("peer_message_uno: close impress start")
        close_draw_family_doc(impress)
        _progress("peer_message_uno: close impress done")
        settle_after_draw_family_close()
        _progress("peer_message_uno: impress post-close settle done")
    return None, None


def _tool_ctx(ctx, doc):
    return ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="chat")


@native_test
def test_peer_missing_deck_is_clear_error(ctx):
    reset_peer_queues()
    reset_live_panels()
    reset_sentry_state()
    writer = None
    other = None
    try:
        writer = _load(ctx, "private:factory/swriter")
        other = _load(ctx, "private:factory/swriter")
        other_uid = get_runtime_uid(other)
        tool = SendPeerMessage()
        result = tool.execute(_tool_ctx(ctx, writer), document_url=other_uid, message="Hello peer")
        assert result["status"] == "error"
        assert result["code"] == "PEER_SIDEBAR_NOT_OPEN"
        assert "sidebar" in result["message"].lower()
    finally:
        _close(other)
        _close(writer)
        reset_peer_queues()
        reset_live_panels()


@native_test
def test_peer_inject_defers_until_drain_idle(ctx):
    reset_peer_queues()
    reset_live_panels()
    reset_sentry_state()
    writer = None
    other = None
    listener = _Listener()
    try:
        writer = _load(ctx, "private:factory/swriter")
        other = _load(ctx, "private:factory/swriter")
        other_uid = get_runtime_uid(other)
        panel = type("P", (), {"send_listener": listener})()
        register_live_panel(other_uid, panel)
        tool = SendPeerMessage()
        with drain_owner_scope("stream"):
            result = tool.execute(_tool_ctx(ctx, writer), document_url=other_uid, message="Compute Q4")
            assert result["status"] == "ok"
            assert result["accepted"] is True
            assert listener.started == []
            assert listener.session.messages
            assert "[Peer from:" in listener.session.messages[0]["content"]
        kick_pending_peer_starts()
        assert listener.started
        assert listener.started[0][1] is True
    finally:
        drop_listener_queue(listener)
        unregister_live_panel(get_runtime_uid(other) if other is not None else "")
        _close(other)
        _close(writer)
        reset_peer_queues()
        reset_live_panels()
        reset_sentry_state()


@native_test
def test_peer_busy_queues_then_starts(ctx):
    reset_peer_queues()
    reset_live_panels()
    reset_sentry_state()
    writer = None
    other = None
    listener = _Listener()
    try:
        writer = _load(ctx, "private:factory/swriter")
        other = _load(ctx, "private:factory/swriter")
        other_uid = get_runtime_uid(other)
        panel = type("P", (), {"send_listener": listener})()
        register_live_panel(other_uid, panel)
        listener.sidebar_state.send.is_busy = True
        tool = SendPeerMessage()
        result = tool.execute(_tool_ctx(ctx, writer), document_url=other_uid, message="Queued")
        assert result["status"] == "ok"
        assert listener.started == []
        assert listener_queue_len(listener) == 1
        listener.sidebar_state.send.is_busy = False
        kick_pending_peer_starts()
        assert listener.started
    finally:
        drop_listener_queue(listener)
        _close(other)
        _close(writer)
        reset_peer_queues()
        reset_live_panels()
        reset_sentry_state()


@native_test
def test_peer_unique_name_and_self_reject(ctx):
    import tempfile

    import uno

    reset_peer_queues()
    reset_live_panels()
    writer = None
    calc = None
    try:
        writer = _load(ctx, "private:factory/swriter")
        calc = _load(ctx, "private:factory/scalc")
        from plugin.doc.peer_message import list_v1_peers

        writer_uid = get_runtime_uid(writer)
        model, code, _msg = resolve_peer_target(ctx, writer, writer_uid)
        assert model is None
        assert code == "PEER_SELF"
        # Untitled Writer + Calc both catalog as "Untitled"; save Calc so the
        # display name is unique and document_url-as-name can resolve.
        calc_path = tempfile.mkdtemp() + "/BudgetPeer.ods"
        calc.storeAsURL(uno.systemPathToFileUrl(calc_path), ())
        peers = list_v1_peers(ctx, writer)
        calc_uid = get_runtime_uid(calc)
        rec = next((p for p in peers if p.get("uid") == calc_uid), None)
        assert rec is not None, peers
        assert rec["name"] == "BudgetPeer.ods"
        model, code, _msg = resolve_peer_target(ctx, writer, rec["name"])
        assert code is None, _msg
        assert get_runtime_uid(model) == calc_uid
    finally:
        _close(calc)
        _close(writer)
        reset_peer_queues()
        reset_live_panels()


# Windows: skip all Writer/Calc close_doc in this file (34544965319).
# Impress still runs last. Teardown raw-closes the first Impress; a
# second close killed soffice (34547869791) so that one is skipped.
# The runner recycles office after the suite.


@native_test
def test_peer_impress_rejected_on_resolved_model(ctx):
    reset_peer_queues()
    reset_live_panels()
    reset_sentry_state()
    writer = None
    impress = None
    try:
        writer = _load(ctx, "private:factory/swriter")
        impress = _load(ctx, "private:factory/simpress")
        assert writer is not None and impress is not None
        impress_uid = get_runtime_uid(impress)
        assert impress_uid
        model, code, msg = resolve_peer_target(ctx, writer, impress_uid)
        assert model is None
        assert code == "PEER_UNSUPPORTED"
        assert "Impress" in msg
    finally:
        writer, impress = _teardown_peer_pair(writer, impress, ctx)
        reset_peer_queues()
        reset_live_panels()


@native_test
def test_peer_catalog_draw_label_is_not_enough_for_impress(ctx):
    """get_open_documents labels Impress as draw; v1 still rejects the model."""
    from plugin.doc.document_research import get_open_documents
    from plugin.doc.peer_message import list_v1_peers

    reset_live_panels()
    writer = None
    impress = None
    try:
        writer = _load(ctx, "private:factory/swriter")
        impress = _load(ctx, "private:factory/simpress")
        catalog = get_open_documents(ctx, writer)
        impress_uid = get_runtime_uid(impress)
        rec = next((r for r in catalog if r.get("uid") == impress_uid), None)
        assert rec is not None
        assert rec.get("doc_type") == "draw"
        peers = list_v1_peers(ctx, writer)
        assert all(p.get("uid") != impress_uid for p in peers)
    finally:
        writer, impress = _teardown_peer_pair(writer, impress, ctx)
