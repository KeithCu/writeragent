#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco/pywebview editor child process (runs in the user venv, not inside LibreOffice)."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import sys
import threading
import time
import traceback
from typing import Any, IO, NoReturn

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_ready_lock = threading.Lock()
_ready_sent = False
_session_lock = threading.Lock()
_closed_sent = False
_closed_session_id = ""
_lifecycle_lock = threading.Lock()
_gui_loop_entered = False
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
    PARENT_TO_CHILD_TYPES,
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
_JEDI_CALL_TIMEOUT_SEC = 5.0
_current_session_id = ""
_current_mode = ""
_current_target: dict[str, str] = {}


def _remember_session(msg: dict[str, Any]) -> None:
    # Bugfix: Session globals were previously modified without locking,
    # allowing concurrent threads (pipe reader vs. GUI thread) to observe or
    # stamp partially updated, mismatched session fields. Computing
    # normalized target outside the lock and assigning the three fields
    # under _session_lock guarantees consistent session snapshots.
    global _current_session_id, _current_mode, _current_target
    sid = session_id_of(msg)
    mode = str(msg.get("mode") or "")
    raw_target = msg.get("target")
    new_target = normalize_target(raw_target) if isinstance(raw_target, dict) else None
    with _session_lock:
        if sid:
            _current_session_id = sid
        if mode:
            _current_mode = mode
        if new_target is not None:
            _current_target = new_target


def _note_load(msg: dict[str, Any]) -> None:
    """The reader publishes identity as soon as the load frame arrives; poll_messages applies the same helper again on the GUI thread immediately before show."""
    _remember_session(msg)
    _reset_closed_sent()


def _put_ui(msg: dict[str, Any]) -> None:
    _ui_queue.put(msg)


def _reset_closed_sent() -> None:
    global _closed_sent
    with _session_lock:
        _closed_sent = False


def _session_snapshot() -> tuple[str, str, dict[str, str]]:
    with _session_lock:
        return _current_session_id, _current_mode, dict(_current_target)


def _emit(message: dict[str, Any]) -> None:
    with _stdout_lock:
        write_message(_ipc_stream, message)


def _write_parent(message: dict[str, Any]) -> None:
    kind = message_type(message)
    if kind and kind != "ready":
        sid, mode, target = _session_snapshot()
        message = stamp_session(
            message,
            session_id=sid,
            mode=mode,
            target=target,
        )
    _emit(message)


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
    # Bugfix: A load frame arriving on the pipe thread could clear _closed_sent
    # after _send_closed_once set the flag and before writing the message, and
    # _write_parent then stamped the closed frame with the newly loaded session,
    # causing the host to close the buffer it had just opened. Building the
    # stamped payload under _session_lock alongside updating the closed flag
    # and session id ensures the closed frame is always stamped with the
    # session identity captured when the close was accepted.
    global _closed_sent, _closed_session_id
    with _session_lock:
        if _closed_sent and _closed_session_id == _current_session_id:
            return
        _closed_sent = True
        _closed_session_id = _current_session_id
        payload = stamp_session(
            {"type": "closed"},
            session_id=_current_session_id,
            mode=_current_mode,
            target=dict(_current_target),
        )
    try:
        _emit(payload)
        log.info("editor_main: sent closed")
    except Exception:
        log.debug("editor_main: closed write failed", exc_info=True)


def _pipe_reader_loop(stdin: IO[bytes] | None = None) -> None:
    global _shutting_down
    stream: IO[bytes] = sys.stdin.buffer if stdin is None else stdin
    try:
        while not _shutting_down:
            msg = read_message(stream)
            if msg is None:
                break
            kind = message_type(msg)
            # Queue for poll_messages. Do not call evaluate_js here: this thread
            # is the stdin reader, and evaluate_js deadlocks GTK / WebView2.
            # scripts_manager.js already feeds poll_messages into
            # handleScriptsManagerMessage, so a direct call also applied load twice.
            if kind in PARENT_TO_CHILD_TYPES:
                if kind == "load":
                    _note_load(msg)
                _put_ui(msg)
            elif kind == "closed":
                break
    except Exception:
        # A bad length desynchronizes the one pipe shared by every session,
        # and truncation already returns None and takes the same exit.
        log.exception("Editor pipe reader failed")
    finally:
        win_to_destroy = None
        with _lifecycle_lock:
            _shutting_down = True
            if _gui_loop_entered:
                win_to_destroy = _window
        log.info("editor_main: stdin reader finished; destroying window to exit event loop")
        if win_to_destroy is not None:
            try:
                win_to_destroy.destroy()
            except Exception:
                pass


