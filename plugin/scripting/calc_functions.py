# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Public ``xl`` facade — NumPy-heavy implementation in ``plugin.scripting.venv.calc_functions``."""

from __future__ import annotations

import importlib
from typing import Any


def __getattr__(name: str) -> Any:
    mod = importlib.import_module("plugin.scripting.venv.calc_functions")
    return getattr(mod, name)


def __dir__() -> list[str]:
    mod = importlib.import_module("plugin.scripting.venv.calc_functions")
    return sorted(set(dir(mod)))
