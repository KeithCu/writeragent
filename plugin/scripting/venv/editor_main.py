#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco/pywebview editor child process (runs in the user venv, not inside LibreOffice)."""

from __future__ import annotations

import concurrent.futures
import logging
import mimetypes
import os
import queue
import sys
import threading
import traceback
from typing import Any, IO, NoReturn

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_ready_lock = threading.Lock()
_ready_sent = False
_closed_lock = threading.Lock()
_closed_sent = False
_window: Any = None
_ipc_stream: IO[bytes] = sys.stdout.buffer


def _bootstrap_plugin_import_path() -> None:
    """Ensure the directory that contains the ``plugin`` package is on sys.path."""
    candidates = [
        os.path.join(_SCRIPT_DIR, "..", "..", ".."),
        os.path.join(_SCRIPT_DIR, "..", "..", "..", ".."),
    ]
    for raw in candidates:
        root = os.path.abspath(raw)
        if os.path.isdir(os.path.join(root, "plugin")) and root not in sys.path:
            sys.path.insert(0, root)
            break
    # Running as a script puts plugin/scripting/venv at sys.path[0], which could shadow venv packages.
    if _SCRIPT_DIR in sys.path:
        sys.path.remove(_SCRIPT_DIR)


def _fatal(msg: str, *, exc: BaseException | None = None, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr, flush=True)
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    raise SystemExit(code)


log = logging.getLogger(__name__)

_bootstrap_plugin_import_path()

try:
    from plugin.framework.uno_bootstrap import register_alias_importer
    register_alias_importer()
except Exception as e:
    log.warning("Could not register alias importer in editor child process: %s", e)

try:
    from plugin.scripting.venv.venv_sandbox import apply_auto_imports
except ImportError:
    apply_auto_imports = None  # type: ignore[assignment]

_ASSETS_DIR = os.path.normpath(
    os.path.join(_SCRIPT_DIR, "..", "..", "contrib", "scripting", "assets", "editor")
)
try:
    from plugin.scripting.editor_ipc import (
    EDITOR_DEFAULT_TITLE,
    message_type,
    normalize_target,
    read_message,
    session_id_of,
    stamp_session,
    write_message,
)
except ImportError as e:
    _fatal(f"editor_main: cannot import plugin.scripting dependencies ({e}). sys.path={sys.path!r}", exc=e)

# Try to import jedi dynamically. If missing, we degrade gracefully without autocomplete.
try:
    import jedi  # type: ignore
except ImportError:
    jedi = None


class JediSession:
    """Manages the persistent jedi.Environment for sub-10ms completions."""

    def __init__(self) -> None:
        self._env: Any = None
        if jedi is None:
            log.warning("jedi is not installed in the current Python environment")
            return

        try:
            self._env = jedi.create_environment(sys.executable)
            log.info("Successfully created persistent Jedi environment for %s", sys.executable)
        except Exception as e:
            log.warning("Could not create persistent Jedi environment, falling back to default: %s", e)

    def is_available(self) -> bool:
        return jedi is not None

    def get_completions(self, code: str, line: int, column: int, *, max_docstrings: int = 20) -> dict[str, Any]:
        if not self.is_available() or jedi is None:
            return {"items": []}

        try:
            lines_added = 0
            if apply_auto_imports is not None:
                code, lines_added = apply_auto_imports(code)
            target_line = line + lines_added
            col_idx = max(0, column - 1)

            script = jedi.Script(code, environment=self._env)
            completions = script.complete(target_line, col_idx)

            items = []
            for idx, comp in enumerate(completions):
                doc = ""
                if idx < max_docstrings:
                    try:
                        doc = comp.docstring()
                    except Exception:
                        doc = ""

                items.append({
                    "label": comp.name,
                    "kind": comp.type,
                    "insertText": comp.name,
                    "detail": comp.description or "",
                    "documentation": doc or "",
                })

            return {"items": items}
        except Exception:
            log.exception("Jedi completions failed")
            return {"items": []}


_ui_queue: queue.Queue[dict[str, Any]] = queue.Queue()
_stdout_lock = threading.Lock()
_shutting_down = False
_jedi_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="jedi")
_current_session_id = ""
_current_mode = ""
_current_target: dict[str, str] = {}


