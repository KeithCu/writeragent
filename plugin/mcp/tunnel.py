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

import dataclasses
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

from plugin.framework.subprocess_env import get_subprocess_creationflags
from plugin.mcp.tunnel_state import DEFAULT_MAX_RETRIES, CancelRetryTimerEffect, NotifyUrlAcquiredEffect, ScheduleRetryTimerEffect, StartProcessEffect, TerminateProcessEffect, TunnelEvent, TunnelEventKind, TunnelState, TunnelStatus, next_state

if TYPE_CHECKING:
    from plugin.framework.worker_pool import AsyncProcess

log = logging.getLogger("writeragent.mcp.tunnel")

# LibreOffice exit must not delegate Tailscale funnel off to a background thread.
_QUIT_FUNNEL_RESET_TIMEOUT_SEC = 2.0

DEFAULT_PROVIDER = "cloudflare"
DEFAULT_BORE_SERVER = "bore.pub"

# ── Provider helpers (pure build/parse) ───────────────────────────────

_CLOUDFLARE_QUICK_URL_RE = re.compile(r"(https://[\w.-]+\.trycloudflare\.com)")
# cloudflared's route box is a line like "INF |  https://host". Any other
# https:// on the line (docs, 1.1.1.1, API hosts) must not become the MCP URL.
_CLOUDFLARE_BANNER_URL_RE = re.compile(r"\|\s*(https://[\w.-]+)")
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

# `tailscale funnel <local-port> off` is legacy. serve_v2.go isLegacyInvocation
# matches `funnel <uint16> off`, and validateArgs returns before unsetServe,
# so the Funnel stays up and subprocess.run does not raise.
# `tailscale funnel <local-port>` serves HTTPS on 443 and proxies to the local
# port. msgDisableProxy tells the operator to run `tailscale funnel --https=443 off`.
# That calls removeWebServe on the shared 443 handler, so a user Serve on 443
# is removed. Start already replaced that handler. Do not pass the local port.
# Do not run `funnel reset` or `serve reset` (those clear every port).
_TAILSCALE_OFF_CMD = ("tailscale", "funnel", "--https=443", "off")


def build_cloudflare_command(port: int, provider_token: str = "") -> list[str]:
    """Quick tunnel when config empty; ``run`` when Provider config set.

    Token tunnels use ingress configured in the Cloudflare dashboard (point that
    service at ``http://localhost:<mcp_port>``). A non-empty token selects
    ``run`` and ``extra_env`` sets ``TUNNEL_TOKEN``; ``--token`` is
    not placed on argv.
    """
    token = (provider_token or "").strip()
    if token:
        return ["cloudflared", "tunnel", "--no-autoupdate", "run"]
    return ["cloudflared", "tunnel", "--no-autoupdate", "--url", "http://localhost:%s" % int(port)]


_CLOUDFLARE_IGNORED_HOSTS = frozenset({"cloudflare.com", "github.com", "api.trycloudflare.com"})


