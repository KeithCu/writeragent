# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Lightweight public tunnel for MCP — hard-coded providers in one file.

Providers: cloudflare, bore, ngrok, tailscale. Settings expose enable, provider
select, and one shared ``mcp.tunnel_provider_token`` (“Provider config”) whose
meaning depends on the selected provider (ngrok authtoken, Cloudflare tunnel
token, Bore server / optional secret). Tailscale ignores it (CLI login).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from plugin.framework.worker_pool import AsyncProcess

log = logging.getLogger("writeragent.mcp.tunnel")

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_PROVIDER = "cloudflare"
DEFAULT_BORE_SERVER = "bore.pub"

# ── Provider helpers (pure build/parse) ───────────────────────────────

_CLOUDFLARE_QUICK_URL_RE = re.compile(r"(https://[\w.-]+\.trycloudflare\.com)")
# Token / named tunnels may log a custom hostname (not trycloudflare.com).
_CLOUDFLARE_ANY_URL_RE = re.compile(r"(https://[\w.-]+)")
_BORE_URL_RE = re.compile(r"listening at ([\w.\-]+:\d+)")
# Real `tailscale funnel` (cmd/tailscale/cli/serve_v2.go messageForPort,
# msgFunnelAvailable) prints the header and the URL on different lines:
#   Available on the internet:
#
#   https://<host>.<tailnet>.ts.net/
#   |-- proxy http://127.0.0.1:<port>
# AsyncProcess._read_stream delivers one line at a time, so a regex that
# requires "Available at " on the same line as the URL never matches,
# parse_tailscale_url stays None, and URL_ACQUIRED never fires. Match the
# Funnel hostname wherever it appears (optional :port for 8443/10000; 443
# omits the port).
_TAILSCALE_URL_RE = re.compile(r"(https://[\w.-]+\.ts\.net(?::\d+)?)")

# `tailscale serve --https=443 off` wipes any user-configured serve on port
# 443. Only turn off the Funnel port this session armed.
def _tailscale_off_commands(port: int) -> tuple[list[str]]:
    return (["tailscale", "funnel", str(int(port)), "off"],)

_REDACT_FLAGS = frozenset({"--authtoken", "--token", "--secret"})


def build_cloudflare_command(port: int, provider_token: str = "") -> list[str]:
    """Quick tunnel when config empty; ``run`` when Provider config set.

    Token tunnels use ingress configured in the Cloudflare dashboard (point that
    service at ``http://localhost:<mcp_port>``).
    """
    token = (provider_token or "").strip()
    if token:
        return ["cloudflared", "tunnel", "--no-autoupdate", "run"]
    return ["cloudflared", "tunnel", "--no-autoupdate", "--url", "http://localhost:%s" % int(port)]


_CLOUDFLARE_IGNORED_HOSTS = frozenset({"cloudflare.com", "www.cloudflare.com", "developers.cloudflare.com", "blog.cloudflare.com", "pkg.cloudflare.com", "github.com", "api.trycloudflare.com"})


def parse_cloudflare_url(line: str) -> Optional[str]:
    """Parse quick tunnel URL or custom hostname from cloudflared logs."""
    if not line:
        return None
    m = _CLOUDFLARE_QUICK_URL_RE.search(line)
    if m:
        url = m.group(1)
        host = url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
        if host not in _CLOUDFLARE_IGNORED_HOSTS:
            return url
    # Token / named tunnels may log a custom hostname; ignore docs/marketing links.
    for match in _CLOUDFLARE_ANY_URL_RE.finditer(line):
        url = match.group(1)
        host = url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
        if host in _CLOUDFLARE_IGNORED_HOSTS or host.endswith(".cloudflare.com"):
            continue
        return url
    return None


def parse_bore_provider_config(value: str) -> tuple[str, str]:
    """Parse Provider config for Bore → ``(server, secret)``.

    - empty → (bore.pub, "")
    - ``host secret`` (whitespace) → server + secret
    - ``host:secret`` when host looks like a hostname (has ``.`` or localhost),
      and the value is not IPv6 (multiple ``:``)
    - value with no ``.`` (and not localhost) → secret for default bore.pub
    - otherwise → server only
    """
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_BORE_SERVER, ""

    if any(ch.isspace() for ch in raw):
        parts = raw.split()
        server = parts[0]
        secret = " ".join(parts[1:]).strip()
        return server or DEFAULT_BORE_SERVER, secret

    colon_count = raw.count(":")
    if colon_count == 1:
        host, secret = raw.split(":", 1)
        host = host.strip()
        secret = secret.strip()
        if host and secret and (host == "localhost" or "." in host):
            return host, secret
    elif colon_count > 1:
        # IPv6 (or similar) — keep whole string as server; use "host secret" for secrets.
        return raw, ""

    if raw.lower() != "localhost" and "." not in raw:
        return DEFAULT_BORE_SERVER, raw

    return raw, ""


