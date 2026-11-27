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
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, final

from plugin.contrib.cdp.browser_cdp_tool import (
    browser_profile_dir,
    cleanup_local_browser,
    find_browser_executable,
    free_port,
    spawn_local_browser,
)
from plugin.framework.errors import best_effort
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.thread_guard import background
from plugin.framework.worker_pool import run_in_background

if TYPE_CHECKING:
    from websockets.sync.client import ClientConnection

log = logging.getLogger(__name__)

# Matches browser_cdp_tool.CDP_CALL_TIMEOUT_SEC.
BIDI_CALL_TIMEOUT = 30.0
# Generous launch wait for cold start (e.g. Windows ARM).
FIREFOX_LAUNCH_TIMEOUT_SEC = 15.0
_PIDFILE_NAME = "firefox_bidi.pid"
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
    _closing: bool
    _close_reason: str

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._lock = threading.Lock()
        self._pending = {}
        self._next_id = 1
        self._closed = False
        self._closing = False
        self._close_reason = ""
        run_in_background(self._read_loop, name="firefox-bidi-reader", dedicated=True)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def call(self, method: str, params: dict[str, Any], timeout: float = BIDI_CALL_TIMEOUT) -> dict[str, Any]:
        """Send one WebDriver BiDi command and wait for its correlated response."""
        with self._lock:
            if self._closed:
                raise RuntimeError(self._close_reason or "Firefox BiDi connection closed")
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
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
        best_effort(lambda: self.call("session.end", {}, timeout=2.0), "firefox bidi: session.end")
        with self._lock:
            self._closed = True
            if not self._close_reason:
                self._close_reason = "Firefox BiDi connection closed"
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
        reason = f"Firefox BiDi connection closed: {exc}"
        with self._lock:
            self._closed = True
            if not self._close_reason:
                self._close_reason = reason
            pending = list(self._pending.values())
            self._pending.clear()
        err = RuntimeError(reason)
        for fut in pending:
            if not fut.done():
                fut.set_exception(err)
        best_effort(self._ws.close, "firefox bidi: ws.close on fail")


_CLIENT: FirefoxBidiClient | None = None


def _pidfile_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, _PIDFILE_NAME)


def _write_pidfile(pidfile: str, firefox_pid: int) -> None:
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(f"{firefox_pid}\n")


def _cmdline_args_matches(args: list[str], profile_dir: str) -> bool:
    """Match exact --profile <profile_dir> and any --remote-debugging-port flag in argv (each launch picks a free port)."""
    has_profile = any(
        args[i] == "--profile" and args[i + 1] == profile_dir
        for i in range(len(args) - 1)
    )
    return has_profile and any(a.startswith("--remote-debugging-port=") for a in args)


def _cmdline_string_matches(cmdline_str: str, profile_dir: str) -> bool:
    """Match --profile <profile_dir> and a --remote-debugging-port flag on a raw command line."""
    escaped = re.escape(profile_dir)
    profile_pat = rf'(?:^|\s)--profile\s+(?:"{escaped}"|{escaped})(?:\s|$)'
    port_pat = r"(?:^|\s)--remote-debugging-port=\d+(?:\s|$)"
    return bool(re.search(profile_pat, cmdline_str) and re.search(port_pat, cmdline_str))


def _cmdline_matches_firefox_profile(pid: int, profile_dir: str) -> bool:
    """Return True if *pid* is running with exact --profile *profile_dir* and a debug port."""
    if pid <= 0:
        return False
    proc_cmdline = f"/proc/{pid}/cmdline"
    if os.path.exists(proc_cmdline):
        try:
            with open(proc_cmdline, "rb") as f:
                raw = f.read()
            if not raw:
                return False
            args = [part.decode("utf-8", errors="replace") for part in raw.split(b"\x00") if part]
        except OSError:
            return False
        return _cmdline_args_matches(args, profile_dir)

    if sys.platform == "win32":
        try:
            res = ManagedSubprocess.run(
                ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return _cmdline_string_matches(res.stdout.strip(), profile_dir)
        except Exception:
            return False

    try:
        res = ManagedSubprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=2.0)
        return _cmdline_string_matches(res.stdout.strip(), profile_dir)
    except Exception:
        return False


