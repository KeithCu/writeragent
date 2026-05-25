#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco/pywebview editor child process (runs in the user venv, not inside LibreOffice)."""

from __future__ import annotations

import importlib
import logging
import os
import queue
import sys
import threading
import traceback
from typing import Any, NoReturn, cast

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_ready_lock = threading.Lock()
_ready_sent = False
_closed_lock = threading.Lock()
_closed_sent = False
_window: Any = None



def _bootstrap_plugin_import_path() -> None:
    """Ensure the directory that contains the ``plugin`` package is on sys.path."""
    candidates = [
        os.path.join(_SCRIPT_DIR, "..", ".."),
        os.path.join(_SCRIPT_DIR, "..", "..", ".."),
    ]
    for raw in candidates:
        root = os.path.abspath(raw)
        if os.path.isdir(os.path.join(root, "plugin")) and root not in sys.path:
            sys.path.insert(0, root)
            return
    plugin_parent = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
    repo = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
    if os.path.basename(plugin_parent) == "plugin" and repo not in sys.path:
        sys.path.insert(0, repo)


def _fatal(msg: str, *, exc: BaseException | None = None, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr, flush=True)
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    raise SystemExit(code)


_bootstrap_plugin_import_path()
try:
    from plugin.scripting.editor_launcher import _ASSETS_DIR
    from plugin.scripting.editor_protocol import message_type, read_message, write_message
    from plugin.scripting.editor_jedi import JediSession
except ImportError as e:
    _fatal(f"editor_main: cannot import plugin.scripting dependencies ({e}). sys.path={sys.path!r}", exc=e)


log = logging.getLogger(__name__)

_ui_queue: queue.Queue[dict[str, Any]] = queue.Queue()
_stdout_lock = threading.Lock()
_shutting_down = False


def _write_parent(message: dict[str, Any]) -> None:
    with _stdout_lock:
        write_message(sys.stdout.buffer, message)


def _send_ready_once() -> None:
    """Tell LibreOffice the GUI is up and stdin is ready for ``load`` messages."""
    global _ready_sent
    with _ready_lock:
        if _ready_sent:
            return
        _write_parent({"type": "ready"})
        _ready_sent = True
        log.info("editor_main: sent ready")


def _send_closed_once() -> None:
    """Tell LibreOffice the editor session ended (Cancel, WM close, or process exit)."""
    global _closed_sent
    with _closed_lock:
        if _closed_sent:
            return
        _closed_sent = True
    try:
        _write_parent({"type": "closed"})
        log.info("editor_main: sent closed")
    except Exception:
        log.debug("editor_main: closed write failed", exc_info=True)


def _pipe_reader_loop() -> None:
    global _shutting_down
    stdin = sys.stdin.buffer
    try:
        while not _shutting_down:
            msg = read_message(stdin)
            if msg is None:
                break
            kind = message_type(msg)
            if kind in ("saved", "error", "load"):
                _ui_queue.put(msg)
            elif kind == "closed":
                break
    except Exception:
        log.exception("Editor pipe reader failed")
    finally:
        _shutting_down = True
        log.info("editor_main: stdin reader finished; destroying window to exit event loop")
        try:
            if _window is not None:
                _window.destroy()
        except Exception:
            pass



