# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shim: IPC implementation lives in ``plugin.framework.ipc`` (logger name unchanged)."""

from __future__ import annotations

import plugin.framework.ipc as _impl
from plugin.framework.ipc import *  # noqa: F403

for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    if _name.startswith("_"):
        globals()[_name] = getattr(_impl, _name)

_write_all = _impl._write_all
_stop_requested = _impl._stop_requested
_PICKLE_LOAD_ERRORS = _impl._PICKLE_LOAD_ERRORS
