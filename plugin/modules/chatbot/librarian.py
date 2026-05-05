# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software.

import logging
import re
import traceback
from typing import Iterable, cast

from plugin.framework.tool_base import ToolBase
from plugin.modules.chatbot.memory import format_upsert_memory_chat_line_from_arguments

log = logging.getLogger(__name__)


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
            from plugin.framework.smol_agent_factory import build_toolcalling_agent
            from plugin.framework.smol_tool_adapter import SmolToolAdapter
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

            instructions = """
LIBRARIAN PERSONALITY:
You are the WriterAgent Librarian - a friendly, curious, and incredibly helpful assistant who wants to get to know users and help them succeed. Think of this like a first date with your AI colleague. You are happy to talk as long as the user wants!

YOUR GOALS:
1. Learn the user's name.
2. Learn their favorite colors to use in future documents, people might like more than one.
3. After learning about the user's name and favorite colors, explain that you are the introductory host agent of the WriterAgent
  extension and ask them if they would like to learn about the features of the WriterAgent extension.
  This is the first time using the extension, so a great time to explain the extension and ask them if they have any questions.

  If they say yes, ALWAYS introduce the tip about document review via comments first.
  Ask if they want more tips, and if so, choose another item from the rest of the list.

   - If the user asks WriterAgent to "review" or "give feedback or suggestions" on a document, WriterAgent will review it all and add comments in the margins near the text. Encourage them to try it.   
   - A great way to work is to select text and tell Writer Agent what to do.
   - If the cursor is in a sentence and the user says "fix (or re-write or improve) this sentence", the agent will figure out what to do and fix the sentence without needing to select it all.
   - WriterAgent is sophisticated multi-threaded software, but this fork is only a few months old so expect issues. File issues at: https://github.com/KeithCu/writeragent/
   - WriterAgent is still a prototype, working towards a complete API for advanced Writer/Calc tools, image-editing, and more Draw/Impress features.
   - For technical users only: WriterAgent has an interesting architecture using a multi-threaded queue, pure state machines, and batch multi-threaded auto-translate into 8 languages.
5. NEVER write a document or output these details as a document. You must only share this information conversationally in the chat.
6. Make the experience enjoyable and personal. If they don't want to tell you information, don't push.
7. IMPORTANT: Call switch_to_document_mode(message='...') when the conversation seems over, or when the user says goodbye or says they want to do document work (writing, editing, spreadsheets, etc.) or when you both agree the onboarding is complete.

CONVERSATION STYLE:
- Be warm, friendly, and genuinely curious to learn about the user.
- Ask questions naturally.
- Listen carefully to answers and extract meaning.
- Use the memory tool to save any preferences that could be useful later besides the name and favorite color.
- Be patient and helpful. You are willing to chat as long as the user wants, until they are ready to switch to document mode.
- Make it fun! Use appropriate emojis and enthusiasm.

TOOLS FOR COMPLETION:
- Use 'reply_to_user' to respond to the user and CONTINUE the onboarding conversation (e.g., asking more questions).
- Use 'switch_to_document_mode' with a friendly 'message' to END the onboarding and hand over to the document assistant.

"""
            agent = build_toolcalling_agent(
                ctx,
                [
                    SmolToolAdapter(MemoryTool(), ctx, safe=False, inputs_style="librarian"),
                    SmolToolAdapter(SwitchToDocumentModeTool(), ctx, safe=False, inputs_style="librarian"),
                ],
                instructions=instructions,
                final_answer_tool_name="reply_to_user",
                examples_block=LIBRARIAN_EXAMPLES_BLOCK,
                status_callback=status_callback,
            )

            task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"

            final_ans = None

            run_stream = cast("Iterable", agent.run(task, stream=True))
            for step in run_stream:
                if stop_checker and stop_checker():
                    return format_error_payload(ToolExecutionError("Librarian stopped by user.", code="USER_STOPPED"))
                if isinstance(step, ToolCall):
                    if step.name == "upsert_memory":
                        line = format_upsert_memory_chat_line_from_arguments(step.arguments)
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
                            obs_str = str(step.observations)
                            if "'status': 'switch_mode'" in obs_str:
                                match = re.search(r"'message': '([^']*)'", obs_str)
                                handoff = match.group(1) if match else None
                                append_thinking_callback(msg + "\n")
                                return {
                                    "status": "switch_mode",
                                    "result": str(handoff) if handoff else "Switching to document mode.",
                                }

                        append_thinking_callback(msg + "\n")
                    elif step.observations:
                        obs_str = str(step.observations)
                        if "'status': 'switch_mode'" in obs_str:
                            match = re.search(r"'message': '([^']*)'", obs_str)
                            handoff = match.group(1) if match else None
                            return {
                                "status": "switch_mode",
                                "result": str(handoff) if handoff else "Switching to document mode.",
                            }
                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            return {
                "status": "ok",
                "result": str(final_ans),
            }
        except Exception as e:
            tb = traceback.format_exc()
            log.error("Librarian error: %s", e)
            err = ToolExecutionError(f"Librarian failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)
