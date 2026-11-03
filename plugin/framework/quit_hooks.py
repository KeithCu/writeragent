# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Callbacks run on the UI thread from the desktop terminate listener.

Hooks run before Kokoro shutdown and ``managed_subprocess.shutdown_all``. Each
hook must finish quickly (no unbounded waits); LibreOffice exit continues even
when a hook logs an exception.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_hooks: list[Callable[[], None]] = []


def register_quit_hook(hook: Callable[[], None]) -> None:
    if hook not in _hooks:
        _hooks.append(hook)


def run_quit_hooks() -> None:
    for hook in _hooks:
        try:
            hook()
        except Exception:
            log.exception("Quit hook failed on desktop termination")
