# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Concurrency of mutating MCP tools (long-running and backpressure paths).
#
# UNO is marshalled to the LibreOffice main thread; these tests measure logical
# overlap in tool bodies (where mutation would happen), not raw UNO thread safety.
#
# _execute_with_backpressure: global Semaphore(1) + main thread + per-doc gate.
# _execute_long_running: HTTP worker thread, no global semaphore + per-doc gate.
#   - backpressure MUTATING, same document              -> serialized (1)
#   - long-running MUTATING, same document              -> serialized (1)
#   - long-running MUTATING, different documents        -> concurrent (2)
#   - long-running READ-ONLY, same document             -> concurrent (2)
#   - long-running + backpressure MUTATING, same doc    -> serialized (1)
import threading
import time

from plugin.mcp.mcp_protocol import MCPProtocolHandler


class _FakeMainThread:
    """Simulates the real single main-thread QueueExecutor: everything marshalled
    here runs serialized."""

    def __init__(self):
        self._lock = threading.Lock()

    def execute(self, fn, *args, **kwargs):  # ignores timeout=
        with self._lock:
            return fn(*args)


class _Doc:
    def __init__(self, url=""):
        self._url = url

    def getURL(self):
        return self._url


class _FakeDocSvc:
    def resolve_document_by_url(self, url):
        return (_Doc(url), "writer")

    def get_active_document(self):
        return _Doc("")

    def detect_doc_type(self, doc):
        return "writer"


class _ToolInfo:
    """Mimics the relevant ToolBase contract used by the handler."""

    def __init__(self, is_mutation, lock_required=None, lock_raises=False):
        self.is_mutation = is_mutation
        self._lock_required = lock_required
        self._lock_raises = lock_raises

    def detects_mutation(self):
        return bool(self.is_mutation)

    def requires_document_lock(self, arguments=None):
        if self._lock_raises:
            raise RuntimeError("hook failed")
        if self._lock_required is not None:
            return self._lock_required
        return self.detects_mutation()


class _Registry:
    """Stub tool_registry: .get(name) reports the lock contract; .execute is
    instrumented to measure the max concurrency observed inside the tool body."""

    def __init__(self, is_mutation=True, hold=0.4, lock_required=None, lock_raises=False, tool_info=None):
        self._is_mutation = is_mutation
        self._lock_required = lock_required
        self._lock_raises = lock_raises
        self._tool_info = tool_info
        self._hold = hold
        self._active = 0
        self.max_concurrency = 0
        self._lock = threading.Lock()

    def get(self, name):
        if self._tool_info is None:
            return _ToolInfo(self._is_mutation, self._lock_required, self._lock_raises)
        return self._tool_info

    def execute(self, name, context, **kwargs):
        with self._lock:
            self._active += 1
            self.max_concurrency = max(self.max_concurrency, self._active)
        time.sleep(self._hold)
        with self._lock:
            self._active -= 1
        return {"status": "ok"}


class _FakeServices:
    def __init__(self, tools):
        self.tools = tools
        self.document = _FakeDocSvc()

    def get(self, key):
        return _FakeMainThread() if key == "main_thread" else None


def _run_concurrent(method, doc_urls, *, kwargs_list=None):
    errors = []
    if kwargs_list is None:
        kwargs_list = [{}] * len(doc_urls)

    def worker(url, extra):
        try:
            method("any_tool", {}, document_url=url, **extra)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (type(e).__name__, e))

    threads = [threading.Thread(target=worker, args=(u, kw)) for u, kw in zip(doc_urls, kwargs_list)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_backpressure_path_serializes_via_semaphore():
    """Non-long-running tools go through _execute_with_backpressure (semaphore + gate)."""
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_with_backpressure, ["file:///x.odt"] * 2)

    assert not errors, "backpressure should not error: %s" % errors
    assert reg.max_concurrency == 1, "expected SERIALIZED (1), got %d" % reg.max_concurrency


def test_long_running_mutating_same_document_serializes():
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 1, (
        "expected SERIALIZED (1) for same-doc mutation, got %d" % reg.max_concurrency
    )


def test_long_running_mutating_different_documents_run_concurrently():
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///a.odt", "file:///b.odt"])

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) across different docs, got %d" % reg.max_concurrency
    )


def test_long_running_readonly_same_document_runs_concurrently():
    reg = _Registry(is_mutation=False)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running read-only should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) for read-only, got %d" % reg.max_concurrency
    )


def test_long_running_tool_can_opt_out_of_document_lock():
    reg = _Registry(is_mutation=True, lock_required=False)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) when the tool opts out of the lock, got %d" % reg.max_concurrency
    )


def test_normalized_doc_urls_share_mutation_gate():
    """file:///same.odt and file:///same.odt/ must serialize mutating long-running tools."""
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(
        handler._execute_long_running,
        ["file:///same.odt", "file:///same.odt/"],
    )

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 1, (
        "expected SERIALIZED (1) for normalized same doc, got %d" % reg.max_concurrency
    )


def test_unknown_tool_uses_mutation_gate():
    """When tool_registry.get returns None, mutating runs on the same doc serialize."""

    class _UnknownRegistry(_Registry):
        def get(self, name):
            return None

    reg = _UnknownRegistry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 1, (
        "expected SERIALIZED (1) for unknown tool on same doc, got %d" % reg.max_concurrency
    )


def test_requires_document_lock_exception_falls_back_to_detects_mutation():
    reg = _Registry(is_mutation=True, lock_raises=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 1, (
        "expected SERIALIZED (1) when hook fails and tool is mutating, got %d" % reg.max_concurrency
    )


def test_cross_path_long_running_and_backpressure_same_document_serializes():
    """Mutating long-running + backpressure on the same doc share the per-doc gate."""
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))
    errors = []

    def run_long():
        try:
            handler._execute_long_running("any_tool", {}, document_url="file:///same.odt")
        except Exception as e:  # noqa: BLE001
            errors.append("long: %s" % e)

    def run_backpressure():
        try:
            handler._execute_with_backpressure("any_tool", {}, document_url="file:///same.odt")
        except Exception as e:  # noqa: BLE001
            errors.append("bp: %s" % e)

    t1 = threading.Thread(target=run_long)
    t2 = threading.Thread(target=run_backpressure)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, "cross-path should not error: %s" % errors
    assert reg.max_concurrency == 1, (
        "expected SERIALIZED (1) across long-running + backpressure, got %d" % reg.max_concurrency
    )
