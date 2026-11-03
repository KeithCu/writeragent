# Automated unit tests for browser CDP functionality in WriterAgent

import json
from unittest.mock import MagicMock, patch

import plugin.contrib.cdp.browser_cdp_tool as browser_cdp_tool
from plugin.contrib.cdp.browser_cdp_tool import (
    get_local_chrome_cdp_url,
    cleanup_local_chrome,
    tool_error,
    _WS_AVAILABLE,
)
from plugin.framework.managed_subprocess import _instances, shutdown_all
from plugin.chatbot.web_research import VisitWebpageCdpTool


def test_websockets_vendored_for_cdp():
    """CDP requires websockets; shipped via requirements-vendor.txt → plugin/lib/."""
    assert _WS_AVAILABLE is True


def test_tool_error_formatting():
    err_str = tool_error("test message", details="some detail")
    parsed = json.loads(err_str)
    assert parsed["success"] is False
    assert parsed["error"] == "test message"
    assert parsed["details"] == "some detail"


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("urllib.request.urlopen")
def test_chrome_process_spawning(mock_urlopen, mock_popen, mock_run):
    # Mock urllib JSON response to simulate Chrome CDP responding on port 9222
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id"
        }
    ]).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response

    # Force starting process since it's not running initially
    mock_urlopen.side_effect = [
        Exception("Connection refused"),  # first try: not running
        mock_urlopen.return_value        # second try: running after spawn
    ]

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc

    mock_ctx = MagicMock()
    ws_url = get_local_chrome_cdp_url(mock_ctx)

    assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "--remote-debugging-port=9222" in args
    assert "--no-first-run" in args

    # Test cleanup uses the managed kill path (MagicMock is not a real Popen)
    cleanup_local_chrome()
    mock_proc.kill.assert_called_once()
    assert browser_cdp_tool._CHROME_BROWSER is None


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("urllib.request.urlopen")
def test_firefox_process_spawning(mock_urlopen, mock_popen, mock_run):
    # Mock urllib JSON response to simulate Firefox CDP responding on port 9222
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id"
        }
    ]).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response

    # Force starting process since it's not running initially
    mock_urlopen.side_effect = [
        Exception("Connection refused"),  # first try: not running
        mock_urlopen.return_value        # second try: running after spawn
    ]

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc

    mock_ctx = MagicMock()
    ws_url = get_local_chrome_cdp_url(mock_ctx, browser_type="firefox")

    assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "--remote-debugging-port=9222" in args
    assert "--profile" in args
    assert "--no-first-run" in args

    # Test cleanup uses the managed kill path (MagicMock is not a real Popen)
    cleanup_local_chrome()
    mock_proc.kill.assert_called_once()
    assert browser_cdp_tool._CHROME_BROWSER is None


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("urllib.request.urlopen")
def test_cdp_browser_registered_and_shutdown_all_kills(
    mock_urlopen, mock_popen, mock_run,
):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id",
        }
    ]).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response
    mock_urlopen.side_effect = [
        Exception("Connection refused"),
        mock_urlopen.return_value,
    ]

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc

    cleanup_local_chrome()
    before = set(_instances)

    get_local_chrome_cdp_url(MagicMock())

    managed = browser_cdp_tool._CHROME_BROWSER
    assert managed is not None
    assert managed in _instances
    assert len(_instances) == len(before) + 1

    shutdown_all()
    mock_proc.kill.assert_called()

    cleanup_local_chrome()
    assert browser_cdp_tool._CHROME_BROWSER is None
    assert managed not in _instances


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("urllib.request.urlopen")
def test_cleanup_local_chrome_kills_registered_browser(
    mock_urlopen, mock_popen, mock_run,
):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id",
        }
    ]).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_response
    mock_urlopen.side_effect = [
        Exception("Connection refused"),
        mock_urlopen.return_value,
    ]

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc

    cleanup_local_chrome()
    get_local_chrome_cdp_url(MagicMock())

    cleanup_local_chrome()
    mock_proc.kill.assert_called_once()
    assert browser_cdp_tool._CHROME_BROWSER is None


@patch("plugin.contrib.cdp.browser_cdp_tool.browser_cdp")
@patch("time.sleep", return_value=None)
def test_visit_webpage_cdp_tool_forward(mock_sleep, mock_browser_cdp):
    # Each visit creates its own target, navigates, reads the body, then closes it.
    # Reusing Target.getTargets shared the first tab across parallel workers.
    mock_browser_cdp.side_effect = [
        json.dumps({"success": True, "result": {"targetId": "page-123"}}),
        json.dumps({"success": True}),
        json.dumps({
            "success": True,
            "result": {
                "result": {
                    "type": "string",
                    "value": "This is page text content retrieved via CDP."
                }
            }
        }),
        json.dumps({"success": True}),
    ]

    tool = VisitWebpageCdpTool(cdp_url="ws://dummy")
    result = tool.forward("https://example.com")

    assert result == "This is page text content retrieved via CDP."
    methods = [call.args[0] for call in mock_browser_cdp.call_args_list]
    assert methods == ["Target.createTarget", "Page.navigate", "Runtime.evaluate", "Target.closeTarget"]
    assert mock_sleep.call_count == 12
    mock_sleep.assert_called_with(0.25)
