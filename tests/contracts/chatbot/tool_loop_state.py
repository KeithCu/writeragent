"""Contracts sidecar for plugin.chatbot.tool_loop_state."""

from __future__ import annotations

import deal

from plugin.chatbot import tool_loop_state as _prod

next_state = deal.pre(
    lambda state, event: isinstance(state.max_rounds, int)
    and state.max_rounds > 0
    and state.round_num >= 0
)(
    deal.ensure(_prod._transition_invariants)(_prod.next_state)
)

PYTEST = [
    "next_state",
]

TARGETS: list[str] = []
