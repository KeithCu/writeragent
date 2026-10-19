# WriterAgent — tests for scripts/release_verify.py
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for release bundle verification script."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import release_verify

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_windows_fails_early_and_names_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    mkdtemp_mock = MagicMock()
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_mock)
    subproc_mock = MagicMock()
    monkeypatch.setattr(subprocess, "run", subproc_mock)

    code = release_verify.main([])
    assert code == 2
    captured = capsys.readouterr()
    assert r"%APPDATA%\LibreOffice\4" in captured.err
    mkdtemp_mock.assert_not_called()
    subproc_mock.assert_not_called()


def test_missing_lo_python_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("LO_PYTHON", raising=False)
    subproc_mock = MagicMock()
    monkeypatch.setattr(subprocess, "run", subproc_mock)

    code = release_verify.main(["--lo-python", ""])
    assert code == 1
    captured = capsys.readouterr()
    assert "LO_PYTHON is not set" in captured.err
    subproc_mock.assert_not_called()


def test_probe_failure_exits_1_without_build_oxt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    run_mock = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=1, stderr="fail"))
    monkeypatch.setattr(subprocess, "run", run_mock)

    code = release_verify.main(["--lo-python", "/custom/python"])
    assert code == 1
    captured = capsys.readouterr()
    assert "cannot import officehelper and uno" in captured.err
    assert run_mock.call_count == 1
    called_cmd = run_mock.call_args[0][0]
    assert "build_oxt.py" not in " ".join(called_cmd)


def test_linux_release_verify_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    bundle_root = tmp_path / "bundle"
    seed_root = tmp_path / "bundle.seed"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))

    monkeypatch.setenv("VIRTUAL_ENV", "/fake/venv")
    monkeypatch.setenv("PYTHONHOME", "/fake/home")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/fake/launcher")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    run_calls: list[dict[str, Any]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    seed_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_profile, "seed", seed_mock)
    kill_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_kill, "kill", kill_mock)
    office_pids_mock = MagicMock(return_value=[1234])
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", office_pids_mock)
    compile_mock = MagicMock(return_value=True)
    monkeypatch.setattr(release_verify.compileall, "compile_dir", compile_mock)
    copy_mock = MagicMock()
    monkeypatch.setattr(shutil, "copy2", copy_mock)
    rmtree_mock = MagicMock()
    monkeypatch.setattr(shutil, "rmtree", rmtree_mock)

    code = release_verify.main(["--lo-python", "/usr/bin/python3"])
    assert code == 0

    expected_profile = seed_root / "home" / ".config" / "libreoffice" / "4"
    expected_oxt = seed_root / "WriterAgent.oxt"
    seed_mock.assert_called_once_with(expected_oxt, expected_profile)

    uno_call = run_calls[-1]
    assert uno_call["cmd"] == ["/usr/bin/python3", "-u", "-m", "plugin.testing_runner"]
    assert uno_call["cwd"] == str(bundle_root)
    uno_env = uno_call["env"]
    assert uno_env["HOME"] == str(seed_root / "home")
    assert uno_env["XDG_CONFIG_HOME"] == str(seed_root / "home" / ".config")
    assert uno_env["WRITERAGENT_UNO_SEED_PROFILE"] == str(expected_profile)
    assert uno_env["WRITERAGENT_PROJECT_ROOT"] == str(PROJECT_ROOT)
    assert uno_env["PYTHONPATH"] == str(bundle_root)
    assert "VIRTUAL_ENV" not in uno_env
    assert "PYTHONHOME" not in uno_env
    assert "__PYVENV_LAUNCHER__" not in uno_env

    office_pids_mock.assert_called_once_with(None)
    kill_mock.assert_called_once_with([1234])
    assert rmtree_mock.call_count == 2


