#!/usr/bin/env python3
# Adapted from Nous Research Hermes Agent (MIT License)
# Original relative path: tools/browser_cdp_tool.py
"""
Raw Chrome DevTools Protocol (CDP) passthrough tool.

Exposes ``browser_cdp``, which sends CDP commands to the local browser's
DevTools WebSocket endpoint attached by ``get_local_cdp_url`` for the current
research run.

Method reference: https://chromedevtools.github.io/devtools-protocol/
"""
from __future__ import annotations

import asyncio
import os
import json
import logging
import shutil
from typing import Any, Dict, Optional

import sys
import time
import socket
import subprocess
import urllib.request
from urllib.parse import urlsplit

from plugin.framework.managed_subprocess import ManagedSubprocess

def tool_error(message: str, **kwargs: Any) -> str:
    payload = {"success": False, "error": message}
    payload.update(kwargs)
    return json.dumps(payload, ensure_ascii=False)

logger = logging.getLogger(__name__)
# The browser is on this machine, so a configured HTTP proxy must not see these requests.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CDP_DOCS_URL = "https://chromedevtools.github.io/devtools-protocol/"

# ``websockets`` is a transitive dependency of hermes-agent (via fal_client
# and firecrawl-py) and is already imported by gateway/platforms/feishu.py.
# Wrap the import so a clean error surfaces if the package is ever absent.
try:
    import websockets
    from websockets.exceptions import WebSocketException

    _WS_AVAILABLE = True
except ImportError:
    websockets: Any = None  # type: ignore[assignment,no-redef]
    WebSocketException: Any = Exception  # type: ignore[assignment,misc,no-redef]
    _WS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Async-from-sync bridge (matches the pattern in homeassistant_tool.py)
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine from a sync handler, safe inside or outside a loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


_LOCAL_BROWSER: ManagedSubprocess | None = None
# Browser-level URL of the CDP browser the current research runs use; empty when no run holds one.
# Written only by launch (under LocalBrowserRefCount.launch_lock) and teardown; browser_cdp only reads it,
# so a page visit can never start a browser. The active browser type is owned by LocalBrowserRefCount.
_CDP_ENDPOINT: str = ""
_ENDPOINT_FILE = "cdp_endpoint"


def cleanup_local_browser() -> None:
    """Kill the locally spawned browser and drop it from quit-time registry."""
    global _LOCAL_BROWSER, _CDP_ENDPOINT
    _CDP_ENDPOINT = ""
    managed = _LOCAL_BROWSER
    if managed is None:
        return
    _LOCAL_BROWSER = None
    logger.info("Terminating local %s process...", managed.label)
    try:
        managed.kill_tree()
        managed.abandon()
    except Exception:
        pass
    managed.unregister()


