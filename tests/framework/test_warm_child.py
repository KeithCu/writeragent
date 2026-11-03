# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
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
from plugin.framework.warm_child import (
    Child,
    ChildSpec,
    Deadline,
    Death,
    Supervisor,
    WarmChild,
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
                "from compute_service.worker_base import run_worker_stdio_loop",
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


def test_handshake_timeout_returns_error(tmp_path: Any, caplog: pytest.LogCaptureFixture, supervisor: Supervisor) -> None:
    script = tmp_path / "slow_ready.py"
    script.write_text(
        "\n".join(
            [
                "import sys, time",
                "sys.stderr.write('not-ready-yet\\n')",
                "sys.stderr.flush()",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    warm = WarmChild(_spec(tmp_path, str(script), ready_timeout_sec=0.4), supervisor=supervisor)
    try:
        with caplog.at_level(logging.ERROR):
            with warm.lease(Deadline(5)) as leased:
                assert isinstance(leased, dict)
                assert leased.get("code") in ("EXECUTION_TIMEOUT", "WORKER_SPAWN_FAILED")
        assert "not-ready-yet" in caplog.text or leased.get("code") == "EXECUTION_TIMEOUT"
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
    first_pid: list[int] = []
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
            first_pid.append(leased.pid)
    finally:
        warm2.retire()
    warm.retire()


def test_death_hook_on_external_sigkill(tmp_path: Any, caplog: pytest.LogCaptureFixture, supervisor: Supervisor) -> None:
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
        with caplog.at_level(logging.WARNING):
            with warm.lease(Deadline(10)) as leased:
                assert not isinstance(leased, dict)
        deadline = time.monotonic() + 5.0
        assert _wait_until(deadline, lambda: len(deaths) == 1)
        assert deaths[0].during == "idle"
        assert deaths[0].returncode == -9
        assert "unexpectedly" in caplog.text.lower() or "died" in caplog.text.lower()
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
    pid_holder: list[int] = []
    try:
        with warm.lease(Deadline(10)) as leased:
            assert not isinstance(leased, dict)
            pid_holder.append(leased.pid)
        supervisor.shutdown_all()
        if pid_holder:
            deadline = time.monotonic() + 5.0

            def pid_gone() -> bool:
                try:
                    os.kill(pid_holder[0], 0)
                    return False
                except OSError:
                    return True

            assert _wait_until(deadline, pid_gone)
        with warm.lease(Deadline(5)) as leased:
            assert isinstance(leased, dict)
            assert leased.get("code") == "SERVICE_SHUTDOWN"
    finally:
        pass


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
                "from plugin.framework.warm_child import ChildSpec, Deadline, WarmChild",
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
    monkeypatch.setattr(supervisor, "spawn", ManagedSubprocess.start)

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
