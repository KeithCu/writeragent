# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-only helpers for calling run_sandboxed_code and forcing the SIGALRM fallback."""

from __future__ import annotations

import signal
from typing import Any

import pytest

from plugin.scripting.payload_codec import ForceBinary
from plugin.scripting.venv.sandbox_request import SandboxRequest
from plugin.scripting.venv.venv_sandbox import run_sandboxed_code


def refuse_sigalrm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force local_python_executor.timeout onto its ThreadPoolExecutor path."""
    real_signal = signal.signal

    def _signal(signum, handler):
        if hasattr(signal, "SIGALRM") and signum == signal.SIGALRM:
            raise ValueError("SIGALRM unavailable")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", _signal)


def sandbox_run(
    code: str,
    data: Any = None,
    *,
    force: ForceBinary = "auto",
    timeout_sec: int = 10,
    **fields: Any,
) -> dict[str, Any]:
    """Build a ``SandboxRequest`` and run it in ``run_sandboxed_code``."""
    return run_sandboxed_code(
        SandboxRequest(code=code, data=data, timeout_sec=timeout_sec, **fields),
        force=force,
    )
