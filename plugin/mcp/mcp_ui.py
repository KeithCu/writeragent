# WriterAgent - MCP UI / Settings Dialog Integration
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP UI components: client configuration snippet generators and tunnel test listeners."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from com.sun.star.awt import XItemListener, XTextListener

if TYPE_CHECKING:
    from com.sun.star.awt import ItemEvent, TextEvent

from plugin.framework.config import get_config_int
from plugin.framework.i18n import _
from plugin.framework.uno_listeners import BaseActionListener, BaseListener
from plugin.chatbot.dialogs import (
    copy_to_clipboard,
    get_checkbox_state,
    get_control_text,
    get_optional,
    set_checkbox_state,
    set_control_text,
)

_active_settings_dialog_ref: Any = None
_tested_provider_tunnel_urls: dict[str, str] = {}

_PROVIDER_DEFAULT_URLS = {
    "cloudflare": "https://<subdomain>.trycloudflare.com/mcp",
    "bore": "http://bore.pub:<remote-port>/mcp",
    "ngrok": "https://<domain>.ngrok-free.app/mcp",
    "tailscale": "https://<machine-name>.tailscale.net/mcp",
}


def set_active_settings_dialog(dlg: Any) -> None:
    """Track active settings dialog reference for tunnel updates."""
    global _active_settings_dialog_ref
    _active_settings_dialog_ref = dlg


def clear_active_settings_dialog(dlg: Any) -> None:
    """Clear active settings dialog reference if it matches dlg."""
    global _active_settings_dialog_ref
    if _active_settings_dialog_ref is dlg:
        _active_settings_dialog_ref = None


def build_mcp_config_snippet(port: int | None = None, url: str | None = None) -> str:
    """Return suggested MCP client JSON configuration for Claude Desktop / Cursor."""
    if not url:
        if port is None:
            try:
                port = get_config_int("mcp.mcp_port")
            except Exception:
                port = 18765
        url = f"http://localhost:{port}/mcp"

    return json.dumps(
        {
            "mcpServers": {
                "libreoffice": {
                    "url": url,
                }
            }
        },
        indent=2,
    )


class CopyMcpConfigListener(BaseActionListener):
    """Settings → MCP: copy client JSON configuration snippet to clipboard."""

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        snippet_ctrl = get_optional(self._dlg, "mcp__client_config_snippet")
        text = get_control_text(snippet_ctrl) if snippet_ctrl else ""
        if not text:
            port_ctrl = get_optional(self._dlg, "mcp__mcp_port")
            port_val = None
            if port_ctrl and hasattr(port_ctrl, "getValue"):
                try:
                    port_val = int(port_ctrl.getValue())
                except Exception:
                    pass
            text = build_mcp_config_snippet(port=port_val)
        if copy_to_clipboard(self._ctx, text):
            copy_btn = get_optional(self._dlg, "mcp__copy_config")
            if copy_btn:
                try:
                    copy_btn.getModel().Label = _("✓ Copied!")
                except Exception:
                    pass


def notify_tunnel_url_acquired(provider: str, url: str) -> None:
    """Record acquired tunnel URL and update active Settings dialog if open."""
    p = provider.strip().lower()
    _tested_provider_tunnel_urls[p] = url
    dlg = _active_settings_dialog_ref
    if dlg is not None:
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(lambda: sync_mcp_config_snippet(dlg))


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

    port_ctrl = get_optional(dlg, "mcp__mcp_port")
    port_val = None
    if port_ctrl:
        if hasattr(port_ctrl, "getValue"):
            try:
                port_val = int(port_ctrl.getValue())
            except Exception:
                pass
        if port_val is None and hasattr(port_ctrl, "getText"):
            try:
                port_val = int(str(port_ctrl.getText() or "").strip())
            except Exception:
                pass

    tunnel_enabled_ctrl = get_optional(dlg, "mcp__tunnel_enabled")
    is_tunnel_enabled = get_checkbox_state(tunnel_enabled_ctrl) if tunnel_enabled_ctrl else False

    if not is_tunnel_enabled:
        # Default local case: always revert to http://localhost:<port>/mcp
        set_control_text(snippet_ctrl, build_mcp_config_snippet(port=port_val))
        return

    provider_ctrl = get_optional(dlg, "mcp__tunnel_provider")
    selected_provider = str(get_control_text(provider_ctrl) or "").strip().lower() if provider_ctrl else "cloudflare"
    if not selected_provider:
        selected_provider = "cloudflare"

    if custom_tunnel_url and custom_provider:
        _tested_provider_tunnel_urls[custom_provider.strip().lower()] = custom_tunnel_url
    elif custom_tunnel_url:
        _tested_provider_tunnel_urls[selected_provider] = custom_tunnel_url

    # Check if we have a tested URL for this specific selected provider
    active_url = _tested_provider_tunnel_urls.get(selected_provider)
    if not active_url:
        from plugin.mcp import _shared_tunnel

        if (
            _shared_tunnel
            and _shared_tunnel.is_running
            and getattr(_shared_tunnel, "_provider", None) == selected_provider
        ):
            active_url = _shared_tunnel.mcp_public_url()
            if not active_url:
                import time

                deadline = time.time() + 1.2
                while time.time() < deadline and not _shared_tunnel._public_url and _shared_tunnel.is_running:
                    time.sleep(0.1)
                active_url = _shared_tunnel.mcp_public_url()

            if active_url:
                _tested_provider_tunnel_urls[selected_provider] = active_url

    if not active_url:
        # Fall back to provider default template
        active_url = _PROVIDER_DEFAULT_URLS.get(
            selected_provider,
            f"http://localhost:{port_val or 18765}/mcp",
        )

    set_control_text(snippet_ctrl, build_mcp_config_snippet(port=port_val, url=active_url))