def build_bore_command(port: int, provider_token: str = "") -> list[str]:
    server, _ = parse_bore_provider_config(provider_token)
    return ["bore", "local", str(int(port)), "--to", server]


def parse_bore_url(line: str) -> Optional[str]:
    if not line:
        return None
    m = _BORE_URL_RE.search(line)
    if not m:
        return None
    # bore prints host:port with no scheme — normalize for mcp_public_url.
    return "http://%s" % m.group(1)


def build_ngrok_command(port: int, authtoken: str = "") -> list[str]:
    # Empty token → rely on ngrok CLI config / env (prior behavior).
    return ["ngrok", "http", "http://localhost:%s" % int(port), "--log", "stdout", "--log-format", "json"]


def parse_ngrok_url(line: str) -> Optional[str]:
    if not line or not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except Exception:
        return None
    if data.get("msg") == "started tunnel" and data.get("url"):
        return str(data["url"])
    return None


def detect_tunnel_auth_error(provider: str, line: str) -> Optional[str]:
    """Return a short user-facing reason when a tunnel CLI line looks like auth failure."""
    if not line:
        return None
    lower = line.lower()
    provider = (provider or "").strip().lower()

    if provider == "ngrok":
        if "ERR_NGROK_105" in line or ("authtoken" in lower and ("required" in lower or "invalid" in lower or "unauthorized" in lower)):
            return "ngrok authtoken required or invalid"
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except Exception:
                data = None
            if isinstance(data, dict):
                err = str(data.get("err") or data.get("error") or "")
                if "ERR_NGROK_105" in err or "authtoken" in err.lower():
                    return "ngrok authtoken required or invalid"

    if provider == "cloudflare":
        if any(phrase in lower for phrase in ("unauthorized", "invalid token", "invalid tunnel token", "failed to parse tunnel token", "bad tunnel token")):
            return "cloudflare tunnel token invalid or unauthorized"

    if provider == "bore" and ("unauthorized" in lower or "invalid secret" in lower):
        return "bore secret rejected by server"

    return None


def build_tailscale_command(port: int) -> list[str]:
    return ["tailscale", "funnel", str(int(port))]


def parse_tailscale_url(line: str) -> Optional[str]:
    if not line:
        return None
    m = _TAILSCALE_URL_RE.search(line)
    if not m:
        return None
    return m.group(1).rstrip("/")


def _tailscale_reset(port: int) -> None:
    for cmd in _tailscale_off_commands(port):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=_CREATION_FLAGS)
            log.debug("Tailscale off: %s", " ".join(cmd))
        except Exception:
            log.debug("Tailscale off failed: %s", " ".join(cmd), exc_info=True)


# Written next to writeragent.json when we spawn `tailscale funnel`. Survives
# a process kill so the next STOPPED stop() can reset tailscaled. Not written
# under pytest (that would touch a real LibreOffice profile).
_TAILSCALE_ARM_FILENAME = "writeragent-tailscale-funnel-armed"


def _tailscale_arm_path() -> str | None:
    """Marker path, or None when the marker must not be read or written.

    init_config() is not called here. It needs a UNO context. Only a path
    already resolved at bootstrap is used. PYTEST_CURRENT_TEST forces None
    so a test that initialized config cannot create the marker in the user's
    profile; tests that need the file monkeypatch this function.
    """
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from plugin.framework import config as config_mod

        config_path = getattr(config_mod, "_resolved_config_path", None)
    except Exception:
        return None
    if not config_path:
        return None
    return os.path.join(os.path.dirname(str(config_path)), _TAILSCALE_ARM_FILENAME)


def _tailscale_arm_file_exists() -> bool:
    import os

    path = _tailscale_arm_path()
    # bool(path) does not narrow str | None for mypy, so isfile saw str | None.
    if not path:
        return False
    return os.path.isfile(path)


def _write_tailscale_arm() -> None:
    path = _tailscale_arm_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("armed\n")
    except OSError:
        log.exception("Could not record Tailscale Funnel arm marker")


def _clear_tailscale_arm() -> None:
    import os

    path = _tailscale_arm_path()
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        log.exception("Could not clear Tailscale Funnel arm marker")


# label used in status toasts; version_args / install_url for binary check.
PROVIDERS: dict[str, dict[str, Any]] = {
    "cloudflare": {
        "label": "Cloudflare",
        "version_args": ["cloudflared", "--version"],
        "install_url": ("https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"),
        "build_command": build_cloudflare_command,
        "parse_line": parse_cloudflare_url,
        "pre_start": None,
        "post_stop": None,
    },
    "bore": {"label": "Bore", "version_args": ["bore", "--version"], "install_url": "https://github.com/ekzhang/bore/releases", "build_command": build_bore_command, "parse_line": parse_bore_url, "pre_start": None, "post_stop": None},
    "ngrok": {"label": "Ngrok", "version_args": ["ngrok", "version"], "install_url": "https://ngrok.com/download", "build_command": build_ngrok_command, "parse_line": parse_ngrok_url, "pre_start": None, "post_stop": None},
    "tailscale": {"label": "Tailscale", "version_args": ["tailscale", "version"], "install_url": "https://tailscale.com/download", "build_command": build_tailscale_command, "parse_line": parse_tailscale_url, "pre_start": _tailscale_reset, "post_stop": _tailscale_reset},
}


