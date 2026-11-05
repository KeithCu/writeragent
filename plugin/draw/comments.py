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
from plugin.framework.uno_context import uno_same

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_LIST_CAP = 100
_UNDO_TITLE = "WriterAgent: Page comment"


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
    pos = uno_read(lambda: ann.Position, None)
    if pos is None:
        return None, None
    return uno_read(lambda: pos.X, None), uno_read(lambda: pos.Y, None)


def _page_supports_annotations(page: Any) -> bool:
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
    """Return annotations in ``createAnnotationEnumeration`` order (empty when enum is None)."""
    try:
        enum = page.createAnnotationEnumeration()
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    if enum is None:
        return []
    items: list[Any] = []
    try:
        while enum.hasMoreElements():
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


def _collect_all_comments(
    tool: Any,
    bridge: DrawBridge,
    *,
    single_page: Any | None,
    single_index: int | None,
) -> tuple[list[tuple[int, int, Any]], dict[str, Any] | None]:
    rows: list[tuple[int, int, Any]] = []
    if single_page is not None and single_index is not None:
        if not _page_supports_annotations(single_page):
            return rows, _annotations_unavailable(tool, single_index)
        for idx, ann in enumerate(_iter_annotations_on_page(single_page)):
            rows.append((single_index, idx, ann))
        return rows, None

    pages = bridge.get_pages()
    try:
        count = pages.getCount()
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    for page_index in range(count):
        page = pages.getByIndex(page_index)
        if not _page_supports_annotations(page):
            return rows, _annotations_unavailable(tool, page_index)
        for idx, ann in enumerate(_iter_annotations_on_page(page)):
            rows.append((page_index, idx, ann))
    return rows, None


class ListPageComments(ToolDrawCommentBase):
    """List comments on one or all Draw/Impress pages."""

    name: str | None = "page_comment_list"
    description: str = (
        "List comments (annotations) on Draw or Impress pages. "
        "Omit page to scan every page; pass page for one slide. "
        "Use offset to paginate (up to 100 per call)."
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
        single_page: Any | None = None
        resolved_page: int | None = None
        if page_kw is not None:
            single_page, resolved_page = bridge.resolve_page(page_kw, active_page_index=ctx.active_page_index)

        flat, err = _collect_all_comments(
            self,
            bridge,
            single_page=single_page,
            single_index=resolved_page,
        )
        if err is not None:
            return err

        total = len(flat)
        if offset >= total:
            window: list[tuple[int, int, Any]] = []
            truncated = False
        elif offset + _LIST_CAP >= total:
            window = flat[offset:]
            truncated = False
        else:
            window = flat[offset : offset + _LIST_CAP]
            truncated = True

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
            if resolved_page is not None:
                result["hint"] = "Showing %d of %d on this page. Pass offset=%d to continue." % (
                    len(comments),
                    total,
                    next_offset,
                )
            else:
                result["hint"] = (
                    "Showing %d of %d. Pass offset=%d to continue, or page to one slide."
                    % (len(comments), total, next_offset)
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

        err = commit_or_rollback_mutation(
            self,
            ctx.doc,
            _UNDO_TITLE,
            mutate,
            undo_unavailable_msg="Cannot change page comments atomically (no usable undo context).",
            rollback_msg="Page comment change failed and was rolled back",
        )
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

        err = commit_or_rollback_mutation(
            self,
            ctx.doc,
            _UNDO_TITLE,
            mutate,
            undo_unavailable_msg="Cannot change page comments atomically (no usable undo context).",
            rollback_msg="Page comment change failed and was rolled back",
        )
        if err is not None:
            return err
        return {
            "status": "ok",
            "page": actual_idx,
            "index": index,
            "message": "Comment deleted.",
        }
