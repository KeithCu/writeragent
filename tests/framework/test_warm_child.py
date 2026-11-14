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

from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.child_runtime import Deadline
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.warm_child import (
    Child,
    ChildSpec,
    Death,
    Supervisor,
    WarmChild,
    _SpawnRequest,
    spawn_managed_child_with_handshake,
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


def test_handshake_timeout_returns_error(tmp_path: Any, supervisor: Supervisor) -> None:
    script = tmp_path / "slow_ready.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    warm = WarmChild(_spec(tmp_path, str(script), ready_timeout_sec=0.4), supervisor=supervisor)
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
    warm = WarmChild(_spec(tmp_path, str(script), ready_timeout_sec=5), supervisor=supervisor)
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
    warm = WarmChild(_spec(tmp_path, str(script), ready_timeout_sec=30), supervisor=supervisor)
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
    assert warm._child is None or not warm.is_alive()
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
def test_death_hook_on_external_sigkill(tmp_path: Any, supervisor: Supervisor) -> None:
    script = _stdio_worker(tmp_path)
    deaths: list[Death] = []

    def on_death(d: Death) -> None:
        deaths.append(d)

    warm = WarmChild(_spec(tmp_path, script), supervisor=supervisor, on_death=on_death)
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
        deadline = time.monotonic() + 5.0
        assert _wait_until(deadline, lambda: len(deaths) == 1)
        assert deaths[0].during == "idle"
        assert deaths[0].returncode == -9
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
        try:
            os.kill(pid, 0)
            return False
        except OSError:
            return True

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
    warm._child = child

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


def test_shutdown_all_sweeps_managed_registry_when_spawner_never_started(monkeypatch) -> None:
    ms_calls = 0

    def _ms_shutdown() -> None:
        nonlocal ms_calls
        ms_calls += 1

    monkeypatch.setattr(
        "plugin.framework.managed_subprocess.shutdown_all",
        _ms_shutdown,
    )
    sup = Supervisor()
    assert not sup._threads_started
    sup.shutdown_all()
    assert ms_calls == 1


def test_shutdown_all_sweeps_managed_registry_once_when_spawner_started(monkeypatch) -> None:
    ms_calls = 0

    def _ms_shutdown() -> None:
        nonlocal ms_calls
        ms_calls += 1

    monkeypatch.setattr(
        "plugin.framework.managed_subprocess.shutdown_all",
        _ms_shutdown,
    )
    sup = Supervisor()
    sup._threads_started = True
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


def test_spawn_helper_refused_signals_without_pipe_io(monkeypatch: pytest.MonkeyPatch) -> None:
    read_calls: list[str] = []

    def fail_read(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        read_calls.append("read")
        raise AssertionError("read_ready_frame_during_io must not run on refused")

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", fail_read)
    managed = _managed_with_popen()
    tag, extra, spawned = spawn_managed_child_with_handshake(
        managed,
        worker_name="warm-test",
        worker_id=0,
        is_alive=lambda: True,
        max_payload_bytes=1024,
        unpacker=None,
        start=lambda unused: None,
        publish=lambda unused: False,
        deadline=None,
        cap_sec=5.0,
    )
    assert tag == "refused"
    assert extra is None
    assert read_calls == []
    assert spawned.was_signaled()


def test_spawn_helper_budget_exhausted_skips_begin_io(monkeypatch: pytest.MonkeyPatch) -> None:
    read_calls: list[str] = []

    def fail_read(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        read_calls.append("read")
        raise AssertionError("read_ready_frame_during_io must not run when budget is gone")

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", fail_read)
    monkeypatch.setattr("plugin.framework.warm_child.spawn_budget", lambda deadline: None)
    managed = _managed_with_popen()
    tag, extra, spawned = spawn_managed_child_with_handshake(
        managed,
        worker_name="warm-test",
        worker_id=0,
        is_alive=lambda: True,
        max_payload_bytes=1024,
        unpacker=None,
        start=lambda unused: None,
        publish=lambda unused: True,
        deadline=Deadline(15.0),
        cap_sec=5.0,
    )
    assert tag == "budget_exhausted"
    assert extra is None
    assert read_calls == []
    assert spawned._io_owner is None


def test_spawn_helper_read_exception_clears_io_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        raise RuntimeError("handshake read failed")

    monkeypatch.setattr("plugin.framework.warm_child.read_ready_frame_during_io", boom)
    managed = _managed_with_popen()
    published: list[Child] = []

    def publish(child: Child) -> bool:
        published.append(child)
        return True

    with pytest.raises(RuntimeError, match="handshake read failed"):
        spawn_managed_child_with_handshake(
            managed,
            worker_name="warm-test",
            worker_id=0,
            is_alive=lambda: True,
            max_payload_bytes=1024,
            unpacker=None,
            start=lambda unused: None,
            publish=publish,
            deadline=None,
            cap_sec=5.0,
        )
    assert len(published) == 1
    spawned = published[0]
    assert spawned._io_owner is not None
    spawned.end_io()
    assert spawned._io_owner is None


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
        assert warm._child is None
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
    import sys

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
    from plugin.framework.warm_child import _SpawnRequest

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

    req.finish()
    assert req.done.is_set()
    assert not kill_called

    assert req.give_up() is False
    assert not req.abandoned
    assert not kill_called


def test_spawn_request_finish_after_give_up_kills_child() -> None:
    from plugin.framework.warm_child import _SpawnRequest

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

    req.finish()
    assert kill_called
    assert req.done.is_set()


def test_spawn_request_finish_without_popen_does_not_kill() -> None:
    from plugin.framework.warm_child import _SpawnRequest

    managed = ManagedSubprocess([sys.executable, "-c", "pass"], register=False)
    assert managed.popen is None
    req = _SpawnRequest(managed)

    assert req.give_up() is True
    assert req.abandoned
    req.finish()
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
