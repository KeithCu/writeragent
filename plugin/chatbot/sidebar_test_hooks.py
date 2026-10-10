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

from plugin.chatbot.panel import (
    StopButtonListener,
    notify_record_mouse_pressed,
    notify_record_mouse_released,
    notify_stop_mouse_pressed,
)
from plugin.chatbot.send_state import SendEvent, SendEventKind
from plugin.framework.constants import EXTENSION_ID_WRITERAGENT

log = logging.getLogger("writeragent.sidebar_test_hooks")

_HOOKS_UNAVAILABLE = "sidebar test hooks are not in release builds"
_DEBUG_SIDEBAR_PREFIX = "chatbot.debug_sidebar"
_DEBUG_SNAPSHOT_NAME = "writeragent_debug_sidebar.json"
# Packet K: URP tests cannot stream 24k HTML through the rich control and
# still land those bytes on ChatSession.messages. Inflate in soffice so
# estimate crosses the mock 32768×75% gate (and force-compact can cut).
_INFLATE_TARGET_TOKENS = 28000
_INFLATE_PAIR_TOKENS = 4000
_INFLATE_MAX_PAIRS = 16

# Debug-only. This module is replaced by a stub in release OXTs (no WeakSet).
_LIVE_CHAT_PANELS: WeakSet[Any] = WeakSet()
# Listeners created by the installed OXT factory may not share this WeakSet.
_LIVE_SEND_LISTENERS: list[Any] = []
# Last native-test ctx so URP fallbacks can executeDispatch without get_ctx().
_HOOK_CTX: Any = None
# Out-of-process mock-sidebar: controller bound to the live XDL ListBox over URP.
_URP_SLASH_POPUP: Any = None


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


def _session_snapshot_fields(sl: Any) -> dict[str, Any]:
    """Packet K: URP cannot read ``session.messages``; snapshot from soffice."""
    session = getattr(sl, "session", None) if sl is not None else None
    messages = list(getattr(session, "messages", None) or []) if session is not None else []
    compaction = getattr(session, "compaction", None) if session is not None else None
    return {
        "session_n_messages": len(messages),
        "session_roles": [str(m.get("role") or "") for m in messages if isinstance(m, dict)],
        "session_content_chars": [
            len(str(m.get("content") or "")) for m in messages if isinstance(m, dict)
        ],
        "has_compaction": compaction is not None,
        # Per-turn now (TurnController); None once the drain dropped the turn.
        "last_compact_reason": getattr(getattr(sl, "_turn", None), "_last_compact_reason", None) if sl is not None else None,
    }


def _inflate_session_history(session: Any) -> int:
    """Append large user/assistant pads onto ``session.messages`` (no UI, no DB).

    Streaming ``flood history`` through the rich control completed HTTP 200
    but did not grow the model-facing list enough for proactive compact or
    overflow retry (``nothing_to_compact`` / ``below_threshold``). Direct
    append is the URP-safe grow path.
    """
    if session is None:
        return 0
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return 0
    from plugin.chatbot.compaction import estimate_tokens, messages_for_llm

    if not messages:
        messages.append({"role": "system", "content": "Packet K inflate"})
    added = 0
    while estimate_tokens(messages_for_llm(session)) < _INFLATE_TARGET_TOKENS and added < _INFLATE_MAX_PAIRS:
        added += 1
        messages.append({"role": "user", "content": "inflate history %d" % added})
        messages.append({"role": "assistant", "content": "x" * (_INFLATE_PAIR_TOKENS * 4)})
    return added



def _tts_snapshot_fields() -> dict[str, Any]:
    """Sidebar Voice checkbox vs config (Settings → Speech audio.tts_enabled)."""
    from plugin.framework.config import get_config_bool_safe

    enabled = bool(get_config_bool_safe("audio.tts_enabled"))
    chk_state: int | None = None
    panels = list(iter_live_chat_panels())
    # Also walk doc.live_panels (always registered; debug WeakSet may be empty).
    try:
        from plugin.doc.live_panels import iter_live_panel_uids, get_live_panel

        for uid in iter_live_panel_uids():
            panel = get_live_panel(uid)
            if panel is not None and panel not in panels:
                panels.append(panel)
    except Exception:
        log.debug("tts snapshot: doc.live_panels unavailable", exc_info=True)
    for panel in panels:
        root = getattr(panel, "m_panelRootWindow", None)
        if root is None or not hasattr(root, "getControl"):
            continue
        try:
            chk = root.getControl("chk_voice")
        except Exception:
            chk = None
        if chk is None:
            continue
        try:
            if hasattr(chk, "getState"):
                chk_state = int(chk.getState())
                break
        except Exception:
            log.debug("tts snapshot: chk_voice getState failed", exc_info=True)
    return {
        "tts_enabled": enabled,
        "chk_voice_state": chk_state,
        "tts_sidebar_in_sync": (chk_state is None) or (bool(chk_state) == enabled),
        "tts_live_panels": len(panels),
    }


def _write_debug_snapshot(sl: Any) -> dict[str, Any]:
    send = sl.sidebar_state.send if sl is not None else None
    audio = sl.sidebar_state.audio if sl is not None else None
    rec = getattr(sl, "audio_recorder", None) if sl is not None else None
    # The paint counters live on RichTextChatWidget, not on the raw response control.
    rich_widget = getattr(sl, "rich_text_widget", None) if sl is not None else None
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
        **_slash_snapshot_fields(sl),
        **_session_snapshot_fields(sl),
        **_tts_snapshot_fields(),
        "slash_lru": _slash_lru_names(),
        "paint_session_count": int(getattr(rich_widget, "_debug_paint_session_count", 0) or 0),
        "stream_session_count": int(getattr(rich_widget, "_debug_stream_session_count", 0) or 0),
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