def _cloudflare_host_ok(url: str) -> bool:
    """True when *url* is a tunnel hostname, not an IP, doc host, or Cloudflare site."""
    host = url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
    if not host or "." not in host or host in _CLOUDFLARE_IGNORED_HOSTS or host.endswith(".cloudflare.com"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def parse_cloudflare_url(line: str) -> Optional[str]:
    """Quick-tunnel host, or a hostname printed in cloudflared's route box."""
    quick = _CLOUDFLARE_QUICK_URL_RE.search(line)
    if quick and _cloudflare_host_ok(quick.group(1)):
        return quick.group(1)
    banner = _CLOUDFLARE_BANNER_URL_RE.search(line)
    if banner and _cloudflare_host_ok(banner.group(1)):
        return banner.group(1)
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
    m = _BORE_URL_RE.search(line)
    if not m:
        return None
    # bore prints host:port with no scheme — normalize for mcp_public_url.
    return "http://%s" % m.group(1)


def build_ngrok_command(port: int, authtoken: str = "") -> list[str]:
    # The authtoken argument is ignored on purpose; the token is NGROK_AUTHTOKEN
    # from extra_env, never --authtoken.
    return ["ngrok", "http", "http://localhost:%s" % int(port), "--log", "stdout", "--log-format", "json"]


def parse_ngrok_url(line: str) -> Optional[str]:
    if not line or not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
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
            except json.JSONDecodeError:
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


def build_tailscale_command(port: int, provider_token: str = "") -> list[str]:
    # Provider config is ignored; Tailscale uses CLI login.
    return ["tailscale", "funnel", str(int(port))]


def parse_tailscale_url(line: str) -> Optional[str]:
    m = _TAILSCALE_URL_RE.search(line)
    if not m:
        return None
    return m.group(1).rstrip("/")


class _FunnelResetFn(Protocol):
    def __call__(self, port: int, *, timeout: float = 5) -> bool: ...


class FunnelReset:
    """Manages Tailscale funnel pre-spawn/post-stop resets and arm marker lifecycle."""

    _reset_fn: _FunnelResetFn
    arm_filename: str
    lock: threading.Lock
    token: object
    _resets_in_flight: int

    def __init__(self, reset_fn: _FunnelResetFn, arm_filename: str) -> None:
        self._reset_fn = reset_fn
        self.arm_filename = arm_filename
        self.lock = threading.Lock()
        self.token = object()
        self._resets_in_flight = 0

    def before_spawn(self, port: int) -> None:
        self._reset_fn(port)

    def after_stop(self, port: int, *, timeout: float = 5) -> bool:
        return self._reset_fn(port, timeout=timeout)

    def arm_path(self) -> str | None:
        """Path to the funnel arm marker file beside writeragent.json (bootstrap resolves config first)."""
        from plugin.framework.config import get_config_file_path

        path = get_config_file_path()
        if not path:
            return None
        return os.path.join(os.path.dirname(path), self.arm_filename)

    def armed_file_exists(self) -> bool:
        path = self.arm_path()
        return path is not None and os.path.isfile(path)

    def mark_armed(self) -> None:
        path = self.arm_path()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("armed\n")
        except OSError:
            log.exception("Could not record Tailscale Funnel arm marker")

    def clear_armed(self) -> None:
        path = self.arm_path()
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError:
            log.exception("Could not clear Tailscale Funnel arm marker")

    def idle_ready(self) -> bool:
        return self._resets_in_flight == 0 and self.armed_file_exists()

    def capture(self) -> object:
        return self.token

    def current(self, token: object) -> bool:
        return token is self.token

    def invalidate(self) -> None:
        self.token = object()

    def note_scheduled(self) -> None:
        self._resets_in_flight += 1

    def mark_finished(self) -> None:
        if self._resets_in_flight <= 0:
            log.error("Tailscale reset in-flight count already zero")
        else:
            self._resets_in_flight -= 1


class Provider:
    name: str = ""
    label: str = ""
    version_args: tuple[str, ...] = ()
    install_url: str = ""
    reset: FunnelReset | None = None

    def build_command(self, port: int, provider_token: str) -> list[str]:
        raise NotImplementedError

    def parse_line(self, line: str) -> str | None:
        raise NotImplementedError

    def extra_env(self, provider_token: str) -> dict[str, str]:
        return {}


class CloudflareProvider(Provider):
    name: str = "cloudflare"
    label: str = "Cloudflare"
    version_args: tuple[str, ...] = ("cloudflared", "--version")
    install_url: str = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"

    def build_command(self, port: int, provider_token: str = "") -> list[str]:
        return build_cloudflare_command(port, provider_token)

    def parse_line(self, line: str) -> str | None:
        return parse_cloudflare_url(line)

    def extra_env(self, provider_token: str = "") -> dict[str, str]:
        token = (provider_token or "").strip()
        if not token:
            return {}
        return {"TUNNEL_TOKEN": token}


class BoreProvider(Provider):
    name: str = "bore"
    label: str = "Bore"
    version_args: tuple[str, ...] = ("bore", "--version")
    install_url: str = "https://github.com/ekzhang/bore/releases"

    def build_command(self, port: int, provider_token: str = "") -> list[str]:
        return build_bore_command(port, provider_token)

    def parse_line(self, line: str) -> str | None:
        return parse_bore_url(line)

    def extra_env(self, provider_token: str = "") -> dict[str, str]:
        token = (provider_token or "").strip()
        if not token:
            return {}
        _, secret = parse_bore_provider_config(token)
        if secret:
            return {"BORE_SECRET": secret}
        return {}


class NgrokProvider(Provider):
    name: str = "ngrok"
    label: str = "Ngrok"
    version_args: tuple[str, ...] = ("ngrok", "version")
    install_url: str = "https://ngrok.com/download"

    def build_command(self, port: int, provider_token: str = "") -> list[str]:
        return build_ngrok_command(port, provider_token)

    def parse_line(self, line: str) -> str | None:
        return parse_ngrok_url(line)

    def extra_env(self, provider_token: str = "") -> dict[str, str]:
        token = (provider_token or "").strip()
        if not token:
            return {}
        return {"NGROK_AUTHTOKEN": token}


class TailscaleProvider(Provider):
    name: str = "tailscale"
    label: str = "Tailscale"
    version_args: tuple[str, ...] = ("tailscale", "version")
    install_url: str = "https://tailscale.com/download"
    ARM_FILENAME: str = "writeragent-tailscale-funnel-armed"
    reset: FunnelReset | None

    def __init__(self) -> None:
        self.reset = FunnelReset(
            lambda port, timeout=5: TailscaleProvider.reset_funnel(port, timeout=timeout),
            self.ARM_FILENAME,
        )

    def build_command(self, port: int, provider_token: str = "") -> list[str]:
        return build_tailscale_command(port, provider_token)

    def parse_line(self, line: str) -> str | None:
        return parse_tailscale_url(line)

    @staticmethod
    def reset_funnel(port: int, *, timeout: float = 5) -> bool:
        """Clear the HTTPS/443 Funnel handler. *port* is the local MCP target, for the log only."""
        cmd = _TAILSCALE_OFF_CMD
        shown = " ".join(cmd)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **get_subprocess_creationflags())
        except Exception:
            log.exception("Tailscale funnel off failed (local MCP port %s): %s", port, shown)
            return False
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:500]
            log.warning("Tailscale funnel off exited %s (local MCP port %s): %s — %s", result.returncode, port, shown, err)
            return False
        # A normal return on failure made _safe_post_stop delete the arm file.
        # Only exit 0 may delete it.
        log.info("Tailscale funnel off (local MCP port %s): %s", port, shown)
        return True


