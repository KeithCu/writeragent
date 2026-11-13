# Automated unit tests for browser CDP functionality in WriterAgent

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import plugin.contrib.cdp.browser_cdp_tool as browser_cdp_tool
from plugin.contrib.cdp.browser_cdp_tool import (
    cleanup_local_chrome,
    get_local_chrome_cdp_url,
    tool_error,
)
from plugin.framework.managed_subprocess import _instances, shutdown_all
from plugin.chatbot.web_research import VisitWebpageCdpTool


def test_tool_error_formatting():
    err_str = tool_error("test message", details="some detail")
    parsed = json.loads(err_str)
    assert parsed["success"] is False
    assert parsed["error"] == "test message"
    assert parsed["details"] == "some detail"


@contextmanager
def _mock_cdp_spawn():
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id",
        }
    ]).encode("utf-8")

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    with (
        patch("subprocess.run"),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        mock_urlopen.return_value.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            Exception("Connection refused"),
            mock_urlopen.return_value,
        ]
        try:
            yield mock_popen, mock_proc
        finally:
            cleanup_local_chrome()


@pytest.mark.parametrize(
    "browser_type,expected_flag",
    [
        ("chrome", None),
        ("firefox", "--profile"),
    ],
)
def test_browser_process_spawning(browser_type, expected_flag):
    with _mock_cdp_spawn() as (mock_popen, mock_proc):
        ws_url = get_local_chrome_cdp_url(MagicMock(), browser_type=browser_type)
        assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "--remote-debugging-port=9222" in args
        assert "--no-first-run" in args
        if expected_flag is not None:
            assert expected_flag in args

        cleanup_local_chrome()
        mock_proc.kill.assert_called_once()
        assert browser_cdp_tool._CHROME_BROWSER is None


def test_cdp_browser_registered_and_shutdown_all_kills():
    cleanup_local_chrome()
    before = set(_instances)
    with _mock_cdp_spawn() as (_mock_popen, mock_proc):
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
