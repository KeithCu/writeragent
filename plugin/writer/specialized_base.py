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
from plugin.framework.constants import USE_SUB_AGENT
from plugin.doc.specialized_base import DelegateToSpecializedBase

log = logging.getLogger("writeragent.writer")


class ToolWriterSpecialBase(ToolBase):
    """Base class for all specialized Writer tools.

    Tools deriving from this base are NOT exposed directly to the main
    agent's general toolset. Instead, they are exposed only to the
    specialized sub-agent when the user delegates a task to that specific
    domain (e.g., 'tables', 'charts').
    """

    # Do not expose to the main agent's default "core" or "extended" tier.
    tier = "specialized"

    # The domain name this tool belongs to (e.g., "tables").
    # Subclasses MUST override this.
    specialized_domain: ClassVar[str | None] = None


class DelegateToSpecializedWriter(DelegateToSpecializedBase):
    """Gateway tool to delegate tasks to specialized Writer toolsets.

    This spins up a sub-agent with a limited set of tools (e.g., only Table tools)
    to focus on the user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_writer_toolset"
    description = (
        "Delegates a specialized task to a sub-agent with a focused toolset. "
        "Use this for specialized complex Writer operations like manipulating "
        "charts, fields, styles (list, edit, create), page (margins, headers/footers, columns, page breaks), "
        "textframes (list_text_frames, get_text_frame_info, set_text_frame_properties), "
        "embedded objects, shapes, indexes, "
        "bookmarks, track changes (tracking), footnotes/endnotes (domain=footnotes), "
        "form templates and controls (domain=forms), "
        "or in-document image work (domain=images: generate, list, insert, replace images, etc.)."
    )

    uno_services = ["com.sun.star.text.TextDocument"]
    _special_base_class = ToolWriterSpecialBase
    _agent_label = "Writer"


# --- Domain-Specific Base Classes ---


class ToolWriterStyleBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "styles"
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterPageBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "page"
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterTextFramesBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "textframes"
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterEmbeddedBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "embedded"
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterImageBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "images"
    intent = "media"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterShapeBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "shapes"


class ToolWriterChartBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "charts"


class ToolWriterIndexBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "indexes"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterFieldBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "fields"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterCommentBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "comments"
    intent = "review"
    uno_services = ["com.sun.star.text.TextDocument"]


class WriterAgentSpecialTracking(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "tracking"
    intent = "review"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterBookmarkBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "bookmarks"
    intent = "navigate"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterStructuralBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "structural"
    intent = "navigate"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterFootnoteBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "footnotes"
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterFormBase(ToolWriterSpecialBase, ToolCalcSpecialBase, ToolDrawFormBase):
    """Form tools for Writer, Calc, and Draw/Impress (single ``specialized_domain``; union ``uno_services`` on concrete tools)."""

    # Same key on both ToolWriterSpecialBase / ToolCalcSpecialBase; explicit ClassVar for checkers.
    specialized_domain: ClassVar[str | None] = "forms"
    intent = "edit"
    uno_services = ["com.sun.star.text.TextDocument"]


class ToolWriterWebResearchBase(ToolWriterSpecialBase):
    specialized_domain: ClassVar[str | None] = "web_research"


class SpecializedWorkflowFinished(ToolBase):
    """Tool called by the main chat model to indicate it has completed its specialized task.
    This mimics the built-in 'final_answer' tool of smolagents for the in-place switching approach.
    """

    name = "specialized_workflow_finished"
    description = "Provides a final answer to the given task and exits the specialized toolset mode."
    parameters = {"type": "object", "properties": {"answer": {"type": "string", "description": "The final answer to the task."}}, "required": ["answer"]}
    tier = "specialized_control"

    def execute(self, ctx, **kwargs):
        # Allow the main LLM loop to exit specialized mode
        if not USE_SUB_AGENT:
            callback = getattr(ctx, "set_active_domain_callback", None)
            if callback:
                callback(None)

        return {"status": "ok", "finished": True, "answer": kwargs.get("answer"), "message": "Specialized task complete. Normal toolset restored."}
