# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ask-box slash-command registry, prefix filter, and LRU ranking.

The product list is /help /clear /stop. Echo-only ``[mock]`` stubs exist for
the mock-LLM sidebar harness and register only through
``enable_mock_slash_commands()``. Ranking is prefix match, then ``slash_command_lru`` in
``writeragent.json`` (empty filter and as a tie-break). Do not implement
the consider-list in ``docs/chat/slash-commands.md`` here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from plugin.framework.i18n import N_, _

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


# Product commands: /help /clear /stop.
SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", N_("Print the command list")),
    SlashCommand("clear", N_("Start a new chat")),
    SlashCommand("stop", N_("Cancel the in-flight send")),
)

# Echo-only stubs for the mock-LLM sidebar harness and prefix/LRU tests.
# Never in the product list: only ``enable_mock_slash_commands()`` (called by
# the debug-only sidebar test hooks) adds them to ``registered_slash_commands``.
MOCK_SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("mock-alpha", N_("Mock command (echo only)")),
    SlashCommand("mock-bravo", N_("Mock command (echo only)")),
    SlashCommand("mock-charlie", N_("Mock command (echo only)")),
    SlashCommand("apply", N_("Apply last draft (mock)")),
    SlashCommand("web", N_("Web research (mock)")),
    SlashCommand("model", N_("Switch model (mock)")),
)

_mock_commands_enabled = False
_MOCK_SLASH_NAMES = frozenset(c.name for c in MOCK_SLASH_COMMANDS)


def enable_mock_slash_commands(enabled: bool = True) -> None:
    """Mock-LLM harness only: show the ``[mock]`` stubs in the popup and /help."""
    global _mock_commands_enabled
    _mock_commands_enabled = bool(enabled)


def registered_slash_commands() -> tuple[SlashCommand, ...]:
    """Commands the popup, /help and ``run_slash_command`` see right now."""
    if _mock_commands_enabled:
        return SLASH_COMMANDS + MOCK_SLASH_COMMANDS
    return SLASH_COMMANDS


def slash_typed_prefix(text: str) -> str | None:
    """Prefix after ``/`` for the first Ask-box token, or None if not a slash draft.

    ``/`` → ``""`` (full list). ``/he`` → ``"he"``. ``hello`` → None.
    """
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    first = stripped.split()[0]
    return first[1:].lower()


def filter_slash_commands(
    typed: str,
    lru: Sequence[str] | None = None,
) -> list[SlashCommand]:
    """Prefix-filter ``commands`` and rank by LRU then registry order.

    ``typed`` is raw Ask-box text (``/he``). Recently used names float to
    the top when the filter is empty and as a tie-break among equal
    prefix matches.
    """
    registry = registered_slash_commands()
    prefix = slash_typed_prefix(typed)
    if prefix is None:
        return []
    matches = [cmd for cmd in registry if cmd.name.startswith(prefix)]
    seq = tuple(lru or ())
    lru_index = {n: i for i, n in enumerate(seq)}
    return sorted(matches, key=lambda cmd: lru_index.get(cmd.name, len(seq)))


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
    tag = " [mock]" if cmd.name in _MOCK_SLASH_NAMES else ""
    return "/%s%s — %s" % (cmd.name, tag, _(cmd.description))


def format_help_text() -> str:
    lines = [_("Slash commands:")]
    for cmd in registered_slash_commands():
        lines.append("  " + format_slash_item(cmd))
    return "\n".join(lines)


def load_slash_lru() -> list[str]:
    from plugin.framework.config import get_config

    raw = get_config(SLASH_LRU_KEY)
    if not isinstance(raw, list):
        return []
    return [str(item).lstrip("/").lower() for item in raw if str(item).strip()]


def record_slash_lru(name: str) -> None:
    from plugin.framework.errors import best_effort
    from plugin.framework.model_lru import update_lru_history

    clean = (name or "").lstrip("/").strip().lower()
    if not clean:
        return
    best_effort(
        lambda: update_lru_history(clean, SLASH_LRU_KEY, ""),
        f"record_slash_lru failed for {clean}",
    )


def run_slash_command(name: str, host: Any) -> bool:
    """Run a registry command on the live send listener. Not a chat turn.

    Mocks echo ``slash: /name`` so the popup can be exercised. ``/help``
    prints the static list. ``/clear`` uses ClearButtonListener. Those and
    mocks clear Ask. ``/stop`` does not: it runs ``StopButtonListener`` (TTS,
    inline web approval, hands-free, and ``STOP_CLICKED``), same as the Stop
    button, and leaves the draft.
    """
    clean = name.lstrip("/").strip().lower()
    cmd = {c.name: c for c in registered_slash_commands()}.get(clean)
    if cmd is None:
        return False
    record_slash_lru(cmd.name)
    host.hide_popup()

    if cmd.name == "stop":
        host.stop_in_flight()
        return True
    host.clear_ask()
    if cmd.name == "help":
        host.append_output("\n" + format_help_text() + "\n")
        return True
    if cmd.name == "clear":
        host.clear_chat()
        return True
    host.append_output("\nslash: /%s\n" % cmd.name)
    return True