def _parse_debug_sidebar_command(command: str) -> tuple[str, str]:
    """Split ``chatbot.debug_sidebar.<OP>`` / ``?OP&uid=`` into ``(op, uid)``.

    ``DispatchHandler`` joins Path+Query with ``.``. LO often leaves the op
    in Path as ``chatbot.debug_sidebar?INFLATE_HISTORY`` (Query empty). The
    URP client may append ``&uid=<RuntimeUID>`` so soffice inflate/snapshot
    bind that deck — not leftover Calc from ``getCurrentComponent()``.
    Parse *before* ``upper()`` so a hyphenated uid is not rewritten.
    """
    rest = command
    if command.startswith(_DEBUG_SIDEBAR_PREFIX):
        rest = command[len(_DEBUG_SIDEBAR_PREFIX) :]
    rest = rest.lstrip(".?")
    op_part, _, extra = rest.partition("&")
    op = (op_part or "SNAPSHOT").upper().replace("-", "_")
    uid = ""
    for part in extra.split("&"):
        key, sep, val = part.partition("=")
        if sep and key.lower() == "uid":
            uid = val
    return op, uid


def handle_debug_sidebar_command(command: str) -> None:
    """Run inside soffice (protocol handler). Packet G URP FSM ops + OPEN_CALC.

    ``DispatchHandler`` runs on the URP thread. ``WRITERAGENT_TESTING=1`` makes
    ``QueueExecutor.post`` inline, so Stop Rec used to start ``_do_send`` off
    the VCL thread and freeze on ``Getting document...``. Marshal FSM work onto
    the listener's executor (VCL) before StartSendEffect posts the drain.
    ``OPEN_CALC`` uses the same post-to-VCL rule: factory ``scalc`` over URP
    after a Writer deck never returns.
    """
    _require_debug()
    adopt_runtime_send_listeners()
    op, target_uid = _parse_debug_sidebar_command(command)
    # Prefer the URP client's document uid (executeDispatch frame). #802 bound
    # INFLATE to soffice getCurrentComponent(); leftover Calc after Packet P /
    # E12 is often still current there, so pads missed the Writer Send deck.
    sl = send_listener_for_uid(target_uid) if target_uid else None
    if sl is None:
        sl = (
            _listener_for_current_doc()
            if op == "INFLATE_HISTORY"
            else _listener_with_slash_popup(send_listener())
        )
    if op == "SNAPSHOT":
        _write_debug_snapshot(sl)
        return
    # Packet K: mutate ChatSession in soffice (URP cannot touch .messages).
    if op == "INFLATE_HISTORY":
        if sl is None:
            log.warning("debug_sidebar %s: no SendButtonListener", op)
            _write_debug_snapshot(None)
            return
        _inflate_session_history(getattr(sl, "session", None))
        ms = getattr(sl, "model_selector", None)
        if ms is not None:
            from plugin.chatbot.dialogs import set_control_text
            from plugin.framework.client.model_fetcher import set_text_model

            set_control_text(ms, "writeragent-mock")
            set_text_model("writeragent-mock", update_lru=False)
        _write_debug_snapshot(sl)
        return
    # Factory scalc over URP after a Writer deck never returns (Dummy-thread
    # load vs VCL). Post the load onto soffice VCL; the URP client polls.
    if op == "OPEN_CALC":
        log.info("debug_sidebar OPEN_CALC posting factory/scalc to VCL sl=%s", sl is not None)
        _post_to_soffice_vcl(_load_visible_calc_factory, sl=sl)
        _write_debug_snapshot(sl)
        return
    if op == "KICK_PEERS":
        # Packet P URP: queues live in soffice; the test-process kick is a no-op.
        # Post to VCL via _post_to_soffice_vcl so peer sends drain on the
        # main UI thread. handle_debug_sidebar_command runs on a URP bridge
        # thread; calling kick_pending_peer_starts() there runs
        # start_extracted_peer_send and the stream drain without a working
        # VCL event pump and deadlocks URP.
        from plugin.doc.peer_message import kick_pending_peer_starts

        _post_to_soffice_vcl(kick_pending_peer_starts, sl=sl)
        _write_debug_snapshot(sl)
        return
    # Settings→Speech writes audio.tts_enabled then emits config:changed; these
    # ops exercise the same path so headed tests can prove sidebar chk_voice sync.
    if op in ("SET_TTS_ON", "SET_TTS_OFF"):
        from plugin.framework.config import set_config

        set_config("audio.tts_enabled", op == "SET_TTS_ON")
        _write_debug_snapshot(sl)
        return
    if op == "TOGGLE_SIDEBAR_TTS":
        # Drive the sidebar Voice checkbox (reverse of Settings→sidebar).
        from plugin.framework.config import set_config

        panels = list(iter_live_chat_panels())
        try:
            from plugin.doc.live_panels import get_live_panel, iter_live_panel_uids

            for uid in iter_live_panel_uids():
                panel = get_live_panel(uid)
                if panel is not None and panel not in panels:
                    panels.append(panel)
        except Exception:
            pass
        for panel in panels:
            root = getattr(panel, "m_panelRootWindow", None)
            if root is None or not hasattr(root, "getControl"):
                continue
            try:
                chk = root.getControl("chk_voice")
            except Exception:
                chk = None
            if chk is None or not hasattr(chk, "getState") or not hasattr(chk, "setState"):
                continue
            try:
                cur = int(chk.getState())
                want = 0 if cur else 1
                chk.setState(want)
                # Ensure config matches even if ItemEvent.Selected was missing.
                set_config("audio.tts_enabled", bool(want))
            except Exception:
                log.exception("TOGGLE_SIDEBAR_TTS setState failed")
            break
        _write_debug_snapshot(sl)
        return
    # Slash ops only touch the Ask ListBox. Run inline like SNAPSHOT —
    # queue_executor.post is not drained on this URP path (no AsyncCallback).
    if op in ("SLASH_REFRESH", "SLASH_ENTER", "SLASH_ESC"):
        if sl is None:
            log.warning("debug_sidebar %s: no SendButtonListener", op)
            _write_debug_snapshot(None)
            return
        if op == "SLASH_REFRESH":
            _slash_refresh_in_soffice(sl)
        elif op == "SLASH_ENTER":
            _slash_key_in_soffice(sl, 1280, 0)
        else:
            _slash_key_in_soffice(sl, 1281, 0)
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

    if getattr(sl, "queue_executor", None) is None:
        _apply()
        return
    # Post, do not execute(): URP executeDispatch + blocking VCL wait deadlocks
    # (office sits idle, tests wait forever). AsyncCallback runs _apply on VCL.
    # _post_to_soffice_vcl initializes AsyncCallback before the post.
    # Force-marshal straight to the listener's executor skips that init, so
    # on a fresh panel the poke is a no-op and the op sits queued. Packet P's
    # opening STOP_CLICKED then runs seconds later, when KICK_PEERS first
    # pokes that executor, and cancels Calc's peer turn mid-flight (P1 never
    # sees write_formula_range).
    _post_to_soffice_vcl(_apply, sl=sl)


