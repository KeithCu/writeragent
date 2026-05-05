# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2025-2026 quazardous (config, registries, build system)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from enum import IntFlag
from typing import Any, Literal, TypedDict


# Model capabilities bitmasks (compatible with OnlyOfficeAI values)
class ModelCapability(IntFlag):
    NONE = 0
    CHAT = 1
    IMAGE = 2
    EMBEDDINGS = 4
    AUDIO = 8
    MODERATIONS = 16
    REALTIME = 32
    CODE = 64
    VISION = 128
    TOOLS = 256


# Status values for tool execution results
StatusValue = Literal["ok", "error"]


# Type for tool execution results (base type)
class ToolResult(TypedDict, total=False):
    status: StatusValue
    code: str
    message: str
    details: dict[str, Any]


# Type for successful tool execution results
class ToolSuccess(TypedDict):
    status: Literal["ok"]
    # Other fields are optional in success case


# Type for failed tool execution results
class ToolError(TypedDict):
    status: Literal["error"]
    code: str
    message: str
    details: dict[str, Any]


# Send-handler FSM (plugin.modules.chatbot.state_machine)
SendHandlerKind = Literal["audio", "image", "agent", "web"]
SendHandlerFsmStatus = Literal["ready", "starting", "running", "done", "error", "stopped"]
# CompleteJobEffect.terminal_status (UI / job completion; capitalized)
SendHandlerCompleteStatus = Literal["Error", "Stopped", "Ready"]

# Tool-loop and send-handler UI channel effects (see ToolLoopUIEffect, SendHandlerUIEffect)
UIEffectKind = Literal["append", "status", "debug", "info"]
