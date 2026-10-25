# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Parent sidebar store: send slice and optional tool-loop slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from plugin.framework.service import BaseState
from plugin.chatbot.send_state import SendButtonState
from plugin.chatbot.tool_loop_state import ToolLoopState


@dataclass(frozen=True)
class SidebarCompositeState(BaseState):
    send: SendButtonState
    tool_loop: Optional[ToolLoopState]
