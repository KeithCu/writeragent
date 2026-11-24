#!/usr/bin/env python3
# Adapted from Nous Research Hermes Agent (MIT License)
# Original relative path: tools/browser_cdp_tool.py
"""
Raw Chrome DevTools Protocol (CDP) passthrough tool.

Exposes a single tool, ``browser_cdp``, that sends arbitrary CDP commands to
the browser's DevTools WebSocket endpoint.  Works when a CDP URL is
configured — either via ``/browser connect`` (sets ``BROWSER_CDP_URL``) or
``browser.cdp_url`` in ``config.yaml`` — or when a CDP-backed cloud provider
session is active.

This is the escape hatch for browser operations not covered by the main
browser tool surface (``browser_navigate``, ``browser_click``,
``browser_console``, etc.) — handling native dialogs, iframe-scoped
evaluation, cookie/network control, low-level tab management, etc.

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
import subprocess
import urllib.request

from plugin.framework.managed_subprocess import ManagedSubprocess

def tool_error(message: str, **kwargs: Any) -> str:
    payload = {"success": False, "error": message}
    payload.update(kwargs)
    return json.dumps(payload, ensure_ascii=False)

logger = logging.getLogger(__name__)

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
# Browser-level URL of the current local browser. Written only by launch (under CdpRefCount.launch_lock) and teardown; browser_cdp only reads it, so a page visit can never start a browser.
_CDP_ENDPOINT: str = ""


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
        (),
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
        (),
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
    if _LOCAL_BROWSER is not None and _LOCAL_BROWSER.is_alive():
        raise RuntimeError("another local browser is already running for web research")
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


def _query_browser_endpoint() -> str:
    """Return the listening browser's browser-level WebSocket URL, or "" if none answers.

    The browser endpoint outlives every tab. A page endpoint from /json closes
    with its tab, which another research worker may own.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.0) as response:
            return str(json.loads(response.read().decode()).get("webSocketDebuggerUrl") or "")
    except Exception:
        return ""


def get_local_cdp_url(browser_type: str = "chrome") -> str:
    global _CDP_ENDPOINT
    # Do not clear on entry or failure: another run may still be using a live browser too busy to answer in 1 s.
    endpoint = _query_browser_endpoint()
    if endpoint:
        _CDP_ENDPOINT = endpoint
        return endpoint

    # Spawn browser non-headlessly if not already running
    profile_dir = browser_profile_dir(browser_type)

    if os.path.exists(browser_type) or "/" in browser_type or "\\" in browser_type:
        executable = browser_type
    elif browser_type in _CDP_BROWSER_CANDIDATES:
        candidates, default_bin, win32_paths = _CDP_BROWSER_CANDIDATES[browser_type]
        executable = find_browser_executable(candidates, default_bin, win32_paths)
    else:
        search_names = [browser_type]
        if browser_type.lower() in ["edge", "microsoft-edge"]:
            search_names.extend(["microsoft-edge-stable", "microsoft-edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"])
        executable = find_browser_executable(search_names, browser_type)

    cmd = [
        executable,
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check"
    ]

    logger.info("Spawning local browser (%s): %s", browser_type, " ".join(cmd))
    spawn_local_browser(cmd, label=browser_type)
    
    # Wait for browser to listen on port 9222
    deadline = time.time() + 6.0
    while time.time() < deadline:
        endpoint = _query_browser_endpoint()
        if endpoint:
            _CDP_ENDPOINT = endpoint
            return endpoint
        time.sleep(0.2)

    raise RuntimeError(f"Browser ({browser_type}) CDP did not start listening on port 9222 after 6 seconds")


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


