# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Spawn the Monaco/pywebview editor child process in the user venv."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, cast

from plugin.scripting.editor_diagnostics import failure_detail
from plugin.scripting.subprocess_env import scrub_subprocess_env
from plugin.scripting.venv_probe import resolve_venv_python

log = logging.getLogger(__name__)

_EDITOR_MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor_main.py")
_ASSETS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contrib", "scripting", "assets", "editor")
)

_WEBVIEW_PROBE_CODE = """\
import sys
import traceback
try:
    import webview
    print(getattr(webview, "__file__", "ok"))
except Exception:
    traceback.print_exc()
    sys.exit(1)
"""


def build_editor_child_env(*, assets_dir: str | None = None) -> dict[str, str]:
    """Environment for editor subprocess (venv python + GUI session variables)."""
    env = scrub_subprocess_env(dict(os.environ))
    env["WRITERAGENT_EDITOR_ASSETS"] = assets_dir or _ASSETS_DIR
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LD_LIBRARY_PATH"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def resolve_editor_python(uno_ctx: Any) -> tuple[str | None, str]:
    """Return (venv python executable, error message). Monaco requires a user venv."""
    from plugin.framework.config import get_config_str

    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if not venv_dir:
        return (
            None,
            "Set the Python venv path in WriterAgent Settings → Python (same venv where you ran "
            "'pip install pywebview'). LibreOffice's built-in Python cannot run the Monaco editor.",
        )
    exe = resolve_venv_python(venv_dir)
    if not exe:
        return (
            None,
            f"No python executable found under configured venv: {venv_dir!r} "
            "(expected bin/python, bin/python3, or bin/python3.x).",
        )
    return exe, ""


def probe_webview_import(exe: str) -> tuple[bool, str]:
    """Return whether *exe* can ``import webview`` (pywebview package), with diagnostics."""
    try:
        r = subprocess.run(
            [exe, "-c", _WEBVIEW_PROBE_CODE],
            capture_output=True,
            timeout=30,
            env=build_editor_child_env(),
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("probe_webview_import failed for %s: %s", exe, e, exc_info=True)
        return False, failure_detail(exc=e)
    detail = (r.stdout or "").strip()
    if r.stderr:
        detail = f"{detail}\n{r.stderr}".strip() if detail else r.stderr.strip()
    if r.returncode == 0:
        return True, detail
    if not detail:
        detail = f"exit code {r.returncode}"
    log.warning("probe_webview_import: %s returned %s: %s", exe, r.returncode, detail)
    return False, detail


def spawn_editor_process(exe: str, *, assets_dir: str | None = None) -> subprocess.Popen[bytes]:
    """Start editor_main.py with stdin/stdout pipes."""
    env = build_editor_child_env(assets_dir=assets_dir)
    popen_kw: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "text": False,
        "bufsize": 0,
    }
    if sys.platform != "win32":
        popen_kw["preexec_fn"] = os.setsid
    return cast("subprocess.Popen[bytes]", subprocess.Popen([exe, _EDITOR_MAIN], **popen_kw))
