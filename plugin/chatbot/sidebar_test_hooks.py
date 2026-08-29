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
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from weakref import WeakSet

from plugin.chatbot.panel import StopButtonListener, notify_stop_mouse_pressed
from plugin.chatbot.send_state import SendEvent, SendEventKind

log = logging.getLogger("writeragent.sidebar_test_hooks")

_HOOKS_UNAVAILABLE = "sidebar test hooks are not in release builds"

# Debug-only. This module is replaced by a stub in release OXTs (no WeakSet).
_LIVE_CHAT_PANELS: WeakSet[Any] = WeakSet()
# Listeners created by the installed OXT factory may not share this WeakSet.
_LIVE_SEND_LISTENERS: list[Any] = []


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
    """False when ``thread_guard`` is the release stub (no ``_designated_main_thread``)."""
    try:
        from plugin.framework import thread_guard as tg

        return hasattr(tg, "_designated_main_thread")
    except Exception:
        return False


def _require_debug() -> None:
    if not debug_hooks_available():
        raise RuntimeError(_HOOKS_UNAVAILABLE)


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


def show_writeragent_chat_deck(ctx: Any, doc: Any) -> None:
    """Make the WriterAgent sidebar deck visible on *doc* (debug tests).

    ``.uno:SidebarDeck.WriterAgentDeck`` shows the sidebar if View → Sidebar is
    off. Do not dispatch ``.uno:Sidebar`` — that *toggles*. ``--norestore``
    skips crash-recovery so this dispatch is what reopens the deck.
    """
    _require_debug()
    if doc is None:
        return
    try:
        controller = doc.getCurrentController()
        frame = controller.getFrame()
    except Exception:
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
    provider = sidebar_provider(controller)
    if provider is None:
        return
    try:
        if hasattr(provider, "isVisible") and not provider.isVisible():
            provider.setVisible(True)
        elif hasattr(provider, "setVisible"):
            provider.setVisible(True)
    except Exception:
        pass
    try:
        provider.showDecks(True)
    except Exception:
        pass
    try:
        decks = provider.getDecks()
        if decks is None:
            return
        name = "WriterAgentDeck"
        if hasattr(decks, "hasByName") and decks.hasByName(name):
            decks.getByName(name).activate(True)
            return
        names = list(decks.getElementNames()) if hasattr(decks, "getElementNames") else []
        for deck_name in names:
            if "WriterAgent" in str(deck_name):
                decks.getByName(deck_name).activate(True)
                return
    except Exception:
        log.debug("show_writeragent_chat_deck failed", exc_info=True)


def wait_for_chat_dialog_controls(ctx: Any, timeout: float = 20.0) -> dict[str, Any] | None:
    """Show WriterAgentDeck until query+send exist. Does not pump VCL over URP."""
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
    if sel is None:
        return
    from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT, set_selector_mode_with_flags, sidebar_mode_flags_for_doc_type

    set_selector_mode_with_flags(sel, CHAT_MODE_CHAT, sidebar_mode_flags_for_doc_type("writer"))


def set_query_text_via_controls(controls: dict[str, Any], text: str) -> None:
    """Set the query box over URP so QueryTextListener can enable Send."""
    _require_debug()
    from plugin.chatbot.dialogs import set_control_text

    set_control_text(controls["query"], text)


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
            if not found and body != before:
                found = wait_for.lower() in body.lower()
        else:
            found = True
        if found and not busy:
            return True
        time.sleep(0.15)
    if wait_for and transcript_fn is not None:
        body = transcript_fn()
        suffix = body[len(before) :] if before and body.startswith(before) else body
        return wait_for.lower() in suffix.lower() or (body != before and wait_for.lower() in body.lower())
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
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    from plugin.chatbot.dialogs import set_control_text

    query = getattr(sl, "query_control", None)
    set_control_text(query, text)
    stripped = (text or "").strip()
    sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(stripped)}))


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
        raise RuntimeError("no live SendButtonListener")
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
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))


def press_stop_rec(*, listener: Any = None) -> None:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))


def set_audio_supported(supported: bool, *, listener: Any = None) -> None:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    ss = sl.sidebar_state
    send = dataclasses.replace(ss.send, audio_supported=bool(supported))
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
        return {"status": "idle", "has_audio": False}
    send = sl.sidebar_state.send
    audio = sl.sidebar_state.audio
    return {
        "status": getattr(audio, "status", "idle"),
        "has_audio": bool(send.has_audio),
        "is_recording": bool(send.is_recording),
        "error_message": getattr(audio, "error_message", None),
    }


def inject_wav(path_or_bytes: Any) -> None:
    _require_debug()
    raise NotImplementedError("inject_wav is reserved for Packet G (no mic)")


def stub_recorder_child() -> None:
    _require_debug()
    raise NotImplementedError("stub_recorder_child is reserved for Packet G (no mic)")


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
        raise RuntimeError("no live SendButtonListener")
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

    def _idle() -> bool:
        sl = listener if listener is not None else send_listener()
        if sl is None:
            return False
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