def _browser_cdp_via_supervisor(
    task_id: str,
    frame_id: str,
    method: str,
    params: Optional[Dict[str, Any]],
    timeout: float,
) -> str:
    """Route a CDP call through the live supervisor session for an OOPIF frame.

    Looks up the frame in the supervisor's snapshot, extracts its child
    ``cdp_session_id``, and dispatches ``method`` with that sessionId via
    the supervisor's already-connected WebSocket (using
    ``asyncio.run_coroutine_threadsafe`` onto the supervisor loop).
    """
    try:
        from plugin.contrib.cdp.browser_supervisor import SUPERVISOR_REGISTRY
    except Exception as exc:  # pragma: no cover — defensive
        return tool_error(
            f"CDP supervisor is not available: {exc}. frame_id routing requires "
            f"a running supervisor attached via /browser connect or an active "
            f"Browserbase session."
        )

    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is None:
        return tool_error(
            f"No CDP supervisor is attached for task={task_id!r}. Call "
            f"browser_navigate or /browser connect first so the supervisor "
            f"can attach. Once attached, browser_snapshot will populate "
            f"frame_tree with frame_ids you can pass here."
        )

    snap = supervisor.snapshot()
    # Search both the top frame and the children for the requested id.
    top = snap.frame_tree.get("top")
    frame_info: Optional[Dict[str, Any]] = None
    if top and top.get("frame_id") == frame_id:
        frame_info = top
    else:
        for child in snap.frame_tree.get("children", []) or []:
            if child.get("frame_id") == frame_id:
                frame_info = child
                break
    if frame_info is None:
        # Check the raw frames dict too (frame_tree is capped at 30 entries)
        with supervisor._state_lock:  # type: ignore[attr-defined]
            raw = supervisor._frames.get(frame_id)  # type: ignore[attr-defined]
        if raw is not None:
            frame_info = raw.to_dict()

    if frame_info is None:
        return tool_error(
            f"frame_id {frame_id!r} not found in supervisor state. "
            f"Call browser_snapshot to see current frame_tree."
        )

    child_sid = frame_info.get("session_id")
    if not child_sid:
        # Not an OOPIF — fall back to top-level session (evaluating at page
        # scope).  Same-origin iframes don't get their own sessionId; the
        # agent can still use contentWindow/contentDocument from the parent.
        return tool_error(
            f"frame_id {frame_id!r} is not an out-of-process iframe (no "
            f"dedicated CDP session). For same-origin iframes, use "
            f"`browser_cdp(method='Runtime.evaluate', params={{'expression': "
            f"\"document.querySelector('iframe').contentDocument.title\"}})` "
            f"at the top-level page instead."
        )

    # Dispatch onto the supervisor's loop.
    loop = supervisor._loop  # type: ignore[attr-defined]
    if loop is None or not loop.is_running():
        return tool_error(
            "CDP supervisor loop is not running. Try reconnecting with "
            "/browser connect."
        )

    async def _do_cdp():
        return await supervisor._cdp(  # type: ignore[attr-defined]
            method,
            params or {},
            session_id=child_sid,
            timeout=timeout,
        )

    try:
        from plugin.contrib.cdp.browser_supervisor import safe_schedule_threadsafe
        fut = safe_schedule_threadsafe(_do_cdp(), loop)
        if fut is None:
            return tool_error(
                "CDP call via supervisor failed: loop unavailable",
                cdp_docs=CDP_DOCS_URL,
            )
        result_msg = fut.result(timeout=timeout + 2)
    except Exception as exc:
        return tool_error(
            f"CDP call via supervisor failed: {type(exc).__name__}: {exc}",
            cdp_docs=CDP_DOCS_URL,
        )

    payload: Dict[str, Any] = {
        "success": True,
        "method": method,
        "frame_id": frame_id,
        "session_id": child_sid,
        "result": result_msg.get("result", {}),
    }
    return json.dumps(payload, ensure_ascii=False)