def _is_shutting_down() -> bool:
    with _lifecycle_lock:
        return _shutting_down


def _claim_gui_loop() -> bool:
    # Bugfix: The stdin reader used to start before _window existed; EOF set
    # _shutting_down and skipped destroy, leaving main to block in webview.start
    # with no load coming. Conversely, EOF between create_window and start destroyed
    # the window while start still ran. The claim and the reader's destroy share
    # _lifecycle_lock, ensuring destroy runs only after the claim has entered the loop.
    global _gui_loop_entered
    with _lifecycle_lock:
        if _shutting_down:
            return False
        _gui_loop_entered = True
        return True


class MonacoEditorApi:
    """JS API exposed via pywebview (runs on the GUI thread)."""

    def __init__(self) -> None:
        self._window: Any = None
        self._jedi: JediSession | None = None
        self._jedi_thread: threading.Thread | None = None
        self._jedi_started: float = 0.0
        self._jedi_lock: threading.Lock = threading.Lock()
        self._completion_gen: int = 0
        self._completion_lock: threading.Lock = threading.Lock()

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
                _note_load(msg)
                try:
                    if self._window is not None:
                        title = msg.get("title")
                        if title:
                            self._window.title = title
                        self._window.show()
                except Exception:
                    log.exception("editor_main: failed to show window on load")
        return batch

    def is_jedi_available(self) -> bool:
        return jedi is not None

    def _bump_completion_gen(self) -> int:
        # Overlapping API calls must not share a generation.
        with self._completion_lock:
            self._completion_gen += 1
            return self._completion_gen

    def _run_completion(self, code: str, line: int, column: int, gen: int) -> dict[str, Any] | None:
        # Bugfix: A hung ThreadPoolExecutor worker never became free, every later
        # completion blocked the API thread for 5 seconds, and concurrent.futures'
        # non-daemon atexit join kept the process alive so the host treated it as
        # healthy. Spawning a raw daemon thread allows abandoning a stuck worker
        # without blocking process exit or queueing behind a deadlocked thread.
        if self._jedi is None and jedi is None:
            return {"items": []}

        with self._jedi_lock:
            if self._jedi_thread is not None and self._jedi_thread.is_alive():
                if time.monotonic() - self._jedi_started < _JEDI_CALL_TIMEOUT_SEC:
                    return None
                self._jedi = None

            session = self._jedi
            done = threading.Event()
            box: dict[str, Any] = {}

            def _run(expected: Any, worker_gen: int) -> None:
                try:
                    if worker_gen != self._completion_gen:
                        box["result"] = {"items": []}
                        return
                    if expected is None:
                        active = JediSession()
                        with self._jedi_lock:
                            if self._jedi_thread is threading.current_thread():
                                self._jedi = active
                            else:
                                return
                    else:
                        active = expected
                    box["result"] = active.get_completions(code, line, column)
                except Exception:
                    log.exception("Jedi completions failed")
                    box["result"] = {"items": []}
                finally:
                    done.set()

            worker = threading.Thread(target=_run, args=(session, gen), name="jedi", daemon=True)
            self._jedi_thread = worker
            self._jedi_started = time.monotonic()
            worker.start()

        if not done.wait(_JEDI_CALL_TIMEOUT_SEC):
            log.warning("Jedi completions timed out or failed")
            return {"items": []}

        # Bugfix: When completions finish within the timeout, the worker thread
        # signalled `done` from its `finally:` block, but `done.wait()` unblocked
        # the caller while the worker was still unwinding. A subsequent completion
        # request arriving immediately after observed `_jedi_thread.is_alive()` as
        # True and was rejected as in-flight. Joining the finished worker ensures
        # its thread lifecycle has completed before returning.
        worker.join()

        return box.get("result", {"items": []})

    def get_completions(self, code: str, line: int, column: int) -> dict[str, Any]:
        gen = self._bump_completion_gen()
        outcome = self._run_completion(code, line, column, gen)
        if outcome is None or gen != self._completion_gen:
            return {"items": []}
        return outcome

    def notify_dirty(self, dirty: bool = True, session_id: str = "", mode: str = "", target: Any = None) -> None:
        _remember_session({"session_id": session_id, "mode": mode, "target": target or {}})
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
        _remember_session({"session_id": session_id, "mode": mode, "target": target or {}})
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
    if _is_shutting_down():
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
    if url_path in (
        "/editor_shell.js",
        "/backend_monaco.js",
        "/backend_cm6.js",
        "/cm6/cm6.bundle.js",
        "/scripts_manager.js",
        "/style.css",
    ):
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
    # The empty suffix is the rocher directory and only files are served.
    if not suffix:
        return None
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


