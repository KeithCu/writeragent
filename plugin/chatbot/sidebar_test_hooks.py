# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Debug-only sidebar hooks for mock-LLM native tests.

Release OXTs replace this module with a stub (see ``scripts/strip_code.py``).
Do not synthesize clicks: drive the same listeners as the widgets.

See docs/chat/rich-text-control-sidebar.md (Hooks).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable
from weakref import WeakSet

from plugin.chatbot.panel import StopButtonListener, notify_stop_mouse_pressed
from plugin.chatbot.send_state import SendEvent, SendEventKind
from plugin.framework.constants import EXTENSION_ID_WRITERAGENT

log = logging.getLogger("writeragent.sidebar_test_hooks")

_HOOKS_UNAVAILABLE = "sidebar test hooks are not in release builds"
_DEBUG_SIDEBAR_PREFIX = "chatbot.debug_sidebar"
_DEBUG_SNAPSHOT_NAME = "writeragent_debug_sidebar.json"

# Debug-only. This module is replaced by a stub in release OXTs (no WeakSet).
_LIVE_CHAT_PANELS: WeakSet[Any] = WeakSet()
# Listeners created by the installed OXT factory may not share this WeakSet.
_LIVE_SEND_LISTENERS: list[Any] = []
# Last native-test ctx so URP fallbacks can executeDispatch without get_ctx().
_HOOK_CTX: Any = None


def register_live_panel(element: Any) -> None:
    _require_debug()
    if element is not None:
        _LIVE_CHAT_PANELS.add(element)


def unregister_live_panel(element: Any) -> None:
    _require_debug()
    _LIVE_CHAT_PANELS.discard(element)


def iter_live_chat_panels() -> list[Any]:
    _require_debug()
    from plugin.chatbot.panel_factory import iter_debug_live_chat_panels

    merged: list[Any] = []
    seen: set[int] = set()
    for panel in list(iter_debug_live_chat_panels()) + list(_LIVE_CHAT_PANELS):
        ident = id(panel)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(panel)
    return merged


def debug_hooks_available() -> bool:
    """False in release OXTs (this file is omitted). True in dev trees.

    ``make test-mock-sidebar`` sets ``WRITERAGENT_UNO_THREAD_GUARD=0`` on soffice,
    so the thread_guard stub has no ``_designated_main_thread``. Still allow
    Packet G protocol dispatch in that process (``WRITERAGENT_TESTING=1``).
    """
    try:
        from plugin.framework import thread_guard as tg

        if hasattr(tg, "_designated_main_thread"):
            return True
    except Exception:
        pass
    return os.environ.get("WRITERAGENT_TESTING") == "1"


def _require_debug() -> None:
    if not debug_hooks_available():
        raise RuntimeError(_HOOKS_UNAVAILABLE)


def debug_sidebar_snapshot_path() -> str:
    return os.path.join(tempfile.gettempdir(), _DEBUG_SNAPSHOT_NAME)


def _history_user_tail(sl: Any) -> str:
    session = getattr(sl, "session", None)
    if session is None:
        return ""
    db = getattr(session, "db", None)
    rows = db.get_messages() if db is not None else list(getattr(session, "messages", None) or [])
    from plugin.chatbot.history_db import message_to_dict

    for msg in reversed(list(rows)):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            return str(message_to_dict("user", content).get("content") or "")
        return str(content or "")
    return ""


