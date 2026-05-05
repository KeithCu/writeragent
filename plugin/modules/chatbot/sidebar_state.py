# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Parent sidebar store: send slice, optional tool-loop slice, mirrored audio slice.

Pure routing via :func:`sidebar_next_state`; no I/O or logging inside transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, cast

from plugin.framework.state import BaseState, FsmTransition
from plugin.modules.chatbot.audio_recorder_state import AudioRecorderState
from plugin.modules.chatbot.send_state import SendButtonState, SendEvent, next_state as send_next_state
from plugin.modules.chatbot.tool_loop_state import ToolLoopEvent, ToolLoopState, next_state as tool_loop_next_state


class SidebarEventKind(str, Enum):
    SEND = "send"
    TOOL_LOOP = "tool_loop"
    AUDIO = "audio"


@dataclass(frozen=True)
class SidebarEvent:
    kind: SidebarEventKind
    payload: Any


@dataclass(frozen=True)
class LogSidebarEffect:
    """Interpreter should log ``message`` (e.g. debug); no I/O in the FSM."""

    message: str


@dataclass(frozen=True)
class SidebarCompositeState(BaseState):
    send: SendButtonState
    tool_loop: Optional[ToolLoopState]
    audio: AudioRecorderState


def sidebar_next_state(composite: SidebarCompositeState, event: SidebarEvent) -> FsmTransition[SidebarCompositeState]:
    """Route a tagged sidebar event to the appropriate child FSM."""

    match event.kind:
        case SidebarEventKind.SEND:
            send_tr = send_next_state(composite.send, cast("SendEvent", event.payload))
            return FsmTransition(SidebarCompositeState(send=send_tr.state, tool_loop=composite.tool_loop, audio=composite.audio), list(send_tr.effects))
        case SidebarEventKind.TOOL_LOOP:
            if composite.tool_loop is None:
                return FsmTransition(composite, [LogSidebarEffect(message="Ignoring tool_loop event: no active session")])
            tool_loop_tr = tool_loop_next_state(composite.tool_loop, cast("ToolLoopEvent", event.payload))
            return FsmTransition(SidebarCompositeState(send=composite.send, tool_loop=tool_loop_tr.state, audio=composite.audio), list(tool_loop_tr.effects))
        case SidebarEventKind.AUDIO:
            # Strategy A: hardware stays in AudioRecorder; composite.audio is mirrored in the shell.
            return FsmTransition(composite, [])
        case _:
            return FsmTransition(composite, [])
