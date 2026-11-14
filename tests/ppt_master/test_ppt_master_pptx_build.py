# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.managed_subprocess import (
    ManagedSubprocess,
    _instances,
    _registry_lock,
    shutdown_all,
)
from plugin.framework.send_cancellation import SendCancellation
from plugin.ppt_master.pptx_build import (
    _BUILD_TIMEOUT_SEC,
    build_project_pptx,
    ensure_project_pptx,
    find_project_pptx,
)


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


def pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_is_alive_win32(int(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_grandchild_gone(gpid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_alive(gpid):
            return True
        if sys.platform.startswith("linux"):
            try:
                state = Path(f"/proc/{gpid}/stat").read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                return True
            except OSError:
                state = ""
            if state == "Z":
                return True
        time.sleep(0.05)
    return False


def _wait_for_build_child(timeout: float = 5.0) -> ManagedSubprocess:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _registry_lock:
            for inst in _instances:
                if getattr(inst, "_label", "") == "pptx-build" and inst.is_alive():
                    return inst
        time.sleep(0.02)
    raise TimeoutError("pptx-build child did not appear in registry")


def test_find_project_pptx_prefers_native_over_svg_snapshot(tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    svg_snap = exports / "deck_20260101_120000_svg.pptx"
    native = exports / "deck_20260101_120000.pptx"
    svg_snap.write_bytes(b"svg")
    native.write_bytes(b"native")
    # Make native newer
    os.utime(svg_snap, (time.time() + 10, time.time() + 10))
    assert find_project_pptx(tmp_path) == native


def test_find_project_pptx_falls_back_to_svg_snapshot(tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    svg_snap = exports / "deck_svg.pptx"
    svg_snap.write_bytes(b"svg")
    assert find_project_pptx(tmp_path) == svg_snap


def test_find_project_pptx_missing_exports(tmp_path: Path):
    assert find_project_pptx(tmp_path) is None


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_success(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    exports = tmp_path / "exports"
    exports.mkdir()
    pptx = exports / "proj_20260101.pptx"
    pptx.write_bytes(b"pptx")
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    (data_root / "scripts" / "svg_to_pptx.py").write_text("# stub\n", encoding="utf-8")

    spawned: list[ManagedSubprocess] = []
    orig_start = ManagedSubprocess.start

    def spy_start(self: ManagedSubprocess) -> None:
        orig_start(self)
        spawned.append(self)

    with patch.object(ManagedSubprocess, "start", spy_start):
        path, err = build_project_pptx(None, tmp_path, data_root)

    assert err is None
    assert path == pptx
    assert spawned and spawned[0]._cmd_line[0] == sys.executable
    with _registry_lock:
        assert not any(getattr(inst, "_label", "") == "pptx-build" for inst in _instances)


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_cancel_kills_child(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)

    gc_pid_file = tmp_path / "grandchild.pid"
    script_text = (
        "import sys, time, subprocess\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'grandchild':\n"
        "    time.sleep(30)\n"
        "else:\n"
        f"    proc = subprocess.Popen([sys.executable, sys.argv[0], 'grandchild'])\n"
        f"    with open({str(gc_pid_file)!r}, 'w') as f:\n"
        "        f.write(str(proc.pid))\n"
        "    time.sleep(30)\n"
    )
    (data_root / "scripts" / "svg_to_pptx.py").write_text(script_text, encoding="utf-8")

    cancel = SendCancellation()
    result_holder: list[tuple[Path | None, str | None]] = []

    def run_build() -> None:
        result_holder.append(
            build_project_pptx(None, tmp_path, data_root, cancel=cancel, timeout_sec=30)
        )

    thread = threading.Thread(target=run_build, daemon=True)
    try:
        thread.start()
        target_inst = _wait_for_build_child()
        pid = target_inst.pid

        # Wait for grandchild PID to be written so we test full tree kill
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not gc_pid_file.exists():
            time.sleep(0.02)
        gc_pid = int(gc_pid_file.read_text().strip()) if gc_pid_file.exists() else None

        cancel.cancel()

        thread.join(timeout=10.0)
        assert not thread.is_alive()

        assert result_holder
        path, err = result_holder[0]
        assert path is None
        assert err == "PPTX build stopped."

        assert not target_inst.is_alive()
        with _registry_lock:
            assert target_inst not in _instances
        assert not pid_is_alive(pid)
        if gc_pid is not None:
            assert _wait_grandchild_gone(gc_pid), f"grandchild pid {gc_pid} survived kill_tree"
    finally:
        shutdown_all()
        if gc_pid is not None and sys.platform != "win32":
            try:
                os.kill(gc_pid, 9)
            except OSError:
                pass


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_deadline_kills_child(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    (data_root / "scripts" / "svg_to_pptx.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    spawned: list[ManagedSubprocess] = []
    orig_start = ManagedSubprocess.start

    def spy_start(self: ManagedSubprocess) -> None:
        orig_start(self)
        spawned.append(self)

    try:
        with patch.object(ManagedSubprocess, "start", spy_start):
            path, err = build_project_pptx(None, tmp_path, data_root, timeout_sec=0.5)

        assert path is None
        assert err == "PPTX build failed: timed out after 0.5 seconds"

        assert spawned
        child = spawned[0]
        assert not child.is_alive()
        with _registry_lock:
            assert child not in _instances
        assert not pid_is_alive(child.pid)
    finally:
        shutdown_all()


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_exception_in_poll_kills_child(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    (data_root / "scripts" / "svg_to_pptx.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    spawned: list[ManagedSubprocess] = []
    orig_start = ManagedSubprocess.start

    def spy_start(self: ManagedSubprocess) -> None:
        orig_start(self)
        spawned.append(self)

    call_count = 0
    orig_wait = subprocess.Popen.wait

    def fake_wait(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise KeyboardInterrupt("simulated interrupt")
        return orig_wait(self, *args, **kwargs)

    try:
        with patch.object(ManagedSubprocess, "start", spy_start), patch.object(subprocess.Popen, "wait", fake_wait):
            with pytest.raises(KeyboardInterrupt):
                build_project_pptx(None, tmp_path, data_root, timeout_sec=30)

        assert spawned
        child = spawned[0]
        assert not child.is_alive()
        with _registry_lock:
            assert child not in _instances
        assert not pid_is_alive(child.pid)
    finally:
        shutdown_all()


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_quit_kills_registered_child(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    (data_root / "scripts" / "svg_to_pptx.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    result_holder: list[tuple[Path | None, str | None]] = []

    def run_build() -> None:
        result_holder.append(
            build_project_pptx(None, tmp_path, data_root, timeout_sec=30)
        )

    thread = threading.Thread(target=run_build, daemon=True)
    try:
        thread.start()
        target_inst = _wait_for_build_child()
        pid = target_inst.pid
        with _registry_lock:
            assert target_inst in _instances

        shutdown_all()

        thread.join(timeout=10.0)
        assert not thread.is_alive()
        assert result_holder
        path, err = result_holder[0]
        assert path is None
        assert not target_inst.is_alive()
        with _registry_lock:
            assert target_inst not in _instances
        assert not pid_is_alive(pid)
    finally:
        shutdown_all()


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_nonzero_uses_stderr(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    script_content = "import sys\nsys.stderr.write('boom')\nsys.exit(2)\n"
    (data_root / "scripts" / "svg_to_pptx.py").write_text(script_content, encoding="utf-8")

    path, err = build_project_pptx(None, tmp_path, data_root)
    assert path is None
    assert err is not None
    assert "exit 2" in err
    assert "boom" in err


@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_requires_venv(mock_cfg, tmp_path: Path):
    mock_cfg.return_value = ""
    path, err = build_project_pptx(None, tmp_path, tmp_path)
    assert path is None
    assert err is not None
    assert "venv" in err.lower()


@patch("plugin.ppt_master.pptx_build.build_project_pptx")
def test_ensure_project_pptx_uses_existing(mock_build, tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    existing = exports / "deck.pptx"
    existing.write_bytes(b"x")
    path, err = ensure_project_pptx(None, tmp_path, tmp_path)
    assert path == existing
    assert err is None
    mock_build.assert_not_called()


@patch("plugin.ppt_master.pptx_build.build_project_pptx")
def test_ensure_project_pptx_builds_when_missing(mock_build, tmp_path: Path):
    built = tmp_path / "exports" / "built.pptx"
    mock_build.return_value = (built, None)
    cancel = MagicMock()
    path, err = ensure_project_pptx(None, tmp_path, tmp_path, cancel=cancel)
    assert path == built
    assert err is None
    mock_build.assert_called_once_with(None, tmp_path, tmp_path, cancel=cancel)


def test_build_timeout_leaves_margin_for_tool_timeout():
    from plugin.ppt_master.tools import ExportPresentationProject
    assert _BUILD_TIMEOUT_SEC < ExportPresentationProject.timeout
