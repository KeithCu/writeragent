# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software.

import getpass
import logging
import re
import traceback
from typing import Any, Iterable, cast

from plugin.framework.tool import ToolBase
from plugin.chatbot.memory import format_upsert_memory_chat_line_from_arguments

log = logging.getLogger(__name__)

_USER_PROFILE_NODE = "/org.openoffice.UserProfile/Data"

# Login / profile placeholders that are not useful as a greeting name.
_GENERIC_NAMES = frozenset(
    {
        "administrator",
        "guest",
        "nobody",
        "root",
        "unknown",
        "user",
    }
)


def _resolve_uno_ctx(ctx: Any) -> Any:
    return getattr(ctx, "ctx", ctx)


def _normalize_suggested_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    name = str(raw).strip()
    if not name:
        return None
    if name.lower() in _GENERIC_NAMES:
        return None
    return name


def get_libreoffice_user_display_name(ctx: Any) -> str | None:
    """Return given name from LibreOffice User Data (Tools → Options → User Data)."""
    uno_ctx = _resolve_uno_ctx(ctx)
    if uno_ctx is None:
        return None
    try:
        from com.sun.star.beans import NamedValue

        smgr = uno_ctx.getServiceManager()
        provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", uno_ctx)
        node = NamedValue()
        node.Name = "nodepath"
        node.Value = _USER_PROFILE_NODE
        access = provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (node,))
        given = _normalize_suggested_name(str(access.getPropertyValue("givenname")))
        if given:
            return given
        sn = _normalize_suggested_name(str(access.getPropertyValue("sn")))
        if sn:
            return sn
    except Exception:
        log.debug("Failed to read LibreOffice user profile name", exc_info=True)
    return None


def get_os_login_name() -> str | None:
    """Return the OS login account name (cross-platform via getpass)."""
    try:
        return _normalize_suggested_name(getpass.getuser())
    except Exception:
        log.debug("Failed to read OS login name", exc_info=True)
        return None


def get_suggested_user_name(ctx: Any) -> str | None:
    """Best-effort name for librarian confirmation: LO profile first, then OS login."""
    return get_libreoffice_user_display_name(ctx) or get_os_login_name()


class SwitchToDocumentModeTool(ToolBase):
    name = "switch_to_document_mode"
    description = "Exits the Librarian onboarding flow and switches the user to the main document assistant mode. Use this when you are done or the user wants to work on the document."
    parameters = {"type": "object", "properties": {"message": {"type": "string", "description": "A final friendly message to the user before switching."}}, "required": ["message"]}
    # Hide from the default main-chat tool surface; librarian onboarding owns this tool.
    tier = "specialized_control"
    is_final_answer_tool = True
    is_mutation = False
    long_running = False

    def is_async(self):
        return False

    def execute(self, ctx, **kwargs):
        from plugin.framework.i18n import _

        # If the tool is called, we will stop the Librarian flow.
        # It's an internal signal, we'll return a specific status.
        return {"status": "switch_mode", "message": kwargs.get("message", _("Switching to document mode..."))}


