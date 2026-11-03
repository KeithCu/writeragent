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


def die_with_parent() -> bool:
    """Kill this child if the parent process dies while we are in native code.

    Linux ``PR_SET_PDEATHSIG`` follows the thread that created the process,
    not the process as a whole. The ppid check covers the parent dying
    before prctl runs. After the parent dies, ``getppid()`` is the
    subreaper, not 1. No-op on non-Linux.

    Returns True when PDEATHSIG was configured (Linux and prctl succeeded).
    """
    if sys.platform != "linux":
        return False
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        SIGKILL = 9
        ppid = os.getppid()
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0:
            return False
        if os.getppid() != ppid:
            os.kill(os.getpid(), SIGKILL)
        return True
    except Exception as exc:
        log.debug("Failed to configure PR_SET_PDEATHSIG: %s", exc)
        return False