def _write_debug_snapshot(sl: Any) -> dict[str, Any]:
    send = sl.sidebar_state.send if sl is not None else None
    audio = sl.sidebar_state.audio if sl is not None else None
    rec = getattr(sl, "audio_recorder", None) if sl is not None else None
    data: dict[str, Any] = {
        "is_busy": bool(getattr(send, "is_busy", False)),
        "is_recording": bool(getattr(send, "is_recording", False)),
        "has_text": bool(getattr(send, "has_text", False)),
        "has_audio": bool(getattr(send, "has_audio", False)),
        "audio_supported": bool(getattr(send, "audio_supported", False)),
        "send_label": _control_label(getattr(sl, "send_control", None)) if sl is not None else "",
        "stop_label": _control_label(getattr(sl, "stop_control", None)) if sl is not None else "",
        "status": getattr(audio, "status", "idle") if audio is not None else "idle",
        "error_message": getattr(audio, "error_message", None) if audio is not None else None,
        "stub_start_count": int(getattr(rec, "_stub_start_count", 0) or 0),
        "history_user_tail": _history_user_tail(sl) if sl is not None else "",
        "approval_active": bool(getattr(sl, "_approval_event", None)) if sl is not None else False,
    }
    with open(debug_sidebar_snapshot_path(), "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return data


def _read_debug_snapshot() -> dict[str, Any]:
    path = debug_sidebar_snapshot_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def handle_debug_sidebar_command(command: str) -> None:
    """Run inside soffice (protocol handler). Packet G URP FSM ops.

    ``DispatchHandler`` runs on the URP thread. ``WRITERAGENT_TESTING=1`` makes
    ``QueueExecutor.post`` inline, so Stop Rec used to start ``_do_send`` off
    the VCL thread and freeze on ``Getting document...``. Marshal FSM work onto
    the listener's executor (VCL) before StartSendEffect posts the drain.
    """
    _require_debug()
    adopt_runtime_send_listeners()
    rest = command[len(_DEBUG_SIDEBAR_PREFIX) :].lstrip(".")
    op = (rest or "SNAPSHOT").upper().replace("-", "_")
    sl = send_listener()
    if op == "SNAPSHOT":
        _write_debug_snapshot(sl)
        return
    if sl is None:
        log.warning("debug_sidebar %s: no SendButtonListener", op)
        _write_debug_snapshot(None)
        return

    def _apply() -> None:
        if op == "RECORD_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        elif op == "STOP_REC_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))
        elif op == "SEND_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        elif op == "STOP_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
        elif op == "SET_AUDIO_0":
            set_audio_supported(False, listener=sl)
        elif op == "SET_AUDIO_1":
            set_audio_supported(True, listener=sl)
        elif op == "AUTO_STOP":
            fire_audio_auto_stop(listener=sl)
        elif op == "SET_TEXT_EMPTY":
            from plugin.chatbot.dialogs import set_control_text

            query = getattr(sl, "query_control", None)
            if query is not None:
                set_control_text(query, "")
            ss = sl.sidebar_state
            send = dataclasses.replace(ss.send, has_audio=False, has_text=False)
            sl.sidebar_state = dataclasses.replace(ss, send=send)
            sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": False}))
        elif op == "SET_TEXT_NONEMPTY":
            sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        elif op == "SET_CHAT_MODE":
            apply_fn = getattr(sl, "_apply_sidebar_mode_fn", None)
            if apply_fn is not None:
                from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

                apply_fn(CHAT_MODE_CHAT)
        else:
            log.warning("debug_sidebar unknown op %s", op)
        _write_debug_snapshot(sl)

    qe = getattr(sl, "queue_executor", None)
    if qe is None:
        _apply()
        return
    from plugin.framework.queue_executor import set_force_marshal_mode

    # Post, do not execute(): URP executeDispatch + blocking VCL wait deadlocks
    # (office sits idle, tests wait forever). AsyncCallback runs _apply on VCL.
    set_force_marshal_mode(True)
    try:
        qe.post(_apply)
    finally:
        set_force_marshal_mode(False)


def execute_debug_sidebar_op(op: str, *, ctx: Any = None) -> dict[str, Any]:
    """URP client: dispatch ``org.extension.writeragent:chatbot.debug_sidebar.<OP>`` in soffice."""
    _require_debug()
    uno_ctx = ctx if ctx is not None else _HOOK_CTX
    if uno_ctx is None:
        from plugin.framework.uno_context import get_ctx

        uno_ctx = get_ctx()
    doc = current_component(uno_ctx)
    frame = None
    try:
        if doc is not None:
            frame = doc.getCurrentController().getFrame()
    except Exception:
        frame = None
    if frame is None:
        raise RuntimeError("debug_sidebar: no frame for executeDispatch")
    url = "%s:%s?%s" % (EXTENSION_ID_WRITERAGENT, _DEBUG_SIDEBAR_PREFIX, op)
    smgr = uno_ctx.getServiceManager()
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", uno_ctx)
    helper.executeDispatch(frame, url, "", 0, ())
    if op.upper() != "SNAPSHOT":
        time.sleep(0.25)
        snap_url = "%s:%s?SNAPSHOT" % (EXTENSION_ID_WRITERAGENT, _DEBUG_SIDEBAR_PREFIX)
        helper.executeDispatch(frame, snap_url, "", 0, ())
    return _read_debug_snapshot()