class McpTunnelEnabledListener(BaseListener, XItemListener):
    """Update MCP client config snippet when tunnel_enabled checkbox is toggled."""

    def __init__(self, dlg: Any) -> None:
        self._dlg = dlg

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        sync_mcp_config_snippet(self._dlg)


class McpTunnelProviderListener(BaseListener, XItemListener, XTextListener):
    """Update MCP client config snippet when tunnel provider dropdown is changed."""

    def __init__(self, dlg: Any) -> None:
        self._dlg = dlg

    def itemStateChanged(self, rEvent: ItemEvent) -> None:
        sync_mcp_config_snippet(self._dlg)

    def textChanged(self, rEvent: TextEvent) -> None:
        sync_mcp_config_snippet(self._dlg)


class McpPortTextListener(BaseListener, XTextListener):
    """Update MCP client config snippet when MCP port is edited."""

    def __init__(self, dlg: Any) -> None:
        self._dlg = dlg

    def textChanged(self, rEvent: TextEvent) -> None:
        sync_mcp_config_snippet(self._dlg)


class TestTunnelListener(BaseActionListener):
    """Settings → MCP: test public tunnel connectivity / provider availability."""

    __test__: bool = False

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.chatbot.dialogs import msgbox
        from plugin.framework.worker_pool import run_in_background
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.mcp.tunnel import test_tunnel_connectivity, DEFAULT_PROVIDER

        provider_ctrl = get_optional(self._dlg, "mcp__tunnel_provider")
        provider = str(get_control_text(provider_ctrl) or "").strip().lower() if provider_ctrl else DEFAULT_PROVIDER
        if not provider:
            provider = DEFAULT_PROVIDER

        token_ctrl = get_optional(self._dlg, "mcp__tunnel_provider_token")
        token = str(get_control_text(token_ctrl) or "").strip() if token_ctrl else ""

        port_ctrl = get_optional(self._dlg, "mcp__mcp_port")
        port = 18765
        if port_ctrl:
            if hasattr(port_ctrl, "getValue"):
                try:
                    port = int(port_ctrl.getValue())
                except Exception:
                    pass
            elif hasattr(port_ctrl, "getText"):
                try:
                    port = int(str(port_ctrl.getText() or "").strip())
                except Exception:
                    pass

        btn = get_optional(self._dlg, "mcp__test_tunnel")
        if btn:
            try:
                btn.getModel().Label = _("Testing…")
                btn.getModel().Enabled = False
            except Exception:
                pass

        def _worker() -> None:
            _ok, msg, pub_url = test_tunnel_connectivity(provider=provider, provider_token=token, port=port)

            def _apply() -> None:
                if btn:
                    try:
                        btn.getModel().Label = _("Test Tunnel")
                        btn.getModel().Enabled = True
                    except Exception:
                        pass
                if pub_url:
                    tunnel_enabled_ctrl = get_optional(self._dlg, "mcp__tunnel_enabled")
                    if tunnel_enabled_ctrl:
                        set_checkbox_state(tunnel_enabled_ctrl, True)
                    sync_mcp_config_snippet(self._dlg, custom_tunnel_url=pub_url, custom_provider=provider)
                msgbox(self._ctx, _("MCP Tunnel Test"), msg)

            post_to_main_thread(_apply)

        run_in_background(_worker)
