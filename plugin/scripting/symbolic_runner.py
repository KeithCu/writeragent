# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted symbolic execution for chat tools and Run Python Script."""

from __future__ import annotations

from typing import Any

from plugin.doc.document_helpers import is_calc, is_writer
from plugin.framework.client.symbolic_client import run_symbolic
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.symbolic_common import HELPER_NAMES


def supports_symbolic_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Math Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def run_trusted_symbolic(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    task_hint: str | None = None,
) -> dict[str, Any]:
    """Run a trusted symbolic helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="SYMBOLIC_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="SYMBOLIC_ERROR")
    if not is_calc(doc) and not is_writer(doc):
        raise ToolExecutionError("Symbolic helpers require a Writer or Calc document.", code="SYMBOLIC_ERROR")

    spec: dict[str, Any] = {"helper": name}
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    if task_hint:
        context["task_hint"] = str(task_hint)

    return run_symbolic(uno_ctx, spec, None, context=context or None)
