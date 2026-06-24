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

import logging

from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.calc.base import ToolCalcSpecialBase
from plugin.framework.constants import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT

log = logging.getLogger("writeragent.calc")


class DelegateToSpecializedCalc(DelegateToSpecializedBase):
    """Gateway tool to delegate tasks to specialized Calc toolsets.

    This spins up a sub-agent with a limited set of tools (e.g., only Table tools)
    to focus on the user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_calc_toolset"
    description = (
        f"Delegates a specialized Calc task. document_research {DELEGATION_USER_FILE_DATA_HINT}; "
        f"web_research {DELEGATION_PUBLIC_WEB_HINT}. "
        "Also: analysis, images, vision (local OCR when venv configured), pivot, sheets, forms, tracking, etc."
    )

    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    _special_base_class = ToolCalcSpecialBase
    _agent_label = "Calc"
