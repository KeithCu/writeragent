# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Callbacks run from the desktop terminate listener before other shutdown steps."""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_hooks: list[Callable[[], None]] = []


def register_quit_hook(hook: Callable[[], None]) -> None:
    _hooks.append(hook)


def run_quit_hooks() -> None:
    for hook in _hooks:
        try:
            hook()
        except Exception:
            log.exception("Quit hook failed on desktop termination")