class MonacoEditorApi:
    """JS API exposed via pywebview (runs on the GUI thread)."""

    def __init__(self) -> None:
        self._window: Any = None
        self._jedi = JediSession()

    def set_window(self, window: Any) -> None:
        self._window = window

    def poll_messages(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                msg = _ui_queue.get_nowait()
                batch.append(msg)
                if msg.get("type") == "load":
                    log.info("editor_main: poll_messages received load; showing window")
                    global _closed_sent
                    with _closed_lock:
                        _closed_sent = False
                    try:
                        if self._window is not None:
                            self._window.show()
                    except Exception:
                        log.exception("editor_main: failed to show window on load")
            except queue.Empty:
                break
        return batch

    def get_completions(self, code: str, line: int, column: int) -> dict[str, Any]:
        return self._jedi.get_completions(code, line, column)

    def notify_save(self, code: str, save_as_plain: bool = False, data_binding: str = "", action: str = "cell_save") -> None:
        if not isinstance(code, str):
            code = str(code) if code is not None else ""
        if not isinstance(data_binding, str):
            data_binding = str(data_binding) if data_binding is not None else ""
        payload: dict[str, Any] = {
            "type": "save",
            "code": code,
            "save_as_plain": bool(save_as_plain),
            "data_binding": data_binding,
        }
        if action and action != "cell_save":
            payload["action"] = action
        _write_parent(payload)

    def notify_run(self, code: str) -> None:
        self.notify_save(code, action="run")

    def notify_save_script(self, code: str) -> None:
        self.notify_save(code, action="save")

    def notify_cancel(self) -> None:
        log.info("editor_main: notify_cancel called; hiding window")
        _send_closed_once()
        try:
            self._window.hide()
        except Exception:
            pass


def _handle_window_closing() -> bool:
    """Hides the window instead of closing/destroying it, notifying the parent."""
    log.info("editor_main: intercepting window close. Hiding window instead.")
    _send_closed_once()
    try:
        if _window is not None:
            _window.hide()
    except Exception:
        log.exception("editor_main: failed to hide window during close interception")
    return False  # Aborts standard window close/destruction


def _bind_window_events(window: Any) -> None:
    """Fire ``ready`` after show; ``closed`` when the user closes the window (WM X button)."""
    events = getattr(window, "events", None)
    if events is None:
        return
    closing_ev = getattr(events, "closing", None)
    if closing_ev is not None:
        try:
            closing_ev += _handle_window_closing
            log.info("editor_main: hooked window.events.closing")
        except Exception:
            log.debug("editor_main: could not hook events.closing", exc_info=True)
    closed_ev = getattr(events, "closed", None)
    if closed_ev is not None:
        try:
            closed_ev += _send_closed_once
            log.info("editor_main: hooked window.events.closed")
        except Exception:
            log.debug("editor_main: could not hook events.closed", exc_info=True)
    for name in ("loaded", "shown"):
        ev = getattr(events, name, None)
        if ev is None:
            continue
        try:
            ev += _send_ready_once
            log.info("editor_main: hooked window.events.%s for ready", name)
            return
        except Exception:
            log.debug("editor_main: could not hook events.%s", name, exc_info=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    assets = os.path.abspath(os.environ.get("WRITERAGENT_EDITOR_ASSETS", _ASSETS_DIR))
    index_html = os.path.join(assets, "index.html")
    if not os.path.isfile(index_html):
        _fatal(f"Editor assets not found: {index_html}")

    try:
        webview = cast("Any", importlib.import_module("webview"))
    except ImportError as e:
        _fatal(f"pywebview is not installed in this interpreter: {e}", exc=e)

    # Listen for parent messages before the GUI loop blocks the main thread.
    threading.Thread(target=_pipe_reader_loop, name="editor-stdin-reader", daemon=True).start()

    api = MonacoEditorApi()
    # pywebview resolves relative URLs against dirname(sys.argv[0]) (plugin/scripting/),
    # not cwd. Pass an absolute path so the HTTP server root is contrib/scripting/assets/editor/.
    log.info("editor_main: assets=%s index=%s argv0=%s", assets, index_html, sys.argv[0])
    print(f"editor_main: serving {index_html}", file=sys.stderr, flush=True)
    global _window
    try:
        window = webview.create_window("PYTHON Editor", url=index_html, width=900, height=640, js_api=api)
        _window = window
    except Exception as e:
        _fatal(f"webview.create_window failed: {e}", exc=e)

    api.set_window(window)
    _bind_window_events(window)

    start_kw: dict[str, Any] = {"debug": False, "http_server": True}
    gui = os.environ.get("WRITERAGENT_PYWEBVIEW_GUI", "").strip()
    if gui:
        start_kw["gui"] = gui

    try:
        webview.start(**start_kw)
    except Exception as e:
        _fatal(f"webview.start failed: {e}", exc=e)
    finally:
        _send_closed_once()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1) from None