def _urp_send_control() -> Any:
    ctx = _HOOK_CTX
    if ctx is None:
        return None
    try:
        controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    except Exception:
        return None
    return controls.get("send")


def _try_click_send_for_kind(kind: SendEventKind) -> bool:
    """G1 path: Record / Stop Rec are the same widget. Click when the label matches."""
    send = _urp_send_control()
    if send is None:
        return False
    label = _control_label(send).lower()
    if kind == SendEventKind.RECORD_CLICKED and "record" in label and "stop rec" not in label:
        uno_click(send)
        return True
    if kind == SendEventKind.STOP_REC_CLICKED and "stop rec" in label:
        uno_click(send)
        return True
    return False


def _send_label_lower() -> str:
    return _control_label(_urp_send_control()).lower()


def _send_event_or_urp(kind: SendEventKind, *, listener: Any = None) -> None:
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        sl.dispatch(SendEvent(kind))
        return
    if _try_click_send_for_kind(kind):
        # G15: ActionEvent on Record can be a no-op while the label still says
        # Record. Fall back to the debug protocol so Stop Rec actually appears.
        if kind == SendEventKind.RECORD_CLICKED:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if "stop rec" in _send_label_lower():
                    return
                time.sleep(0.1)
            execute_debug_sidebar_op(kind.name)
        return
    execute_debug_sidebar_op(kind.name)


def sidebar_panel(frame: Any = None) -> Any:
    """Return the live ``ChatPanelElement`` for *frame*, or the only live panel."""
    _require_debug()
    panels = iter_live_chat_panels()
    if not panels:
        return None
    if frame is not None:
        for panel in panels:
            if getattr(panel, "xFrame", None) is frame or getattr(panel, "Frame", None) is frame:
                return panel
    if len(panels) == 1:
        return panels[0]
    return panels[0]


def desktop_from_ctx(ctx: Any) -> Any:
    """Desktop from the remote ``ctx`` without ``get_ctx()`` (avoids disposed fallbacks)."""
    _require_debug()
    smgr = ctx.getServiceManager()
    return smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)


def current_component(ctx: Any) -> Any:
    _require_debug()
    return desktop_from_ctx(ctx).getCurrentComponent()


def uno_click(control: Any) -> None:
    """Fire the control's default accessible action (button click) over URP."""
    _require_debug()
    acc = None
    try:
        acc = control.getAccessibleContext()
    except Exception:
        acc = None
    if acc is not None and hasattr(acc, "doAccessibleAction"):
        acc.doAccessibleAction(0)
        return
    raise RuntimeError("control has no accessible click")


def _query_uno_interface(obj: Any, typename: str) -> Any:
    """PyUNO ``queryInterface`` needs ``uno.getTypeByName``, not the IDL class."""
    if obj is None or not hasattr(obj, "queryInterface"):
        return None
    try:
        import uno

        return obj.queryInterface(uno.getTypeByName(typename))
    except Exception:
        return None


def sidebar_provider(controller: Any) -> Any:
    """Return ``XSidebarProvider`` (decks / setVisible), or None.

    On ``SwXTextView``, ``getDecks`` is ``controller.Sidebar`` (the property),
    not a method on the controller. ``queryInterface(XSidebarProvider)`` on
    the controller is None. Prefer the property, then a controller that
    already has ``getDecks``.
    """
    _require_debug()
    if controller is None:
        return None
    sidebar = getattr(controller, "Sidebar", None)
    if sidebar is not None and callable(getattr(sidebar, "getDecks", None)):
        return sidebar
    if callable(getattr(controller, "getDecks", None)):
        return controller
    return _query_uno_interface(controller, "com.sun.star.ui.XSidebarProvider")


def sidebar_deck_names(ctx: Any, doc: Any) -> list[str]:
    """Deck ids from XSidebarProvider, or empty if the API is unavailable."""
    _require_debug()
    if doc is None:
        return []
    try:
        controller = doc.getCurrentController()
        provider = sidebar_provider(controller)
        if provider is None:
            return []
        decks = provider.getDecks()
        if hasattr(decks, "getElementNames"):
            return [str(n) for n in decks.getElementNames()]
    except Exception:
        return []
    return []


