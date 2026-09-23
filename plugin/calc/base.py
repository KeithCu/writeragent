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
"""Base classes for specialized Calc toolsets."""

from typing import ClassVar

from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT
from plugin.framework.tool import ToolBase


class ToolCalcSpecialBase(ToolBase):
    """Base class for all specialized Calc tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain (e.g., 'images').
    """

    tier: str = "specialized"
    specialized_domain: ClassVar[str | None] = None
    specialized_domain_description: ClassVar[str | None] = None
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_sheet_summary", "read_cell_range"])
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]


# --- Domain-Specific Base Classes ---


class ToolCalcImageBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "images"
    specialized_domain_description: ClassVar[str | None] = (
        "Image manipulation and insertion in spreadsheets; image_list_nearby_files for folder discovery, "
        "image_list for in-sheet graphics; edit a selected image with image_generate(source_image='selection')."
    )
    intent: str | None = "media"


class ToolCalcVisionBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "vision"
    specialized_domain_description: ClassVar[str | None] = (
        "Extract text and structure (layout, tables) from embedded sheet graphics; extract_structure_from_image."
    )
    intent: str | None = "media"
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset()


class ToolCalcWebResearchBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "web_research"
    specialized_domain_description: ClassVar[str | None] = DELEGATION_PUBLIC_WEB_HINT


class ToolCalcDocumentResearchBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_domain_description: ClassVar[str | None] = f"{DELEGATION_USER_FILE_DATA_HINT}; one delegation for file(s), matching descriptions"


class ToolCalcCommentBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "comments"
    specialized_domain_description: ClassVar[str | None] = "View, add, and manage cell comments and feedback."
    intent: str | None = "review"


class ToolCalcConditionalBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "conditional_formatting"
    specialized_domain_description: ClassVar[str | None] = "Apply rules to format cells based on their values."
    intent: str | None = "edit"


class ToolCalcSheetBase(ToolCalcSpecialBase):
    """Base for sheet operations and sheet filtering (AutoFilter)."""
    specialized_domain: ClassVar[str | None] = "sheets"
    specialized_domain_description: ClassVar[str | None] = "Create, list, switch, protect, rename, and delete sheets; apply/clear AutoFilter operations."
    intent: str | None = "edit"


class ToolCalcPivotBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "pivot_tables"
    specialized_domain_description: ClassVar[str | None] = "Create and manage data pivot tables for analysis."
    intent: str | None = "analyze"


class ToolCalcChartBase(ToolCalcSpecialBase):
    """Charts domain (``manage_charts``); shared implementation also serves Writer/Draw via union ``uno_services`` on the concrete tool."""

    specialized_domain: ClassVar[str | None] = "charts"
    specialized_domain_description: ClassVar[str | None] = (
        "Create and edit charts on the active sheet or embedded chart in the document."
    )
    intent: str | None = "edit"


class ToolCalcShapeBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "shapes"
    specialized_domain_description: ClassVar[str | None] = "Create and edit drawing shapes, connectors, and groups."


class ToolCalcRangeBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "ranges"
    specialized_domain_description: ClassVar[str | None] = "Bulk operations on cell ranges (sort, advanced find/replace)."
    intent: str | None = "edit"


class ToolCalcSearchBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "search"
    specialized_domain_description: ClassVar[str | None] = "Search for text or values or replace across the entire spreadsheet."
    intent: str | None = "navigate"


class ToolCalcAnalysisBase(ToolCalcSpecialBase):
    # Not a chat/MCP domain (tools are ToolBaseDummy). Keep the class for tests that
    # register a local tool with specialized_domain = "analysis".
    specialized_domain: ClassVar[str | None] = None
    specialized_domain_description: ClassVar[str | None] = None
    intent: str | None = "analyze"
    # Deliberately do NOT include "read_cell_range" here (unlike the general Calc special base).
    # Analysis sub-agents must use data_range (A1 address strings) with analyze_data / run_venv_python_script.
    # This keeps large data out-of-band: the host resolves the address on the main thread and hands
    # shaped data to the venv via the optimized split_grid/payload_codec path. Passing full values
    # through read_cell_range would materialize them into the sub-agent's observations / LLM context.
    # get_sheet_summary provides cheap structural discovery (used range, headers, counts) without values.
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_sheet_summary"])


class ToolCalcErrorBase(ToolCalcSpecialBase):
    specialized_domain: ClassVar[str | None] = "errors"
    specialized_domain_description: ClassVar[str | None] = "Find, diagnose, and suggest fixes for formula errors (e.g. #REF!, #DIV/0!)."
    intent: str | None = "edit"


class ToolCalcSpecialTracking(ToolCalcSpecialBase):
    """Track changes (shared tool classes with Writer via multiple inheritance)."""
    specialized_domain: ClassVar[str | None] = "tracking"
    specialized_domain_description: ClassVar[str | None] = "Manage and review tracked changes in the spreadsheet."
    intent: str | None = "review"


class ToolCalcPythonBase(ToolCalcSpecialBase):
    """External venv Python (numpy/pandas stack); marker for delegation prompts."""

    specialized_domain: ClassVar[str | None] = "python"
    specialized_domain_description: ClassVar[str | None] = (
        "Run Python in the user-configured venv (subprocess). Assign output to variable `result` for JSON return."
    )
    intent: str | None = "analyze"
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset()
