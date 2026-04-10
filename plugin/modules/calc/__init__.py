# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Calc module — tools for Calc spreadsheet manipulation."""

from plugin.framework.errors import WriterAgentException
from plugin.framework.module_base import ModuleBase


class CalcError(WriterAgentException):
    """Calc-specific errors."""

    def __init__(self, message, code="CALC_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class CalcModule(ModuleBase):
    """Registers Calc tools for cells, sheets, formulas, charts."""

    def initialize(self, services):
        self.services = services

        services.tools.auto_discover_package(__name__)

from . import forms  # noqa: F401 — re-exports writer.forms tools for package clarity
from . import specialized
