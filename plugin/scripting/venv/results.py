# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Error result dict for venv helpers and the sandbox (a leaf: the compute bundle ships it)."""

from __future__ import annotations

import traceback
from typing import Any


def error_result(
    code: str | None = None,
    message: str = "",
    *,
    helper: str | None = None,
    details: dict[str, Any] | None = None,
    stdout: str | None = None,
    with_traceback: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "message": message}
    if code is not None:
        out["code"] = code
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    if stdout is not None:
        out["stdout"] = stdout
    if with_traceback:
        out["traceback"] = traceback.format_exc()
    return out
