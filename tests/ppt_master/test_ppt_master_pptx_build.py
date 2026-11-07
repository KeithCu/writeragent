# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from plugin.framework.managed_subprocess import (
    ManagedSubprocess,
    _instances,
    _registry_lock,
    shutdown_all,
)
from plugin.framework.send_cancellation import SendCancellation
from plugin.ppt_master.pptx_build import (
    _BUILD_POLL_SEC,
    build_project_pptx,
    ensure_project_pptx,
    find_project_pptx,
)


def pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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

    path, err = build_project_pptx(None, tmp_path, data_root)
    assert err is None
    assert path == pptx
    with _registry_lock:
        assert not any(getattr(inst, "_label", "") == "pptx-build" for inst in _instances)


@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config")
def test_build_project_pptx_cancel_kills_child(mock_cfg, mock_resolve, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = sys.executable
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    (data_root / "scripts" / "svg_to_pptx.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    cancel = SendCancellation()
    result_holder: list[tuple[Path | None, str | None]] = []

    def run_build() -> None:
        result_holder.append(
            build_project_pptx(None, tmp_path, data_root, cancel=cancel, timeout_sec=30)
        )

    thread = threading.Thread(target=run_build, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 5.0
        target_inst = None
        while time.monotonic() < deadline:
            with _registry_lock:
                for inst in _instances:
                    if getattr(inst, "_label", "") == "pptx-build" and inst.is_alive():
                        target_inst = inst
                        break
            if target_inst is not None:
                break
            time.sleep(0.02)
        assert target_inst is not None
        pid = target_inst.pid

        cancel_time = time.monotonic()
        cancel.cancel()

        thread.join(timeout=_BUILD_POLL_SEC + 1.0)
        assert not thread.is_alive()
        elapsed = time.monotonic() - cancel_time
        assert elapsed <= _BUILD_POLL_SEC + 1.0

        assert result_holder
        path, err = result_holder[0]
        assert path is None
        assert err == "PPTX build stopped."

        assert not target_inst.is_alive()
        with _registry_lock:
            assert target_inst not in _instances
        assert not pid_is_alive(pid)
    finally:
        shutdown_all()


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
            t0 = time.monotonic()
            path, err = build_project_pptx(None, tmp_path, data_root, timeout_sec=0.5)
            elapsed = time.monotonic() - t0

        assert path is None
        assert err == "PPTX build failed: timed out after 0.5 seconds"
        assert elapsed < 0.5 + _BUILD_POLL_SEC + 1.0

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
        deadline = time.monotonic() + 5.0
        target_inst = None
        while time.monotonic() < deadline:
            with _registry_lock:
                for inst in _instances:
                    if getattr(inst, "_label", "") == "pptx-build" and inst.is_alive():
                        target_inst = inst
                        break
            if target_inst is not None:
                break
            time.sleep(0.02)
        assert target_inst is not None
        pid = target_inst.pid
        with _registry_lock:
            assert target_inst in _instances

        shutdown_all()

        thread.join(timeout=5.0)
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
    path, err = ensure_project_pptx(None, tmp_path, tmp_path)
    assert path == built
    assert err is None
    mock_build.assert_called_once()


def test_export_presentation_project_is_async():
    from plugin.ppt_master.tools import ExportPresentationProject
    assert ExportPresentationProject().is_async() is True


def test_export_presentation_project_timeout_is_600():
    from plugin.ppt_master.tools import ExportPresentationProject
    assert ExportPresentationProject().timeout == 600.0
