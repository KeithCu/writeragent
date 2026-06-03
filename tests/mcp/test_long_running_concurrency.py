# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Concurrency of mutating long-running MCP tools.
#
# _execute_with_backpressure acquires threading.Semaphore(1) AND marshals the whole
# tool body to the main thread, so those tools are serialized (add_comment goes
# through here, not long_running). _execute_long_running runs the tool body on the
# HTTP worker thread, off the semaphore, so two long-running tools could mutate the
# same document concurrently. The fix holds a PER-DOCUMENT lock for the duration of
# MUTATING long-running tools. So:
#   - backpressure path                         -> serialized (max concurrency 1)
#   - long-running MUTATING, same document      -> serialized (max concurrency 1)
#   - long-running MUTATING, different documents -> concurrent (max concurrency 2)
#   - long-running READ-ONLY, same document     -> concurrent (max concurrency 2)
# This guarantees no concurrent UNO mutation of the same document without blocking
# read-only work or work on other documents.
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

    def __init__(self, is_mutation, lock_required=None):
        self.is_mutation = is_mutation
        self._lock_required = lock_required

    def detects_mutation(self):
        return bool(self.is_mutation)

    def requires_document_lock(self, arguments=None):
        if self._lock_required is not None:
            return self._lock_required
        return self.detects_mutation()


class _Registry:
    """Stub tool_registry: .get(name) reports the lock contract; .execute is
    instrumented to measure the max concurrency observed inside the tool body
    (where a real UNO mutation would happen)."""

    def __init__(self, is_mutation=True, hold=0.4, lock_required=None):
        self._is_mutation = is_mutation
        self._lock_required = lock_required
        self._hold = hold
        self._active = 0
        self.max_concurrency = 0
        self._lock = threading.Lock()

    def get(self, name):
        return _ToolInfo(self._is_mutation, self._lock_required)

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


def _run_concurrent(method, doc_urls):
    errors = []

    def worker(url):
        try:
            method("any_tool", {}, document_url=url)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (type(e).__name__, e))

    threads = [threading.Thread(target=worker, args=(u,)) for u in doc_urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_backpressure_path_serializes_via_semaphore():
    """Non-long-running tools (add_comment & co.) go through _execute_with_backpressure,
    which serializes via Semaphore(1) + main thread. Max concurrency == 1."""
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_with_backpressure, ["file:///x.odt"] * 2)

    assert not errors, "backpressure should not error: %s" % errors
    assert reg.max_concurrency == 1, "expected SERIALIZED (1), got %d" % reg.max_concurrency


def test_long_running_mutating_same_document_serializes():
    """FIX: two MUTATING long-running tools on the SAME document must not overlap."""
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 1, (
        "expected SERIALIZED (1) for same-doc mutation, got %d" % reg.max_concurrency
    )


def test_long_running_mutating_different_documents_run_concurrently():
    """Mutating long-running tools on DIFFERENT documents are not over-serialized."""
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///a.odt", "file:///b.odt"])

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) across different docs, got %d" % reg.max_concurrency
    )


def test_long_running_readonly_same_document_runs_concurrently():
    """READ-ONLY long-running tools (e.g. document_research) are never blocked by the
    per-document mutation lock, even on the same document."""
    reg = _Registry(is_mutation=False)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running read-only should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) for read-only, got %d" % reg.max_concurrency
    )


def test_long_running_tool_can_opt_out_of_document_lock():
    """A tool flagged is_mutation=True but whose requires_document_lock() returns
    False (e.g. the delegate gateway routing to a read-only domain like
    document_research) must NOT be serialized — it runs concurrently on the same doc."""
    reg = _Registry(is_mutation=True, lock_required=False)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) when the tool opts out of the lock, got %d" % reg.max_concurrency
    )