def _debug_sidebar_query(op: str, uid: str = "") -> str:
    """``INFLATE_HISTORY`` or ``INFLATE_HISTORY&uid=34`` for executeDispatch."""
    token = (op or "SNAPSHOT").strip()
    uid = str(uid or "").strip()
    if uid:
        return "%s&uid=%s" % (token, uid)
    return token


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
    uid = ""
    try:
        from plugin.framework.uno_context import get_runtime_uid

        uid = get_runtime_uid(doc) or ""
    except Exception:
        uid = ""
    url = "%s:%s?%s" % (EXTENSION_ID_WRITERAGENT, _DEBUG_SIDEBAR_PREFIX, _debug_sidebar_query(op, uid))
    smgr = uno_ctx.getServiceManager()
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", uno_ctx)
    helper.executeDispatch(frame, url, "", 0, ())
    if op.upper() != "SNAPSHOT":
        time.sleep(0.25)
        snap_url = "%s:%s?%s" % (
            EXTENSION_ID_WRITERAGENT,
            _DEBUG_SIDEBAR_PREFIX,
            _debug_sidebar_query("SNAPSHOT", uid),
        )
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


def _urp_record_click_took() -> bool:
    """True once a URP Record click left the idle Record state.

    Stop Rec is the usual sign. A stub silence auto-stop can flip the label
    straight on to Send (busy, Stop enabled) between two polls.
    """
    label = _send_label_lower()
    if "stop rec" in label or (label and "record" not in label):
        return True
    ctx = _HOOK_CTX
    if ctx is None:
        return False
    try:
        controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    except Exception:
        return False
    return control_enabled(controls.get("stop")) is True


def _send_event_or_urp(kind: SendEventKind, *, listener: Any = None) -> None:
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        sl.dispatch(SendEvent(kind))
        return
    if kind == SendEventKind.RECORD_CLICKED:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if "record" in _send_label_lower() and "stop rec" not in _send_label_lower():
                break
            time.sleep(0.05)
        if _try_click_send_for_kind(kind):
            # Any move off Record, or Stop enabled, means it took. Counting
            # only Stop Rec misses G4's stub auto-stop, which sends the take
            # before a poll sees Stop Rec. The fallback op then starts a
            # second take that never stops (hidden while Stop is greyed
            # during takes, since idle = Stop off).
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if _urp_record_click_took():
                    return
                time.sleep(0.05)
        execute_debug_sidebar_op(kind.name)
        return
    if kind == SendEventKind.STOP_REC_CLICKED:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if "stop rec" in _send_label_lower():
                break
            time.sleep(0.05)
        if _try_click_send_for_kind(kind):
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if "stop rec" not in _send_label_lower():
                    return
                time.sleep(0.05)
        execute_debug_sidebar_op(kind.name)
        return
    if _try_click_send_for_kind(kind):
        return
    execute_debug_sidebar_op(kind.name)


def _panel_frame(panel: Any) -> Any:
    return getattr(panel, "xFrame", None) or getattr(panel, "Frame", None)


