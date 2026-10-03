# WriterAgent - MCP UI Unit Tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
from unittest.mock import MagicMock, patch

from plugin.mcp.mcp_ui import (
    CopyMcpConfigListener,
    build_mcp_config_snippet,
    clear_active_settings_dialog,
    set_active_settings_dialog,
)


class TestMcpUi:
    @patch("plugin.mcp.mcp_ui.get_config_int", return_value=18765)
    def test_build_mcp_config_snippet_default(self, mock_port):
        snippet = build_mcp_config_snippet()
        parsed = json.loads(snippet)
        assert (parsed["mcpServers"]["libreoffice"]["url"]) == ("http://localhost:18765/mcp")

    def test_build_mcp_config_snippet_custom(self):
        snippet = build_mcp_config_snippet(url="https://custom.trycloudflare.com/mcp")
        parsed = json.loads(snippet)
        assert (parsed["mcpServers"]["libreoffice"]["url"]) == ("https://custom.trycloudflare.com/mcp")

    @patch("plugin.mcp.mcp_ui.copy_to_clipboard", return_value=True)
    def test_copy_mcp_config_listener(self, mock_copy):
        ctx = MagicMock()
        dlg = MagicMock()
        snippet_ctrl = MagicMock()
        snippet_ctrl.getText.return_value = '{"test": 1}'
        btn_ctrl = MagicMock()
        btn_model = MagicMock()
        btn_ctrl.getModel.return_value = btn_model

        def optional_mock(d, name):
            if name == "mcp__client_config_snippet":
                return snippet_ctrl
            if name == "mcp__copy_config":
                return btn_ctrl
            return None

        with patch("plugin.mcp.mcp_ui.get_optional", side_effect=optional_mock), patch(
            "plugin.mcp.mcp_ui.get_control_text", return_value='{"test": 1}'
        ):
            listener = CopyMcpConfigListener(ctx, dlg)
            listener.on_action_performed(MagicMock())

        mock_copy.assert_called_once_with(ctx, '{"test": 1}')
        assert (btn_model.Label) == ("✓ Copied!")

    def test_active_settings_dialog_tracking(self):
        dlg = MagicMock()
        set_active_settings_dialog(dlg)
        from plugin.mcp import mcp_ui

        assert (mcp_ui._active_settings_dialog_ref) is (dlg)

        clear_active_settings_dialog(dlg)
        assert (mcp_ui._active_settings_dialog_ref) is None


def _snippet_dialog(provider: str = "cloudflare", port: int = 18765):
    snippet = MagicMock()
    port_ctrl = MagicMock()
    port_ctrl.getValue.return_value = port
    checkbox = MagicMock()
    checkbox.getState.return_value = 1
    provider_ctrl = MagicMock()
    provider_ctrl.getText.return_value = provider
    controls = {
        "mcp__client_config_snippet": snippet,
        "mcp__mcp_port": port_ctrl,
        "mcp__tunnel_enabled": checkbox,
        "mcp__tunnel_provider": provider_ctrl,
    }
    dlg = MagicMock()
    dlg.getControl.side_effect = lambda name: controls.get(name)
    return dlg, snippet


def _snippet_url(snippet: MagicMock) -> str:
    return json.loads(snippet.setText.call_args[0][0])["mcpServers"]["libreoffice"]["url"]


def _stopped_tunnel(provider: str, last_error: str | None = None):
    """Match TunnelManager after stop / auth failure / missing binary.

    ``_provider`` is None once ``desired_running`` is false. ``state.provider``
    still names the provider that exited.
    """
    tunnel = MagicMock()
    tunnel.is_running = False
    tunnel._provider = None
    tunnel.state.provider = provider
    tunnel.last_error = last_error
    tunnel.mcp_public_url.return_value = None
    return tunnel


