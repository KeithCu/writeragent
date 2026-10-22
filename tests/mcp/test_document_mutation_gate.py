# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the MCP per-document mutation gate."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from plugin.framework.errors import WriterAgentException
from plugin.mcp.document_mutation_gate import (
    BusyError,
    _doc_gates,
    _document_mutation_gate,
    _get_document_mutation_gate,
    _tool_needs_document_mutation_gate,
)


def test_busy_error_is_writeragent_exception() -> None:
    assert issubclass(BusyError, WriterAgentException)
    err = BusyError("server is busy")
    assert err.code == "SERVER_BUSY"
    assert "server is busy" in str(err)


def test_get_document_mutation_gate_same_and_different_keys() -> None:
    key_a = f"test_doc_{uuid.uuid4().hex}"
    key_b = f"test_doc_{uuid.uuid4().hex}"

    lock_a1 = _get_document_mutation_gate(key_a)
    lock_a2 = _get_document_mutation_gate(key_a)
    lock_b = _get_document_mutation_gate(key_b)

    assert isinstance(lock_a1, type(threading.Lock()))
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


def test_tool_needs_document_mutation_gate_none_tool() -> None:
    assert _tool_needs_document_mutation_gate(None) is True
    assert _tool_needs_document_mutation_gate(None, {"domain": "anything"}) is True


def test_tool_needs_document_mutation_gate_passthrough_and_bool_conversion() -> None:
    tool = MagicMock()
    tool.requires_document_lock.return_value = 1
    args = {"domain": "text", "opt": 42}
    result = _tool_needs_document_mutation_gate(tool, args)
    assert result is True
    tool.requires_document_lock.assert_called_once_with(args)

    tool.requires_document_lock.reset_mock()
    tool.requires_document_lock.return_value = 0
    assert _tool_needs_document_mutation_gate(tool, args) is False

    tool.requires_document_lock.return_value = "yes"
    assert _tool_needs_document_mutation_gate(tool) is True

    tool.requires_document_lock.return_value = None
    assert _tool_needs_document_mutation_gate(tool) is False


def test_tool_needs_document_mutation_gate_propagates_exception() -> None:
    tool = MagicMock()
    tool.requires_document_lock.side_effect = ValueError("lock check failed")
    with pytest.raises(ValueError, match="lock check failed"):
        _tool_needs_document_mutation_gate(tool, {"domain": "err"})


def test_document_mutation_gate_disabled_does_not_insert_key_or_acquire() -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    assert key not in _doc_gates

    executed = False
    with _document_mutation_gate(key, enabled=False):
        executed = True

    assert executed is True
    assert key not in _doc_gates


def test_document_mutation_gate_serializes_threads_and_releases() -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    events: list[str] = []
    started_t1 = threading.Event()

    def worker_1() -> None:
        with _document_mutation_gate(key, enabled=True):
            events.append("w1_start")
            started_t1.set()
            time.sleep(0.05)
            events.append("w1_end")

    def worker_2() -> None:
        started_t1.wait(timeout=2.0)
        with _document_mutation_gate(key, enabled=True):
            events.append("w2_start")
            events.append("w2_end")

    t1 = threading.Thread(target=worker_1)
    t2 = threading.Thread(target=worker_2)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert events == ["w1_start", "w1_end", "w2_start", "w2_end"]


def test_document_mutation_gate_releases_on_exception() -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError, match="inside gate"):
        with _document_mutation_gate(key, enabled=True):
            raise RuntimeError("inside gate")

    # Subsequent acquisition on the same key must succeed without blocking
    acquired = False
    with _document_mutation_gate(key, enabled=True, timeout=0.1):
        acquired = True
    assert acquired is True


def test_document_mutation_gate_timeout_raises_busy_error_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    gate = _get_document_mutation_gate(key)
    assert gate.acquire(timeout=1.0) is True

    try:
        with caplog.at_level(logging.WARNING, logger="writeragent.mcp.protocol"):
            with pytest.raises(BusyError) as exc_info:
                with _document_mutation_gate(key, enabled=True, timeout=0.02):
                    pass

        assert exc_info.value.code == "SERVER_BUSY"
        assert key in str(exc_info.value)
        assert any(
            "MCP _document_mutation_gate timed out" in record.message and key in record.message
            for record in caplog.records
        )
    finally:
        gate.release()
