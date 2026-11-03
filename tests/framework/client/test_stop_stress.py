# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Hammer Stop across threads before/after client cancellation refactor."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from threading import Event
from typing import Callable, Iterator
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.llm_client import LlmClient

from plugin.framework.client.request_controls import (
    reset_host_pacing_for_tests,
    reset_local_unverified_hosts_for_tests,
)
from plugin.framework.errors import NetworkError
from plugin.framework.send_cancellation import SendCancellation
from plugin.tests.testing_utils import MockContext, create_mock_http_response

# Transport holds ``http.client`` on the module; patch here so mocks never leak
# to another thread after ``stop_patch`` restores the real class.
_HTTPS_PATCH = "plugin.framework.client.http_transport.http.client.HTTPSConnection"
_RUN_BG_PATCH = "plugin.framework.worker_pool.run_in_background"


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


def _teardown_client(client: LlmClient) -> None:
    client.stop()
    client._close_connection()


def _sync_run_in_background(fn: Callable[[], object], **kwargs: object) -> object:
    """Run connect (and similar) on the caller thread so no worker outlives a patch."""

    class _Handle:
        def join(self, timeout: float | None = None) -> None:
            fn()

    return _Handle()


@contextmanager
def _patched_http(setup: Callable[[MagicMock], None]) -> Iterator[MagicMock]:
    with (
        patch(_HTTPS_PATCH) as mock_https,
        patch(_RUN_BG_PATCH, _sync_run_in_background),
    ):
        setup(mock_https)
        yield mock_https


def test_stop_hammer_four_phases() -> None:
    """Four threads × two iterations exercise Stop before/during connect, 429 wait, and mid-stream."""
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()
    errors: list[str] = []
    request_after_stop = threading.Event()
    # Patches are process-wide: phase_lock serializes phases; helper threads must
    # finish before the patch context exits; reset host pacing between phases.
    phase_lock = threading.Lock()

    def _run_once(phase: int) -> None:
        reset_host_pacing_for_tests()
        reset_local_unverified_hosts_for_tests()
        with phase_lock:
            _run_once_locked(phase)

    def _run_once_locked(phase: int) -> None:
        client = _client()
        messages = [{"role": "user", "content": "Hi"}]
        try:
            if phase == 0:
                client.stop()
                with _patched_http(
                    lambda mock_https: setattr(mock_https, "return_value", MagicMock())
                ) as mock_https:
                    mock_conn = mock_https.return_value

                    def _track_request(*args: object, **kwargs: object) -> None:
                        if client.stopped():
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

                with _patched_http(lambda mock_https: None) as mock_https:
                    mock_conn = MagicMock()
                    mock_https.return_value = mock_conn
                    mock_conn.sock = None

                    def _blocking_connect() -> None:
                        connect_started.set()
                        connect_release.wait(10)
                        if client.stopped():
                            raise ConnectionError("connect aborted after Stop")
                        mock_conn.sock = MagicMock()

                    mock_conn.connect.side_effect = _blocking_connect
                    mock_conn.request.side_effect = AssertionError("HTTP request must not run after Stop during connect")

                    def _worker() -> None:
                        if connect_started.wait(2):
                            client.stop()
                        connect_release.set()

                    worker = threading.Thread(target=_worker, name="phase1-stop", daemon=False)
                    worker.start()
                    try:
                        t0 = time.monotonic()
                        try:
                            result = client.stream_request_with_tools(messages=messages, max_tokens=50)
                        finally:
                            connect_release.set()
                        assert time.monotonic() - t0 < 6.0
                    finally:
                        worker.join(timeout=5)
                        if worker.is_alive():
                            raise AssertionError("phase 1 stop helper still running after join")
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
                    _patched_http(
                        lambda mock_https: setattr(
                            mock_https, "return_value", _conn_with_response(busy)
                        )
                    ) as mock_https,
                    patch("plugin.framework.client.http_transport.backoff_delay_sec", side_effect=_backoff),
                    patch("plugin.framework.client.http_transport.wait_abortable", side_effect=_wait_with_signal),
                    patch("plugin.framework.client.request_controls.wait_abortable", side_effect=_wait_with_signal),
                ):
                    t0 = time.monotonic()
                    result = client.stream_request_with_tools(messages=messages, max_tokens=50)
                    elapsed = time.monotonic() - t0
                    assert result["finish_reason"] == "stop"
                    assert elapsed < 1.5
                    assert mock_https.call_count <= 1
                return

            if phase == 3:
                seen: list[str] = []

                def _on_content(text: str) -> None:
                    seen.append(text)
                    client.stop()

                resp = create_mock_http_response(
                    sse_lines=_sse_content_lines("one", "two"),
                    iter_side_effect=ConnectionResetError("reset after stop"),
                )

                with _patched_http(
                    lambda mock_https: setattr(
                        mock_https, "return_value", _conn_with_response(resp)
                    )
                ) as mock_https:
                    result = client.stream_request_with_tools(
                        messages=messages,
                        max_tokens=50,
                        append_callback=_on_content,
                    )
                    assert seen == ["one"]
                    assert result["content"] == "one"
                    assert result["finish_reason"] == "stop"
                    assert mock_https.call_count == 1
                return

            if phase == 4:
                scope = SendCancellation()

                def _tool_iter():
                    yield b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\\"q\\":"}}]}}]}\n'
                    scope.cancel()
                    yield b'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}\n'

                resp = create_mock_http_response(sse_lines=[])
                resp.__iter__.return_value = _tool_iter()

                with _patched_http(
                    lambda mock_https: setattr(
                        mock_https, "return_value", _conn_with_response(resp)
                    )
                ) as mock_https:
                    result = client.stream_request_with_tools(
                        messages=messages,
                        max_tokens=50,
                        tools=[{"type": "function", "function": {"name": "get_weather"}}],
                        cancel=scope,
                    )
                    assert result["finish_reason"] == "stop"
                    assert result.get("tool_calls") is None
                    assert client.stopped() is False
                    assert scope.is_cancelled() is True
                return

            raise AssertionError(f"unexpected phase {phase}")
        finally:
            _teardown_client(client)

    def _thread_main(tid: int) -> None:
        for iteration in range(2):
            phase = (tid * 2 + iteration) % 5
            try:
                _run_once(phase)
            except Exception as exc:
                errors.append(f"thread {tid} iter {iteration} phase {phase}: {exc!r}")

    threads = [threading.Thread(target=_thread_main, args=(tid,), name=f"hammer-{tid}") for tid in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
        if th.is_alive():
            errors.append(f"thread {th.name} still alive after 20s join")

    client = _client()
    try:
        resp = create_mock_http_response(
            sse_lines=[f'data: {json.dumps({"choices": [{"delta": {"content": "x"}}]})}'.encode()],
            iter_side_effect=ConnectionResetError("drop"),
        )
        with _patched_http(
            lambda mock_https: setattr(mock_https, "return_value", _conn_with_response(resp))
        ):
            with pytest.raises(NetworkError) as err:
                client.stream_request_with_tools(
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=50,
                )
        assert err.value.code == "CONNECTION_LOST"
    finally:
        _teardown_client(client)

    assert not request_after_stop.is_set()
    assert errors == [], errors


def _conn_with_response(response: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.getresponse.return_value = response
    return conn
