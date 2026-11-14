import datetime
import json
import urllib.request

from plugin.mcp.server import mcp_endpoint_url
from plugin.mcp.mcp_protocol import format_mcp_clock_context, build_initialize_instructions


def test_mcp_endpoint_url_helper():
    assert mcp_endpoint_url("localhost", 18765) == "http://localhost:18765/mcp"
    assert mcp_endpoint_url("127.0.0.1", 9000, use_ssl=True) == "https://127.0.0.1:9000/mcp"


def test_mcp_clock_context_is_offset_free_wall_time():
    """Clock stamp matches Calc write ISO (no +HH:MM / Z); weekday/tz name stay human-facing."""
    now = datetime.datetime(2026, 8, 6, 13, 11, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-4), "EDT"))
    text = format_mcp_clock_context(now)
    local = now.astimezone()
    wall = local.replace(tzinfo=None).isoformat(timespec="seconds")
    weekday = local.strftime("%A")
    assert text.startswith(f"Current local date and time: {weekday}, {wall}")
    assert "-04:00" not in text
    assert "+00:00" not in text
    assert not text.rstrip(".").endswith("Z")
    tzname = local.tzname()
    if tzname:
        assert f"({tzname})" in text


def test_initialize_instructions_clock_and_mode_hints():
    """Every exposure mode includes the offset-free clock stamp and a distinct mode hint."""
    now = datetime.datetime(2026, 8, 6, 13, 11, 0, tzinfo=datetime.timezone.utc)
    expected_wall = now.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    for mode in ("delegate", "direct_flat", "direct_discovery", "unknown"):
        text = build_initialize_instructions(mode, now=now)
        assert "Current local date and time:" in text
        assert expected_wall in text
        assert "+00:00" not in text
        assert "-04:00" not in text
        # Claude Desktop drops instructions; Claude Code truncates ~2KB.
        assert len(text) < 2000

    mode_texts = {
        build_initialize_instructions(mode, now=now)
        for mode in ("direct_flat", "direct_discovery", "delegate")
    }
    assert len(mode_texts) == 3


def test_health_endpoint(mcp_server):
    """Test the /health endpoint schema and status code."""
    url = f"{mcp_server}/health"
    req = urllib.request.Request(url, method="GET")
    
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        body = response.read().decode('utf-8')
        data = json.loads(body)

        assert "status" in data
        assert data["status"] == "healthy"
        assert "server" in data
        assert data["server"] == "WriterAgent"
        assert "version" in data


def test_mcp_tools(mcp_server):
    """Test the MCP JSON-RPC tools/list endpoint schema and status code."""
    url = f"{mcp_server}/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list"
    }

    data_bytes = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, method="POST", data=data_bytes)
    req.add_header('Content-Type', 'application/json')

    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        body = response.read().decode('utf-8')
        data = json.loads(body)

        assert data.get("jsonrpc") == "2.0"
        assert data.get("id") == 1
        assert "result" in data

        result = data["result"]
        assert "tools" in result
        assert isinstance(result["tools"], list)

        if len(result["tools"]) > 0:
            tool = result["tools"][0]
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool


def test_mcp_resources(mcp_server):
    """Test the MCP JSON-RPC resources/list endpoint schema and status code."""
    url = f"{mcp_server}/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "resources/list"
    }
    
    data_bytes = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, method="POST", data=data_bytes)
    req.add_header('Content-Type', 'application/json')
    
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        body = response.read().decode('utf-8')
        data = json.loads(body)

        assert data.get("jsonrpc") == "2.0"
        assert data.get("id") == 2
        assert "result" in data

        result = data["result"]
        assert "resources" in result
        assert isinstance(result["resources"], list)


def test_root_info_includes_mcp_endpoint(mcp_server):
    """GET / advertises the streamable-HTTP MCP endpoint when MCP routes are registered."""
    req = urllib.request.Request(f"{mcp_server}/", method="GET")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        data = json.loads(response.read().decode("utf-8"))
        assert data.get("mcp_endpoint") == f"{mcp_server}/mcp"


