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
# _execute_with_backpressure: global Semaphore(1) + tool body on main + per-doc gate
#   acquired on the caller (not inside the marshalled main-thread callable).
# _execute_long_running: HTTP worker thread, no global semaphore + per-doc gate.
#   - backpressure MUTATING, same document              -> serialized (1)
#   - long-running MUTATING, same document              -> serialized (1)
#   - long-running MUTATING, different documents        -> concurrent (2)
#   - long-running READ-ONLY, same document             -> concurrent (2)
#   - long-running + backpressure MUTATING, same doc    -> serialized (1)
#   - backpressure gate wait while long-running holds it -> VCL thread stays free
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

    def __init__(self, is_mutation=True, hold=0.05, lock_required=None, lock_raises=False, tool_info=None):
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


def test_unknown_tool_is_rejected_up_front_without_executing():
    """An unknown tool name returns a structured UNKNOWN_TOOL error BEFORE the mutation gate and
    the registry ever run (previously it flowed through the gate and died later as a raw KeyError
    serialized under INTERNAL_ERROR, which reads as a server bug instead of a bad tool name)."""

    class _UnknownRegistry(_Registry):
        def get(self, name):
            return None

    reg = _UnknownRegistry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    result = handler._execute_long_running("totally_bogus_tool", {}, document_url="file:///same.odt")

    assert result["status"] == "error" and result["code"] == "UNKNOWN_TOOL"
    assert "tools/list" in result["message"]
    assert reg.max_concurrency == 0, "an unknown tool must never reach the registry"


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


def test_backpressure_gate_wait_does_not_block_vcl_thread():
    """A long-running mutator holds the per-doc gate on a worker.

    Concurrent backpressure must wait on its own thread. Marshalling the
    acquire onto the VCL thread froze the UI for up to the 30s timeout.
    """
    import queue as queue_mod

    held = threading.Event()
    release = threading.Event()
    bp_prepared = threading.Event()

    class _Vcl:
        def __init__(self):
            self._q = queue_mod.Queue()
            self.inside = threading.Event()
            self.durations = []
            self.thread = threading.Thread(target=self._loop, name="fake-vcl", daemon=True)
            self.thread.start()

        def _loop(self):
            while True:
                item = self._q.get()
                if item is None:
                    return
                fn, args, done = item
                self.inside.set()
                t0 = time.perf_counter()
                try:
                    result = fn(*args)
                    err = None
                except Exception as exc:  # noqa: BLE001
                    result, err = None, exc
                self.durations.append(time.perf_counter() - t0)
                self.inside.clear()
                done.put((result, err))

        def execute(self, fn, *args, **kwargs):
            done = queue_mod.Queue()
            self._q.put((fn, args, done))
            result, err = done.get(timeout=5)
            if err is not None:
                raise err
            return result

        def close(self):
            self._q.put(None)
            self.thread.join(timeout=2)

    class _Reg(_Registry):
        def get(self, name):
            info = super().get(name)
            # Capture the original bound method. Assigning the wrapper first
            # would recurse: the wrapper would call itself.
            original = info.requires_document_lock

            def requires_document_lock(arguments=None, _original=original):
                needed = _original(arguments)
                if held.is_set() and threading.current_thread() is vcl.thread:
                    bp_prepared.set()
                return needed

            info.requires_document_lock = requires_document_lock
            return info

        def execute(self, name, context, **kwargs):
            if threading.current_thread() is not vcl.thread:
                held.set()
                with self._lock:
                    self._active += 1
                    self.max_concurrency = max(self.max_concurrency, self._active)
                try:
                    assert release.wait(timeout=5), "long-running gate hold was not released"
                finally:
                    with self._lock:
                        self._active -= 1
                return {"status": "ok"}
            return super().execute(name, context, **kwargs)

    vcl = _Vcl()
    reg = _Reg(is_mutation=True, hold=0.05)
    handler = MCPProtocolHandler(_FakeServices(reg))
    handler.queue_executor = vcl
    errors = []

    def run_long():
        try:
            handler._execute_long_running("any_tool", {}, document_url="file:///gate-wait.odt")
        except Exception as exc:  # noqa: BLE001
            errors.append("long: %s: %s" % (type(exc).__name__, exc))

    def run_bp():
        try:
            handler._execute_with_backpressure("any_tool", {}, document_url="file:///gate-wait.odt")
        except Exception as exc:  # noqa: BLE001
            errors.append("bp: %s: %s" % (type(exc).__name__, exc))

    t_long = threading.Thread(target=run_long)
    t_bp = threading.Thread(target=run_bp)
    try:
        t_long.start()
        assert held.wait(timeout=2), "long-running never acquired the document gate"
        t_bp.start()
        assert bp_prepared.wait(timeout=2), "backpressure never finished main-thread prepare"
        # Prepare has returned. If the gate wait were inside that marshalled
        # call, the VCL thread would still be inside it until release.
        deadline = time.perf_counter() + 0.4
        while vcl.inside.is_set() and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert not vcl.inside.is_set(), "VCL thread blocked while the document gate was held"
        time.sleep(0.1)
        assert not vcl.inside.is_set(), "VCL thread blocked for the gate wait"
    finally:
        release.set()
        t_long.join(timeout=3)
        t_bp.join(timeout=3)
        vcl.close()

    assert not errors, "gate wait off the VCL thread should still complete: %s" % errors
    assert not t_long.is_alive() and not t_bp.is_alive()
    assert reg.max_concurrency == 1, "expected SERIALIZED (1), got %d" % reg.max_concurrency
    assert vcl.durations, "expected marshalled main-thread calls"
    assert max(vcl.durations) < 0.3, "a marshalled call included the gate wait: %s" % vcl.durations
