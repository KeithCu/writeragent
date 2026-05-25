# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Calc add-in metadata for single-function UNO components."""

from __future__ import annotations

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
    """XAddIn-style metadata for an add-in that exposes exactly one function."""

    def __init__(self, ctx: Any, spec: CalcFunctionSpec) -> None:
        self.ctx = ctx
        self._spec = spec

    def getProgrammaticFunctionName(self, aDisplayName: str) -> str:
        if aDisplayName == self._spec.display_name:
            return self._spec.programmatic_name
        return ""

    def getDisplayFunctionName(self, aProgrammaticName: str) -> str:
        if aProgrammaticName == self._spec.programmatic_name:
            return self._spec.display_name
        return ""

    def getFunctionDescription(self, aProgrammaticName: str) -> str:
        if aProgrammaticName == self._spec.programmatic_name:
            return self._spec.description
        return ""

    def getArgumentDescription(self, aProgrammaticName: str, nArgument: int) -> str:
        if aProgrammaticName != self._spec.programmatic_name:
            return ""
        if 0 <= nArgument < len(self._spec.arg_descriptions):
            return self._spec.arg_descriptions[nArgument]
        return ""

    def getArgumentName(self, aProgrammaticName: str, nArgument: int) -> str:
        if aProgrammaticName != self._spec.programmatic_name:
            return ""
        if 0 <= nArgument < len(self._spec.arg_names):
            return self._spec.arg_names[nArgument]
        return ""

    def hasFunctionWizard(self, aProgrammaticName: str) -> bool:
        return aProgrammaticName == self._spec.programmatic_name

    def getArgumentCount(self, aProgrammaticName: str) -> int:
        if aProgrammaticName == self._spec.programmatic_name:
            return len(self._spec.arg_names)
        return 0

    def getArgumentIsOptional(self, aProgrammaticName: str, nArgument: int) -> bool:
        if aProgrammaticName != self._spec.programmatic_name:
            return False
        return nArgument >= self._spec.optional_from

    def getProgrammaticCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In"

    def getDisplayCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In"

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