_CDP_BROWSER_CANDIDATES: dict[str, tuple[list[str], str, tuple[str, ...]]] = {
    "brave": (
        [
            "brave",
            "brave-browser",
            "brave-browser-stable",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ],
        "brave",
        (
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    ),
    "chromium": (
        [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
            "chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ],
        "chromium",
        (
            r"%ProgramFiles%\Chromium\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Chromium\Application\chrome.exe",
            r"%LocalAppData%\Chromium\Application\chrome.exe",
        ),
    ),
    "chrome": (
        [
            "google-chrome",
            "google-chrome-stable",
            "chrome",
            "chromium-browser",
            "chromium",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ],
        "google-chrome",
        (
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
}


def _win32_app_path(exe_name: str) -> str | None:
    """Look up an executable in Windows Registry App Paths (HKCU then HKLM)."""
    if sys.platform != "win32":
        return None
    import winreg  # Windows-only stdlib module.

    subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, subkey) as key:
                val, _idx = winreg.QueryValueEx(key, None)
                if isinstance(val, str) and val and os.path.exists(val):
                    return val
        except OSError:
            continue
    return None


def find_browser_executable(
    candidates: list[str],
    default: str,
    win32_paths: tuple[str, ...] = (),
    win32_app_name: str = "",
) -> str:
    """Return the first existing absolute path or PATH binary from candidates."""
    search = list(candidates)
    if sys.platform == "win32":
        if win32_paths:
            search.extend(os.path.expandvars(p) for p in win32_paths)
        if win32_app_name:
            reg_path = _win32_app_path(win32_app_name)
            if reg_path:
                search.append(reg_path)
    for name in search:
        try:
            if os.path.isabs(name):
                if os.path.exists(name):
                    return name
                continue
            found = shutil.which(name)
            if found:
                return found
        except Exception:
            continue
    return default


def browser_profile_dir(browser_type: str, *, create: bool = True) -> str:
    """Return (and optionally create) the dedicated profile dir for a research browser."""
    # The profile lives under the WriterAgent config dir so it survives LibreOffice profile resets.
    config_dir = os.path.expanduser("~/.config/writeragent")
    safe_name = "".join([c if c.isalnum() else "_" for c in browser_type])
    profile_dir = os.path.join(config_dir, f"{safe_name}_profile")
    if create:
        os.makedirs(profile_dir, exist_ok=True)
    return profile_dir


def _win32_minimized_startupinfo() -> Any:
    # Windows-only: start Firefox minimized without activating so it does not steal focus.
    # Linux and macOS have no portable non-headless equivalent (headless risks bot-blocking).
    si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
    return si


def spawn_local_browser(cmd: list[str], *, label: str, minimized: bool = False) -> ManagedSubprocess:
    """Launch a local browser process, register it for quit-time cleanup, and return its handle."""
    global _LOCAL_BROWSER
    if _LOCAL_BROWSER is not None:
        if _LOCAL_BROWSER.is_alive():
            raise RuntimeError("another local browser is already running for web research")
        # A reaped pid can be reused, so quit-time shutdown must not signal it.
        _LOCAL_BROWSER.unregister()
    # Own session for tree kill at quit; no CREATE_NO_WINDOW so the browser stays visible on Windows.
    popen_kw: dict[str, Any] = {}
    if sys.platform != "win32":
        popen_kw["start_new_session"] = True
    elif minimized:
        popen_kw["startupinfo"] = _win32_minimized_startupinfo()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_kw,
    )
    _LOCAL_BROWSER = ManagedSubprocess.wrap_existing(
        proc,
        label=label,
        register=True,
    )
    return _LOCAL_BROWSER


def free_port() -> int:
    # Chosen here because --remote-debugging-port=0 makes Chromium set navigator.webdriver, which sites block.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _query_browser_endpoint(host_port: str) -> str:
    """Return the listening browser's browser-level WebSocket URL, or "" if none answers.

    The browser endpoint outlives every tab. A page endpoint from /json closes
    with its tab, which another research worker may own.
    """
    try:
        with _LOCAL_OPENER.open(f"http://{host_port}/json/version", timeout=1.0) as response:
            return str(json.loads(response.read().decode()).get("webSocketDebuggerUrl") or "")
    except Exception:
        return ""


def _recorded_endpoint(profile_dir: str) -> str:
    """Return the URL recorded for this profile if that same browser process still answers, else "".

    The id in the URL is new for every browser process, so a stale record or another browser on the port never matches.
    """
    try:
        with open(os.path.join(profile_dir, _ENDPOINT_FILE), encoding="utf-8") as f:
            recorded = f.read()
        host_port = urlsplit(recorded).netloc
    except (FileNotFoundError, ValueError):  # ValueError: undecodable bytes or a malformed URL
        return ""
    endpoint = _query_browser_endpoint(host_port)
    return endpoint if endpoint == recorded else ""


def _launch_cdp_browser(browser_type: str, profile_dir: str) -> str:
    candidates, default_bin, win32_paths = _CDP_BROWSER_CANDIDATES[browser_type]
    executable = find_browser_executable(candidates, default_bin, win32_paths)

    port = free_port()
    cmd = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    logger.info("Spawning local browser (%s): %s", browser_type, " ".join(cmd))
    spawn_local_browser(cmd, label=browser_type)

    deadline = time.time() + 6.0
    while time.time() < deadline:
        endpoint = _query_browser_endpoint(f"127.0.0.1:{port}")
        if endpoint:
            with open(os.path.join(profile_dir, _ENDPOINT_FILE), "w", encoding="utf-8") as f:
                f.write(endpoint)
            return endpoint
        # A browser left by a WriterAgent that crashed may have been too busy for the first check; Chromium then hands off to it and exits.
        endpoint = _recorded_endpoint(profile_dir)
        if endpoint:
            return endpoint
        time.sleep(0.2)

    raise RuntimeError(f"Browser ({browser_type}) CDP did not start listening on port {port} after 6 seconds")


def get_local_cdp_url(browser_type: str) -> str:
    global _CDP_ENDPOINT
    profile_dir = browser_profile_dir(browser_type)
    # Do not clear on entry or failure: another run may still be using a live browser too busy to answer in 1 s.
    endpoint = _recorded_endpoint(profile_dir)
    if not endpoint:
        endpoint = _launch_cdp_browser(browser_type, profile_dir)
    _CDP_ENDPOINT = endpoint
    return endpoint


# ---------------------------------------------------------------------------
# Core CDP call
# ---------------------------------------------------------------------------


async def _cdp_call(
    ws_url: str,
    method: str,
    params: Dict[str, Any],
    target_id: Optional[str],
    timeout: float,
) -> Dict[str, Any]:
    """Make a single CDP call, optionally attaching to a target first.

    When ``target_id`` is provided, we call ``Target.attachToTarget`` with
    ``flatten=True`` to multiplex a page-level session over the same
    browser-level WebSocket, then send ``method`` with that ``sessionId``.
    When ``target_id`` is None, ``method`` is sent at browser level — which
    works for ``Target.*``, ``Browser.*``, ``Storage.*`` and a few other
    globally-scoped domains.
    """
    assert websockets is not None  # guarded by _WS_AVAILABLE at call-site

    async with websockets.connect(
        ws_url,
        max_size=None,  # CDP responses (e.g. DOM.getDocument) can be large
        open_timeout=timeout,
        close_timeout=5,
        ping_interval=None,  # CDP server doesn't expect pings
        proxy=None,  # An env HTTP(S)_PROXY must not be used for the local CDP socket.
    ) as ws:
        next_id = 1
        session_id: Optional[str] = None

        # --- Step 1: attach to target if requested ---
        if target_id:
            attach_id = next_id
            next_id += 1
            await ws.send(
                json.dumps(
                    {
                        "id": attach_id,
                        "method": "Target.attachToTarget",
                        "params": {"targetId": target_id, "flatten": True},
                    }
                )
            )
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out attaching to target {target_id}"
                    )
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("id") == attach_id:
                    if "error" in msg:
                        raise RuntimeError(
                            f"Target.attachToTarget failed: {msg['error']}"
                        )
                    session_id = msg.get("result", {}).get("sessionId")
                    if not session_id:
                        raise RuntimeError(
                            "Target.attachToTarget did not return a sessionId"
                        )
                    break
                # Ignore events (messages without "id") while waiting

        # --- Step 2: dispatch the real method ---
        call_id = next_id
        next_id += 1
        req: Dict[str, Any] = {
            "id": call_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            req["sessionId"] = session_id
        await ws.send(json.dumps(req))

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for response to {method}"
                )
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})
            # Ignore events / out-of-order responses


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------