def test_macos_profile_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle_root = tmp_path / "bundle"
    seed_root = tmp_path / "bundle.seed"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))

    # The UNO vars come from lo_paths (derived from the .app), not from the caller's env.
    monkeypatch.delenv("LO_PYTHON_ENV", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")

    run_calls: list[dict[str, Any]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    seed_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_profile, "seed", seed_mock)
    monkeypatch.setattr(release_verify.lo_kill, "kill", MagicMock())
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", MagicMock(return_value=[]))
    monkeypatch.setattr(release_verify.compileall, "compile_dir", MagicMock(return_value=True))
    monkeypatch.setattr(shutil, "copy2", MagicMock())
    monkeypatch.setattr(shutil, "rmtree", MagicMock())

    code = release_verify.main(["--lo-python", "/Applications/LibreOffice.app/Contents/Resources/python"])
    assert code == 0

    expected_profile = (
        seed_root / "home" / "Library" / "Application Support" / "LibreOffice" / "4"
    )
    expected_oxt = seed_root / "WriterAgent.oxt"
    seed_mock.assert_called_once_with(expected_oxt, expected_profile)

    uno_call = run_calls[-1]
    uno_env = uno_call["env"]
    contents = Path("/Applications/LibreOffice.app/Contents")
    assert uno_env["PYTHONPATH"] == os.pathsep.join([str(bundle_root), str(contents / "Resources"), str(contents / "Frameworks")])
    assert uno_env["UNO_PATH"] == str(contents / "MacOS")
    assert uno_env["URE_BOOTSTRAP"].startswith("vnd.sun.star.pathname:" + str(contents / "Resources"))


def test_execution_order_and_pytest_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    bundle_root = tmp_path / "bundle"
    seed_root = tmp_path / "bundle.seed"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))
    monkeypatch.setenv("VIRTUAL_ENV", "/my/dev/venv")

    event_order: list[str] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "officehelper" in cmd_str:
            event_order.append("probe")
        elif "--strip" in cmd:
            event_order.append("strip")
        elif "--repack" in cmd:
            event_order.append("repack")
            assert str(seed_root / "WriterAgent.oxt") in cmd
        elif "-m pytest" in cmd_str:
            event_order.append("pytest")
            assert kwargs["env"]["VIRTUAL_ENV"] == "/my/dev/venv"
            assert kwargs["env"]["PYTHONPATH"] == str(bundle_root)
            assert kwargs["cwd"] == str(bundle_root)
            for ignored in release_verify.IGNORED_TESTS:
                assert f"--ignore={ignored}" in cmd
            assert "-k" in cmd
            k_expr = "not " + " and not ".join(release_verify.DESELECTED_K)
            assert k_expr in cmd
        elif "-m" in cmd and "plugin.testing_runner" in cmd:
            event_order.append("runner")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(
        release_verify.lo_profile, "seed", lambda oxt, profile: event_order.append("seed")
    )
    monkeypatch.setattr(
        release_verify.compileall, "compile_dir", lambda *a, **k: event_order.append("compileall") or True
    )
    monkeypatch.setattr(
        shutil, "copy2", lambda src, dst: event_order.append("copy")
    )
    monkeypatch.setattr(release_verify.lo_kill, "kill", MagicMock())
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", MagicMock(return_value=[]))
    monkeypatch.setattr(shutil, "rmtree", MagicMock())

    code = release_verify.main(["--lo-python", "/usr/bin/python3"])
    assert code == 0

    assert event_order == [
        "probe",
        "strip",
        "repack",
        "seed",
        "compileall",
        "compileall",
        "copy",
        "pytest",
        "runner",
    ]


def test_pytest_failure_aborts_uno_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    bundle_root = tmp_path / "bundle"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "-m pytest" in cmd_str:
            return subprocess.CompletedProcess(args=cmd, returncode=42)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(release_verify.lo_profile, "seed", MagicMock())
    monkeypatch.setattr(release_verify.compileall, "compile_dir", MagicMock(return_value=True))
    monkeypatch.setattr(shutil, "copy2", MagicMock())
    kill_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_kill, "kill", kill_mock)
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", MagicMock(return_value=[555]))
    rmtree_mock = MagicMock()
    monkeypatch.setattr(shutil, "rmtree", rmtree_mock)

    code = release_verify.main(["--lo-python", "/usr/bin/python3"])
    assert code == 42
    kill_mock.assert_called_once_with([555])
    assert rmtree_mock.call_count == 2


def test_uno_failure_returns_code_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    bundle_root = tmp_path / "bundle"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-m" in cmd and "plugin.testing_runner" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=17)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(release_verify.lo_profile, "seed", MagicMock())
    monkeypatch.setattr(release_verify.compileall, "compile_dir", MagicMock(return_value=True))
    monkeypatch.setattr(shutil, "copy2", MagicMock())
    kill_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_kill, "kill", kill_mock)
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", MagicMock(return_value=[]))
    rmtree_mock = MagicMock()
    monkeypatch.setattr(shutil, "rmtree", rmtree_mock)

    code = release_verify.main(["--lo-python", "/usr/bin/python3"])
    assert code == 17
    kill_mock.assert_called_once()
    assert rmtree_mock.call_count == 2


def test_seed_runtime_error_returns_1_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    bundle_root = tmp_path / "bundle"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)

    def failing_seed(oxt: Path, profile: Path) -> None:
        raise RuntimeError("cannot seed into profile")

    monkeypatch.setattr(release_verify.lo_profile, "seed", failing_seed)
    kill_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_kill, "kill", kill_mock)
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", MagicMock(return_value=[]))
    rmtree_mock = MagicMock()
    monkeypatch.setattr(shutil, "rmtree", rmtree_mock)

    code = release_verify.main(["--lo-python", "/usr/bin/python3"])
    assert code == 1
    kill_mock.assert_called_once()
    assert rmtree_mock.call_count == 2
