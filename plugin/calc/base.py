# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""Base classes for specialized Calc toolsets: the Calc delegate's domain catalog.

Each direct subclass of ``ToolCalcSpecialBase`` is one row of the Calc
delegate's domain list: ``specialized_catalog.catalog_entries_from_base`` walks
``ToolCalcSpecialBase.__subclasses__()`` (direct subclasses only) and
``DelegateToSubagent.domains_for_services`` calls it. A class
here owns its row's domain and description, ``tier``,
``required_core_tools`` and ``uno_services``; it owns no tool behaviour.
One row is defined elsewhere: ``ToolWriterFormBase`` (forms) in
``plugin/writer/specialized_base.py``. Three rows have no tool subclass in
this package: ``images`` is served by the shared ``ToolWriterImageBase``
tools (``plugin/writer/images/images.py``, which also run on Calc),
``web_research`` is run by the delegate gateway itself
(``plugin/doc/specialized_base.py``), and ``document_research`` by the
``plugin/doc/document_research_*`` tools that declare that domain.
"""

from typing import ClassVar

from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT
from plugin.framework.tool import ToolSpecialBase

# Params using these notes resolve through CalcBridge.resolve_range_or_address /
# resolve_cell / resolve, which accept Calc '.', Excel '!', quoted names, and '$',
# and never activate the target sheet. The note shows only the Calc form because
# model-facing text teaches Calc syntax only. insert_html is excluded because
# ScTabViewObj::select switches tabs when the target is on another sheet.
CALC_SHEET_PREFIX_NOTE: str = (
    "A sheet prefix (Sheet2.A1, 'Data Sheet'.B2) targets that sheet without switching the active sheet."
)
CALC_RANGE_LIST_NOTE: str = f"One entry per area: A1, A1:D10 or a named range. {CALC_SHEET_PREFIX_NOTE}"


class ToolCalcSpecialBase(ToolSpecialBase):
    """Base class for all specialized Calc tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain (e.g., 'images').
    """

    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_sheet_summary", "read_range"])
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]


# --- Domain-Specific Base Classes ---


class ToolCalcImageBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "images"
    specialized_domain_description: ClassVar[str | None] = (
        "Image manipulation and insertion in spreadsheets; image_list_nearby_files for folder discovery, "
        "image_list for in-sheet graphics; generate or edit images."
    )


class ToolCalcVisionBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "vision"
    specialized_domain_description: ClassVar[str | None] = (
        "Runs OCR on the currently selected image and inserts the recognized text, with its layout and tables, below it in the sheet. Use this call for OCR."
    )
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset()


class ToolCalcWebResearchBase(ToolCalcSpecialBase):
    # Sidebar chat only; MCP clients never see this domain.
    tier: str = "chat"
    specialized_domain: ClassVar[str | None] = "web_research"
    specialized_domain_description: ClassVar[str | None] = DELEGATION_PUBLIC_WEB_HINT


class ToolCalcDocumentResearchBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_domain_description: ClassVar[str | None] = f"{DELEGATION_USER_FILE_DATA_HINT}; one delegation for file(s), matching descriptions"


class ToolCalcCommentBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "cell_comments"
    specialized_domain_description: ClassVar[str | None] = "View, add, and manage cell comments and feedback."


class ToolCalcConditionalBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "conditional_formatting"
    specialized_domain_description: ClassVar[str | None] = "Apply rules to format cells based on their values."
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(
        {"get_sheet_summary", "read_range", "style_list"}
    )


class ToolCalcSheetBase(ToolCalcSpecialBase):
    """Base for sheet operations and sheet filtering (AutoFilter)."""

    specialized_domain: ClassVar[str | None] = "sheets"
    specialized_domain_description: ClassVar[str | None] = (
        "Create, list, switch, copy, move, hide, protect, rename, and delete sheets; apply/clear AutoFilter operations."
    )


class ToolCalcPivotBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "pivot_tables"
    specialized_domain_description: ClassVar[str | None] = "Create and manage data pivot tables for analysis."


class ToolCalcChartBase(ToolCalcSpecialBase):
    """Charts domain (``chart_list``, ``chart_get_info``, ``chart_upsert``, ``chart_delete``);
    shared implementation also serves Writer/Draw via union ``uno_services`` on the concrete tool."""

    specialized_domain: ClassVar[str | None] = "charts"
    specialized_domain_description: ClassVar[str | None] = "Create and edit charts on the active sheet or embedded chart in the document."


class ToolCalcShapeBase(ToolCalcSpecialBase):
    """Delegate catalog row only; the tools are the ``plugin.draw.shapes`` classes, which take this class's ``required_core_tools``."""

    specialized_domain: ClassVar[str | None] = "shapes"
    specialized_domain_description: ClassVar[str | None] = (
        "Create and edit drawing shapes, connectors, groups, align/distribute layouts, and diagrams."
    )


class ToolCalcRangeBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "ranges"
    specialized_domain_description: ClassVar[str | None] = "Sort a range and create, edit, list, or delete named ranges."


class ToolCalcSearchBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "search"
    specialized_domain_description: ClassVar[str | None] = "Search for text or values or replace across the entire spreadsheet."


class ToolCalcErrorBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "errors"
    specialized_domain_description: ClassVar[str | None] = "Find, diagnose, and suggest fixes for formula errors (e.g. #REF!, #DIV/0!)."


class ToolCalcSpecialTracking(ToolCalcSpecialBase):
    """Track changes (shared tool classes with Writer via multiple inheritance)."""

    specialized_domain: ClassVar[str | None] = "tracking"
    specialized_domain_description: ClassVar[str | None] = "Manage and review tracked changes in the spreadsheet."


class ToolCalcPythonSqlBase(ToolCalcSpecialBase):
    """Read-only DuckDB SQL (sql_query_folder). Direct subclass so the discovery catalog lists it.

    Calc chat hides this domain (CALC_HIDDEN_SPECIALIZED_DOMAINS), same as python.
    tool_find uses for_discovery and still offers it. Only get_sheet_summary
    is required (no read_range), keeping large data out-of-band through
    data_range and split_grid/payload_codec instead of sub-agent observations.
    """

    specialized_domain: ClassVar[str | None] = "python/sql"
    specialized_domain_description: ClassVar[str | None] = (
        "Run read-only SQL (DuckDB) against folder files and live Calc ranges; sql_query_folder."
    )
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_sheet_summary"])


class ToolCalcPythonBase(ToolCalcSpecialBase):
    """External venv Python (numpy/pandas stack); marker for delegation prompts."""

    specialized_domain: ClassVar[str | None] = "python"
    specialized_domain_description: ClassVar[str | None] = "Run Python in the user-configured venv (subprocess). Assign output to variable `result` for JSON return."
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset()
