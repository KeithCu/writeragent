from __future__ import annotations
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Hammer Stop across threads before/after client cancellation refactor."""


import json
import threading
import time
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.llm_client import LlmClient

from plugin.framework.client.request_controls import (
    reset_host_pacing_for_tests,
    reset_local_unverified_hosts_for_tests,
)
from plugin.framework.errors import NetworkError
from plugin.tests.testing_utils import MockContext, create_mock_http_response


def _sse_content_lines(*parts: str) -> list[bytes]:
    lines = [f'data: {json.dumps({"choices": [{"delta": {"content": p}}]})}'.encode() for p in parts]
    lines.append(b"data: [DONE]")
    return lines


def _client() -> LlmClient:
    config = {
        "endpoint": "https://api.openai.com",
        "api_key": "sk-test-key",
        "model": "gpt-4o",
        "temperature": 0.7,
        "request_timeout": 60,
    }
    return LlmClient(config, MockContext(), register_with_send=False)


def _https_steps(mock_https, *responses):
    conns = []
    for resp in responses:
        conn = MagicMock()
        conn.getresponse.return_value = resp
        conns.append(conn)
    mock_https.side_effect = conns
    return conns


def test_stop_hammer_four_phases() -> None:
    """Eight threads × four iterations exercise Stop before/during connect, 429 wait, and mid-stream."""
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()
    errors: list[str] = []
    request_after_stop = threading.Event()
    lock = threading.Lock()
    # Patches on http.client.HTTPSConnection are process-wide; serialize mock setup.
    phase_lock = threading.Lock()

    def _run_once(phase: int) -> None:
        with phase_lock:
            _run_once_locked(phase)

    def _run_once_locked(phase: int) -> None:
        client = _client()
        messages = [{"role": "user", "content": "Hi"}]

        if phase == 0:
            client.stop()
            with patch("http.client.HTTPSConnection") as mock_https:
                mock_conn = MagicMock()
                mock_https.return_value = mock_conn

                def _track_request(*args: object, **kwargs: object) -> None:
                    if client.stopped() if hasattr(client, "stopped") else client.stopped():
                        request_after_stop.set()
                    raise AssertionError("request() after stop")

                mock_conn.request.side_effect = _track_request
                result = client.stream_request_with_tools(messages=messages, max_tokens=50)
            assert result["finish_reason"] == "stop"
            assert mock_https.call_count == 0
            return

        if phase == 1:
            connect_started = Event()
            connect_release = Event()

            def _sync_connect_worker(fn, **kwargs: object) -> object:
                class _Handle:
                    def join(self, timeout: float | None = None) -> None:
                        fn()

                return _Handle()

            with (
                patch("plugin.framework.worker_pool.run_in_background", _sync_connect_worker),
                patch("http.client.HTTPSConnection") as mock_https,
            ):
                mock_conn = MagicMock()
                mock_https.return_value = mock_conn
                mock_conn.sock = None

                def _blocking_connect() -> None:
                    connect_started.set()
                    connect_release.wait(10)
                    mock_conn.sock = MagicMock()

                mock_conn.connect.side_effect = _blocking_connect

                def _worker() -> None:
                    if connect_started.wait(2):
                        client.stop()
                    connect_release.set()

                threading.Thread(target=_worker, daemon=True).start()
                t0 = time.monotonic()
                try:
                    result = client.stream_request_with_tools(messages=messages, max_tokens=50)
                finally:
                    connect_release.set()
                assert time.monotonic() - t0 < 6.0
            assert result["finish_reason"] == "stop"
            mock_conn.request.assert_not_called()
            return

        if phase == 2:
            busy = create_mock_http_response(
                429,
                json_data={"error": {"message": "overloaded"}},
                reason="Too Many Requests",
            )
            wait_started = Event()

            def _backoff(*args: object, **kwargs: object) -> float:
                return 2.0

            def _wait_with_signal(delay: float, cancel=None, **kwargs: object) -> bool:
                wait_started.set()
                client.stop()
                return False

            with (
                patch("http.client.HTTPSConnection") as mock_https,
                patch("plugin.framework.client.http_transport.backoff_delay_sec", side_effect=_backoff),
                patch("plugin.framework.client.http_transport.wait_abortable", side_effect=_wait_with_signal),
                patch("plugin.framework.client.request_controls.wait_abortable", side_effect=_wait_with_signal),
            ):
                conn = MagicMock()
                conn.getresponse.return_value = busy
                mock_https.return_value = conn
                t0 = time.monotonic()
                result = client.stream_request_with_tools(messages=messages, max_tokens=50)
                elapsed = time.monotonic() - t0
            assert result["finish_reason"] == "stop"
            assert elapsed < 1.5
            assert mock_https.call_count <= 1
            return

        # phase 3: stop after first token, then connection reset
        seen: list[str] = []

        def _on_content(text: str) -> None:
            seen.append(text)
            client.stop()

        resp = create_mock_http_response(
            sse_lines=_sse_content_lines("one", "two"),
            iter_side_effect=ConnectionResetError("reset after stop"),
        )
        with patch("http.client.HTTPSConnection") as mock_https:
            conn = MagicMock()
            conn.getresponse.return_value = resp
            mock_https.return_value = conn
            result = client.stream_request_with_tools(
                messages=messages,
                max_tokens=50,
                append_callback=_on_content,
            )
        assert seen == ["one"]
        assert result["content"] == "one"
        assert result["finish_reason"] == "stop"
        assert mock_https.call_count == 1

    def _thread_main(tid: int) -> None:
        try:
            for iteration in range(2):
                phase = (tid * 2 + iteration) % 4
                _run_once(phase)
        except Exception as exc:
            with lock:
                errors.append(f"thread {tid}: {exc!r}")

    threads = [threading.Thread(target=_thread_main, args=(tid,), daemon=True) for tid in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
        if th.is_alive():
            errors.append(f"thread {th.name} still alive after 20s join")

    # Reset without stop must surface CONNECTION_LOST after content.
    client = _client()
    resp = create_mock_http_response(
        sse_lines=[f'data: {json.dumps({"choices": [{"delta": {"content": "x"}}]})}'.encode()],
        iter_side_effect=ConnectionResetError("drop"),
    )
    with patch("http.client.HTTPSConnection") as mock_https:
        conn = MagicMock()
        conn.getresponse.return_value = resp
        mock_https.return_value = conn
        with pytest.raises(NetworkError) as err:
            client.stream_request_with_tools(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=50,
            )
    assert err.value.code == "CONNECTION_LOST"

    assert not request_after_stop.is_set()
    assert errors == [], errors
