# Automated unit tests for Firefox WebDriver BiDi client and browser visit tool.

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

import plugin.chatbot.web_research_firefox as wrf
from plugin.chatbot.web_research import VisitWebpageBrowserTool
from plugin.chatbot.web_research_firefox import (
    FIREFOX_BIDI_PORT,
    FirefoxBidiClient,
    begin_firefox_bidi,
    close_firefox_bidi,
)


@pytest.fixture(autouse=True)
def _isolate_home_and_forbid_kill(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(
        wrf,
        "_terminate_pid",
        lambda pid: pytest.fail(f"Unexpected _terminate_pid({pid}) in test"),
    )


class _FakeWebSocket:
    """In-memory WebSocket double whose recv() blocks on a queue."""

    def __init__(self, on_send=None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: queue.Queue[str | Exception] = queue.Queue()
        self._on_send = on_send
        self.closed = False
        self.close_event = threading.Event()

    def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if self._on_send is not None:
            self._on_send(msg, self)

    def recv(self) -> str:
        item = self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    def push_json(self, obj: dict[str, Any]) -> None:
        self.incoming.put(json.dumps(obj))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.incoming.put(ConnectionClosedOK(None, None))
            self.close_event.set()


def test_call_correlates_out_of_order_responses():
    first_sent = threading.Event()
    both_sent = threading.Event()
    sent_lock = threading.Lock()
    sent_ids: list[int] = []

    def on_send(msg, ws):
        with sent_lock:
            sent_ids.append(msg["id"])
            if len(sent_ids) == 1:
                first_sent.set()
            elif len(sent_ids) == 2:
                both_sent.set()
                # Reply to id 2 before id 1.
                ws.push_json({"type": "success", "id": sent_ids[1], "result": {"which": "second"}})
                ws.push_json({"type": "success", "id": sent_ids[0], "result": {"which": "first"}})

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    results: dict[str, dict[str, Any]] = {}

    def call_first():
        results["first"] = client.call("test.first", {})

    def call_second():
        first_sent.wait(2.0)
        results["second"] = client.call("test.second", {})

    t1 = threading.Thread(target=call_first)
    t2 = threading.Thread(target=call_second)
    try:
        t1.start()
        t2.start()
        t1.join(2.0)
        t2.join(2.0)
        assert both_sent.is_set()
        assert results["first"] == {"which": "first"}
        assert results["second"] == {"which": "second"}
        assert client._pending == {}
    finally:
        client.close()
        t1.join(1.0)
        t2.join(1.0)


def test_error_response_raises_runtime_error_with_message():
    def on_send(msg, ws):
        ws.push_json({
            "type": "error",
            "id": msg["id"],
            "error": "unknown error",
            "message": "Error: NS_ERROR_UNKNOWN_HOST",
        })

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    try:
        with pytest.raises(RuntimeError, match="unknown error: Error: NS_ERROR_UNKNOWN_HOST"):
            client.call("browsingContext.navigate", {"context": "t1", "url": "https://bad.invalid"})
    finally:
        client.close()


def test_call_timeout_drops_the_late_reply():
    timed_out_id: list[int] = []

    def on_send(msg, ws):
        if msg["method"] == "slow.cmd":
            timed_out_id.append(msg["id"])
        elif msg["method"] == "fast.cmd":
            # Deliver the late reply for slow.cmd first, then the reply for fast.cmd.
            ws.push_json({"type": "success", "id": timed_out_id[0], "result": {"stale": True}})
            ws.push_json({"type": "success", "id": msg["id"], "result": {"ok": True}})

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    try:
        with pytest.raises(TimeoutError, match="Timed out waiting for slow.cmd"):
            client.call("slow.cmd", {}, timeout=0.05)
        assert client.call("fast.cmd", {}, timeout=1.0) == {"ok": True}
        assert client._pending == {}
    finally:
        client.close()


def test_connection_drop_fails_pending_and_future_calls():
    def on_send(msg, ws):
        ws.incoming.put(ConnectionClosedError(None, None))

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    try:
        with pytest.raises(RuntimeError, match="Firefox BiDi connection closed"):
            client.call("browsingContext.create", {})
        assert client.closed is True
        sent_before = len(ws.sent)
        with pytest.raises(RuntimeError, match="Firefox BiDi connection closed: "):
            client.call("browsingContext.create", {})
        assert len(ws.sent) == sent_before
    finally:
        client.close()


def test_page_text_returns_value_and_raises_on_js_exception():
    responses = iter([
        {"type": "success", "result": {"value": "hello world"}},
        {"type": "exception", "exceptionDetails": {"text": "ReferenceError: nosuchvar is not defined"}},
    ])

    def on_send(msg, ws):
        ws.push_json({"type": "success", "id": msg["id"], "result": next(responses)})

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    try:
        assert client.page_text("ctx-1") == "hello world"
        with pytest.raises(RuntimeError, match="ReferenceError: nosuchvar is not defined"):
            client.page_text("ctx-1")
    finally:
        client.close()


def test_open_tab_navigate_close_send_bidi_methods():
    def on_send(msg, ws):
        if msg["method"] == "browsingContext.create":
            ws.push_json({"type": "success", "id": msg["id"], "result": {"context": "ctx-42"}})
        else:
            ws.push_json({"type": "success", "id": msg["id"], "result": {}})

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    try:
        tab = client.open_tab()
        assert tab == "ctx-42"
        client.navigate(tab, "https://example.test")
        client.close_tab(tab)
        assert ws.sent[0]["method"] == "browsingContext.create"
        assert ws.sent[0]["params"] == {"type": "tab", "background": True}
        assert ws.sent[1]["method"] == "browsingContext.navigate"
        assert ws.sent[1]["params"] == {"context": "ctx-42", "url": "https://example.test", "wait": "none"}
        assert ws.sent[2]["method"] == "browsingContext.close"
        assert ws.sent[2]["params"] == {"context": "ctx-42"}
    finally:
        client.close()


def test_begin_launches_firefox_without_chromium_flags_and_reuses_the_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    def on_send(msg, ws):
        ws.push_json({"type": "success", "id": msg["id"], "result": {}})

    ws = _FakeWebSocket(on_send=on_send)
    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", side_effect=[ConnectionRefusedError("down"), ws]) as mock_connect,
            patch("plugin.chatbot.web_research_firefox.find_browser_executable", return_value="/usr/bin/firefox"),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            client1 = begin_firefox_bidi()
            client2 = begin_firefox_bidi()
            assert client1 is client2
            assert mock_connect.call_count == 2
            mock_spawn.assert_called_once()
            cmd = mock_spawn.call_args.args[0]
            assert "--no-remote" in cmd
            assert "--new-instance" in cmd
            assert f"--remote-debugging-port={FIREFOX_BIDI_PORT}" in cmd
            assert "--profile" in cmd
            assert "--no-first-run" not in cmd
            assert not any(arg.startswith("--user-data-dir") for arg in cmd)
    finally:
        close_firefox_bidi()


def test_begin_does_not_spawn_when_port_holds_a_session_it_cannot_use():
    def on_send(msg, ws):
        ws.push_json({
            "type": "error",
            "id": msg["id"],
            "error": "session not created",
            "message": "Maximum number of active sessions",
        })

    ws = _FakeWebSocket(on_send=on_send)
    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", return_value=ws),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            with pytest.raises(RuntimeError, match="refused a new WebDriver BiDi session"):
                begin_firefox_bidi()
            mock_spawn.assert_not_called()
    finally:
        close_firefox_bidi()


def test_begin_relaunch_kills_dead_client_before_spawning_new_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    def on_send(msg, ws):
        ws.push_json({"type": "success", "id": msg["id"], "result": {}})

    ws1 = _FakeWebSocket(on_send=on_send)
    ws2 = _FakeWebSocket(on_send=on_send)
    events: list[str] = []
    connect_calls = 0

    def fake_connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        events.append("connect")
        if connect_calls == 1:
            return ws1
        if connect_calls == 2:
            raise ConnectionRefusedError("down")
        return ws2

    def fake_cleanup():
        events.append("cleanup")

    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", side_effect=fake_connect),
            patch("plugin.chatbot.web_research_firefox.cleanup_local_chrome", side_effect=fake_cleanup),
            patch("plugin.chatbot.web_research_firefox.find_browser_executable", return_value="/usr/bin/firefox"),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            mock_spawn.return_value.pid = 12345
            mock_spawn.return_value.popen.poll.return_value = None
            client1 = begin_firefox_bidi()
            ws1.incoming.put(ConnectionClosedError(None, None))
            assert ws1.close_event.wait(2.0)

            client2 = begin_firefox_bidi()
            assert client2 is not client1
            assert events == ["connect", "cleanup", "connect", "connect"]
            mock_spawn.assert_called_once()
    finally:
        close_firefox_bidi()


def test_begin_raises_on_15s_deadline(tmp_path, monkeypatch):
    clock = {"now": 100.0}

    def fake_time():
        return clock["now"]

    def fake_sleep(seconds: float):
        clock["now"] += seconds

    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", side_effect=ConnectionRefusedError("refused")),
            patch("plugin.chatbot.web_research_firefox.find_browser_executable", return_value="/usr/bin/firefox"),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
            patch("plugin.chatbot.web_research_firefox.time.time", side_effect=fake_time),
            patch("plugin.chatbot.web_research_firefox.time.sleep", side_effect=fake_sleep),
        ):
            mock_spawn.return_value.pid = 11111
            mock_spawn.return_value.popen.poll.return_value = None
            with pytest.raises(RuntimeError, match=f"after {int(wrf.FIREFOX_LAUNCH_TIMEOUT_SEC)} seconds"):
                begin_firefox_bidi()
            mock_spawn.assert_called_once()
    finally:
        close_firefox_bidi()


def test_begin_stops_early_on_nonzero_exit():
    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", side_effect=ConnectionRefusedError("refused")),
            patch("plugin.chatbot.web_research_firefox.find_browser_executable", return_value="/usr/bin/firefox"),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            mock_spawn.return_value.pid = 11111
            mock_spawn.return_value.popen.poll.return_value = 1
            with pytest.raises(RuntimeError, match="Firefox exited during startup \\(code 1\\)"):
                begin_firefox_bidi()
            mock_spawn.assert_called_once()
    finally:
        close_firefox_bidi()


def test_pid_is_alive_win32_timeout_fails_closed(monkeypatch):
    monkeypatch.setattr(wrf.sys, "platform", "win32")
    with patch.object(wrf.ManagedSubprocess, "run", side_effect=subprocess.TimeoutExpired(["tasklist"], 3.0)):
        assert wrf._pid_is_alive(1234) is True


def test_finish_does_not_remove_pidfile_owned_by_another_process(tmp_path):
    from plugin.chatbot import web_research as wr

    profile_dir = tmp_path / ".config" / "writeragent" / "firefox_profile"
    profile_dir.mkdir(parents=True)
    pidfile = profile_dir / wrf._PIDFILE_NAME
    pidfile.write_text("98765\n999999\n", encoding="utf-8")

    wr._cdp_refcount.reset_for_tests()
    try:
        wr._cdp_refcount.run_enter()
        with patch("plugin.contrib.cdp.browser_cdp_tool.cleanup_local_chrome"):
            wr._cdp_refcount.finish()
        assert pidfile.exists()
    finally:
        wr._cdp_refcount.reset_for_tests()


def test_cmdline_args_matches_exact_profile_and_port():
    profile_dir = "/tmp/writeragent/firefox_profile"
    port_flag = f"--remote-debugging-port={wrf.FIREFOX_BIDI_PORT}"
    assert wrf._cmdline_args_matches(["firefox", "--profile", profile_dir, port_flag], profile_dir) is True
    assert wrf._cmdline_args_matches(["firefox", "--profile", f"{profile_dir}_backup", port_flag], profile_dir) is False
    assert wrf._cmdline_args_matches(["firefox", "--profile", profile_dir], profile_dir) is False


def test_begin_kills_owned_orphan_and_respawns(tmp_path):
    profile_dir = tmp_path / ".config" / "writeragent" / "firefox_profile"
    profile_dir.mkdir(parents=True)
    pidfile = profile_dir / wrf._PIDFILE_NAME
    pidfile.write_text("98765\n55555\n", encoding="utf-8")

    def on_send_refuse(msg, ws):
        ws.push_json({"type": "error", "id": msg["id"], "error": "session not created", "message": "Maximum number of active sessions"})

    def on_send_ok(msg, ws):
        ws.push_json({"type": "success", "id": msg["id"], "result": {}})

    ws_refuse = _FakeWebSocket(on_send=on_send_refuse)
    ws_ok = _FakeWebSocket(on_send=on_send_ok)
    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", side_effect=[ws_refuse, ws_ok]),
            patch("plugin.chatbot.web_research_firefox._pid_is_alive", side_effect=lambda p: p != 55555),
            patch("plugin.chatbot.web_research_firefox._cmdline_matches_firefox_profile", return_value=True),
            patch("plugin.chatbot.web_research_firefox._terminate_pid") as mock_kill,
            patch("plugin.chatbot.web_research_firefox.find_browser_executable", return_value="/usr/bin/firefox"),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
            patch("plugin.chatbot.web_research_firefox.time.sleep"),
        ):
            mock_spawn.return_value.pid = 22222
            mock_spawn.return_value.is_alive.return_value = True
            client = begin_firefox_bidi()
            assert client is wrf._CLIENT
            mock_kill.assert_called_once_with(98765)
            mock_spawn.assert_called_once()
            lines = pidfile.read_text(encoding="utf-8").splitlines()
            assert lines[0] == "22222"
    finally:
        close_firefox_bidi()


