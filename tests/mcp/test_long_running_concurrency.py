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
#   - backpressure waits for that gate off the main-thread executor
import threading
import time

from plugin.mcp.mcp_protocol import MCPProtocolHandler
import pytest


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


@pytest.mark.parametrize(
    "is_mutation, value, expected, value_2",
    [
        pytest.param(True, "long_running should not error: %s", 1, "expected SERIALIZED (1) for same-doc mutation, got %d", id="test_long_running_mutating_same_document_serializes"),
        pytest.param(False, "long_running read-only should not error: %s", 2, "expected CONCURRENT (2) for read-only, got %d", id="test_long_running_readonly_same_document_runs_concurrently"),
    ],
)
def test_long_running_mutating_same_document_serializes(is_mutation, value, expected, value_2):
    reg = _Registry(is_mutation=is_mutation)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, value % errors
    assert reg.max_concurrency == expected, (
        value_2 % reg.max_concurrency
    )


@pytest.mark.parametrize(
    "value, value_2, expected, value_3",
    [
        pytest.param("file:///a.odt", "file:///b.odt", 2, "expected CONCURRENT (2) across different docs, got %d", id="test_long_running_mutating_different_documents_run_concurrently"),
        # file:///same.odt and file:///same.odt/ must serialize mutating long-running tools.
        pytest.param("file:///same.odt", "file:///same.odt/", 1, "expected SERIALIZED (1) for normalized same doc, got %d", id="test_normalized_doc_urls_share_mutation_gate"),
    ],
)
def test_long_running_mutating_different_documents_run_concurrently(value, value_2, expected, value_3):
    reg = _Registry(is_mutation=True)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, [value, value_2])

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == expected, (
        value_3 % reg.max_concurrency
    )

def test_long_running_tool_can_opt_out_of_document_lock():
    reg = _Registry(is_mutation=True, lock_required=False)
    handler = MCPProtocolHandler(_FakeServices(reg))

    errors = _run_concurrent(handler._execute_long_running, ["file:///same.odt"] * 2)

    assert not errors, "long_running should not error: %s" % errors
    assert reg.max_concurrency == 2, (
        "expected CONCURRENT (2) when the tool opts out of the lock, got %d" % reg.max_concurrency
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


class _DepthMainThread:
    """Records which functions ran inside the main-thread executor and how deep it is."""

    def __init__(self):
        self._lock = threading.Lock()
        self.depth = 0
        self.seen = []

    def execute(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", str(fn))
        with self._lock:
            self.depth += 1
            self.seen.append(name)
        try:
            return fn(*args)
        finally:
            with self._lock:
                self.depth -= 1


def test_backpressure_waits_for_document_gate_off_the_main_thread():
    """A mutating backpressure call must not occupy the main thread while the gate is held.

    What was wrong: _execute_with_backpressure marshalled _execute_tool_on_main, and
    that function acquired the per-document gate. The UI thread blocked until the
    30s timeout (BusyError) while a long-running mutator held the same gate.
    """
    release = threading.Event()
    long_in_body = threading.Event()
    body_depth = []

    class _Reg(_Registry):
        def execute(self, name, context, **kwargs):
            if not long_in_body.is_set():
                long_in_body.set()
                assert release.wait(timeout=3), "long-running body was not released"
                return {"status": "ok"}
            body_depth.append(main.depth)
            return {"status": "ok"}

    main = _DepthMainThread()
    reg = _Reg(is_mutation=True, hold=0)
    handler = MCPProtocolHandler(_FakeServices(reg))
    handler.queue_executor = main
    errors = []

    def run_long():
        try:
            handler._execute_long_running("any_tool", {}, document_url="file:///same.odt")
        except Exception as e:  # noqa: BLE001
            errors.append("long: %s" % e)

    def run_bp():
        try:
            handler._execute_with_backpressure("any_tool", {}, document_url="file:///same.odt")
        except Exception as e:  # noqa: BLE001
            errors.append("bp: %s" % e)

    t_long = threading.Thread(target=run_long)
    t_long.start()
    assert long_in_body.wait(timeout=2), "long-running tool never acquired the gate"
    t_bp = threading.Thread(target=run_bp)
    t_bp.start()

    deadline = time.perf_counter() + 2.0
    blocked_off_main = False
    while time.perf_counter() < deadline:
        with main._lock:
            names = list(main.seen)
            depth = main.depth
        if "_execute_tool_on_main" in names:
            release.set()
            t_long.join(timeout=2)
            t_bp.join(timeout=2)
            raise AssertionError("gate wait ran inside the main-thread dispatch: %s" % names)
        if names.count("_prepare_mcp_execution") >= 2 and depth == 0 and not body_depth:
            blocked_off_main = True
            break
        time.sleep(0.01)

    assert blocked_off_main, "backpressure never reached the off-thread gate wait (seen=%s)" % main.seen
    with main._lock:
        assert main.depth == 0
    assert not body_depth
    release.set()
    t_long.join(timeout=2)
    t_bp.join(timeout=2)

    assert not errors, "cross-path should not error: %s" % errors
    assert body_depth == [1], "backpressure tool body must run inside the main-thread dispatch, got %s" % body_depth
    assert "_invoke_prepared_mcp_tool" in main.seen


def test_readonly_backpressure_does_not_wait_on_held_mutation_gate():
    """Read-only fast tools skip the gate, so a long-running mutator must not stall them."""
    release = threading.Event()
    long_in_body = threading.Event()
    readonly_done = threading.Event()

    class _Reg(_Registry):
        def get(self, name):
            return _ToolInfo(name != "readonly_tool")

        def execute(self, name, context, **kwargs):
            if name == "readonly_tool":
                readonly_done.set()
                return {"status": "ok"}
            long_in_body.set()
            assert release.wait(timeout=3), "long-running body was not released"
            return {"status": "ok"}

    reg = _Reg(is_mutation=True, hold=0)
    handler = MCPProtocolHandler(_FakeServices(reg))
    errors = []

    def run_long():
        try:
            handler._execute_long_running("mutator", {}, document_url="file:///same.odt")
        except Exception as e:  # noqa: BLE001
            errors.append("long: %s" % e)

    def run_bp():
        try:
            handler._execute_with_backpressure("readonly_tool", {}, document_url="file:///same.odt")
        except Exception as e:  # noqa: BLE001
            errors.append("bp: %s" % e)

    t_long = threading.Thread(target=run_long)
    t_long.start()
    assert long_in_body.wait(timeout=2)
    t_bp = threading.Thread(target=run_bp)
    t_bp.start()
    assert readonly_done.wait(timeout=2), "read-only backpressure waited on the mutation gate"
    release.set()
    t_long.join(timeout=2)
    t_bp.join(timeout=2)
    assert not errors, errors
