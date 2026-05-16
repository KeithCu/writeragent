# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Resolve a venv directory to its python executable and run a trivial subprocess check."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple


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


def run_venv_self_check(python_exe: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Run a diagnostic script; return (success, user-facing message)."""
    # Diagnostic script to gather version and package info.
    # We use newlines instead of semicolons because compound statements (for/try)
    # cannot be semicolon-separated in a single line.
    script = (
        "import sys, json\n"
        "res = {'v': sys.version.split()[0]}\n"
        "pkgs = {}\n"
        "for p in ['numpy', 'pandas', 'scipy', 'sklearn', 'matplotlib']:\n"
        "    try:\n"
        "        m = __import__(p)\n"
        "        v = getattr(m, '__version__', 'present')\n"
        "        pkgs[p] = str(v)\n"
        "    except ImportError:\n"
        "        pkgs[p] = None\n"
        "res['p'] = pkgs\n"
        "print(json.dumps(res))"
    )

    try:
        proc = subprocess.run(
            [python_exe, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for Python (check venv and try again)."
    except OSError as e:
        return False, f"Could not run Python: {e}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        tail = err[:400] + ("…" if len(err) > 400 else "")
        msg = f"Python exited with code {proc.returncode}."
        if tail:
            msg = f"{msg}\n{tail}"
        return False, msg

    out = (proc.stdout or "").strip()
    # Find the last line that looks like JSON in case there's noise (e.g. from imports)
    json_line = ""
    for line in out.splitlines():
        if line.strip().startswith('{"v":'):
            json_line = line.strip()
            break
    if not json_line and out.strip().startswith('{'):
        json_line = out.strip().splitlines()[-1]

    if not json_line:
        return False, f"Unexpected output from test run: {out!r}"

    try:
        import json

        data = json.loads(json_line)
        version = data.get("v", "unknown")
        packages = data.get("p", {})

        msg_lines = [f"Python {version} responds OK."]

        found = []
        missing = []
        # Specifically highlight requested ones first
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

        return True, "\n".join(msg_lines)
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {out!r}"


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
