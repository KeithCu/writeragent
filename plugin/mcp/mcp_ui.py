# WriterAgent - MCP UI / Settings Dialog Integration
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP UI components: client configuration snippet generators and tunnel test listeners.

Concurrency:
Tunnel line, exit, and retry threads and the UI thread share
``_tested_provider_tunnel_urls`` and ``_retired_provider_tunnel_urls``.
``_active_settings_dialog_lock`` covers those two URL caches plus the dialog
pointer. ``_refresh_active_snippet`` runs only after that lock is released
because inline dispatch on the main thread re-acquires the lock.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any

from com.sun.star.awt import XItemListener, XTextListener

if TYPE_CHECKING:
    from com.sun.star.awt import ItemEvent, TextEvent

from plugin.framework.config import get_config_int_safe
from plugin.framework.errors import suppress_disposed
from plugin.framework.i18n import _
from plugin.framework.uno_listeners import BaseActionListener, BaseListener
from plugin.chatbot.dialogs import copy_to_clipboard, get_checkbox_state, get_control_text, get_optional, set_checkbox_state, set_control_text

log = logging.getLogger("writeragent.mcp.ui")

_active_settings_dialog_ref: Any = None
# The lock covers the dialog pointer and both URL caches. The posted refresh
# runs after the lock is released because post can invoke it inline and that
# path takes the lock again.
_active_settings_dialog_lock = threading.Lock()
_tested_provider_tunnel_urls: dict[str, str] = {}
# Providers whose cached public URL must not be shown. Set when the tunnel
# stops, fails, or drops the URL; cleared by a fresh Test or a new connect.
_retired_provider_tunnel_urls: set[str] = set()

_PROVIDER_DEFAULT_URLS = {"cloudflare": "https://<subdomain>.trycloudflare.com/mcp", "bore": "http://bore.pub:<remote-port>/mcp", "ngrok": "https://<domain>.ngrok-free.app/mcp", "tailscale": "https://<machine>.<tailnet>.ts.net/mcp"}


def set_active_settings_dialog(dlg: Any) -> None:
    """Track active settings dialog reference for tunnel updates."""
    global _active_settings_dialog_ref
    with _active_settings_dialog_lock:
        _active_settings_dialog_ref = dlg


def clear_active_settings_dialog(dlg: Any) -> None:
    """Clear active settings dialog reference if it matches dlg."""
    global _active_settings_dialog_ref
    with _active_settings_dialog_lock:
        if _active_settings_dialog_ref is dlg:
            _active_settings_dialog_ref = None


def _current_settings_dialog() -> Any:
    with _active_settings_dialog_lock:
        return _active_settings_dialog_ref


def build_mcp_config_snippet(port: int | None = None, url: str | None = None) -> str:
    """Return suggested MCP client JSON configuration for Claude Desktop / Cursor."""
    if not url:
        if port is None:
            port = get_config_int_safe("mcp.mcp_port")
        url = f"http://localhost:{port}/mcp"

    return json.dumps({"mcpServers": {"libreoffice": {"url": url}}}, indent=2)


def _read_mcp_port(dlg: Any) -> int | None:
    """Port from the Settings field, or None when it is missing or not a number."""
    port_ctrl = get_optional(dlg, "mcp__mcp_port")
    if not port_ctrl:
        return None
    if hasattr(port_ctrl, "getValue"):
        try:
            return int(port_ctrl.getValue())
        except (TypeError, ValueError):
            log.debug("mcp snippet: port value parse failed", exc_info=True)
    if hasattr(port_ctrl, "getText"):
        try:
            return int(str(port_ctrl.getText() or "").strip())
        except (TypeError, ValueError):
            log.debug("mcp snippet: port text parse failed", exc_info=True)
    return None


def _read_tunnel_provider(dlg: Any) -> str:
    """Selected tunnel provider, defaulting to cloudflare."""
    provider_ctrl = get_optional(dlg, "mcp__tunnel_provider")
    provider = str(get_control_text(provider_ctrl) or "").strip().lower() if provider_ctrl else ""
    return provider or "cloudflare"


