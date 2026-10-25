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
    arg_names: tuple[str, ...]
    arg_descriptions: tuple[str, ...]
    optional_from: int  # first optional argument index (0-based)

    def __post_init__(self) -> None:
        if not self.display_name or not self.programmatic_name:
            raise ValueError("display_name and programmatic_name cannot be empty")
        if len(self.arg_names) != len(self.arg_descriptions):
            raise ValueError("arg_names and arg_descriptions must have the same length")
        if not (0 <= self.optional_from <= len(self.arg_names)):
            raise ValueError(f"optional_from ({self.optional_from}) must be between 0 and {len(self.arg_names)}")


class CalcAddInBase(unohelper.Base):
    """XAddIn-style metadata for one or more Calc add-in functions on one component."""

    ctx: Any
    _specs: tuple[CalcFunctionSpec, ...]
    _by_display: dict[str, CalcFunctionSpec]
    _by_programmatic: dict[str, CalcFunctionSpec]

    def __init__(self, ctx: Any, spec: CalcFunctionSpec | tuple[CalcFunctionSpec, ...]) -> None:
        self.ctx = ctx
        self._specs = (spec,) if isinstance(spec, CalcFunctionSpec) else spec
        self._by_display = {s.display_name: s for s in self._specs}
        self._by_programmatic = {s.programmatic_name: s for s in self._specs}
        if len(self._by_display) < len(self._specs) or len(self._by_programmatic) < len(self._specs):
            raise ValueError("Duplicate display_name or programmatic_name in specs")

    def _resolve(self, programmatic_name: str) -> CalcFunctionSpec | None:
        return self._by_programmatic.get(programmatic_name)

    @staticmethod
    def _arg_value(values: tuple[str, ...], index: int) -> str:
        return values[index] if 0 <= index < len(values) else ""

    def getProgrammaticFunctionName(self, aDisplayName: str) -> str:
        spec = self._by_display.get(aDisplayName)
        return spec.programmatic_name if spec is not None else ""

    def getDisplayFunctionName(self, aProgrammaticName: str) -> str:
        spec = self._resolve(aProgrammaticName)
        return spec.display_name if spec is not None else ""

    def getFunctionDescription(self, aProgrammaticName: str) -> str:
        spec = self._resolve(aProgrammaticName)
        return spec.description if spec is not None else ""

    def getArgumentDescription(self, aProgrammaticName: str, nArgument: int) -> str:
        spec = self._resolve(aProgrammaticName)
        return self._arg_value(spec.arg_descriptions, nArgument) if spec is not None else ""

    def getProgrammaticCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if self._resolve(aProgrammaticName) is not None else ""

    def getDisplayCategoryName(self, aProgrammaticName: str) -> str:
        return "Add-In" if self._resolve(aProgrammaticName) is not None else ""

    def getLocale(self) -> Any:
        return self.ctx.ServiceManager.createInstance("com.sun.star.lang.Locale", ("en", "US", ""))

    def setLocale(self, locale: Any) -> None:
        pass

    def supportsService(self, name: str) -> bool:
        return name in self.getSupportedServiceNames()

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return (ADDIN_SERVICE,)