def _panel_root_window(panel: Any) -> Any:
    if panel is None:
        return None
    for attr in ("getDialog", "getWindow"):
        getter = getattr(panel, attr, None)
        if not callable(getter):
            continue
        try:
            win = getter()
        except Exception:
            continue
        if win is not None:
            return win
    return getattr(panel, "Window", None) or getattr(panel, "PanelWindow", None)


def _control_container(window: Any) -> Any:
    if window is None:
        return None
    if hasattr(window, "getControl"):
        return window
    return _query_uno_interface(window, "com.sun.star.awt.XControlContainer") or window


_CHAT_CONTROL_NAMES = (
    "query",
    "send",
    "stop",
    "clear",
    "response",
    "response_rich",
    "status",
    "model_selector",
    "chat_mode_selector",
)


def _controls_from_window(window: Any) -> dict[str, Any] | None:
    root = _control_container(window)
    if root is None or not hasattr(root, "getControl"):
        return None
    out: dict[str, Any] = {}
    for name in _CHAT_CONTROL_NAMES:
        try:
            ctrl = root.getControl(name)
        except Exception:
            ctrl = None
        if ctrl is not None:
            out[name] = ctrl
    if "query" in out and "send" in out:
        return out
    return None


def chat_dialog_controls(ctx: Any, doc: Any) -> dict[str, Any] | None:
    """Controls on the live WriterAgent chat panel dialog (out-of-process URP)."""
    _require_debug()
    if doc is None:
        return None
    try:
        controller = doc.getCurrentController()
        provider = sidebar_provider(controller)
        if provider is None:
            return None
        decks = provider.getDecks()
        deck = None
        if hasattr(decks, "hasByName") and decks.hasByName("WriterAgentDeck"):
            deck = decks.getByName("WriterAgentDeck")
        if deck is None:
            return None
        panels = deck.getPanels()
        panel = None
        if hasattr(panels, "hasByName") and panels.hasByName("ChatPanel"):
            panel = panels.getByName("ChatPanel")
        elif hasattr(panels, "getByIndex"):
            panel = panels.getByIndex(0)
        return _controls_from_window(_panel_root_window(panel))
    except Exception:
        log.debug("chat_dialog_controls failed", exc_info=True)
    return None


def send_listener(frame: Any = None) -> Any:
    _require_debug()
    panel = sidebar_panel(frame)
    if panel is not None:
        sl = getattr(panel, "send_listener", None)
        if sl is not None:
            return sl
    if _LIVE_SEND_LISTENERS:
        return _LIVE_SEND_LISTENERS[-1]
    return None


def adopt_runtime_send_listeners() -> int:
    """Find ``SendButtonListener`` instances already wired by the installed factory.

    UNO may load ``panel_factory`` from the OXT cache while tests import the
    checkout copy, so the debug WeakSet can be empty even with a live sidebar.
    """
    _require_debug()
    import gc

    found = 0
    for obj in gc.get_objects():
        try:
            if type(obj).__name__ != "SendButtonListener":
                continue
            if getattr(obj, "dispatch", None) is None:
                continue
            if getattr(obj, "query_control", None) is None:
                continue
        except Exception:
            continue
        if obj not in _LIVE_SEND_LISTENERS:
            _LIVE_SEND_LISTENERS.append(obj)
            found += 1
    return found


def _writeragent_deck(provider: Any) -> Any:
    """Return the WriterAgent XDeck from *provider*, or None."""
    if provider is None:
        return None
    try:
        decks = provider.getDecks()
        if decks is None:
            return None
        name = "WriterAgentDeck"
        if hasattr(decks, "hasByName") and decks.hasByName(name):
            return decks.getByName(name)
        names = list(decks.getElementNames()) if hasattr(decks, "getElementNames") else []
        for deck_name in names:
            if "WriterAgent" in str(deck_name):
                return decks.getByName(deck_name)
    except Exception:
        return None
    return None


def _activate_writeragent_deck(provider: Any) -> None:
    """Switch to WriterAgent via XDeck.activate (no toggle)."""
    deck = _writeragent_deck(provider)
    if deck is None:
        return
    try:
        deck.activate(True)
    except Exception:
        log.debug("activate WriterAgentDeck failed", exc_info=True)