def test_begin_does_not_kill_when_owner_is_alive(tmp_path):
    profile_dir = tmp_path / ".config" / "writeragent" / "firefox_profile"
    profile_dir.mkdir(parents=True)
    pidfile = profile_dir / wrf._PIDFILE_NAME
    pidfile.write_text("98765\n55555\n", encoding="utf-8")

    def on_send_refuse(msg, ws):
        ws.push_json({"type": "error", "id": msg["id"], "error": "session not created", "message": "Maximum number of active sessions"})

    ws_refuse = _FakeWebSocket(on_send=on_send_refuse)
    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", return_value=ws_refuse),
            patch("plugin.chatbot.web_research_firefox._pid_is_alive", return_value=True),
            patch("plugin.chatbot.web_research_firefox._cmdline_matches_firefox_profile", return_value=True),
            patch("plugin.chatbot.web_research_firefox._terminate_pid") as mock_kill,
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            with pytest.raises(RuntimeError, match="refused a new WebDriver BiDi session"):
                begin_firefox_bidi()
            mock_kill.assert_not_called()
            mock_spawn.assert_not_called()
    finally:
        close_firefox_bidi()


def test_cmdline_string_matches_rejects_prefix_and_handles_spaces():
    profile_dir = r"C:\Users\John Smith\.config\writeragent\firefox_profile"
    port_flag = f"--remote-debugging-port={wrf.FIREFOX_BIDI_PORT}"
    assert wrf._cmdline_string_matches(f'firefox.exe --profile "{profile_dir}" {port_flag}', profile_dir) is True
    assert wrf._cmdline_string_matches(f'firefox.exe --profile "{profile_dir}_backup" {port_flag}', profile_dir) is False
    assert wrf._cmdline_string_matches(f'firefox.exe --profile "{profile_dir}"', profile_dir) is False