def browser_cdp(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    target_id: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    """Send a raw CDP command.  See ``CDP_DOCS_URL`` for method documentation.

    Args:
        method: CDP method name, e.g. ``"Target.getTargets"``.
        params: Method-specific parameters; defaults to ``{}``.
        target_id: Optional target/tab ID for page-level methods.  When set,
            we first attach to the target (``flatten=True``) and send
            ``method`` with the resulting ``sessionId``.  Uses a fresh
            stateless CDP connection.
        timeout: Seconds to wait for the call to complete.

    Returns:
        JSON string ``{"success": True, "method": ..., "result": {...}}`` on
        success, or ``{"error": "..."}`` on failure.
    """

    if not method or not isinstance(method, str):
        return tool_error(
            "'method' is required (e.g. 'Target.getTargets')",
            cdp_docs=CDP_DOCS_URL,
        )

    if not _WS_AVAILABLE:
        return tool_error(
            "The 'websockets' Python package is required but not installed. "
            "Install it with: pip install websockets"
        )

    endpoint = _CDP_ENDPOINT
    if not endpoint:
        return tool_error(
            "The research browser is not running. The next web research run starts it again. "
            "To read pages without a browser, set 'Web Research Browser' to 'Off (Static HTTP)' in Settings.",
            cdp_docs=CDP_DOCS_URL,
        )

    if not endpoint.startswith(("ws://", "wss://")):
        return tool_error(
            f"CDP endpoint is not a WebSocket URL: {endpoint!r}."
        )

    call_params: Dict[str, Any] = params or {}
    if not isinstance(call_params, dict):
        return tool_error(
            f"'params' must be an object/dict, got {type(call_params).__name__}"
        )

    try:
        safe_timeout = float(timeout) if timeout else 30.0
    except (TypeError, ValueError):
        safe_timeout = 30.0
    safe_timeout = max(1.0, min(safe_timeout, 300.0))

    try:
        result = _run_async(
            _cdp_call(endpoint, method, call_params, target_id, safe_timeout)
        )
    except TimeoutError as exc:
        return tool_error(
            f"CDP call timed out after {safe_timeout}s: {exc}",
            method=method,
        )
    except RuntimeError as exc:
        return tool_error(str(exc), method=method)
    except (WebSocketException, OSError) as exc:
        # A refused connection means the browser exited. Keep after TimeoutError, which is an OSError.
        return tool_error(
            f"WebSocket error talking to CDP at {endpoint}: {exc}. The research browser may have been closed. "
            "Further page visits in this run will fail, so answer from what has already been collected. "
            "The next web research run starts it again.",
            method=method,
        )
    except Exception as exc:  # pragma: no cover — unexpected
        logger.exception("browser_cdp unexpected error")
        return tool_error(
            f"Unexpected error: {type(exc).__name__}: {exc}",
            method=method,
        )

    payload: Dict[str, Any] = {
        "success": True,
        "method": method,
        "result": result,
    }
    if target_id:
        payload["target_id"] = target_id
    return json.dumps(payload, ensure_ascii=False)
