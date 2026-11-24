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

from typing import Any, ClassVar

from plugin.framework.doc_type import DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE
from plugin.framework.prompts import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT
from plugin.framework.tool import ToolBase

DRAW_AND_IMPRESS = [DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE]
_IMPRESS_ONLY = [IMPRESS_DOCUMENT_SERVICE]

# Every Draw/Impress tool whose optional `page` parameter goes through
# DrawBridge.resolve_page shares this schema. When omitted, it falls back
# to the chat's active page, then the view (DrawBridge.active_or_ctx_index).
DRAW_PAGE_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "description": "0-based page (slide) index (the user's page 2 is page=1); the active page if omitted.",
}

# For Draw/Impress tools whose `page` parameter is required.
DRAW_REQUIRED_PAGE_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "description": "0-based page (slide) index: the user's page 2 is page=1.",
}


class ToolDrawSpecialBase(ToolBase):
    """Base class for all specialized Draw tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain.
    """

    tier: str = "specialized"
    is_app_specialized_tool: ClassVar[bool] = True
    specialized_domain: ClassVar[str | None] = None
    specialized_domain_description: ClassVar[str | None] = None


# --- Domain-Specific Base Classes ---


class ToolDrawWebResearchBase(ToolDrawSpecialBase):
    # Sidebar chat only; MCP clients never see this domain.
    tier: str = "chat"
    specialized_domain: ClassVar[str | None] = "web_research"
    specialized_domain_description: ClassVar[str | None] = DELEGATION_PUBLIC_WEB_HINT


class ToolDrawDocumentResearchBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_domain_description: ClassVar[str | None] = f"{DELEGATION_USER_FILE_DATA_HINT}; one delegation for file(s), sub-agent matches descriptions"


class ToolDrawChartBase(ToolDrawSpecialBase):
    """Delegate catalog row only; the tools are the ``plugin.calc.charts`` classes."""

    specialized_domain: ClassVar[str | None] = "charts"
    specialized_domain_description: ClassVar[str | None] = "Create and edit data charts within the drawing or presentation."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawShapeBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "shapes"
    specialized_domain_description: ClassVar[str | None] = "Create and edit drawing shapes, connectors, and groups; fill paper-form blanks by name."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawFormBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "forms"
    specialized_domain_description: ClassVar[str | None] = "Create and manage form templates and UI controls."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawHeaderFooterBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "headers_footers"
    specialized_domain_description: ClassVar[str | None] = "Manage presentation-wide headers, footers, and page numbers."
    uno_services: list[str] | None = _IMPRESS_ONLY


class ToolDrawSpeakerNotesBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "speaker_notes"
    specialized_domain_description: ClassVar[str | None] = "Read and edit Impress speaker notes per slide."
    uno_services: list[str] | None = _IMPRESS_ONLY


class ToolDrawCommentBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "page_comments"
    specialized_domain_description: ClassVar[str | None] = "List, add, and delete comments on Draw and Impress pages."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawSlideTransitionsBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "slide_transitions"
    specialized_domain_description: ClassVar[str | None] = "Slide transition effects and timing."
    uno_services: list[str] | None = _IMPRESS_ONLY


class ToolDrawSlideLayoutBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "slide_layouts"
    specialized_domain_description: ClassVar[str | None] = "Get and set Impress slide layouts by layout name or type."
    uno_services: list[str] | None = _IMPRESS_ONLY


class ToolDrawSlideMastersBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "slide_masters"
    specialized_domain_description: ClassVar[str | None] = "List master slides and assign masters to slides."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawPythonBase(ToolDrawSpecialBase):
    """Marker for Draw/Impress delegation prompt listing (domain=python); see plugin/calc/python/venv.py."""

    specialized_domain: ClassVar[str | None] = "python"
    specialized_domain_description: ClassVar[str | None] = "Run Python in the user-configured venv (subprocess); isolated from LibreOffice."


class ToolDrawImageBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "images"
    specialized_domain_description: ClassVar[str | None] = (
        "Insert, list, generate, and edit images on Draw/Impress pages (same image_* tools as Writer/Calc)."
    )
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawTableBase(ToolDrawSpecialBase):
    specialized_domain: ClassVar[str | None] = "tables"
    specialized_domain_description: ClassVar[str | None] = "Insert and edit tables on Draw/Impress pages (same table_* tools as Writer)."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawMathBase(ToolDrawSpecialBase):
    """Catalog row for math_insert (plugin/draw/math_insert.py)."""

    specialized_domain: ClassVar[str | None] = "math"
    specialized_domain_description: ClassVar[str | None] = "Insert an editable LibreOffice Math formula from LaTeX or MathML on a page."
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawVisionBase(ToolDrawSpecialBase):
    """Catalog marker only: lists the vision domain on Draw/Impress; no tool class derives from this.

    OCR runs through ``ExtractStructureFromImage`` (Calc vision base) and the delegate gateway.
    """

    specialized_domain: ClassVar[str | None] = "vision"
    specialized_domain_description: ClassVar[str | None] = (
        "Runs OCR on the selected picture and returns its text; the page is unchanged. "
        "Use this call for OCR; with no picture selected, ask the user to select one."
    )
    uno_services: list[str] | None = DRAW_AND_IMPRESS


class ToolDrawPptMasterBase(ToolDrawSpecialBase):
    """PPT-Master sidebar mode — not exposed via delegate_to_subagent."""

    # Sidebar mode reaches this by active_domain; MCP cannot list or call it.
    tier: str = "unlisted"
    specialized_domain: ClassVar[str | None] = "ppt-master"
    specialized_domain_description: ClassVar[str | None] = "PPT-Master workflow: export SVG projects to native Impress shapes, validate, template-fill, enhance."
    uno_services: list[str] | None = DRAW_AND_IMPRESS
