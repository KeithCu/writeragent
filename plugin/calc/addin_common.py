# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Calc add-in metadata for single- or multi-function UNO components."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import unohelper

ADDIN_SERVICE = "com.sun.star.sheet.AddIn"


@dataclass(frozen=True)
class CalcFunctionSpec:
    """Metadata for one Calc add-in function (display name, args, descriptions)."""

    display_name: str
    programmatic_name: str
    description: str
    arg_descriptions: tuple[str, ...]


class CalcAddInBase(unohelper.Base):
    """XAddIn-style metadata for one or more Calc add-in functions on one component."""

    ctx: Any
    _by_display: dict[str, CalcFunctionSpec]
    _by_programmatic: dict[str, CalcFunctionSpec]

    def __init__(self, ctx: Any, spec: tuple[CalcFunctionSpec, ...]) -> None:
        self.ctx = ctx
        self._by_display = {s.display_name: s for s in spec}
        self._by_programmatic = {s.programmatic_name: s for s in spec}

    def getProgrammaticFunctionName(self, aDisplayName: str) -> str:
        spec = self._by_display.get(aDisplayName)
        return spec.programmatic_name if spec is not None else ""

    def getDisplayFunctionName(self, aProgrammaticName: str) -> str:
        spec = self._by_programmatic.get(aProgrammaticName)
        return spec.display_name if spec is not None else ""

    def getFunctionDescription(self, aProgrammaticName: str) -> str:
        spec = self._by_programmatic.get(aProgrammaticName)
        return spec.description if spec is not None else ""

    def getArgumentDescription(self, aProgrammaticName: str, nArgument: int) -> str:
        spec = self._by_programmatic.get(aProgrammaticName)
        if spec is None:
            return ""
        descriptions = spec.arg_descriptions
        return descriptions[nArgument] if 0 <= nArgument < len(descriptions) else ""

    def getProgrammaticCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if aProgrammaticName in self._by_programmatic else ""

    def getDisplayCategoryName(self, aProgrammaticName: str) -> str:
        return self.getProgrammaticCategoryName(aProgrammaticName)

    def getLocale(self) -> Any:
        return self.ctx.ServiceManager.createInstance("com.sun.star.lang.Locale", ("en", "US", ""))

    def setLocale(self, locale: Any) -> None:
        pass

    def supportsService(self, name: str) -> bool:
        return name == ADDIN_SERVICE

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return (ADDIN_SERVICE,)
