# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software.

import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger(__name__)

class SmolToolAdapter:
    """Adapts a WriterAgent ToolBase to smolagents.tools.Tool."""
    def __new__(cls, tool_instance, tctx):
        from plugin.contrib.smolagents.tools import Tool as SmolTool
        
        class Adapted(SmolTool):
            name = tool_instance.name
            description = tool_instance.description
            
            # ToolBase.parameters is JSON Schema
            # smolagents.Tool.inputs is {name: {"type": ..., "description": ...}}
            inputs = {}
            props = tool_instance.parameters.get("properties", {})
            required = tool_instance.parameters.get("required", [])
            for p_name, p_schema in props.items():
                inputs[p_name] = {
                    "type": p_schema.get("type", "string"),
                    "description": p_schema.get("description", ""),
                    "nullable": p_name not in required
                }
            output_type = "any"
            skip_forward_signature_validation = True
            
            def __init__(self, inner_tool, inner_tctx):
                super().__init__()
                self._inner_tool = inner_tool
                self._inner_tctx = inner_tctx

            def forward(self, **kwargs):
                return self._inner_tool.execute(self._inner_tctx, **kwargs)

        return Adapted(tool_instance, tctx)

class SwitchToDocumentModeTool(ToolBase):
    name = "switch_to_document_mode"
    description = "Exits the Librarian onboarding flow and switches the user to the main document assistant mode. Use this when you are done or the user wants to work on the document."
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "A final friendly message to the user before switching."
            }
        },
        "required": ["message"]
    }
    # Hide from the default main-chat tool surface; librarian onboarding owns this tool.
    tier = "specialized_control"
    is_mutation = False
    long_running = False

    def is_async(self):
        return False

    def execute(self, ctx, **kwargs):
        from plugin.framework.i18n import _

        # If the tool is called, we will stop the Librarian flow.
        # It's an internal signal, we'll return a specific status.
        return {
            "status": "switch_mode",
            "message": kwargs.get("message", _("Switching to document mode..."))
        }

class LibrarianOnboardingTool(ToolBase):
    name = "librarian_onboarding"
    description = "Librarian agent for new user onboarding."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "User message"
            },
            "history_text": {
                "type": "string",
                "description": "Previous conversation text"
            }
        },
        "required": ["query"]
    }
    # Hide from the default main-chat tool surface; librarian onboarding owns this tool.
    tier = "specialized_control"
    is_mutation = False
    long_running = True

    def is_async(self):
        return True

    def execute(self, ctx, query, history_text=None):
        from plugin.framework.errors import format_error_payload, ToolExecutionError

        try:
            from plugin.framework.config import get_api_config
            from plugin.modules.http.client import LlmClient
            from plugin.framework.smol_model import WriterAgentSmolModel
            from plugin.contrib.smolagents.agents import ToolCallingAgent
            from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
            from plugin.modules.chatbot.memory import MemoryTool
        except (ImportError, ValueError, TypeError) as e:
            return format_error_payload(ToolExecutionError(f"Failed to load dependencies: {e}"))

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        stop_checker = getattr(ctx, "stop_checker", None)

        if history_text:
            if len(history_text) > 4000:
                history_text = "..." + history_text[-4000:]

        try:
            if status_callback:
                status_callback("Librarian is thinking...")

            config = get_api_config(ctx.ctx)
            max_tokens = int(config.get("chat_max_tokens", 2048))

            smol_model = WriterAgentSmolModel(
                LlmClient(config, ctx.ctx), max_tokens=max_tokens,
                status_callback=status_callback,
            )

            instructions = """
LIBRARIAN PERSONALITY:
You are the WriterAgent Librarian - a friendly, curious, and incredibly helpful assistant who wants to get to know users and help them succeed. Think of this like a first date with your AI colleague. You are happy to talk as long as the user wants!

YOUR GOALS:
1. Learn the user's preferred name and what to call them
2. Discover their favorite color and preferences
3. Understand how they work and what they need
4. Make the experience enjoyable and personal
5. IMPORTANT: Call switch_to_document_mode(message='...') when the user wants to do document work (writing, editing, etc.) or when you both agree the onboarding is complete.

CONVERSATION STYLE:
- Be warm, extremely friendly, and genuinely curious.
- Ask questions naturally, not like an interview.
- Listen carefully to answers and extract meaning.
- Use the memory tool to save preferences.
- Be patient and helpful. You are willing to chat as long as the user wants!
- Make it fun! Use appropriate emojis and enthusiasm.

TOOLS FOR COMPLETION:
- Use 'reply_to_user' to respond to the user and CONTINUE the onboarding conversation (e.g., asking more questions).
- Use 'switch_to_document_mode' with a friendly 'message' to END the onboarding and hand over to the document assistant.
- NEVER explain that you lack document tools. Instead, just say "I'll switch you to document mode for that!" and call the switch tool.
"""
            agent = ToolCallingAgent(
                tools=[
                    SmolToolAdapter(MemoryTool(), ctx),
                    SmolToolAdapter(SwitchToDocumentModeTool(), ctx)
                ],
                model=smol_model,
                max_steps=10,
                instructions=instructions,
                final_answer_tool_name="reply_to_user",
            )

            task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"

            final_ans = None
            switch_mode_requested = False

            for step in agent.run(task, stream=True):
                if stop_checker and stop_checker():
                    return format_error_payload(ToolExecutionError("Librarian stopped by user.", code="USER_STOPPED"))
                if isinstance(step, ToolCall):
                    if append_thinking_callback:
                        append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
                    if status_callback:
                        status_callback(f"{step.name}...")
                elif isinstance(step, ActionStep):
                    if append_thinking_callback:
                        msg = f"Step {step.step_number}:\n"
                        if step.model_output:
                            msg += f"{step.model_output.strip()}\n"
                        elif getattr(step, "model_output_message", None) and step.model_output_message.content:
                            msg += f"{str(step.model_output_message.content).strip()}\n"

                        if step.observations:
                            msg += f"Observation: {str(step.observations).strip()}\n"
                            # If the observation is our switch mode status, break early
                            obs_str = str(step.observations)
                            if "'status': 'switch_mode'" in obs_str:
                                switch_mode_requested = True
                                # Try to extract the message from the observation
                                import re
                                match = re.search(r"'message': '([^']*)'", obs_str)
                                if match:
                                    final_ans = match.group(1)

                        append_thinking_callback(msg + "\n")
                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            if switch_mode_requested:
                return {
                    "status": "switch_mode",
                    "result": str(final_ans) if final_ans else "Switching to document mode."
                }

            return {
                "status": "ok",
                "result": str(final_ans),
            }
        except Exception as e:
            log.error("Librarian error: %s", e)
            err = ToolExecutionError(f"Librarian failed: {str(e)}", details={"query": query})
            return format_error_payload(err)
