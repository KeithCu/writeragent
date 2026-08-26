# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor IPC protocol (pickle protocol 5) and failure formatting for user-visible dialogs."""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify IPC frame structures or protocol message envelopes here,
# you MUST also update the corresponding JavaScript / Python files:
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor.js
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Host Bridge:              plugin/scripting/editor_host.py
# =========================================================================================

from __future__ import annotations

import traceback
import uuid
from typing import Any, IO, Mapping

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal
from plugin.scripting.ipc import IpcFrameError, pack_pickle_frame, read_frame_payload, unpack_pickle_frame

EDITOR_DEFAULT_TITLE = " "

# JSON-safe identity keys on every session message (omit empties).
_TARGET_KEYS = ("cell_address", "script_name", "script_origin", "doc_url", "resource")

# Cap payloads to avoid accidental OOM from a corrupted length header.
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one pickle-framed message from *stream*. Returns None on clean EOF."""
    # crosshair: off
    payload = read_frame_payload(stream, max_payload_bytes=_MAX_PAYLOAD_BYTES, frame_label="editor message")
    if payload is None:
        return None
    try:
        decoded = unpack_pickle_frame(payload)
    except ValueError as e:
        raise ValueError(f"Invalid editor message pickle: {e}") from e
    if not isinstance(decoded, dict):
        raise ValueError("Editor message must be a dict")
    return decoded


def write_message(stream: IO[bytes], message: dict[str, Any]) -> None:
    """Write one dict to *stream* as pickle protocol 5 with a 4-byte big-endian length prefix."""
    # crosshair: off
    try:
        frame = pack_pickle_frame(message, max_payload_bytes=_MAX_PAYLOAD_BYTES)
    except IpcFrameError as exc:
        raise ValueError("Editor message exceeds maximum payload size") from exc
    stream.write(frame)
    stream.flush()


def message_type(message: dict[str, Any]) -> str:
    """Return the ``type`` field or empty string."""
    raw = message.get("type")
    return str(raw) if raw is not None else ""


def new_session_id() -> str:
    """Opaque routing id for one editor buffer (host-minted)."""
    return uuid.uuid4().hex


def normalize_target(target: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep only string identity fields; drop empty values and UNO objects."""
    if not target:
        return {}
    out: dict[str, str] = {}
    for key in _TARGET_KEYS:
        raw = target.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            out[key] = text
    return out


def target_from_load(msg: Mapping[str, Any]) -> dict[str, str]:
    """Build ``target`` from an explicit dict plus top-level load aliases."""
    raw = msg.get("target")
    target = normalize_target(raw if isinstance(raw, Mapping) else None)
    aliases = (
        ("cell_address", "cell_address"),
        ("selected_script_name", "script_name"),
        ("script_name", "script_name"),
        ("script_origin", "script_origin"),
        ("doc_url", "doc_url"),
        ("resource", "resource"),
    )
    for src, dest in aliases:
        if dest in target:
            continue
        value = msg.get(src)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            target[dest] = text
    return target


def target_identity_key(mode: str, target: Mapping[str, str] | None) -> tuple[str, str, str, str, str]:
    """Stable key so reopening the same cell/script reuses ``session_id``."""
    t = normalize_target(target)
    return (
        str(mode or ""),
        t.get("cell_address", ""),
        t.get("script_name", ""),
        t.get("doc_url", ""),
        t.get("resource", ""),
    )


def session_id_of(message: Mapping[str, Any]) -> str:
    raw = message.get("session_id")
    return str(raw).strip() if raw is not None else ""


def stamp_session(
    msg: Mapping[str, Any],
    *,
    session_id: str,
    mode: str = "",
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy *msg* and attach ``session_id``, ``mode``, and ``target`` (always)."""
    out = dict(msg)
    out["session_id"] = str(session_id or "")
    use_mode = str(mode or out.get("mode") or "")
    if use_mode:
        out["mode"] = use_mode
    merged = dict(out.get("target") or {}) if isinstance(out.get("target"), dict) else {}
    if target:
        merged.update(dict(target))
    out["target"] = normalize_target(merged)
    return out


def exception_traceback(exc: BaseException) -> str:
    """Full traceback string for *exc*."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@deal.pre(
    lambda detail=None, exc=None: (detail is None or str_bounded(detail, DEAL_MAX_SOURCE))
    and exc is None
)
@deal.post(lambda result: isinstance(result, str))
def failure_detail(*, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Combine subprocess stderr, probe output, and/or an exception traceback.

    ``exc is None`` in the pre so CrossHair cannot run ``traceback.format_exception``
    on a symbolic BaseException (check-all deep 32900105768, 8:22). Production
    still formats ``exc`` when deal is stripped; pytest uses ``exception_traceback``.
    """
    chunks: list[str] = []
    detail_text = (detail or "").strip()
    if detail_text:
        chunks.append(detail_text)
    if exc is not None:
        chunks.append(exception_traceback(exc).rstrip())
    return "\n\n".join(chunks)


@deal.pre(
    lambda summary, detail=None, exc=None: str_bounded(summary, DEAL_MAX_SOURCE)
    and (detail is None or str_bounded(detail, DEAL_MAX_SOURCE))
    and exc is None
)
@deal.post(lambda result: isinstance(result, str))
def failure_message(summary: str, *, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Build a msgbox body: *summary* plus optional detail/traceback blocks."""
    body = failure_detail(detail=detail, exc=exc)
    if body:
        return f"{summary}\n\n{body}"
    return summary
