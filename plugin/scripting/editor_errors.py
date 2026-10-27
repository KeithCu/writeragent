# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Failure detail and message formatting for user-visible scripting dialogs (Monaco editor, Run Python Script, Calc cell editor)."""

from __future__ import annotations

import re
import traceback
from typing import Any

from plugin.framework.deal_shim import (
    DEAL_MAX_SOURCE,
    _profile,
    deal,
    str_bounded,
)


def _deal_text_ok_pytest(value: object) -> bool:
    return isinstance(value, str)


def _deal_text_ok_crosshair(value: object) -> bool:
    return str_bounded(value, DEAL_MAX_SOURCE)


_deal_text_ok = _profile(_deal_text_ok_crosshair, _deal_text_ok_pytest)


def _deal_optional_exc_ok_pytest(exc: object) -> bool:
    return exc is None or isinstance(exc, BaseException)


def _deal_optional_exc_ok_crosshair(exc: object) -> bool:
    return exc is None


_deal_optional_exc_ok = _profile(_deal_optional_exc_ok_crosshair, _deal_optional_exc_ok_pytest)


def _deal_failure_ok(detail: object = None, exc: object = None) -> bool:
    return (detail is None or _deal_text_ok(detail)) and _deal_optional_exc_ok(exc)


@deal.pre(lambda exc: isinstance(exc, BaseException))
def exception_traceback(exc: BaseException) -> str:
    """Full traceback for *exc*.

    The pre is isinstance(exc, BaseException) on both profiles because a
    CrossHair domain of exc is None has no __traceback__; the callable
    stays # crosshair: off so traceback.format_exception is not a
    check/cover entry.
    """
    # crosshair: off
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@deal.pre(lambda detail=None, exc=None: _deal_failure_ok(detail, exc))
@deal.post(lambda result: isinstance(result, str))
def failure_detail(*, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Combine subprocess stderr, probe output, and/or an exception traceback."""
    # crosshair: off
    chunks: list[str] = []
    detail_text = (detail or "").strip()
    if detail_text:
        chunks.append(detail_text)
    if exc is not None:
        chunks.append(exception_traceback(exc).rstrip())
    return "\n\n".join(chunks)


@deal.pre(lambda summary, detail=None, exc=None: _deal_text_ok(summary) and _deal_failure_ok(detail, exc))
@deal.post(lambda result: isinstance(result, str))
def failure_message(summary: str, *, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Build a msgbox body: *summary* plus optional detail/traceback blocks."""
    # crosshair: off
    body = failure_detail(detail=detail, exc=exc)
    if body:
        return f"{summary}\n\n{body}"
    return summary


def editor_error_markers(message: str, traceback_text: str = "", code: str = "") -> list[dict[str, Any]]:
    """Extract editor error markers from failure messages and tracebacks."""
    combined = f"{message}\n{traceback_text}" if traceback_text else message
    line: int | None = None

    m_syntax = re.search(r"Code parsing failed on line (\d+)", combined)
    if m_syntax:
        line = int(m_syntax.group(1))
    else:
        m_exec = re.search(r"Code execution failed at line '(.*)' due to:", combined, re.DOTALL)
        if m_exec:
            segment = m_exec.group(1)
            if segment and segment != "None" and code:
                idx = code.find(segment)
                if idx != -1:
                    line = code[:idx].count("\n") + 1
        else:
            sandbox_matches = list(re.finditer(r'File "<sandbox>", line (\d+)', combined))
            if sandbox_matches:
                line = int(sandbox_matches[-1].group(1))

    if line is None or line < 1:
        return []

    if code:
        total_lines = code.count("\n") + 1
        if line > total_lines:
            return []

    target_text = message if message.strip() else traceback_text
    first_line = ""
    for raw_line in target_text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            first_line = stripped
            break
    first_line = re.sub(r"\s*\(took\s+[^)]*\)$", "", first_line).strip()

    return [{"line": line, "message": first_line, "severity": "error"}]