PROVIDER_CLASSES: tuple[type[Provider], ...] = (
    CloudflareProvider,
    BoreProvider,
    NgrokProvider,
    TailscaleProvider,
)
_PROVIDER_BY_NAME: dict[str, type[Provider]] = {cls.name: cls for cls in PROVIDER_CLASSES}


def provider_label(name: str) -> str:
    cls = _PROVIDER_BY_NAME.get(name)
    if cls:
        return cls.label
    return name.title() if name else DEFAULT_PROVIDER.title()


def binary_available(provider: str) -> str | None:
    """None when the version command finished; otherwise the short last_error.

    A non-zero exit is still None.
    """
    cls = _PROVIDER_BY_NAME.get(provider)
    if not cls:
        log.error("Unknown tunnel provider: %s", provider)
        return "unknown tunnel provider: %s" % provider
    version_args = cls.version_args
    binary = version_args[0]
    install_url = cls.install_url
    try:
        result = subprocess.run(version_args, capture_output=True, text=True, timeout=10, **get_subprocess_creationflags())
        ver = (result.stdout or result.stderr or "").strip()
        log.info("%s version: %s", provider, ver or "(empty)")
        return None
    except FileNotFoundError:
        log.warning("%s binary not found on PATH. Install from: %s", binary, install_url)
        return "%s binary not found on PATH" % binary
    except subprocess.TimeoutExpired:
        log.warning("%s binary check timed out", binary)
        return "%s binary check timed out" % binary
    except OSError:
        # PermissionError and other exec failures. FileNotFoundError and
        # TimeoutExpired are handled above. Anything else is a bug.
        log.exception("Error checking %s binary", provider)
        return "could not execute %s" % binary