def browser_cdp(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    target_id: Optional[str] = None,
    frame_id: Optional[str] = None,
    timeout: float = 30.0,
    task_id: Optional[str] = None,
) -> str:
    """Send a raw CDP command.  See ``CDP_DOCS_URL`` for method documentation.

    Args:
        method: CDP method name, e.g. ``"Target.getTargets"``.
        params: Method-specific parameters; defaults to ``{}``.
        target_id: Optional target/tab ID for page-level methods.  When set,
            we first attach to the target (``flatten=True``) and send
            ``method`` with the resulting ``sessionId``.  Uses a fresh
            stateless CDP connection.
        frame_id: Optional cross-origin (OOPIF) iframe ``frame_id`` from
            ``browser_snapshot.frame_tree.children[]``.  When set (and the
            frame is an OOPIF with a live session tracked by the CDP
            supervisor), routes the call through the supervisor's existing
            WebSocket — which is how you Runtime.evaluate *inside* an
            iframe on backends where per-call fresh CDP connections would
            hit signed-URL expiry (Browserbase) or expensive reattach.
        timeout: Seconds to wait for the call to complete.
        task_id: Task identifier for supervisor lookup.  When ``frame_id``
            is set, this identifies which task's supervisor to use; the
            handler will default to ``"default"`` otherwise.

    Returns:
        JSON string ``{"success": True, "method": ..., "result": {...}}`` on
        success, or ``{"error": "..."}`` on failure.
    """
    # --- Route iframe-scoped calls through the supervisor ---------------
    if frame_id:
        return _browser_cdp_via_supervisor(
            task_id=task_id or "default",
            frame_id=frame_id,
            method=method,
            params=params,
            timeout=timeout,
        )
    del task_id  # stateless path below

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
            "No CDP endpoint is available. Run '/browser connect' to attach "
            "to a running Chrome, Brave, Chromium, or Edge browser, or set "
            "'browser.cdp_url' in config.yaml. The Camofox backend is REST-only "
            "and does not expose CDP.",
            cdp_docs=CDP_DOCS_URL,
        )

    if not endpoint.startswith(("ws://", "wss://")):
        return tool_error(
            f"CDP endpoint is not a WebSocket URL: {endpoint!r}. "
            "Expected ws://... or wss://... — the /browser connect "
            "resolver should have rewritten this. Check that a Chromium-family "
            "browser is actually listening on the debug port."
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
            f"WebSocket error talking to CDP at {endpoint}: {exc}. The "
            "browser may have disconnected — try '/browser connect' again.",
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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


BROWSER_CDP_SCHEMA: Dict[str, Any] = {
    "name": "browser_cdp",
    "description": (
        "Send a raw Chrome DevTools Protocol (CDP) command. Escape hatch for "
        "browser operations not covered by browser_navigate, browser_click, "
        "browser_console, etc.\n\n"
        "**Requires a reachable CDP endpoint.** Available when the user has "
        "run '/browser connect' to attach to a running Chrome, Brave, Chromium, "
        "or Edge browser, or when 'browser.cdp_url' is set in config.yaml. "
        "Not currently wired up for cloud backends (Browserbase, Browser Use, "
        "Firecrawl) — those expose CDP per session but live-session routing is "
        "a follow-up. Camofox is REST-only and will never support CDP. If the "
        "tool is in your toolset at all, a CDP endpoint is already reachable.\n\n"
        f"**CDP method reference:** {CDP_DOCS_URL} — use web_extract on a "
        "method's URL (e.g. '/tot/Page/#method-handleJavaScriptDialog') "
        "to look up parameters and return shape.\n\n"
        "**Common patterns:**\n"
        "- List tabs: method='Target.getTargets', params={}\n"
        "- Handle a native JS dialog: method='Page.handleJavaScriptDialog', "
        "params={'accept': true, 'promptText': ''}, target_id=<tabId>\n"
        "- Get all cookies: method='Network.getAllCookies', params={}\n"
        "- Eval in a specific tab: method='Runtime.evaluate', "
        "params={'expression': '...', 'returnByValue': true}, "
        "target_id=<tabId>\n"
        "- Set viewport for a tab: method='Emulation.setDeviceMetricsOverride', "
        "params={'width': 1280, 'height': 720, 'deviceScaleFactor': 1, "
        "'mobile': false}, target_id=<tabId>\n\n"
        "**Usage rules:**\n"
        "- Browser-level methods (Target.*, Browser.*, Storage.*): omit "
        "target_id and frame_id.\n"
        "- Page-level methods (Page.*, Runtime.*, DOM.*, Emulation.*, "
        "Network.* scoped to a tab): pass target_id from Target.getTargets.\n"
        "- **Cross-origin iframe scope** (Runtime.evaluate inside an OOPIF, "
        "Page.* targeting a frame target, etc.): pass frame_id from the "
        "browser_snapshot frame_tree output. This routes through the CDP "
        "supervisor's live connection — the only reliable way on "
        "Browserbase where stateless CDP calls hit signed-URL expiry.\n"
        "- Each stateless call (without frame_id) is independent — sessions "
        "and event subscriptions do not persist between calls. For stateful "
        "workflows, prefer the dedicated browser tools or use frame_id "
        "routing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "description": (
                    "CDP method name, e.g. 'Target.getTargets', "
                    "'Runtime.evaluate', 'Page.handleJavaScriptDialog'."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Method-specific parameters as a JSON object. Omit or "
                    "pass {} for methods that take no parameters."
                ),
                "properties": {},
                "additionalProperties": True,
            },
            "target_id": {
                "type": "string",
                "description": (
                    "Optional. Target/tab ID from Target.getTargets result "
                    "(each entry's 'targetId'). Use for page-level methods "
                    "at the top-level tab scope. Mutually exclusive with "
                    "frame_id."
                ),
            },
            "frame_id": {
                "type": "string",
                "description": (
                    "Optional. Out-of-process iframe (OOPIF) frame_id from "
                    "browser_snapshot.frame_tree.children[] where "
                    "is_oopif=true. When set, routes the call through the "
                    "CDP supervisor's live session for that iframe. "
                    "Essential for Runtime.evaluate inside cross-origin "
                    "iframes, especially on Browserbase where fresh "
                    "per-call CDP connections can't keep up with signed "
                    "URL rotation. For same-origin iframes, use parent "
                    "contentWindow/contentDocument from Runtime.evaluate "
                    "at the top-level page instead."
                ),
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Timeout in seconds (default 30, max 300)."
                ),
                "default": 30,
            },
        },
        "required": ["method"],
    },
}


# registry.register code removed to fit WriterAgent