def _frames_match(left: Any, right: Any) -> bool:
    """True when *left* and *right* are the same frame.

    PyUNO hands out distinct wrappers; bare ``is`` misses after Packet P / E12
    reopen the Writer deck. ``uno_same`` is the product identity test.

    Off the main thread, identity is ``is`` only. ``uno_same`` is
    main-thread-only. ``handle_debug_sidebar_command`` runs on the URP
    thread, the guard raises, and this ``except`` turns that into a miss.
    ``sidebar_panel`` then returns ``panels[0]``.
    """
    if left is None or right is None:
        return False
    if left is right:
        return True
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return False
    try:
        from plugin.framework.uno_context import uno_same

        return bool(uno_same(left, right))
    except Exception:
        return False


def _listener_torn_down(listener: Any) -> bool:
    """True when deck close already set ``_panel_teardown`` on the listener."""
    try:
        return bool(getattr(listener, "_panel_teardown", False))
    except Exception:
        return False


def _panel_send_listener(panel: Any) -> Any:
    if panel is None:
        return None
    listener = getattr(panel, "send_listener", None)
    if listener is None or _listener_torn_down(listener):
        return None
    return listener


def _prune_teardown_listeners() -> None:
    """Drop closed decks from the strong adopt list so they can be collected."""
    if not _LIVE_SEND_LISTENERS:
        return
    kept = [obj for obj in _LIVE_SEND_LISTENERS if not _listener_torn_down(obj)]
    if len(kept) != len(_LIVE_SEND_LISTENERS):
        _LIVE_SEND_LISTENERS[:] = kept


def _current_frame() -> Any:
    """Frame of ``desktop.getCurrentComponent()``, or None.

    Packet K inflate / URP debug ops must bind this frame. WeakSet[0] is
    leftover Calc after Packet P / E12 (those panels stay alive because
    ``_LIVE_SEND_LISTENERS`` holds a strong ref).
    """
    try:
        ctx = _HOOK_CTX
        if ctx is None:
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()
        doc = current_component(ctx)
        if doc is None:
            return None
        return doc.getCurrentController().getFrame()
    except Exception:
        return None


def sidebar_panel(frame: Any = None, *, uid: str = "") -> Any:
    """Return the live ``ChatPanelElement`` for *uid* or *frame*.

    When *frame* is omitted and several decks are live, prefer the current
    component. A single live panel is still returned when nothing was named.

    A named frame that does not match returns None. ``panels[0]`` is
    only the no-frame, one-deck debug fallback. A frame miss (including
    ``uno_same`` raising off the URP thread) that falls through to
    ``panels[0]`` is the leftover-Calc pad from Packet K: both the match
    branch and the single-panel branch used to return the first panel
    even when the caller had already named a frame.
    """
    _require_debug()
    panels = iter_live_chat_panels()
    if not panels:
        return None
    token = str(uid or "").strip()
    if token:
        for panel in panels:
            if str(getattr(panel, "_live_panel_uid", "") or "") == token:
                return panel
        return None
    if frame is not None:
        for panel in panels:
            if _frames_match(_panel_frame(panel), frame):
                return panel
        return None
    target = _current_frame()
    if target is not None:
        for panel in panels:
            if _frames_match(_panel_frame(panel), target):
                return panel
    if len(panels) == 1:
        return panels[0]
    return None


def desktop_from_ctx(ctx: Any) -> Any:
    """Desktop from the remote ``ctx`` without ``get_ctx()`` (avoids disposed fallbacks)."""
    _require_debug()
    smgr = ctx.getServiceManager()
    return smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)


def current_component(ctx: Any) -> Any:
    _require_debug()
    return desktop_from_ctx(ctx).getCurrentComponent()


