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

from typing import Any

from plugin.framework.errors import CalcError
from plugin.framework.module_base import ModuleBase

__all__ = ["CalcError", "CalcModule"]




class CalcModule(ModuleBase):
    """Registers Calc tools for cells, sheets, formulas, charts."""

    services: Any

    def initialize(self, services: Any) -> None:
        self.services = services

        # Move to late-import to avoid circular dependency (writer.base -> calc.base -> calc.__init__ -> forms -> writer.base)
        from . import forms  # noqa: F401  # pyright: ignore[reportUnusedImport]
        from . import specialized as specialized  # noqa: F401  # pyright: ignore[reportUnusedImport]
        from . import shapes  # noqa: F401  # pyright: ignore[reportUnusedImport]

        services.tools.auto_discover_package(__name__)
        services.tools.auto_discover_package(f"{__name__}.python")
        services.tools.auto_discover_package(f"{__name__}.spreadsheet_import")
