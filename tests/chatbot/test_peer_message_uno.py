"""UNO tests for A1 peer messaging: addressing, missing deck, inject/queue."""

from __future__ import annotations

import sys
from contextlib import contextmanager

from plugin.doc.live_panels import register_live_panel, unregister_live_panel
import plugin.doc.peer_message as pm
from plugin.doc.peer_message import (
    PeerQueue,
    SendPeerWork,
    drop_listener_queue,
    kick_pending_peer_starts,
    resolve_peer_target,
)
from plugin.framework.async_drain_guard import drain_owner_scope, reset_sentry_state
from plugin.framework.tool import ToolContext
from plugin.framework.uno_context import get_desktop, get_runtime_uid
from plugin.testing_runner import _progress_verbose as _progress, native_test
from plugin.tests.testing_utils import (
    TestingFactory,
    _draw_family_raw_close,
    close_draw_family_doc,
    settle_after_draw_family_close,
)


class _Listener:
    def __init__(self):
        self.session = _Session()
        self.injected = []
        self.started = []
        self.sidebar_state = _SendState()

    def is_busy(self) -> bool:
        return bool(self.sidebar_state.send.is_busy)

    def in_chat_mode(self) -> bool:
        return True

    def inject_user_turn(self, text: str) -> bool:
        if self.session is None:
            return False
        self.session.add_user_message(text)
        self.injected.append(text)
        return True

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
    # Log factory URL so a faulthandler dump shows whether a hang is on load or
    # on teardown after a prior Impress close (swriter vs simpress).
    _progress("peer_message_uno: load start %s" % factory_url)
    desktop = get_desktop(ctx)
    doc = desktop.loadComponentFromURL(factory_url, "_blank", 0, (_hidden_prop(),))
    _progress("peer_message_uno: load done %s" % factory_url)
    return doc


# Set after the first Windows Impress raw close in this file. A second
# ``close(True)`` can exit soffice; the runner recycles at suite end instead.
_windows_impress_raw_closed = False


def _windows_skip_impress_close() -> bool:
    """True after this suite already raw-closed one Impress on Windows.

    The first Impress raw close must run; leftover Impress before another Writer
    load can hang the factory. Skip only a second raw close in this file, when
    recycle will tear down soffice anyway. Leftover Impress after that skip is
    acceptable because this is the last test in the module.
    """
    return _windows_impress_raw_closed


def _windows_skip_doc_close() -> bool:
    """True when this suite must not call Writer/Calc ``close_doc``.

    On Windows, dual hidden-Writer ``close_doc`` calls can hang at ``doc.close``.
    Drop the proxy instead of ``close_doc`` for Writer/Calc here. POSIX still
    uses the harness ``close_doc`` path.
    """
    return sys.platform == "win32"


def _doc_uid(doc) -> str:
    try:
        return str(getattr(doc, "RuntimeUID", None) or "?")
    except Exception:
        return "?"