def component_is_calc(doc: Any) -> bool:
    """``supportsService`` over URP. Do not use ``is_calc`` (``@main_thread_only``)."""
    _require_debug()
    if doc is None:
        return False
    try:
        return bool(doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"))
    except Exception:
        return False


def iter_desktop_components(ctx: Any) -> list[Any]:
    """Open models from ``XDesktop.getComponents()`` (URP-safe enumeration)."""
    _require_debug()
    desktop = desktop_from_ctx(ctx)
    comps = getattr(desktop, "getComponents", lambda: None)()
    if comps is None or not hasattr(comps, "createEnumeration"):
        return []
    enum = comps.createEnumeration()
    out: list[Any] = []
    while True:
        try:
            if not enum.hasMoreElements():
                break
            out.append(enum.nextElement())
        except Exception:
            break
    return out


def find_calc_component(ctx: Any) -> Any:
    """First open Calc model, or None. Does not load a document."""
    _require_debug()
    for doc in iter_desktop_components(ctx):
        if component_is_calc(doc):
            return doc
    return None


def close_component(doc: Any) -> None:
    """Close *doc* if it is still alive. Swallows dispose-after-close."""
    _require_debug()
    if doc is None:
        return
    try:
        if hasattr(doc, "close"):
            doc.close(True)
        elif hasattr(doc, "dispose"):
            doc.dispose()
    except Exception:
        pass


def _load_visible_calc_factory() -> None:
    """Create a visible Calc on soffice VCL. ``_blank`` keeps the Writer window.

    URP ``loadComponentFromURL('private:factory/scalc')`` after a Writer deck
    never returns (120s watchdog; File→New Spreadsheet on VCL is fine). This
    job must run via :func:`_post_to_soffice_vcl`, not on the Dummy URP thread.
    """
    from plugin.framework.uno_context import get_ctx, get_desktop

    try:
        desktop = get_desktop(get_ctx())
        desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
        log.info("OPEN_CALC: loaded factory/scalc _blank on VCL")
    except Exception:
        log.exception("OPEN_CALC: factory/scalc _blank failed")
        raise


def _post_to_soffice_vcl(fn: Callable[[], None], *, sl: Any = None) -> None:
    """Enqueue *fn* on soffice VCL. Do not run it inline on the URP Dummy thread.

    ``WRITERAGENT_TESTING=1`` makes ``QueueExecutor.post`` inline. Force-marshal
    so AsyncCallback runs the work on VCL. ``execute()`` from URP dispatch
    deadlocks (office idle, tests wait forever) — same as Packet G FSM ops.
    """
    qe = getattr(sl, "queue_executor", None) if sl is not None else None
    if qe is None:
        from plugin.framework.queue_executor import default_executor

        qe = default_executor
    # force_marshal skips _get_async_callback inside post(); without a prior
    # init, _poke_main_thread is a no-op and the factory load never runs
    # (E12 2026-09-08: "poke skipped (no AsyncCallback)" after OPEN_CALC).
    init_cb = getattr(qe, "_get_async_callback", None)
    if callable(init_cb):
        try:
            init_cb()
        except Exception:
            log.exception("debug_sidebar: AsyncCallback init failed")
    from plugin.framework.queue_executor import set_force_marshal_mode

    set_force_marshal_mode(True)
    try:
        qe.post(fn)
    finally:
        set_force_marshal_mode(False)


def open_calc_document(ctx: Any, *, timeout: float = 30.0) -> Any:
    """Open a visible Calc after a Writer deck without blocking URP on factory/scalc.

    Dual-peer / E12 / G17 recipe: keep Writer open, call this, then
    :func:`adopt_chat_sidebar` on the returned model. Never call
    ``desktop.loadComponentFromURL('private:factory/scalc', …)`` from the
    URP test process after ``show_writeragent_chat_deck``.
    """
    _require_debug()
    existing = find_calc_component(ctx)
    if existing is not None:
        return existing
    execute_debug_sidebar_op("OPEN_CALC", ctx=ctx)
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() <= deadline:
        found = find_calc_component(ctx)
        if found is not None:
            return found
        time.sleep(0.25)
    raise RuntimeError(
        "Calc did not appear after OPEN_CALC (factory/scalc was posted to VCL; "
        "do not loadComponentFromURL factory/scalc over URP after a Writer deck)"
    )


def adopt_chat_sidebar(ctx: Any, doc: Any, *, timeout: float = 20.0) -> tuple[Any, Any]:
    """Show WriterAgentDeck on *doc* and return ``(controls, send_listener)``."""
    _require_debug()
    controls = wait_for_chat_dialog_controls(ctx, timeout=timeout, doc=doc)
    adopt_runtime_send_listeners()
    frame = None
    try:
        if doc is not None:
            frame = doc.getCurrentController().getFrame()
    except Exception:
        frame = None
    return controls, send_listener(frame)


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
    "slash_popup",
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


def send_listener(frame: Any = None, *, uid: str = "") -> Any:
    _require_debug()
    _prune_teardown_listeners()
    panels = iter_live_chat_panels()
    panel = sidebar_panel(frame, uid=uid)
    sl = _panel_send_listener(panel)
    if sl is not None:
        # Do not steal a leftover slash-popup listener from another deck.
        # Packet K inflate + URP Send must share this panel's ChatSession.
        return sl
    # A named frame or uid already picked a deck. Do not substitute another
    # window's listener when that deck missed (uno_same off the URP thread
    # used to fall through to panels[0] / the last adopted listener).
    if frame is not None or panel is not None or uid or len(panels) > 1:
        return None
    with_popup = [
        obj for obj in _LIVE_SEND_LISTENERS if getattr(obj, "slash_popup", None) is not None and not _listener_torn_down(obj)
    ]
    if with_popup:
        return with_popup[-1]
    if _LIVE_SEND_LISTENERS:
        return _LIVE_SEND_LISTENERS[-1]
    return None


def _listener_for_current_doc() -> Any:
    """SendButtonListener for the current component (Packet K inflate)."""
    try:
        ctx = _HOOK_CTX
        if ctx is None:
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()
        sl = send_listener_for_doc(current_component(ctx))
        if sl is not None:
            return sl
    except Exception:
        pass
    return send_listener()


def _listener_with_slash_popup(sl: Any) -> Any:
    """Prefer a SendButtonListener that already has the Ask-box controller."""
    if sl is not None and not _listener_torn_down(sl) and getattr(sl, "slash_popup", None) is not None:
        return sl
    for obj in list(_LIVE_SEND_LISTENERS):
        if _listener_torn_down(obj):
            continue
        if getattr(obj, "slash_popup", None) is not None:
            return obj
    try:
        panels = iter_live_chat_panels()
    except Exception:
        panels = []
    for panel in panels:
        cand = _panel_send_listener(panel)
        if cand is not None and getattr(cand, "slash_popup", None) is not None:
            return cand
    if _listener_torn_down(sl):
        return None
    return sl


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
        if _listener_torn_down(obj):
            continue
        if obj not in _LIVE_SEND_LISTENERS:
            _LIVE_SEND_LISTENERS.append(obj)
            found += 1
    _prune_teardown_listeners()
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


def wait_for_chat_dialog_controls(
    ctx: Any, timeout: float = 20.0, *, doc: Any = None
) -> dict[str, Any] | None:
    """Show WriterAgentDeck until query+send exist. Does not pump VCL over URP.

    Pass *doc* to target a specific model (Calc after :func:`open_calc_document`,
    Packet P dual Writer+Calc). Default is ``current_component``.
    """
    global _HOOK_CTX
    _HOOK_CTX = ctx
    _require_debug()
    deadline = time.monotonic() + max(0.0, timeout)
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            target = doc if doc is not None else current_component(ctx)
            show_writeragent_chat_deck(ctx, target)
            last = chat_dialog_controls(ctx, target)
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


def send_listener_for_doc(doc: Any) -> Any:
    """``send_listener`` bound to *doc*'s frame (dual-deck Packet P)."""
    _require_debug()
    if doc is None:
        return None
    try:
        frame = doc.getCurrentController().getFrame()
    except Exception:
        return None
    return send_listener(frame)


def send_listener_for_uid(uid: str) -> Any:
    """``SendButtonListener`` for *uid* (production live-panel map, then debug walk).

    Packet K inflate must not use soffice ``getCurrentComponent()`` when the
    URP client already named the Writer RuntimeUID. Leftover Calc after
    Packet P / E12 stays current in soffice and used to eat INFLATE_HISTORY.
    """
    _require_debug()
    token = str(uid or "").strip()
    if not token:
        return None
    try:
        from plugin.doc.live_panels import get_live_panel

        panel = get_live_panel(token)
    except Exception:
        panel = None
    sl = _panel_send_listener(panel)
    if sl is not None:
        return sl
    # Debug WeakSet panels store the uid at register. Match that before
    # get_runtime_uid, which is main-thread-only and raises on the URP thread.
    sl = _panel_send_listener(sidebar_panel(uid=token))
    if sl is not None:
        return sl
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return None
    from plugin.framework.uno_context import get_runtime_uid

    for sl in iter_send_listeners():
        frame = getattr(sl, "frame", None)
        if frame is None:
            continue
        try:
            model = frame.getController().getModel()
            if str(get_runtime_uid(model) or "") == token:
                return sl
        except Exception:
            continue
    return None


def iter_send_listeners() -> list[Any]:
    """All live SendButtonListeners (panels first, then adopted OXT copies)."""
    _require_debug()
    adopt_runtime_send_listeners()
    _prune_teardown_listeners()
    out: list[Any] = []
    seen: set[int] = set()
    try:
        panels = iter_live_chat_panels()
    except Exception:
        panels = []
    for panel in panels:
        sl = _panel_send_listener(panel)
        if sl is None:
            continue
        ident = id(sl)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(sl)
    for sl in list(_LIVE_SEND_LISTENERS):
        if _listener_torn_down(sl):
            continue
        ident = id(sl)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(sl)
    return out


def ensure_sidebar_chat_mode(
    controls: dict[str, Any] | None,
    *,
    doc_type: str = "writer",
    listener: Any = None,
) -> None:
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

        set_selector_mode_with_flags(sel, CHAT_MODE_CHAT, sidebar_mode_flags_for_doc_type(doc_type))
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        apply_fn = getattr(sl, "_apply_sidebar_mode_fn", None)
        if apply_fn is not None:
            from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

            apply_fn(CHAT_MODE_CHAT)
    else:
        execute_debug_sidebar_op("SET_CHAT_MODE")


def set_query_text_via_controls(controls: dict[str, Any], text: str, *, listener: Any = None) -> None:
    """Set the query box over URP so QueryTextListener can enable Send."""
    _require_debug()
    from plugin.chatbot.dialogs import set_control_text

    if "query" in controls:
        set_control_text(controls["query"], text)
    sl = listener if listener is not None else send_listener()
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
    saw_busy = False
    while time.monotonic() <= deadline:
        en = control_enabled(stop) if stop is not None else None
        busy = en is True
        if busy:
            saw_busy = True
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
            found = bool(suffix.strip()) or (not before and bool(body.strip()))
        if (saw_busy or found) and not busy:
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
        popup = getattr(sl, "slash_popup", None)
        on_text = getattr(popup, "on_query_text", None) if popup is not None else None
        if callable(on_text):
            on_text(text or "")
        return
    ctx = _HOOK_CTX
    if ctx is not None:
        controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
        set_query_text_via_controls(controls, text)
        # QueryTextListener may not fire over URP setText; refresh inside soffice.
        popup = ensure_slash_popup()
        on_text = getattr(popup, "on_query_text", None) if popup is not None else None
        if callable(on_text):
            on_text(text or "")
        elif (text or "").lstrip().startswith("/") or not (text or "").strip():
            execute_debug_sidebar_op("SLASH_REFRESH")


def query_text(*, listener: Any = None) -> str:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return ""
    from plugin.chatbot.dialogs import get_control_text

    return get_control_text(getattr(sl, "query_control", None), default="") or ""


def _slash_control_near_query(query: Any) -> Any:
    if query is None:
        return None
    for getter_name in ("getContext", "getPeer"):
        getter = getattr(query, getter_name, None)
        if not callable(getter):
            continue
        try:
            cur = getter()
        except Exception:
            continue
        hops = 0
        while cur is not None and hops < 6:
            hops += 1
            get_control = getattr(cur, "getControl", None)
            if callable(get_control):
                try:
                    ctrl = get_control("slash_popup")
                except Exception:
                    ctrl = None
                if ctrl is not None:
                    return ctrl
            parent = getattr(cur, "getParent", None)
            cur = parent() if callable(parent) else None
    return None


def _attach_slash_popup_on_listener(sl: Any) -> Any:
    if sl is None:
        return None
    popup = getattr(sl, "slash_popup", None)
    if popup is not None:
        return popup
    query = getattr(sl, "query_control", None)
    ctrl = _slash_control_near_query(query)
    if ctrl is None:
        return None
    from plugin.chatbot.slash_popup import SlashPopupController

    popup = SlashPopupController(ctrl, sl, query)
    sl.slash_popup = popup
    return popup


def _slash_lru_names() -> list[str]:
    try:
        from plugin.chatbot.slash_commands import load_slash_lru

        return list(load_slash_lru())
    except Exception:
        return []


def _slash_state_from_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "visible": bool(data.get("slash_visible")),
        "items": list(data.get("slash_items") or []),
        "selected": data.get("slash_selected"),
        "available": bool(data.get("slash_available")),
        "lru": list(data.get("slash_lru") or []),
    }


