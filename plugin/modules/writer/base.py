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
"""Base classes for specialized Writer toolsets."""

from plugin.framework.tool_base import ToolBase


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
    specialized_domain = None


# --- Domain-Specific Base Classes ---

class ToolWriterTableBase(ToolWriterSpecialBase):
    specialized_domain = "tables"

class ToolWriterStyleBase(ToolWriterSpecialBase):
    specialized_domain = "styles"

class ToolWriterLayoutBase(ToolWriterSpecialBase):
    specialized_domain = "layout"

class ToolWriterEmbeddedBase(ToolWriterSpecialBase):
    specialized_domain = "embedded"

class ToolWriterShapeBase(ToolWriterSpecialBase):
    specialized_domain = "shapes"

class ToolWriterChartBase(ToolWriterSpecialBase):
    specialized_domain = "charts"

class ToolWriterIndexBase(ToolWriterSpecialBase):
    specialized_domain = "indexes"

class ToolWriterFieldBase(ToolWriterSpecialBase):
    specialized_domain = "fields"


class ToolWriterBookmarkBase(ToolWriterSpecialBase):
    specialized_domain = "bookmarks"


class SpecializedWorkflowFinished(ToolBase):
    """Tool called by the sub-agent to indicate it has completed its task."""

    name = "specialized_workflow_finished"
    description = "Call this tool when you have successfully completed the specialized task."
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A brief summary of what you accomplished.",
            },
        },
        "required": ["summary"],
    }
    tier = "specialized_control"

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "finished": True, "summary": kwargs.get("summary")}
