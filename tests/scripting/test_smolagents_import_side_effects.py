# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _child_modules_after(import_stmt: str) -> set[str]:
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    # Fresh interpreter: parent pytest already imported huggingface in this process.
    code = import_stmt + "; import sys; print('\\n'.join(sorted(sys.modules)))"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return {line for line in proc.stdout.splitlines() if line}


def test_sandbox_import_does_not_load_huggingface_hub() -> None:
    mods = _child_modules_after("import plugin.scripting.sandbox")
    leaked = {m for m in mods if m == "huggingface_hub" or m.startswith("huggingface_hub.")}
    assert not leaked
    assert "plugin.contrib.smolagents.tools" not in mods


def test_worker_base_import_does_not_load_huggingface_hub() -> None:
    mods = _child_modules_after("import compute_service.worker_base")
    leaked = {m for m in mods if m == "huggingface_hub" or m.startswith("huggingface_hub.")}
    assert not leaked
    assert "plugin.contrib.smolagents.tools" not in mods


def test_formula_executor_import_does_not_load_huggingface_hub() -> None:
    """formula_worker.py imports executor + worker_base + payload_codec."""
    mods = _child_modules_after(
        "import compute_service.executor, compute_service.worker_base, "
        "plugin.scripting.payload_codec"
    )
    leaked = {m for m in mods if m == "huggingface_hub" or m.startswith("huggingface_hub.")}
    assert not leaked
    assert "plugin.contrib.smolagents.tools" not in mods
