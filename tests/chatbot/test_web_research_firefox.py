# Automated unit tests for Firefox WebDriver BiDi client and browser visit tool.

from __future__ import annotations

import json
import queue
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


class _FakeWebSocket:
    """In-memory WebSocket double whose recv() blocks on a queue."""

    def __init__(self, on_send=None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: queue.Queue[str | Exception] = queue.Queue()
        self._on_send = on_send
        self.closed = False

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
        with pytest.raises(RuntimeError, match="Firefox BiDi connection closed"):
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
            patch("plugin.chatbot.web_research_firefox.connect", side_effect=[ConnectionRefusedError("down"), ws]) as mock_connect,
            patch("plugin.chatbot.web_research_firefox.find_browser_executable", return_value="/usr/bin/firefox"),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            client1 = begin_firefox_bidi()
            client2 = begin_firefox_bidi()
            assert client1 is client2
            assert mock_connect.call_count == 2
            mock_spawn.assert_called_once()
            cmd = mock_spawn.call_args.args[0]
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
            patch("plugin.chatbot.web_research_firefox.connect", return_value=ws),
            patch("plugin.chatbot.web_research_firefox.spawn_local_browser") as mock_spawn,
        ):
            with pytest.raises(RuntimeError, match="refused a new WebDriver BiDi session"):
                begin_firefox_bidi()
            mock_spawn.assert_not_called()
    finally:
        close_firefox_bidi()


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
