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


_last_status_indicator: Any = None


def update_libreoffice_status_bar(phase: str, text: str, result: str) -> None:
    """Update the LibreOffice window status bar using XStatusIndicator."""
    global _last_status_indicator
    from plugin.framework.uno_context import get_ctx, get_active_document, get_desktop

    # Determine status message
    msg = f"LibreHarper: {result or 'Checking...'}"
    if phase == "done":
        msg = "LibreHarper: Grammar check complete"
    elif phase == "failed":
        msg = f"LibreHarper: Failed ({result})"

    try:
        ctx = get_ctx()
        frame = None
        doc = get_active_document(ctx)
        if doc is not None:
            try:
                controller = doc.getCurrentController()
                if controller is not None:
                    frame = controller.getFrame()
            except Exception:
                pass
        if frame is None:
            try:
                desktop = get_desktop(ctx)
                if desktop is not None:
                    frame = desktop.getCurrentFrame()
            except Exception:
                pass

        if frame is not None:
            if phase in ("start", "request"):
                if _last_status_indicator is None:
                    try:
                        _last_status_indicator = frame.createStatusIndicator()
                        if _last_status_indicator is not None:
                            _last_status_indicator.start(msg, 100)
                    except Exception:
                        _last_status_indicator = None
                else:
                    try:
                        _last_status_indicator.setText(msg)
                        _last_status_indicator.setValue(50)
                    except Exception:
                        pass
            elif phase in ("done", "failed"):
                if _last_status_indicator is not None:
                    try:
                        _last_status_indicator.setText(msg)
                        _last_status_indicator.setValue(100)
                        _last_status_indicator.end()
                    except Exception:
                        pass
                    _last_status_indicator = None
    except Exception as e:
        log.debug("[grammar] update_libreoffice_status_bar failed: %s", e)
        _last_status_indicator = None


def emit_grammar_status(
    phase: str,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
    preview_source: str | None = None,
    length_hint: int | None = None,
) -> None:
    """Emit status to the LibreOffice status bar (for LibreHarper) or sidebar event bus (for WriterAgent)."""
    from .grammar_proofread_text import slice_preview_debug
    from plugin.framework.uno_context import is_libreharper

    try:
        if preview_source is not None:
            raw = preview_source.strip() or "(empty)"
            preview = slice_preview_debug(raw, 10)
            length = len(raw) if length_hint is None else length_hint
        else:
            preview = slice_preview_debug(text.strip() or "(empty)", 10)
            length = len(text)

        if is_libreharper():
            from plugin.framework.queue_executor import post_to_main_thread
            try:
                post_to_main_thread(update_libreoffice_status_bar, phase, text, result)
            except Exception:
                update_libreoffice_status_bar(phase, text, result)
        else:
            event_bus.global_event_bus.emit("grammar:status", phase=phase, preview=preview, length=length, result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


def emit_harper_worker_status(sentence_text: str, message: str) -> None:
    """Relay Harper venv-worker progress to the sidebar grammar status field."""
    emit_grammar_status("request", sentence_text, result=message, preview_source=sentence_text)


def play_diagnostic_beep() -> None:
    """Asynchronously play a short system sound to indicate when error sentences are sent."""
    import os
    import subprocess

    # Find the extension root relative to this file (four directories up)
    ext_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    beep_path = os.path.join(ext_root, "assets", "beep.wav")

    sound_paths = [
        beep_path,
        "/usr/share/sounds/kshisen/tile-touch.ogg",
        "/usr/share/sounds/Oxygen-Sys-Log-In-Short.ogg",
        "/usr/share/sounds/oxygen/stereo/dialog-information.ogg",
    ]
    sound_file = None
    for path in sound_paths:
        if os.path.exists(path):
            sound_file = path
            break

    if not sound_file:
        return

    for player in ("pw-play", "paplay"):
        try:
            subprocess.Popen([player, sound_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except FileNotFoundError:
            continue
        except Exception:
            pass


