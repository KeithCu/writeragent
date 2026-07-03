# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Shared text for document_research in the chat response (multi-line status blocks).

from __future__ import annotations

import os
import posixpath


def display_name_for_path_or_name(path_or_name: str) -> str:
    """Basename for absolute paths; otherwise the string as given (basename, filter, URL fragment)."""
    raw = (path_or_name or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return posixpath.basename(raw) or raw
    if os.path.isabs(raw):
        return os.path.basename(raw) or raw
    return raw


def document_open_preview_line(path_or_name: str) -> str:
    """Sentence shown before a read-only sibling document open."""
    from plugin.framework.i18n import _

    label = display_name_for_path_or_name(path_or_name)
    return _("Opening '%s' for read-only access.") % (label,)


def document_open_step_chat_text(path_or_name: str, step_index: int) -> str:
    """Chat text for each delegate_read_document step (tool name + open preview only)."""
    from plugin.framework.i18n import _

    del step_index  # callers pass index; format is the same for every step
    block = "\n" + _("Tool: %s") % "delegate_read_document" + "\n"
    block += document_open_preview_line(path_or_name) + "\n\n"
    return block
