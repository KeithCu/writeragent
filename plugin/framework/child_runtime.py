# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Child-side helpers for warm worker subprocesses (stdlib only)."""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)


def die_with_parent() -> None:
    """Kill this child if the parent process dies while we are in native code.

    Linux ``PR_SET_PDEATHSIG`` is the parent-death signal; the ppid check
    covers the parent dying before prctl runs. No-op on non-Linux.
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        ppid = os.getppid()
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(1, 9, 0, 0, 0) != 0:
            return
        if os.getppid() != ppid:
            os.kill(os.getpid(), 9)
    except Exception as exc:
        log.debug("Failed to configure PR_SET_PDEATHSIG: %s", exc)