def _slash_snapshot_fields(sl: Any) -> dict[str, Any]:
    popup = _attach_slash_popup_on_listener(sl) if sl is not None else None
    if popup is None:
        return {"slash_available": False, "slash_visible": False, "slash_items": [], "slash_selected": None}
    return {
        "slash_available": True,
        "slash_visible": bool(getattr(popup, "is_open", False)),
        "slash_items": list(getattr(popup, "visible_names", None) or []),
        "slash_selected": getattr(popup, "selected_name", None),
    }


def _slash_refresh_in_soffice(sl: Any) -> None:
    from plugin.chatbot.dialogs import get_control_text

    popup = _attach_slash_popup_on_listener(sl)
    if popup is None:
        return
    popup.on_query_text(get_control_text(getattr(sl, "query_control", None), default="") or "")


def _slash_key_in_soffice(sl: Any, key_code: int, modifiers: int) -> None:
    popup = _attach_slash_popup_on_listener(sl)
    if popup is None:
        return
    popup.handle_key(int(key_code), int(modifiers))


class _UrpSlashHost:
    """Stand-in SendButtonListener so the popup can run over URP without gc."""

    query_control: Any
    response_control: Any
    clear_listener: Any
    _stop: Any

    def __init__(self, query: Any, response: Any, clear: Any, stop: Any) -> None:
        self.query_control = query
        self.response_control = response
        self.slash_popup: Any = None
        self.clear_listener = type(
            "ClearProxy",
            (),
            {"on_action_performed": staticmethod(lambda _ev: uno_click(clear) if clear is not None else None)},
        )()
        self._stop = stop

    def dispatch(self, event: Any) -> None:
        kind = getattr(event, "kind", None)
        if kind == SendEventKind.STOP_CLICKED and self._stop is not None:
            uno_click(self._stop)

    def _append_response(self, text: str, role: str = "assistant") -> None:
        from plugin.chatbot.dialogs import get_control_text, set_control_text

        # ``role`` matches SendButtonListener._append_response (run_slash_command).
        current = get_control_text(self.response_control, default="") or ""
        set_control_text(self.response_control, current + ("" if role is None else text))