class LibrarianOnboardingTool(ToolBase):
    name = "librarian_onboarding"
    description = "Librarian agent for new user onboarding."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message"},
            "history_text": {"type": "string", "description": "Previous conversation text"},
            "suggested_user_name": {"type": "string", "description": "OS/LO suggested name for confirmation (internal)."},
        },
        "required": ["query"],
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
        suggested_user_name = kwargs.get("suggested_user_name")
        if isinstance(suggested_user_name, str):
            suggested_user_name = suggested_user_name.strip() or None
        else:
            suggested_user_name = None
        from plugin.framework.errors import format_error_payload, ToolExecutionError

        try:
            from plugin.chatbot.smol_agent import build_toolcalling_agent, SmolToolAdapter
            from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
            from plugin.chatbot.smol_examples import get_examples_block
            from plugin.chatbot.memory import MemoryTool, MemoryStore
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
            user_mem = ""
            try:
                store = MemoryStore(ctx)
                user_mem = store.read("user")
            except Exception as e:
                log.debug("Failed to read user memory for librarian: %s", e)

            if status_callback:
                status_callback("Librarian is thinking...")

            if suggested_user_name:
                priority_1 = f"""Priority 1. Confirm what to call the user and save it to memory (key "name") for later.
  Ask whether they would like to be called {suggested_user_name} (phrase naturally in the user's language).
  Only call upsert_memory for "name" after they clearly confirm (yes, sure, that works).
  If they prefer a different name, save what they say. If they decline to share a name, do not pressure them and skip saving it.
  Also save everything else that could be useful later for future document work."""
            else:
                priority_1 = """Priority 1. Learn what to call the user and save it to memory (key "name") for later.
  Ask what they would like to be called, then save after they answer.
  Also save everything else that could be useful later for future document work."""

            instructions = f"""
LIBRARIAN PERSONALITY:
You are the WriterAgent Librarian - a friendly, curious, and helpful assistant who wants to get to know users and help them succeed.
Think of this like a first date with your new AI colleague. You are happy to talk as long as the user wants or switch to work mode when they are ready.

YOUR GOALS:
{priority_1}
Priority 2. Learn their favorite colors (and accent colors) so WriterAgent can use them later for document formatting.
When you ask, explain why you are asking so the user feels comfortable sharing.
Explain that it helps WriterAgent be better in the future when formatting documents since everyone eventually gets bored of only black and white.
Priority 3. After learning about the user's name and favorite colors and accent colors, explain that you are the introductory host agent of the WriterAgent
  extension and ask them if they would like to learn about the features of the extension.
  This agent runs the FIRST time using the extension, so a great time to explain it and ask them if they have any questions.
Ask if they would like to learn about WriterAgent. If so, go through the list. Explain each one at a time. 
    and then ask if they have any questions about it or would like to learn another topic.
Either: a. answer the question about that topic or LibreOffice or the extension generally, or 
        b. explain the next topc in the list if they want to hear another tip, or 
        c. switch to document mode so they can do work if they don't have any questions and don't want to chat more or learn the next tip, 
        d. If they tell you something about themselves that could be useful for future document work, save that in memory for later. 

Tip 1: If the user asks WriterAgent to "review" or "give feedback" or "suggestions" (using their own language) on a document, WriterAgent will review it all and add comments in the margins near the text. Encourage them to try it.
Tip 2: For work on their personal or business documents, tell them to say "my / our" (using their own language) so WriterAgent does document research on local files, not web research on public topics.
Tip 3: WriterAgent has been auto-translated in 34 language by a variety of different AI models. If the user find a bug in the translations, or the code, file an issue or create a pull request at https://github.com/KeithCu/writeragent/
Tip 4: To enable advanced data analysis, scripting, and audio recording to talk with the WriterAgent AI, the user must set up a Python virtual environment (venv) and configure the path in Settings → Python. The 'sounddevice' package must be installed for audio recording.
Tip 5: A great way to work is to select text and tell Writer Agent what to do. If they say "fix this" (or a synonym in their own language), WriterAgent corrects spelling and grammar in the current sentence only, unless the context makes it clear there is another specific error to fix. The cursor or selection implies which sentence.
Tip 6: WriterAgent is sophisticated multi-threaded software, but this codebase is only a few months old so expect issues. 
            WriterAgent is working towards a complete API for advanced Writer/Calc/Draw/Impress tools, image-editing, Python scripting, and more. File issues at: https://github.com/KeithCu/writeragent/

NEVER write a document or output these details as a document.
You must only share this information conversationally in the chat one at a time, as they may want to discuss each topic separately.
NEVER mention a tip twice.
Make the experience enjoyable and personal.
IMPORTANT: Call switch_to_document_mode(message='...') when the conversation seems over, or when the user says goodbye or says they want to do document work (writing, editing, spreadsheets, etc.) or when you both agree the onboarding is complete.

CONVERSATION STYLE:
- Be warm, friendly, and genuinely curious to learn about the user.
- Ask questions naturally.
- When you ask about favorite colors, always state in that message that WriterAgent can use those colors for headings and other places.
- Listen carefully to answers and extract meaning.
- Use the memory tool to save any preferences that could be useful later besides the name and favorite color.
- Be patient and helpful. You are willing to chat as long as the user wants, until they are ready to switch to document mode.
- Make it fun! Use appropriate emojis and enthusiasm.

TOOLS FOR COMPLETION:
- Use 'reply_to_user' to respond to the user and CONTINUE the onboarding conversation (e.g., asking more questions).
- Use 'switch_to_document_mode' with a friendly 'message' to END the onboarding and hand over to the document assistant.

"""

#Unused for now
# Tip 7: In Writer, the sidebar mode dropdown includes Brainstorming. Choose Brainstorming to start a multi-turn design session: the agent asks one question at a time, can read the open document, search nearby files, and do web research, then discusses approaches with you. 

            from plugin.framework.constants import get_chat_response_format_instructions

            instructions += (
                "\n\n"
                + get_chat_response_format_instructions(ctx.ctx)
                + "\nFormat reply_to_user and switch_to_document_mode message with this style; that text is shown in the chat sidebar."
            )
            if user_mem and user_mem.strip():
                instructions += "\n\n[USER PROFILE / MEMORY]\n" + user_mem.strip() + "\n"

            agent = build_toolcalling_agent(
                ctx,
                [SmolToolAdapter(MemoryTool(), ctx, safe=False, inputs_style="librarian"), SmolToolAdapter(SwitchToDocumentModeTool(), ctx, safe=False, inputs_style="librarian")],
                instructions=instructions,
                final_answer_tool_name="reply_to_user",
                examples_block=get_examples_block("librarian"),
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
                            append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
                    elif append_thinking_callback:
                        append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
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
                                return {"status": "switch_mode", "result": str(handoff) if handoff else "Switching to document mode."}

                        append_thinking_callback(msg + "\n")
                    elif step.observations:
                        obs_str = str(step.observations)
                        if "'status': 'switch_mode'" in obs_str:
                            match = re.search(r"'message': '([^']*)'", obs_str)
                            handoff = match.group(1) if match else None
                            return {"status": "switch_mode", "result": str(handoff) if handoff else "Switching to document mode."}
                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            return {"status": "ok", "result": str(final_ans)}
        except Exception as e:
            tb = traceback.format_exc()
            log.error("Librarian error: %s", e)
            err = ToolExecutionError(f"Librarian failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)
