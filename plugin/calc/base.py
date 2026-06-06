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

from plugin.framework.constants import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT
from plugin.framework.tool import ToolBase


class ToolCalcSpecialBase(ToolBase):
    """Base class for all specialized Calc tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain (e.g., 'images').
    """

    tier = "specialized"
    specialized_domain: ClassVar[str | None] = None
    specialized_domain_description: ClassVar[str | None] = None
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_sheet_summary", "read_cell_range"])
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


# --- Domain-Specific Base Classes ---


class ToolCalcImageBase(ToolCalcSpecialBase):
    specialized_domain = "images"
    specialized_domain_description: ClassVar[str | None] = (
        "Image manipulation and insertion in spreadsheets; list_nearby_image_files for folder discovery, list_images for in-sheet graphics."
    )
    intent = "media"


class ToolCalcWebResearchBase(ToolCalcSpecialBase):
    specialized_domain = "web_research"
    specialized_domain_description: ClassVar[str | None] = DELEGATION_PUBLIC_WEB_HINT


class ToolCalcDocumentResearchBase(ToolCalcSpecialBase):
    specialized_domain = "document_research"
    specialized_domain_description: ClassVar[str | None] = f"{DELEGATION_USER_FILE_DATA_HINT}; one delegation for file(s), matching descriptions"


class ToolCalcCommentBase(ToolCalcSpecialBase):
    specialized_domain = "comments"
    specialized_domain_description: ClassVar[str | None] = "View, add, and manage cell comments and feedback."
    intent = "review"


class ToolCalcConditionalBase(ToolCalcSpecialBase):
    specialized_domain = "conditional_formatting"
    specialized_domain_description: ClassVar[str | None] = "Apply rules to format cells based on their values."
    intent = "edit"


class ToolCalcSheetBase(ToolCalcSpecialBase):
    """Base for sheet operations and sheet filtering (AutoFilter)."""
    specialized_domain = "sheets"
    specialized_domain_description: ClassVar[str | None] = "List, switch, protect, rename, and delete sheets; apply/clear AutoFilter operations."
    intent = "edit"


class ToolCalcPivotBase(ToolCalcSpecialBase):
    specialized_domain = "pivot_tables"
    specialized_domain_description: ClassVar[str | None] = "Create and manage data pivot tables for analysis."
    intent = "analyze"


class ToolCalcChartBase(ToolCalcSpecialBase):
    """Charts domain (``manage_charts``); shared implementation also serves Writer/Draw via union ``uno_services`` on the concrete tool."""

    specialized_domain = "charts"
    specialized_domain_description: ClassVar[str | None] = (
        "Create and edit charts on the active sheet or embedded chart in the document."
    )
    intent = "edit"


class ToolCalcRangeBase(ToolCalcSpecialBase):
    specialized_domain = "ranges"
    specialized_domain_description: ClassVar[str | None] = "Bulk operations on cell ranges (sort, advanced find/replace)."
    intent = "edit"


class ToolCalcSearchBase(ToolCalcSpecialBase):
    specialized_domain = "search"
    specialized_domain_description: ClassVar[str | None] = "Search for text or values or replace across the entire spreadsheet."
    intent = "navigate"


class ToolCalcAnalysisBase(ToolCalcSpecialBase):
    specialized_domain = "analysis"
    specialized_domain_description: ClassVar[str | None] = (
        "Data analysis and spreadsheet optimization: trusted numpy/pandas helpers (analyze_data), "
        "Goal Seek (calc_goal_seek), and Solver (calc_solver)."
    )
    intent = "analyze"


class ToolCalcErrorBase(ToolCalcSpecialBase):
    specialized_domain = "errors"
    specialized_domain_description: ClassVar[str | None] = "Find, diagnose, and suggest fixes for formula errors (e.g. #REF!, #DIV/0!)."
    intent = "edit"


class ToolCalcSpecialTracking(ToolCalcSpecialBase):
    """Track changes (shared tool classes with Writer via multiple inheritance)."""
    specialized_domain: ClassVar[str | None] = "tracking"
    specialized_domain_description: ClassVar[str | None] = "Manage and review tracked changes in the spreadsheet."
    intent = "review"


class ToolCalcPythonBase(ToolCalcSpecialBase):
    """External venv Python (numpy/pandas stack); marker for delegation prompts."""

    specialized_domain: ClassVar[str | None] = "python"
    specialized_domain_description: ClassVar[str | None] = (
        "Run Python in the user-configured venv (subprocess). Assign output to variable `result` for JSON return."
    )
    intent = "analyze"
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset()
