# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Calc add-in metadata for single- or multi-function UNO components."""

from __future__ import annotations

import os
import sys

# --- Minimal stdlib-only bootstrap (MUST be before any "from plugin..." import) ---
_this = os.path.abspath(__file__)
for __ in range(3):  # plugin/calc/addin_common.py → plugin/calc/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from dataclasses import dataclass
from typing import Any

import unohelper


from plugin.framework.uno_bootstrap import ensure_plugin_on_path


def ensure_addin_paths() -> str:
    """
    Legacy wrapper around the central bootstrap for Calc add-ins.

    Prefer calling ensure_plugin_on_path directly in new code.
    This function is kept for backward compatibility during the TD1 migration.
    """
    # levels_up=3 from inside plugin/calc/
    # also_add_plugin_dir gives us the plugin/ directory itself
    return ensure_plugin_on_path(
        __file__,
        levels_up=3,
        also_add_plugin_dir=True,
    )


@dataclass(frozen=True)
class CalcFunctionSpec:
    """Metadata for one Calc add-in function (display name, args, descriptions)."""

    display_name: str
    programmatic_name: str
    description: str
    arg_names: tuple[str, ...]
    arg_descriptions: tuple[str, ...]
    optional_from: int  # first optional argument index (0-based)


class SingleFunctionAddInBase(unohelper.Base):
    """XAddIn-style metadata for one or more Calc add-in functions on one component."""

    def __init__(self, ctx: Any, spec: CalcFunctionSpec | tuple[CalcFunctionSpec, ...]) -> None:
        self.ctx = ctx
        self._specs = (spec,) if isinstance(spec, CalcFunctionSpec) else spec

    def _spec_by_display_name(self, display_name: str) -> CalcFunctionSpec | None:
        for spec in self._specs:
            if display_name == spec.display_name:
                return spec
        return None

    def _spec_by_programmatic_name(self, programmatic_name: str) -> CalcFunctionSpec | None:
        for spec in self._specs:
            if programmatic_name == spec.programmatic_name:
                return spec
        return None

    def getProgrammaticFunctionName(self, aDisplayName: str) -> str:
        spec = self._spec_by_display_name(aDisplayName)
        return spec.programmatic_name if spec is not None else ""

    def getDisplayFunctionName(self, aProgrammaticName: str) -> str:
        spec = self._spec_by_programmatic_name(aProgrammaticName)
        return spec.display_name if spec is not None else ""

    def getFunctionDescription(self, aProgrammaticName: str) -> str:
        spec = self._spec_by_programmatic_name(aProgrammaticName)
        return spec.description if spec is not None else ""

    def getArgumentDescription(self, aProgrammaticName: str, nArgument: int) -> str:
        spec = self._spec_by_programmatic_name(aProgrammaticName)
        if spec is None:
            return ""
        if 0 <= nArgument < len(spec.arg_descriptions):
            return spec.arg_descriptions[nArgument]
        return ""

    def getArgumentName(self, aProgrammaticName: str, nArgument: int) -> str:
        spec = self._spec_by_programmatic_name(aProgrammaticName)
        if spec is None:
            return ""
        if 0 <= nArgument < len(spec.arg_names):
            return spec.arg_names[nArgument]
        return ""

    def hasFunctionWizard(self, aProgrammaticName: str) -> bool:
        return self._spec_by_programmatic_name(aProgrammaticName) is not None

    def getArgumentCount(self, aProgrammaticName: str) -> int:
        spec = self._spec_by_programmatic_name(aProgrammaticName)
        return len(spec.arg_names) if spec is not None else 0

    def getArgumentIsOptional(self, aProgrammaticName: str, nArgument: int) -> bool:
        spec = self._spec_by_programmatic_name(aProgrammaticName)
        if spec is None:
            return False
        return nArgument >= spec.optional_from

    def getProgrammaticCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if self._spec_by_programmatic_name(aProgrammaticName) is not None else ""

    def getDisplayCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if self._spec_by_programmatic_name(aProgrammaticName) is not None else ""

    # Future: case-insensitive programmatic name matching (e.g. XLSX import lowercases PYTHON → python).
    # Uncomment and replace the methods above when ready to try.
    '''
    def _matches_programmatic_name(self, name: str) -> bool:
        # Calc may pass display or programmatic id; XLSX import often lowercases PYTHON → python.
        return name.lower() == self._spec.programmatic_name.lower()

    def getProgrammaticFunctionName(self, aDisplayName: str) -> str:
        if aDisplayName == self._spec.display_name or self._matches_programmatic_name(aDisplayName):
            return self._spec.programmatic_name
        return ""

    def getDisplayFunctionName(self, aProgrammaticName: str) -> str:
        if self._matches_programmatic_name(aProgrammaticName):
            return self._spec.display_name
        return ""

    def getFunctionDescription(self, aProgrammaticName: str) -> str:
        if not self._matches_programmatic_name(aProgrammaticName):
            return ""
        return self._spec.description

    def getArgumentDescription(self, aProgrammaticName: str, nArgument: int) -> str:
        if not self._matches_programmatic_name(aProgrammaticName):
            return ""
        if 0 <= nArgument < len(self._spec.arg_descriptions):
            return self._spec.arg_descriptions[nArgument]
        return ""

    def getArgumentName(self, aProgrammaticName: str, nArgument: int) -> str:
        if not self._matches_programmatic_name(aProgrammaticName):
            return ""
        if 0 <= nArgument < len(self._spec.arg_names):
            return self._spec.arg_names[nArgument]
        return ""

    def hasFunctionWizard(self, aProgrammaticName: str) -> bool:
        return self._matches_programmatic_name(aProgrammaticName)

    def getArgumentCount(self, aProgrammaticName: str) -> int:
        if not self._matches_programmatic_name(aProgrammaticName):
            return 0
        return len(self._spec.arg_names)

    def getArgumentIsOptional(self, aProgrammaticName: str, nArgument: int) -> bool:
        if not self._matches_programmatic_name(aProgrammaticName):
            return False
        return nArgument >= self._spec.optional_from

    def getProgrammaticCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if self._matches_programmatic_name(aProgrammaticName) else ""

    def getDisplayCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if self._matches_programmatic_name(aProgrammaticName) else ""
    '''

    def getLocale(self) -> Any:
        return self.ctx.ServiceManager.createInstance("com.sun.star.lang.Locale", ("en", "US", ""))

    def setLocale(self, locale: Any) -> None:
        pass

    def load(self, xSomething: Any) -> None:
        pass

    def unload(self) -> None:
        pass

    def supportsService(self, name: str) -> bool:
        return name in self.getSupportedServiceNames()

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return ("com.sun.star.sheet.AddIn",)