def normalize_public_base(url: str) -> str:
    """Ensure a tunnel base URL has a scheme (bore prints host:port)."""
    base = url.rstrip("/")
    if "://" not in base:
        return "http://%s" % base
    return base


class TunnelManager:
    """Owns a single tunnel subprocess for the selected provider with pure FSM state."""

    _lock: threading.RLock
    _session: object
    _pending_start: object | None
    _providers: dict[str, Provider]
    _state: TunnelState
    _process: Optional[AsyncProcess]
    _reconnect_timer: Optional[threading.Timer]

    def __init__(self) -> None:
        self._state = TunnelState()
        self._process = None
        self._reconnect_timer = None
        self._lock = threading.RLock()
        self._session = object()
        self._pending_start = None
        self._providers = {cls.name: cls() for cls in PROVIDER_CLASSES}
        self._quit_shutdown: bool = False

    @property
    def public_url(self) -> Optional[str]:
        return self._state.public_url

    @property
    def provider(self) -> Optional[str]:
        return self._state.provider if self._state.desired_running else None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    @property
    def is_reconnecting(self) -> bool:
        return self._state.status == TunnelStatus.RECONNECTING

    @property
    def status(self) -> TunnelStatus:
        return self._state.status

    @property
    def retry_count(self) -> int:
        return self._state.retry_count

    @property
    def last_error(self) -> Optional[str]:
        """Short reason for the last failed start / auth / early exit, if any."""
        return self._state.last_error

    def mcp_public_url(self) -> Optional[str]:
        """Streamable-HTTP MCP endpoint on the public tunnel, if known."""
        base = self._state.public_url
        if not base:
            return None
        return "%s/mcp" % normalize_public_base(base)

    def _replace_session(self) -> object:
        self._session = object()
        return self._session

    def _dispatch_unlocked(self, event: TunnelEvent) -> StartProcessEffect | None:
        previous_provider = self._state.provider
        previous_url = self._state.public_url
        previous_status = self._state.status
        transition = next_state(self._state, event)
        self._state = transition.state
        pending = self._apply_effects_unlocked(transition.effects, previous_status)
        self._retire_snippet_url_if_not_live(previous_provider, previous_url, previous_status)
        return pending

    def _retire_snippet_provider(self, provider: str) -> None:
        """Drop the settings snippet cache for *provider* (no tunnel lock needed)."""
        # In-memory cache plus a posted snippet refresh. Swallowing a failure
        # left the settings snippet on a URL this provider no longer serves.
        from plugin.mcp.mcp_ui import clear_tested_provider_tunnel_url

        clear_tested_provider_tunnel_url(provider)

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

    def _reset_finished(self, reset: FunnelReset) -> None:
        with self._lock:
            reset.mark_finished()

    def _run_post_stop_body(self, provider_name: str, port: int, reset: FunnelReset, token: object, *, timeout: float) -> None:
        lock = reset.lock
        with lock:
            with self._lock:
                stale = not reset.current(token)
            if stale:
                log.info("Ignoring stale MCP tunnel post_stop for %s", provider_name)
                return
            ok = False
            try:
                ok = reset.after_stop(port, timeout=timeout) is True
            except Exception:
                log.exception("Tunnel post_stop failed for %s", provider_name)
            with self._lock:
                if ok and reset.current(token):
                    reset.clear_armed()

    def _schedule_post_stop_unlocked(self, provider_name: str, port: int) -> None:
        """Run provider post_stop off the manager lock, unless a newer session started."""
        provider = self._providers.get(provider_name)
        reset = provider.reset if provider is not None else None
        if reset is None:
            return
        token = reset.capture()
        if self._quit_shutdown:
            self._run_post_stop_body(provider_name, port, reset, token, timeout=_QUIT_FUNNEL_RESET_TIMEOUT_SEC)
            return
        reset.note_scheduled()

        def _safe_post_stop() -> None:
            try:
                self._run_post_stop_body(provider_name, port, reset, token, timeout=5)
            finally:
                self._reset_finished(reset)

        from plugin.framework.worker_pool import run_in_background

        try:
            run_in_background(_safe_post_stop, name="tunnel-post-stop", dedicated=True)
        except Exception:
            # The failure decrement takes _lock because the body will not.
            self._reset_finished(reset)
            raise

    def _finish_pending_launch(self, effect: StartProcessEffect, session: object, reset: FunnelReset, reset_token: object) -> None:
        """before_spawn runs outside _lock."""
        failed = False
        with reset.lock:
            if not reset.current(reset_token):
                log.info("Ignoring stale MCP tunnel pre_start for %s", effect.provider)
                return
            try:
                reset.before_spawn(effect.port)
            except Exception:
                log.exception("Tunnel pre_start failed for %s", effect.provider)
                failed = True
        with self._lock:
            if self._session is not session or not reset.current(reset_token):
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
        provider_name = effect.provider
        provider = self._providers[provider_name]
        parse_line: Callable[[str], Optional[str]] = provider.parse_line
        cmd = provider.build_command(effect.port, effect.provider_token)
        log.info("Starting MCP tunnel (%s): %s", provider_name, " ".join(cmd))
        # Identity of the process this pair of callbacks belongs to.
        # Filled in after construction, before start(), so a line or
        # exit that arrives during start() still sees the owner.
        proc_ref: Optional[AsyncProcess] = None

        def _is_current_process() -> bool:
            return proc_ref is not None and self._process is proc_ref

        def _on_line(line: str) -> None:
            with self._lock:
                # A replaced process can still emit a late line. Applying
                # it would publish the old URL or fail the new tunnel.
                if not _is_current_process():
                    return
                if self._state.public_url:
                    return
                auth_err = detect_tunnel_auth_error(provider_name, line)
                if auth_err:
                    log.error("MCP tunnel auth error (%s): %s", provider_name, auth_err)
                    self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1, "auth_error": auth_err}))
                    return
                url = parse_line(line)
                if url:
                    log.info("MCP tunnel URL (%s): %s", provider_name, url)
                    self._dispatch_unlocked(TunnelEvent(TunnelEventKind.URL_ACQUIRED, {"url": url, "now": time.monotonic()}))

        def _on_exit(rc: int) -> None:
            log.info("MCP tunnel process (%s) exited with code %s", provider_name, rc)
            with self._lock:
                # A provider/token restart does Terminate then Start. The old wait
                # thread's _on_exit must not run unconditionally: that sets
                # _process = None and dispatches PROCESS_EXITED, which forces
                # RECONNECTING and orphans the replacement (a retry can then
                # spawn a third binary). Ignore the exit unless this callback
                # still owns the process TunnelManager tracks.
                if not _is_current_process():
                    log.info("Ignoring stale MCP tunnel exit (%s, code %s)", provider_name, rc)
                    return
                self._process = None
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": rc, "now": time.monotonic()}))

        # Record the arm before start() returns. A kill in that window still
        # leaves the marker. If the CLI never starts, clear it in the handlers
        # below so a failed spawn does not look like a live Funnel.
        if provider.reset is not None:
            provider.reset.mark_armed()
        try:
            from plugin.framework.worker_pool import AsyncProcess

            # Some CLIs (cloudflared) print the URL on stderr more often than stdout.
            env = os.environ.copy()
            env.update(provider.extra_env(effect.provider_token))
            proc = AsyncProcess(cmd, stdout_cb=_on_line, stderr_cb=_on_line, on_exit_cb=_on_exit, env=env)
            proc_ref = proc
            self._process = proc
            proc.start()
        except Exception as e:
            from plugin.framework.errors import ToolExecutionError
            self._process = None
            if provider.reset is not None:
                provider.reset.clear_armed()
            binary = provider.version_args[0]
            if isinstance(e, FileNotFoundError) or (isinstance(e, ToolExecutionError) and isinstance(e.__cause__, FileNotFoundError)):
                log.exception("%s binary not found", binary)
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1, "auth_error": "%s binary not found on PATH" % binary}))
            else:
                log.exception("Failed to start MCP tunnel (%s)", provider_name)
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1, "auth_error": "failed to start %s tunnel" % provider_name}))

    def _apply_effects_unlocked(self, effects: list[Any], previous_status: TunnelStatus) -> StartProcessEffect | None:
        pending = None
        for effect in effects:
            if isinstance(effect, CancelRetryTimerEffect):
                if self._reconnect_timer is not None:
                    # Timer.cancel only sets an Event. It does not raise.
                    self._reconnect_timer.cancel()
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
                provider_obj = self._providers[effect.provider]
                if provider_obj.reset is not None:
                    provider_obj.reset.invalidate()
                    pending = effect
                    continue

                self._spawn_process_unlocked(effect)

            elif isinstance(effect, ScheduleRetryTimerEffect):
                if self._reconnect_timer is not None:
                    # Timer.cancel only sets an Event. It does not raise.
                    self._reconnect_timer.cancel()
                log.info("Scheduling MCP tunnel reconnect in %.1fs (attempt %s/%s)", effect.delay_seconds, effect.attempt, effect.max_retries)
                session = self._session
                timer = threading.Timer(effect.delay_seconds, lambda: None)

                def _fire(t: threading.Timer = timer, s: object = session) -> None:
                    self._on_retry_timer_expired(t, s)

                timer.function = _fire
                timer.daemon = True
                self._reconnect_timer = timer
                timer.start()

            elif isinstance(effect, NotifyUrlAcquiredEffect):
                # effect.url is the tunnel base, also stored on state.public_url.
                # Snippet and toast need the /mcp endpoint from mcp_public_url().
                try:
                    from plugin.mcp.mcp_ui import notify_tunnel_url_acquired

                    mcp_url = self.mcp_public_url()
                    if mcp_url:
                        notify_tunnel_url_acquired(effect.provider, mcp_url)
                except Exception:
                    # The public URL is already on tunnel state. A failed toast
                    # or snippet refresh must not abort the transition, and it
                    # must not be silent.
                    log.exception("Failed to notify MCP tunnel URL for %s", effect.provider)
        return pending

    def _on_retry_timer_expired(self, timer: Optional[threading.Timer], session: object) -> None:
        reset_token = None
        reset = None
        effect = None
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
            if session is not self._session:
                log.info("Ignoring MCP tunnel retry during provider binary check")
                self._reconnect_timer = None
                return
            self._reconnect_timer = None
            effect = self._dispatch_unlocked(TunnelEvent(TunnelEventKind.RETRY_TIMER_EXPIRED))
            session_snap = self._session
            if effect is not None:
                provider_obj = self._providers[effect.provider]
                reset = provider_obj.reset
                if reset is not None:
                    reset_token = reset.capture()
        # pre_start (Tailscale reset) must not run while this thread holds
        # _lock. stop() has to be able to take the lock during that CLI.
        if effect is not None and reset is not None:
            self._finish_pending_launch(effect, session_snap, reset, reset_token)

    def note_pending_start(self, token: object) -> None:
        """Arm *token* as the only start that may proceed until stop() or a newer arm."""
        with self._lock:
            self._pending_start = token

    def start(self, port: int, provider: str = DEFAULT_PROVIDER, provider_token: str = "", max_retries: int = DEFAULT_MAX_RETRIES, start_token: object | None = None) -> bool:
        """Start (or keep) a tunnel to *port*. Returns False if start failed."""
        from plugin.framework.queue_executor import Policy, main

        if main.policy is Policy.test_inline:
            return True

        provider = (provider or DEFAULT_PROVIDER).strip().lower()
        token = (provider_token or "").strip()
        provider_obj = self._providers.get(provider)
        if provider_obj is None:
            log.error("Unknown tunnel provider: %s", provider)
            with self._lock:
                if start_token is not None and self._pending_start is not start_token:
                    log.info("Ignoring superseded MCP tunnel start (%s)", provider)
                    return False
                self._replace_session()
                self._pending_start = None
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
                self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error="unknown tunnel provider: %s" % provider, desired_running=False)
            self._retire_snippet_provider(provider)
            return False

        with self._lock:
            if start_token is not None and self._pending_start is not start_token:
                log.info("Ignoring superseded MCP tunnel start (%s)", provider)
                return False
            session = self._replace_session()
            if self.is_running and self._state.port == int(port) and self._state.provider == provider and self._state.provider_token == token:
                log.info("Tunnel already running (%s) at %s", provider, self.public_url)
                if self.public_url:
                    self._state = dataclasses.replace(self._state, last_error=None)
                return True

        # binary_available() is a subprocess with a 10s timeout. Holding
        # _lock across it freezes LibreOffice: _sync_tunnel() runs on the UI
        # thread on config:changed, and stop()/retry cannot take the lock
        # either. Probe with the lock released.
        try:
            probe_error = binary_available(provider)
        except BaseException:
            with self._lock:
                if self._session is session:
                    if self._state.status == TunnelStatus.RECONNECTING and self._reconnect_timer is None:
                        self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
            raise

        reset_token = None
        effect = None
        with self._lock:
            if start_token is not None and self._pending_start is not start_token:
                log.info("Ignoring superseded MCP tunnel start (%s)", provider)
                return False
            if self._session is not session:
                log.info("Ignoring stale MCP tunnel start (%s)", provider)
                return False
            if probe_error:
                # The probe used to return only a bool, so every failure
                # (timeout, permission, exec error) was stored as "binary not
                # found on PATH". start() now stores the probe string so MCP
                # Server Status matches the failure.
                self._dispatch_unlocked(TunnelEvent(TunnelEventKind.STOP_REQUESTED))
                self._state = dataclasses.replace(self._state, status=TunnelStatus.FAILED, last_error=probe_error, desired_running=False)
                self._retire_snippet_provider(provider)
                return False

            effect = self._dispatch_unlocked(TunnelEvent(TunnelEventKind.START_REQUESTED, {"port": int(port), "provider": provider, "provider_token": token, "max_retries": max_retries}))
            reset = provider_obj.reset
            reset_token = None
            if effect is not None and reset is not None:
                reset_token = reset.capture()
        # Tailscale pre_start is the same reset as post_stop (~10s). Run it
        # outside _lock so stop() is not stuck behind it, then spawn only if
        # that stop (or a newer start) did not win.
        if effect is not None and reset is not None:
            self._finish_pending_launch(effect, session, reset, reset_token)
        with self._lock:
            if self._session is not session:
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
            self._replace_session()
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
            # process resets Tailscale and clears it. The count blocks
            # another idle reset until the scheduled body finishes. A failed
            # or stale body leaves the marker, and the next idle stop() after
            # the count returns to 0 tries again. A settings save on an
            # already-stopped tunnel with no marker still does not reset.
            if idle:
                for provider in self._providers.values():
                    if provider.reset is not None and provider.reset.idle_ready():
                        self._schedule_post_stop_unlocked(provider.name, self._state.port)

    def stop_for_quit(self) -> None:
        """Stop the tunnel and run Tailscale funnel off on this thread (LibreOffice exit)."""
        self._quit_shutdown = True
        try:
            self.stop()
        finally:
            self._quit_shutdown = False


