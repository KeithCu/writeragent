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

from plugin.framework.tool_base import ToolBase


class ToolCalcSpecialBase(ToolBase):
    """Base class for all specialized Calc tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain (e.g., 'images').
    """

    tier = "specialized"
    specialized_domain: ClassVar[str | None] = None


# --- Domain-Specific Base Classes ---

class ToolCalcImageBase(ToolCalcSpecialBase):
    specialized_domain = "images"
    intent = "media"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcWebResearchBase(ToolCalcSpecialBase):
    specialized_domain = "web_research"


class ToolCalcCommentBase(ToolCalcSpecialBase):
    specialized_domain = "comments"
    intent = "review"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcConditionalBase(ToolCalcSpecialBase):
    specialized_domain = "conditional_formatting"
    intent = "edit"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcSheetFilterBase(ToolCalcSpecialBase):
    specialized_domain = "sheet_filter"
    intent = "edit"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcSheetBase(ToolCalcSpecialBase):
    specialized_domain = "sheets"
    intent = "edit"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcPivotBase(ToolCalcSpecialBase):
    specialized_domain = "pivot_tables"
    intent = "analyze"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcAnalysisBase(ToolCalcSpecialBase):
    specialized_domain = "analysis"
    intent = "analyze"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]


class ToolCalcSpecialTracking(ToolCalcSpecialBase):
    """Track changes (shared tool classes with Writer via multiple inheritance)."""

    specialized_domain: ClassVar[str | None] = "tracking"
    intent = "review"
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