def test_sync_drops_cached_url_when_tunnel_stopped():
    from plugin.mcp import mcp_ui

    mcp_ui._tested_provider_tunnel_urls["cloudflare"] = "https://old.trycloudflare.com/mcp"
    dlg, snippet = _snippet_dialog()
    tunnel = _stopped_tunnel("cloudflare")
    try:
        with patch("plugin.mcp._shared_tunnel", tunnel):
            mcp_ui.sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"
        assert "cloudflare" not in mcp_ui._tested_provider_tunnel_urls
    finally:
        mcp_ui._tested_provider_tunnel_urls.clear()


def test_sync_drops_cached_url_on_auth_failure_and_missing_binary():
    from plugin.mcp import mcp_ui

    dlg, snippet = _snippet_dialog()
    cases = (
        "cloudflared authentication failed",
        "cloudflared binary not found on PATH",
    )
    try:
        for err in cases:
            mcp_ui._tested_provider_tunnel_urls["cloudflare"] = "https://old.trycloudflare.com/mcp"
            tunnel = _stopped_tunnel("cloudflare", last_error=err)
            with patch("plugin.mcp._shared_tunnel", tunnel):
                mcp_ui.sync_mcp_config_snippet(dlg)
            assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"
            assert "cloudflare" not in mcp_ui._tested_provider_tunnel_urls
    finally:
        mcp_ui._tested_provider_tunnel_urls.clear()


def test_sync_replaces_cached_url_when_quick_tunnel_rotates():
    from plugin.mcp import mcp_ui

    mcp_ui._tested_provider_tunnel_urls["cloudflare"] = "https://old.trycloudflare.com/mcp"
    dlg, snippet = _snippet_dialog()
    tunnel = MagicMock()
    tunnel.is_running = True
    tunnel._provider = "cloudflare"
    tunnel.state.provider = "cloudflare"
    tunnel.last_error = None
    tunnel.mcp_public_url.return_value = "https://new.trycloudflare.com/mcp"
    try:
        with patch("plugin.mcp._shared_tunnel", tunnel):
            mcp_ui.sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://new.trycloudflare.com/mcp"
        assert mcp_ui._tested_provider_tunnel_urls["cloudflare"] == "https://new.trycloudflare.com/mcp"
    finally:
        mcp_ui._tested_provider_tunnel_urls.clear()


def test_sync_drops_cached_url_while_new_process_has_no_url_yet():
    from plugin.mcp import mcp_ui

    mcp_ui._tested_provider_tunnel_urls.clear()
    mcp_ui._tested_provider_tunnel_urls["cloudflare"] = "https://old.trycloudflare.com/mcp"
    mcp_ui._mcp_snippet_refresh_scheduled = False
    dlg, snippet = _snippet_dialog()
    tunnel = MagicMock()
    tunnel.is_running = True
    tunnel._provider = "cloudflare"
    tunnel.state.provider = "cloudflare"
    tunnel.last_error = None
    tunnel.mcp_public_url.return_value = ""
    started: list[str] = []

    def run_bg(fn, name=None):
        del fn
        started.append(name or "")

    try:
        with patch("plugin.mcp._shared_tunnel", tunnel), patch("plugin.framework.worker_pool.run_in_background", side_effect=run_bg):
            mcp_ui.sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"
        assert "cloudflare" not in mcp_ui._tested_provider_tunnel_urls
        assert started == ["mcp-snippet-refresh"]
    finally:
        mcp_ui._tested_provider_tunnel_urls.clear()
        mcp_ui._mcp_snippet_refresh_scheduled = False


def test_sync_keeps_cached_url_when_no_shared_tunnel_tracks_provider():
    """Dropdown memory: another provider's last test is not a live tunnel."""
    from plugin.mcp import mcp_ui

    mcp_ui._tested_provider_tunnel_urls["cloudflare"] = "https://kept.trycloudflare.com/mcp"
    dlg, snippet = _snippet_dialog("cloudflare")
    try:
        with patch("plugin.mcp._shared_tunnel", None):
            mcp_ui.sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://kept.trycloudflare.com/mcp"
    finally:
        mcp_ui._tested_provider_tunnel_urls.clear()


