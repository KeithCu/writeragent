# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""DEBUG observability and sidebar status helpers for the grammar worker."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework import event_bus

log = logging.getLogger("writeragent.grammar")


def grammar_obs(event: str, **fields: Any) -> None:
    """DEBUG-only observability for queue / worker (grep ``[grammar] obs`` in logs)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.debug("[grammar] obs %s %s", event, kv)


def emit_grammar_status(
    phase: str,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
    preview_source: str | None = None,
    length_hint: int | None = None,
) -> None:
    """Emit ``grammar:status``. Pass ``preview_source`` for a sentence snippet (sidebar, clipped to a few chars)."""
    from .grammar_proofread_text import slice_preview_debug

    try:
        if preview_source is not None:
            raw = preview_source.strip() or "(empty)"
            preview = slice_preview_debug(raw, 10)
            length = len(raw) if length_hint is None else length_hint
        else:
            preview = slice_preview_debug(text.strip() or "(empty)", 10)
            length = len(text)
        event_bus.global_event_bus.emit("grammar:status", phase=phase, preview=preview, length=length, result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)