def _urp_slash_controller() -> Any:
    """Bind ``SlashPopupController`` to the live sidebar ListBox over URP."""
    global _URP_SLASH_POPUP
    ctx = _HOOK_CTX
    if ctx is None:
        return None
    controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    ctrl = controls.get("slash_popup")
    query = controls.get("query")
    if ctrl is None or query is None:
        return None
    # URP getControl returns a new proxy each time; do not compare with ``is``.
    if _URP_SLASH_POPUP is not None:
        return _URP_SLASH_POPUP
    from plugin.chatbot.slash_popup import SlashPopupController

    response = controls.get("response_rich") or controls.get("response")
    host = _UrpSlashHost(query, response, controls.get("clear"), controls.get("stop"))
    popup = SlashPopupController(ctrl, host, query)
    host.slash_popup = popup
    _URP_SLASH_POPUP = popup
    return popup


def ensure_slash_popup(*, listener: Any = None) -> Any:
    """Return the Ask-box slash controller, attaching it if the XDL ListBox exists.

    OXT factory and checkout tests can see different ``SendButtonListener``
    objects. Re-bind from the live ``slash_popup`` control so hooks can drive
    the menu even when the listener we found was not the one wiring attached.
    Out-of-process mock-sidebar has no in-process listener; bind over URP.
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        popup = getattr(sl, "slash_popup", None)
        if popup is not None:
            return popup
        ctrl = None
        ctx = _HOOK_CTX
        if ctx is not None:
            controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
            ctrl = controls.get("slash_popup")
        if ctrl is None:
            return _urp_slash_controller()
        from plugin.chatbot.slash_popup import SlashPopupController

        popup = SlashPopupController(ctrl, sl, getattr(sl, "query_control", None) or ctrl)
        sl.slash_popup = popup
        return popup
    return _urp_slash_controller()


def slash_popup_state(*, listener: Any = None) -> dict[str, Any]:
    """Visible names and selection for the Ask-box slash completion menu."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    popup = getattr(sl, "slash_popup", None) if sl is not None else None
    if popup is None:
        popup = ensure_slash_popup(listener=sl)
    if popup is None:
        # Last resort: in-soffice snapshot (listener lives only in soffice).
        return _slash_state_from_snapshot(execute_debug_sidebar_op("SNAPSHOT"))
    visible = bool(getattr(popup, "is_open", False))
    items = list(getattr(popup, "visible_names", None) or [])
    selected = getattr(popup, "selected_name", None)
    if not items:
        ctrl = getattr(popup, "control", None)
        if ctrl is not None and hasattr(ctrl, "getItemCount"):
            try:
                count = int(ctrl.getItemCount() or 0)
                items = [str(ctrl.getItem(i)) for i in range(count)]
            except Exception:
                items = []
    return {
        "visible": visible,
        "items": items,
        "selected": selected,
        "available": True,
        "lru": _slash_lru_names(),
    }