def show_writeragent_chat_deck(ctx: Any, doc: Any) -> None:
    """Make the WriterAgent sidebar deck visible on *doc* (debug tests).

    ``.uno:SidebarDeck.WriterAgentDeck`` is LibreOffice OpenThenToggleDeck
    (tdf#67627): if WriterAgent is already the visible deck, a second summon
    *hides* the sidebar. Skip that dispatch when ``XSidebarProvider.isVisible()``
    is already true; use ``showDecks`` / ``XDeck.activate`` instead. When the
    sidebar is off, dispatch once to open it. Do not dispatch ``.uno:Sidebar`` —
    that also toggles. ``--norestore`` skips crash-recovery so this path is what
    reopens the deck for mock-sidebar tests.
    """
    _require_debug()
    if doc is None:
        return
    try:
        controller = doc.getCurrentController()
        frame = controller.getFrame()
    except Exception:
        return
    provider = sidebar_provider(controller)
    already_visible = False
    if provider is not None and hasattr(provider, "isVisible"):
        try:
            already_visible = bool(provider.isVisible())
        except Exception:
            already_visible = False

    # OpenThenToggleDeck: same-deck second time closes the sidebar — skip when on.
    if already_visible and provider is not None:
        try:
            provider.showDecks(True)
        except Exception:
            pass
        deck = _writeragent_deck(provider)
        if deck is not None:
            try:
                if hasattr(deck, "isActive") and deck.isActive():
                    return
            except Exception:
                pass
            try:
                deck.activate(True)
            except Exception:
                log.debug("show_writeragent_chat_deck activate while visible failed", exc_info=True)
        return

    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        try:
            helper.executeDispatch(frame, ".uno:SidebarDeck.WriterAgentDeck", "", 0, ())
        except Exception:
            log.debug("show_writeragent_chat_deck dispatch WriterAgentDeck failed", exc_info=True)
    except Exception:
        log.debug("show_writeragent_chat_deck DispatchHelper failed", exc_info=True)
    if provider is None:
        return
    try:
        if hasattr(provider, "setVisible"):
            provider.setVisible(True)
    except Exception:
        pass
    try:
        provider.showDecks(True)
    except Exception:
        pass
    _activate_writeragent_deck(provider)


def wait_for_chat_dialog_controls(ctx: Any, timeout: float = 20.0) -> dict[str, Any] | None:
    """Show WriterAgentDeck until query+send exist. Does not pump VCL over URP."""
    global _HOOK_CTX
    _HOOK_CTX = ctx
    _require_debug()
    deadline = time.monotonic() + max(0.0, timeout)
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            doc = current_component(ctx)
            show_writeragent_chat_deck(ctx, doc)
            last = chat_dialog_controls(ctx, doc)
            if last is not None:
                return last
        except Exception:
            log.debug("wait_for_chat_dialog_controls attempt failed", exc_info=True)
        time.sleep(0.4)
    return last


def control_enabled(control: Any) -> bool | None:
    """``model.Enabled`` over URP, or None if unreadable."""
    _require_debug()
    if control is None:
        return None
    try:
        model = control.getModel()
        return bool(getattr(model, "Enabled"))
    except Exception:
        return None


def ensure_sidebar_chat_mode(controls: dict[str, Any] | None) -> None:
    """Select main Chat (not Librarian) so Packet F hits the chat completions path."""
    _require_debug()
    if not controls:
        return
    sel = controls.get("chat_mode_selector")
    if sel is not None:
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            set_selector_mode_with_flags,
            sidebar_mode_flags_for_doc_type,
        )

        set_selector_mode_with_flags(sel, CHAT_MODE_CHAT, sidebar_mode_flags_for_doc_type("writer"))
    sl = send_listener()
    if sl is not None:
        apply_fn = getattr(sl, "_apply_sidebar_mode_fn", None)
        if apply_fn is not None:
            from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

            apply_fn(CHAT_MODE_CHAT)
    else:
        execute_debug_sidebar_op("SET_CHAT_MODE")


def set_query_text_via_controls(controls: dict[str, Any], text: str) -> None:
    """Set the query box over URP so QueryTextListener can enable Send."""
    _require_debug()
    from plugin.chatbot.dialogs import set_control_text

    if "query" in controls:
        set_control_text(controls["query"], text)
    sl = send_listener()
    if sl is not None:
        sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(text.strip())}))
    else:
        execute_debug_sidebar_op("SET_TEXT_NONEMPTY" if text.strip() else "SET_TEXT_EMPTY")


