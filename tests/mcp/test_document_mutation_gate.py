# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the MCP per-document mutation gate."""

from __future__ import annotations

import gc
import logging
import threading
import time
import uuid

import pytest

from plugin.framework.errors import WriterAgentException
from plugin.mcp.document_mutation_gate import (
    BusyError,
    _doc_gates,
    _document_mutation_gate,
    _get_document_mutation_gate,
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


def test_document_mutation_gate_serializes_threads_and_releases() -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    events: list[str] = []
    started_t1 = threading.Event()

    def worker_1() -> None:
        with _document_mutation_gate(key):
            events.append("w1_start")
            started_t1.set()
            time.sleep(0.05)
            events.append("w1_end")

    def worker_2() -> None:
        started_t1.wait(timeout=2.0)
        with _document_mutation_gate(key):
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
        with _document_mutation_gate(key):
            raise RuntimeError("inside gate")

    # Subsequent acquisition on the same key must succeed without blocking
    acquired = False
    with _document_mutation_gate(key, timeout=0.1):
        acquired = True
    assert acquired is True


def test_document_mutation_gate_timeout_raises_busy_error_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    gate = _get_document_mutation_gate(key)
    assert gate.acquire(timeout=1.0) is True

    try:
        with caplog.at_level(logging.WARNING, logger="writeragent.mcp.protocol"):
            with pytest.raises(BusyError) as exc_info:
                with _document_mutation_gate(key, timeout=0.02):
                    pass

        assert exc_info.value.code == "SERVER_BUSY"
        assert key not in str(exc_info.value)
        assert any(
            "MCP _document_mutation_gate timed out" in record.message and key in record.message
            for record in caplog.records
        )
    finally:
        gate.release()


def test_doc_gates_are_weakly_held() -> None:
    key = f"test_doc_{uuid.uuid4().hex}"
    gate = _get_document_mutation_gate(key)
    assert _doc_gates.get(key) is gate
    del gate
    gc.collect()
    assert key not in _doc_gates

