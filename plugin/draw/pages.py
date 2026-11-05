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
"""Page/slide management tools for Draw/Impress documents."""

import logging
from typing import Any

from plugin.framework.doc_type import is_impress
from plugin.draw.bridge import DrawBridge, page_size
from plugin.draw.base import DRAW_AND_IMPRESS
from plugin.draw.masters import iter_masters
from plugin.framework.errors import available_miss_details, is_disposal
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.uno_context import uno_same
from plugin.writer.search import DEFAULT_SEARCH_RESULTS

log = logging.getLogger("writeragent.draw.pages")


# Impress Title + Content (title + body). Not "title" (id 0 = title+subtitle).
_DEFAULT_IMPRESS_LAYOUT = "text"


def inherit_master_from_neighbor(pages: Any, new_page: Any, insert_at: int) -> str:
    """Copy MasterPage from a slide that is not the new page.

    ``insertNewByIndex`` can leave a factory Default master even when the deck
    already has an assigned design master. Prefer the previous slide, then the
    following one, so add_slide keeps the deck look.

    ``add_slide(page=0)`` can pass ``insert_at=0`` while the new
    page actually sits at index 1. The only candidate is then index
    1, which is the new page, so the copy assigns that page's own
    master. ``InsertSdPage`` cannot create a page at index 0, and a
    neighbor chosen by integer index alone is the new page. Skip
    the new page object (``uno_same``), not the integer
    ``insert_at``. A stale ``insert_at`` of 0 is the neighbor when
    the new page actually sits at 1. With placement fixed, index
    0's neighbor is the previous first slide.
    """
    # pages.getCount() does not fail on a live deck. Returning "" hid a closed document.
    count = int(pages.getCount())
    if count < 2:
        return ""

    # False sorts first: previous, then next, then ascending. Out-of-range neighbors are not in range(count).
    order = sorted(range(count), key=lambda i: (i != insert_at - 1, i != insert_at + 1, i))
    for ref_idx in order:
        try:
            ref = pages.getByIndex(ref_idx)
            if uno_same(ref, new_page):
                continue
            master = ref.MasterPage
            new_page.MasterPage = master
            return master.Name if hasattr(master, "Name") else ""
        except Exception as exc:
            # Prefer the previous slide, then the following one. Returning
            # here stopped on the first failure. Only disposal escapes;
            # a live-document RuntimeException tries the next neighbor.
            if is_disposal(exc):
                raise
            log.debug("inherit_master_from_neighbor failed insert_at=%s ref=%s", insert_at, ref_idx, exc_info=True)
    return ""


class AddSlide(ToolBase):
    name: str | None = "add_slide"
    description: str = "Inserts a new slide (page) at the specified index. Impress defaults to the Title + Content ('text') layout. New slides inherit the deck's assigned master. Call list_placeholders before set_placeholder_text."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based index where to insert the new slide (defaults to appending at the end if omitted)"},
            "activate": {"type": "boolean", "description": "Whether to switch the view to the new slide (default: true)"},
            "layout": {"type": "string", "description": ("Impress layout name (default: 'text' = Title + Content). Use 'blank' or 'none' for an empty slide. Ignored on Draw documents.")},
        },
        "required": [],
    }
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.layouts import available_layout_names, layout_id
        from plugin.draw.transitions import apply_slide_layout

        bridge = DrawBridge(ctx.doc)
        page_idx = kwargs.get("page")
        activate = kwargs.get("activate", True)
        switch_view = bool(activate if activate is not None else True)

        layout_name: str | None = None
        if is_impress(ctx.doc):
            raw = kwargs.get("layout")
            layout_name = _DEFAULT_IMPRESS_LAYOUT if raw is None or (isinstance(raw, str) and not raw.strip()) else str(raw).strip().lower()
            # Validate before insert so a bad name does not leave a stray page.
            if layout_id(layout_name) is None:
                avail = available_layout_names()
                return self._tool_error(
                    "Unknown layout: %s" % layout_name,
                    available=avail,
                    **available_miss_details(str(layout_name), avail),
                )

        # Bugfix: create_slide(switch=True) moved the view onto the new page
        # before master inheritance and layout. A failure then removed that page
        # and left the controller on it. insert_slide_from_master already inserts
        # with switch=False and switches only after MasterPage is assigned.
        # Do not trust get_active_page_index after a requested switch — Impress
        # DrawPage.getNumber() is missing/None, so the helper falls back to 0.
        # The landed index is the slot the new page occupies.
        new_page, landed = bridge.create_slide(page_idx, switch=False)

        result: dict[str, Any] = {"status": "ok", "message": "Slide added"}

        try:
            # insertNewByIndex can attach factory Default even when the deck already
            # has a designed master (M1′). Copy the neighbor slide's MasterPage.
            # Pass the landed index: page=0 used to claim insert_at=0 while the
            # new page sat at 1, so the "neighbor" was the new page itself.
            if layout_name is not None:
                inherited = inherit_master_from_neighbor(bridge.get_pages(), new_page, landed)
                if inherited:
                    result["master"] = inherited
                result["placeholders_hint"] = "call list_placeholders on this page"
                # blank/none are AUTOLAYOUT_NONE (20). insertNewByIndex already
                # leaves that id with 0 shapes; assigning it keeps the page empty.
                # Skipping used to be required because the PowerPoint blank id (11)
                # is AUTOLAYOUT_OBJ and grew a title plus an OLE placeholder.
                result["layout"] = apply_slide_layout(new_page, layout_name)
        except Exception as exc:
            # Only disposal escapes; a live-document RuntimeException removes the new page and reports the error.
            if is_disposal(exc):
                raise
            cleanup_failed = False
            try:
                bridge.get_pages().remove(new_page)
            except Exception as rm_exc:
                if is_disposal(rm_exc):
                    raise
                cleanup_failed = True
                log.debug("add_slide cleanup remove failed", exc_info=True)
            return self._tool_error(
                f"Failed to set layout or master on new slide: {exc}",
                cleanup_failed=cleanup_failed,
            )

        # What was wrong: failed set_current_page_index return was ignored, reporting landed as active_page_index.
        # How it happened: bridge.set_current_page_index return value was not checked.
        # Why this change: report real active page when switch fails and set view_switch_failed flag.
        switched = bridge.set_current_page_index(landed) if switch_view else False
        if switch_view and not switched:
            result["view_switch_failed"] = True
        result["active_page_index"] = landed if switched else bridge.get_active_page_index()
        return result


