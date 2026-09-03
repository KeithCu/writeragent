# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ask-box slash-command registry, prefix filter, and LRU ranking.

The sidebar popup is UI-first: most names are stubs so Keith can pick the
real set later. Ranking is prefix match, then ``slash_command_lru`` in
``writeragent.json`` (empty filter and as a tie-break). Do not implement
the consider-list in ``docs/chat/slash-commands.md`` here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from plugin.framework.i18n import _

log = logging.getLogger("writeragent.slash_commands")

# Persist via the same LRU helper as model/prompt history (not a sidecar file).
SLASH_LRU_KEY = "slash_command_lru"

# com.sun.star.awt.Key — integers so unit tests need no soffice.
KEY_DOWN = 1024
KEY_UP = 1025
KEY_RETURN = 1280
KEY_ESCAPE = 1281
KEY_TAB = 1282
KEY_MODIFIER_SHIFT = 1


@dataclass(frozen=True)
class SlashCommand:
    """One popup row. ``name`` is without the leading slash."""

    name: str
    description: str
    kind: str  # "wired" | "mock"


# Wired: /help /clear /stop. The rest exist so prefix + LRU tests do not
# depend on future product commands. Consider-list names are tagged mock.
SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", _("Print the command list"), "wired"),
    SlashCommand("clear", _("Start a new chat"), "wired"),
    SlashCommand("stop", _("Cancel the in-flight send"), "wired"),
    SlashCommand("mock-alpha", _("Mock command (echo only)"), "mock"),
    SlashCommand("mock-bravo", _("Mock command (echo only)"), "mock"),
    SlashCommand("mock-charlie", _("Mock command (echo only)"), "mock"),
    SlashCommand("apply", _("Apply last draft (mock)"), "mock"),
    SlashCommand("web", _("Web research (mock)"), "mock"),
    SlashCommand("model", _("Switch model (mock)"), "mock"),
)


def slash_typed_prefix(text: str) -> str | None:
    """Prefix after ``/`` for the first Ask-box token, or None if not a slash draft.

    ``/`` → ``""`` (full list). ``/he`` → ``"he"``. ``hello`` → None.
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return None
    first = stripped.split()[0]
    return first[1:].lower()


def filter_slash_commands(
    typed: str,
    lru: Sequence[str] | None = None,
    commands: Sequence[SlashCommand] | None = None,
) -> list[SlashCommand]:
    """Prefix-filter ``commands`` and rank by LRU then registry order.

    ``typed`` is raw Ask-box text (``/he``) or a bare prefix. Recently used
    names float to the top when the filter is empty and as a tie-break
    among equal prefix matches.
    """
    registry = tuple(commands) if commands is not None else SLASH_COMMANDS
    prefix = slash_typed_prefix(typed)
    if prefix is None:
        if (typed or "").startswith("/"):
            prefix = ""
        elif typed:
            # Allow unit tests to pass ``"he"`` without a leading slash.
            prefix = typed.lower().lstrip("/")
        else:
            return []
    matches = [cmd for cmd in registry if cmd.name.startswith(prefix)]
    lru_names = [str(n).lstrip("/").lower() for n in (lru or ()) if str(n).strip()]
    lru_index = {name: idx for idx, name in enumerate(lru_names)}
    registry_index = {cmd.name: idx for idx, cmd in enumerate(registry)}
    sentinel = len(lru_names) + 1

    def _key(cmd: SlashCommand) -> tuple[int, int]:
        return (lru_index.get(cmd.name, sentinel), registry_index.get(cmd.name, sentinel))

    return sorted(matches, key=_key)


def classify_slash_key(key_code: int, modifiers: int = 0) -> str | None:
    """Map a UNO key event to a popup action, or None if the Ask box should keep it."""
    if key_code == KEY_ESCAPE:
        return "escape"
    if key_code == KEY_UP:
        return "up"
    if key_code == KEY_DOWN:
        return "down"
    if key_code == KEY_TAB:
        return "tab"
    if key_code == KEY_RETURN and (modifiers & KEY_MODIFIER_SHIFT) == 0:
        return "enter"
    return None


def format_slash_item(cmd: SlashCommand) -> str:
    tag = " [mock]" if cmd.kind == "mock" else ""
    return "/%s%s — %s" % (cmd.name, tag, cmd.description)


def format_help_text(commands: Sequence[SlashCommand] | None = None) -> str:
    rows = commands if commands is not None else SLASH_COMMANDS
    lines = [_("Slash commands:")]
    for cmd in rows:
        lines.append("  " + format_slash_item(cmd))
    return "\n".join(lines)


def load_slash_lru() -> list[str]:
    from plugin.framework.config import get_config

    raw = get_config(SLASH_LRU_KEY)
    if not isinstance(raw, list):
        return []
    return [str(item).lstrip("/").lower() for item in raw if str(item).strip()]


def record_slash_lru(name: str) -> None:
    from plugin.chatbot.config_ui_helpers import update_lru_history

    clean = (name or "").lstrip("/").strip().lower()
    if not clean:
        return
    update_lru_history(clean, SLASH_LRU_KEY, "")


def _append_sidebar_line(host: Any, text: str) -> None:
    append = getattr(host, "_append_response", None)
    if callable(append):
        append(text, role="assistant")
        return
    response = getattr(host, "response_control", None)
    if response is None:
        return
    from plugin.chatbot.dialogs import get_control_text, set_control_text

    current = get_control_text(response) or ""
    set_control_text(response, current + text)


def _clear_ask_box(host: Any) -> None:
    from plugin.chatbot.dialogs import set_control_text
    from plugin.chatbot.send_state import SendEvent, SendEventKind

    query = getattr(host, "query_control", None)
    set_control_text(query, "")
    dispatch = getattr(host, "dispatch", None)
    if callable(dispatch):
        dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": False}))


def run_slash_command(name: str, host: Any) -> bool:
    """Run a registry command on the live send listener. Not a chat turn.

    Mocks echo ``slash: /name`` so the popup can be exercised. ``/help``
    prints the static list. ``/clear`` uses ClearButtonListener. ``/stop``
    is the same one-liner as the Stop button (``STOP_CLICKED``).
    """
    clean = (name or "").lstrip("/").strip().lower()
    cmd = next((item for item in SLASH_COMMANDS if item.name == clean), None)
    if cmd is None:
        return False
    record_slash_lru(cmd.name)
    popup = getattr(host, "slash_popup", None)
    hide = getattr(popup, "hide", None)
    if callable(hide):
        hide()
    _clear_ask_box(host)
    if cmd.name == "help":
        _append_sidebar_line(host, "\n" + format_help_text() + "\n")
        return True
    if cmd.name == "clear":
        clear_listener = getattr(host, "clear_listener", None)
        action = getattr(clear_listener, "on_action_performed", None)
        if callable(action):
            action(None)
        else:
            session = getattr(host, "session", None)
            if session is not None and hasattr(session, "clear"):
                session.clear()
        return True
    if cmd.name == "stop":
        from plugin.chatbot.send_state import SendEvent, SendEventKind

        dispatch = getattr(host, "dispatch", None)
        if callable(dispatch):
            dispatch(SendEvent(SendEventKind.STOP_CLICKED))
        return True
    _append_sidebar_line(host, "\nslash: /%s\n" % cmd.name)
    return True
