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
"""Base classes for specialized Draw toolsets."""

from typing import ClassVar

from plugin.framework.tool import ToolBase


class ToolDrawSpecialBase(ToolBase):
    """Base class for all specialized Draw tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain.
    """

    tier = "specialized"
    specialized_domain: ClassVar[str | None] = None
    specialized_domain_description: ClassVar[str | None] = None


# --- Domain-Specific Base Classes ---


class ToolDrawWebResearchBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "web_research"
    specialized_domain_description: ClassVar[str | None] = "Search the web for information to help with the drawing or presentation."


class ToolDrawChartBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "charts"
    specialized_domain_description: ClassVar[str | None] = "Create and edit data charts within the drawing or presentation."
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawShapeBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "shapes"
    specialized_domain_description: ClassVar[str | None] = "Create and edit drawing shapes, connectors, and groups."
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawFormBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "forms"
    specialized_domain_description: ClassVar[str | None] = "Create and manage form templates and UI controls."
    intent = "edit"
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawHeaderFooterBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "headers_footers"
    specialized_domain_description: ClassVar[str | None] = "Manage presentation-wide headers, footers, and page numbers."
    uno_services = ["com.sun.star.presentation.PresentationDocument"]