class _SlideMutationTool(ToolBase):
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    is_mutation: bool | None = True


class DeleteSlide(_SlideMutationTool):
    name: str | None = "delete_slide"
    description: str = "Deletes the slide (page) at the specified index."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of slide to delete"}}, "required": ["page"]}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = DrawBridge(ctx.doc)
        page_idx = kwargs["page"]
        pages = bridge.get_pages()
        count = pages.getCount()
        # Range check only; reuse count for the one-slide guard below.
        bridge.resolve_page(page_idx)
        # A one-page Draw or Impress document keeps its only slide. An
        # in-range index is not enough to call bridge.delete_slide.
        # SlideCommandEngine._delete_slide already refuses that case
        # before it touches the document. Return the same error and skip
        # the removal when one page remains.
        if count <= 1:
            return self._tool_error("Cannot delete the only slide")
        bridge.delete_slide(page_idx)

        return {"status": "ok", "message": "Slide deleted", "active_page_index": bridge.get_active_page_index()}


class ListPages(ToolBase):
    name: str | None = "list_pages"
    description: str = "Lists all pages (slides). Each entry is that page's Name, or \"Page N\" when Name is missing or blank. The list position is the 0-based page index."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = DrawBridge(ctx.doc)
        active_idx = bridge.active_or_ctx_index(None, active_page_index=ctx.active_page_index)
        pages = bridge.get_pages()
        labels: list[str] = []
        count = pages.getCount()
        for i in range(count):
            name = str(pages.getByIndex(i).Name or "").strip()
            labels.append(name or f"Page {i}")
        return {"status": "ok", "pages": labels, "count": count, "active_page_index": active_idx}


def _shape_tree_address(path: str) -> int | str:
    return int(path) if "." not in path else path


def _table_tsv_record(shape: Any) -> str | None:
    from plugin.draw.tables import _is_table_shape, _table_model, get_draw_cells

    if not _is_table_shape(shape):
        return None
    model = _table_model(shape)
    if model is None:
        return None
    matrix = get_draw_cells({"model": model})
    lines: list[str] = []
    for row in matrix:
        if not any(cell.strip() for cell in row):
            continue
        lines.append("\t".join(cell.strip() for cell in row))
    if not lines:
        return None
    return "\n".join(lines)


def iter_shape_text_units(page: Any):
    """Yield (address, shape_name, text) for searchable / readable shape text units."""
    from plugin.draw.tree import _is_group, iter_shape_addresses
    from plugin.draw.tables import _is_table_shape, _table_model, get_draw_cells

    for path, shape, _parent in iter_shape_addresses(page):
        if _is_group(shape):
            continue
        addr = _shape_tree_address(path)
        shape_name = str(getattr(shape, "Name", "") or "")
        if _is_table_shape(shape):
            model = _table_model(shape)
            if model is None:
                continue
            try:
                matrix = get_draw_cells({"model": model})
            except Exception as exc:
                if is_disposal(exc):
                    raise
                continue
            for row in matrix:
                for cell in row:
                    text = cell.strip()
                    if text:
                        yield addr, shape_name, text
            continue
        try:
            text = shape.getString()
        except Exception as exc:
            if is_disposal(exc):
                raise
            continue
        if text and str(text).strip():
            yield addr, shape_name, str(text).strip()


