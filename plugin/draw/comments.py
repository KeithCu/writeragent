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

"""Draw and Impress page annotation (comment) tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.doc.text_helpers import normalize_linebreaks
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.draw.base import ToolDrawCommentBase
from plugin.draw.bridge import DrawBridge
from plugin.framework.errors import reraise_if_disposed, uno_read
from plugin.framework.uno_context import enum_has_more, uno_same

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

# offapi/com/sun/star/office/XAnnotationAccess.idl — implemented on Impress/Draw pages in
# sd/source/ui/inc/unopage.hxx (SdGenericDrawPage), though drawing.DrawPage does not publish it.
_ANNOTATION_ACCESS_SVC = "com.sun.star.office.XAnnotationAccess"

_LIST_CAP = 100
_UNDO_TITLE = "WriterAgent: Page comment"
_UNDO_UNAVAILABLE_MSG = "Cannot change page comments atomically (no usable undo context)."
_ROLLBACK_MSG = "Page comment change failed and was rolled back"


def _annotation_date(dt: Any) -> str:
    if isinstance(dt, str):
        return dt
    if not dt:
        return ""
    return "%04d-%02d-%02d %02d:%02d" % (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)


def _annotation_text(ann: Any) -> str:
    text_range = uno_read(lambda: ann.TextRange, None)
    if text_range is None:
        return ""
    raw = uno_read(lambda: text_range.getString(), "") or ""
    return normalize_linebreaks(raw)


def _annotation_xy(ann: Any) -> tuple[Any, Any]:
    # Raw RealPoint2D from XAnnotation.Position — LibreOffice units, not converted to slide mm.
    pos = uno_read(lambda: ann.Position, None)
    if pos is None:
        return None, None
    return uno_read(lambda: pos.X, None), uno_read(lambda: pos.Y, None)


def _page_supports_annotations(page: Any) -> bool:
    # Prefer the published IDL service when LO reports it; live Impress/Draw pages implement
    # XAnnotationAccess on SdGenericDrawPage (sd/source/ui/inc/unopage.hxx) but often return
    # False from supportsService — then require the three IDL methods directly.
    if uno_read(lambda: page.supportsService(_ANNOTATION_ACCESS_SVC), False) is True:
        return True
    return (
        hasattr(page, "createAnnotationEnumeration")
        and hasattr(page, "createAndInsertAnnotation")
        and hasattr(page, "removeAnnotation")
    )


def _annotations_unavailable(tool: Any, page_index: int) -> dict[str, Any]:
    return tool._tool_error(
        "Page %d does not support comments (annotations)." % page_index,
        code="ANNOTATIONS_UNAVAILABLE",
        page=page_index,
    )


def _iter_annotations_on_page(page: Any) -> list[Any]:
    """Return annotations in ``createAnnotationEnumeration`` order.

    That order is the ``index`` contract for ``page_comment_delete`` (see
    ``offapi/com/sun/star/office/XAnnotationAccess.idl``). ``createAnnotationEnumeration``
    (svx/source/annotation/AnnotationEnumeration.cxx, ``createAnnotationEnumeration``)
    always returns an enumeration, empty when the page has no comments; the None guard
    below only protects against a runtime or stub that hands back nothing, and yields
    an empty list rather than raising.
    """
    try:
        enum = page.createAnnotationEnumeration()
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    if enum is None:
        return []
    items: list[Any] = []
    try:
        while enum_has_more(enum):
            items.append(enum.nextElement())
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    return items


def _comment_row(page_index: int, index: int, ann: Any) -> dict[str, Any]:
    x_val, y_val = _annotation_xy(ann)
    return {
        "page": page_index,
        "index": index,
        "author": uno_read(lambda: ann.Author, "") or "",
        "initials": uno_read(lambda: ann.Initials, "") or "",
        "date": _annotation_date(uno_read(lambda: ann.DateTime, None)),
        "text": _annotation_text(ann),
        "x": x_val,
        "y": y_val,
    }


def _page_targets(
    bridge: DrawBridge,
    page_kw: Any,
    *,
    active_page_index: Any,
) -> tuple[list[tuple[int, Any]], int | None]:
    """Build (page_index, page) pairs for one slide or the full deck."""
    if page_kw is not None:
        page, resolved = bridge.resolve_page(page_kw, active_page_index=active_page_index)
        return [(resolved, page)], resolved
    pages = bridge.get_pages()
    try:
        count = pages.getCount()
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    return [(page_index, pages.getByIndex(page_index)) for page_index in range(count)], None


def _collect_comments(
    tool: Any,
    page_targets: list[tuple[int, Any]],
) -> tuple[list[tuple[int, int, Any]], dict[str, Any] | None]:
    rows: list[tuple[int, int, Any]] = []
    for page_index, page in page_targets:
        if not _page_supports_annotations(page):
            return rows, _annotations_unavailable(tool, page_index)
        for idx, ann in enumerate(_iter_annotations_on_page(page)):
            rows.append((page_index, idx, ann))
    return rows, None


def _list_truncation_hint(count: int, total: int, next_offset: int, *, one_page: bool) -> str:
    if one_page:
        return "Showing %d of %d on this page. Pass offset=%d to continue." % (count, total, next_offset)
    return "Showing %d of %d. Pass offset=%d to continue, or page to one slide." % (count, total, next_offset)


def _run_page_comment_mutation(tool: Any, doc: Any, mutate: Any) -> dict[str, Any] | None:
    return commit_or_rollback_mutation(
        tool,
        doc,
        _UNDO_TITLE,
        mutate,
        undo_unavailable_msg=_UNDO_UNAVAILABLE_MSG,
        rollback_msg=_ROLLBACK_MSG,
    )


class ListPageComments(ToolDrawCommentBase):
    """List comments on one or all Draw/Impress pages."""

    name: str | None = "page_comment_list"
    description: str = (
        "List comments (annotations) on Draw or Impress pages. "
        "Omit page to scan every page; pass page for one slide. "
        "Use offset to paginate (up to 100 per call). "
        "Each row includes x/y as raw RealPoint2D Position values from LibreOffice (not slide mm)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (all pages if omitted)."},
            "offset": {"type": "integer", "description": "Skip this many comments in deck order (default 0)."},
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        offset = kwargs.get("offset", 0)
        if type(offset) is not int or offset < 0:
            return self._tool_error("offset must be a non-negative integer.", code="INVALID_PARAM")

        bridge = DrawBridge(ctx.doc)
        page_kw = kwargs.get("page")
        targets, resolved_page = _page_targets(
            bridge,
            page_kw,
            active_page_index=ctx.active_page_index,
        )
        flat, err = _collect_comments(self, targets)
        if err is not None:
            return err

        total = len(flat)
        window = flat[offset : offset + _LIST_CAP]
        truncated = offset + _LIST_CAP < total

        comments = [_comment_row(p_idx, idx, ann) for p_idx, idx, ann in window]
        result: dict[str, Any] = {
            "status": "ok",
            "comments": comments,
            "count": len(comments),
            "total": total,
            "truncated": truncated,
            "offset": offset,
        }
        if resolved_page is not None:
            result["page"] = resolved_page
        if truncated:
            next_offset = offset + len(comments)
            result["hint"] = _list_truncation_hint(
                len(comments),
                total,
                next_offset,
                one_page=resolved_page is not None,
            )
        return result


class AddPageComment(ToolDrawCommentBase):
    """Add a comment on a Draw or Impress page."""

    name: str | None = "page_comment_add"
    description: str = "Add a comment (annotation) on a Draw or Impress page."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Comment text."},
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)."},
            "author": {"type": "string", "description": "Author name (optional)."},
        },
        "required": ["text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        raw_text = kwargs.get("text")
        if raw_text is None or not str(raw_text).strip():
            return self._tool_error("text must be a non-empty string.", code="INVALID_PARAM")

        bridge = DrawBridge(ctx.doc)
        page, actual_idx = bridge.resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        if not _page_supports_annotations(page):
            return _annotations_unavailable(self, actual_idx)

        text = normalize_linebreaks(str(raw_text))
        author_kw = kwargs.get("author")
        author_strip = (author_kw or "").strip() if author_kw is not None else ""

        outcome: dict[str, Any] = {}

        def mutate() -> dict[str, Any] | None:
            try:
                new_ann = page.createAndInsertAnnotation()
            except Exception as exc:
                reraise_if_disposed(exc)
                raise
            text_range = uno_read(lambda: new_ann.TextRange, None)
            if text_range is not None:
                text_range.setString(text)
            if author_strip:
                new_ann.Author = author_strip
            # Re-scan enumeration: PyUNO may hand back a different wrapper for the same UNO object
            # (plugin/framework/uno_context.py uno_same); do not assume "last index" after insert.
            for idx, ann in enumerate(_iter_annotations_on_page(page)):
                if uno_same(ann, new_ann):
                    outcome["index"] = idx
                    outcome["author"] = uno_read(lambda: new_ann.Author, "") or ""
                    return None
            return {
                "status": "error",
                "code": "ANNOTATION_NOT_LISTED",
                "message": "New comment was inserted but is not visible in the page enumeration.",
            }

        err = _run_page_comment_mutation(self, ctx.doc, mutate)
        if err is not None:
            return err
        return {
            "status": "ok",
            "page": actual_idx,
            "index": outcome["index"],
            "text": text,
            "author": outcome["author"],
        }


class DeletePageComment(ToolDrawCommentBase):
    """Delete a comment by index on a Draw or Impress page."""

    name: str | None = "page_comment_delete"
    description: str = (
        "Delete a comment on a Draw or Impress page by its 0-based index "
        "(the index field from page_comment_list on that page). "
        "Indices shift after a delete."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "0-based index on the page."},
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)."},
        },
        "required": ["index"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        index = kwargs.get("index")
        if type(index) is not int or index < 0:
            return self._tool_error("index must be a non-negative integer.", code="INVALID_PARAM")

        bridge = DrawBridge(ctx.doc)
        page, actual_idx = bridge.resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        if not _page_supports_annotations(page):
            return _annotations_unavailable(self, actual_idx)

        annotations = _iter_annotations_on_page(page)
        if index >= len(annotations):
            return self._tool_error(
                "No comment at index %d on page %d." % (index, actual_idx),
                code="COMMENT_NOT_FOUND",
                hint="Call page_comment_list with page=%d to list current indices." % actual_idx,
            )

        target = annotations[index]

        def mutate() -> dict[str, Any] | None:
            try:
                page.removeAnnotation(target)
            except Exception as exc:
                reraise_if_disposed(exc)
                raise
            return None

        err = _run_page_comment_mutation(self, ctx.doc, mutate)
        if err is not None:
            return err
        return {
            "status": "ok",
            "page": actual_idx,
            "index": index,
            "message": "Comment deleted.",
        }
