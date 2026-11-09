# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional dependency import without pulling venv_sandbox (breaks to_wire cycle)."""
from __future__ import annotations

import importlib
import sys
import threading
from typing import Any

# RLock: a module that calls optional_module during its own import on this
# thread must re-enter. A plain Lock deadlocks there. The nested call then
# takes the "still initializing → None" path inside the lock.
_OPTIONAL_MODULE_LOCK = threading.RLock()


def optional_module(name: str) -> Any | None:
    if name in sys.modules:
        mod = sys.modules[name]
        spec = getattr(mod, "__spec__", None)
        if spec is None or not getattr(spec, "_initializing", False):
            return mod
    with _OPTIONAL_MODULE_LOCK:
        if name in sys.modules:
            mod = sys.modules[name]
            spec = getattr(mod, "__spec__", None)
            if spec is None or not getattr(spec, "_initializing", False):
                return mod
            # import_module returns the partial module already in sys.modules
            # while spec._initializing is set, so holding the lock does not
            # mean the import has finished. Another thread still owns it.
            # None is "not ready", same as the check above the lock.
            return None
        try:
            return importlib.import_module(name)
        except Exception:
            return None