def test_begin_does_not_kill_external_firefox_when_cmdline_lacks_profile(tmp_path):
    profile_dir = tmp_path / ".config" / "writeragent" / "firefox_profile"
    profile_dir.mkdir(parents=True)
    pidfile = profile_dir / wrf._PIDFILE_NAME
    pidfile.write_text("98765\n55555\n", encoding="utf-8")

    def on_send_refuse(msg, ws):
        ws.push_json({"type": "error", "id": msg["id"], "error": "session not created", "message": "Maximum number of active sessions"})

    ws_refuse = _FakeWebSocket(on_send=on_send_refuse)
    wrf._CLIENT = None
    try:
        with (
            patch("websockets.sync.client.connect", return_value=ws_refuse),
            patch("plugin.chatbot.web_research_firefox._pid_is_alive", return_value=False),
            patch("plugin.chatbot.web_research_firefox._cmdline_matches_firefox_profile", return_value=False),
            patch("plugin.chatbot.web_research_firefox._terminate_pid") as mock_kill,
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            with pytest.raises(RuntimeError, match="refused a new WebDriver BiDi session"):
                begin_firefox_bidi()
            mock_kill.assert_not_called()
            mock_spawn.assert_not_called()
    finally:
        close_firefox_bidi()


def test_reader_skips_undecodable_frame_and_fail_closes_socket():
    def on_send(msg, ws):
        ws.incoming.put("not valid json{{{")
        ws.push_json({"type": "success", "id": msg["id"], "result": {"ok": True}})

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    try:
        assert client.call("test.ping", {}) == {"ok": True}
        ws.incoming.put(RuntimeError("unexpected reader error"))
        assert ws.close_event.wait(2.0)
        assert client.closed is True
    finally:
        client.close()


def test_close_ends_session_once():
    def on_send(msg, ws):
        ws.push_json({"type": "success", "id": msg["id"], "result": {}})

    ws = _FakeWebSocket(on_send=on_send)
    client = FirefoxBidiClient(ws)  # type: ignore[arg-type]
    client.close()
    client.close()
    assert client.closed is True
    assert [m["method"] for m in ws.sent] == ["session.end"]


def test_visit_webpage_browser_tool_reports_driver_errors(monkeypatch):
    driver = MagicMock()
    driver.open_tab.return_value = "tab-1"
    driver.navigate.side_effect = RuntimeError("NS_ERROR_UNKNOWN_HOST")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    out = VisitWebpageBrowserTool(driver).forward("https://bad.invalid")
    assert out == "Error visiting webpage via browser: NS_ERROR_UNKNOWN_HOST"
    driver.close_tab.assert_called_once_with("tab-1")
