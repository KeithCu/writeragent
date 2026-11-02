"""Contracts sidecar for plugin.chatbot.state_machine."""

from __future__ import annotations

import deal

from plugin.chatbot import state_machine as _prod
from plugin.chatbot.state_machine import StopRequestedEvent
from tests.contracts.bounds import DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, str_bounded

stop_effects_exclude_spawns = deal.post(lambda result: type(result) is bool)(
    _prod.stop_effects_exclude_spawns
)

ui_lines_for_handler_error = deal.pre(
    lambda handler_type, err_msg: str_bounded(handler_type, DEAL_MAX_TOKEN)
    and str_bounded(err_msg, DEAL_MAX_SOURCE)
)(
    deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))(
        _prod.ui_lines_for_handler_error
    )
)

spawn_effects_for_start = deal.pre(
    lambda handler_type, query_text, model, doc_type_str, mode="": (
        handler_type in ("image", "agent", "web")
        and str_bounded(handler_type, DEAL_MAX_TOKEN)
        and str_bounded(query_text, DEAL_MAX_SOURCE)
        and str_bounded(doc_type_str, DEAL_MAX_TOKEN)
        and str_bounded(mode, DEAL_MAX_TOKEN)
    )
)(
    deal.post(lambda result: isinstance(result, list))(
        _prod.spawn_effects_for_start
    )
)

next_state = deal.ensure(
    lambda state, event, result: (not isinstance(event, StopRequestedEvent))
    or _prod.stop_effects_exclude_spawns(result.effects)
)(_prod.next_state)

PYTEST = [
    "stop_effects_exclude_spawns",
    "ui_lines_for_handler_error",
    "spawn_effects_for_start",
    "next_state",
]

TARGETS = [
    f"{__name__}.stop_effects_exclude_spawns",
    f"{__name__}.ui_lines_for_handler_error",
    f"{__name__}.spawn_effects_for_start",
]
