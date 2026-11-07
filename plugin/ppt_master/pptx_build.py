# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find or build ppt-master PPTX exports via the user venv (upstream svg_to_pptx)."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from plugin.framework.config import get_config
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.send_cancellation import SendCancellation, is_cancelled
from plugin.framework.worker_pool import start_stderr_drain
from plugin.scripting.sandbox import resolve_venv_python

log = logging.getLogger(__name__)

_SVG_SNAPSHOT_SUFFIX = "_svg.pptx"
# Leave 60s margin for document import before ExportPresentationProject's 600s tool timeout.
_BUILD_TIMEOUT_SEC = 540
_BUILD_POLL_SEC = 0.2


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


def build_project_pptx(
    ctx: Any,
    project_path: Path,
    data_root: Path,
    *,
    cancel: SendCancellation | None = None,
    timeout_sec: float = _BUILD_TIMEOUT_SEC,
) -> tuple[Path | None, str | None]:
    """Run upstream ``svg_to_pptx.py -q`` in the configured user venv."""
    del ctx  # reserved for future venv worker IPC
    project_path = Path(project_path).expanduser().resolve()
    data_root = Path(data_root).expanduser().resolve()
    venv_dir = str(get_config("scripting.python_venv_path") or "").strip()
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
        managed = ManagedSubprocess(
            cmd,
            inherit_env=True,
            new_session=True,
            register=True,
            stdin=subprocess.DEVNULL,
            log_start_failure=False,
            stderr_tail_chars=800,
            label="pptx-build",
        )
        managed.start()
    except OSError as exc:
        log.warning("pptx build failed for %s: %s", project_path, exc)
        return None, f"PPTX build failed: {exc}"

    proc = managed.popen
    assert proc is not None

    stdout_tail = start_stderr_drain(managed.stdout, max_tail_chars=800, name="pptx-build-stdout")

    # Bugfix: subprocess.run ignored Stop, was not registered, and was not a session leader,
    # so quit could not killpg it and it could write exports/*.pptx after soffice was gone.
    # kill_tree on Stop or the build deadline happens before the function returns a path,
    # so the caller does not import.
    deadline = time.monotonic() + timeout_sec
    stopped = False
    timed_out = False

    try:
        while proc.poll() is None:
            if is_cancelled(cancel):
                managed.kill_tree()
                stopped = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                managed.kill_tree()
                timed_out = True
                break
            try:
                proc.wait(timeout=min(_BUILD_POLL_SEC, remaining))
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        managed.kill_tree()
        raise
    finally:
        managed.wait_once()
        stdout_text = stdout_tail.finish_text(1.0) if stdout_tail is not None else ""
        stderr_text = managed.join_stderr(timeout=1.0)
        managed.unregister()
        if managed.is_alive():
            managed.abandon()

    if stopped:
        return None, "PPTX build stopped."
    if timed_out:
        return None, f"PPTX build failed: timed out after {timeout_sec:g} seconds"

    retcode = proc.returncode
    if retcode != 0:
        detail = (stderr_text or stdout_text).strip()
        return None, f"PPTX build failed (exit {retcode}): {detail or 'see venv logs'}"

    pptx = find_project_pptx(project_path)
    if pptx is None:
        return None, "PPTX build finished but no exports/*.pptx was produced."
    return pptx, None


def ensure_project_pptx(
    ctx: Any,
    project_path: Path,
    data_root: Path,
    *,
    cancel: SendCancellation | None = None,
) -> tuple[Path | None, str | None]:
    """Return an existing exports PPTX or build one from SVG via the user venv."""
    existing = find_project_pptx(project_path)
    if existing is not None:
        return existing, None
    return build_project_pptx(ctx, project_path, data_root, cancel=cancel)