class ReadSlideText(ToolBase):
    """Read all text content from a slide plus speaker notes."""

    name: str | None = "read_slide_text"
    description: str = (
        "Read text from shapes on a slide (including grouped descendants and table cells as TSV) "
        "and speaker notes. Returns structured text per shape or table."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (default: active slide)."}}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import notes_text
        from plugin.draw.tree import _is_group, iter_shape_addresses

        bridge = DrawBridge(ctx.doc)
        page, actual_idx = bridge.resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        texts: list[dict[str, Any]] = []
        for path, shape, _parent in iter_shape_addresses(page):
            if _is_group(shape):
                continue
            addr = _shape_tree_address(path)
            shape_name = str(getattr(shape, "Name", "") or "")
            tsv = _table_tsv_record(shape)
            if tsv is not None:
                texts.append({"index": addr, "text": tsv, "shape_name": shape_name})
                continue
            try:
                txt = shape.getString()
            except Exception as exc:
                if is_disposal(exc):
                    raise
                continue
            if txt and str(txt).strip():
                texts.append({"index": addr, "text": str(txt).strip(), "shape_name": shape_name})

        nt = notes_text(page)
        notes_str = "" if nt is None else nt

        return {"status": "ok", "page": actual_idx, "texts": texts, "notes": notes_str}


class SearchSlides(ToolBase):
    """Search shape text and speaker notes across Draw/Impress slides."""

    name: str | None = "search_slides"
    description: str = (
        "Search text in slide shapes (including grouped descendants and table cells) and speaker notes. "
        "Each match reports the page, shape name, matched text, and shape index when available."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default: %d). Must be a positive integer." % DEFAULT_SEARCH_RESULTS,
            },
        },
        "required": ["pattern"],
    }
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import notes_text
        from plugin.writer.search import (
            MAX_SEARCH_HITS,
            invalid_regex_tool_message,
            shape_text_hits,
            validate_regex_pattern,
        )

        raw_max = kwargs.get("max_results", DEFAULT_SEARCH_RESULTS)
        if type(raw_max) is not int or raw_max < 1:
            return self._tool_error("max_results must be a positive integer.", code="INVALID_PARAM")
        max_results = raw_max

        pattern = kwargs["pattern"]
        assert isinstance(pattern, str)
        if not pattern:
            return self._tool_error("pattern is required.")

        use_regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)

        if use_regex:
            rex_err = validate_regex_pattern(pattern)
            if rex_err:
                return self._tool_error(invalid_regex_tool_message(rex_err), code="INVALID_REGEX", count=0)

        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        matches: list[dict[str, Any]] = []
        total_count = 0

        def append_hit(page_idx: int, shape_name: str, hit_text: str, index: int | str | None) -> None:
            nonlocal total_count
            if total_count >= MAX_SEARCH_HITS:
                return
            total_count += 1
            if len(matches) < max_results:
                entry: dict[str, Any] = {"page": page_idx, "shape_name": shape_name, "text": hit_text}
                if index is not None:
                    entry["index"] = index
                matches.append(entry)

        page_count = pages.getCount()
        for page_idx in range(page_count):
            if total_count >= MAX_SEARCH_HITS:
                break
            page = pages.getByIndex(page_idx)
            for addr, shape_name, unit_text in iter_shape_text_units(page):
                if total_count >= MAX_SEARCH_HITS:
                    break
                for hit in shape_text_hits(unit_text, pattern, use_regex, case_sensitive):
                    append_hit(page_idx, shape_name, hit, addr)
                    if total_count >= MAX_SEARCH_HITS or len(matches) >= max_results:
                        break
            notes = notes_text(page)
            if notes and total_count < MAX_SEARCH_HITS:
                for hit in shape_text_hits(notes, pattern, use_regex, case_sensitive):
                    append_hit(page_idx, "Notes", hit, None)
                    if total_count >= MAX_SEARCH_HITS or len(matches) >= max_results:
                        break

        result: dict[str, Any] = {
            "status": "ok",
            "matches": matches,
            "count": total_count,
            "returned": len(matches),
        }
        if len(matches) < total_count:
            result["hint"] = (
                "Showing %d of %d. Narrow the query. This tool has no offset." % (len(matches), total_count)
            )
        return result


