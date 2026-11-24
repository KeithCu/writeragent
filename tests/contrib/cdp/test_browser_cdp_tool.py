# Automated unit tests for browser CDP functionality in WriterAgent

import json
import ntpath
from contextlib import contextmanager
from urllib.parse import urlsplit
from unittest.mock import MagicMock, patch

import pytest

import plugin.contrib.cdp.browser_cdp_tool as browser_cdp_tool
from plugin.contrib.cdp.browser_cdp_tool import (
    cleanup_local_browser,
    find_browser_executable,
    get_local_cdp_url,
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
    page_response = MagicMock()
    page_response.read.return_value = json.dumps([
        {
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test-id",
        }
    ]).encode("utf-8")

    mock_proc = MagicMock()
    mock_proc.pid = 4321
    mock_proc.poll.return_value = None
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    calls = 0

    def fake_urlopen(url, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Exception("Connection refused")
        ctx_mgr = MagicMock()
        if url.endswith("/json/version"):
            netloc = urlsplit(url).netloc
            version_response = MagicMock()
            version_response.read.return_value = json.dumps({
                "webSocketDebuggerUrl": f"ws://{netloc}/devtools/browser/test-id",
            }).encode("utf-8")
            ctx_mgr.__enter__.return_value = version_response
        else:
            ctx_mgr.__enter__.return_value = page_response
        return ctx_mgr

    which_fn = which_side_effect or (lambda name: f"/usr/bin/{name}")
    with (
        patch("shutil.which", side_effect=which_fn),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        try:
            yield mock_popen, mock_proc
        finally:
            cleanup_local_browser()


def _port_from_popen_args(args: list[str]) -> int:
    for arg in args:
        if arg.startswith("--remote-debugging-port="):
            return int(arg.split("=", 1)[1])
    raise AssertionError(f"missing --remote-debugging-port in {args}")


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
        ws_url = get_local_cdp_url(browser_type)
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        port = _port_from_popen_args(args)
        assert port != 0
        assert ws_url == f"ws://127.0.0.1:{port}/devtools/browser/test-id"
        assert "--no-first-run" in args
        assert "--no-default-browser-check" in args
        expected_profile = tmp_path / ".config" / "writeragent" / f"{browser_type}_profile"
        assert f"--user-data-dir={expected_profile}" in args

        cleanup_local_browser()
        mock_proc.kill.assert_called_once()
        assert browser_cdp_tool._LOCAL_BROWSER is None


@pytest.mark.parametrize("binary_name", ["brave", "brave-browser", "brave-browser-stable"])
def test_brave_linux_binary_lookup_and_profile_dir(binary_name, tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)

    def fake_which(name):
        if name == binary_name:
            return f"/usr/bin/{name}"
        return None

    with _mock_cdp_spawn(which_side_effect=fake_which) as (mock_popen, _mock_proc):
        ws_url = get_local_cdp_url("brave")
        args = mock_popen.call_args[0][0]
        port = _port_from_popen_args(args)
        assert ws_url == f"ws://127.0.0.1:{port}/devtools/browser/test-id"
        assert args[0] == f"/usr/bin/{binary_name}"
        expected_profile = tmp_path / ".config" / "writeragent" / "brave_profile"
        assert f"--user-data-dir={expected_profile}" in args


@pytest.mark.parametrize(
    ("browser_type", "env_var", "env_val", "expected_exe"),
    [
        ("brave", "ProgramFiles", r"C:\Program Files", r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("chromium", "ProgramFiles", r"C:\Program Files", r"C:\Program Files\Chromium\Application\chrome.exe"),
        ("chrome", "ProgramFiles", r"C:\Program Files", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ("chrome", "LocalAppData", r"C:\Users\me\AppData\Local", r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ],
)
def test_windows_binary_lookup(browser_type, env_var, env_val, expected_exe, tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ProgramFiles", r"C:\MissingProgramFiles")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\MissingProgramFilesX86")
    monkeypatch.setenv("LocalAppData", r"C:\MissingLocalAppData")
    monkeypatch.setenv(env_var, env_val)
    monkeypatch.setattr(browser_cdp_tool.sys, "platform", "win32")
    monkeypatch.setattr(browser_cdp_tool.os.path, "expandvars", ntpath.expandvars)
    monkeypatch.setattr(browser_cdp_tool.os.path, "isabs", ntpath.isabs)

    with (
        patch("os.path.exists", side_effect=lambda p: p == expected_exe),
        patch.object(browser_cdp_tool, "_win32_app_path", return_value=None),
        _mock_cdp_spawn(which_side_effect=lambda _name: None) as (mock_popen, _mock_proc),
    ):
        ws_url = get_local_cdp_url(browser_type)
        args = mock_popen.call_args[0][0]
        port = _port_from_popen_args(args)
        assert ws_url == f"ws://127.0.0.1:{port}/devtools/browser/test-id"
        assert args[0] == expected_exe
        expected_profile = tmp_path / ".config" / "writeragent" / f"{browser_type}_profile"
        assert f"--user-data-dir={expected_profile}" in args


def test_cdp_browser_registered_and_shutdown_all_kills(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    cleanup_local_browser()
    before = set(_instances)
    with _mock_cdp_spawn() as (_mock_popen, mock_proc):
        get_local_cdp_url("chrome")

        managed = browser_cdp_tool._LOCAL_BROWSER
        assert managed is not None
        assert managed in _instances
        assert len(_instances) == len(before) + 1

        shutdown_all()
        mock_proc.kill.assert_called()

        cleanup_local_browser()
        assert browser_cdp_tool._LOCAL_BROWSER is None
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
    cleanup_local_browser()
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
        cleanup_local_browser()


def test_browser_cdp_reuses_launch_endpoint(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    seen_urls: list[str] = []

    async def fake_cdp_call(ws_url, _method, _params, _target_id, _timeout):
        seen_urls.append(ws_url)
        return {"ok": True}

    with _mock_cdp_spawn() as (mock_popen, _mock_proc):
        ws_url = get_local_cdp_url("chrome")
        port = _port_from_popen_args(mock_popen.call_args[0][0])
        expected_url = f"ws://127.0.0.1:{port}/devtools/browser/test-id"
        assert ws_url == expected_url
        with (
            patch.object(browser_cdp_tool, "_cdp_call", side_effect=fake_cdp_call),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            res1 = json.loads(browser_cdp_tool.browser_cdp("Target.createTarget", {"url": "about:blank"}))
            res2 = json.loads(browser_cdp_tool.browser_cdp("Target.closeTarget", {"targetId": "T1"}))
            assert res1["success"] is True
            assert res2["success"] is True
            assert seen_urls == [expected_url, expected_url]
            mock_urlopen.assert_not_called()


@pytest.mark.parametrize("already_listening", [False, True])
def test_browser_cdp_after_cleanup_never_spawns(already_listening, tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    cleanup_local_browser()
    try:
        if already_listening:
            profile_dir = tmp_path / ".config" / "writeragent" / "chrome_profile"
            profile_dir.mkdir(parents=True)
            recorded_url = "ws://127.0.0.1:45678/devtools/browser/existing"
            (profile_dir / browser_cdp_tool._ENDPOINT_FILE).write_text(recorded_url, encoding="utf-8")
            version_response = MagicMock()
            version_response.read.return_value = json.dumps({
                "webSocketDebuggerUrl": recorded_url,
            }).encode("utf-8")
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value.__enter__.return_value = version_response
                ws_url = get_local_cdp_url("chrome")
            assert ws_url == recorded_url
            assert browser_cdp_tool._LOCAL_BROWSER is None
        else:
            with _mock_cdp_spawn():
                get_local_cdp_url("chrome")

        cleanup_local_browser()

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            result = VisitWebpageBrowserTool(CdpPageDriver()).forward("https://example.com")
            assert result.startswith(
                "Error visiting webpage via browser: Failed to create page target: The research browser is not running."
            )
            mock_popen.assert_not_called()
            mock_urlopen.assert_not_called()
    finally:
        cleanup_local_browser()


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (ConnectionRefusedError("Connection refused"), "may have been closed"),
        (TimeoutError("x"), "timed out"),
    ],
)
def test_dead_browser_fails_the_visit_without_spawning(exc, expected_fragment, tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)

    async def fake_cdp_call(_ws_url, _method, _params, _target_id, _timeout):
        raise exc

    with _mock_cdp_spawn() as (mock_popen, _mock_proc):
        get_local_cdp_url("chrome")
        assert mock_popen.call_count == 1
        with patch.object(browser_cdp_tool, "_cdp_call", side_effect=fake_cdp_call):
            result = VisitWebpageBrowserTool(CdpPageDriver()).forward("https://example.com")
            assert expected_fragment in result
            assert mock_popen.call_count == 1


def test_failed_relaunch_keeps_the_running_endpoint(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    seen_urls: list[str] = []

    async def fake_cdp_call(ws_url, _method, _params, _target_id, _timeout):
        seen_urls.append(ws_url)
        return {"ok": True}

    with _mock_cdp_spawn():
        first_url = get_local_cdp_url("chrome")
        with (
            patch("urllib.request.urlopen", side_effect=Exception("busy")),
            patch.object(
                browser_cdp_tool,
                "spawn_local_browser",
                side_effect=RuntimeError("another local browser is already running"),
            ),
        ):
            with pytest.raises(RuntimeError, match="another local browser is already running"):
                get_local_cdp_url("chrome")

        with patch.object(browser_cdp_tool, "_cdp_call", side_effect=fake_cdp_call):
            res = json.loads(browser_cdp_tool.browser_cdp("Target.getTargets", {}))
            assert res["success"] is True
            assert seen_urls == [first_url]


def test_chosen_browser_is_launched_when_another_listens_on_9222(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    seen_urls: list[str] = []

    mock_proc = MagicMock()
    mock_proc.pid = 4321
    mock_proc.poll.return_value = None
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    def fake_urlopen(url, **_kwargs):
        netloc = urlsplit(url).netloc
        ws = (
            "ws://127.0.0.1:9222/devtools/browser/foreign-id"
            if netloc == "127.0.0.1:9222"
            else f"ws://{netloc}/devtools/browser/test-id"
        )
        resp = MagicMock()
        resp.read.return_value = json.dumps({"webSocketDebuggerUrl": ws}).encode("utf-8")
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__.return_value = resp
        return ctx_mgr

    async def fake_cdp_call(ws_url, _method, _params, _target_id, _timeout):
        seen_urls.append(ws_url)
        return {"ok": True}

    try:
        with (
            patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            ws_url = get_local_cdp_url("brave")
            args = mock_popen.call_args[0][0]
            port = _port_from_popen_args(args)
            assert port != 9222
            assert args[0].endswith("brave")
            assert ws_url == f"ws://127.0.0.1:{port}/devtools/browser/test-id"

            with patch.object(browser_cdp_tool, "_cdp_call", side_effect=fake_cdp_call):
                res = json.loads(browser_cdp_tool.browser_cdp("Target.getTargets", {}))
                assert res["success"] is True
                assert seen_urls == [ws_url]
    finally:
        cleanup_local_browser()


def test_recorded_browser_is_reused(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    with _mock_cdp_spawn() as (mock_popen, _mock_proc):
        first_url = get_local_cdp_url("chrome")
        assert mock_popen.call_count == 1
        cleanup_local_browser()
        second_url = get_local_cdp_url("chrome")
        assert second_url == first_url
        assert mock_popen.call_count == 1
        assert browser_cdp_tool._LOCAL_BROWSER is None


def test_stale_record_launches_again(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    profile_dir = tmp_path / ".config" / "writeragent" / "chrome_profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / browser_cdp_tool._ENDPOINT_FILE).write_text(
        "ws://127.0.0.1:45678/devtools/browser/old-id",
        encoding="utf-8",
    )

    mock_proc = MagicMock()
    mock_proc.pid = 4321
    mock_proc.poll.return_value = None
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    def fake_urlopen(url, **_kwargs):
        netloc = urlsplit(url).netloc
        ws = (
            "ws://127.0.0.1:45678/devtools/browser/different-id"
            if netloc == "127.0.0.1:45678"
            else f"ws://{netloc}/devtools/browser/test-id"
        )
        resp = MagicMock()
        resp.read.return_value = json.dumps({"webSocketDebuggerUrl": ws}).encode("utf-8")
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__.return_value = resp
        return ctx_mgr

    try:
        with (
            patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            ws_url = get_local_cdp_url("chrome")
            mock_popen.assert_called_once()
            port = _port_from_popen_args(mock_popen.call_args[0][0])
            assert ws_url == f"ws://127.0.0.1:{port}/devtools/browser/test-id"
    finally:
        cleanup_local_browser()


def test_launch_adopts_browser_started_by_another_instance(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    sibling_url = "ws://127.0.0.1:54321/devtools/browser/sibling-id"
    profile_dir = tmp_path / ".config" / "writeragent" / "chrome_profile"

    mock_proc = MagicMock()
    mock_proc.pid = 4321
    mock_proc.poll.return_value = 0
    mock_proc.kill.side_effect = lambda: setattr(mock_proc.poll, "return_value", 0)

    def fake_urlopen(url, **_kwargs):
        if urlsplit(url).netloc == "127.0.0.1:54321":
            resp = MagicMock()
            resp.read.return_value = json.dumps({"webSocketDebuggerUrl": sibling_url}).encode("utf-8")
            ctx_mgr = MagicMock()
            ctx_mgr.__enter__.return_value = resp
            return ctx_mgr
        raise Exception("Connection refused")

    def fake_sleep(_seconds):
        (profile_dir / browser_cdp_tool._ENDPOINT_FILE).write_text(sibling_url, encoding="utf-8")

    try:
        with (
            patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch("plugin.contrib.cdp.browser_cdp_tool.time.sleep", side_effect=fake_sleep),
        ):
            assert get_local_cdp_url("chrome") == sibling_url
    finally:
        cleanup_local_browser()


def test_other_browser_type_refused_while_one_is_in_use(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    seen_urls: list[str] = []

    async def fake_cdp_call(ws_url, _method, _params, _target_id, _timeout):
        seen_urls.append(ws_url)
        return {"ok": True}

    with _mock_cdp_spawn() as (mock_popen, _mock_proc):
        chrome_url = get_local_cdp_url("chrome")
        assert mock_popen.call_count == 1
        with pytest.raises(RuntimeError, match="another local browser is already in use"):
            get_local_cdp_url("brave")
        assert mock_popen.call_count == 1

        with patch.object(browser_cdp_tool, "_cdp_call", side_effect=fake_cdp_call):
            res = json.loads(browser_cdp_tool.browser_cdp("Target.getTargets", {}))
            assert res["success"] is True
            assert seen_urls == [chrome_url]

        cleanup_local_browser()
        brave_url = get_local_cdp_url("brave")
        assert mock_popen.call_count == 2
        assert brave_url.startswith("ws://127.0.0.1:")


def test_spawn_unregisters_dead_browser():
    cleanup_local_browser()
    proc1 = MagicMock()
    proc1.pid = 4321
    proc1.poll.return_value = 0

    proc2 = MagicMock()
    proc2.pid = 4322
    proc2.poll.return_value = None
    proc2.kill.side_effect = lambda: setattr(proc2.poll, "return_value", 0)

    try:
        with patch("subprocess.Popen", side_effect=[proc1, proc2]):
            first = browser_cdp_tool.spawn_local_browser(["chrome"], label="chrome")
            assert first in _instances
            second = browser_cdp_tool.spawn_local_browser(["chrome"], label="chrome")
            assert first not in _instances
            assert second in _instances
    finally:
        cleanup_local_browser()
