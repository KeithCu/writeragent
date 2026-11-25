# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from typing import Any

import pytest

import plugin.framework.warm_child as warm_child_mod
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.child_runtime import Deadline
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.warm_child import (
    Child,
    ChildSlot,
    ChildSpec,
    Supervisor,
    WarmChild,
    _SpawnRequest,
    stderr_thread_name,
)


def _repo_root() -> str:
    # make release verifies against a stripped temp tree that has no
    # compute_service/; release_verify sets WRITERAGENT_PROJECT_ROOT to the
    # real checkout so spawned worker scripts can import it.
    env_root = os.environ.get("WRITERAGENT_PROJECT_ROOT", "").strip()
    if env_root and os.path.isdir(os.path.join(env_root, "compute_service")):
        return os.path.abspath(env_root)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _stdio_worker(
    tmp_path: Any,
    *,
    preamble: str = "",
    handler_body: str = "return {'status': 'ok', 'echo': req}",
) -> str:
    root = _repo_root()
    script = tmp_path / "warm_test_worker.py"
    handler_lines = handler_body.splitlines() or ["return {'status': 'ok', 'echo': req}"]
    indented = "\n".join("    " + line for line in handler_lines)
    script.write_text(
        "\n".join(
            [
                "import os, sys",
                f"sys.path.insert(0, {root!r})",
                preamble,
                "from compute_service.worker_stdio import run_worker_stdio_loop",
                "def handle(req):",
                indented,
                "if __name__ == '__main__':",
                "    raise SystemExit(run_worker_stdio_loop(handle))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return str(script)


def _spec(tmp_path: Any, script: str, **kwargs: Any) -> ChildSpec:
    env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    return ChildSpec(name="warm-test", argv=[sys.executable, script], env=env, **kwargs)


@pytest.fixture
def supervisor() -> Generator[Supervisor, None, None]:
    sup = Supervisor()
    yield sup
    sup.shutdown_all()


def _wait_until(deadline: float, predicate: Callable[[], bool], *, interval: float = 0.05) -> bool:
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_spawn_runs_on_supervisor_spawner_thread(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    start_idents: list[int] = []
    orig = ManagedSubprocess.start

    def tracked_start(self: ManagedSubprocess) -> None:
        start_idents.append(threading.get_ident())
        return orig(self)

    monkeypatch.setattr(ManagedSubprocess, "start", tracked_start)
    try:
        with warm.lease(Deadline(15)) as leased:
            assert not isinstance(leased, dict)
        assert supervisor.spawner_ident is not None
        assert start_idents == [supervisor.spawner_ident]
    finally:
        warm.retire()


@pytest.mark.skipif(sys.platform != "linux", reason="PDEATHSIG is Linux-only")
def test_pdeathsig_survives_short_lived_caller_thread(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    try:

        def brief() -> None:
            with warm.lease(Deadline(15)) as leased:
                assert not isinstance(leased, dict)

        t = threading.Thread(target=brief)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
        deadline = time.monotonic() + 2.0
        assert _wait_until(deadline, warm.is_alive)
    finally:
        warm.retire()


def test_handshake_timeout_returns_error(tmp_path: Any, supervisor: Supervisor, monkeypatch: Any) -> None:
    import plugin.framework.warm_child as warm_child_mod

    monkeypatch.setattr(warm_child_mod, "SPAWN_READY_TIMEOUT_SEC", 0.4)
    script = tmp_path / "slow_ready.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    warm = WarmChild(_spec(tmp_path, str(script)), supervisor=supervisor)
    try:
        with warm.lease(Deadline(5)) as leased:
            assert isinstance(leased, dict)
            assert leased.get("code") == "EXECUTION_TIMEOUT"
        assert not warm.is_alive()
    finally:
        warm.retire()


def test_garbage_ready_frame_not_adopted(tmp_path: Any, supervisor: Supervisor) -> None:
    root = _repo_root()
    script = tmp_path / "garbage_ready.py"
    script.write_text(
        "\n".join(
            [
                "import sys, time",
                f"sys.path.insert(0, {root!r})",
                "from plugin.framework.ipc import write_pickle_frame, claim_ipc_channel",
                "out = claim_ipc_channel()",
                "write_pickle_frame(out, {'status': 'booting'})",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    warm = WarmChild(_spec(tmp_path, str(script)), supervisor=supervisor)
    try:
        with warm.lease(Deadline(10)) as leased:
            assert isinstance(leased, dict)
            assert leased.get("code") == "WORKER_SPAWN_FAILED"
        assert not warm.is_alive()
    finally:
        warm.retire()


def test_kill_during_handshake_not_adopted(tmp_path: Any, supervisor: Supervisor) -> None:
    script = tmp_path / "hang_ready.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    warm = WarmChild(_spec(tmp_path, str(script)), supervisor=supervisor)
    err: list[dict[str, Any] | None] = [None]

    def lease_thread() -> None:
        with warm.lease(Deadline(30)) as leased:
            if isinstance(leased, dict):
                err[0] = leased

    t = threading.Thread(target=lease_thread)
    t.start()
    time.sleep(0.3)
    warm.kill()
    t.join(timeout=10)
    assert not t.is_alive()
    assert warm.child is None or not warm.is_alive()
    script2 = _stdio_worker(tmp_path)
    warm2 = WarmChild(_spec(tmp_path, script2), supervisor=supervisor)
    try:
        with warm2.lease(Deadline(10)) as leased:
            assert not isinstance(leased, dict)
            assert leased.pid > 0
    finally:
        warm2.retire()
    warm.retire()


@pytest.mark.skipif(sys.platform != "linux", reason="SIGKILL idle-death check is Linux-only")
def test_external_sigkill_of_idle_child_respawns(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    child_pid: int | None = None
    try:
        with warm.lease(Deadline(10)) as leased:
            assert not isinstance(leased, dict)
            child_pid = leased.pid
        assert child_pid is not None
        os.kill(child_pid, signal.SIGKILL)
        gone_deadline = time.monotonic() + 5.0
        assert _wait_until(gone_deadline, lambda: not warm.is_alive())
        with warm.lease(Deadline(10)) as leased:
            assert not isinstance(leased, dict)
            assert leased.pid != child_pid
    finally:
        warm.retire()


def test_idle_ttl_kills_idle_not_leased(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script, idle_ttl_sec=0.2), supervisor=supervisor)
    try:
        with warm.lease(Deadline(10)) as leased:
            assert not isinstance(leased, dict)
        assert warm.is_alive()
        deadline = time.monotonic() + 3.0
        assert _wait_until(deadline, lambda: not warm.is_alive())
        with warm.lease(Deadline(10)) as leased:
            assert not isinstance(leased, dict)
        assert warm.is_alive()
    finally:
        warm.retire()


def _pid_is_alive_win32(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    access = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    exit_code = ctypes.c_ulong()
    still_active = 259
    alive = (
        bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
        and exit_code.value == still_active
    )
    kernel32.CloseHandle(handle)
    return alive


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_is_alive_win32(int(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_shutdown_all_kills_children_and_refuses_lease(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(
        tmp_path,
        preamble="import time",
        handler_body="time.sleep(60)\nreturn {'status': 'ok'}",
    )
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    with warm.lease(Deadline(10)) as leased:
        assert not isinstance(leased, dict)
        pid = leased.pid
    supervisor.shutdown_all()
    deadline = time.monotonic() + 5.0

    def pid_gone() -> bool:
        return not _pid_is_alive(pid)

    assert _wait_until(deadline, pid_gone)
    with warm.lease(Deadline(5)) as leased:
        assert isinstance(leased, dict)
        assert leased.get("code") == "SERVICE_SHUTDOWN"


def test_shutdown_all_abandons_without_waiting(tmp_path: Any, supervisor: Supervisor) -> None:
    from unittest.mock import MagicMock, patch

    class SlowPopen:
        def __init__(self) -> None:
            self.pid = 99999
            self.stdout = MagicMock()
            self.stdin = MagicMock()
            self.stderr = MagicMock()

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            time.sleep(1.0)
            return 0

        def send_signal(self, sig: int) -> None:
            pass

        def kill(self) -> None:
            pass

        def terminate(self) -> None:
            pass

    proc = SlowPopen()
    managed = ManagedSubprocess.wrap_existing(proc, label="slow-child")
    warm = WarmChild(_spec(tmp_path, "pass"), supervisor=supervisor)
    child = Child(managed, worker_name="test", worker_id=0)
    warm.child = child

    calls: list[str] = []

    def record_wait_once(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        calls.append("wait_once")
        return True

    def record_finish_text(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        calls.append("finish_text")
        return ""

    with patch.object(managed, "wait_once", side_effect=record_wait_once), \
         patch("plugin.framework.managed_subprocess.StderrTail.finish_text", side_effect=record_finish_text):
        supervisor.shutdown_all()

    assert calls == []
    assert warm._retired is True


@pytest.mark.parametrize("threads_started", [False, True], ids=["never_started", "started"])
def test_shutdown_all_sweeps_managed_registry_once(monkeypatch, threads_started: bool) -> None:
    ms_calls = 0

    def _ms_shutdown() -> None:
        nonlocal ms_calls
        ms_calls += 1

    monkeypatch.setattr(
        "plugin.framework.managed_subprocess.shutdown_all",
        _ms_shutdown,
    )
    sup = Supervisor()
    sup._threads_started = threads_started
    sup.shutdown_all()
    assert ms_calls == 1


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "linux", reason="orphan PDEATHSIG check is Linux-only")
def test_orphan_warm_child_dies_with_parent(tmp_path: Any) -> None:
    root = _repo_root()
    child_script = _stdio_worker(
        tmp_path,
        handler_body="import time\ntime.sleep(60)\nreturn {'status': 'ok'}",
    )
    parent_script = tmp_path / "parent_warm.py"
    parent_script.write_text(
        "\n".join(
            [
                "import os, sys, time",
                f"sys.path.insert(0, {root!r})",
                "from plugin.framework.child_runtime import Deadline",
                "from plugin.framework.warm_child import ChildSpec, WarmChild",
                f"script = {child_script!r}",
                "env = {k: v for k, v in os.environ.items() if isinstance(v, str)}",
                "spec = ChildSpec(name='orphan', argv=[sys.executable, script], env=env)",
                "warm = WarmChild(spec)",
                "with warm.lease(Deadline(30)) as leased:",
                "    if isinstance(leased, dict):",
                "        raise SystemExit(2)",
                "    pid = leased.pid",
                "    with open(sys.argv[1], 'w', encoding='utf-8') as f:",
                "        f.write(str(pid))",
                "time.sleep(120)",
            ]
        ),
        encoding="utf-8",
    )
    pid_file = tmp_path / "child.pid"
    parent = subprocess.Popen([sys.executable, str(parent_script), str(pid_file)])
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if pid_file.is_file():
                break
            time.sleep(0.05)
        assert pid_file.is_file(), "parent never recorded child pid"
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        gone_by = time.monotonic() + 5.0
        while time.monotonic() < gone_by:
            try:
                os.kill(child_pid, 0)
            except OSError:
                return
            time.sleep(0.05)
        pytest.fail(f"warm child {child_pid} outlived SIGKILL parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


class _FakePopen:
    def __init__(self) -> None:
        self.pid = 4242
        self.stdout = object()
        self.stdin = object()
        self.stderr = object()

    def poll(self) -> None:
        return None


def _managed_with_popen() -> ManagedSubprocess:
    return ManagedSubprocess.wrap_existing(_FakePopen(), label="spawn-helper-test")


def test_child_slot_refused_signals_only_its_own_child(monkeypatch: pytest.MonkeyPatch) -> None:
    read_calls: list[str] = []

    def fail_read(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        read_calls.append("read")
        raise AssertionError("read_ready_frame_during_io must not run on refused")

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", fail_read)
    sentinel = {"status": "error", "code": "SENTINEL", "message": "refused"}

    class _RefusingSlot(ChildSlot):
        def _publish_refusal(self) -> dict[str, Any] | None:
            return sentinel

    slot = _RefusingSlot()
    outcome = slot._spawn_child(
        _managed_with_popen(),
        worker_name="warm-test",
        worker_id=0,
        start=lambda _m: None,
        deadline=None,
        cap_sec=5.0,
        max_payload_bytes=1024,
        unpacker=None,
    )
    assert outcome.tag == "refused"
    assert outcome.detail is sentinel
    assert read_calls == []
    assert outcome.child is not None
    assert outcome.child.was_signaled()
    assert slot.child is None

    occupied = ChildSlot()
    existing = Child(_managed_with_popen(), worker_name="warm-test", worker_id=0)
    occupied.child = existing
    outcome2 = occupied._spawn_child(
        _managed_with_popen(),
        worker_name="warm-test",
        worker_id=0,
        start=lambda _m: None,
        deadline=None,
        cap_sec=5.0,
        max_payload_bytes=1024,
        unpacker=None,
    )
    assert outcome2.tag == "refused"
    assert outcome2.detail["code"] == "WORKER_SPAWN_FAILED"
    assert not existing.was_signaled()
    assert occupied.child is existing
    assert outcome2.child is not None
    assert outcome2.child.was_signaled()


def test_child_slot_budget_exhausted_kills_without_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    read_calls: list[str] = []

    def fail_read(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        read_calls.append("read")
        raise AssertionError("read_ready_frame_during_io must not run when budget is gone")

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", fail_read)
    monkeypatch.setattr("plugin.framework.warm_child.spawn_budget", lambda deadline: None)
    slot = ChildSlot()
    outcome = slot._spawn_child(
        _managed_with_popen(),
        worker_name="warm-test",
        worker_id=0,
        start=lambda _m: None,
        deadline=Deadline(15.0),
        cap_sec=5.0,
        max_payload_bytes=1024,
        unpacker=None,
    )
    assert outcome.tag == "budget_exhausted"
    assert outcome.published is True
    assert read_calls == []
    assert outcome.child is not None
    assert outcome.child._io_owner is None
    assert outcome.child.was_signaled()
    assert slot.child is None


def test_child_slot_read_exception_reports_failed_and_empties_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    err = RuntimeError("handshake read failed")

    def boom(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        raise err

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", boom)
    slot = ChildSlot()
    outcome = slot._spawn_child(
        _managed_with_popen(),
        worker_name="warm-test",
        worker_id=0,
        start=lambda _m: None,
        deadline=None,
        cap_sec=5.0,
        max_payload_bytes=1024,
        unpacker=None,
    )
    assert outcome.tag == "failed"
    assert outcome.detail is err
    assert outcome.published is True
    assert slot.child is None
    assert outcome.child is not None
    assert outcome.child._io_owner is None
    assert outcome.child.was_signaled()


def test_child_slot_timeout_reports_returncode_before_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimeoutPopen(_FakePopen):
        def __init__(self) -> None:
            super().__init__()
            self._polls = 0

        def poll(self) -> int | None:
            self._polls += 1
            return None if self._polls == 1 else -9

    monkeypatch.setattr(
        "plugin.framework.warm_child.read_ready_frame_during_io",
        lambda *args, **kwargs: ("timeout", None),
    )
    slot = ChildSlot()
    outcome = slot._spawn_child(
        ManagedSubprocess.wrap_existing(_TimeoutPopen(), label="spawn-helper-test"),
        worker_name="warm-test",
        worker_id=0,
        start=lambda _m: None,
        deadline=None,
        cap_sec=5.0,
        max_payload_bytes=1024,
        unpacker=None,
    )
    assert outcome.tag == "timeout"
    assert outcome.detail is None
    assert slot.child is None
    assert outcome.child is not None
    assert outcome.child.was_signaled()


def test_stderr_thread_name() -> None:
    assert stderr_thread_name("Kokoro", fallback="worker", worker_id=1) == "wa-kokoro-w1-stderr"
    assert stderr_thread_name("---", fallback="worker", worker_id=3) == "wa-worker-w3-stderr"
    assert stderr_thread_name("Venv Worker", fallback="warm") == "wa-venv-worker-stderr"
    assert stderr_thread_name("---", fallback="warm") == "wa-warm-stderr"


def test_lease_cancel_aborts_wait_within_poll(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with warm.lease(Deadline(30)) as leased:
            if isinstance(leased, dict):
                return
            holder_ready.set()
            release_holder.wait(timeout=30)

    holder_thread = threading.Thread(target=holder, name="lease-holder")
    holder_thread.start()
    assert holder_ready.wait(timeout=15)
    scope = SendCancellation()

    def flip() -> None:
        time.sleep(0.1)
        scope.cancel()

    threading.Thread(target=flip, daemon=True).start()
    try:
        with warm.lease(
            Deadline(30),
            wait=30,
            cancel=scope,
            poll_sec=0.05,
        ) as leased:
            assert isinstance(leased, dict)
            assert leased.get("code") == "CANCELLED"
    finally:
        release_holder.set()
        holder_thread.join(timeout=15)
        warm.retire()


def test_lease_refuse_aborts_wait_within_poll(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with warm.lease(Deadline(30)) as leased:
            if isinstance(leased, dict):
                return
            holder_ready.set()
            release_holder.wait(timeout=30)

    holder_thread = threading.Thread(target=holder, name="lease-holder")
    holder_thread.start()
    assert holder_ready.wait(timeout=15)
    refuse_at = time.monotonic() + 0.1
    err = {"status": "error", "code": "TEST", "error": "nope"}

    def refuse() -> dict[str, Any] | None:
        if time.monotonic() >= refuse_at:
            return err
        return None

    try:
        with warm.lease(Deadline(30), wait=30, refuse=refuse, poll_sec=0.05) as leased:
            assert leased == err
    finally:
        release_holder.set()
        holder_thread.join(timeout=15)
        warm.retire()


def test_missing_interpreter_lease_raises_oserror(supervisor: Supervisor) -> None:
    env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    spec = ChildSpec(
        name="missing-interpreter",
        argv=["/nonexistent/writeragent-python", "unused.py"],
        env=env,
        log_start_failure=False,
    )
    warm = WarmChild(spec, supervisor=supervisor)
    try:
        with pytest.raises(OSError):
            with warm.lease(Deadline(5.0)) as leased:
                assert not isinstance(leased, dict)
        assert warm.child is None
    finally:
        warm.retire()


def test_warm_ensure_live_interrupted_drops_child(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    drops: list[Child] = []
    orig_drop = warm._drop_child

    def track_drop(child: Child) -> None:
        drops.append(child)
        orig_drop(child)

    monkeypatch.setattr(warm, "_drop_child", track_drop)
    monkeypatch.setattr(
        "plugin.framework.warm_child.read_ready_frame_during_io",
        lambda *args, **kwargs: ("interrupted", None),
    )
    monkeypatch.setattr(
        supervisor, "spawn", lambda managed, timeout_sec=None: ManagedSubprocess.start(managed)
    )

    try:
        err = warm._ensure_live(Deadline(15.0))
        assert isinstance(err, dict)
        assert err.get("code") == "WORKER_SPAWN_FAILED"
        assert drops, "interrupted spawn should drop the child"
    finally:
        warm.retire()


def test_process_worker_respawn_interrupted_drops_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework.process_worker import BaseProcessWorker

    worker = BaseProcessWorker(1, "unused.py", start=False)
    drops: list[Child] = []
    orig_drop = worker._drop_child

    def track_drop(child: Child) -> None:
        drops.append(child)
        orig_drop(child)

    monkeypatch.setattr(worker, "_drop_child", track_drop)
    monkeypatch.setattr(
        "plugin.framework.warm_child.read_ready_frame_during_io",
        lambda *args, **kwargs: ("interrupted", None),
    )

    def fake_start(self: ManagedSubprocess) -> None:
        with self._lock:
            if self._proc is not None:
                raise RuntimeError("ManagedSubprocess is single-use")
            self._proc = _FakePopen()

    monkeypatch.setattr(ManagedSubprocess, "start", fake_start)
    worker.respawn()
    assert drops, "interrupted respawn should drop the child"


def test_supervisor_is_one_process_global() -> None:
    import importlib.machinery
    import importlib.util
    from plugin.framework.warm_child import supervisor

    assert supervisor is sys._writeragent_warm_child_supervisor
    origin = importlib.util.find_spec("plugin.framework.warm_child").origin
    assert origin is not None
    mod_name = "warm_child_reimport_isolated"
    loader = importlib.machinery.SourceFileLoader(mod_name, origin)
    spec = importlib.util.spec_from_loader(mod_name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        loader.exec_module(mod)
    finally:
        sys.modules.pop(mod_name, None)
    assert mod.supervisor is supervisor


def test_blocked_start_lease_returns_worker_spawn_failed_within_budget(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    started = threading.Event()
    stop_event = threading.Event()

    def blocked_start(self: ManagedSubprocess) -> None:
        started.set()
        stop_event.wait(5.0)

    monkeypatch.setattr(ManagedSubprocess, "start", blocked_start)

    t0 = time.monotonic()
    try:
        with warm.lease(Deadline(1.0)) as leased:
            elapsed = time.monotonic() - t0
            assert isinstance(leased, dict)
            assert leased.get("code") == "WORKER_SPAWN_FAILED"
            assert "timed out" in str(leased.get("error", ""))
            assert elapsed < 4.5
    finally:
        stop_event.set()
        warm.retire()


def test_spawn_after_shutdown_raises_and_cannot_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = Supervisor()
    sup._ensure_threads()

    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)
    start_called = False

    def no_start(self: ManagedSubprocess) -> None:
        nonlocal start_called
        start_called = True

    monkeypatch.setattr(ManagedSubprocess, "start", no_start)

    sup.shutdown_all()
    _wait_until(time.monotonic() + 1.0, sup._spawn_queue.empty)

    with pytest.raises(RuntimeError, match="Supervisor is shut down"):
        sup.spawn(managed)

    assert not start_called
    assert not any(isinstance(x, _SpawnRequest) for x in sup._spawn_queue.queue)


def test_spawn_racing_shutdown_all_raises_without_queueing() -> None:
    from plugin.framework.warm_child import _SPAWN_SHUTDOWN

    sup = Supervisor()
    sup._ensure_threads()
    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)

    spawn_error: list[Exception | None] = [None]
    gate_held = threading.Event()
    release_gate = threading.Event()

    def holder() -> None:
        with sup._spawn_gate:
            gate_held.set()
            release_gate.wait(5.0)
            sup._shutdown = True
            sup._spawn_queue.put(_SPAWN_SHUTDOWN)

    t = threading.Thread(target=holder)
    t.start()
    assert gate_held.wait(5.0)

    def try_spawn() -> None:
        try:
            sup.spawn(managed)
        except Exception as exc:
            spawn_error[0] = exc

    t_spawn = threading.Thread(target=try_spawn)
    t_spawn.start()

    release_gate.set()
    t.join(timeout=5.0)
    t_spawn.join(timeout=5.0)

    assert isinstance(spawn_error[0], RuntimeError)
    assert "Supervisor is shut down" in str(spawn_error[0])
    _wait_until(time.monotonic() + 1.0, sup._spawn_queue.empty)
    assert not any(isinstance(x, _SpawnRequest) for x in sup._spawn_queue.queue)
    sup.shutdown_all()


def test_spawn_timeout_kills_a_child_that_appears_after_the_waiter_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = Supervisor()
    start_gate = threading.Event()
    kill_called = threading.Event()

    class _FakeProc:
        def __init__(self) -> None:
            self._killed = False
            self.pid = 99999

        def poll(self) -> int | None:
            return 0 if self._killed else None

        def kill(self) -> None:
            self._killed = True
            kill_called.set()

    fake = _FakeProc()

    def delayed_start(self: ManagedSubprocess) -> None:
        start_gate.wait(5.0)
        self._proc = fake  # type: ignore[assignment]

    monkeypatch.setattr(ManagedSubprocess, "start", delayed_start)

    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)
    try:
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="timed out"):
            sup.spawn(managed, timeout_sec=0.2)
        elapsed = time.monotonic() - t0
        assert elapsed < 4.5

        start_gate.set()
        assert kill_called.wait(2.0), "Late child should be killed when start() completes"
    finally:
        start_gate.set()
        sup.shutdown_all()


def test_spawn_request_give_up_after_finish_returns_false_and_does_not_kill() -> None:
    kill_called = False

    class _FakeProc:
        def kill(self) -> None:
            nonlocal kill_called
            kill_called = True

        def poll(self) -> int | None:
            return 0

    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)
    managed._proc = _FakeProc()  # type: ignore[assignment]
    req = _SpawnRequest(managed)

    req.finish_spawn()
    assert req.done.is_set()
    assert not kill_called

    assert req.give_up() is False
    assert not req.abandoned
    assert not kill_called


def test_spawn_request_finish_after_give_up_kills_child() -> None:
    kill_called = False

    class _FakeProc:
        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            nonlocal kill_called
            kill_called = True

        def poll(self) -> int | None:
            return 0 if self.killed else None

    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)
    managed._proc = _FakeProc()  # type: ignore[assignment]
    req = _SpawnRequest(managed)

    assert req.give_up() is True
    assert req.abandoned
    assert not kill_called

    req.finish_spawn()
    assert kill_called
    assert req.done.is_set()


def test_spawn_request_finish_without_popen_does_not_kill() -> None:
    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)
    assert managed.popen is None
    req = _SpawnRequest(managed)

    assert req.give_up() is True
    assert req.abandoned
    req.finish_spawn()
    assert req.done.is_set()


def test_spawner_skips_start_for_abandoned_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = Supervisor()
    first_gate = threading.Event()
    starts: list[str] = []

    def mock_start(self: ManagedSubprocess) -> None:
        starts.append(self._label)
        if self._label == "first":
            first_gate.wait(5.0)

    monkeypatch.setattr(ManagedSubprocess, "start", mock_start)

    managed1 = ManagedSubprocess([sys.executable, "-c", "pass"], label="first", register=False)
    managed2 = ManagedSubprocess([sys.executable, "-c", "pass"], label="second", register=False)

    t1 = threading.Thread(target=lambda: sup.spawn(managed1, timeout_sec=5.0))
    t1.start()

    _wait_until(time.monotonic() + 3.0, lambda: len(starts) == 1)

    with pytest.raises(RuntimeError, match="Supervisor spawn timed out"):
        sup.spawn(managed2, timeout_sec=0.1)

    first_gate.set()
    t1.join(timeout=5.0)
    assert not t1.is_alive()

    _wait_until(time.monotonic() + 1.0, lambda: sup._spawn_queue.empty())
    assert starts == ["first"]
    sup.shutdown_all()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_child_signal_kills_grandchild() -> None:
    code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(120)\n"
        "    os._exit(0)\n"
        "sys.stdout.write('GRANDCHILD %s\\n' % pid)\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n"
    )
    managed = ManagedSubprocess([sys.executable, "-c", code])
    managed.start()
    child = Child(managed, worker_name="Formula", worker_id=1)
    gpid: int | None = None
    try:
        assert managed.stdout is not None
        line = managed.stdout.readline()
        text = line.decode()
        assert text.startswith("GRANDCHILD "), text
        gpid = int(text.split()[1])

        leader_pid = managed.pid
        assert leader_pid is not None
        assert os.getpgid(leader_pid) == leader_pid

        assert child.signal_from() is True

        def _is_alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _is_alive(gpid):
            time.sleep(0.05)
        assert not _is_alive(gpid), f"grandchild {gpid} was still alive after 5s"
    finally:
        if gpid is not None:
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass
        managed.kill_tree()


@pytest.mark.parametrize("exc_type", [OSError, RuntimeError])
def test_spawn_exception_after_publish_kills_child_and_reports(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)
    recorded: list[Child] = []

    def _boom(child: Child, *_args: Any, **_kwargs: Any) -> Any:
        recorded.append(child)
        raise exc_type("handshake boom")

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", _boom)
    try:
        with warm.lease(Deadline(10.0)) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "WORKER_SPAWN_FAILED"
            assert "handshake boom" in leased["error"]
        assert warm.child is None
        assert len(recorded) == 1
        assert recorded[0].managed.popen is not None
        assert recorded[0].managed.popen.poll() is not None
    finally:
        warm.retire()


def test_spawn_start_runtime_error_before_publish_reports(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _stdio_worker(tmp_path)
    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor)

    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Supervisor spawn timed out")

    monkeypatch.setattr(supervisor, "spawn", _fail)
    try:
        with warm.lease(Deadline(10.0)) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "WORKER_SPAWN_FAILED"
            assert "Supervisor spawn timed out" in leased["error"]
        assert warm.child is None
    finally:
        warm.retire()


def test_cancel_during_publish_is_not_lost(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    scope = SendCancellation()
    scope.register_on_cancel(warm.kill)
    spawned: list[_FakePopen] = []
    cancel_thread: threading.Thread | None = None

    def _fake_spawn(managed: ManagedSubprocess, *, timeout_sec: float | None = None) -> None:
        del timeout_sec
        proc = _FakePopen()
        managed._proc = proc  # type: ignore[assignment]
        spawned.append(proc)

    orig_refusal = warm._publish_refusal

    def _refusal_then_cancel() -> dict[str, Any] | None:
        nonlocal cancel_thread
        refusal = orig_refusal()
        if spawned and cancel_thread is None:
            cancel_thread = threading.Thread(target=scope.cancel)
            cancel_thread.start()
            # kill must block on _lifecycle_lock, which publish holds here, so this join times out.
            # With an unlocked kill it returns, the child is stored afterwards, and the lease below is live.
            cancel_thread.join(timeout=0.2)
        return refusal

    def _ready_after_cancel(child: Child, *_args: Any, **_kwargs: Any) -> tuple[str, Any]:
        del child
        if cancel_thread is not None:
            cancel_thread.join(timeout=5.0)
        return ("ready", {"status": "ready"})

    monkeypatch.setattr(supervisor, "spawn", _fake_spawn)
    monkeypatch.setattr(warm, "_publish_refusal", _refusal_then_cancel)
    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", _ready_after_cancel)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "CANCELLED"
        assert len(spawned) == 1
        assert warm.child is None
    finally:
        if cancel_thread is not None:
            cancel_thread.join(timeout=5.0)
        warm.retire()


def test_cancel_during_spawn_kills_the_unpublished_child(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    scope = SendCancellation()
    scope.register_on_cancel(warm.kill)
    orig_spawn = supervisor.spawn
    popens: list[subprocess.Popen[bytes]] = []

    def _spawn_then_cancel(managed: ManagedSubprocess, *, timeout_sec: float | None = None) -> None:
        orig_spawn(managed, timeout_sec=timeout_sec)
        assert managed.popen is not None
        popens.append(managed.popen)
        scope.cancel()

    monkeypatch.setattr(supervisor, "spawn", _spawn_then_cancel)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "CANCELLED"
        assert warm.child is None
        assert len(popens) == 1
        assert popens[0].poll() is not None
    finally:
        warm.retire()


@pytest.mark.parametrize("when", ["before_ready", "after_ready"])
@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        ("kill", "WORKER_SPAWN_FAILED"),
        ("retire", "SERVICE_SHUTDOWN"),
        ("cancel", "CANCELLED"),
        ("shutdown_all", "SERVICE_SHUTDOWN"),
    ],
)
def test_stop_during_handshake_reports_the_stop(
    tmp_path: Any,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    action: str,
    expected_code: str,
) -> None:
    preamble = "import time\ntime.sleep(60)" if when == "before_ready" else ""
    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path, preamble=preamble)), supervisor=supervisor)
    scope = SendCancellation()
    scope.register_on_cancel(warm.kill)

    def _stop() -> None:
        if action == "kill":
            warm.kill()
        elif action == "retire":
            warm.retire()
        elif action == "cancel":
            scope.cancel()
        else:
            supervisor.shutdown_all()

    orig_read_ready = warm_child_mod.read_ready_frame_during_io
    recorded_children: list[Child] = []
    recorded_tags: list[str] = []
    stop_done: list[bool] = []

    def _hook(child: Child, *args: Any, **kwargs: Any) -> tuple[str, Any]:
        recorded_children.append(child)
        if when == "before_ready":
            timer = threading.Timer(0.2, _stop)
            timer.start()
            try:
                tag, detail = orig_read_ready(child, *args, **kwargs)
            finally:
                timer.join(timeout=5.0)
                stop_done.append(not timer.is_alive())
        else:
            tag, detail = orig_read_ready(child, *args, **kwargs)
            t = threading.Thread(target=_stop)
            t.start()
            t.join(timeout=5.0)
            stop_done.append(not t.is_alive())
        recorded_tags.append(tag)
        return (tag, detail)

    monkeypatch.setattr(warm_child_mod, "read_ready_frame_during_io", _hook)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == expected_code
        if when == "before_ready" and action == "cancel":
            # SendCancellation sets its flag before running the kill hook, so the
            # reader's cancel poll can raise TimeoutExpired before kill() signals.
            assert len(recorded_tags) == 1 and recorded_tags[0] in {"interrupted", "timeout"}
        else:
            assert recorded_tags == (["interrupted"] if when == "before_ready" else ["ready"])
        assert stop_done == [True]
        assert warm.child is None
        assert warm._generation == 0
        assert len(recorded_children) == 1
        popen = recorded_children[0].managed.popen
        assert popen is not None
        assert _wait_until(time.monotonic() + 5.0, lambda: popen.poll() is not None)
    finally:
        warm.retire()


def test_cancel_without_kill_hook_during_hung_handshake_reports_the_cancel(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The venv worker leases with a scope that has no kill hook, so the flag alone
    # must end the read and the "timeout" branch is what kills the hung child.
    warm = WarmChild(
        _spec(tmp_path, _stdio_worker(tmp_path, preamble="import time\ntime.sleep(60)")),
        supervisor=supervisor,
    )
    scope = SendCancellation()
    orig_read_ready = warm_child_mod.read_ready_frame_during_io
    recorded_children: list[Child] = []
    recorded_tags: list[str] = []

    def _cancel_then_read(child: Child, *args: Any, **kwargs: Any) -> tuple[str, Any]:
        recorded_children.append(child)
        scope.cancel()
        tag, detail = orig_read_ready(child, *args, **kwargs)
        recorded_tags.append(tag)
        return (tag, detail)

    monkeypatch.setattr(warm_child_mod, "read_ready_frame_during_io", _cancel_then_read)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "CANCELLED"
        assert recorded_tags == ["timeout"]
        assert warm.child is None
        assert warm._generation == 0
        assert len(recorded_children) == 1
        popen = recorded_children[0].managed.popen
        assert popen is not None
        assert popen.poll() is not None
    finally:
        warm.retire()


@pytest.mark.skipif(sys.platform == "win32", reason="select slices are POSIX only")
def test_cancel_during_hung_handshake_aborts_mid_wait(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plugin.framework.ipc as ipc_mod

    warm = WarmChild(
        _spec(tmp_path, _stdio_worker(tmp_path, preamble="import time\ntime.sleep(60)")),
        supervisor=supervisor,
    )
    scope = SendCancellation()
    calls = 0

    def _mock_select(r: Any, w: Any, x: Any, timeout: float | None = None) -> tuple[list[Any], list[Any], list[Any]]:
        nonlocal calls
        del r, w, x, timeout
        calls += 1
        if calls == 1:
            scope.cancel()
        return ([], [], [])

    orig_read_ready = warm_child_mod.read_ready_frame_during_io
    recorded_children: list[Child] = []

    def _record_child(child: Child, *args: Any, **kwargs: Any) -> tuple[str, Any]:
        recorded_children.append(child)
        return orig_read_ready(child, *args, **kwargs)

    monkeypatch.setattr(ipc_mod.select, "select", _mock_select)
    monkeypatch.setattr(warm_child_mod, "read_ready_frame_during_io", _record_child)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "CANCELLED"
        assert calls <= 2
        assert warm.child is None
        assert warm._generation == 0
        assert len(recorded_children) == 1
        popen = recorded_children[0].managed.popen
        assert popen is not None
        assert popen.poll() is not None
    finally:
        warm.retire()


@pytest.mark.parametrize("missing_interpreter", [False, True])
@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        ("retire", "SERVICE_SHUTDOWN"),
        ("cancel", "CANCELLED"),
        ("shutdown_all", "SERVICE_SHUTDOWN"),
    ],
)
def test_stop_between_refusal_check_and_spawn_reports_the_stop(
    tmp_path: Any,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    missing_interpreter: bool,
    action: str,
    expected_code: str,
) -> None:
    if missing_interpreter:
        spec = ChildSpec(
            name="warm-test",
            argv=["/nonexistent/writeragent-python", "unused.py"],
            env={},
            log_start_failure=False,
        )
    else:
        spec = _spec(tmp_path, _stdio_worker(tmp_path))
    warm = WarmChild(spec, supervisor=supervisor)
    scope = SendCancellation()
    scope.register_on_cancel(warm.kill)
    orig_spawn = supervisor.spawn

    def _stop_then_spawn(managed: ManagedSubprocess, *, timeout_sec: float | None = None) -> None:
        if action == "retire":
            warm.retire()
        elif action == "cancel":
            scope.cancel()
        else:
            supervisor.shutdown_all()
        orig_spawn(managed, timeout_sec=timeout_sec)

    monkeypatch.setattr(supervisor, "spawn", _stop_then_spawn)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == expected_code
        assert warm.child is None
    finally:
        warm.retire()


@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        ("kill", "WORKER_SPAWN_FAILED"),
        ("retire", "SERVICE_SHUTDOWN"),
        ("cancel", "CANCELLED"),
    ],
)
def test_stop_between_ensure_live_and_lease_yields_error(
    tmp_path: Any,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_code: str,
) -> None:
    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    warm.child = Child(_managed_with_popen(), worker_name="warm-test", worker_id=1)
    scope: SendCancellation | None = None
    if action == "cancel":
        scope = SendCancellation()
        scope.register_on_cancel(warm.kill)

    def _ensure_then_stop(_deadline: Deadline) -> dict[str, Any] | None:
        if action == "kill":
            warm.kill()
        elif action == "retire":
            warm.retire()
        else:
            assert scope is not None
            scope.cancel()
        return None

    monkeypatch.setattr(warm, "_ensure_live", _ensure_then_stop)
    try:
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == expected_code
    finally:
        warm.retire()


def test_lease_refuses_signaled_child_not_yet_dropped(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    child = Child(_managed_with_popen(), worker_name="warm-test", worker_id=1)
    warm.child = child

    def _ensure_then_signal(_deadline: Deadline) -> dict[str, Any] | None:
        child._signaled = True
        return None

    monkeypatch.setattr(warm, "_ensure_live", _ensure_then_signal)
    try:
        with warm.lease(Deadline(10.0)) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "WORKER_SPAWN_FAILED"
        assert warm.child is None
    finally:
        warm.retire()


def test_cancel_before_lease_clears_the_slot_refuses_spawn(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    scope = SendCancellation()
    scope.register_on_cancel(warm.kill)
    spawn_mock = MagicMock(wraps=supervisor.spawn)
    monkeypatch.setattr(supervisor, "spawn", spawn_mock)
    try:
        # refuse runs after the first cancel check and before the lock is taken.
        with warm.lease(Deadline(10.0), cancel=scope, refuse=scope.cancel) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "CANCELLED"
        assert warm.child is None
        assert spawn_mock.call_count == 0

        with warm.lease(Deadline(10.0)) as leased2:
            assert not isinstance(leased2, dict)
        assert warm.child is not None
    finally:
        warm.retire()


def test_cancel_of_idle_child_before_ensure_live_does_not_respawn(
    tmp_path: Any, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    try:
        with warm.lease(Deadline(10.0)) as first:
            assert not isinstance(first, dict)
        assert warm._generation == 1

        spawn_mock = MagicMock(wraps=supervisor.spawn)
        monkeypatch.setattr(supervisor, "spawn", spawn_mock)
        scope = SendCancellation()
        scope.register_on_cancel(warm.kill)
        orig_ensure = warm._ensure_live

        def _cancel_then_ensure(deadline: Deadline) -> dict[str, Any] | None:
            scope.cancel()
            return orig_ensure(deadline)

        monkeypatch.setattr(warm, "_ensure_live", _cancel_then_ensure)
        with warm.lease(Deadline(10.0), cancel=scope) as leased:
            assert isinstance(leased, dict)
            assert leased["code"] == "CANCELLED"
        assert warm._generation == 1
        assert warm.child is None
        assert spawn_mock.call_count == 0
    finally:
        warm.retire()


def test_cancelled_scope_of_one_lease_does_not_refuse_the_next(tmp_path: Any, supervisor: Supervisor) -> None:
    warm = WarmChild(_spec(tmp_path, _stdio_worker(tmp_path)), supervisor=supervisor)
    scope1 = SendCancellation()
    scope1.register_on_cancel(warm.kill)
    try:
        # refuse runs after the first cancel check and before the lock is taken.
        with warm.lease(Deadline(10.0), cancel=scope1, refuse=scope1.cancel) as leased1:
            assert isinstance(leased1, dict)
            assert leased1["code"] == "CANCELLED"

        scope2 = SendCancellation()
        with warm.lease(Deadline(10.0), cancel=scope2) as leased2:
            assert not isinstance(leased2, dict)
        assert warm.child is not None
    finally:
        warm.retire()
