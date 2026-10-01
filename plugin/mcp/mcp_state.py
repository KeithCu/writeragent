import dataclasses
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from plugin.framework.service import BaseState, FsmTransition

from plugin.framework.deal_shim import deal


# --- States ---
class MCPStateStr(Enum):
    IDLE = "idle"
    PARSING_REQUEST = "parsing_request"
    EXECUTING_TOOL = "executing_tool"
    STREAMING_RESPONSE = "streaming_response"  # Despite name, we send a single JSON-RPC response
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class MCPState(BaseState):
    status: MCPStateStr
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = dataclasses.field(default_factory=dict)
    document_url: Optional[str] = None
    result: Any = None  # The final result payload
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    is_long_running: bool = False
    is_error: bool = False


# --- Events ---
class EventKind(Enum):
    REQUEST_RECEIVED = auto()
    TOOL_EXECUTION_STARTED = auto()
    TOOL_COMPLETED = auto()
    REQUEST_ERROR = auto()


@dataclasses.dataclass(frozen=True)
class MCPEvent:
    kind: EventKind
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)


# --- Effects ---


@dataclasses.dataclass(frozen=True)
class ParseRequestEffect:
    pass


@dataclasses.dataclass(frozen=True)
class ExecuteToolEffect:
    """Interpreter runs document resolve + tool body; this effect only names the call."""

    tool_name: str
    arguments: Dict[str, Any]
    is_long_running: bool
    document_url: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class StreamResponseEffect:
    result: Any
    is_error: bool


@dataclasses.dataclass(frozen=True)
class SendErrorEffect:
    message: str
    code: str


# --- State Machine Transition ---


@deal.ensure(lambda state, event, result: bool(event.data.get("tool_name")) or event.kind != EventKind.REQUEST_RECEIVED or (result.state.is_error and any(isinstance(e, SendErrorEffect) for e in result.effects)))
@deal.ensure(lambda state, event, result: event.kind != EventKind.TOOL_COMPLETED or any(isinstance(e, StreamResponseEffect) for e in result.effects))
@deal.ensure(lambda state, event, result: event.kind != EventKind.REQUEST_ERROR or result.state.is_error)
def next_state(state: MCPState, event: MCPEvent) -> FsmTransition[MCPState]:
    """Pure transition function for the MCP tool-calling loop."""
    # event.data is Dict[str, Any] (nested tool arguments); same class as
    # state_machine.next_state. Hypothesis covers transitions; check-all skips.
    # crosshair: off
    effects: List[Any] = []

    if event.kind == EventKind.REQUEST_RECEIVED:
        tool_name = event.data.get("tool_name")
        arguments = event.data.get("arguments", {})
        document_url = event.data.get("document_url")
        is_long_running = event.data.get("is_long_running", False)

        if not tool_name:
            effects.append(SendErrorEffect(message="Missing 'name' in tools/call params", code="INVALID_PARAMS"))
            return FsmTransition(dataclasses.replace(state, status=MCPStateStr.ERROR, is_error=True), effects)

        effects.append(ParseRequestEffect())
        effects.append(ExecuteToolEffect(tool_name=tool_name, arguments=arguments, is_long_running=is_long_running, document_url=document_url))
        return FsmTransition(dataclasses.replace(state, status=MCPStateStr.EXECUTING_TOOL, tool_name=tool_name, arguments=arguments, document_url=document_url, is_long_running=is_long_running), effects)

    elif event.kind == EventKind.TOOL_EXECUTION_STARTED:
        # Just an informational event, we stay in EXECUTING_TOOL
        return FsmTransition(state, effects)

    elif event.kind == EventKind.TOOL_COMPLETED:
        result = event.data.get("result")
        is_error = isinstance(result, dict) and result.get("status") == "error"

        effects.append(StreamResponseEffect(result=result, is_error=is_error))
        return FsmTransition(dataclasses.replace(state, status=MCPStateStr.STREAMING_RESPONSE, result=result, is_error=is_error), effects)

    elif event.kind == EventKind.REQUEST_ERROR:
        message = event.data.get("message", "Unknown error")
        code = event.data.get("code", "INTERNAL_ERROR")

        # Determine if we should send a raw exception bubble-up or stream response effect
        # For simplicity, we can trigger StreamResponseEffect with an error payload
        err_payload = {"status": "error", "code": code, "message": message}
        effects.append(StreamResponseEffect(result=err_payload, is_error=True))
        return FsmTransition(dataclasses.replace(state, status=MCPStateStr.ERROR, is_error=True, error_message=message, error_code=code, result=err_payload), effects)

    return FsmTransition(state, effects)
