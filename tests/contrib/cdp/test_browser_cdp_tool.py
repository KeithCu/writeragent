# Automated unit tests for browser CDP functionality in WriterAgent

import json
from unittest.mock import MagicMock, patch
import pytest

from plugin.contrib.cdp.browser_cdp_tool import (
    get_local_chrome_cdp_url,
    cleanup_local_chrome,
    browser_cdp,
    tool_error,
)
from plugin.chatbot.web_research import VisitWebpageCdpTool


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
    mock_popen.return_value = mock_proc

    mock_ctx = MagicMock()
    ws_url = get_local_chrome_cdp_url(mock_ctx)

    assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "--remote-debugging-port=9222" in args
    assert "--no-first-run" in args

    # Test cleanup terminates process
    cleanup_local_chrome()
    mock_proc.terminate.assert_called_once()


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
    mock_popen.return_value = mock_proc

    mock_ctx = MagicMock()
    ws_url = get_local_chrome_cdp_url(mock_ctx, browser_type="firefox")

    assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "--remote-debugging-port=9222" in args
    assert "--profile" in args
    assert "--no-first-run" in args

    # Test cleanup terminates process
    cleanup_local_chrome()
    mock_proc.terminate.assert_called_once()


@patch("plugin.contrib.cdp.browser_cdp_tool.browser_cdp")
def test_visit_webpage_cdp_tool_forward(mock_browser_cdp):
    # Mock CDP calls inside VisitWebpageCdpTool
    # First: Target.getTargets
    # Second: Page.navigate
    # Third: Runtime.evaluate
    mock_browser_cdp.side_effect = [
        json.dumps({
            "success": True,
            "result": {
                "targetInfos": [
                    {"type": "page", "targetId": "page-123"}
                ]
            }
        }),
        json.dumps({"success": True}),
        json.dumps({
            "success": True,
            "result": {
                "result": {
                    "type": "string",
                    "value": "This is page text content retrieved via CDP."
                }
            }
        })
    ]

    tool = VisitWebpageCdpTool(cdp_url="ws://dummy")
    result = tool.forward("https://example.com")

    assert result == "This is page text content retrieved via CDP."
    assert mock_browser_cdp.call_count == 3
