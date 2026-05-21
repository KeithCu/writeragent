# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Backward-compatible re-exports; UNO registration lives in python_addin.py and prompt_addin.py.

Split (2026-05): =PYTHON() -> plugin.calc.python_addin / calc_python_handlers;
=PROMPT() -> plugin.calc.prompt_addin / calc_prompt_handlers.

IDL: extension/idl/XPythonFunction.idl, extension/idl/XPromptFunction.idl
"""

from __future__ import annotations

from plugin.calc.calc_python_handlers import execute_python_addin
from plugin.calc.calc_python_helpers import (
    MATRIX_SCALAR_SESSIONS,
    WorkerResultSession,
    _WorkerResultSession,
    _is_scalar_index_arg,
    finalize_python_return,
    is_scalar_index_arg,
    to_calc_compatible,
)
from plugin.calc.prompt_addin import PromptAddIn, PromptFunction
from plugin.calc.python_addin import PythonAddIn, PythonFunction

__all__ = [
    "PromptFunction",
    "PromptAddIn",
    "PythonFunction",
    "PythonAddIn",
    "execute_python_addin",
    "finalize_python_return",
    "is_scalar_index_arg",
    "to_calc_compatible",
    "MATRIX_SCALAR_SESSIONS",
    "WorkerResultSession",
    "_WorkerResultSession",
    "_is_scalar_index_arg",
]