def provider_label(name: str) -> str:
    info = PROVIDERS.get(name)
    if info:
        return str(info["label"])
    return name.title() if name else DEFAULT_PROVIDER.title()


def binary_available(provider: str) -> bool:
    """True when the provider binary can be executed."""
    info = PROVIDERS.get(provider)
    if not info:
        log.error("Unknown tunnel provider: %s", provider)
        return False
    version_args = info["version_args"]
    install_url = info["install_url"]
    try:
        result = subprocess.run(version_args, capture_output=True, text=True, timeout=10, creationflags=_CREATION_FLAGS)
        ver = (result.stdout or result.stderr or "").strip()
        log.info("%s version: %s", provider, ver or "(empty)")
        return True
    except FileNotFoundError:
        log.exception("%s binary not found on PATH. Install from: %s", version_args[0], install_url)
        return False
    except Exception:
        log.exception("Error checking %s binary", provider)
        return False


def normalize_public_base(url: str) -> str:
    """Ensure a tunnel base URL has a scheme (bore prints host:port)."""
    base = url.rstrip("/")
    if "://" not in base:
        return "http://%s" % base
    return base


def _build_provider_command(provider: str, port: int, provider_token: str) -> list[str]:
    """Dispatch Provider config into the selected provider's CLI argv."""
    if provider == "ngrok":
        return build_ngrok_command(port, provider_token)
    if provider == "cloudflare":
        return build_cloudflare_command(port, provider_token)
    if provider == "bore":
        return build_bore_command(port, provider_token)
    info = PROVIDERS[provider]
    return info["build_command"](port)


def _build_provider_env(provider: str, provider_token: str) -> dict[str, str]:
    """Return environment variables for provider secrets."""
    env: dict[str, str] = {}
    token = (provider_token or "").strip()
    if not token:
        return env

    if provider == "cloudflare":
        env["TUNNEL_TOKEN"] = token
    elif provider == "ngrok":
        env["NGROK_AUTHTOKEN"] = token
    elif provider == "bore":
        _, secret = parse_bore_provider_config(token)
        if secret:
            env["BORE_SECRET"] = secret
    return env


import dataclasses
from plugin.mcp.tunnel_state import DEFAULT_MAX_RETRIES, CancelRetryTimerEffect, NotifyUrlAcquiredEffect, ScheduleRetryTimerEffect, StartProcessEffect, TerminateProcessEffect, TunnelEvent, TunnelEventKind, TunnelState, TunnelStatus, next_state


