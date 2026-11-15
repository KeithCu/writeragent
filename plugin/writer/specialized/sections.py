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
"""Writer section tools (sections domain, specialized tier).

Read-only section inspection (`section_list`, `section_read`) lives in
``plugin.writer.structural``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from plugin.doc.undo import commit_or_rollback_mutation
from plugin.doc.visual_helpers import mm_to_unit
from plugin.framework.errors import ToolExecutionError

from ..edit_review import flag_style_unreviewed
from ..page import apply_column_layout
from ..specialized_base import ToolWriterSectionBase, index_error
from ..target_resolver import resolve_target_cursor

if TYPE_CHECKING:
    from collections.abc import Callable
    from plugin.framework.tool import ToolContext

# Matches LibreOffice Writer's UI limit (sw/source/uibase/inc/column.hxx: nMaxCols = 99).
_MAX_COLUMNS = 99


def _section_or_error(tool: ToolWriterSectionBase, ctx: ToolContext, name: str | None) -> Any:
    """Resolve a named section or return a structured error payload."""
    if not name:
        return tool._tool_error("name is required.")
    section = tool.get_item(
        ctx.doc,
        "getTextSections",
        name,
        not_found_msg="Section '%s' not found." % name,
        list_tool="section_list",
    )
    if isinstance(section, dict):
        return section

    # unosect.cxx WID_SECT_DOCUMENT_INDEX is set on index sections and their _Head children.
    index = section.getPropertyValue("DocumentIndex")
    if index is not None:
        return tool._tool_error(
            f"Section '{name}' belongs to index {index!r}; use the indexes domain.",
            code="INDEX_SECTION",
        )

    # uiregionsw.cxx:SwEditRegionDlg::CheckPasswd guards protection, removal, and columns;
    # UNO itself does not enforce ProtectionKey, so refuse here rather than bypassing it.
    if len(section.getPropertyValue("ProtectionKey")):
        return tool._tool_error(
            f"Section '{name}' has a password; change it in Format > Sections.",
            code="SECTION_PASSWORD_PROTECTED",
        )

    return section


def _paragraph_span(
    tool: ToolWriterSectionBase,
    ctx: ToolContext,
    locator: str,
    end_locator: str | None,
) -> Any:
    """Resolve locator(s) to a text cursor widened to whole paragraphs."""
    if locator == "selection":
        if end_locator:
            return tool._tool_error("end_locator is not allowed when locator='selection'.")
        try:
            cur = resolve_target_cursor(ctx, "selection", None)
        except ValueError:
            return tool._tool_error(
                "Could not read the current selection; pass locator='paragraph:N' (and end_locator)."
            )
        start, end = cur.getStart(), cur.getEnd()
    else:
        svc = ctx.services.document
        try:
            first = svc.resolve_locator(ctx.doc, locator)["para_index"]
            last = svc.resolve_locator(ctx.doc, end_locator or locator)["para_index"]
        except ToolExecutionError as e:
            return tool._tool_error(str(e))

        paras = svc.get_paragraph_ranges(ctx.doc)
        for idx in (first, last):
            err = index_error(idx, len(paras), "paragraph")
            if err is not None:
                return tool._tool_error(err)
        if last < first:
            return tool._tool_error(
                f"end_locator paragraph {last} is before locator paragraph {first}."
            )
        if not all(paras[i].supportsService("com.sun.star.text.Paragraph") for i in (first, last)):
            return tool._tool_error(
                "Locator points at a table; use the paragraph before or after it."
            )
        start, end = paras[first].getStart(), paras[last].getEnd()

    # Widening to whole paragraphs stops SwDoc::InsertSwSection from splitting
    # paragraphs at a mid-paragraph range (ndsect.cxx SplitNode).
    cursor = start.getText().createTextCursorByRange(start)
    cursor.gotoStartOfParagraph(False)
    cursor.gotoRange(end, True)
    cursor.gotoEndOfParagraph(True)
    return cursor


def _commit(
    tool: ToolWriterSectionBase,
    ctx: ToolContext,
    title: str,
    what: str,
    code: str,
    mutate: Callable[[], None],
    result: dict[str, Any] | Callable[[], dict[str, Any]],
    *,
    undo_unavailable_msg: str | None = None,
    rollback_msg: str | None = None,
) -> dict[str, Any]:
    err = commit_or_rollback_mutation(
        tool,
        ctx.doc,
        title,
        mutate,
        undo_unavailable_msg or f"Undo is unavailable; could not {what}.",
        rollback_msg or f"Failed to {what}; the change was rolled back",
        code=code,
    )
    if err is not None:
        return err
    payload = result if isinstance(result, dict) else result()
    flag_style_unreviewed(payload)
    return payload


class SectionCreate(ToolWriterSectionBase):
    """Create a named text section wrapping whole paragraphs."""

    name: str | None = "section_create"
    description: str = "Create a named text section wrapping whole paragraphs."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique name for the new section."},
            "locator": {
                "type": "string",
                "description": (
                    "First paragraph, e.g. paragraph:12, heading_text:Methods, "
                    "bookmark:NAME, or the literal selection for the current selection."
                ),
            },
            "end_locator": {
                "type": "string",
                "description": (
                    "Last paragraph, inclusive; defaults to locator; not allowed with selection."
                ),
            },
        },
        "required": ["name", "locator"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name = kwargs.get("name")
        if not name:
            return self._tool_error("name is required.")

        doc = ctx.doc
        sections = self.get_collection(doc, "getTextSections", "Document does not support text sections.")
        if isinstance(sections, dict):
            return sections

        # SwXTextSection::attach -> GetUniqueSectionName would otherwise rename duplicates silently to SectionN.
        if sections.hasByName(name):
            return self._tool_error(
                f"Section '{name}' already exists.",
                code="INVALID_PARAM",
                parameter="name",
            )

        cursor = _paragraph_span(self, ctx, kwargs["locator"], kwargs.get("end_locator"))
        if isinstance(cursor, dict):
            return cursor

        enclosing = cursor.getPropertyValue("TextSection")
        if enclosing is not None and enclosing.getPropertyValue("DocumentIndex") is not None:
            return self._tool_error(
                f"Target range is inside index section '{enclosing.getName()}'; use the indexes domain.",
                code="INDEX_SECTION",
            )

        section = doc.createInstance("com.sun.star.text.TextSection")
        section.setName(name)

        # With bAbsorb=True the whole range is attached and no text is deleted (unotext.cxx:SwXText::insertTextContent).
        def mutate() -> None:
            cursor.getText().insertTextContent(cursor, section, True)

        return _commit(
            self,
            ctx,
            "WriterAgent: Create section",
            "create the section",
            "CREATE_SECTION_FAILED",
            mutate,
            lambda: {"status": "ok", "section_name": section.getName()},
            undo_unavailable_msg="Undo is unavailable; no section was created.",
            rollback_msg="Failed to create the section (a range that partly overlaps another section is refused); the change was rolled back",
        )


class SectionDelete(ToolWriterSectionBase):
    """Delete a named text section without removing its paragraphs."""

    name: str | None = "section_delete"
    description: str = "Remove the section but keep its text; sections nested inside it stay."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the section to delete."},
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name: str = kwargs["name"]
        section = _section_or_error(self, ctx, name)
        if isinstance(section, dict):
            return section

        # removeTextContent disposes the section and calls DelSectionFormat with bDelNodes=false, so paragraphs stay.
        def mutate() -> None:
            section.getAnchor().getText().removeTextContent(section)

        return _commit(
            self,
            ctx,
            "WriterAgent: Delete section",
            "delete the section",
            "DELETE_SECTION_FAILED",
            mutate,
            {"status": "ok", "section_name": name, "deleted": True},
        )


class SectionSetColumns(ToolWriterSectionBase):
    """Set the number of columns and column spacing for a text section."""

    name: str | None = "section_set_columns"
    description: str = "Set the number of columns and column spacing (in mm) for a text section."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the section."},
            "column_count": {"type": "integer", "description": "Number of columns (1-99)."},
            "spacing_mm": {"type": "number", "description": "Spacing between columns in mm. Defaults to 0."},
        },
        "required": ["name", "column_count"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        column_count: int = kwargs["column_count"]
        if not (1 <= column_count <= _MAX_COLUMNS):
            return self._tool_error(
                f"column_count must be between 1 and {_MAX_COLUMNS}.",
                code="INVALID_PARAM",
                parameter="column_count",
            )

        spacing_mm = kwargs.get("spacing_mm", 0)
        if not (math.isfinite(spacing_mm) and spacing_mm >= 0):
            return self._tool_error(
                "spacing_mm must be a finite number >= 0.",
                code="INVALID_PARAM",
                parameter="spacing_mm",
            )

        name: str = kwargs["name"]
        section = _section_or_error(self, ctx, name)
        if isinstance(section, dict):
            return section

        def mutate() -> None:
            cols = section.getPropertyValue("TextColumns")
            apply_column_layout(cols, column_count, mm_to_unit(spacing_mm))
            section.setPropertyValue("TextColumns", cols)

        return _commit(
            self,
            ctx,
            "WriterAgent: Set section columns",
            "set section columns",
            "SET_SECTION_COLUMNS_FAILED",
            mutate,
            {
                "status": "ok",
                "section_name": name,
                "column_count": column_count,
                "spacing_mm": spacing_mm,
            },
        )


class SectionSetProtection(ToolWriterSectionBase):
    """Set whether a text section is protected from editing in the Writer UI."""

    name: str | None = "section_set_protection"
    description: str = (
        "Set whether a text section is protected from editing (protection stops editing in the Writer UI only)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the section."},
            "protected": {"type": "boolean", "description": "True to protect the section, False to unprotect."},
        },
        "required": ["name", "protected"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name: str = kwargs["name"]
        protected = bool(kwargs["protected"])
        section = _section_or_error(self, ctx, name)
        if isinstance(section, dict):
            return section

        def mutate() -> None:
            section.setPropertyValue("IsProtected", protected)

        return _commit(
            self,
            ctx,
            "WriterAgent: Set section protection",
            "set section protection",
            "SET_SECTION_PROTECTION_FAILED",
            mutate,
            {"status": "ok", "section_name": name, "protected": protected},
        )