def _orphan_pid(pidfile: str, profile_dir: str) -> int | None:
    """Return Firefox PID from *pidfile* when its cmdline still matches *profile_dir*.

    The caller holds the profile lock, so no live WriterAgent owns that process.
    The record is never removed, so a recycled PID is expected and the argv match
    is what keeps an unrelated process alive. Reading the first line also accepts
    the older two-line file.
    """
    try:
        with open(pidfile, encoding="utf-8") as f:
            first = f.readline().strip()
        if not first:
            return None
        ff_pid = int(first)
    except (OSError, ValueError):
        return None
    if not _cmdline_matches_firefox_profile(ff_pid, profile_dir):
        return None
    return ff_pid


def _terminate_pid(pid: int) -> None:
    if sys.platform == "win32":
        ManagedSubprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            timeout=5.0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def connect_firefox_bidi(port: int, open_timeout: float) -> FirefoxBidiClient:
    """Connect to a listening Firefox BiDi endpoint and start one session."""
    # Lazy import: avoids paying websockets/ssl import cost at startup when browser
    # research is off, and keeps an older/missing websockets from breaking web_research import.
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    url = f"ws://127.0.0.1:{port}/session"
    try:
        ws = connect(
            url,
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
            f"Firefox on port {port} refused a new WebDriver BiDi session: {exc}"
        ) from exc
    return client


def _launch_firefox(profile_dir: str) -> FirefoxBidiClient:
    pidfile = _pidfile_path(profile_dir)
    orphan_pid = _orphan_pid(pidfile, profile_dir)
    if orphan_pid is not None:
        # Do not attach first: Firefox keeps the dead owner's session reserved.
        log.warning("Killing orphaned WriterAgent Firefox (pid=%d) holding profile", orphan_pid)
        best_effort(lambda: _terminate_pid(orphan_pid), "firefox bidi: kill orphan")
        time.sleep(0.3)

    port = free_port()
    executable = find_browser_executable(
        _FIREFOX_CANDIDATES,
        "firefox",
        _FIREFOX_WIN32_PATHS,
        win32_app_name="firefox.exe",
    )
    cmd = [
        executable,
        "--no-remote",
        "--new-instance",
        f"--remote-debugging-port={port}",
        "--profile",
        profile_dir,
    ]
    if sys.platform == "win32":
        # Firefox's launcher process exits with code 0 at once without it, which would look like a handoff.
        cmd.append("--wait-for-browser")
    cmd.append("about:blank")
    log.info("Spawning local browser (firefox): %s", " ".join(cmd))
    managed = spawn_local_browser(cmd, label="Firefox", minimized=True)
    if managed.pid is not None:
        best_effort(lambda: _write_pidfile(pidfile, int(managed.pid)), "firefox bidi: write pidfile")

    deadline = time.monotonic() + FIREFOX_LAUNCH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            return connect_firefox_bidi(port, 1.0)
        except OSError:
            rc = managed.popen.poll() if managed.popen is not None else None
            if rc is not None:
                # Code 0 means Firefox found the profile already open.
                raise RuntimeError(f"Firefox exited during startup (code {rc})")
            time.sleep(0.2)
    raise RuntimeError(
        f"Firefox did not start listening for WebDriver BiDi on port {port} "
        f"after {int(FIREFOX_LAUNCH_TIMEOUT_SEC)} seconds"
    )


def begin_firefox_bidi() -> FirefoxBidiClient:
    """Return a live Firefox BiDi client, launching Firefox if needed.

    The caller holds ``LocalBrowserRefCount.launch_lock`` and a counted Firefox run,
    so the local browser process can only be this Firefox.
    """
    global _CLIENT
    if _CLIENT is not None:
        if not _CLIENT.closed:
            return _CLIENT
        close_firefox_bidi()
        cleanup_local_browser()

    _CLIENT = _launch_firefox(browser_profile_dir("firefox"))
    return _CLIENT


def close_firefox_bidi() -> None:
    """Close and clear the shared Firefox BiDi client (does not kill the process)."""
    global _CLIENT
    client, _CLIENT = _CLIENT, None
    if client is not None:
        client.close()
