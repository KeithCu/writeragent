# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software.

import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger(__name__)

class SwitchToDocumentModeTool(ToolBase):
    name = "switch_to_document_mode"
    description = "Exits the Librarian onboarding flow and switches the user to the main document assistant mode."
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    tier = "agent"
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
            "message": _("Switching to document mode...")
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
    tier = "agent"
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
You are the WriterAgent Librarian - a friendly, curious assistant who wants to get to know users and help them succeed. Think of this like a first date with your AI colleague.

YOUR GOALS:
1. Learn the user's preferred name and what to call them
2. Discover their favorite color and preferences
3. Understand how they work and what they need
4. Make the experience enjoyable and personal
5. IMPORTANT: Call switch_to_document_mode when you feel you've gotten to know the user or when they want to do document work.

CONVERSATION STYLE:
- Be warm, friendly, and genuinely curious
- Ask questions naturally, not like an interview
- Listen carefully to answers and extract meaning
- Use the memory tool to save preferences
- Be patient and helpful
- Make it fun! Use appropriate emojis and enthusiasm

CONVERSATION FLOW:
Start friendly and natural. Don't rush through questions. Let the conversation develop organically. Mix learning with teaching.
Once the user's preferences are saved, ALWAYS call switch_to_document_mode.
"""
            agent = ToolCallingAgent(
                tools=[
                    MemoryTool(),
                    SwitchToDocumentModeTool()
                ],
                model=smol_model,
                max_steps=10,
                instructions=instructions,
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
                            if "'status': 'switch_mode'" in str(step.observations):
                                switch_mode_requested = True

                        append_thinking_callback(msg + "\n")
                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            if switch_mode_requested:
                return {
                    "status": "switch_mode",
                    "result": "Switching to document mode."
                }

            return {
                "status": "ok",
                "result": str(final_ans),
            }
        except Exception as e:
            log.error("Librarian error: %s", e)
            err = ToolExecutionError(f"Librarian failed: {str(e)}", details={"query": query})
            return format_error_payload(err)
