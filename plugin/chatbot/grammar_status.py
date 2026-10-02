# WriterAgent - Native Grammar Status Formatting
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formatting helpers for realtime grammar and language detection sidebar status."""

from __future__ import annotations

from typing import Any, Literal

_GRAMMAR_STATUS_PREVIEW_CHARS = 10


def _clip_grammar_status_preview(s: str, max_len: int = _GRAMMAR_STATUS_PREVIEW_CHARS) -> str:
    """One-line snippet for the sidebar status field (short to save space)."""
    compact = " ".join(s.strip().split())
    if not compact:
        return "(empty)"
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}…"


def _grammar_status_area(phase: str, result: str, preview: str) -> Literal["language", "grammar"]:
    """Sidebar label bucket: language-detection LLM / failures vs grammar pipeline."""
    if phase == "request" and result == "Detecting language":
        return "language"
    # emit_grammar_status clips the preview to 10 characters before this
    # runs (slice_preview_debug). A language-detection failure therefore
    # arrives as "Language d…" and used to take the Grammar label.
    clipped = preview.strip().lower()
    if phase == "failed" and clipped in ("language detection", "language d\u2026"):
        return "language"
    return "grammar"


def format_grammar_status(data: dict[str, Any]) -> str:
    """Format native grammar proofreader progress for the sidebar status field."""
    phase = str(data.get("phase") or "")
    preview_raw = str(data.get("preview") or "")
    result = str(data.get("result") or "")
    try:
        length = int(data.get("length") or 0)
    except Exception:
        length = 0
    elapsed = data.get("elapsed_ms")
    area = _grammar_status_area(phase, result, preview_raw)
    preview = _clip_grammar_status_preview(preview_raw)
    prefix = "Language:" if area == "language" else "Grammar:"
    if phase == "start":
        return f"{prefix} queued '{preview}' len {length}"
    if phase == "join":
        return f"{prefix} waiting '{preview}' len {length}"
    if phase == "request":
        verb = "detecting" if area == "language" else "checking"
        base = f"{prefix} {verb} '{preview}' len {length}"
        if result and result not in ("LLM request", "LLM batch request", "Detecting language"):
            return f"{prefix} {result}"
        return base
    if phase == "complete":
        suffix = result or "done"
        if elapsed is not None:
            suffix = f"{suffix}, {elapsed}ms"
        return f"{prefix} done '{preview}' len {length}: {suffix}"
    if phase == "done":
        suffix = result or "done"
        if elapsed is not None:
            suffix = f"{suffix}, {elapsed}ms"
        return f"{prefix} done '{preview}' len {length}: {suffix}"
    if phase == "timeout":
        return f"{prefix} still running '{preview}' len {length}: {result}"
    if phase == "skipped":
        return f"{prefix} skipped '{preview}' len {length}: {result}"
    if phase == "failed":
        return f"{prefix} failed '{preview}' len {length}: {result}"
    return f"{prefix} {phase or 'update'} '{preview}' len {length}"
