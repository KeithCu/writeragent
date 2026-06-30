# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find or build ppt-master PPTX exports via the user venv (upstream svg_to_pptx)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from plugin.framework.config import get_config_str
from plugin.scripting.sandbox import resolve_venv_python

log = logging.getLogger(__name__)

_SVG_SNAPSHOT_SUFFIX = "_svg.pptx"
_BUILD_TIMEOUT_SEC = 600


def find_project_pptx(project_path: Path) -> Path | None:
    """Return the newest native PPTX under ``exports/`` (skip ``*_svg.pptx`` snapshots)."""
    exports = Path(project_path).expanduser().resolve() / "exports"
    if not exports.is_dir():
        return None
    candidates = sorted(exports.glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        if not path.name.endswith(_SVG_SNAPSHOT_SUFFIX):
            return path
    return candidates[0] if candidates else None


def build_project_pptx(ctx: Any, project_path: Path, data_root: Path) -> tuple[Path | None, str | None]:
    """Run upstream ``svg_to_pptx.py -q`` in the configured user venv."""
    del ctx  # reserved for future venv worker IPC
    project_path = Path(project_path).expanduser().resolve()
    data_root = Path(data_root).expanduser().resolve()
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    python_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not python_exe:
        return None, "PPTX missing; configure Python venv (Settings → Python) to build from SVG."

    script = data_root / "scripts" / "svg_to_pptx.py"
    if not script.is_file():
        return None, f"svg_to_pptx.py not found under {data_root}"

    cmd = [python_exe, str(script), str(project_path), "-q"]
    if not (project_path / "svg_output").is_dir() and (project_path / "svg_final").is_dir():
        cmd.extend(["-s", "final"])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_BUILD_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("pptx build failed for %s: %s", project_path, exc)
        return None, f"PPTX build failed: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:800]
        return None, f"PPTX build failed (exit {proc.returncode}): {detail or 'see venv logs'}"

    pptx = find_project_pptx(project_path)
    if pptx is None:
        return None, "PPTX build finished but no exports/*.pptx was produced."
    return pptx, None


def ensure_project_pptx(ctx: Any, project_path: Path, data_root: Path) -> tuple[Path | None, str | None]:
    """Return an existing exports PPTX or build one from SVG via the user venv."""
    existing = find_project_pptx(project_path)
    if existing is not None:
        return existing, None
    return build_project_pptx(ctx, project_path, data_root)
