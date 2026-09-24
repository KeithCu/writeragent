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


