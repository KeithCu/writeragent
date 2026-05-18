# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Resolve a venv directory to its python executable and run a warm-worker self-check."""

from __future__ import annotations

import os
import sys
from typing import Any, Optional, Tuple

from plugin.scripting.python_worker_manager import PythonWorkerManager
from plugin.scripting.subprocess_env import scrub_subprocess_env


def resolve_libreoffice_python() -> Optional[str]:
    """Return ``sys.executable`` if it names a real file (no other heuristics).

    Under PyUNO this is normally the office-bundled Python; on broken installs it
    may be wrong or missing — callers surface an error and the user can set a venv.
    """
    exe = (getattr(sys, "executable", None) or "").strip()
    if not exe or not os.path.isfile(exe):
        return None
    if os.name != "nt" and not os.access(exe, os.X_OK):
        return None
    return exe


def resolve_venv_python(venv_dir: str) -> Optional[str]:
    """Return the python executable inside *venv_dir*, or None if missing or not a file."""
    if not venv_dir or not venv_dir.strip():
        return None
    expanded = os.path.expanduser(os.path.expandvars(venv_dir.strip()))
    if os.name == "nt":
        candidate = os.path.join(expanded, "Scripts", "python.exe")
    else:
        candidate = os.path.join(expanded, "bin", "python")
    if os.path.isfile(candidate):
        return candidate
    return None


_DIAGNOSTIC_SCRIPT = (
    "import platform\n"
    "res = {'v': platform.python_version()}\n"
    "pkgs = {}\n"
    "try:\n"
    "    import numpy\n"
    "    pkgs['numpy'] = 'present'\n"
    "except ImportError:\n"
    "    pkgs['numpy'] = None\n"
    "try:\n"
    "    import pandas\n"
    "    pkgs['pandas'] = 'present'\n"
    "except ImportError:\n"
    "    pkgs['pandas'] = None\n"
    "try:\n"
    "    import scipy\n"
    "    pkgs['scipy'] = 'present'\n"
    "except ImportError:\n"
    "    pkgs['scipy'] = None\n"
    "try:\n"
    "    import sklearn\n"
    "    pkgs['sklearn'] = 'present'\n"
    "except ImportError:\n"
    "    pkgs['sklearn'] = None\n"
    "try:\n"
    "    import matplotlib\n"
    "    pkgs['matplotlib'] = 'present'\n"
    "except ImportError:\n"
    "    pkgs['matplotlib'] = None\n"
    "res['p'] = pkgs\n"
    "result = res"
)


def _format_self_check_success(data: dict[str, Any]) -> str:
    version = data.get("v", "unknown")
    packages = data.get("p", {})
    if not isinstance(packages, dict):
        packages = {}

    msg_lines = [f"Python {version} responds OK."]

    found = []
    missing = []
    requested = ["numpy", "pandas"]
    others = [p for p in packages if p not in requested]

    for p in requested + others:
        ver = packages.get(p)
        if ver:
            found.append(f"{p} ({ver})" if ver != "present" else p)
        else:
            missing.append(p)

    if found:
        msg_lines.append(f"Packages: {', '.join(found)}")
    if missing:
        msg_lines.append(f"Missing: {', '.join(missing)}")

    return "\n".join(msg_lines)


def run_venv_self_check(python_exe: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Run a diagnostic script via the warm worker; return (success, user-facing message)."""
    timeout_sec = max(1, int(timeout))
    try:
        manager = PythonWorkerManager.get(python_exe, scrub_subprocess_env(dict(os.environ)))
        response = manager.execute(_DIAGNOSTIC_SCRIPT, timeout_sec=timeout_sec)
    except OSError as e:
        return False, f"Could not run Python: {e}"

    if response.get("status") != "ok":
        msg = str(response.get("message", "Unknown error"))
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return False, "Timed out waiting for Python (check venv and try again)."
        return False, msg

    data = response.get("result")
    if not isinstance(data, dict):
        return False, f"Unexpected output from test run: {data!r}"

    try:
        return True, _format_self_check_success(data)
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def probe_venv_path(venv_dir: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Resolve *venv_dir* and run a self-check; single entry for UI and tests."""
    if not venv_dir or not str(venv_dir).strip():
        exe = resolve_libreoffice_python()
        if not exe:
            return False, "No process interpreter: sys.executable is missing, not a file, or not executable. Set a venv path in Settings → Python, or fix the LibreOffice install."
        ok, msg = run_venv_self_check(exe, timeout=timeout)
        if ok:
            return True, f"LibreOffice process Python ({exe}) responds OK."
        return ok, msg
    expanded = os.path.expanduser(os.path.expandvars(str(venv_dir).strip()))
    if not os.path.isdir(expanded):
        return False, f"Not a directory: {expanded}"

    exe = resolve_venv_python(expanded)
    if not exe:
        return False, "No python found (expected bin/python or Scripts\\python.exe under that path)."
    return run_venv_self_check(exe, timeout=timeout)