class GetPresentationInfo(ToolBase):
    """Get presentation metadata."""

    name: str | None = "get_presentation_info"
    description: str = "Get presentation metadata: slide count, dimensions, master slide names, and whether it is an Impress document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        pages = doc.getDrawPages()
        count = pages.getCount()

        # Dimensions from first page
        width_mm = 0
        height_mm = 0
        if count > 0:
            p = pages.getByIndex(0)
            size = page_size(p)
            if size:
                width_mm, height_mm = size[0] // 100, size[1] // 100

        # Master pages
        masters = [name or ("Master_%d" % i) for i, _m, name in iter_masters(doc)]

        bridge = DrawBridge(doc)
        active_idx = bridge.active_or_ctx_index(None, active_page_index=ctx.active_page_index)

        return {"status": "ok", "slide_count": count, "width_mm": width_mm, "height_mm": height_mm, "master_slides": masters, "is_impress": is_impress(doc), "active_page_index": active_idx}


class SetActivePage(ToolBase):
    name: str | None = "set_active_page"
    description: str = "Changes the currently active slide (page) in Draw/Impress."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of page to activate"}}, "required": ["page"]}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    # View change only. None would still detect a mutation: the name
    # set_active_page is not a read prefix. read_only_target and the MCP
    # document lock both follow detects_mutation.
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = DrawBridge(ctx.doc)
        idx = kwargs["page"]
        # Range check only before switching the controller view.
        bridge.resolve_page(idx)
        # Bugfix: this reimplemented setCurrentPage and caught every exception,
        # so a disposed controller became a normal tool error instead of
        # DOCUMENT_DISPOSED. set_current_page_index already re-raises disposal
        # and returns False for a missing controller or a non-disposal failure.
        if not bridge.set_current_page_index(idx):
            return self._tool_error("Failed to set active page.")
        return {"status": "ok", "message": "Active page changed to %d" % idx, "active_page_index": idx}


class DuplicateSlide(_SlideMutationTool):
    name: str | None = "duplicate_slide"
    description: str = "Duplicates the slide at the given 0-based index. The copy is inserted immediately after the source slide."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of the slide to duplicate"}, "activate": {"type": "boolean", "description": "Whether to switch the view to the new slide (default: true)"}}, "required": ["page"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs["page"]
        bridge = DrawBridge(ctx.doc)
        bridge.resolve_page(page_idx)  # range check only
        activate = kwargs.get("activate", True)
        switch_view = bool(activate if activate is not None else True)
        bridge.duplicate_slide(page_idx, switch=switch_view)
        # The copy is inserted immediately after the source. Do not re-read
        # getNumber() — Impress leaves it missing and the helper used to
        # report 0 after a successful switch.
        active_idx = page_idx + 1 if switch_view else bridge.get_active_page_index()
        return {"status": "ok", "message": "Slide duplicated", "source_page": page_idx, "active_page_index": active_idx}


class MoveSlide(_SlideMutationTool):
    name: str | None = "move_slide"
    description: str = "Moves a slide from from_page to to_page (both 0-based). to_page is the destination index after removal of the source (insert-at that index)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"from_page": {"type": "integer", "description": "0-based source slide index"}, "to_page": {"type": "integer", "description": "0-based destination slide index"}}, "required": ["from_page", "to_page"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import PageExchangeRollbackError

        from_page = kwargs["from_page"]
        to_page = kwargs["to_page"]
        bridge = DrawBridge(ctx.doc)
        try:
            ok = bridge.move_slide(from_page, to_page)
        except PageExchangeRollbackError as exc:
            # Bugfix: move_slide now raises when the temp page still holds shapes;
            # execute_safe would prefix that with "Tool execution failed", and the old
            # False path hid it as "Could not move slide". Return the rollback error directly.
            return self._tool_error(str(exc))
        if not ok:
            return self._tool_error("Could not move slide from %s to %s." % (from_page, to_page))
        return {"status": "ok", "message": "Slide moved", "from_page": from_page, "to_page": to_page, "active_page_index": bridge.get_active_page_index()}


class RenameSlide(_SlideMutationTool):
    name: str | None = "rename_slide"
    description: str = "Sets the Name property of a slide (0-based page index)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index"}, "name": {"type": "string", "description": "New slide name"}}, "required": ["page", "name"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs["page"]
        name = kwargs["name"]
        if not name:
            return self._tool_error("name is required.")
        bridge = DrawBridge(ctx.doc)
        bridge.resolve_page(page_idx)
        ok = bridge.rename_slide(page_idx, str(name))
        if not ok:
            return self._tool_error("Slide does not support renaming.")
        return {"status": "ok", "message": "Slide renamed", "page": page_idx, "name": str(name), "active_page_index": bridge.get_active_page_index()}
