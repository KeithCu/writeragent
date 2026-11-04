# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Callbacks run on the UI thread from the desktop terminate listener.

Cooperative hooks register during module init; the WriterAgent listener calls
``register_quit_hook_last(get_supervisor().shutdown_all)`` immediately before
``run_quit_hooks()`` so warm-child retire and the managed sweep run after
cooperative stop (tunnel funnel reset needs its child alive; ``shutdown_all``
SIGKILLs registered peers). Other hooks run in list order. Each hook must return
quickly (no unbounded waits); a raised hook is logged and skipped; LibreOffice
exit continues.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_hooks: list[Callable[[], None]] = []


def register_quit_hook(hook: Callable[[], None]) -> None:
    if hook not in _hooks:
        _hooks.append(hook)


def register_quit_hook_last(hook: Callable[[], None]) -> None:
    """Drop hook if present (equality), then append. One entry, at the end."""
    if hook in _hooks:
        _hooks.remove(hook)
    _hooks.append(hook)


def run_quit_hooks() -> None:
    for hook in _hooks:
        try:
            hook()
        except Exception:
            log.exception("Quit hook failed on desktop termination")
