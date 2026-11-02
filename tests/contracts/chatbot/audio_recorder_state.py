"""Contracts sidecar for plugin.chatbot.audio_recorder_state."""

from __future__ import annotations

import deal

from plugin.chatbot import audio_recorder_state as _prod
from plugin.chatbot.audio_recorder_state import (
    AudioRecorderEvent,
    AudioRecorderState,
    ErrorOccurredEvent,
    ReportErrorEffect,
    _VALID_AUDIO_STATUSES,
)
from plugin.framework.service import FsmTransition
from tests.contracts.bounds import DEAL_MAX_SOURCE, str_bounded


def _audio_transition_invariants(
    state: AudioRecorderState,
    event: AudioRecorderEvent,
    result: FsmTransition[AudioRecorderState],
) -> bool:
    if result.state.status not in _VALID_AUDIO_STATUSES:
        return False
    if isinstance(event, ErrorOccurredEvent):
        if result.state.status != "error":
            return False
        if not any(isinstance(e, ReportErrorEffect) for e in result.effects):
            return False
    return True


next_state = deal.pre(
    lambda state, event: state.status in _VALID_AUDIO_STATUSES
    and (
        not isinstance(event, ErrorOccurredEvent)
        or str_bounded(event.error_message, DEAL_MAX_SOURCE)
    )
)(
    deal.ensure(_audio_transition_invariants)(_prod.next_state)
)

PYTEST = [
    "next_state",
]

TARGETS = [
    f"{__name__}.next_state",
]