class CopyMcpConfigListener(BaseActionListener):
    """Settings → MCP: copy client JSON configuration snippet to clipboard."""

    _ctx: Any
    _dlg: Any

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        # Sync first so the copied text matches the current fields.
        sync_mcp_config_snippet(self._dlg)
        snippet_ctrl = get_optional(self._dlg, "mcp__client_config_snippet")
        text = get_control_text(snippet_ctrl) if snippet_ctrl else ""
        if not text:
            text = build_mcp_config_snippet(port=_read_mcp_port(self._dlg))
        if copy_to_clipboard(self._ctx, text):
            copy_btn = get_optional(self._dlg, "mcp__copy_config")
            if copy_btn:
                copy_btn.getModel().Label = _("✓ Copied!")


def _refresh_active_snippet() -> None:
    """Push the current snippet into the open Settings dialog, if any."""
    from plugin.framework.queue_executor import post_to_main_thread

    def _apply() -> None:
        # The tunnel worker must not close over the dialog. The UI thread can
        # clear it before this lambda runs; QueueExecutor then swallows the
        # disposed-dialog error and the update is dropped. Re-read under the
        # same lock on the UI thread. If clear won, there is nothing to update.
        dlg = _current_settings_dialog()
        if dlg is None:
            return
        sync_mcp_config_snippet(dlg)

    post_to_main_thread(_apply)


def remember_tested_tunnel_url(provider: str, url: str) -> None:
    """Store a public URL from a successful Test or a live connect."""
    p = provider.strip().lower()
    if not p or not url:
        return
    with _active_settings_dialog_lock:
        _retired_provider_tunnel_urls.discard(p)
        _tested_provider_tunnel_urls[p] = url


def clear_tested_provider_tunnel_url(provider: str | None = None) -> None:
    """Drop cached public URLs that are no longer a live tunnel.

    ``sync_mcp_config_snippet`` kept copying
    ``_tested_provider_tunnel_urls`` after the tunnel stopped, failed, or
    lost its public URL. Settings copy and the client snippet still showed
    that dead URL. Retiring the provider makes the next sync use the local
    URL or the provider template. ``remember_tested_tunnel_url`` (Test or a
    new connect) is what puts a URL back.
    """
    with _active_settings_dialog_lock:
        if provider is None:
            for name in list(_tested_provider_tunnel_urls):
                _retired_provider_tunnel_urls.add(name)
            _tested_provider_tunnel_urls.clear()
        else:
            p = provider.strip().lower()
            if not p:
                return
            _tested_provider_tunnel_urls.pop(p, None)
            _retired_provider_tunnel_urls.add(p)
    # The refresh takes this lock, and post_to_main_thread may run it before post returns.
    _refresh_active_snippet()


def notify_tunnel_url_acquired(provider: str, url: str) -> None:
    """Record acquired tunnel URL and update active Settings dialog if open."""
    remember_tested_tunnel_url(provider, url)
    _refresh_active_snippet()


