# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO Calc add-in for =PY() / =PYTHON() (no LLM imports at module load).

WriterAgent OXT entry. LibrePy uses ``addin_librepy.py`` (same implementation
name so saved ``ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.*`` formulas stay portable).
"""

from __future__ import annotations

import os
import sys

# --- Minimal stdlib-only bootstrap (MUST be before any "from plugin..." import) ---
# unopkg writeRegistryInfo loads this file before the extension root is on sys.path.
_this = os.path.abspath(__file__)
for __ in range(4):  # plugin/calc/python/addin.py → plugin/calc/python/ → plugin/calc/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=4, also_add_plugin_dir=True, also_add_lib=True, also_add_vendor=True)

import unohelper  # noqa: E402

from plugin.calc.python.addin_impl import IMPL_NAME, PythonFunction  # noqa: E402

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PythonFunction, IMPL_NAME, ("com.sun.star.sheet.AddIn",))
