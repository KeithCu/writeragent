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
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest

from scripts import release_verify

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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


class _VerifyRun(NamedTuple):
    code: int
    run_calls: list[dict[str, Any]]
    seed_mock: MagicMock
    kill_mock: MagicMock
    rmtree_mock: MagicMock


def _run_verify_with_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    platform: str,
    lo_python: str,
) -> _VerifyRun:
    monkeypatch.setattr(sys, "platform", platform)
    bundle_root = tmp_path / "bundle"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(bundle_root))

    run_calls: list[dict[str, Any]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    seed_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_profile, "seed", seed_mock)
    kill_mock = MagicMock()
    monkeypatch.setattr(release_verify.lo_kill, "kill", kill_mock)
    monkeypatch.setattr(release_verify.lo_kill, "office_pids", MagicMock(return_value=[1234]))
    monkeypatch.setattr(release_verify.compileall, "compile_dir", MagicMock(return_value=True))
    monkeypatch.setattr(shutil, "copy2", MagicMock())
    rmtree_mock = MagicMock()
    monkeypatch.setattr(shutil, "rmtree", rmtree_mock)

    code = release_verify.main(["--lo-python", lo_python])
    return _VerifyRun(code, run_calls, seed_mock, kill_mock, rmtree_mock)


@pytest.mark.parametrize(
    ("platform", "lo_python", "profile_parts"),
    [
        ("linux", "/usr/bin/python3", ("home", ".config", "libreoffice", "4")),
        (
            "darwin",
            "/Applications/LibreOffice.app/Contents/Resources/python",
            ("home", "Library", "Application Support", "LibreOffice", "4"),
        ),
        ("win32", r"C:\Program Files\LibreOffice\program\python.exe", ("h", "LibreOffice", "4")),
    ],
)
def test_platform_profile_and_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    lo_python: str,
    profile_parts: tuple[str, ...],
) -> None:
    if platform == "darwin":
        # The UNO vars come from lo_paths (derived from the .app), not from the caller's env.
        monkeypatch.delenv("LO_PYTHON_ENV", raising=False)
        monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    elif platform == "win32":
        monkeypatch.setattr(release_verify.lo_paths, "build_child_env", lambda _py: {})

    run = _run_verify_with_mocks(monkeypatch, tmp_path, platform=platform, lo_python=lo_python)
    assert run.code == 0

    bundle_root = tmp_path / "bundle"
    seed_root = tmp_path / "bundle.seed"
    expected_profile = seed_root.joinpath(*profile_parts)
    run.seed_mock.assert_called_once_with(seed_root / "WriterAgent.oxt", expected_profile)

    uno_call = run.run_calls[-1]
    assert uno_call["cwd"] == str(bundle_root)
    uno_env = uno_call["env"]
    assert uno_env["PYTHONPATH"].split(os.pathsep)[0] == str(bundle_root)
    if platform == "darwin":
        contents = Path("/Applications/LibreOffice.app/Contents")
        assert "/somewhere/else" not in uno_env["PYTHONPATH"]
        assert uno_env["UNO_PATH"] == str(contents / "MacOS")
        assert uno_env["URE_BOOTSTRAP"].startswith("vnd.sun.star.pathname:" + str(contents / "Resources"))
    elif platform == "win32":
        assert uno_env["UserInstallation"] == expected_profile.as_uri()
        win_home = seed_root / "h"
        assert uno_env["APPDATA"] == str(win_home)
        assert uno_env.get("USERPROFILE") != str(win_home)
    else:
        assert uno_env["WRITERAGENT_UNO_SEED_PROFILE"] == str(expected_profile)
        assert uno_env["WRITERAGENT_PROJECT_ROOT"] == str(PROJECT_ROOT)

    run.kill_mock.assert_called_once_with([1234])
    assert {c.args[0] for c in run.rmtree_mock.call_args_list} == {bundle_root, seed_root}


def test_uno_env_isolation_and_pytest_bundle_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/fake/venv")
    monkeypatch.setenv("PYTHONHOME", "/fake/home")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/fake/launcher")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    run = _run_verify_with_mocks(monkeypatch, tmp_path, platform="linux", lo_python="/usr/bin/python3")
    assert run.code == 0

    bundle_root = tmp_path / "bundle"
    pytest_calls = [c for c in run.run_calls if "-m" in c["cmd"] and "pytest" in c["cmd"]]
    assert len(pytest_calls) == 1
    assert pytest_calls[0]["cwd"] == str(bundle_root)
    assert pytest_calls[0]["env"]["PYTHONPATH"] == str(bundle_root)
    assert pytest_calls[0]["env"]["VIRTUAL_ENV"] == "/fake/venv"

    uno_env = run.run_calls[-1]["env"]
    assert "VIRTUAL_ENV" not in uno_env
    assert "PYTHONHOME" not in uno_env
    assert "__PYVENV_LAUNCHER__" not in uno_env
    assert uno_env["HOME"] == str(tmp_path / "bundle.seed" / "home")
    assert uno_env["XDG_CONFIG_HOME"] == str(tmp_path / "bundle.seed" / "home" / ".config")


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
    assert {c.args[0] for c in rmtree_mock.call_args_list} == {bundle_root, tmp_path / "bundle.seed"}


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
    assert {c.args[0] for c in rmtree_mock.call_args_list} == {bundle_root, tmp_path / "bundle.seed"}


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
    assert {c.args[0] for c in rmtree_mock.call_args_list} == {bundle_root, tmp_path / "bundle.seed"}


def test_windows_office_logs_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import scripts.release_verify as rv

    env: dict[str, str] = {}
    monkeypatch.setattr(rv.sys, "platform", "linux")
    assert rv._add_windows_office_logs(env, tmp_path) is None
    assert env == {}
    monkeypatch.setattr(rv.sys, "platform", "win32")
    diag = rv._add_windows_office_logs(env, tmp_path)
    assert diag == tmp_path / "diag" and diag.is_dir()
    assert env["PYUNO_LOGLEVEL"] == "CALL"
    assert env["PYUNO_LOGTARGET"] == (diag / "pyuno").as_uri()
