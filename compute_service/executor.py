# WriterAgent - Python Compute Service Executor
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""AST Sandbox executor for the standalone Python Compute Service."""

from __future__ import annotations

import os
import sys
from typing import Any

# Ensure repo root is on sys.path to resolve plugin.* imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.framework.uno_bootstrap import register_alias_importer
register_alias_importer()

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

def execute_code(
    code: str,
    data: Any = None,
    session_id: str | None = None,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    """Execute the given Python code under AST sandboxing and return results."""
    # Force Agg backend to prevent matplotlib window popping up on host/container
    try:
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        pass

    return run_sandboxed_code(
        code=code,
        data=data,
        session_id=session_id,
        timeout_sec=timeout_sec,
    )