def tunnel_status_line(
    pname: str,
    tunnel: TunnelManager | None,
    public_url: str | None,
    tunnel_enabled: bool,
) -> str | None:
    """One Status/toast line for public tunnel state, or None if tunnel off."""
    from plugin.framework.i18n import _

    if not tunnel_enabled:
        return None
    if public_url:
        return _("Public tunnel via {0} (no auth)").format(pname)
    if tunnel and getattr(tunnel, "is_reconnecting", False):
        return _("Public tunnel via {0}: {1}").format(pname, tunnel.last_error or _("reconnecting…"))
    err = tunnel.last_error if tunnel else None
    if err:
        return _("Public tunnel via {0} failed: {1}").format(pname, err)
    if tunnel and tunnel.is_running:
        return _("Public tunnel via {0} starting…").format(pname)
    return _("Public tunnel via {0} not running (is the provider binary installed?)").format(pname)


def _health_probe_ok(url: str, timeout: float) -> bool:
    """True when *url* answers HTTP 200; probe failures are logged at debug and are False."""
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WriterAgent-Probe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode() == 200
    except Exception:
        log.debug("MCP tunnel health probe failed for %s", url, exc_info=True)
        return False


def test_tunnel_connectivity(provider: str = DEFAULT_PROVIDER, port: int = 18765) -> tuple[bool, str, Optional[str]]:
    """Checks that the provider binary runs, whether local MCP /health answers, and whether a tunnel is already running.

    Does not validate provider credentials and does not start a tunnel.
    Returns (ok, user_facing_message, public_url).
    """
    from plugin.framework.i18n import _
    from plugin.framework.queue_executor import Policy, main

    if main.policy is Policy.test_inline:
        sim_url = f"https://simulated-{provider}.example.com/mcp"
        return True, _("Tunnel test mode: {0} provider simulated successfully.").format(provider), sim_url

    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    cls = _PROVIDER_BY_NAME.get(provider)
    if not cls:
        return False, _("Unknown tunnel provider: {0}").format(provider), None

    pname = provider_label(provider)
    binary = cls.version_args[0]

    try:
        res = subprocess.run(cls.version_args, capture_output=True, text=True, timeout=5, **get_subprocess_creationflags())
        version_str = (res.stdout or res.stderr or "").strip().splitlines()[0] if (res.stdout or res.stderr) else ""
    except FileNotFoundError:
        return False, _("Binary '{0}' for {1} not found on PATH.\n\nInstall from: {2}").format(binary, pname, cls.install_url), None
    except Exception as exc:
        return False, _("Failed to execute {0} binary ({1}): {2}").format(pname, binary, exc), None

    local_running = _health_probe_ok(f"http://localhost:{port}/health", 1.5)

    from plugin.mcp import current_tunnel

    tunnel = current_tunnel()
    if tunnel and tunnel.is_running:
        active_p = getattr(tunnel, "provider", None) or DEFAULT_PROVIDER
        active_url = tunnel.mcp_public_url()
        if active_url:
            base_url = normalize_public_base(tunnel.public_url or "")
            probe_ok = _health_probe_ok(f"{base_url}/health", 2.0)

            if active_p == provider:
                if probe_ok:
                    return True, _("{0} tunnel is running and responsive!\n\nPublic endpoint:\n{1}\n\nHealth check: OK (200)").format(pname, active_url), active_url
                return True, _("{0} tunnel is active!\n\nPublic endpoint:\n{1}\n\n(Public URL acquired from active tunnel session.)").format(pname, active_url), active_url
            else:
                return True, _("{0} binary '{1}' is verified ({2}).\n\n(Note: An active {3} tunnel is currently running at {4}.)").format(pname, binary, version_str or "OK", provider_label(active_p), active_url), None

    if local_running:
        return True, _("{0} binary '{1}' is installed and verified ({2}).\n\nMCP server is running locally on port {3}.\nCheck 'Expose via public tunnel' and click OK to activate public routing.").format(pname, binary, version_str or "OK", port), None

    return True, _("{0} binary '{1}' is installed and verified ({2}).\n\nNote: MCP server is not currently running on port {3}.").format(pname, binary, version_str or "OK", port), None
