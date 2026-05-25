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
"""Specialized Writer toolset infrastructure and delegation."""

import logging
from typing import ClassVar

from plugin.framework.tool import ToolBase
from plugin.calc.base import ToolCalcSpecialBase
from plugin.draw.base import ToolDrawFormBase
from plugin.framework.constants import DELEGATION_PUBLIC_WEB_HINT, DELEGATION_USER_FILE_DATA_HINT, USE_SUB_AGENT
from plugin.doc.specialized_base import DelegateToSpecializedBase

log = logging.getLogger("writeragent.writer")


class ToolWriterSpecialBase(ToolBase):
    """Base class for all specialized Writer tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain (e.g., 'tables', 'charts').
    """

    # Not on the main chat default tool list (tier specialized); exposed via delegation only.
    tier = "specialized"

    # The domain name this tool belongs to (e.g., "tables").
    # Subclasses MUST override this.
    specialized_domain: ClassVar[str | None] = None
    specialized_domain_description: ClassVar[str | None] = None
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_document_content", "get_document_tree"])
    uno_services = ["com.sun.star.text.TextDocument"]


class DelegateToSpecializedWriter(DelegateToSpecializedBase):
    """Gateway tool to delegate tasks to specialized Writer toolsets.

    This spins up a sub-agent with a limited set of tools (e.g., only Table tools)
    to focus on the user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_writer_toolset"
    description = (
        "Delegates a specialized task to a sub-agent with a focused toolset. "
        f"document_research {DELEGATION_USER_FILE_DATA_HINT}; web_research {DELEGATION_PUBLIC_WEB_HINT}. "
        "Also: charts, fields, styles, page, textframes, embedded (active doc OLE only), shapes, indexes, "
        "bookmarks, tracking, footnotes, forms, images."
    )

    uno_services = ["com.sun.star.text.TextDocument"]
    _special_base_class = ToolWriterSpecialBase
    _agent_label = "Writer"


# --- Domain-Specific Base Classes ---


class ToolWriterStyleBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "styles"
    specialized_domain_description: ClassVar[str | None] = "Manage and edit paragraph, character, and list styles."
    required_core_tools: ClassVar[frozenset[str] | None] = (ToolWriterSpecialBase.required_core_tools or frozenset()) | frozenset(["search_in_document"])
    intent = "edit"


class ToolWriterPageBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "page"
    specialized_domain_description: ClassVar[str | None] = "Page layout, margins, columns, headers, footers, and page breaks."
    intent = "edit"


class ToolWriterTextFramesBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "textframes"
    specialized_domain_description: ClassVar[str | None] = "Manage text frames, their content, and positioning."
    intent = "edit"


class ToolWriterEmbeddedBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "embedded"
    specialized_domain_description: ClassVar[str | None] = "OLE in active doc only (not sibling files on disk)."
    intent = "edit"


class ToolWriterImageBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "images"
    specialized_domain_description: ClassVar[str | None] = (
        "In-document image operations (list_images) and nearby folder images (list_nearby_image_files); generate, insert, and replace."
    )
    intent = "media"


class ToolWriterShapeBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "shapes"
    specialized_domain_description: ClassVar[str | None] = "Create and edit drawing shapes, lines, and connectors."


class ToolWriterPythonBase(ToolWriterSpecialBase):
    """Marker for Writer delegation prompt listing (domain=python); see plugin/calc/venv_python.py."""

    specialized_domain: ClassVar[str | None] = "python"
    specialized_domain_description: ClassVar[str | None] = (
        "Run Python / Numpy in the user-configured venv (subprocess)."
    )


class ToolWriterChartBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "charts"
    specialized_domain_description: ClassVar[str | None] = "Create and edit data charts within the document."


class ToolWriterIndexBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "indexes"
    specialized_domain_description: ClassVar[str | None] = "Manage Table of Contents and alphabetical indexes."


class ToolWriterFieldBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "fields"
    specialized_domain_description: ClassVar[str | None] = "Manage document fields, variables, and cross-references."
    required_core_tools: ClassVar[frozenset[str] | None] = (ToolWriterSpecialBase.required_core_tools or frozenset()) | frozenset(["search_in_document"])


class ToolWriterCommentBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "comments"
    specialized_domain_description: ClassVar[str | None] = "View, add, and manage document comments and feedback."
    intent = "review"


class WriterAgentSpecialTracking(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "tracking"
    specialized_domain_description: ClassVar[str | None] = "Manage and review tracked changes (redlines) in the document."
    intent = "review"


class ToolWriterBookmarkBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "bookmarks"
    specialized_domain_description: ClassVar[str | None] = "Manage document bookmarks and navigation points."
    required_core_tools: ClassVar[frozenset[str] | None] = (ToolWriterSpecialBase.required_core_tools or frozenset()) | frozenset(["search_in_document"])
    intent = "navigate"


class ToolWriterStructuralBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "structural"
    specialized_domain_description: ClassVar[str | None] = "Document navigation, headings, and structural summary."
    intent = "navigate"


class ToolWriterFootnoteBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "footnotes"
    specialized_domain_description: ClassVar[str | None] = "Create and manage footnotes and endnotes."
    required_core_tools: ClassVar[frozenset[str] | None] = (ToolWriterSpecialBase.required_core_tools or frozenset()) | frozenset(["search_in_document"])
    intent = "edit"


class ToolWriterFormBase(ToolWriterSpecialBase, ToolCalcSpecialBase, ToolDrawFormBase):
    """Form tools for Writer, Calc, and Draw/Impress (single ``specialized_domain``; union ``uno_services`` on concrete tools)."""

    # Same key on both ToolWriterSpecialBase / ToolCalcSpecialBase; explicit ClassVar for checkers.
    specialized_domain: ClassVar[str | None] = "forms"
    specialized_domain_description: ClassVar[str | None] = "Create and manage form templates and UI controls."
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterWebResearchBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "web_research"
    specialized_domain_description: ClassVar[str | None] = DELEGATION_PUBLIC_WEB_HINT


class ToolWriterDocumentResearchBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_domain_description: ClassVar[str | None] = f"{DELEGATION_USER_FILE_DATA_HINT}; one delegation for file(s), sub-agent matches descriptions"


'''
# Mock domain base classes: uncomment when implementations are ready.
class ToolWriterSectionBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "sections"
    specialized_domain_description: ClassVar[str | None] = "Manage document sections, protection, columns, and properties."
    intent = "edit"


class ToolWriterMailMergeBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "mail_merge"
    specialized_domain_description: ClassVar[str | None] = "Configure and execute mail merge workflows with data sources."
    intent = "edit"


class ToolWriterBibliographyBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "bibliography"
    specialized_domain_description: ClassVar[str | None] = "Manage citations and generate document bibliographies."
    intent = "edit"


class ToolWriterWatermarkBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "watermark"
    specialized_domain_description: ClassVar[str | None] = "Insert, configure, or remove page watermarks and backgrounds."
    intent = "edit"


class ToolWriterAutoTextBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "autotext"
    specialized_domain_description: ClassVar[str | None] = "Insert, list, and manage AutoText quick-insert entries."
    intent = "edit"


class ToolWriterTocEnhancementBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "toc_enhancement"
    specialized_domain_description: ClassVar[str | None] = "Advanced multi-level custom Table of Contents design and enhancement."
    intent = "edit"


class ToolWriterDocumentAutomationBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_automation"
    specialized_domain_description: ClassVar[str | None] = "Run macros, register event bindings, and automate document scripting."
    intent = "edit"


class ToolWriterSecurityBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "security"
    specialized_domain_description: ClassVar[str | None] = "Digital signatures, document encryption, and pattern-based content redaction."
    intent = "review"


class ToolWriterDocumentManagementBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "document_management"
    specialized_domain_description: ClassVar[str | None] = "Read and write document metadata, compare documents, and assemble files."
    intent = "review"


class ToolWriterCollaborationBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "collaboration"
    specialized_domain_description: ClassVar[str | None] = "Manage editing users, custom notifications, and conflict resolution."
    intent = "review"


class ToolWriterCustomizationBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "customization"
    specialized_domain_description: ClassVar[str | None] = "Customize keyboard shortcuts, menu items, and custom commands."
    intent = "edit"
'''


class SpecializedWorkflowFinished(ToolBase):
    """Tool called by the main chat model to indicate it has completed its specialized task.
    This mimics the built-in 'final_answer' tool of smolagents for the in-place switching approach.
    """

    name = "specialized_workflow_finished"
    description = "Provides a final answer to the given task and exits the specialized toolset mode."
    parameters = {"type": "object", "properties": {"answer": {"type": "string", "description": "The final answer to the task."}}, "required": ["answer"]}
    tier = "specialized_control"
    is_final_answer_tool = True

    def execute(self, ctx, **kwargs):
        # Allow the main LLM loop to exit specialized mode
        if not USE_SUB_AGENT:
            callback = getattr(ctx, "set_active_domain_callback", None)
            if callback:
                callback(None)

        return {"status": "ok", "finished": True, "answer": kwargs.get("answer"), "message": "Specialized task complete. Normal toolset restored."}
