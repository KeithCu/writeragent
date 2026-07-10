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
"""Gateway tool to delegate tasks to specialized Draw toolsets."""

import logging

from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.draw.base import ToolDrawSpecialBase
from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT

log = logging.getLogger("writeragent.draw")


class DelegateToSpecializedDraw(DelegateToSpecializedBase):
    """Gateway tool to delegate tasks to specialized Draw toolsets.

    This spins up a sub-agent with a limited set of tools to focus on the
    user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_draw_toolset"
    description = (
        f"Delegates a specialized Draw task. document_research {DELEGATION_USER_FILE_DATA_HINT}; "
        f"web_research {DELEGATION_PUBLIC_WEB_HINT}. "
        "Also: shapes, charts, forms, math, slide transitions, slide masters, etc."
    )

    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    _special_base_class = ToolDrawSpecialBase
    _agent_label = "Draw"
