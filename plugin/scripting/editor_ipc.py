# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor IPC protocol (pickle protocol 5)."""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify IPC frame structures or protocol message envelopes here,
# you MUST also update the corresponding JavaScript / Python files:
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor.js
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Host Bridge:              plugin/scripting/editor_host.py
# =========================================================================================

from __future__ import annotations

import uuid
from typing import Any, IO, Mapping

from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_TOKEN,
    ascii_bounded,
    deal,
)
from plugin.scripting.editor_errors import (
    _profile,
    exception_traceback,
    failure_detail,
    failure_message,
)
from plugin.scripting.ipc import (
    _write_all,
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
)

__all__ = [
    "EDITOR_DEFAULT_TITLE",
    "exception_traceback",
    "failure_detail",
    "failure_message",
    "message_type",
    "new_session_id",
    "normalize_target",
    "read_message",
    "session_id_of",
    "stamp_session",
    "target_from_load",
    "target_identity_key",
    "write_message",
]

EDITOR_DEFAULT_TITLE = " "

# Identity keys on every session message (omit empties).
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
    _write_all(stream, frame)


def message_type(message: dict[str, Any]) -> str:
    """Return the ``type`` field or empty string."""
    raw = message.get("type")
    return str(raw) if raw is not None else ""


def new_session_id() -> str:
    """Opaque routing id for one editor buffer (host-minted)."""
    return uuid.uuid4().hex


def _deal_ipc_dict_ok_pytest(msg: object) -> bool:
    # Editor IPC carries script source, doc URLs, and stderr. The old pre
    # capped every string at DEAL_MAX_TOKEN (64) and the dict at 32 keys, so
    # stamp_session raised PreContractError before it copied the message.
    # A dict of any size is in domain; the body reads the keys it knows.
    return isinstance(msg, dict)


def _deal_ipc_dict_ok_crosshair(msg: object, allow_nested: bool = True) -> bool:
    return type(msg) is dict and len(msg) <= DEAL_MAX_CMD_ARGS and all(
        type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) and (
            v is None
            or (isinstance(v, str) and ascii_bounded(v, DEAL_MAX_TOKEN))
            or (allow_nested and isinstance(v, dict) and _deal_ipc_dict_ok_crosshair(v, allow_nested=False))
        )
        for k, v in msg.items()
    )


_deal_ipc_dict_ok = _profile(_deal_ipc_dict_ok_pytest, _deal_ipc_dict_ok_crosshair)


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
def target_identity_key(mode: str, target: Mapping[str, str] | None) -> tuple[str, str, str, str, str, str]:
    """Stable key so reopening the same cell/script reuses ``session_id``."""
    # crosshair: off  # nested IPC dict domain (cover-all 33293627157: ~9m, 159k lines). Doable later; thin wrapper over normalize_target.
    t = normalize_target(target)
    # Include script_origin. Omitting it gave user and document scripts with
    # the same name one session_id, so a save overwrote the other target.
    return (
        str(mode or ""),
        t.get("cell_address", ""),
        t.get("script_name", ""),
        t.get("script_origin", ""),
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