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
"""In-process UNO bridge for Calc.

Wraps a Calc document and provides convenience methods for accessing
sheets, cells, and ranges. Ported from core/calc_bridge.py for the
plugin framework.
"""

import logging
from typing import Any

from plugin.calc.address_utils import (
    column_to_index,
    index_to_column,
    parse_address,
    parse_range_string,
    split_sheet_prefix,
)

log = logging.getLogger("writeragent.calc")


def is_agent_visible_sheet(name: str) -> bool:
    """True unless *name* is a LibreOffice internal sheet.

    Leading ``_`` covers ``__Anonymous_Sheet_DB__0`` (xlsx→ods database-range
    tab) and similar internals. The sheets stay in the document; agent-facing
    lists omit them. Delete-at-trial-open is parked (eval-2 §2.7).
    """
    return bool(name) and not str(name).startswith("_")


def filter_agent_sheet_names(names: Any) -> list[str]:
    """Drop leading-``_`` names from an agent-facing sheet enumeration."""
    return [n for n in names if is_agent_visible_sheet(n)]


class CalcBridge:
    """Bridge between the plugin layer and the UNO Calc document."""

    doc: Any

    def __init__(self, doc: Any) -> None:
        self.doc = doc

    def get_active_document(self) -> Any:
        """Return the wrapped document."""
        return self.doc

    def get_active_sheet(self) -> Any:
        """Return the currently active sheet.

        Falls back to the first sheet when the controller does not expose
        *getActiveSheet* (e.g. headless mode).

        Raises:
            RuntimeError: Document is not a spreadsheet or no sheet found.
        """
        if not hasattr(self.doc, "getSheets"):
            raise RuntimeError("Active document is not a spreadsheet.")

        controller = self.doc.getCurrentController()
        if hasattr(controller, "getActiveSheet"):
            sheet = controller.getActiveSheet()
        else:
            sheets = self.doc.getSheets()
            sheet = sheets.getByIndex(0)

        if sheet is None:
            raise RuntimeError("No active sheet found.")
        return sheet

    def get_sheet(self, name: str) -> Any:
        """Return a sheet by name.

        Raises:
            ValueError: no such sheet, listing the ones that exist.
        """
        sheets = self.doc.getSheets()
        if not sheets.hasByName(name):
            available = filter_agent_sheet_names(sheets.getElementNames())
            raise ValueError(
                "No sheet named '%s'. Available: %s" % (name, ", ".join(available))
            )
        return sheets.getByName(name)

    def resolve(self, ref: str, sheet_name: str | None = None) -> tuple[Any, str]:
        """Resolve a possibly sheet-qualified reference.

        Returns ``(sheet, address)`` where *address* has no prefix. A
        prefix on the reference wins over *sheet_name*; disagreeing is an
        error rather than a silent choice.
        """
        prefix, address = split_sheet_prefix(ref)
        if prefix is not None and sheet_name and prefix != sheet_name:
            raise ValueError(
                "Reference names sheet '%s' but sheet_name says '%s' — "
                "pass one or the other." % (prefix, sheet_name)
            )
        name = prefix or sheet_name
        sheet = self.get_sheet(name) if name else self.get_active_sheet()
        return sheet, address

    def get_cell(self, sheet: Any, col: int, row: int) -> Any:
        """Return the cell object at *col*, *row* on *sheet*."""
        return sheet.getCellByPosition(col, row)

    def get_cell_by_address(self, address: str) -> Any:
        """Return the cell object for *address* (optionally sheet-qualified)."""
        sheet, address = self.resolve(address)
        col, row = parse_address(address)
        return self.get_cell(sheet, col, row)

    def get_cell_range(self, sheet: Any, range_str: str) -> Any:
        """Return a cell range object from a range string like ``A1:D10``.

        A sheet prefix on *range_str* wins over the *sheet* argument, so a
        caller that has already resolved the active sheet still honours an
        explicit ``Sheet1.A1:C5``.
        """
        prefix, range_str = split_sheet_prefix(range_str)
        if prefix is not None:
            sheet = self.get_sheet(prefix)
        start, end = parse_range_string(range_str)
        return sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1])

    def resolve_range_or_address(self, range_or_address: str) -> Any:
        """Resolves a string identifier to a cell or cell range object.

        Supports:
        - Named Ranges (global or sheet-local, e.g. "MyRange", "'Sheet1'!MyRange", "Sheet1.MyRange")
        - Sheet-qualified A1 refs (e.g. "Sheet1.A1:B2", "'Data Sheet'!B2")
        - A1:B2 Range Strings
        - A1 Cell Address Strings
        """
        range_or_address = range_or_address.strip()
        prefix, bare_name = split_sheet_prefix(range_or_address)

        # 1. If explicitly sheet-qualified (e.g. Sheet1.MyRange), check sheet-local NamedRanges first
        if prefix and hasattr(self.doc, "getSheets"):
            sheets = self.doc.getSheets()
            if sheets.hasByName(prefix):
                sheet_obj = sheets.getByName(prefix)
                if hasattr(sheet_obj, "NamedRanges") and sheet_obj.NamedRanges.hasByName(bare_name):
                    named_range = sheet_obj.NamedRanges.getByName(bare_name)
                    if hasattr(named_range, "getReferredCells"):
                        cells = named_range.getReferredCells()
                        if cells is not None:
                            return cells

        # 2. Check workbook global NamedRanges
        if hasattr(self.doc, "NamedRanges") and self.doc.NamedRanges.hasByName(range_or_address):
            named_range = self.doc.NamedRanges.getByName(range_or_address)
            if hasattr(named_range, "getReferredCells"):
                cells = named_range.getReferredCells()
                if cells is not None:
                    return cells

        # 3. Check active sheet's local NamedRanges
        try:
            active_sheet = self.get_active_sheet()
            if hasattr(active_sheet, "NamedRanges") and active_sheet.NamedRanges.hasByName(range_or_address):
                named_range = active_sheet.NamedRanges.getByName(range_or_address)
                if hasattr(named_range, "getReferredCells"):
                    cells = named_range.getReferredCells()
                    if cells is not None:
                        return cells
        except Exception:
            pass

        sheet, address = self.resolve(range_or_address)

        if ":" in address:
            return self.get_cell_range(sheet, address)

        try:
            col, row = parse_address(address)
            return self.get_cell(sheet, col, row)
        except Exception:
            return sheet.getCellRangeByName(address)

    @staticmethod
    def _index_to_column(index: int) -> str:
        return index_to_column(index)

    @staticmethod
    def _column_to_index(col_str: str) -> int:
        return column_to_index(col_str)

    @staticmethod
    def parse_range_string(range_str: str) -> tuple[tuple[int, int], tuple[int, int]]:
        return parse_range_string(range_str)

    @staticmethod
    def _range_to_str(range_addr: Any) -> str:
        """Convert a CellRangeAddress to a string."""
        return "%s%d:%s%d" % (
            index_to_column(range_addr.StartColumn),
            range_addr.StartRow + 1,
            index_to_column(range_addr.EndColumn),
            range_addr.EndRow + 1,
        )