def test_mcp_initialize_returns_instructions_with_clock(mcp_server):
    """initialize.instructions returns a non-empty string with the connection-time clock."""
    url = f"{mcp_server}/mcp"
    payload = {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        data = json.loads(response.read().decode("utf-8"))

    instr = data["result"]["instructions"]
    assert isinstance(instr, str) and instr.strip()
    assert "Current local date and time:" in instr


def test_mcp_action_server_status_dialog_controls():
    from unittest.mock import MagicMock, patch
    from plugin.mcp import McpModule

    mod = McpModule()
    mock_server = MagicMock()
    mock_server.is_running.return_value = True
    mock_server.get_status.return_value = {"running": True, "mcp_url": "http://localhost:18765/mcp", "routes": 5}

    def fake_get(k):
        return {
            "mcp.tunnel_enabled": True,
            "mcp.tunnel_provider": "cloudflare",
        }.get(k)

    mock_tunnel = MagicMock()
    mock_tunnel.mcp_public_url.return_value = "https://test.trycloudflare.com/mcp"

    mod.server = mock_server
    mod.tunnel = mock_tunnel
    mock_services = MagicMock()
    mod._services = mock_services

    mock_dlg = MagicMock()
    mock_controls = {}
    for name in ("Msg", "LocalUrlLabel", "UrlField", "TunnelLabel", "TunnelUrlField", "TunnelStatusMsg", "CopyBtn", "OKBtn"):
        ctrl = MagicMock()
        ctrl.getModel.return_value = MagicMock()
        mock_controls[name] = ctrl

    mock_dlg.getControl.side_effect = lambda name: mock_controls.get(name)

    with (
        patch("plugin.mcp.get_config", side_effect=fake_get),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.chatbot.dialogs.load_writeragent_dialog", return_value=mock_dlg),
    ):
        mod._action_server_status()

    # Local URL field has local url
    mock_controls["UrlField"].setText.assert_called_with("http://localhost:18765/mcp")
    # Tunnel URL field has public url
    mock_controls["TunnelUrlField"].setText.assert_called_with("https://test.trycloudflare.com/mcp")
    assert mock_controls["TunnelUrlField"].getModel().Visible is True


def test_mcp_action_toggle_server_start_does_not_popup_modal_msgbox():
    """Toggling server start must not show modal msgbox (blocks sidebar and drops typed prompts)."""
    from unittest.mock import MagicMock, patch
    from plugin.mcp import McpModule

    mod = McpModule()
    mod._registry = MagicMock()
    mock_server = MagicMock()
    mock_server.is_running.return_value = True

    mock_services = MagicMock()
    mod._services = mock_services

    with (
        patch("plugin.mcp.get_config", return_value=True),
        patch("plugin.mcp.save"),
        patch("plugin.mcp.is_mcp_server_running", side_effect=[False, True]),
        patch.object(mod, "_start_server") as mock_start,
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.chatbot.dialogs.msgbox") as mock_msgbox,
    ):
        mod._action_toggle_server()

    mock_start.assert_called_once_with(mock_services)
    mock_msgbox.assert_not_called()


def test_mcp_action_toggle_server_stop_does_not_popup_modal_msgbox():
    """Toggling server stop must not show modal msgbox."""
    from unittest.mock import MagicMock, patch
    from plugin.mcp import McpModule

    mod = McpModule()
    mock_server = MagicMock()
    mock_server.is_running.return_value = True

    mock_services = MagicMock()
    mod._services = mock_services

    with (
        patch("plugin.mcp.is_mcp_server_running", return_value=True),
        patch.object(mod, "_stop_server") as mock_stop,
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.chatbot.dialogs.msgbox") as mock_msgbox,
    ):
        mod._action_toggle_server()

    mock_stop.assert_called_once()
    mock_msgbox.assert_not_called()


def test_mcp_action_toggle_server_failure_shows_failure_dialog():
    """Failure to start on toggle must still surface an error dialog."""
    from unittest.mock import MagicMock, patch
    from plugin.mcp import McpModule

    mod = McpModule()
    mod._registry = MagicMock()

    mock_services = MagicMock()
    mod._services = mock_services

    with (
        patch("plugin.mcp.get_config", return_value=True),
        patch("plugin.mcp.save"),
        patch("plugin.mcp.is_mcp_server_running", return_value=False),
        patch.object(mod, "_start_server"),
        patch.object(mod, "_show_start_failure_dialog") as mock_fail_dlg,
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
    ):
        mod._action_toggle_server()

    mock_fail_dlg.assert_called_once()



