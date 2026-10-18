#!/usr/bin/env python3
# WriterAgent — verify release bundle in a clean temporary tree
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify release bundle in a clean temporary tree.

Builds a stripped extension bundle in a temporary directory, registers it into
a scratch HOME profile, compiles bytecode with compileall, and runs both unit
and UNO tests against the stripped tree before release packaging.
"""

from __future__ import annotations

import argparse
import compileall
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import lo_kill, lo_profile  # noqa: E402

_SCRUB = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")

IGNORED_TESTS = (
    "tests/scripts",
    "tests/compute_service",
    "tests/librepy/test_main_core.py",
    "tests/scripts/test_merge_module_yaml_into_pot.py",
    "tests/framework/test_logging.py",
    "tests/writer/locale/test_grammar_linguistic_xcu.py",
    "tests/scripting/test_generate_tool_proxies.py",
    "tests/framework/test_thread_guard.py",
    "tests/framework/test_thread_affinity.py",
    "tests/doc/test_specialized_delegation_threading.py",
    "tests/writer/locale/test_grammar_obs.py",
    "tests/writer/locale/test_libreharper_oxt.py",
    "tests/chatbot/test_sidebar_test_hooks.py",
)

DESELECTED_K = (
    "test_sync_tool_marshaled_from_background",
    "test_execute_on_main_thread_timeout",
    "test_execute_python_addin_from_background_thread",
)


def _make_uno_env(
    base_dir: Path,
    home: Path | None = None,
    profile: Path | None = None,
) -> dict[str, str]:
    # UNO child environment scrubbed of host virtualenv leaks
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONUNBUFFERED"] = "1"
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    if inherited_pythonpath:
        env["PYTHONPATH"] = f"{base_dir}{os.pathsep}{inherited_pythonpath}"
    else:
        env["PYTHONPATH"] = str(base_dir)
    if home is not None:
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["WRITERAGENT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    if profile is not None:
        env["WRITERAGENT_UNO_SEED_PROFILE"] = str(profile)
    return env


def main(argv: list[str]) -> int:
    # Windows LibreOffice runner uses %APPDATA%\LibreOffice\4 and ignores HOME
    if sys.platform == "win32":
        sys.stderr.write(
            "error: the runner uses %APPDATA%\\LibreOffice\\4 and Windows ignores HOME, "
            "so this would write the real profile\n"
        )
        return 2

    parser = argparse.ArgumentParser(description="Verify release bundle in a clean temporary tree.")
    parser.add_argument(
        "--lo-python",
        default=os.environ.get("LO_PYTHON", ""),
        help="Path to LibreOffice python",
    )
    args = parser.parse_args(argv)

    lo_python = args.lo_python.strip()
    if not lo_python:
        sys.stderr.write(
            "error: LO_PYTHON is not set and could not be detected.\n"
            "  macOS: /Applications/LibreOffice.app/Contents/Resources/python\n"
            "  Linux: distro python3 with python3-uno installed\n"
            "  Windows: LibreOffice/program/python.exe\n"
            "Set LO_PYTHON to LibreOffice's bundled interpreter — not the project .venv.\n"
        )
        return 1

    probe_env = _make_uno_env(PROJECT_ROOT)
    probe_cmd = [
        lo_python,
        "-c",
        "from plugin.testing_runner import _ensure_libreoffice_python_path; "
        "_ensure_libreoffice_python_path(); import officehelper, uno",
    ]
    proc = subprocess.run(probe_cmd, cwd=str(PROJECT_ROOT), env=probe_env, check=False)
    if proc.returncode != 0:
        sys.stderr.write(
            f"error: {lo_python} cannot import officehelper and uno.\n"
            "On macOS use Contents/Resources/python (sets URE_BOOTSTRAP / PYTHONPATH)\n"
            "or set those vars yourself. officehelper.py lives in Contents/Resources.\n"
            "Do not fall back to the project .venv — that CPython cannot host pyuno.\n"
        )
        return 1

    root = Path(tempfile.mkdtemp(prefix="writeragent-release-"))
    seed_dir = Path(str(root) + ".seed")
    home = seed_dir / "home"
    seed_dir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        profile = home / "Library" / "Application Support" / "LibreOffice" / "4"
    else:
        profile = home / ".config" / "libreoffice" / "4"

    try:
        build_script = str(PROJECT_ROOT / "scripts" / "build_oxt.py")
        oxt_path = seed_dir / "WriterAgent.oxt"

        proc = subprocess.run(
            [sys.executable, build_script, "--strip", "--bundle-dir", str(root), "--skip-zip"],
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode

        proc = subprocess.run(
            [sys.executable, build_script, "--repack", "--bundle-dir", str(root), "--output", str(oxt_path)],
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode

        try:
            lo_profile.seed(oxt_path, profile)
        except (RuntimeError, ValueError) as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1

        ok_plugin = compileall.compile_dir(str(root / "plugin"), quiet=1, workers=0)
        ok_tests = compileall.compile_dir(str(root / "tests"), quiet=1, workers=0)
        if not (ok_plugin and ok_tests):
            sys.stderr.write("error: compileall failed on stripped bundle\n")
            return 1

        shutil.copy2(PROJECT_ROOT / "pyproject.toml", root / "pyproject.toml")

        pytest_cmd = [sys.executable, "-m", "pytest"]
        for ignore in IGNORED_TESTS:
            pytest_cmd.append(f"--ignore={ignore}")
        k_expr = "not " + " and not ".join(DESELECTED_K)
        pytest_cmd.extend(["-k", k_expr, "tests"])

        pytest_env = os.environ.copy()
        pytest_env["PYTHONPATH"] = str(root)
        proc = subprocess.run(pytest_cmd, cwd=str(root), env=pytest_env, check=False)
        if proc.returncode != 0:
            return proc.returncode

        uno_cmd = [lo_python, "-u", "-m", "plugin.testing_runner"]
        uno_env = _make_uno_env(root, home=home, profile=profile)
        proc = subprocess.run(uno_cmd, cwd=str(root), env=uno_env, check=False)
        if proc.returncode != 0:
            return proc.returncode

        return 0
    finally:
        lo_kill.kill(lo_kill.office_pids(None))
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(seed_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