class TunnelManager:
    """Owns a single tunnel subprocess for the selected provider with pure FSM state."""

    _lock: threading.RLock
    # Serializes Tailscale reset (pre_start and post_stop) against each other.
    # Not the manager lock: holding that across the ~10s CLI froze stop().
    _provider_cfg_lock: threading.Lock
    # Bumped by start() and stop(). A binary probe captured the value it
    # began with; if it differs when the probe finishes, that result is stale.
    _start_epoch: int
    # Epoch of a start() currently inside binary_available(), or None.
    # Retry callbacks must not spawn a process during that window.
    _binary_probe_epoch: int | None
    # Identity of the start _sync_tunnel armed. stop() clears it so a
    # start that was only queued cannot run after a disable.
    _pending_start: object | None
    # Bumped when a provider whose post_stop would wipe its own public
    # config is about to start. A background reset captured the old value.
    _tunnel_generation: int
    # True after this process has scheduled a Tailscale reset. An idle
    # stop() in the same process must not reset again. A new process starts
    # false, so a crash marker is still honored.
    _tailscale_reset_issued: bool
    # Set by StartProcessEffect when pre_start must run off _lock. Cleared
    # when _dispatch_unlocked returns it to the caller.
    _pending_launch: tuple[StartProcessEffect, int, int, Callable[[], None]] | None

    def __init__(self) -> None:
        self._state: TunnelState = TunnelState()
        self._process: Optional[AsyncProcess] = None
        self._reconnect_timer: Optional[threading.Timer] = None
        self._lock = threading.RLock()
        self._provider_cfg_lock = threading.Lock()
        self._start_epoch = 0
        self._binary_probe_epoch = None
        self._pending_start = None
        self._tunnel_generation = 0
        self._tailscale_reset_issued = False
        self._pending_launch = None

    @property
    def public_url(self) -> Optional[str]:
        return self._state.public_url

    @property
    def _public_url(self) -> Optional[str]:
        return self._state.public_url

    @_public_url.setter
    def _public_url(self, val: Optional[str]) -> None:
        self._state = dataclasses.replace(self._state, public_url=val)

    @property
    def provider(self) -> Optional[str]:
        return self._state.provider if self._state.desired_running else None

    @property
    def _provider(self) -> Optional[str]:
        return self._state.provider if self._state.desired_running else None

    @_provider.setter
    def _provider(self, val: Optional[str]) -> None:
        self._state = dataclasses.replace(self._state, provider=val or DEFAULT_PROVIDER)

    @property
    def _port(self) -> Optional[int]:
        return self._state.port if self._state.desired_running else None

    @_port.setter
    def _port(self, val: Optional[int]) -> None:
        if val is not None:
            self._state = dataclasses.replace(self._state, port=val)

    @property
    def _provider_token(self) -> str:
        return self._state.provider_token

    @_provider_token.setter
    def _provider_token(self, val: str) -> None:
        self._state = dataclasses.replace(self._state, provider_token=val or "")

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    @property
    def is_reconnecting(self) -> bool:
        return self._state.status == TunnelStatus.RECONNECTING

    @property
    def state(self) -> TunnelState:
        return self._state

    @property
    def status(self) -> TunnelStatus:
        return self._state.status

    @property
    def retry_count(self) -> int:
        return self._state.retry_count

    @property
    def max_retries(self) -> int:
        return self._state.max_retries

    @property
    def last_error(self) -> Optional[str]:
        """Short reason for the last failed start / auth / early exit, if any."""
        return self._state.last_error

    @property
    def _last_error(self) -> Optional[str]:
        return self._state.last_error

    @_last_error.setter
    def _last_error(self, val: Optional[str]) -> None:
        self._state = dataclasses.replace(self._state, last_error=val)

    def mcp_public_url(self) -> Optional[str]:
        """Streamable-HTTP MCP endpoint on the public tunnel, if known."""
        base = self._state.public_url
        if not base:
            return None
        return "%s/mcp" % normalize_public_base(base)

    def _dispatch_unlocked(self, event: TunnelEvent) -> tuple[StartProcessEffect, int, int, Callable[[], None]] | None:
        previous_provider = self._state.provider
        previous_url = self._state.public_url
        previous_status = self._state.status
        self._pending_launch = None
        transition = next_state(self._state, event)
        self._state = transition.state
        self._apply_effects_unlocked(transition.effects, previous_status)
        self._retire_snippet_url_if_not_live(previous_provider, previous_url, previous_status)
        pending = self._pending_launch
        self._pending_launch = None
        return pending

    def _retire_snippet_provider(self, provider: str) -> None:
        """Drop the settings snippet cache for *provider* (no tunnel lock needed)."""
        try:
            from plugin.mcp.mcp_ui import clear_tested_provider_tunnel_url

            clear_tested_provider_tunnel_url(provider)
        except Exception:
            log.exception("Failed to clear cached MCP tunnel URL for %s", provider)

    def _retire_snippet_url_if_not_live(self, previous_provider: str, previous_url: Optional[str], previous_status: TunnelStatus) -> None:
        """Retire the snippet URL when this provider is no longer serving it.

        URL acquire copies the public URL into the settings cache, and that
        cache outlives stop, failure, and reconnect (those clear
        ``TunnelState.public_url``). The snippet would keep copying the dead
        URL. Retire the provider that lost the URL. A later acquire or Test
        stores a new one. An idle ``stop()`` that was already stopped does not
        retire, so a Settings Test URL survives until the tunnel actually runs.
        """
        serving = self._state.status == TunnelStatus.CONNECTED and bool(self._state.public_url)
        if serving:
            # Only the provider that actually published a URL is stale after a switch.
            # A fresh manager's default provider has no URL; retiring it would drop a
            # Settings Test for that provider when the first start uses another one.
            if previous_url and previous_provider and previous_provider != self._state.provider:
                self._retire_snippet_provider(previous_provider)
            return
        # First start has not published a URL yet.
        if self._state.status == TunnelStatus.STARTING and not previous_url and previous_status == TunnelStatus.STOPPED:
            return
        # stop() on a manager that never left STOPPED.
        if self._state.status == TunnelStatus.STOPPED and previous_status == TunnelStatus.STOPPED and not previous_url:
            return
        provider = previous_provider or self._state.provider
        if provider:
            self._retire_snippet_provider(provider)

    def _schedule_post_stop_unlocked(self, provider: str, port: int) -> None:
        """Run provider post_stop off the manager lock, unless a newer session started.

        The generation is captured now (default arg, not a closure over the
        loop variable). StartProcessEffect bumps it before spawning a provider
        whose reset would destroy that new session. The background thread
        re-checks under _provider_cfg_lock so a reset that already passed the
        check cannot overlap the next pre_start/spawn: that start waits for
        the same lock.
        """
        info = PROVIDERS.get(provider)
        post_stop: Optional[Callable[[int], None]] = info.get("post_stop") if info else None
        if not post_stop:
            return
        if provider == "tailscale":
            self._tailscale_reset_issued = True
        generation = self._tunnel_generation
        cfg_lock = self._provider_cfg_lock
        # START_REQUESTED updates self._state before effects run, so reading
        # self._state.port here is the new port and `tailscale funnel <newport>
        # off` hits the wrong one. Prefer the port from TerminateProcessEffect
        # (the old session's port).
        target_port = port

        def _safe_post_stop(expected: int = generation, fn: Callable[[int], None] = post_stop, prov: str = provider, p: int = target_port) -> None:
            with cfg_lock:
                # A newer Tailscale session bumped the generation after this
                # reset was scheduled. Running it now would clear the Funnel
                # that session's pre_start is about to (or already did) create.
                if expected != self._tunnel_generation:
                    log.info("Ignoring stale MCP tunnel post_stop for %s", prov)
                    return
                try:
                    assert fn is not None
                    fn(p)
                except Exception:
                    log.exception("Tunnel post_stop failed for %s", prov)
                    return
                # Do not clear the crash marker if a newer session started
                # while the CLI was running. That session owns the marker.
                if prov == "tailscale" and expected == self._tunnel_generation:
                    _clear_tailscale_arm()

        from plugin.framework.worker_pool import run_in_background

        run_in_background(_safe_post_stop, name="tunnel-post-stop", dedicated=True)

    def _finish_pending_launch(self, pending: tuple[StartProcessEffect, int, int, Callable[[], None]]) -> None:
        """Run pre_start without _lock, then spawn if this start is still current.

        Caller must not hold _lock. pre_start takes _provider_cfg_lock so an
        in-flight post_stop finishes before the reset that precedes spawn.
        """
        effect, epoch, generation, pre_start = pending
        failed = False
        with self._provider_cfg_lock:
            if generation != self._tunnel_generation:
                log.info("Ignoring stale MCP tunnel pre_start for %s", effect.provider)
                return
            try:
                pre_start()
            except Exception:
                log.exception("Tunnel pre_start failed for %s", effect.provider)
                failed = True
        with self._lock:
            if self._start_epoch != epoch or self._tunnel_generation != generation:
                log.info("Ignoring stale MCP tunnel start after pre_start (%s)", effect.provider)
                return
            if not self._state.desired_running:
                return
            if failed:
                self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error="%s pre_start failed" % effect.provider, desired_running=False)
                return
            self._spawn_process_unlocked(effect)

    def _spawn_process_unlocked(self, effect: StartProcessEffect) -> None:
        """Start the provider CLI. Caller holds _lock. pre_start has already finished."""
        provider = effect.provider
        info = PROVIDERS.get(provider)
        if not info:
            self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error="unknown tunnel provider: %s" % provider, desired_running=False)
            return
        parse_line: Callable[[str], Optional[str]] = info["parse_line"]
        cmd = _build_provider_command(provider, effect.port, effect.provider_token)
        log.info("Starting MCP tunnel (%s): %s", provider, _redact_cmd_for_log(cmd))
        # Identity of the process this pair of callbacks belongs to.
        # Filled in after construction, before start(), so a line or
        # exit that arrives during start() still sees the owner.
        spawned: dict[str, Any] = {}

        def _is_current_process() -> bool:
            proc = spawned.get("proc")
            return proc is not None and self._process is proc

        def _on_line(line: str) -> None:
            with self._lock:
                # A replaced process can still emit a late line. Applying
                # it would publish the old URL or fail the new tunnel.
                if not _is_current_process():
                    return
                if self._state.public_url:
                    return
                auth_err = detect_tunnel_auth_error(provider, line)
                if auth_err:
                    log.error("MCP tunnel auth error (%s): %s", provider, auth_err)
                    self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1, "auth_error": auth_err}))
                    return
                url = parse_line(line)
                if url:
                    log.info("MCP tunnel URL (%s): %s", provider, url)
                    self._dispatch_unlocked(TunnelEvent(TunnelEventKind.URL_ACQUIRED, {"url": url}))

        def _on_exit(rc: int) -> None:
            log.info("MCP tunnel process (%s) exited with code %s", provider, rc)
            with self._lock:
                # A provider/token restart does Terminate then Start. The old wait
                # thread's _on_exit must not run unconditionally: that sets
                # _process = None and dispatches PROCESS_EXITED, which forces
                # RECONNECTING and orphans the replacement (a retry can then
                # spawn a third binary). Ignore the exit unless this callback
                # still owns the process TunnelManager tracks.
                if not _is_current_process():
                    log.info("Ignoring stale MCP tunnel exit (%s, code %s)", provider, rc)
                    return
                self._process = None
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": rc}))

        # Record the arm before start() returns. A kill in that window still
        # leaves the marker. If the CLI never starts, clear it in the handlers
        # below so a failed spawn does not look like a live Funnel.
        if provider == "tailscale":
            _write_tailscale_arm()
        try:
            from plugin.framework.worker_pool import AsyncProcess

            # Some CLIs (cloudflared) print the URL on stderr more often than stdout.
            env = os.environ.copy()
            env.update(_build_provider_env(provider, effect.provider_token))
            proc = AsyncProcess(cmd, stdout_cb=_on_line, stderr_cb=_on_line, on_exit_cb=_on_exit, creationflags=_CREATION_FLAGS, env=env)
            spawned["proc"] = proc
            self._process = proc
            proc.start()
        except Exception as e:
            from plugin.framework.errors import ToolExecutionError
            self._process = None
            if provider == "tailscale":
                _clear_tailscale_arm()
            if isinstance(e, FileNotFoundError) or (isinstance(e, ToolExecutionError) and isinstance(e.__cause__, FileNotFoundError)):
                log.exception("%s binary not found", info["version_args"][0])
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1, "auth_error": "%s binary not found on PATH" % info["version_args"][0]}))
            else:
                log.exception("Failed to start MCP tunnel (%s)", provider)
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1, "auth_error": "failed to start %s tunnel" % provider}))

    def _apply_effects_unlocked(self, effects: list[Any], previous_status: TunnelStatus) -> None:
        for effect in effects:
            if isinstance(effect, CancelRetryTimerEffect):
                if self._reconnect_timer is not None:
                    try:
                        self._reconnect_timer.cancel()
                    except Exception:
                        pass
                    self._reconnect_timer = None

            elif isinstance(effect, TerminateProcessEffect):
                proc = self._process
                if proc is not None:
                    self._process = None
                    try:
                        proc.terminate()
                    except Exception:
                        log.exception("Error terminating tunnel process")
                # next_state stores the new provider and port on TunnelState before
                # effects run. post_stop must use TerminateProcessEffect's
                # provider and port (the session that owned this process), or
                # Tailscale off hits the new port or the wrong provider.
                provider = effect.provider or self._state.provider
                # The effect carries the provider and port that owned this process.
                # Reading TunnelState here is the post-transition value.
                port = effect.port
                # RECONNECTING and FAILED already cleared _process in _on_exit.
                # Requiring a live process skipped Tailscale funnel/serve reset
                # when switching provider or disabling the tunnel. That config
                # lives on tailscaled and kept the public URL pointed at the
                # local MCP port. Retry exhaustion emits this effect too, with
                # the process already cleared. Reset whenever we leave that
                # provider, including when the subprocess is already gone. An
                # idle stop that was already STOPPED must not reset — settings
                # sync calls stop() again and would wipe Funnel on every save.
                # A crash marker is the exception, handled in stop().
                leaving_live_session = proc is not None or previous_status != TunnelStatus.STOPPED
                if leaving_live_session:
                    self._schedule_post_stop_unlocked(provider, port)

            elif isinstance(effect, StartProcessEffect):
                provider = effect.provider
                info = PROVIDERS.get(provider)
                if not info:
                    self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error="unknown tunnel provider: %s" % provider, desired_running=False)
                    continue

                pre_start: Optional[Callable[[int], None]] = info.get("pre_start")
                if pre_start:
                    # _tailscale_reset is two CLIs, 5s each. Running it on this thread
                    # while _lock is held blocks stop() and the UI sync path
                    # for that whole wait. post_stop was already moved off the
                    # lock for the same reason. The reset must finish before
                    # `tailscale funnel <port>` or it tears down the Funnel
                    # this start just configured, so it is not fired and
                    # forgotten. The caller releases _lock, runs pre_start,
                    # then spawns only if this epoch and generation are still
                    # current.
                    # Bump only when this provider's own post_stop would wipe
                    # the config it is about to create. A cloudflare start
                    # must still let a pending Tailscale reset run.
                    if info.get("post_stop"):
                        self._tunnel_generation += 1
                    # Bind the port so the lambda takes 0 args for the tuple.
                    port = effect.port

                    def bound_pre_start(p: int = port) -> None:
                        assert pre_start is not None
                        pre_start(p)

                    self._pending_launch = (effect, self._start_epoch, self._tunnel_generation, bound_pre_start)
                    continue

                self._spawn_process_unlocked(effect)

            elif isinstance(effect, ScheduleRetryTimerEffect):
                if self._reconnect_timer is not None:
                    try:
                        self._reconnect_timer.cancel()
                    except Exception:
                        pass
                log.info("Scheduling MCP tunnel reconnect in %.1fs (attempt %s/%s)", effect.delay_seconds, effect.attempt, effect.max_retries)
                # Identity of this timer, same idea as spawned["proc"] for
                # exit. Filled in before start() so a callback that is
                # already queued still sees the owner.
                scheduled: dict[str, threading.Timer] = {}

                def _fire(owner: dict[str, threading.Timer] = scheduled) -> None:
                    self._on_retry_timer_expired(owner.get("timer"))

                timer = threading.Timer(effect.delay_seconds, _fire)
                timer.daemon = True
                scheduled["timer"] = timer
                self._reconnect_timer = timer
                timer.start()

            elif isinstance(effect, NotifyUrlAcquiredEffect):
                try:
                    from plugin.mcp.mcp_ui import notify_tunnel_url_acquired

                    mcp_url = self.mcp_public_url()
                    if mcp_url:
                        notify_tunnel_url_acquired(effect.provider, mcp_url)
                except Exception:
                    pass

    def _on_retry_timer_expired(self, timer: Optional[threading.Timer]) -> None:
        with self._lock:
            # Timer.cancel() does not stop a callback that has already started.
            # That callback used to block on this lock while start() ran
            # binary_available() (up to ~10s), survive CancelRetryTimerEffect,
            # then dispatch RETRY_TIMER_EXPIRED after StartProcessEffect and
            # leave a second tunnel running. Ignore the callback unless this
            # timer is still the one TunnelManager tracks. Also skip it while
            # start() is outside the lock probing the provider binary — that
            # start() owns the next process, or marks the tunnel FAILED.
            if timer is None or self._reconnect_timer is not timer:
                log.info("Ignoring stale MCP tunnel retry timer")
                return
            if self._binary_probe_epoch is not None:
                log.info("Ignoring MCP tunnel retry during provider binary check")
                self._reconnect_timer = None
                return
            self._reconnect_timer = None
            pending = self._dispatch_unlocked(TunnelEvent(TunnelEventKind.RETRY_TIMER_EXPIRED))
        # pre_start (Tailscale reset) must not run while this thread holds
        # _lock. stop() has to be able to take the lock during that CLI.
        if pending is not None:
            self._finish_pending_launch(pending)

    def note_pending_start(self, token: object) -> None:
        """Arm *token* as the only start that may proceed until stop() or a newer arm."""
        with self._lock:
            self._pending_start = token

    def start(self, port: int, provider: str = DEFAULT_PROVIDER, provider_token: str = "", max_retries: int = DEFAULT_MAX_RETRIES, start_token: object | None = None) -> bool:
        """Start (or keep) a tunnel to *port*. Returns False if start failed."""
        import os

        if os.environ.get("WRITERAGENT_TESTING"):
            return True

        provider = (provider or DEFAULT_PROVIDER).strip().lower()
        token = (provider_token or "").strip()
        info = PROVIDERS.get(provider)
        if info is None:
            log.error("Unknown tunnel provider: %s", provider)
            with self._lock:
                if start_token is not None and self._pending_start is not start_token:
                    log.info("Ignoring superseded MCP tunnel start (%s)", provider)
                    return False
                self._start_epoch += 1
                self._binary_probe_epoch = None
                self._pending_start = None
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
                self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error="unknown tunnel provider: %s" % provider, desired_running=False)
            self._retire_snippet_provider(provider)
            return False

        with self._lock:
            if start_token is not None and self._pending_start is not start_token:
                log.info("Ignoring superseded MCP tunnel start (%s)", provider)
                return False
            self._start_epoch += 1
            epoch = self._start_epoch
            if self.is_running and self._state.port == int(port) and self._state.provider == provider and self._state.provider_token == token:
                log.info("Tunnel already running (%s) at %s", provider, self.public_url)
                if self.public_url:
                    self._state = dataclasses.replace(self._state, last_error=None)
                return True
            self._binary_probe_epoch = epoch

        # binary_available() is a subprocess with a 10s timeout. Holding
        # _lock across it freezes LibreOffice: _sync_tunnel() runs on the UI
        # thread on config:changed, and stop()/retry cannot take the lock
        # either. Probe with the lock released. _start_epoch drops the result
        # when stop() or a newer start() landed during the probe.
        try:
            available = binary_available(provider)
        except BaseException:
            with self._lock:
                if self._binary_probe_epoch == epoch:
                    self._binary_probe_epoch = None
                    if self._state.status == TunnelStatus.RECONNECTING and self._reconnect_timer is None:
                        self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
            raise

        with self._lock:
            if self._binary_probe_epoch == epoch:
                self._binary_probe_epoch = None
            if start_token is not None and self._pending_start is not start_token:
                log.info("Ignoring superseded MCP tunnel start (%s)", provider)
                return False
            if epoch != self._start_epoch:
                log.info("Ignoring stale MCP tunnel start (%s)", provider)
                return False
            if not available:
                binary = info["version_args"][0]
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
                self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error="%s binary not found on PATH" % binary, desired_running=False)
                self._retire_snippet_provider(provider)
                return False

            pending = self._dispatch_unlocked(TunnelEvent(TunnelEventKind.START_REQUESTED, {"port": int(port), "provider": provider, "provider_token": token, "max_retries": max_retries}))
        # Tailscale pre_start is the same reset as post_stop (~10s). Run it
        # outside _lock so stop() is not stuck behind it, then spawn only if
        # that stop (or a newer start) did not win.
        if pending is not None:
            self._finish_pending_launch(pending)
        with self._lock:
            if epoch != self._start_epoch:
                log.info("Ignoring stale MCP tunnel start (%s)", provider)
                return False
            # Match the state machine, not substrings in last_error ("not found
            # on PATH", "failed to start"). A reworded message would otherwise
            # return True for a failed start.
            return self._state.status != TunnelStatus.FAILED

    def stop(self) -> None:
        with self._lock:
            # Invalidate an in-flight or merely queued binary probe so it
            # cannot start afterwards.
            self._start_epoch += 1
            self._binary_probe_epoch = None
            self._pending_start = None
            # Captured before STOP_REQUESTED moves every state to STOPPED.
            idle = self._state.status == TunnelStatus.STOPPED and self._process is None
            self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
            # LibreOffice killed while Funnel was armed leaves tailscaled
            # forwarding the MCP port. The new process is STOPPED and its
            # provider defaults to cloudflare, so the terminate effect does
            # not look up Tailscale post_stop. The idle-stop guard skips
            # reset on purpose: settings sync calls stop() on every save and
            # must not wipe a Funnel this process did not arm. Spawn writes a
            # marker next to writeragent.json. The first stop() in a later
            # process resets Tailscale and clears it. _tailscale_reset_issued
            # blocks a second stop in this process from resetting again before
            # the background clear finishes.
            if idle and not self._tailscale_reset_issued and _tailscale_arm_file_exists():
                self._schedule_post_stop_unlocked("tailscale", self._state.port)


