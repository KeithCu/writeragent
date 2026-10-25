# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software.

from __future__ import annotations

import getpass
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.errors import best_effort
from plugin.chatbot.sub_agent_tools import SubAgentSessionToolBase
from plugin.chatbot.memory import format_upsert_memory_chat_line

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

MAX_SUGGESTED_NAME_LEN = 60
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


def _normalize_suggested_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    # Strip control characters
    cleaned = "".join(ch for ch in str(raw) if ch.isprintable() or ch.isspace())
    # Collapse whitespace
    name = " ".join(cleaned.split()).strip()
    if not name:
        return None
    if len(name) > MAX_SUGGESTED_NAME_LEN:
        name = name[:MAX_SUGGESTED_NAME_LEN].strip()
    if not name or name.lower() in _GENERIC_NAMES:
        return None
    return name


def get_libreoffice_user_display_name(ctx: Any) -> str | None:
    """Return given name from LibreOffice User Data (Tools → Options → User Data)."""
    uno_ctx = getattr(ctx, "ctx", ctx)
    if uno_ctx is None:
        return None

    def _read() -> str | None:
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
        return _normalize_suggested_name(str(access.getPropertyValue("sn")))

    return best_effort(_read, "Failed to read LibreOffice user profile name")


def get_os_login_name() -> str | None:
    """Return the OS login account name (cross-platform via getpass)."""
    return best_effort(lambda: _normalize_suggested_name(getpass.getuser()), "Failed to read OS login name")


def get_suggested_user_name(ctx: Any) -> str | None:
    """Best-effort name for librarian confirmation: LO profile first, then OS login."""
    return get_libreoffice_user_display_name(ctx) or get_os_login_name()


def _run_librarian_agent(
    ctx: ToolContext,
    *,
    query: str = "",
    history_text: str | None = None,
    suggested_user_name: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one turn of the Librarian onboarding smol sub-agent."""
    try:
        from plugin.chatbot.smol_agent import run_smol_side_turn
        from plugin.chatbot.memory import MemoryTool
    except (ImportError, ValueError, TypeError) as e:
        from plugin.framework.errors import ToolExecutionError, format_error_payload
        return format_error_payload(ToolExecutionError(f"Failed to load dependencies: {e}"))

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    chat_append_callback = getattr(ctx, "chat_append_callback", None)

    if suggested_user_name:
        hint_block = f"[SUGGESTED USER NAME]\n{suggested_user_name}\n[/SUGGESTED USER NAME]"
        priority_1 = f"""Priority 1. Confirm what to call the user and save it to memory (key "name") for later.
  {hint_block}
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
Tip 3: WriterAgent has been auto-translated by a variety of different AI models. If the user find a bug in the translations, or the code, file an issue or create a pull request at https://github.com/KeithCu/writeragent/
Tip 4: To enable advanced data analysis, scripting, and audio recording to talk with the WriterAgent AI, the user must set up a Python virtual environment (venv) and configure the path in Settings → Python. The 'sounddevice' package must be installed for audio recording.
Tip 5: A great way to work is to select text and tell Writer Agent what to do. If they say "fix this" (or a synonym in their own language), WriterAgent corrects spelling and grammar in the current sentence only, unless the context makes it clear there is another specific error to fix. The cursor or selection implies which sentence.
Tip 6: WriterAgent is sophisticated multi-threaded software, but this codebase is only a few months old so expect issues. 
            WriterAgent is working towards a complete API for advanced Writer/Calc/Draw/Impress tools, image-editing, Python scripting, and more. File issues at: https://github.com/KeithCu/writeragent/

NEVER write a document or output these details as a document.
You must only share this information conversationally in the chat one at a time, as they may want to discuss each topic separately.
NEVER mention a tip twice.
Make the experience enjoyable and personal.
IMPORTANT: Call reply_to_user with answer and switch_to_document_mode=true when the conversation seems over, or when the user says goodbye or says they want to do document work (writing, editing, spreadsheets, etc.) or when you both agree the onboarding is complete.

CONVERSATION STYLE:
- Be warm, friendly, and genuinely curious to learn about the user.
- Ask questions naturally.
- When you ask about favorite colors, always state in that message that WriterAgent can use those colors for headings and other places.
- Listen carefully to answers and extract meaning.
- Use the memory tool to save any preferences that could be useful later besides the name and favorite color.
- Be patient and helpful. You are willing to chat as long as the user wants, until they are ready to switch to document mode.
- Make it fun! Use appropriate emojis and enthusiasm.

TOOLS FOR COMPLETION:
- Use reply_to_user with 'answer' to CONTINUE the onboarding conversation.
- Use reply_to_user with 'answer' and switch_to_document_mode=true to END onboarding and switch the sidebar to Chat.

"""

    from plugin.framework.prompts import (
        format_user_memory_prompt_block,
        get_chat_response_format_instructions,
    )

    instructions += (
        "\n\n"
        + get_chat_response_format_instructions()
        + "\nFormat reply_to_user answer with this style; that text is shown in the chat sidebar."
    )
    mem_block = format_user_memory_prompt_block()
    if mem_block:
        # Cap USER.md with the same CHAT_DOCUMENT_CONTEXT_MAX_CHARS limit
        # as the main chat prompt. A raw profile crowds out the librarian
        # instructions.
        instructions += mem_block

    from plugin.chatbot.sticky_reply import LIBRARIAN_REPLY_SPEC

    def tool_call_handler(step: Any) -> None:
        name = getattr(step, "name", "")
        arguments = getattr(step, "arguments", {})
        if name == "upsert_memory":
            line = format_upsert_memory_chat_line(arguments)
            if callable(chat_append_callback):
                chat_append_callback(line)
            elif append_thinking_callback:
                append_thinking_callback(f"Running tool: {name} with {arguments}\n")
        elif append_thinking_callback:
            append_thinking_callback(f"Running tool: {name} with {arguments}\n")
        if status_callback:
            status_callback(f"{name}...")

    return run_smol_side_turn(
        ctx,
        query=query,
        history_text=history_text,
        collector=lambda c: [MemoryTool()],
        instructions=instructions,
        examples_key="librarian",
        reply_spec=LIBRARIAN_REPLY_SPEC,
        status_message="Librarian is thinking...",
        stop_message="Librarian stopped by user.",
        error_prefix="Librarian failed",
        collector_safe=False,
        inputs_style="librarian",
        tool_call_handler=tool_call_handler,
    )


class LibrarianOnboardingTool(SubAgentSessionToolBase):
    # Hide from the default main-chat tool surface; librarian onboarding owns this tool.
    name: str | None = "librarian_onboarding"
    description: str = "Librarian agent for new user onboarding."
    agent_label: str = "Librarian"
    runner: Callable[..., dict[str, Any]] | None = staticmethod(_run_librarian_agent)
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message"},
            "history_text": {"type": "string", "description": "Previous conversation text"},
            "suggested_user_name": {"type": "string", "description": "OS/LO suggested name for confirmation (internal)."},
        },
        "required": ["query"],
    }