def wait_controls_send_finished(
    controls: dict[str, Any],
    timeout: float = 60.0,
    *,
    transcript_fn: Callable[[], str] | None = None,
    wait_for: str | None = None,
    before: str = "",
) -> bool:
    """Wait until Stop is idle and optional new transcript text appeared.

    Out-of-process tests cannot read ``SendButtonListener.is_busy``. Stop is
    enabled while a send is in flight (Packet F HTTP errors included).
    """
    _require_debug()
    deadline = time.monotonic() + max(0.0, timeout)
    stop = controls.get("stop")
    # Let the click start; HTTP 500 can finish before the first poll.
    time.sleep(0.25)
    while time.monotonic() <= deadline:
        en = control_enabled(stop) if stop is not None else None
        busy = en is True
        body = transcript_fn() if transcript_fn is not None else ""
        suffix = body[len(before) :] if before and body.startswith(before) else body
        if wait_for:
            found = wait_for.lower() in suffix.lower()
            # Only search the whole control when a rich rerender dropped the prefix.
            # ``body != before`` is too weak: any new character plus a needle already
            # in earlier turns (Packet C truncated banner) would look finished.
            if not found and before and body and not body.startswith(before):
                found = wait_for.lower() in body.lower()
        else:
            found = True
        if found and not busy:
            return True
        time.sleep(0.15)
    if wait_for and transcript_fn is not None:
        body = transcript_fn()
        suffix = body[len(before) :] if before and body.startswith(before) else body
        if wait_for.lower() in suffix.lower():
            return True
        if before and body and not body.startswith(before):
            return wait_for.lower() in body.lower()
        return False
    en = control_enabled(stop) if stop is not None else None
    return en is not True


def _control_label(control: Any) -> str:
    try:
        model = control.getModel() if control is not None else None
        if model is not None:
            return str(getattr(model, "Label", "") or "")
    except Exception:
        log.debug("control label read failed", exc_info=True)
    return ""


def set_query_text(text: str, *, listener: Any = None) -> None:
    """Set the query box and dispatch ``TEXT_UPDATED`` (same as ``QueryTextListener``)."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        from plugin.chatbot.dialogs import set_control_text

        query = getattr(sl, "query_control", None)
        set_control_text(query, text)
        stripped = (text or "").strip()
        sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(stripped)}))
        return
    ctx = _HOOK_CTX
    if ctx is not None:
        controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
        set_query_text_via_controls(controls, text)


def query_text(*, listener: Any = None) -> str:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return ""
    from plugin.chatbot.dialogs import get_control_text

    return get_control_text(getattr(sl, "query_control", None), default="") or ""


def transcript_text(*, listener: Any = None) -> str:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return ""
    from plugin.chatbot.dialogs import get_control_text

    widget = getattr(sl, "rich_text_widget", None)
    control = getattr(widget, "control", None) if widget is not None else None
    if control is None:
        control = getattr(sl, "response_control", None)
    return get_control_text(control, default="") or ""


def transcript_contains(needle: str, *, listener: Any = None) -> bool:
    _require_debug()
    return needle in transcript_text(listener=listener)


def press_send(*, listener: Any = None) -> None:
    """Primary Send button path (also Accept when HITL owns the label)."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl.on_action_performed(None)


def press_stop(*, listener: Any = None) -> None:
    """Windows / ActionEvent Stop path (``StopButtonListener.on_action_performed``)."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        _send_event_or_urp(SendEventKind.STOP_CLICKED, listener=None)
        return
    StopButtonListener(sl).on_action_performed(None)


def press_stop_mouse(*, listener: Any = None) -> None:
    """GTK Stop ``mousePressed`` path. No-op while web-search approval is active."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    notify_stop_mouse_pressed(sl)


def press_accept(*, listener: Any = None) -> None:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl.on_action_performed(None)


def press_change(query_override: str | None = None, *, listener: Any = None) -> None:
    """HITL Change without the modal edit dialog (Packet E9c). Not ``STOP_CLICKED``."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    if query_override is None:
        query_override = getattr(sl, "_approval_query_for_engine", None) or ""
    sl._finish_inline_web_approval(True, query_override=query_override)


def press_reject(*, listener: Any = None) -> None:
    """HITL Reject (Clear-button overlay). Not ``STOP_CLICKED``."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl._finish_inline_web_approval(False)


