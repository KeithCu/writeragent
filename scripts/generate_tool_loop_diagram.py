#!/usr/bin/env python3
"""
Generate focused Tool Loop state machine diagram for WriterAgent.
Extracted from the full super-unified diagram to show only tool loop states and transitions.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict, Any
import json


class Component(str, Enum):
    """Component categories for color coding"""
    TOOL_LOOP = "tool_loop"


@dataclass
class ToolLoopState:
    """Represents a state in the tool loop state machine"""
    name: str
    description: str
    component: Component
    is_terminal: bool = False
    is_error: bool = False
    is_initial: bool = False


@dataclass
class ToolLoopTransition:
    """Represents a transition between states"""
    from_state: str
    to_state: str
    trigger: str
    description: str
    component: Component
    condition: Optional[str] = None


class ToolLoopStateMachine:
    """Complete tool loop state machine"""
    
    def __init__(self):
        self.states = self._define_states()
        self.transitions = self._define_transitions()
        self.state_map = {s.name: s for s in self.states}
    
    def _define_states(self) -> List[ToolLoopState]:
        return [
            ToolLoopState(
                name="ToolLoop_Idle",
                description="Waiting for tool calls",
                component=Component.TOOL_LOOP,
                is_initial=True
            ),
            ToolLoopState(
                name="ToolLoop_HasToolCalls",
                description="Tool calls received from agent",
                component=Component.TOOL_LOOP
            ),
            ToolLoopState(
                name="ToolLoop_ToolResult",
                description="Tool execution completed",
                component=Component.TOOL_LOOP
            ),
            ToolLoopState(
                name="ToolLoop_HasMoreTools",
                description="More tool calls available",
                component=Component.TOOL_LOOP
            ),
            ToolLoopState(
                name="ToolLoop_Done",
                description="Tool loop completed",
                component=Component.TOOL_LOOP,
                is_terminal=True
            ),
            ToolLoopState(
                name="ToolLoop_Error",
                description="Tool execution failed",
                component=Component.TOOL_LOOP,
                is_terminal=True,
                is_error=True
            ),
            ToolLoopState(
                name="ToolLoop_InvalidTool",
                description="Invalid tool call received",
                component=Component.TOOL_LOOP,
                is_terminal=True,
                is_error=True
            ),
            ToolLoopState(
                name="ToolLoop_Timeout",
                description="Tool execution timed out",
                component=Component.TOOL_LOOP,
                is_terminal=True,
                is_error=True
            ),
        ]
    
    def _define_transitions(self) -> List[ToolLoopTransition]:
        transitions = []
        
        # Tool Loop Transitions
        transitions.extend([
            ToolLoopTransition(
                from_state="ToolLoop_Idle",
                to_state="ToolLoop_HasToolCalls",
                trigger="Tool calls received",
                description="Agent returned tool calls",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_HasToolCalls",
                to_state="ToolLoop_ToolResult",
                trigger="Tool executed",
                description="Tool execution completed",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_ToolResult",
                to_state="ToolLoop_HasMoreTools",
                trigger="More tools available",
                description="Additional tool calls to process",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_HasMoreTools",
                to_state="ToolLoop_ToolResult",
                trigger="Tool executed",
                description="Execute next tool",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_ToolResult",
                to_state="ToolLoop_Done",
                trigger="No more tools",
                description="All tools processed",
                component=Component.TOOL_LOOP
            ),
            # Error transitions
            ToolLoopTransition(
                from_state="ToolLoop_HasToolCalls",
                to_state="ToolLoop_InvalidTool",
                trigger="Invalid tool call",
                description="Tool call has invalid parameters",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_HasToolCalls",
                to_state="ToolLoop_Error",
                trigger="Tool execution failed",
                description="Tool execution encountered error",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_HasToolCalls",
                to_state="ToolLoop_Timeout",
                trigger="Execution timeout",
                description="Tool execution timed out",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_ToolResult",
                to_state="ToolLoop_Error",
                trigger="Result processing failed",
                description="Failed to process tool result",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_HasMoreTools",
                to_state="ToolLoop_Error",
                trigger="Tool execution failed",
                description="Tool execution encountered error",
                component=Component.TOOL_LOOP
            ),
            ToolLoopTransition(
                from_state="ToolLoop_HasMoreTools",
                to_state="ToolLoop_Timeout",
                trigger="Execution timeout",
                description="Tool execution timed out",
                component=Component.TOOL_LOOP
            ),
        ])
        
        return transitions
    
    def to_mermaid(self) -> str:
        """Convert state machine to Mermaid.js diagram"""
        
        # Component colors
        component_colors = {
            Component.TOOL_LOOP: "#50c878"
        }
        
        # Build Mermaid diagram (without triple backticks for mmdc)
        lines = []
        lines.append("stateDiagram-v2")
        lines.append("    %% Tool Loop State Machine - High Resolution")
        lines.append("    %% Focused diagram showing only tool loop states and transitions")
        lines.append("    %% High resolution settings for crisp rendering")
        lines.append("    %%{init: {'theme': 'default', 'flowchart': {'curve': 'basis', 'padding': 50, 'rankSpacing': 100, 'nodeSpacing': 150}}}%%")
        
        # Define state styles
        for component, color in component_colors.items():
            lines.append(f"    classDef {component.value} fill:{color},color:white,stroke:#333")
        lines.append("    classDef terminal fill:#7b68ee,color:white,stroke:#333")
        lines.append("    classDef error fill:#ff4444,color:white,stroke:#333")
        
        # Add initial state
        lines.append("    [*] --> ToolLoop_Idle")
        
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
        
        # No legend needed for focused diagram
        
        return "\n".join(lines)
    
    def to_markdown(self) -> str:
        """Generate comprehensive markdown documentation"""
        lines = []
        lines.append("# Tool Loop State Machine")
        lines.append("")
        lines.append("Focused diagram showing the tool loop component from WriterAgent.")
        lines.append("")
        
        # Component legend
        lines.append("## Component Legend")
        lines.append("")
        lines.append("| Component | Color | Description |")
        lines.append("|-----------|-------|-------------|")
        lines.append("| Tool Loop | #50c878 | Tool calling workflow |")
        lines.append("")
        
        # States by component
        lines.append("## States")
        lines.append("")
        lines.append("| State | Description | Terminal | Error |")
        lines.append("|-------|-------------|----------|-------|")
        
        for state in self.states:
            terminal = "✓" if state.is_terminal else ""
            error = "✓" if state.is_error else ""
            lines.append(f"| `{state.name}` | {state.description} | {terminal} | {error} |")
        lines.append("")
        
        # Transitions summary
        lines.append("## Transition Summary")
        lines.append("")
        lines.append(f"Total states: {len(self.states)}")
        lines.append(f"Total transitions: {len(self.transitions)}")
        lines.append("")
        
        # Key interactions
        lines.append("## Key Interactions")
        lines.append("")
        lines.append("- **ToolLoop_Idle → ToolLoop_HasToolCalls**: Agent returns tool calls")
        lines.append("- **ToolLoop_HasToolCalls → ToolLoop_ToolResult**: Tool execution completes")
        lines.append("- **ToolLoop_ToolResult → ToolLoop_HasMoreTools**: More tools to process")
        lines.append("- **ToolLoop_HasMoreTools → ToolLoop_ToolResult**: Execute next tool")
        lines.append("- **ToolLoop_ToolResult → ToolLoop_Done**: All tools processed")
        lines.append("")
        
        # Error handling
        lines.append("## Error Handling")
        lines.append("")
        lines.append("- **ToolLoop_HasToolCalls → ToolLoop_InvalidTool**: Invalid tool parameters")
        lines.append("- **ToolLoop_HasToolCalls → ToolLoop_Error**: Execution failures")
        lines.append("- **ToolLoop_HasToolCalls → ToolLoop_Timeout**: Execution timeouts")
        lines.append("- **ToolLoop_ToolResult → ToolLoop_Error**: Result processing failures")
        lines.append("- **ToolLoop_HasMoreTools → ToolLoop_Error**: Execution failures during iteration")
        lines.append("- **ToolLoop_HasMoreTools → ToolLoop_Timeout**: Timeout during iteration")
        lines.append("")
        
        # Architecture notes
        lines.append("## Architecture Notes")
        lines.append("")
        lines.append("- **Component Isolation**: Tool loop has clear responsibilities")
        lines.append("- **Event-Driven**: Transitions triggered by specific events/conditions")
        lines.append("- **Iterative Processing**: Handles multiple tool calls in sequence")
        lines.append("- **Clean Termination**: Clear completion state when all tools processed")
        lines.append("")
        
        return "\n".join(lines)


def main():
    """Generate tool loop diagram"""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python generate_tool_loop_diagram.py <format>")
        print("Formats: mermaid, markdown")
        sys.exit(1)
    
    format_type = sys.argv[1].lower()
    machine = ToolLoopStateMachine()
    
    if format_type == "mermaid":
        print(machine.to_mermaid())
    elif format_type == "markdown":
        print(machine.to_markdown())
    else:
        print(f"Unknown format: {format_type}")
        sys.exit(1)


if __name__ == "__main__":
    main()
