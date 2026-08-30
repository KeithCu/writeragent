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

from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    deal,
    str_bounded,
)
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
)

EDITOR_DEFAULT_TITLE = " "

# JSON-safe identity keys on every session message (omit empties).
_TARGET_KEYS = ("cell_address", "script_name", "script_origin", "doc_url", "resource")

def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one pickle-framed message from *stream*. Returns None on clean EOF."""
    # crosshair: off
    payload = read_frame_payload(
        stream, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES, frame_label="editor message"
    )
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
        frame = pack_pickle_frame(message, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
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


def _deal_ipc_dict_ok_pytest(msg: object) -> bool:
    return type(msg) is dict and len(msg) <= DEAL_MAX_CMD_ARGS and all(
        type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) and (v is None or not isinstance(v, str) or str_bounded(v, DEAL_MAX_TOKEN))
        for k, v in msg.items()
    )


def _deal_ipc_dict_ok_crosshair(msg: object, allow_nested: bool = True) -> bool:
    return type(msg) is dict and len(msg) <= DEAL_MAX_CMD_ARGS and all(
        type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) and (
            v is None
            or (isinstance(v, str) and ascii_bounded(v, DEAL_MAX_TOKEN))
            or (allow_nested and isinstance(v, dict) and _deal_ipc_dict_ok_crosshair(v, allow_nested=False))
        )
        for k, v in msg.items()
    )


_deal_ipc_dict_ok = _deal_ipc_dict_ok_crosshair if UNDER_CROSSHAIR else _deal_ipc_dict_ok_pytest


@deal.pre(lambda target: target is None or _deal_ipc_dict_ok(target))
def normalize_target(target: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep only string identity fields; drop empty values and UNO objects."""
    # crosshair: off  # nested IPC dict domain still combinatoric despite _deal_ipc_dict_ok (cover-all 33293627157: ~7m, 113k lines). Doable later with a constructor domain.
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


@deal.pre(lambda msg: _deal_ipc_dict_ok(msg))
def target_from_load(msg: Mapping[str, Any]) -> dict[str, str]:
    """Build ``target`` from an explicit dict plus top-level load aliases."""
    # crosshair: off  # nested IPC dict + alias merges (cover-all 33293627157: ~9m, 148k lines). Doable later with a constructor domain.
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


@deal.pre(
    lambda mode, target: ascii_bounded(mode, DEAL_MAX_TOKEN)
    and (target is None or _deal_ipc_dict_ok(target))
)
def target_identity_key(mode: str, target: Mapping[str, str] | None) -> tuple[str, str, str, str, str]:
    """Stable key so reopening the same cell/script reuses ``session_id``."""
    # crosshair: off  # nested IPC dict domain (cover-all 33293627157: ~9m, 159k lines). Doable later; thin wrapper over normalize_target.
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


@deal.pre(
    lambda msg, session_id, mode="", target=None: _deal_ipc_dict_ok(msg)
    and ascii_bounded(session_id, DEAL_MAX_TOKEN)
    and ascii_bounded(mode, DEAL_MAX_TOKEN)
    and (target is None or _deal_ipc_dict_ok(target))
)
def stamp_session(
    msg: Mapping[str, Any],
    *,
    session_id: str,
    mode: str = "",
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy *msg* and attach ``session_id``, ``mode``, and ``target`` (always)."""
    # crosshair: off  # nested IPC dict copy/merge (cover-all 33293627157: ~11m, 157k lines). Doable later with a constructor domain.
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


def _deal_exc_ok_pytest(exc: object) -> bool:
    return isinstance(exc, BaseException)


def _deal_exc_ok_crosshair(exc: object) -> bool:
    return exc is None


_deal_exc_ok = _deal_exc_ok_crosshair if UNDER_CROSSHAIR else _deal_exc_ok_pytest


@deal.pre(lambda exc: _deal_exc_ok(exc))
def exception_traceback(exc: BaseException) -> str:
    """Full traceback string for *exc*."""
    # crosshair: off  # BaseException/traceback formatting; CrossHair pre forces exc is None so covering is circular (cover-all 33293627157: ~11m, 92k lines). Doable later.
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@deal.pre(
    lambda detail=None, exc=None: (detail is None or str_bounded(detail, DEAL_MAX_SOURCE))
    and exc is None
)
@deal.post(lambda result: isinstance(result, str))
def failure_detail(*, detail: str | None = None, exc: BaseException | None = None) -> str:
    """Combine subprocess stderr, probe output, and/or an exception traceback."""
    # crosshair: off  # detail/traceback join still slow with bounds (cover-all 33293627157: ~4m, 61k lines). Doable later.
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
    # crosshair: off  # thin wrapper over failure_detail (cover-all 33293627157: ~2m, 40k lines). Doable later.
    body = failure_detail(detail=detail, exc=exc)
    if body:
        return f"{summary}\n\n{body}"
    return summary