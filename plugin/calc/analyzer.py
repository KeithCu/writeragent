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
"""Sheet analyzer — analyses the structure and statistics of Calc sheets.

Ported from core/calc_sheet_analyzer.py for the plugin framework.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Sequence

from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS, truncate_chat_context
from plugin.framework.errors import (
    DocumentDisposedError,
    ToolExecutionError,
    check_disposed,
    is_disposed_exception,
    is_real_disposal,
    safe_call,
    uno_read,
)
from plugin.framework.thread_guard import main_thread_only
from plugin.calc.address_utils import format_range
from plugin.calc.bridge import CalcBridge, filter_agent_sheet_names, is_agent_visible_sheet

log = logging.getLogger("writeragent.calc")

_MAX_CONTEXT_HEADERS = 100


def _format_columns(headers: Sequence[str | None]) -> str:
    """Format non-blank headers, capping at _MAX_CONTEXT_HEADERS."""
    non_blank = [h for h in headers if h is not None]
    if len(non_blank) > _MAX_CONTEXT_HEADERS:
        remaining = len(non_blank) - _MAX_CONTEXT_HEADERS
        return f"{', '.join(non_blank[:_MAX_CONTEXT_HEADERS])} (+{remaining} more columns)"
    return ", ".join(non_blank)


def _header_text(val: Any) -> str | None:
    """Normalize a header value from getDataArray."""
    if val is None or val == "":
        return None
    if isinstance(val, float) and abs(val) < 1e15 and val.is_integer():
        return str(int(val))
    return str(val)


@dataclass(frozen=True)
class _VisibleSheet:
    names: list[str]
    target: str | None
    summary: dict[str, Any] | None
    active_is_visible: bool
    bridge: CalcBridge


def _resolve_visible_sheet(model: Any) -> _VisibleSheet:
    bridge = CalcBridge(model)
    names = filter_agent_sheet_names(model.getSheets().getElementNames())
    active_name = bridge.get_active_sheet().getName()
    if is_agent_visible_sheet(active_name):
        target = active_name
        active_is_visible = True
    elif names:
        target = names[0]
        active_is_visible = False
    else:
        target = None
        active_is_visible = False

    summary = (
        SheetAnalyzer(bridge).get_sheet_summary(sheet_name=target, include_extras=False)
        if target is not None
        else None
    )
    return _VisibleSheet(names, target, summary, active_is_visible, bridge)



class SheetAnalyzer:
    """Analyses the structure and data of a worksheet."""

    bridge: Any

    def __init__(self, bridge: Any) -> None:
        """
        Args:
            bridge: CalcBridge instance.
        """
        self.bridge = bridge

    def get_sheet_summary(self, sheet_name: str | None = None, include_extras: bool = True) -> dict[str, Any]:
        """Return a general summary of the active or specified sheet.

        Args:
            sheet_name: Optional name of the sheet to analyse.
            include_extras: Whether to query optional metrics (charts, annotations,
                merges, shapes). False for chat context.

        Returns:
            dict with keys: sheet_name, used_range, row_count, col_count,
            headers, and optionally chart_count, charts, annotation_count,
            has_merges, shape_count.
        """
        try:
            if sheet_name:
                sheet = self.bridge.get_sheet(sheet_name)
            else:
                sheet = self.bridge.get_active_sheet()

            cursor = sheet.createCursor()
            cursor.gotoStartOfUsedArea(False)
            cursor.gotoEndOfUsedArea(True)

            range_addr = cursor.getRangeAddress()
            start_col = range_addr.StartColumn
            start_row = range_addr.StartRow
            end_col = range_addr.EndColumn
            end_row = range_addr.EndRow

            row_count = end_row - start_row + 1
            col_count = end_col - start_col + 1

            used_range = format_range(start_col, start_row, end_col, end_row, collapse_single=False)

            header_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, start_row)
            header_data = header_range.getDataArray()

            headers = [_header_text(val) for val in header_data[0]] if header_data else []

            result: dict[str, Any] = {
                "sheet_name": sheet.getName(),
                "used_range": used_range,
                "row_count": row_count,
                "col_count": col_count,
                "headers": headers,
            }

            if include_extras:
                charts = uno_read(sheet.getCharts, None)
                result["chart_count"] = 0 if charts is None else uno_read(charts.getCount, 0)
                names = () if charts is None else uno_read(charts.getElementNames, ())
                result["charts"] = list(names or [])
                result["annotation_count"] = uno_read(lambda: sheet.getAnnotations().getCount(), 0)
                result["has_merges"] = uno_read(lambda: sheet.getPropertyValue("HasMergedCells"), None)
                result["shape_count"] = uno_read(lambda: sheet.DrawPage.getCount(), 0)

            return result
        except Exception as e:
            # Re-raise disposal. Wrapping DisposedException as
            # ToolExecutionError hides it from get_calc_context_for_chat and
            # from get_sheet_summary (execute_safe then reports
            # TOOL_EXECUTION_ERROR).
            if is_disposed_exception(e):
                raise
            raise ToolExecutionError(str(e)) from e


@main_thread_only
def get_calc_context_for_chat(
    model: Any,
    max_context: int = CHAT_DOCUMENT_CONTEXT_MAX_CHARS,
    ctx: Any = None,
) -> str:
    """Get context summary for a Calc spreadsheet.

    ctx is required and unused.
    """
    if ctx is None:
        raise ValueError("ctx is required for get_calc_context_for_chat")
    try:
        check_disposed(model, "Document Model")
        vs = _resolve_visible_sheet(model)

        ctx_str = f"Spreadsheet Document: {model.getURL() or 'Untitled'}\n"
        ctx_str += f"Sheets: {vs.names}\n"

        if vs.target is None or vs.summary is None:
            return truncate_chat_context(ctx_str + "No user-visible sheets.\n", max_context)

        summary = vs.summary
        ctx_str += f"Active Sheet: {summary['sheet_name']}\n"
        ctx_str += f"Used Range: {summary['used_range']} ({summary['row_count']} rows x {summary['col_count']} columns)\n"
        ctx_str += f"Columns: {_format_columns(summary['headers'])}\n"

        # Add selection context if available
        if vs.active_is_visible:
            try:
                controller = safe_call(model.getCurrentController, "Get current controller")
                selection = safe_call(controller.getSelection, "Get selection") if controller is not None else None
                if selection:
                    if hasattr(selection, "getRangeAddress"):
                        addr = safe_call(selection.getRangeAddress, "Get range address")
                        if addr is not None:
                            sel_range = format_range(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow, collapse_single=False)
                            ctx_str += f"Current Selection: {sel_range}\n"

                            # Check for selected values if small
                            if (addr.EndRow - addr.StartRow + 1) * (addr.EndColumn - addr.StartColumn + 1) < 100:
                                from plugin.calc.inspector import CellInspector

                                inspector = CellInspector(vs.bridge)
                                # LLM-facing context: enrich dates like read_cell_range (#374 Bug 3).
                                cells = inspector.read_range(sel_range, include_format_info=True)
                                ctx_str += "Selection Content (CSV-like):\n"
                                for row in cells:
                                    ctx_str += ", ".join([str(c["value"]) if c["value"] is not None else "" for c in row]) + "\n"
            except DocumentDisposedError:
                raise
            except Exception as exc:
                if is_real_disposal(exc):
                    raise DocumentDisposedError("Calc document disposed during selection read", object_type="calc") from exc
                log.debug("Failed to read Calc selection context: %s", exc)
        else:
            ctx_str += "Selection omitted: active tab is an internal sheet.\n"

        return truncate_chat_context(ctx_str, max_context)
    except DocumentDisposedError:
        raise
    except Exception as exc:
        if is_real_disposal(exc):
            raise DocumentDisposedError("Calc document disposed", object_type="calc") from exc
        log.exception("get_calc_context_for_chat error: %s", exc)
        return "[Unable to read Calc spreadsheet context. The document may be locked or initializing.]"


@main_thread_only
def get_full_calc_text(model: Any, max_chars: int = CHAT_DOCUMENT_CONTEXT_MAX_CHARS) -> str:
    """Short active-sheet summary for ``get_full_document_text`` (not the chat excerpt).

    Kept next to ``get_calc_context_for_chat`` so ``document_helpers`` can lazy-import
    this module instead of constructing ``SheetAnalyzer`` itself. ``max_chars`` is
    this prefix cap.
    """
    check_disposed(model, "Document Model")
    vs = _resolve_visible_sheet(model)
    if vs.target is None or vs.summary is None:
        return truncate_chat_context("No user-visible sheets.\n", max_chars)
    summary = vs.summary
    text = f"Sheet: {summary['sheet_name']}\nUsed Range: {summary['used_range']}\n"
    text += f"Columns: {_format_columns(summary['headers'])}\n"
    return truncate_chat_context(text, max_chars)