def selected_backend() -> str:
    """Editor chosen by the host (scripting.python_editor) via WRITERAGENT_EDITOR_BACKEND."""
    return "codemirror" if os.environ.get("WRITERAGENT_EDITOR_BACKEND") == "codemirror" else "monaco"


def editor_config_js() -> bytes:
    """Body of /editor_config.js, loaded by index.html before editor_shell.js."""
    return f"window.WA_EDITOR_BACKEND = {json.dumps(selected_backend())};\n".encode("utf-8")


def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    path = environ.get("PATH_INFO", "")
    if path == "/editor_config.js":
        body = editor_config_js()
        start_response("200 OK", [("Content-Type", "application/javascript"), ("Content-Length", str(len(body)))])
        return [body]
    rocher_root = _ROCHER_ROOT if path.startswith("/vs/") else ""
    assets = os.path.abspath(os.environ.get("WRITERAGENT_EDITOR_ASSETS", _ASSETS_DIR))
    filepath = monaco_static_path(path, assets, rocher_root) or ""

    if filepath and os.path.isfile(filepath):
        mime, _unused = mimetypes.guess_type(filepath)
        if not mime:
            mime = "application/octet-stream"
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            start_response("200 OK", [("Content-Type", mime), ("Content-Length", str(len(content)))])
            return [content]
        except Exception as e:
            log.exception("editor_main: failed to serve %s", filepath)
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

    if selected_backend() == "codemirror":
        # CodeMirror ships in the extension, so rocher is not needed.
        for name in ("backend_cm6.js", os.path.join("cm6", "cm6.bundle.js")):
            if not os.path.isfile(os.path.join(assets, name)):
                _fatal(f"CodeMirror assets not found: {os.path.join(assets, name)}")
    else:
        # editor_shell.js loads vs/loader.js, which wsgi_app serves from _ROCHER_ROOT.
        # Without it the window never shows, so fail where the host shows stderr.
        loader_js = os.path.join(_ROCHER_ROOT, "loader.js")
        if not _ROCHER_ROOT or not os.path.isfile(loader_js):
            _fatal(
                f"Monaco assets not found: {loader_js} "
                "(install the 'rocher' package in the configured venv)"
            )

    try:
        import webview  # type: ignore[import-untyped]
    except ImportError as e:
        _fatal(f"pywebview is not installed in this interpreter: {e}", exc=e)

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

    # Listen for parent messages after window and API events are set up.
    threading.Thread(target=_pipe_reader_loop, name="editor-stdin-reader", daemon=True).start()

    start_kw: dict[str, Any] = {"debug": False, "http_server": True}
    gui = os.environ.get("WRITERAGENT_PYWEBVIEW_GUI", "").strip()
    if gui:
        start_kw["gui"] = gui

    try:
        if not _claim_gui_loop():
            log.info("editor_main: stdin closed before the GUI loop; not calling webview.start")
            return
        webview.start(**start_kw)
    except Exception as e:
        _fatal(f"webview.start failed: {e}", exc=e)
    finally:
        _send_closed_once()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1) from None