def approval_active(*, listener: Any = None) -> bool:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return False
    return getattr(sl, "_approval_event", None) is not None


def press_record(*, listener: Any = None) -> None:
    _require_debug()
    _send_event_or_urp(SendEventKind.RECORD_CLICKED, listener=listener)


def press_stop_rec(*, listener: Any = None) -> None:
    _require_debug()
    _send_event_or_urp(SendEventKind.STOP_REC_CLICKED, listener=listener)


def press_send_clicked(*, listener: Any = None) -> None:
    """Always ``SEND_CLICKED`` (ignore Record / Stop Rec / Accept labels). Packet G15."""
    _require_debug()
    _send_event_or_urp(SendEventKind.SEND_CLICKED, listener=listener)


def set_audio_supported(supported: bool, *, listener: Any = None) -> None:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        execute_debug_sidebar_op("SET_AUDIO_1" if supported else "SET_AUDIO_0")
        return
    ss = sl.sidebar_state
    send = dataclasses.replace(ss.send, audio_supported=bool(supported), has_audio=False)
    sl.sidebar_state = dataclasses.replace(ss, send=send)
    sl.dispatch(
        SendEvent(
            SendEventKind.TEXT_UPDATED,
            {"has_text": bool(send.has_text)},
        )
    )


def audio_status(*, listener: Any = None) -> dict[str, Any]:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        data = execute_debug_sidebar_op("SNAPSHOT")
        return {
            "status": data.get("status", "idle"),
            "has_audio": bool(data.get("has_audio")),
            "is_recording": bool(data.get("is_recording")),
            "error_message": data.get("error_message"),
            "stub_start_count": int(data.get("stub_start_count") or 0),
            "history_user_tail": str(data.get("history_user_tail") or ""),
        }
    send = sl.sidebar_state.send
    audio = sl.sidebar_state.audio
    rec = getattr(sl, "audio_recorder", None)
    return {
        "status": getattr(audio, "status", "idle"),
        "has_audio": bool(send.has_audio),
        "is_recording": bool(send.is_recording),
        "error_message": getattr(audio, "error_message", None),
        "stub_start_count": int(getattr(rec, "_stub_start_count", 0) or 0),
        "history_user_tail": _history_user_tail(sl),
    }


def _live_audio_recorder(*, listener: Any = None) -> Any:
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return None
    return getattr(sl, "audio_recorder", None)


def inject_wav(path_or_bytes: Any, *, listener: Any = None) -> None:
    """Point the stub capture child at a finished WAV (path or bytes). No mic."""
    _require_debug()
    from plugin.chatbot.audio_recorder import write_stub_recorder_control

    wav_path = path_or_bytes
    if isinstance(path_or_bytes, (bytes, bytearray)):
        dest = os.path.join(__import__("tempfile").gettempdir(), "writeragent_stub_inject.wav")
        with open(dest, "wb") as handle:
            handle.write(path_or_bytes)
        wav_path = dest
    write_stub_recorder_control(wav=wav_path, skip=True)
    rec = _live_audio_recorder(listener=listener)
    if rec is None:
        return
    rec._test_skip_spawn = True
    rec._test_inject_wav = wav_path
    if rec.temp_filename and wav_path is not None:
        rec._write_injected_wav()


def stub_recorder_child(
    *,
    listener: Any = None,
    fail_start: str | None = None,
    missing_wav: bool = False,
    hang_ready: bool = False,
) -> None:
    """Skip venv/PortAudio spawn; InitializeDeviceEffect fakes a ready child."""
    _require_debug()
    from plugin.chatbot.audio_recorder import clear_stub_recorder_control, write_stub_recorder_control

    # Replace the control file so G4 auto_stop / G12 fail_start / G21 hang_ready cannot leak.
    clear_stub_recorder_control()
    write_stub_recorder_control(
        skip=True,
        fail_start=fail_start,
        missing_wav=bool(missing_wav),
        hang_ready=bool(hang_ready),
    )
    rec = _live_audio_recorder(listener=listener)
    if rec is None:
        return
    rec._test_skip_spawn = True
    rec._test_fail_start = fail_start
    rec._test_missing_wav = bool(missing_wav)
    rec._test_hang_ready = bool(hang_ready)
    rec._stub_start_count = 0


