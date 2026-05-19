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
"""General utilities for Calc tools."""

from plugin.framework.errors import UnoObjectError


def resolve_sheet(doc, sheet_name=None):
    """Return the target sheet (by name or active)."""
    if sheet_name:
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise UnoObjectError("Sheet not found: %s" % sheet_name)
        return sheets.getByName(sheet_name)
    controller = doc.getCurrentController()
    if hasattr(controller, "getActiveSheet"):
        return controller.getActiveSheet()
    return doc.getSheets().getByIndex(0)