def _redact_cmd_for_log(cmd: list[str]) -> str:
    """Join argv for logs; mask values after secret-bearing flags."""
    out: list[str] = []
    skip_next = False
    for part in cmd:
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if part in _REDACT_FLAGS:
            out.append(part)
            skip_next = True
            continue
        out.append(part)
    return " ".join(out)


def test_tunnel_connectivity(provider: str = DEFAULT_PROVIDER, provider_token: str = "", port: int = 18765, timeout: float = 6.0) -> tuple[bool, str, Optional[str]]:
    """Test tunnel provider availability and optionally probe connectivity to port.

    Returns (ok, user_facing_message, public_url).
    """
    import os
    import urllib.request
    from plugin.framework.i18n import _

    if os.environ.get("WRITERAGENT_TESTING"):
        sim_url = f"https://simulated-{provider}.example.com/mcp"
        return True, _("Tunnel test mode: {0} provider simulated successfully.").format(provider), sim_url

    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    info = PROVIDERS.get(provider)
    if not info:
        return False, _("Unknown tunnel provider: {0}").format(provider), None

    pname = provider_label(provider)
    binary = info["version_args"][0]

    # 1. Check binary availability and version
    try:
        res = subprocess.run(info["version_args"], capture_output=True, text=True, timeout=5, creationflags=_CREATION_FLAGS)
        version_str = (res.stdout or res.stderr or "").strip().splitlines()[0] if (res.stdout or res.stderr) else ""
    except FileNotFoundError:
        return False, _("Binary '{0}' for {1} not found on PATH.\n\nInstall from: {2}").format(binary, pname, info["install_url"]), None
    except Exception as exc:
        return False, _("Failed to execute {0} binary ({1}): {2}").format(pname, binary, exc), None

    # 2. Check if local MCP server is running on port
    local_url = f"http://localhost:{port}/health"
    local_running = False
    try:
        req = urllib.request.Request(local_url, headers={"User-Agent": "WriterAgent-Probe"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.getcode() == 200:
                local_running = True
    except Exception:
        local_running = False

    # 3. Check if an active tunnel is already running in LibreOffice
    from plugin.mcp import _shared_tunnel

    if _shared_tunnel and _shared_tunnel.is_running:
        active_p = getattr(_shared_tunnel, "_provider", None) or DEFAULT_PROVIDER
        active_url = _shared_tunnel.mcp_public_url()
        if active_url:
            base_url = normalize_public_base(_shared_tunnel._public_url or "")
            health_url = f"{base_url}/health"
            probe_ok = False
            try:
                probe_req = urllib.request.Request(health_url, headers={"User-Agent": "WriterAgent-Probe"})
                with urllib.request.urlopen(probe_req, timeout=2.0) as probe_resp:
                    if probe_resp.getcode() == 200:
                        probe_ok = True
            except Exception:
                pass

            if active_p == provider:
                if probe_ok:
                    return True, _("{0} tunnel is running and responsive!\n\nPublic endpoint:\n{1}\n\nHealth check: OK (200)").format(pname, active_url), active_url
                return True, _("{0} tunnel is active!\n\nPublic endpoint:\n{1}\n\n(Public URL acquired from active tunnel session.)").format(pname, active_url), active_url
            else:
                return True, _("{0} binary '{1}' is verified ({2}).\n\n(Note: An active {3} tunnel is currently running at {4}.)").format(pname, binary, version_str or "OK", provider_label(active_p), active_url), None

    if local_running:
        return True, _("{0} binary '{1}' is installed and verified ({2}).\n\nMCP server is running locally on port {3}.\nCheck 'Expose via public tunnel' and click OK to activate public routing.").format(pname, binary, version_str or "OK", port), None

    return True, _("{0} binary '{1}' is installed and verified ({2}).\n\nNote: MCP server is not currently running on port {3}.").format(pname, binary, version_str or "OK", port), None
