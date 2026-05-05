# WriterAgent - OpenRouter chat-completions body extras
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deep-merge optional OpenRouter-specific fields into chat completion JSON bodies."""

from __future__ import annotations

import copy
from typing import Any

# Keys WriterAgent builds; extras must not replace these.
OPENROUTER_CHAT_EXTRA_BLOCKLIST: frozenset[str] = frozenset({"messages", "tools", "tool_choice", "stream"})


def merge_openrouter_chat_extra(base: dict[str, Any], extra: dict[str, Any] | None) -> None:
    """Merge *extra* into *base* in place. Skips blocklisted keys; recurses into dict values."""
    if not extra:
        return
    for key, val in extra.items():
        if key in OPENROUTER_CHAT_EXTRA_BLOCKLIST:
            continue
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            merge_openrouter_chat_extra(base[key], val)
        elif isinstance(val, dict):
            base[key] = copy.deepcopy(val)
        else:
            base[key] = val
