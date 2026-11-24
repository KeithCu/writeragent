# Automated unit tests for browser CDP functionality in WriterAgent

import json
import ntpath
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import plugin.contrib.cdp.browser_cdp_tool as browser_cdp_tool
from plugin.contrib.cdp.browser_cdp_tool import (
    cleanup_local_chrome,
    find_browser_executable,
    get_local_chrome_cdp_url,
    tool_error,
)
from plugin.framework.managed_subprocess import _instances, shutdown_all
from plugin.chatbot.web_research import CdpPageDriver, VisitWebpageBrowserTool


def test_tool_error_formatting():
    err_str = tool_error("test message", details="some detail")
    parsed = json.loads(err_str)
    assert parsed["success"] is False
    assert parsed["error"] == "test message"
    assert parsed["details"] == "some detail"


@contextmanager
def _mock_cdp_spawn(which_side_effect=None):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id",
        }
    ]).encode("utf-8")

    mock_proc = MagicMock()
    mock_proc.pid = 4321
    mock_proc.poll.return_value = None
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    which_fn = which_side_effect or (lambda name: f"/usr/bin/{name}")
    with (
        patch("shutil.which", side_effect=which_fn),
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


def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


@pytest.mark.parametrize(
    "browser_type",
    ["chrome", "chromium", "brave"],
)
def test_browser_process_spawning(browser_type, tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    with _mock_cdp_spawn() as (mock_popen, mock_proc):
        ws_url = get_local_chrome_cdp_url(MagicMock(), browser_type=browser_type)
        assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "--remote-debugging-port=9222" in args
        assert "--no-first-run" in args
        assert "--no-default-browser-check" in args
        expected_profile = tmp_path / ".config" / "writeragent" / f"{browser_type}_profile"
        assert f"--user-data-dir={expected_profile}" in args

        cleanup_local_chrome()
        mock_proc.kill.assert_called_once()
        assert browser_cdp_tool._CHROME_BROWSER is None


@pytest.mark.parametrize("binary_name", ["brave", "brave-browser", "brave-browser-stable"])
def test_brave_linux_binary_lookup_and_profile_dir(binary_name, tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)

    def fake_which(name):
        if name == binary_name:
            return f"/usr/bin/{name}"
        return None

    with _mock_cdp_spawn(which_side_effect=fake_which) as (mock_popen, _mock_proc):
        ws_url = get_local_chrome_cdp_url(MagicMock(), browser_type="brave")
        assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
        args = mock_popen.call_args[0][0]
        assert args[0] == f"/usr/bin/{binary_name}"
        expected_profile = tmp_path / ".config" / "writeragent" / "brave_profile"
        assert f"--user-data-dir={expected_profile}" in args


def test_brave_windows_binary_lookup(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setattr(browser_cdp_tool.sys, "platform", "win32")
    monkeypatch.setattr(browser_cdp_tool.os.path, "expandvars", ntpath.expandvars)
    monkeypatch.setattr(browser_cdp_tool.os.path, "isabs", ntpath.isabs)

    win_brave = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

    with (
        patch("os.path.exists", side_effect=lambda p: p == win_brave),
        patch.object(browser_cdp_tool, "_win32_app_path", return_value=None),
        _mock_cdp_spawn(which_side_effect=lambda _name: None) as (mock_popen, _mock_proc),
    ):
        ws_url = get_local_chrome_cdp_url(MagicMock(), browser_type="brave")
        assert ws_url == "ws://127.0.0.1:9222/devtools/page/test-id"
        args = mock_popen.call_args[0][0]
        assert args[0] == win_brave
        expected_profile = tmp_path / ".config" / "writeragent" / "brave_profile"
        assert f"--user-data-dir={expected_profile}" in args


def test_cdp_browser_registered_and_shutdown_all_kills(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
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

    tool = VisitWebpageBrowserTool(CdpPageDriver())
    result = tool.forward("https://example.com")

    assert result == "This is page text content retrieved via CDP."
    methods = [call.args[0] for call in mock_browser_cdp.call_args_list]
    assert methods == ["Target.createTarget", "Page.navigate", "Runtime.evaluate", "Target.closeTarget"]


def test_find_browser_executable_expands_win32_paths_only_on_windows(monkeypatch):
    win_path = r"%ProgramFiles%\Mozilla Firefox\firefox.exe"
    expanded = r"C:\Program Files\Mozilla Firefox\firefox.exe"
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")

    with patch("shutil.which", return_value=None):
        monkeypatch.setattr(browser_cdp_tool.sys, "platform", "linux")
        with patch("os.path.exists", return_value=True) as mock_exists:
            assert find_browser_executable(["firefox"], "firefox", (win_path,)) == "firefox"
            mock_exists.assert_not_called()

        monkeypatch.setattr(browser_cdp_tool.sys, "platform", "win32")
        monkeypatch.setattr(browser_cdp_tool.os.path, "expandvars", ntpath.expandvars)
        monkeypatch.setattr(browser_cdp_tool.os.path, "isabs", ntpath.isabs)
        with (
            patch("os.path.exists", side_effect=lambda p: p == expanded),
            patch.object(browser_cdp_tool, "_win32_app_path", return_value=None),
        ):
            assert find_browser_executable(["firefox"], "firefox", (win_path,), win32_app_name="firefox.exe") == expanded


def test_win32_app_path_reads_registry_and_requires_existing_file(monkeypatch):
    import types

    win_exe = r"C:\Apps\Firefox\firefox.exe"
    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=1,
        HKEY_LOCAL_MACHINE=2,
        OpenKey=lambda root, _subkey: MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda *a: False,
        ) if root == 2 else (_ for _ in ()).throw(OSError("not found")),
        QueryValueEx=lambda _key, _name: (win_exe, 1),
    )
    monkeypatch.setattr(browser_cdp_tool.sys, "platform", "win32")
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    with patch("os.path.exists", side_effect=lambda p: p == win_exe):
        assert browser_cdp_tool._win32_app_path("firefox.exe") == win_exe
    with patch("os.path.exists", return_value=False):
        assert browser_cdp_tool._win32_app_path("firefox.exe") is None


def test_spawn_local_browser_refuses_when_previous_browser_is_alive():
    cleanup_local_chrome()
    mock_proc = MagicMock()
    mock_proc.pid = 4321
    mock_proc.poll.return_value = None
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    try:
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            browser_cdp_tool.spawn_local_browser(["chrome"], label="cdp-browser")
            with pytest.raises(RuntimeError, match="another local browser is already running"):
                browser_cdp_tool.spawn_local_browser(["firefox"], label="bidi-browser")
            mock_popen.assert_called_once()
    finally:
        cleanup_local_chrome()
