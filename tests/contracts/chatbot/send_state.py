"""Contracts sidecar for plugin.chatbot.send_state."""

from __future__ import annotations

import deal

from plugin.chatbot import send_state as _prod

next_state = deal.ensure(_prod._send_ui_invariants)(_prod.next_state)

PYTEST = [
    "next_state",
]

TARGETS: list[str] = []
