# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.editor_ipc."""

from __future__ import annotations

import deal

from plugin.scripting import editor_ipc as _prod
from tests.contracts.bounds import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, profile, str_bounded


def _deal_ipc_dict_ok_pytest(msg: object) -> bool:
    return isinstance(msg, dict)


def _deal_ipc_dict_ok_crosshair(msg: object, allow_nested: bool = True) -> bool:
    if not isinstance(msg, dict) or len(msg) > DEAL_MAX_CMD_ARGS:
        return False
    for k, v in msg.items():
        if not isinstance(k, str) or not str_bounded(k, DEAL_MAX_TOKEN):
            return False
        if not (
            isinstance(v, (str, int, float, bool))
            or (allow_nested and isinstance(v, dict) and _deal_ipc_dict_ok_crosshair(v, allow_nested=False))
        ):
            return False
    return True


_deal_ipc_dict_ok = profile(_deal_ipc_dict_ok_crosshair, _deal_ipc_dict_ok_pytest)

normalize_target = deal.pre(lambda target: target is None or _deal_ipc_dict_ok(target))(
    _prod.normalize_target
)

target_from_load = deal.pre(lambda msg: _deal_ipc_dict_ok(msg))(_prod.target_from_load)

target_identity_key = deal.pre(
    lambda target, default="": (target is None or _deal_ipc_dict_ok(target))
    and str_bounded(default, DEAL_MAX_TOKEN)
)(_prod.target_identity_key)

stamp_session = deal.pre(
    lambda msg, session_id, mode="", target=None: _deal_ipc_dict_ok(msg)
    and str_bounded(session_id, DEAL_MAX_TOKEN)
    and str_bounded(mode, DEAL_MAX_TOKEN)
    and (target is None or _deal_ipc_dict_ok(target))
)(_prod.stamp_session)

PYTEST = [
    "normalize_target",
    "target_from_load",
    "target_identity_key",
    "stamp_session",
]
TARGETS: list[str] = []
