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
"""Gateway tool to delegate tasks to specialized Writer toolsets."""

import logging

from plugin.framework.specialized_base import DelegateToSpecializedBase
from plugin.modules.writer.base import ToolWriterSpecialBase

log = logging.getLogger("writeragent.writer")


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
