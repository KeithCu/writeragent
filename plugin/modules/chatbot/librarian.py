# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software.

import logging
from typing import Any, cast

from plugin.framework.tool_base import ToolBase

log = logging.getLogger(__name__)


def _memory_key_from_tool_arguments(arguments: object) -> str | None:
    """Extract memory key from smolagents ToolCall.arguments (dict or JSON string)."""
    if isinstance(arguments, dict):
        k = cast(dict[str, Any], arguments).get("key")
        return k if isinstance(k, str) else None
    if isinstance(arguments, str):
        from plugin.framework.errors import safe_json_loads

        parsed = safe_json_loads(arguments)
        if isinstance(parsed, dict):
            k = cast(dict[str, Any], parsed).get("key")
            return k if isinstance(k, str) else None
    return None


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

    def execute(self, ctx, **kwargs):
        query = kwargs.get("query")
        history_text = kwargs.get("history_text")
        from plugin.framework.errors import format_error_payload, ToolExecutionError

        try:
            from plugin.framework.config import get_api_config
            from plugin.modules.http.client import LlmClient
            from plugin.framework.smol_model import WriterAgentSmolModel
            from plugin.contrib.smolagents.agents import ToolCallingAgent
            from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
            from plugin.contrib.smolagents.toolcalling_agent_prompts import LIBRARIAN_EXAMPLES_BLOCK
            from plugin.modules.chatbot.memory import MemoryTool
        except (ImportError, ValueError, TypeError) as e:
            return format_error_payload(ToolExecutionError(f"Failed to load dependencies: {e}"))

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        chat_append_callback = getattr(ctx, "chat_append_callback", None)
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
1. Learn the user's name.
2. Learn their favorite colors to use in future documents, people might like more than one.
3. Learn their writing style and comfort level of LibreOffice.
4. After learning about the user's name and favorite colors, explain that you are the introductory host agent of the WriterAgent 
  extension and ask them if they would like to learn about you / the WriterAgent extension. 
  This entire script only runs once, when it's their first time using this extension, 
  so ask them if they'd like to learn about WriterAgent after learning their name and favorite colors.
  If they say yes, randomly select one of the following tips to start with. Let the user respond.
  If the conversation continues, pick another one later to keep things fresh.
   - You are an expert in LibreOffice (and can use web research if needed), so they can ask you how to do things in LibreOffice, not just edit documents.
   - A great way to work is to select text and tell Writer Agent what to do.
   - If the cursor is in a sentence and the user says "fix this sentence", the agent guesses the sentence without needing a selection.
   - WriterAgent is sophisticated multi-threaded software, but this fork is only a few weeks old so expect issues. File issues at: https://github.com/KeithCu/writeragent/
   - WriterAgent is still a prototype, working towards a complete API for advanced Writer/Calc tools, image-editing, and more Draw/Impress features.
   - For technical users only: WriterAgent has an interesting architecture using a multi-threaded queue, pure state machines, and batch multi-threaded auto-translate into 8 languages.
5. NEVER write a document or output these details as a document. You must only share this information conversationally in the chat.
6. Make the experience enjoyable and personal. If they don't want to tell you information, don't push.
7. IMPORTANT: Call switch_to_document_mode(message='...') when the user wants to do document work (writing, editing, etc.) or when you both agree the onboarding is complete.

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
            from typing import cast, Iterable
            from plugin.contrib.smolagents.tools import Tool as SmolTool
            agent = ToolCallingAgent(
                tools=cast(list[SmolTool], [
                    SmolToolAdapter(MemoryTool(), ctx),
                    SmolToolAdapter(SwitchToDocumentModeTool(), ctx)
                ]),
                model=smol_model,
                max_steps=10,
                instructions=instructions,
                final_answer_tool_name="reply_to_user",
                system_prompt_examples=LIBRARIAN_EXAMPLES_BLOCK,
            )

            task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"

            final_ans = None
            switch_mode_requested = False

            run_stream = cast(Iterable, agent.run(task, stream=True))
            for step in run_stream:
                if stop_checker and stop_checker():
                    return format_error_payload(ToolExecutionError("Librarian stopped by user.", code="USER_STOPPED"))
                if isinstance(step, ToolCall):
                    if step.name == "upsert_memory":
                        mem_key = _memory_key_from_tool_arguments(step.arguments)
                        line = (
                            f"[Memory update: key '{mem_key}']\n"
                            if mem_key
                            else "[Memory update: upsert_memory]\n"
                        )
                        if callable(chat_append_callback):
                            chat_append_callback(line)
                        elif append_thinking_callback:
                            append_thinking_callback(
                                f"Running tool: {step.name} with {step.arguments}\n"
                            )
                    elif append_thinking_callback:
                        append_thinking_callback(
                            f"Running tool: {step.name} with {step.arguments}\n"
                        )
                    if status_callback:
                        status_callback(f"{step.name}...")
                elif isinstance(step, ActionStep):
                    if append_thinking_callback:
                        msg = f"Step {step.step_number}:\n"
                        if step.model_output:
                            mo = step.model_output
                            msg += f"{(mo.strip() if isinstance(mo, str) else str(mo).strip())}\n"
                        else:
                            mom = getattr(step, "model_output_message", None)
                            if mom is not None and getattr(mom, "content", None):
                                mc = mom.content
                                msg += f"{(mc.strip() if isinstance(mc, str) else str(mc).strip())}\n"

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
