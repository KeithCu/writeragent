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

from typing import Any, NamedTuple

from plugin.framework.errors import UnoObjectError, uno_read
from plugin.calc.address_utils import (
    format_range_address,
    parse_address,
    parse_range_string,
    qualify_a1,
    split_sheet_and_name,
    split_sheet_prefix,
)
from plugin.doc.doc_type import is_calc


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


class NamedRangeHit(NamedTuple):
    container: Any
    scope: str  # sheet name, or "global"
    sheet: Any | None
    named_range: Any


def _named_range_hit(container: Any, name: str, scope: str, sheet: Any | None) -> NamedRangeHit | None:
    """Return the hit when *container* has a NamedRanges entry called *name*."""
    nrs = getattr(container, "NamedRanges", None)
    if nrs is None or not hasattr(nrs, "hasByName") or not nrs.hasByName(name):
        return None
    return NamedRangeHit(nrs, scope, sheet, nrs.getByName(name))


def find_named_range(doc: Any, name: str) -> NamedRangeHit | None:
    """Resolve a Calc named range in documented priority order.

    Order:
    1. Sheet-prefixed (e.g. ``Sheet1.Name`` or ``'Sheet1'!Name``): if prefix
       matches an existing sheet, look only in that sheet's NamedRanges.
    2. Active sheet local NamedRanges.
    3. Workbook global NamedRanges.
    4. Other agent-visible (leading ``_`` skipped) sheets in index order.
    """
    clean_name = name.strip()
    if not clean_name:
        return None

    prefix, bare_name = split_sheet_prefix(clean_name)
    if ":" in bare_name:
        return None

    sheets = uno_read(lambda: doc.getSheets() if hasattr(doc, "getSheets") else None, None)

    # 1. Sheet prefix
    if prefix and sheets is not None and hasattr(sheets, "hasByName") and sheets.hasByName(prefix):
        target_sheet = sheets.getByName(prefix)
        return _named_range_hit(target_sheet, bare_name, prefix, target_sheet)

    # 2. Active sheet
    active_sheet = uno_read(lambda: CalcBridge(doc).get_active_sheet(), None)
    active_name = (
        active_sheet.getName()
        if (active_sheet is not None and hasattr(active_sheet, "getName"))
        else None
    )
    if active_sheet is not None:
        scope = "" if active_name is None else active_name
        hit = _named_range_hit(active_sheet, clean_name, scope, active_sheet)
        if hit is not None:
            return hit

    # 3. Workbook global
    hit = _named_range_hit(doc, clean_name, "global", None)
    if hit is not None:
        return hit

    # 4. Other agent-visible sheets in index order
    if sheets is not None and hasattr(sheets, "getCount"):
        for idx in range(sheets.getCount()):
            sheet = sheets.getByIndex(idx)
            s_name = sheet.getName() if hasattr(sheet, "getName") else ""
            # Each pyuno call returns a new proxy, so identity never matches the active sheet.
            if active_name is not None and s_name == active_name:
                continue
            if not is_agent_visible_sheet(s_name):
                continue
            hit = _named_range_hit(sheet, clean_name, s_name, sheet)
            if hit is not None:
                return hit

    return None


class CalcBridge:
    """Bridge between the plugin layer and the UNO Calc document."""

    doc: Any

    def __init__(self, doc: Any) -> None:
        self.doc = doc

    def get_active_sheet(self) -> Any:
        """Return the currently active sheet.

        Falls back to the first sheet when the controller does not expose
        *getActiveSheet* (e.g. headless mode).

        Raises:
            UnoObjectError: Document is not a spreadsheet, or no sheet was found.
        """
        if not hasattr(self.doc, "getSheets"):
            raise UnoObjectError("Active document is not a spreadsheet.")

        controller = self.doc.getCurrentController()
        if hasattr(controller, "getActiveSheet"):
            sheet = controller.getActiveSheet()
        else:
            sheets = self.doc.getSheets()
            sheet = sheets.getByIndex(0)

        if sheet is None:
            raise UnoObjectError("No active sheet found.")
        return sheet

    def get_sheet(self, name: str) -> Any:
        """Return a sheet by name.

        Raises:
            UnoObjectError: no such sheet, listing the visible sheets that exist.
        """
        sheets = self.doc.getSheets()
        if not sheets.hasByName(name):
            available = filter_agent_sheet_names(sheets.getElementNames())
            # Sheet tools propagate this UnoObjectError; execute_safe keeps WriterAgentException text.
            raise UnoObjectError("No sheet named '%s'. Available: %s" % (name, ", ".join(available)))
        return sheets.getByName(name)

    def resolve(self, ref: str, sheet_name: str | None = None) -> tuple[Any, str]:
        """Resolve a possibly sheet-qualified reference.

        Returns ``(sheet, address)`` where *address* has no prefix. A
        prefix on the reference wins over *sheet_name*; disagreeing is an
        error rather than a silent choice.
        """
        address, name = split_sheet_and_name(ref, sheet_name)
        sheet = self.get_sheet(name) if name else self.get_active_sheet()
        return sheet, address

    def get_cell_by_address(self, address: str) -> Any:
        """Return the cell object for *address* (optionally sheet-qualified)."""
        sheet, address = self.resolve(address)
        col, row = parse_address(address)
        return sheet.getCellByPosition(col, row)

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

        An unqualified name defined both on the active sheet and in the
        workbook resolves to the sheet-local range, matching Calc.
        """
        range_or_address = range_or_address.strip()
        hit = find_named_range(self.doc, range_or_address)
        if hit is not None and hasattr(hit.named_range, "getReferredCells"):
            cells = hit.named_range.getReferredCells()
            if cells is not None:
                return cells

        sheet, address = self.resolve(range_or_address)

        if ":" in address:
            return self.get_cell_range(sheet, address)

        try:
            col, row = parse_address(address)
            return sheet.getCellByPosition(col, row)
        except ValueError:
            return sheet.getCellRangeByName(address)


def calc_selection_to_a1(doc: Any) -> str | None:
    """Format the current Calc selection as a sheet-qualified A1 address."""
    if doc is None or not is_calc(doc):
        return None

    def _get() -> str | None:
        controller = doc.getCurrentController()
        if controller is None:
            return None
        selection = controller.getSelection()
        if selection is None or not hasattr(selection, "getRangeAddress"):
            return None
        addr = selection.getRangeAddress()
        bridge = CalcBridge(doc)
        sheet = bridge.get_active_sheet()
        sheet_name = str(sheet.getName())
        cell_part = format_range_address(addr, collapse_single=True)
        return qualify_a1(sheet_name, cell_part)

    return uno_read(_get, None)
