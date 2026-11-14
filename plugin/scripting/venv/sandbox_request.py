# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared execute request shape for host, child harness, compute worker, and tests.

Standard library only so the venv child and compute bundle can import it without
loading ``venv_worker`` or UNO.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True, kw_only=True)
class SandboxRequest:
    """Everything the venv child reads for one user-code execute.

    Host (``ExecRequest``), compute ``formula_worker`` and tests build it;
    the pipe carries ``wire()`` as a plain dict.
    """

    timeout_sec: int  # already bounded by the sender; child never clamps
    session_id: str | None = None
    data: Any = None
    code: str = ""
    bindings: dict[str, Any] | None = None
    init_script: str | None = None
    init_session_id: str | None = None
    init_script_hash: str | None = None
    sandbox_policy: dict[str, Any] | None = None

    def wire(self) -> dict[str, Any]:
        """Plain dict for Pickle5 IPC, walking only ``SandboxRequest`` fields.

        Subclasses (such as host-only ``ExecRequest`` fields) never travel.
        ``None`` values are omitted; values are not copied (no ``asdict`` deepcopy of ``data``).
        """
        out: dict[str, Any] = {}
        for f in fields(SandboxRequest):
            val = getattr(self, f.name)
            if val is not None:
                out[f.name] = val
        return out

    @staticmethod
    def from_wire(frame: dict[str, Any]) -> SandboxRequest:
        """Rebuild a ``SandboxRequest`` from a decoded wire dict, ignoring extra keys."""
        kwargs = {f.name: frame[f.name] for f in fields(SandboxRequest) if f.name in frame}
        return SandboxRequest(**kwargs)
