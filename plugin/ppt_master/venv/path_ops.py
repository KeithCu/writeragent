# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Safe path resolution for ppt-master data root and project folders."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT_TIMEOUT_SEC = 600


def resolve_under_root(root: Path, relative: str) -> Path | None:
    """Resolve *relative* under *root*; reject traversal."""
    rel = str(relative or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    try:
        candidate = (root / rel).resolve()
        root_resolved = root.expanduser().resolve()
        candidate.relative_to(root_resolved)
    except (ValueError, OSError):
        return None
    return candidate


def resolve_project_file(project_path: str | Path, relative: str) -> Path | None:
    project = Path(project_path).expanduser().resolve()
    if not project.is_dir():
        return None
    return resolve_under_root(project, relative)


def run_script(data_root: Path, script_relative: str, args: list[str] | None = None) -> dict:
    """Run an upstream script under data_root/scripts/ using the current venv python."""
    rel = str(script_relative or "").strip().replace("\\", "/").lstrip("/")
    if not rel.startswith("scripts/"):
        rel = f"scripts/{rel}" if not rel.startswith("/") else rel.lstrip("/")
    script_path = resolve_under_root(data_root, rel)
    if script_path is None or not script_path.is_file():
        return {"status": "error", "message": f"Script not found or not allowed: {script_relative}"}

    cmd = [sys.executable, str(script_path)]
    for arg in args or []:
        cmd.append(str(arg))
    env = dict(os.environ)
    env.setdefault("PPT_MASTER_DATA_ROOT", str(data_root))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SCRIPT_TIMEOUT_SEC,
            check=False,
            env=env,
            cwd=str(data_root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "message": str(exc)}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out
        return {
            "status": "error",
            "message": f"exit {proc.returncode}: {detail[:800]}",
            "stdout": out[:2000],
            "stderr": err[:2000],
        }
    return {"status": "ok", "stdout": out[:8000], "stderr": err[:2000] if err else ""}
