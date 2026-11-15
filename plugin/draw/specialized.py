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

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Type

from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.draw.base import DRAW_AND_IMPRESS, ToolDrawSpecialBase
from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT

if TYPE_CHECKING:
    from plugin.framework.tool import ToolBase


def _format_draw_delegate_description(domains: list[str]) -> str:
    """Build the Draw/Impress delegate tool description listing the exact bound domains."""
    domain_list = ", ".join(domains)
    return (
        f"Delegates a specialized Draw task. document_research {DELEGATION_USER_FILE_DATA_HINT}; "
        f"web_research {DELEGATION_PUBLIC_WEB_HINT}. "
        f"Available domains: {domain_list}."
    )


class DelegateToSpecializedDraw(DelegateToSpecializedBase):
    """Gateway tool to delegate tasks to specialized Draw toolsets.

    This spins up a sub-agent with a limited set of tools to focus on the
    user's specific request, preventing context pollution.
    """

    name: str | None = "delegate_to_specialized_draw_toolset"
    description: str = (
        f"Delegates a specialized Draw task. document_research {DELEGATION_USER_FILE_DATA_HINT}; "
        f"web_research {DELEGATION_PUBLIC_WEB_HINT}. "
        "Available domains: charts, document_research, forms, images, page_comments, python, shapes, slide_masters, tables, vision, web_research, "
        "and (Impress only) headers_footers, slide_layouts, slide_transitions, speaker_notes."
    )

    uno_services: list[str] | None = DRAW_AND_IMPRESS
    _special_base_class: ClassVar[Type[ToolBase]] = ToolDrawSpecialBase  # type: ignore[type-abstract]
    _agent_label: ClassVar[str] = "Draw"

    def description_for_services(self, uno_services: frozenset[str]) -> str:
        """Return the delegate description listing only the domains bound for *uno_services*."""
        return _format_draw_delegate_description(self.domains_for_services(uno_services))
