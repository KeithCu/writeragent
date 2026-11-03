#!/usr/bin/env python3
# WriterAgent - Static Yellow-Root Entry Points for Thread Safety Tools
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Static yellow-root entry points shared across thread-safety tools."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Static yellow roots (sync host / bridge dispatch). Add a new sync UNO
# entry point here only. Note that _notify_thread_violation is included
# for the #402 deadlock even though it does not enter sync_host_dispatch().
SYNC_HOST_ENTRYPOINTS: frozenset[str] = frozenset({
    "execute_python_addin",
    "_execute_python_addin_impl",
    "execute_prompt_addin",
    "_execute_prompt_addin_impl",
    "py",
    "python",
    "prompt",
    "session_key",
    "_notify_thread_violation",
})

_TOKEN = "__SYNC_HOST_ENTRYPOINT_REGEX__"


def sync_host_entrypoint_regex() -> str:
    escaped = [re.escape(name) for name in sorted(SYNC_HOST_ENTRYPOINTS)]
    return f"^({'|'.join(escaped)})$"


def write_opengrep_rules(template: Path, dest: Path) -> None:
    text = template.read_text(encoding="utf-8")
    count = text.count(_TOKEN)
    if count != 1:
        raise SystemExit(
            f"Expected template '{template}' to contain '{_TOKEN}' exactly once, found {count}"
        )
    rendered = text.replace(_TOKEN, sync_host_entrypoint_regex())
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered, encoding="utf-8")
