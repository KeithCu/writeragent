# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO Calc add-in for LibrePy =PY() / =PYTHON() (no LLM imports at module load).

Registers under ``org.extension.writeragent.PythonFunction`` so spreadsheets saved with
fully qualified ``ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.*`` formulas work when opened
under WriterAgent. Extension id / menus remain ``org.extension.librepy``.
"""

from __future__ import annotations

import os
import sys

_this = os.path.abspath(__file__)
for __ in range(4):
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=4, also_add_plugin_dir=True, also_add_lib=True, also_add_vendor=True)

import unohelper  # noqa: E402

from plugin.calc.python.addin_impl import IMPL_NAME, PythonFunction  # noqa: E402

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PythonFunction, IMPL_NAME, ("com.sun.star.sheet.AddIn",))
