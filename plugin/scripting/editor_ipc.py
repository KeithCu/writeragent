# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor IPC protocol (pickle protocol 5)."""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify IPC frame structures or protocol message envelopes here,
# you MUST also update the corresponding JavaScript / Python files:
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor_shell.js
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Host Bridge:              plugin/scripting/editor_host.py
# Parent-to-child types that the child queues are PARENT_TO_CHILD_TYPES in this file, and a new type is added there only when the host sends it and editor_shell.js or scripts_manager.js handles it.
# =========================================================================================

from __future__ import annotations

from typing import Any, IO, Mapping

from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_TOKEN,
    _profile,
    ascii_bounded,
    deal,
)
from plugin.scripting.ipc import (
    _write_all,
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcPayloadSizeError,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
)

__all__ = [
    "EDITOR_DEFAULT_TITLE",
    "PARENT_TO_CHILD_TYPES",
    "message_type",
    "normalize_target",
    "read_message",
    "session_id_of",
    "stamp_session",
    "target_from_load",
    "target_identity_key",
    "write_message",
]

# Same placeholder as <title> </title> in
# plugin/contrib/scripting/assets/editor/index.html. The localized title
# arrives on the load frame: editor_main sets window.title when msg["title"]
# is truthy, and editor_shell.js sets document.title from that field.
EDITOR_DEFAULT_TITLE = " "

# Queued by the editor child for poll_messages. ``closed`` is not here:
# the child reader treats it as process shutdown. ``theme`` is handled
# by editor_shell.js; the host currently attaches theme data to ``load``.
PARENT_TO_CHILD_TYPES = frozenset({
    "saved",
    "error",
    "load",
    "request_save",
    "theme",
    "scripts_list",
    "unsaved_choice",
})

# Identity keys on every session message (omit empties).
_TARGET_KEYS = ("cell_address", "script_name", "script_origin", "doc_url", "resource")

# Top-level load names that fill a target key when the nested target omitted
# it. First non-empty stripped str wins. selected_script_name is before
# script_name so a disagreeing pair uses the picker field.
_LOAD_ALIAS_SOURCES: dict[str, tuple[str, ...]] = {
    "script_name": ("selected_script_name", "script_name"),
}


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one pickle-framed message from *stream*.

    Returns None on clean EOF (zero bytes at a frame boundary). EOF after at
    least one byte of a frame raises IpcFrameReadError.
    The child reader treats clean EOF as shutdown.
    """
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
    except IpcPayloadSizeError as exc:
        # pack_pickle_frame's message includes the byte count. A fixed
        # sentence here dropped that count from str(exc) at the host dialog.
        raise ValueError(str(exc)) from exc
    _write_all(stream, frame)


def message_type(message: dict[str, Any]) -> str:
    """Return the ``type`` field or empty string."""
    raw = message.get("type")
    return str(raw) if raw is not None else ""


def _deal_ipc_dict_ok_pytest(msg: object) -> bool:
    # Editor IPC carries script source, doc URLs, and stderr. The old pre
    # capped every string at DEAL_MAX_TOKEN (64) and the dict at 32 keys, so
    # stamp_session raised PreContractError before it copied the message.
    # A Mapping of any size is in domain; the body reads the keys it knows.
    # Mapping, not dict: the signatures are Mapping so dict[str, str] targets
    # typecheck (pyright dict is invariant). A dict-only pre raised
    # PreContractError for any other Mapping while LibreOffice, where deal is
    # stubbed, accepted it. CrossHair stays type(msg) is dict.
    return isinstance(msg, Mapping)


def _deal_ipc_dict_ok_crosshair(msg: object, allow_nested: bool = True) -> bool:
    return type(msg) is dict and len(msg) <= DEAL_MAX_CMD_ARGS and all(
        type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) and (
            v is None
            or (isinstance(v, str) and ascii_bounded(v, DEAL_MAX_TOKEN))
            or (allow_nested and isinstance(v, dict) and _deal_ipc_dict_ok_crosshair(v, allow_nested=False))
        )
        for k, v in msg.items()
    )


_deal_ipc_dict_ok = _profile(_deal_ipc_dict_ok_crosshair, _deal_ipc_dict_ok_pytest)


@deal.pre(lambda target: target is None or _deal_ipc_dict_ok(target))
def normalize_target(target: Mapping[str, Any] | None) -> dict[str, str]:
    """Copy ``_TARGET_KEYS`` whose values are non-empty strings.

    Drop None, whitespace-only strings, and every non-str, including UNO
    objects. Do not stringify. Empty input returns ``{}``.
    """
    # crosshair: off
    if not target:
        return {}
    out: dict[str, str] = {}
    for key in _TARGET_KEYS:
        raw = target.get(key)
        # str() on a UNO object or other non-str used to land in
        # target_identity_key. Callers build these fields as str; a non-str
        # must not become a session id.
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if text:
            out[key] = text
    return out


@deal.pre(lambda msg: _deal_ipc_dict_ok(msg))
def target_from_load(msg: Mapping[str, Any]) -> dict[str, str]:
    """Build ``target`` from an explicit dict plus top-level load aliases."""
    # crosshair: off
    raw = msg.get("target")
    target = normalize_target(raw if isinstance(raw, Mapping) else None)
    for dest in _TARGET_KEYS:
        if dest in target:
            continue
        for src in _LOAD_ALIAS_SOURCES.get(dest, (dest,)):
            value = msg.get(src)
            # Same string-only rule as normalize_target. Aliases must not
            # stringify a value the nested target just dropped.
            if not isinstance(value, str):
                continue
            text = value.strip()
            if text:
                target[dest] = text
                break
    return target


@deal.pre(
    lambda mode, target: ascii_bounded(mode, DEAL_MAX_TOKEN)
    and (target is None or _deal_ipc_dict_ok(target))
)
def target_identity_key(mode: str, target: Mapping[str, str] | None) -> tuple[str, ...]:
    """Stable key so reopening the same cell/script reuses ``session_id``."""
    # crosshair: off
    t = normalize_target(target)
    # One list. A hand-written tuple omitted script_origin, so a user script and
    # a document script with the same name shared a session_id and a save wrote
    # the other target.
    return (str(mode or ""), *(t.get(key, "") for key in _TARGET_KEYS))


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
    """Copy *msg* and attach routing fields.

    ``session_id`` is always set. ``target`` is always set to the normalized
    identity dict, which may be empty. ``mode`` is set only when *mode* or
    the copied message's ``mode`` is a non-empty string.
    """
    # crosshair: off
    out = dict(msg)
    out["session_id"] = str(session_id or "")
    use_mode = str(mode or out.get("mode") or "")
    if use_mode:
        out["mode"] = use_mode
    raw_target = out.get("target")
    merged = dict(raw_target) if isinstance(raw_target, Mapping) else {}
    if target:
        merged.update(dict(target))
    out["target"] = normalize_target(merged)
    return out