def _remember_session(msg: dict[str, Any]) -> None:
    global _current_session_id, _current_mode, _current_target
    sid = session_id_of(msg)
    if sid:
        _current_session_id = sid
    mode = str(msg.get("mode") or "")
    if mode:
        _current_mode = mode
    raw_target = msg.get("target")
    if isinstance(raw_target, dict):
        _current_target = normalize_target(raw_target)


def _remember(session_id: str = "", mode: str = "", target: Any = None) -> None:
    if session_id:
        _remember_session({"session_id": session_id, "mode": mode, "target": target or {}})


def _put_ui(msg: dict[str, Any]) -> None:
    _ui_queue.put(msg)


def _reset_closed_sent() -> None:
    global _closed_sent
    with _closed_lock:
        _closed_sent = False


def _write_parent(message: dict[str, Any]) -> None:
    kind = message_type(message)
    if kind and kind != "ready":
        message = stamp_session(
            message,
            session_id=_current_session_id,
            mode=_current_mode,
            target=_current_target,
        )
    with _stdout_lock:
        write_message(_ipc_stream, message)


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
            # Queue for poll_messages. Do not call evaluate_js here: this thread
            # is the stdin reader, and evaluate_js deadlocks GTK / WebView2.
            # scripts_manager.js already feeds poll_messages into
            # handleScriptsManagerMessage, so a direct call also applied load twice.
            if kind in ("saved", "error", "load", "request_save", "theme", "scripts_list"):
                if kind == "load":
                    _remember_session(msg)
                    _reset_closed_sent()
                _put_ui(msg)
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

    _jedi: JediSession
    _completion_gen: int = 0

    def __init__(self) -> None:
        self._window: Any = None
        self._jedi = JediSession()
        self._completion_gen = 0

    def set_window(self, window: Any) -> None:
        self._window = window

    def poll_messages(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                msg = _ui_queue.get_nowait()
            except queue.Empty:
                break
            batch.append(msg)
            if msg.get("type") == "load":
                log.info("editor_main: poll_messages received load; showing window")
                _remember_session(msg)
                _reset_closed_sent()
                try:
                    if self._window is not None:
                        title = msg.get("title")
                        if title:
                            self._window.title = title
                        self._window.show()
                except Exception:
                    log.exception("editor_main: failed to show window on load")
        return batch

    def get_completions(self, code: str, line: int, column: int) -> dict[str, Any]:
        self._completion_gen += 1
        gen = self._completion_gen

        def _run_jedi(target_gen: int) -> dict[str, Any]:
            if target_gen != self._completion_gen:
                return {"items": []}
            return self._jedi.get_completions(code, line, column)

        future = _jedi_pool.submit(_run_jedi, gen)
        try:
            res = future.result(timeout=5)
            if gen != self._completion_gen:
                return {"items": []}
            return res
        except Exception:
            log.exception("Jedi completions timed out or failed")
            return {"items": []}

    def is_jedi_available(self) -> bool:
        return self._jedi.is_available()

    def notify_dirty(self, dirty: bool = True, session_id: str = "", mode: str = "", target: Any = None) -> None:
        _remember(session_id, mode, target)
        _write_parent({"type": "dirty", "dirty": bool(dirty)})

    def notify_save(
        self,
        code: str,
        save_as_plain: bool = False,
        data_binding: str = "",
        action: str = "cell_save",
        session_id: str = "",
        mode: str = "",
        target: Any = None,
    ) -> None:
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
        _remember(session_id, mode, target)
        _write_parent(payload)

    def notify_run(self, code: str, session_id: str = "", mode: str = "", target: Any = None) -> None:
        self.notify_save(code, action="run", session_id=session_id, mode=mode, target=target)

    def notify_save_script(self, code: str, session_id: str = "", mode: str = "", target: Any = None) -> None:
        self.notify_save(code, action="save", session_id=session_id, mode=mode, target=target)

    def _forward(self, type_name: str, **fields: Any) -> None:
        _write_parent({"type": type_name, **fields})

    def request_scripts(self) -> None:
        self._forward("request_scripts")

    def save_script(self, name: str, code: str, origin: str = "user") -> None:
        self._forward("save_script", name=name, code=code, origin=origin)

    def attach_script(self, name: str, code: str, overwrite: bool = False) -> None:
        self._forward("attach_script", name=name, code=code, overwrite=overwrite)

    def copy_script_to_user(self, name: str, code: str, overwrite: bool = False) -> None:
        self._forward("copy_script_to_user", name=name, code=code, overwrite=overwrite)

    def delete_script(self, name: str, origin: str = "user") -> None:
        self._forward("delete_script", name=name, origin=origin)

    def select_script(self, name: str) -> None:
        self._forward("select_script", name=str(name) if name is not None else "")

    def notify_cancel(self) -> None:
        log.info("editor_main: notify_cancel called; hiding window")
        _send_closed_once()
        _hide_and_clear(self._window)


def _hide_and_clear(win: Any) -> None:
    try:
        if win is not None:
            win.hide()
    except Exception:
        pass


def _handle_window_closing() -> bool:
    """Hides the window instead of closing/destroying it, notifying the parent."""
    # Return True while shutting down so the window can be destroyed. False
    # cancels close and leaves the editor process running after stdin EOF.
    if _shutting_down:
        return True
    log.info("editor_main: intercepting window close. Hiding window instead.")
    _send_closed_once()
    _hide_and_clear(_window)
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


def monaco_static_path(url_path: str, assets: str, rocher_root: str) -> str | None:
    """Map a request path to a file, or None.

    ``/vs//etc/passwd`` used to make path[4:] absolute, and os.path.join then
    dropped the rocher root. ``/vs/../../`` escaped after normpath.
    """
    if url_path in ("/", "/index.html"):
        return os.path.join(assets, "index.html")
    if url_path in ("/editor.js", "/scripts_manager.js", "/style.css"):
        return os.path.join(assets, url_path.lstrip("/"))
    if not url_path.startswith("/vs/"):
        return None
    # ImportError passes an empty root. realpath("") is the cwd, which would
    # serve files outside the Monaco tree.
    if not rocher_root:
        return None
    root = os.path.realpath(rocher_root)
    raw_suffix = url_path[4:]
    # `/vs//etc/passwd` makes path[4:] absolute, so os.path.join drops the
    # rocher root. Refuse that form instead of re-rooting it under Monaco.
    # On Windows (3.13+) isabs("/etc/passwd") is False (no drive), so check
    # for a leading separator too.
    if raw_suffix.startswith(("/", "\\")) or os.path.isabs(raw_suffix):
        return None
    suffix = raw_suffix.lstrip("/")
    candidate = os.path.realpath(os.path.normpath(os.path.join(root, suffix)))
    try:
        if os.path.commonpath([candidate, root]) != root:
            return None
    except ValueError:
        return None
    return candidate


try:
    import rocher  # type: ignore[import-untyped]
    _ROCHER_ROOT: str = rocher.path()
except Exception:
    _ROCHER_ROOT = ""


def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    path = environ.get("PATH_INFO", "")
    rocher_root = _ROCHER_ROOT if path.startswith("/vs/") else ""
    assets = os.path.abspath(os.environ.get("WRITERAGENT_EDITOR_ASSETS", _ASSETS_DIR))
    filepath = monaco_static_path(path, assets, rocher_root) or ""

    if filepath and os.path.exists(filepath) and os.path.isfile(filepath):
        mime, _unused = mimetypes.guess_type(filepath)
        if not mime:
            mime = "application/octet-stream"
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            start_response("200 OK", [("Content-Type", mime), ("Content-Length", str(len(content)))])
            return [content]
        except Exception as e:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain")])
            return [str(e).encode("utf-8")]
    else:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]


def main() -> None:
    global _ipc_stream
    from plugin.scripting.ipc import claim_ipc_channel
    _ipc_stream = claim_ipc_channel()

    logging.basicConfig(level=logging.INFO)
    assets = os.path.abspath(os.environ.get("WRITERAGENT_EDITOR_ASSETS", _ASSETS_DIR))
    index_html = os.path.join(assets, "index.html")
    if not os.path.isfile(index_html):
        _fatal(f"Editor assets not found: {index_html}")

    try:
        import webview  # type: ignore[import-untyped]
    except ImportError as e:
        _fatal(f"pywebview is not installed in this interpreter: {e}", exc=e)

    # Listen for parent messages before the GUI loop blocks the main thread.
    threading.Thread(target=_pipe_reader_loop, name="editor-stdin-reader", daemon=True).start()

    api = MonacoEditorApi()
    log.info("editor_main: assets=%s index=%s argv0=%s", assets, index_html, sys.argv[0])
    print(f"editor_main: serving {index_html} via WSGI app", file=sys.stderr, flush=True)
    global _window
    try:
        window = webview.create_window(EDITOR_DEFAULT_TITLE, url=wsgi_app, width=900, height=640, js_api=api, hidden=True)
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
