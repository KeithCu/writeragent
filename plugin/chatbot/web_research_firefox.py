# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""First-party WebDriver BiDi client for Firefox web research.

Concurrency: one dedicated reader thread (``firefox-bidi-reader``) reads from
the single WebSocket and resolves per-command ``Future`` instances under
``_lock`` so concurrent deep-research workers can share one Firefox session.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, final

from plugin.contrib.cdp.browser_cdp_tool import (
    browser_profile_dir,
    cleanup_local_browser,
    find_browser_executable,
    spawn_local_browser,
)
from plugin.framework.errors import best_effort
from plugin.framework.thread_guard import background
from plugin.framework.worker_pool import run_in_background

if TYPE_CHECKING:
    from websockets.sync.client import ClientConnection

log = logging.getLogger(__name__)

FIREFOX_BIDI_PORT = 9222
FIREFOX_BIDI_URL = f"ws://127.0.0.1:{FIREFOX_BIDI_PORT}/session"
# Matches browser_cdp's per-call timeout.
BIDI_CALL_TIMEOUT = 30.0
_FIREFOX_CANDIDATES = [
    "firefox",
    "firefox-esr",
    "/Applications/Firefox.app/Contents/MacOS/firefox",
]
_FIREFOX_WIN32_PATHS = (
    r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
    r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
    r"%LocalAppData%\Mozilla Firefox\firefox.exe",
)


@final
class FirefoxBidiClient:
    """One WebDriver BiDi session over one WebSocket. Firefox allows a single
    active session per process and keeps it reserved after the socket drops,
    so a dead client is never reconnected: the process is killed and relaunched."""

    _ws: ClientConnection
    _lock: threading.Lock
    _pending: dict[int, Future[dict[str, Any]]]
    _next_id: int
    _closed: bool

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._lock = threading.Lock()
        self._pending = {}
        self._next_id = 1
        self._closed = False
        run_in_background(self._read_loop, name="firefox-bidi-reader", dedicated=True)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def call(self, method: str, params: dict[str, Any], timeout: float = BIDI_CALL_TIMEOUT) -> dict[str, Any]:
        """Send one WebDriver BiDi command and wait for its correlated response."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Firefox BiDi connection closed")
            cmd_id = self._next_id
            self._next_id += 1
            fut: Future[dict[str, Any]] = Future()
            self._pending[cmd_id] = fut

        payload = json.dumps({"id": cmd_id, "method": method, "params": params})
        try:
            self._ws.send(payload)
        except Exception:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise

        try:
            msg = fut.result(timeout=timeout)
        except TimeoutError as exc:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise TimeoutError(f"Timed out waiting for {method}") from exc

        if msg.get("type") == "error":
            raise RuntimeError(f"{msg.get('error', 'unknown error')}: {msg.get('message', '')}")
        result = msg.get("result", {})
        return result if isinstance(result, dict) else {}

    def open_tab(self) -> str:
        res = self.call("browsingContext.create", {"type": "tab", "background": True})
        return str(res["context"])

    def navigate(self, tab: str, url: str) -> None:
        # wait="none" returns immediately so VisitWebpageBrowserTool owns the 3 s settle loop.
        self.call("browsingContext.navigate", {"context": tab, "url": url, "wait": "none"})

    def page_text(self, tab: str) -> str:
        res = self.call(
            "script.evaluate",
            {
                "expression": "document.body.innerText",
                "target": {"context": tab},
                "awaitPromise": False,
            },
        )
        if res.get("type") == "exception":
            details = res.get("exceptionDetails") or {}
            raise RuntimeError(str(details.get("text") or "JavaScript evaluation failed"))
        inner = res.get("result") or {}
        return str(inner.get("value") or "")

    def close_tab(self, tab: str) -> None:
        self.call("browsingContext.close", {"context": tab})

    def close(self) -> None:
        """End the BiDi session (best effort) and close the WebSocket."""
        if self.closed:
            return
        best_effort(lambda: self.call("session.end", {}, timeout=2.0), "firefox bidi: session.end")
        with self._lock:
            self._closed = True
        best_effort(self._ws.close, "firefox bidi: ws.close")

    @background
    def _read_loop(self) -> None:
        try:
            while True:
                raw = self._ws.recv()
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    log.debug("firefox bidi: ignoring undecodable frame: %s", exc)
                    continue
                if not isinstance(msg, dict):
                    continue
                cmd_id = msg.get("id")
                if cmd_id is None:
                    continue
                with self._lock:
                    fut = self._pending.pop(cmd_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        err = RuntimeError(f"Firefox BiDi connection closed: {exc}")
        for fut in pending:
            if not fut.done():
                fut.set_exception(err)
        best_effort(self._ws.close, "firefox bidi: ws.close on fail")


_CLIENT: FirefoxBidiClient | None = None


def connect_firefox_bidi(open_timeout: float) -> FirefoxBidiClient:
    """Connect to a listening Firefox BiDi endpoint and start one session."""
    # Lazy import: avoids paying websockets/ssl import cost at startup when browser
    # research is off, and keeps an older/missing websockets from breaking web_research import.
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    try:
        ws = connect(
            FIREFOX_BIDI_URL,
            open_timeout=open_timeout,
            # Disable keepalive pings and message size cap for local BiDi pages.
            ping_interval=None,
            max_size=None,
            # An env HTTP(S)_PROXY must not be used for the local BiDi socket.
            proxy=None,
            legacy=True,
        )
    except WebSocketException as exc:
        raise ConnectionError(str(exc)) from exc
    client = FirefoxBidiClient(ws)
    try:
        client.call("session.new", {"capabilities": {}}, timeout=10.0)
    except Exception as exc:
        client.close()
        raise RuntimeError(
            f"Firefox on port {FIREFOX_BIDI_PORT} refused a new WebDriver BiDi session: {exc}"
        ) from exc
    return client


def begin_firefox_bidi() -> FirefoxBidiClient:
    """Return a live Firefox BiDi client, launching Firefox if needed.

    Caller must hold ``LocalBrowserRefCount.launch_lock``.
    """
    global _CLIENT
    if _CLIENT is not None:
        if not _CLIENT.closed:
            return _CLIENT
        close_firefox_bidi()
        cleanup_local_browser()

    try:
        _CLIENT = connect_firefox_bidi(1.0)
        return _CLIENT
    except OSError:
        # Nothing is listening yet, so launch Firefox. A RuntimeError from
        # session.new propagates because the port is held by a Firefox we cannot drive.
        pass

    executable = find_browser_executable(_FIREFOX_CANDIDATES, "firefox", _FIREFOX_WIN32_PATHS)
    cmd = [
        executable,
        f"--remote-debugging-port={FIREFOX_BIDI_PORT}",
        "--profile",
        browser_profile_dir("firefox"),
        "about:blank",
    ]
    log.info("Spawning local browser (firefox): %s", " ".join(cmd))
    spawn_local_browser(cmd, label="bidi-browser")

    deadline = time.time() + 6.0
    while time.time() < deadline:
        try:
            _CLIENT = connect_firefox_bidi(1.0)
            return _CLIENT
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(
        f"Firefox did not start listening for WebDriver BiDi on port {FIREFOX_BIDI_PORT} after 6 seconds"
    )


def close_firefox_bidi() -> None:
    """Close and clear the shared Firefox BiDi client (does not kill the process)."""
    global _CLIENT
    client, _CLIENT = _CLIENT, None
    if client is not None:
        client.close()
