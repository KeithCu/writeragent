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
"""Gateway tool to delegate tasks to specialized Calc toolsets."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Type

from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.calc.base import ToolCalcSpecialBase
from plugin.framework.constants import CALC_SUBAGENT_TOOL
from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT

if TYPE_CHECKING:
    from plugin.framework.tool import ToolBase


class DelegateToSpecializedCalc(DelegateToSpecializedBase):
    """Gateway tool to delegate tasks to specialized Calc toolsets.

    This spins up a sub-agent with a limited set of tools (e.g., only Table tools)
    to focus on the user's specific request, preventing context pollution.
    """

    name: str | None = CALC_SUBAGENT_TOOL
    description: str = (
        f"Starts a Calc sub-agent that has the chosen domain's tools. It carries out task, written in plain language, "
        f"in this spreadsheet and returns a summary. document_research {DELEGATION_USER_FILE_DATA_HINT}; "
        f"web_research {DELEGATION_PUBLIC_WEB_HINT}; vision runs OCR on the currently selected image and inserts the "
        f"recognized text below it (when configured). Also: images, sheets, ranges, search, charts, shapes, "
        f"pivot_tables, cell_comments, conditional_formatting, errors, tracking, forms."
    )

    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    _special_base_class: ClassVar[Type[ToolBase]] = ToolCalcSpecialBase  # type: ignore[type-abstract]
    _agent_label: ClassVar[str] = "Calc"
