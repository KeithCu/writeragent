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


def _snippet_dialog(provider: str = "cloudflare", port: int = 19000, tunnel_on: bool = True):
    snippet = MagicMock()
    port_ctrl = MagicMock()
    port_ctrl.getValue.return_value = port
    port_ctrl.getText.return_value = str(port)
    checkbox = MagicMock()
    checkbox.getState.return_value = 1 if tunnel_on else 0
    checkbox.State = checkbox.getState.return_value
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


def _reset_tunnel_url_cache() -> None:
    from plugin.mcp import mcp_ui

    mcp_ui._tested_provider_tunnel_urls.clear()
    mcp_ui._retired_provider_tunnel_urls.clear()
    mcp_ui._active_settings_dialog_ref = None


def test_sync_drops_retired_tunnel_url_and_accepts_a_fresh_test() -> None:
    """A stopped/failed provider must not keep its public URL in the snippet."""
    from plugin.mcp.mcp_ui import clear_tested_provider_tunnel_url, remember_tested_tunnel_url, sync_mcp_config_snippet

    _reset_tunnel_url_cache()
    try:
        dlg, snippet = _snippet_dialog()
        remember_tested_tunnel_url("cloudflare", "https://dead.trycloudflare.com/mcp")
        sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://dead.trycloudflare.com/mcp"

        clear_tested_provider_tunnel_url("cloudflare")
        sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"

        sync_mcp_config_snippet(dlg, custom_tunnel_url="https://fresh.trycloudflare.com/mcp", custom_provider="cloudflare")
        assert _snippet_url(snippet) == "https://fresh.trycloudflare.com/mcp"
    finally:
        _reset_tunnel_url_cache()


def test_tunnel_stop_and_failure_clear_snippet_cache(monkeypatch) -> None:
    """Stop, unexpected exit, and a failed start retire the cached public URL."""
    from plugin.mcp.mcp_ui import sync_mcp_config_snippet
    from plugin.mcp import mcp_ui
    from plugin.mcp.tunnel import TunnelManager

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    _reset_tunnel_url_cache()
    dlg, snippet = _snippet_dialog()
    exit_cb: dict[str, object] = {"fn": None}

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        del cmd, kwargs
        proc = MagicMock()
        proc.is_running = True
        exit_cb["fn"] = on_exit_cb

        def start() -> None:
            if stderr_cb:
                stderr_cb("INF |  https://live.trycloudflare.com")

        proc.start = start
        proc.terminate = MagicMock()
        return proc

    try:
        with (
            patch("plugin.mcp.tunnel.binary_available", return_value=True),
            patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
        ):
            mgr = TunnelManager()
            assert mgr.start(18765, "cloudflare") is True
            assert mcp_ui._tested_provider_tunnel_urls["cloudflare"] == "https://live.trycloudflare.com/mcp"
            sync_mcp_config_snippet(dlg)
            assert _snippet_url(snippet) == "https://live.trycloudflare.com/mcp"

            mgr.stop()
            assert "cloudflare" not in mcp_ui._tested_provider_tunnel_urls
            assert "cloudflare" in mcp_ui._retired_provider_tunnel_urls
            sync_mcp_config_snippet(dlg)
            assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"

            assert mgr.start(18765, "cloudflare") is True
            assert mcp_ui._tested_provider_tunnel_urls["cloudflare"] == "https://live.trycloudflare.com/mcp"
            exit_fn = exit_cb["fn"]
            assert callable(exit_fn)
            exit_fn(1)
            # Cancel the reconnect timer before it can publish the URL again.
            timer = mgr._reconnect_timer
            if timer is not None:
                timer.cancel()
            assert mgr.public_url is None
            assert "cloudflare" not in mcp_ui._tested_provider_tunnel_urls
            sync_mcp_config_snippet(dlg)
            assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"
            mgr.stop()

        with patch("plugin.mcp.tunnel.binary_available", return_value=False):
            remember = TunnelManager()
            mcp_ui.remember_tested_tunnel_url("cloudflare", "https://stale.trycloudflare.com/mcp")
            assert remember.start(18765, "cloudflare") is False
            assert "cloudflare" not in mcp_ui._tested_provider_tunnel_urls
            sync_mcp_config_snippet(dlg)
            assert _snippet_url(snippet) == "https://<subdomain>.trycloudflare.com/mcp"
    finally:
        _reset_tunnel_url_cache()


def test_idle_stop_keeps_a_fresh_test_url(monkeypatch) -> None:
    """stop() on a tunnel that never started must not wipe a Settings Test URL."""
    from plugin.mcp import mcp_ui
    from plugin.mcp.mcp_ui import sync_mcp_config_snippet
    from plugin.mcp.tunnel import TunnelManager

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    _reset_tunnel_url_cache()
    try:
        dlg, snippet = _snippet_dialog()
        sync_mcp_config_snippet(dlg, custom_tunnel_url="https://tested.trycloudflare.com/mcp", custom_provider="cloudflare")
        TunnelManager().stop()
        sync_mcp_config_snippet(dlg)
        assert _snippet_url(snippet) == "https://tested.trycloudflare.com/mcp"
        assert mcp_ui._tested_provider_tunnel_urls["cloudflare"] == "https://tested.trycloudflare.com/mcp"
    finally:
        _reset_tunnel_url_cache()