def fire_audio_auto_stop(*, listener: Any = None) -> None:
    """Same host path as IPC auto_stopped (silence detector), no wall-clock wait."""
    _require_debug()
    from plugin.chatbot.audio_recorder import write_stub_recorder_control

    write_stub_recorder_control(auto_stop=True, skip=True)
    rec = _live_audio_recorder(listener=listener)
    if rec is None:
        return
    rec._notify_auto_stop(rec.temp_filename)


@dataclass(frozen=True)
class SidebarHookSendView:
    is_busy: bool
    is_recording: bool
    has_text: bool
    has_audio: bool
    audio_supported: bool
    send_label: str
    stop_label: str


def send_state(*, listener: Any = None) -> SidebarHookSendView:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        data = execute_debug_sidebar_op("SNAPSHOT")
        return SidebarHookSendView(
            is_busy=bool(data.get("is_busy")),
            is_recording=bool(data.get("is_recording")),
            has_text=bool(data.get("has_text")),
            has_audio=bool(data.get("has_audio")),
            audio_supported=bool(data.get("audio_supported")),
            send_label=str(data.get("send_label") or ""),
            stop_label=str(data.get("stop_label") or ""),
        )
    send = sl.sidebar_state.send
    return SidebarHookSendView(
        is_busy=bool(send.is_busy),
        is_recording=bool(send.is_recording),
        has_text=bool(send.has_text),
        has_audio=bool(send.has_audio),
        audio_supported=bool(send.audio_supported),
        send_label=_control_label(getattr(sl, "send_control", None)),
        stop_label=_control_label(getattr(sl, "stop_control", None)),
    )


def pump_until(pred: Callable[[], bool], timeout: float = 30.0, *, ctx: Any = None) -> bool:
    """Idle-pump until *pred* is true. Uses ``force=True`` so native tests still pump VCL."""
    _require_debug()
    from plugin.framework.uno_context import get_ctx, process_events_to_idle

    deadline = time.monotonic() + max(0.0, timeout)
    uno_ctx = ctx
    if uno_ctx is None:
        sl = send_listener()
        uno_ctx = getattr(sl, "ctx", None) if sl is not None else None
        if uno_ctx is None:
            try:
                uno_ctx = get_ctx()
            except Exception:
                uno_ctx = None
    while time.monotonic() <= deadline:
        if pred():
            return True
        # Visible user-profile soffice: processEventsToIdle over URP can hang the pipe.
        if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1" or uno_ctx is None:
            time.sleep(0.05)
        else:
            process_events_to_idle(uno_ctx, rounds=1, force=True)
    return pred()


def wait_idle(*, listener: Any = None, timeout: float = 30.0) -> bool:
    _require_debug()
    sl0 = listener if listener is not None else send_listener()
    if sl0 is None:
        # Let Stop Rec / Send enable Stop before the first idle poll (else we
        # return immediately and Packet G never waits for the mock reply).
        time.sleep(0.35)

    def _idle() -> bool:
        sl = listener if listener is not None else send_listener()
        if sl is None:
            # Do not executeDispatch SNAPSHOT in a loop — that hangs the URP pipe.
            ctx = _HOOK_CTX
            if ctx is None:
                return False
            try:
                controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
            except Exception:
                return False
            stop = controls.get("stop")
            send = controls.get("send")
            if control_enabled(stop) is True:
                return False
            return control_enabled(send) is True
        send = sl.sidebar_state.send
        return (not send.is_busy) and (not send.is_recording)

    ctx = getattr(listener, "ctx", None) if listener is not None else None
    return pump_until(_idle, timeout, ctx=ctx)


def next_hello_ok(*, listener: Any = None, timeout: float = 60.0) -> bool:
    """Send ``hello``, wait until idle, require assistant HTML or hello text in the transcript."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    set_query_text("hello", listener=sl)
    press_send(listener=sl)
    if not wait_idle(listener=sl, timeout=timeout):
        return False
    text = transcript_text(listener=sl).lower()
    if "hello" in text or "<p" in text or "<ul" in text or "<ol" in text:
        return True
    log.warning("next_hello_ok: idle but transcript did not look like a hello reply")
    return False