def _close(doc):
    """Close Writer/Calc via harness ``close_doc`` (GC + 50 ms, then ``doc.close``).

    POSIX uses the same path as ``@with_native_doc``. On Windows, drop the proxy
    instead of ``close_doc``. Progress logs name uid and phase so hangs are
    identifiable. Impress teardown is ``_teardown_peer_pair``; do not fold
    draw-family settle into ``close_doc``.
    """
    if doc is None:
        return None
    uid = _doc_uid(doc)
    if _windows_skip_doc_close():
        _progress("peer_message_uno: skip close (windows) uid=%s" % uid)
        # Leftover hidden docs poison later suites' close_doc (34544965319).
        from plugin.testing_runner import current_office

        current_office().request_recycle()
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

    On Windows, close Impress with a bare ``close(True)`` while Writer stays
    open, drop the proxy, post-close settle, and re-activate Writer. Do **not**
    ``close_doc`` the sibling Writer afterward—leftover Impress, not the keeper
    Writer, wedges the next factory load. ``_close`` skips Writer/Calc
    ``close_doc`` on win32; skip a second Impress raw close when it would exit
    soffice and request runner recycle instead. POSIX closes Writer first, then
    the Draw-family path (setModified + pre-close settle + ``close(True)``) with
    the same post-close settle. Harness-only; not a product fix.
    """
    had_impress = impress is not None
    if had_impress and _draw_family_raw_close():
        from plugin.testing_runner import current_office

        if _windows_skip_impress_close():
            # A second raw close can exit soffice; skip and recycle instead.
            _progress("peer_message_uno: skip second impress close (windows)")
            current_office().request_recycle()
            return None, None
        _progress("peer_message_uno: close impress start")
        close_draw_family_doc(impress)
        impress = None
        _progress("peer_message_uno: close impress done")
        settle_after_draw_family_close()
        _progress("peer_message_uno: impress post-close settle done")
        _reactivate_writer_after_impress(ctx, writer)
        # Drop the proxy only; ``close_doc`` on the keeper Writer can hang.
        # Request recycle so later suites do not inherit a wedged office.
        global _windows_impress_raw_closed
        _windows_impress_raw_closed = True
        current_office().request_recycle()
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


@contextmanager
def _fresh_peer_queue():
    orig = pm.peer_queue
    pm.peer_queue = PeerQueue()
    try:
        yield
    finally:
        pm.peer_queue = orig


@native_test
def test_peer_missing_deck_is_clear_error(ctx):
    with _fresh_peer_queue():
        reset_sentry_state()
        writer = None
        other = None
        try:
            writer = _load(ctx, "private:factory/swriter")
            other = _load(ctx, "private:factory/swriter")
            other_uid = get_runtime_uid(other)
            tool = SendPeerWork()
            result = tool.execute(_tool_ctx(ctx, writer), document_url=other_uid, message="Hello peer")
            assert result["status"] == "error"
            assert result["code"] == "PEER_SIDEBAR_NOT_OPEN"
            assert "sidebar" in result["message"].lower()
        finally:
            _close(other)
            _close(writer)


def _assert_inject_defers_and_kicks(ctx, writer, target_uid: str, message: str) -> None:
    listener = _Listener()
    panel = type("P", (), {"send_listener": listener})()
    register_live_panel(target_uid, panel)
    try:
        tool = SendPeerWork()
        with drain_owner_scope("stream"):
            result = tool.execute(_tool_ctx(ctx, writer), document_url=target_uid, message=message)
            assert result["status"] == "ok"
            assert result["accepted"] is True
            assert listener.started == []
            assert listener.session.messages
            assert "[Peer work from:" in listener.session.messages[0]["content"]
        kick_pending_peer_starts()
        assert listener.started
        assert listener.started[0][1] is True
    finally:
        drop_listener_queue(listener)
        unregister_live_panel(target_uid, panel)


@native_test
def test_peer_inject_defers_until_drain_idle(ctx):
    with _fresh_peer_queue():
        reset_sentry_state()
        writer = None
        other = None
        try:
            writer = _load(ctx, "private:factory/swriter")
            other = _load(ctx, "private:factory/swriter")
            other_uid = get_runtime_uid(other)
            _assert_inject_defers_and_kicks(ctx, writer, other_uid, "Compute Q4")
        finally:
            _close(other)
            _close(writer)
            reset_sentry_state()


@native_test
def test_peer_busy_queues_then_starts(ctx):
    with _fresh_peer_queue():
        reset_sentry_state()
        writer = None
        other = None
        panel = None
        other_uid = ""
        listener = _Listener()
        try:
            writer = _load(ctx, "private:factory/swriter")
            other = _load(ctx, "private:factory/swriter")
            other_uid = get_runtime_uid(other)
            panel = type("P", (), {"send_listener": listener})()
            register_live_panel(other_uid, panel)
            listener.sidebar_state.send.is_busy = True
            tool = SendPeerWork()
            result = tool.execute(_tool_ctx(ctx, writer), document_url=other_uid, message="Queued")
            assert result["status"] == "ok"
            assert listener.started == []
            assert pm.peer_queue.pending(listener) == 1
            listener.sidebar_state.send.is_busy = False
            kick_pending_peer_starts()
            assert listener.started
        finally:
            drop_listener_queue(listener)
            if other_uid and panel is not None:
                unregister_live_panel(other_uid, panel)
            _close(other)
            _close(writer)
            reset_sentry_state()


@native_test
def test_peer_unique_name_and_self_reject(ctx):
    with _fresh_peer_queue():
        import tempfile

        import uno

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


# Windows: skip all Writer/Calc close_doc in this file (34544965319).
# Impress still runs last. Teardown raw-closes the first Impress; a
# second close killed soffice (34547869791) so that one is skipped.
# The runner recycles office after the suite.


@native_test
def test_peer_impress_resolves_as_v1_peer(ctx):
    """Writer can resolve a live Impress model and inject a queued turn."""
    with _fresh_peer_queue():
        from plugin.doc.peer_message import v1_peer_type_label

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
            assert code is None, msg
            assert model is not None
            assert get_runtime_uid(model) == impress_uid
            assert v1_peer_type_label(model) == "impress"
            _assert_inject_defers_and_kicks(ctx, writer, impress_uid, "Add a title slide")
        finally:
            writer, impress = _teardown_peer_pair(writer, impress, ctx)
            reset_sentry_state()


@native_test
def test_peer_catalog_labels_impress_explicitly(ctx):
    """get_open_documents still labels Impress as draw; v1 catalog uses type=impress."""
    with _fresh_peer_queue():
        from plugin.doc.document_research import get_open_documents
        from plugin.doc.peer_message import list_v1_peers

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
            peer = next((p for p in peers if p.get("uid") == impress_uid), None)
            assert peer is not None, peers
            assert peer.get("type") == "impress"
        finally:
            writer, impress = _teardown_peer_pair(writer, impress, ctx)
