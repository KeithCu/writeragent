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

from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT
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
    required_core_tools: ClassVar[frozenset[str] | None] = None


# --- Domain-Specific Base Classes ---


class ToolDrawWebResearchBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "web_research"
    specialized_domain_description: ClassVar[str | None] = DELEGATION_PUBLIC_WEB_HINT


class ToolDrawDocumentResearchBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_domain_description: ClassVar[str | None] = f"{DELEGATION_USER_FILE_DATA_HINT}; one delegation for file(s), sub-agent matches descriptions"


class ToolDrawChartBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "charts"
    specialized_domain_description: ClassVar[str | None] = "Create and edit data charts within the drawing or presentation."
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawShapeBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "shapes"
    specialized_domain_description: ClassVar[str | None] = (
        "Create and edit drawing shapes, connectors, and groups; fill paper-form blanks by name."
    )
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


class ToolDrawSpeakerNotesBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "speaker_notes"
    specialized_domain_description: ClassVar[str | None] = "Read and edit Impress speaker notes per slide."
    uno_services = ["com.sun.star.presentation.PresentationDocument"]


class ToolDrawSlideTransitionsBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "slide_transitions"
    specialized_domain_description: ClassVar[str | None] = "Slide transition effects, timing, and Impress slide layouts."
    uno_services = ["com.sun.star.presentation.PresentationDocument"]


class ToolDrawSlideLayoutBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "slide_layouts"
    specialized_domain_description: ClassVar[str | None] = "Get and set Impress slide layouts by layout name or type."
    uno_services = ["com.sun.star.presentation.PresentationDocument"]


class ToolDrawSlideMastersBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "slide_masters"
    specialized_domain_description: ClassVar[str | None] = "List master slides and assign masters to slides."
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawPythonBase(ToolDrawSpecialBase):
    """Marker for Draw/Impress delegation prompt listing (domain=python); see plugin/calc/python/venv.py."""

    specialized_domain: ClassVar[str | None] = "python"
    specialized_domain_description: ClassVar[str | None] = (
        "Run Python in the user-configured venv (subprocess); isolated from LibreOffice."
    )


class ToolDrawImageBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "images"
    specialized_domain_description: ClassVar[str | None] = (
        "Insert, list, and generate images on Draw/Impress pages (same image_* tools as Writer/Calc); "
        "edit a selected image with image_generate(source_image='selection')."
    )
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawTableBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "tables"
    specialized_domain_description: ClassVar[str | None] = (
        "Insert and edit tables on Draw/Impress pages (same table_* tools as Writer)."
    )
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class ToolDrawPptMasterBase(ToolDrawSpecialBase):
    """PPT-Master sidebar mode — not exposed via delegate_to_specialized_draw_toolset."""

    specialized_domain: ClassVar[str | None] = "ppt-master"
    specialized_domain_description: ClassVar[str | None] = (
        "PPT-Master workflow: export SVG projects to native Impress shapes, validate, template-fill, enhance."
    )
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
