#!/usr/bin/env python3
"""
Generate FULL super-unified state machine diagram for WriterAgent.
Includes ALL components: Send Button, Send Handler, Tool Loop, Web Search, AND Streaming Deltas.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict, Any
import json


class Component(str, Enum):
    """Component categories for color coding"""
    SEND_BUTTON = "send_button"
    SEND_HANDLER = "send_handler"
    TOOL_LOOP = "tool_loop"
    WEB_SEARCH = "web_search"
    STREAMING = "streaming"


@dataclass
class UnifiedState:
    """Represents a state in the super-unified state machine"""
    name: str
    description: str
    component: Component
    is_terminal: bool = False
    is_error: bool = False
    is_initial: bool = False


@dataclass
class UnifiedTransition:
    """Represents a transition between states"""
    from_state: str
    to_state: str
    trigger: str
    description: str
    component: Component
    condition: Optional[str] = None


class FullSuperUnifiedStateMachine:
    """Complete super-unified state machine with all components"""
    
    def __init__(self):
        self.states = self._define_states()
        self.transitions = self._define_transitions()
        self.state_map = {s.name: s for s in self.states}
    
    def _define_states(self) -> List[UnifiedState]:
        return [
            # Send Button Component
            UnifiedState(
                name="SendButton_Ready",
                description="Send button ready for input",
                component=Component.SEND_BUTTON,
                is_initial=True
            ),
            UnifiedState(
                name="SendButton_Recording",
                description="Audio recording in progress",
                component=Component.SEND_BUTTON
            ),
            UnifiedState(
                name="SendButton_Sending",
                description="Send operation in progress",
                component=Component.SEND_BUTTON
            ),
            UnifiedState(
                name="SendButton_StopRecording",
                description="Stop recording state",
                component=Component.SEND_BUTTON
            ),
            
            # Send Handler Component
            UnifiedState(
                name="SendHandler_Ready",
                description="Ready to handle send request",
                component=Component.SEND_HANDLER
            ),
            UnifiedState(
                name="SendHandler_Starting",
                description="Starting send operation",
                component=Component.SEND_HANDLER
            ),
            UnifiedState(
                name="SendHandler_Streaming",
                description="Streaming response content",
                component=Component.SEND_HANDLER
            ),
            UnifiedState(
                name="SendHandler_Done",
                description="Send operation completed",
                component=Component.SEND_HANDLER,
                is_terminal=True
            ),
            UnifiedState(
                name="SendHandler_Error",
                description="Send operation failed",
                component=Component.SEND_HANDLER,
                is_terminal=True,
                is_error=True
            ),
            UnifiedState(
                name="SendHandler_Stopped",
                description="Send operation stopped",
                component=Component.SEND_HANDLER,
                is_terminal=True
            ),
            
            # Tool Loop Component
            UnifiedState(
                name="ToolLoop_Idle",
                description="Waiting for tool calls",
                component=Component.TOOL_LOOP
            ),
            UnifiedState(
                name="ToolLoop_HasToolCalls",
                description="Tool calls received from agent",
                component=Component.TOOL_LOOP
            ),
            UnifiedState(
                name="ToolLoop_ToolResult",
                description="Tool execution completed",
                component=Component.TOOL_LOOP
            ),
            UnifiedState(
                name="ToolLoop_HasMoreTools",
                description="More tool calls available",
                component=Component.TOOL_LOOP
            ),
            UnifiedState(
                name="ToolLoop_Done",
                description="Tool loop completed",
                component=Component.TOOL_LOOP,
                is_terminal=True
            ),
            
            # Web Search Component
            UnifiedState(
                name="WebSearch_ApprovalRequired",
                description="Waiting for user approval",
                component=Component.WEB_SEARCH
            ),
            UnifiedState(
                name="WebSearch_Approved",
                description="User approved web search",
                component=Component.WEB_SEARCH
            ),
            UnifiedState(
                name="WebSearch_Rejected",
                description="User rejected web search",
                component=Component.WEB_SEARCH,
                is_terminal=True
            ),
            UnifiedState(
                name="WebSearch_Executing",
                description="Web search in progress",
                component=Component.WEB_SEARCH
            ),
            UnifiedState(
                name="WebSearch_Complete",
                description="Web search completed",
                component=Component.WEB_SEARCH,
                is_terminal=True
            ),
            
            # Streaming Deltas Component - FULL SET
            UnifiedState(
                name="Streaming_Idle",
                description="Streaming system idle",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_Content",
                description="Streaming content chunks",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_Thinking",
                description="Streaming thinking indicators",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_ToolCalling",
                description="Processing tool call from agent",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_ToolResult",
                description="Displaying tool execution result",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_ToolThinking",
                description="Showing tool execution progress",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_ApprovalRequired",
                description="Waiting for user approval (HITL)",
                component=Component.STREAMING
            ),
            UnifiedState(
                name="Streaming_Done",
                description="Stream completed successfully",
                component=Component.STREAMING,
                is_terminal=True
            ),
            UnifiedState(
                name="Streaming_FinalDone",
                description="Final response received",
                component=Component.STREAMING,
                is_terminal=True
            ),
            UnifiedState(
                name="Streaming_Stopped",
                description="Stream stopped by user",
                component=Component.STREAMING,
                is_terminal=True
            ),
            UnifiedState(
                name="Streaming_Error",
                description="Stream encountered error",
                component=Component.STREAMING,
                is_terminal=True,
                is_error=True
            ),
        ]
    
    def _define_transitions(self) -> List[UnifiedTransition]:
        transitions = []
        
        # Send Button Transitions
        transitions.extend([
            UnifiedTransition(
                from_state="SendButton_Ready",
                to_state="SendButton_Recording",
                trigger="Record button clicked",
                description="Start audio recording",
                component=Component.SEND_BUTTON
            ),
            UnifiedTransition(
                from_state="SendButton_Recording",
                to_state="SendButton_Sending",
                trigger="Send button clicked",
                description="Stop recording, start sending",
                component=Component.SEND_BUTTON
            ),
            UnifiedTransition(
                from_state="SendButton_Sending",
                to_state="SendButton_StopRecording",
                trigger="Stop button clicked",
                description="Stop current operation",
                component=Component.SEND_BUTTON
            ),
            UnifiedTransition(
                from_state="SendButton_StopRecording",
                to_state="SendButton_Ready",
                trigger="Operation completed",
                description="Return to ready state",
                component=Component.SEND_BUTTON
            ),
        ])
        
        # Send Handler Transitions
        transitions.extend([
            UnifiedTransition(
                from_state="SendHandler_Ready",
                to_state="SendHandler_Starting",
                trigger="Send initiated",
                description="Begin send operation",
                component=Component.SEND_HANDLER
            ),
            UnifiedTransition(
                from_state="SendHandler_Starting",
                to_state="SendHandler_Streaming",
                trigger="Stream started",
                description="Begin streaming response",
                component=Component.SEND_HANDLER
            ),
            UnifiedTransition(
                from_state="SendHandler_Streaming",
                to_state="SendHandler_Done",
                trigger="Stream complete",
                description="Streaming completed successfully",
                component=Component.SEND_HANDLER
            ),
            UnifiedTransition(
                from_state="SendHandler_Streaming",
                to_state="SendHandler_Error",
                trigger="Error occurred",
                description="Streaming failed",
                component=Component.SEND_HANDLER
            ),
            UnifiedTransition(
                from_state="SendHandler_Streaming",
                to_state="SendHandler_Stopped",
                trigger="User stopped",
                description="User stopped the operation",
                component=Component.SEND_HANDLER
            ),
        ])
        
        # Tool Loop Transitions
        transitions.extend([
            UnifiedTransition(
                from_state="ToolLoop_Idle",
                to_state="ToolLoop_HasToolCalls",
                trigger="Tool calls received",
                description="Agent returned tool calls",
                component=Component.TOOL_LOOP
            ),
            UnifiedTransition(
                from_state="ToolLoop_HasToolCalls",
                to_state="ToolLoop_ToolResult",
                trigger="Tool executed",
                description="Tool execution completed",
                component=Component.TOOL_LOOP
            ),
            UnifiedTransition(
                from_state="ToolLoop_ToolResult",
                to_state="ToolLoop_HasMoreTools",
                trigger="More tools available",
                description="Additional tool calls to process",
                component=Component.TOOL_LOOP
            ),
            UnifiedTransition(
                from_state="ToolLoop_HasMoreTools",
                to_state="ToolLoop_ToolResult",
                trigger="Tool executed",
                description="Execute next tool",
                component=Component.TOOL_LOOP
            ),
            UnifiedTransition(
                from_state="ToolLoop_ToolResult",
                to_state="ToolLoop_Done",
                trigger="No more tools",
                description="All tools processed",
                component=Component.TOOL_LOOP
            ),
        ])
        
        # Web Search Transitions
        transitions.extend([
            UnifiedTransition(
                from_state="WebSearch_ApprovalRequired",
                to_state="WebSearch_Approved",
                trigger="User approved",
                description="User granted approval",
                component=Component.WEB_SEARCH
            ),
            UnifiedTransition(
                from_state="WebSearch_ApprovalRequired",
                to_state="WebSearch_Rejected",
                trigger="User rejected",
                description="User denied approval",
                component=Component.WEB_SEARCH
            ),
            UnifiedTransition(
                from_state="WebSearch_Approved",
                to_state="WebSearch_Executing",
                trigger="Execution started",
                description="Begin web search execution",
                component=Component.WEB_SEARCH
            ),
            UnifiedTransition(
                from_state="WebSearch_Executing",
                to_state="WebSearch_Complete",
                trigger="Execution complete",
                description="Web search finished",
                component=Component.WEB_SEARCH
            ),
        ])
        
        # Streaming Deltas Transitions - COMPLETE SET
        transitions.extend([
            # Initial transitions
            UnifiedTransition(
                from_state="Streaming_Idle",
                to_state="Streaming_Content",
                trigger="CHUNK received",
                description="Start receiving content chunks",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Idle",
                to_state="Streaming_Thinking",
                trigger="THINKING received",
                description="Start receiving thinking indicators",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Idle",
                to_state="Streaming_ToolCalling",
                trigger="TOOL_CALL received",
                description="Agent backend tool call",
                component=Component.STREAMING
            ),
            
            # Content streaming transitions
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="Streaming_Thinking",
                trigger="THINKING received",
                description="Switch to thinking mode",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="Streaming_ToolCalling",
                trigger="TOOL_CALL received",
                description="Process tool call",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="Streaming_ApprovalRequired",
                trigger="APPROVAL_REQUIRED received",
                description="Human-in-the-loop approval needed",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="Streaming_Done",
                trigger="STREAM_DONE received",
                description="Content stream completed",
                component=Component.STREAMING
            ),
            
            # Thinking transitions
            UnifiedTransition(
                from_state="Streaming_Thinking",
                to_state="Streaming_Content",
                trigger="CHUNK received",
                description="Switch back to content mode",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Thinking",
                to_state="Streaming_ToolThinking",
                trigger="TOOL_THINKING received",
                description="Tool execution in progress",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Thinking",
                to_state="Streaming_Done",
                trigger="STREAM_DONE received",
                description="Thinking stream completed",
                component=Component.STREAMING
            ),
            
            # Tool-related transitions
            UnifiedTransition(
                from_state="Streaming_ToolCalling",
                to_state="Streaming_ToolResult",
                trigger="TOOL_RESULT received",
                description="Tool execution completed",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolCalling",
                to_state="Streaming_ToolThinking",
                trigger="TOOL_THINKING received",
                description="Tool execution in progress",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolResult",
                to_state="Streaming_Content",
                trigger="CHUNK received",
                description="Continue with content after tool result",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolThinking",
                to_state="Streaming_ToolResult",
                trigger="TOOL_RESULT received",
                description="Tool execution completed",
                component=Component.STREAMING
            ),
            
            # Approval flow
            UnifiedTransition(
                from_state="Streaming_ApprovalRequired",
                to_state="Streaming_Content",
                trigger="Approval granted",
                description="User approved, continue streaming",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ApprovalRequired",
                to_state="Streaming_Stopped",
                trigger="Approval denied",
                description="User rejected, stop streaming",
                component=Component.STREAMING
            ),
            
            # Terminal transitions
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="Streaming_Stopped",
                trigger="STOPPED received",
                description="User stopped the stream",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Thinking",
                to_state="Streaming_Stopped",
                trigger="STOPPED received",
                description="User stopped during thinking",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolCalling",
                to_state="Streaming_Stopped",
                trigger="STOPPED received",
                description="User stopped during tool call",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolResult",
                to_state="Streaming_Stopped",
                trigger="STOPPED received",
                description="User stopped after tool result",
                component=Component.STREAMING
            ),
            
            # Error transitions (can happen from any state)
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="Streaming_Error",
                trigger="ERROR received",
                description="Stream error occurred",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Thinking",
                to_state="Streaming_Error",
                trigger="ERROR received",
                description="Stream error during thinking",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolCalling",
                to_state="Streaming_Error",
                trigger="ERROR received",
                description="Tool call error",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ToolResult",
                to_state="Streaming_Error",
                trigger="ERROR received",
                description="Tool result error",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_ApprovalRequired",
                to_state="Streaming_Error",
                trigger="ERROR received",
                description="Approval error",
                component=Component.STREAMING
            ),
        ])
        
        # Cross-component transitions
        transitions.extend([
            UnifiedTransition(
                from_state="SendButton_Sending",
                to_state="SendHandler_Starting",
                trigger="Send handler activated",
                description="Button triggers send handler",
                component=Component.SEND_BUTTON
            ),
            UnifiedTransition(
                from_state="SendHandler_Streaming",
                to_state="Streaming_Content",
                trigger="Streaming started",
                description="Handler activates streaming",
                component=Component.SEND_HANDLER
            ),
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="ToolLoop_HasToolCalls",
                trigger="Tool calls detected",
                description="Streaming triggers tool loop",
                component=Component.STREAMING
            ),
            UnifiedTransition(
                from_state="Streaming_Content",
                to_state="WebSearch_ApprovalRequired",
                trigger="Web search requested",
                description="Streaming requests web search",
                component=Component.STREAMING
            ),
            # Connect streaming approval to web search component
            UnifiedTransition(
                from_state="Streaming_ApprovalRequired",
                to_state="WebSearch_ApprovalRequired",
                trigger="Approval dialog shown",
                description="Streaming hands off to web search approval",
                component=Component.STREAMING
            ),
            # Connect web search back to streaming
            UnifiedTransition(
                from_state="WebSearch_Complete",
                to_state="Streaming_Content",
                trigger="Web search finished",
                description="Web search returns to streaming",
                component=Component.WEB_SEARCH
            ),
            # Connect tool loop back to streaming
            UnifiedTransition(
                from_state="ToolLoop_Done",
                to_state="Streaming_Content",
                trigger="Tools completed",
                description="Tool loop returns to streaming",
                component=Component.TOOL_LOOP
            ),
        ])
        
        return transitions
    
    def to_mermaid(self) -> str:
        """Convert state machine to Mermaid.js diagram"""
        
        # Component colors
        component_colors = {
            Component.SEND_BUTTON: "#ff6b6b",
            Component.SEND_HANDLER: "#4a90e2",
            Component.TOOL_LOOP: "#50c878",
            Component.WEB_SEARCH: "#f06292",
            Component.STREAMING: "#9575cd"
        }
        
        # Build Mermaid diagram (without triple backticks for mmdc)
        lines = []
        lines.append("stateDiagram-v2")
        lines.append("    %% Full Super-Unified State Machine")
        lines.append("    %% All 5 components with complete streaming deltas")
        
        # Define state styles
        for component, color in component_colors.items():
            lines.append(f"    classDef {component.value} fill:{color},color:white,stroke:#333")
        lines.append("    classDef terminal fill:#7b68ee,color:white,stroke:#333")
        lines.append("    classDef error fill:#ff4444,color:white,stroke:#333")
        
        # Add initial state
        lines.append("    [*] --> SendButton_Ready")
        
        # Add states
        for state in self.states:
            state_name = state.name.replace(" ", "")
            
            # Determine class based on component and type
            if state.is_error:
                state_class = "error"
            elif state.is_terminal:
                state_class = "terminal"
            else:
                state_class = state.component.value
            
            # Format state definition
            lines.append(f"    {state_name}")
            lines.append(f"    class {state_name} {state_class}")
        
        # Add transitions
        for trans in self.transitions:
            from_name = trans.from_state.replace(" ", "")
            to_name = trans.to_state.replace(" ", "")
            
            # Format transition label
            label = trans.trigger
            if trans.condition:
                label += f"\n[{trans.condition}]"
            
            lines.append(f"    {from_name} --> {to_name} : {label}")
        
        # Add legend
        lines.append("    %% Legend")
        lines.append("    note right of SendButton_Ready")
        lines.append("        Component Colors:")
        for component, color in component_colors.items():
            lines.append(f"        {component.value.replace('_', ' ').title()}: {color}")
        lines.append("    end note")
        
        return "\n".join(lines)
    
    def to_markdown(self) -> str:
        """Generate comprehensive markdown documentation"""
        lines = []
        lines.append("# Full Super-Unified WriterAgent State Machine")
        lines.append("")
        lines.append("This diagram integrates ALL 5 state machines from WriterAgent:")
        lines.append("- Send Button State Machine")
        lines.append("- Send Handler State Machine")
        lines.append("- Tool Loop State Machine")
        lines.append("- Web Search Approval Flow")
        lines.append("- Streaming Deltas State Machine (COMPLETE)")
        lines.append("")
        
        # Component legend
        lines.append("## Component Legend")
        lines.append("")
        lines.append("| Component | Color | Description |")
        lines.append("|-----------|-------|-------------|")
        lines.append("| Send Button | #ff6b6b | Button state management |")
        lines.append("| Send Handler | #4a90e2 | Send operation lifecycle |")
        lines.append("| Tool Loop | #50c878 | Tool calling workflow |")
        lines.append("| Web Search | #f06292 | Web search approval |")
        lines.append("| Streaming | #9575cd | Queue-based streaming |")
        lines.append("")
        
        # States by component
        lines.append("## States by Component")
        lines.append("")
        
        for component in Component:
            lines.append(f"### {component.value.replace('_', ' ').title()}")
            lines.append("")
            lines.append("| State | Description | Terminal | Error |")
            lines.append("|-------|-------------|----------|-------|")
            
            component_states = [s for s in self.states if s.component == component]
            for state in component_states:
                terminal = "✓" if state.is_terminal else ""
                error = "✓" if state.is_error else ""
                lines.append(f"| `{state.name}` | {state.description} | {terminal} | {error} |")
            lines.append("")
        
        # Transitions summary
        lines.append("## Transition Summary")
        lines.append("")
        lines.append(f"Total states: {len(self.states)}")
        lines.append(f"Total transitions: {len(self.transitions)}")
        lines.append(f"Components: {len(Component)}")
        lines.append("")
        
        # Key interactions
        lines.append("## Key Cross-Component Interactions")
        lines.append("")
        lines.append("- **Send Button → Send Handler**: User interaction triggers send operations")
        lines.append("- **Send Handler → Streaming**: Send operations initiate streaming workflow")
        lines.append("- **Streaming → Tool Loop**: Tool calls detected in stream activate tool processing")
        lines.append("- **Streaming → Web Search**: Web search requests trigger approval workflow")
        lines.append("- **Streaming → Approval**: HITL approval required during streaming")
        lines.append("- **Tool Loop → Streaming**: Tool results return to streaming context")
        lines.append("- **Web Search → Streaming**: Web search completion returns to streaming")
        lines.append("")
        
        # Streaming deltas specifics
        lines.append("## Streaming Deltas Component Details")
        lines.append("")
        lines.append("The streaming component implements the queue-based architecture from `async_stream.py`:")
        lines.append("")
        lines.append("### StreamQueueKind Types")
        lines.append("")
        lines.append("```python")
        lines.append("CHUNK          = 'chunk'          # Content chunks")
        lines.append("THINKING       = 'thinking'       # Thinking indicators")
        lines.append("STATUS         = 'status'         # Status updates")
        lines.append("STREAM_DONE    = 'stream_done'    # Stream completion")
        lines.append("TOOL_CALL      = 'tool_call'      # Agent tool calls")
        lines.append("TOOL_RESULT    = 'tool_result'    # Tool results")
        lines.append("TOOL_THINKING  = 'tool_thinking'  # Tool progress")
        lines.append("APPROVAL_REQUIRED = 'approval_required' # HITL approval")
        lines.append("STOPPED        = 'stopped'        # User stop")
        lines.append("ERROR          = 'error'          # Errors")
        lines.append("```")
        lines.append("")
        
        # Architecture notes
        lines.append("## Architecture Notes")
        lines.append("")
        lines.append("- **Component Isolation**: Each component has clear responsibilities")
        lines.append("- **Event-Driven**: Transitions triggered by specific events/conditions")
        lines.append("- **Error Handling**: Comprehensive error states and recovery paths")
        lines.append("- **User Control**: Stop/approval states allow user intervention")
        lines.append("- **Queue-Based**: Streaming component uses queue architecture for responsiveness")
        lines.append("- **HITL Integration**: Human-in-the-loop approval points in streaming workflow")
        lines.append("")
        
        return "\n".join(lines)


def main():
    """Generate full super-unified diagram"""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python generate_full_super_unified_diagram.py <format>")
        print("Formats: mermaid, markdown")
        sys.exit(1)
    
    format_type = sys.argv[1].lower()
    machine = FullSuperUnifiedStateMachine()
    
    if format_type == "mermaid":
        print(machine.to_mermaid())
    elif format_type == "markdown":
        print(machine.to_markdown())
    else:
        print(f"Unknown format: {format_type}")
        sys.exit(1)


if __name__ == "__main__":
    main()