def press_query_key(key_code: int, modifiers: int = 0, *, listener: Any = None) -> None:
    """Drive ``QueryKeyListener`` (Enter / Esc / arrows / Tab) on the Ask field."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        popup = ensure_slash_popup()
        handle = getattr(popup, "handle_key", None) if popup is not None else None
        if callable(handle) and handle(int(key_code), int(modifiers)) is True:
            return
        if int(key_code) == 1280 and int(modifiers) == 0:
            execute_debug_sidebar_op("SLASH_ENTER")
            return
        if int(key_code) == 1281:
            execute_debug_sidebar_op("SLASH_ESC")
            return
        raise RuntimeError("no live SendButtonListener")
    from plugin.chatbot.panel import QueryKeyListener

    event = type("KeyEvent", (), {"KeyCode": int(key_code), "Modifiers": int(modifiers), "Consume": False})()
    QueryKeyListener(sl).on_key_pressed(event)


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


def inflate_sidebar_history(*, ctx: Any = None) -> dict[str, Any]:
    """Grow ChatSession.messages in soffice past the mock compaction gate."""
    _require_debug()
    sl = None
    if ctx is not None:
        try:
            sl = send_listener_for_doc(current_component(ctx))
        except Exception:
            sl = None
    if sl is None:
        sl = send_listener()
    session = getattr(sl, "session", None) if sl is not None else None
    messages = getattr(session, "messages", None)
    # In-process listener only. A URP proxy has no ChatSession.messages list.
    if isinstance(messages, list):
        added = _inflate_session_history(session)
        data = _write_debug_snapshot(sl)
        data["inflate_pairs"] = added
        return data
    return execute_debug_sidebar_op("INFLATE_HISTORY", ctx=ctx)


def clear_sidebar_chat(*, listener: Any = None) -> None:
    """New-chat: same path as the Clear button.

    Packet G canned-string asserts must not see leftover Packet E/F body
    (``hello``, ``look up cats``, ``document_research``). URP clicks the
    Clear control. In-process, ``clear_listener.on_action_performed`` stops
    speech, drops hands-free, releases the mic, and latches Stop when busy.

    ``ClearButtonListener`` is the path that latches Stop first.
    ``session.clear`` and ``clear_and_greeting("")`` let a drain still
    paint the reply onto the wiped transcript.
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        clear_listener = getattr(sl, "clear_listener", None)
        action = getattr(clear_listener, "on_action_performed", None)
        if callable(action):
            action(None)
            return
        session = getattr(sl, "session", None)
        clearer = getattr(session, "clear", None) if session is not None else None
        if callable(clearer):
            clearer()
        widget = getattr(sl, "rich_text_widget", None)
        greet = getattr(widget, "clear_and_greeting", None) if widget is not None else None
        if callable(greet):
            greet("")
            return
        from plugin.chatbot.dialogs import set_control_text

        response = getattr(sl, "response_control", None)
        if response is not None:
            set_control_text(response, "")
        return
    ctx = _HOOK_CTX
    if ctx is None:
        return
    try:
        controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    except Exception:
        return
    clear_btn = controls.get("clear")
    if clear_btn is not None:
        try:
            uno_click(clear_btn)
            return
        except Exception:
            log.debug("clear_sidebar_chat URP Clear click failed", exc_info=True)
    from plugin.chatbot.dialogs import set_control_text

    response = controls.get("response_rich") or controls.get("response")
    if response is not None:
        set_control_text(response, "")


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
    """Short Record click. No-op unless the Send button label is Record.

    No-op unless the Send button label is Record. Dispatching
    ``RECORD_CLICKED`` when the label is Send does not match the URP click,
    which already requires the Record label. A real short click is mouse
    press then release (``actionPerformed`` is swallowed when that release
    already dispatched).
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        notify_record_mouse_pressed(sl)
        notify_record_mouse_released(sl)
        return
    _send_event_or_urp(SendEventKind.RECORD_CLICKED, listener=None)


def press_stop_rec(*, listener: Any = None) -> None:
    """Stop Rec click. Dispatches only when the label is Stop Rec.

    Mouse press/release owns the Record hold. On a Stop Rec label those
    notifies do not dispatch; ``actionPerformed`` does, and it refuses Send
    and Record labels. The URP branch clicks only when the label matches.
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        from plugin.framework.i18n import _

        notify_record_mouse_pressed(sl)
        notify_record_mouse_released(sl)
        if _control_label(getattr(sl, "send_control", None)) == _("Stop Rec"):
            action = getattr(sl, "on_action_performed", None)
            if callable(action):
                action(None)
        return
    _send_event_or_urp(SendEventKind.STOP_REC_CLICKED, listener=None)


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
