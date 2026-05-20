# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Run user Python in a warm venv subprocess (fresh namespace per call)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from plugin.framework.config import get_config_str
from plugin.scripting.python_worker_manager import PythonWorkerManager
from plugin.scripting.subprocess_env import scrub_subprocess_env
from plugin.scripting.timeout_limits import configured_python_exec_timeout, resolve_python_exec_timeout
from plugin.scripting.venv_probe import resolve_libreoffice_python, resolve_venv_python

log = logging.getLogger(__name__)


def run_code_in_user_venv(
    uno_ctx: Any,
    code: str,
    *,
    data: Any = None,
    timeout_sec: int | None = None,
    active_domain: str | None = None,
    python_tool_domain: str | None = None,
) -> Dict[str, Any]:
    """Execute *code* via :class:`PythonWorkerManager` (warm process, isolated namespace per call).

    *active_domain* / *python_tool_domain* are reserved for future venv→LO tool RPC (not wired yet).
    """
    del active_domain, python_tool_domain  # deferred — see docs/enabling_numpy_in_libreoffice.md §7
    if not (code or "").strip():
        return {"status": "error", "message": "No code provided."}

    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return {
                "status": "error",
                "message": f"No python executable found under configured venv: {venv_dir!r}",
            }
        log.debug("run_venv_code: using venv interpreter under %s", venv_dir)
    else:
        exe = resolve_libreoffice_python()
        if not exe:
            return {
                "status": "error",
                "message": (
                    "Could not resolve a Python interpreter (sys.executable missing, not a file, or not executable). "
                    "Set scripting.python_venv_path in Settings → Python for a dedicated venv, or fix the LibreOffice install."
                ),
            }
        log.debug("run_venv_code: using process interpreter %s (no venv path set)", exe)

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    child_env = scrub_subprocess_env(dict(os.environ))
    manager = PythonWorkerManager.get(exe, child_env)
    return manager.execute(code, data=data, timeout_sec=timeout_sec)