def sync_mcp_config_snippet(
    dlg: Any,
    custom_tunnel_url: str | None = None,
    custom_provider: str | None = None,
) -> None:
    """Synchronize MCP client config snippet according to port, tunnel_enabled, and provider."""
    if not dlg:
        return
    snippet_ctrl = get_optional(dlg, "mcp__client_config_snippet")
    if not snippet_ctrl:
        return

    port_val = _read_mcp_port(dlg)

    tunnel_enabled_ctrl = get_optional(dlg, "mcp__tunnel_enabled")
    is_tunnel_enabled = get_checkbox_state(tunnel_enabled_ctrl) if tunnel_enabled_ctrl else False

    if not is_tunnel_enabled:
        # Default local case: always revert to http://localhost:<port>/mcp
        set_control_text(snippet_ctrl, build_mcp_config_snippet(port=port_val))
        return

    selected_provider = _read_tunnel_provider(dlg)

    if custom_tunnel_url and custom_provider:
        remember_tested_tunnel_url(custom_provider, custom_tunnel_url)
    elif custom_tunnel_url:
        remember_tested_tunnel_url(selected_provider, custom_tunnel_url)

    # A retired entry is a URL from a tunnel that has since stopped, failed,
    # or dropped its public address. Do not copy it into the snippet. The
    # membership test and the pop share the lock so a connect cannot store a
    # URL in the gap the pop would then delete.
    with _active_settings_dialog_lock:
        if selected_provider in _retired_provider_tunnel_urls:
            _tested_provider_tunnel_urls.pop(selected_provider, None)
        active_url = _tested_provider_tunnel_urls.get(selected_provider)
    if not active_url:
        from plugin.mcp import _shared_tunnel

        if _shared_tunnel and _shared_tunnel.is_running and getattr(_shared_tunnel, "provider", None) == selected_provider:
            active_url = _shared_tunnel.mcp_public_url()
            # No URL yet: write the template now; notify_tunnel_url_acquired refreshes it later.
            if active_url:
                remember_tested_tunnel_url(selected_provider, active_url)

    if not active_url:
        # Fall back to provider default template
        active_url = _PROVIDER_DEFAULT_URLS.get(selected_provider, f"http://localhost:{port_val or 18765}/mcp")

    set_control_text(snippet_ctrl, build_mcp_config_snippet(port=port_val, url=active_url))


class McpTunnelEnabledListener(BaseListener, XItemListener):
    """Update MCP client config snippet when tunnel_enabled checkbox is toggled."""

    _dlg: Any

    def __init__(self, dlg: Any) -> None:
        self._dlg = dlg

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        sync_mcp_config_snippet(self._dlg)


class McpTunnelProviderListener(BaseListener, XItemListener, XTextListener):
    """Update MCP client config snippet when tunnel provider dropdown is changed."""

    _dlg: Any

    def __init__(self, dlg: Any) -> None:
        self._dlg = dlg

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        sync_mcp_config_snippet(self._dlg)

    def textChanged(self, rEvent: TextEvent) -> None:
        sync_mcp_config_snippet(self._dlg)


class McpPortTextListener(BaseListener, XTextListener):
    """Update MCP client config snippet when MCP port is edited."""

    _dlg: Any

    def __init__(self, dlg: Any) -> None:
        self._dlg = dlg

    def textChanged(self, rEvent: TextEvent) -> None:
        sync_mcp_config_snippet(self._dlg)


class TestTunnelListener(BaseActionListener):
    """Settings → MCP: test public tunnel connectivity / provider availability."""

    __test__: bool = False
    _ctx: Any
    _dlg: Any

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.chatbot.dialogs import msgbox
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.worker_pool import run_in_background
        from plugin.mcp.tunnel import test_tunnel_connectivity

        provider = _read_tunnel_provider(self._dlg)
        port = _read_mcp_port(self._dlg)
        if port is None:
            port = 18765

        btn = get_optional(self._dlg, "mcp__test_tunnel")
        if btn:
            btn.getModel().Label = _("Testing…")
            btn.getModel().Enabled = False

        def _worker() -> None:
            # A probe exception must still reach _apply, or the button stays on "Testing…".
            try:
                _ok, msg, pub_url = test_tunnel_connectivity(provider=provider, port=port)
            except Exception as exc:
                log.exception("MCP tunnel test failed for %s", provider)
                msg, pub_url = str(exc), None

            def _apply() -> None:
                if _current_settings_dialog() is not self._dlg:
                    return
                with suppress_disposed("mcp tunnel test result", logger=log):
                    if btn:
                        btn.getModel().Label = _("Test Tunnel")
                        btn.getModel().Enabled = True
                    if pub_url:
                        tunnel_enabled_ctrl = get_optional(self._dlg, "mcp__tunnel_enabled")
                        if tunnel_enabled_ctrl:
                            set_checkbox_state(tunnel_enabled_ctrl, True)
                        sync_mcp_config_snippet(self._dlg, custom_tunnel_url=pub_url, custom_provider=provider)
                    msgbox(self._ctx, _("MCP Tunnel Test"), msg)

            post_to_main_thread(_apply)

        run_in_background(_worker, name="mcp-tunnel-test", dedicated=True)
