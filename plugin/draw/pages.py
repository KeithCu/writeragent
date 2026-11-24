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
from typing import Any, Literal

from plugin.framework.doc_type import is_impress
from plugin.draw.bridge import DrawBridge, page_size
from plugin.draw.base import DRAW_AND_IMPRESS, DRAW_PAGE_PROPERTY
from plugin.draw.masters import iter_masters
from plugin.draw.placeholders import slide_title
from plugin.draw.tables import table_cells_for_shape, table_matrix_to_tsv
from plugin.draw.tree import is_group, iter_shape_addresses
from plugin.framework.errors import available_miss_details, is_disposal
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.uno_context import uno_same
from plugin.writer.search import (
    DEFAULT_SEARCH_RESULTS,
    MAX_SEARCH_HITS,
    case_sensitive_param,
    invalid_regex_tool_message,
    resolve_case_sensitive,
    search_results_cap_hint,
    shape_text_hits,
    validate_regex_pattern,
)

log = logging.getLogger("writeragent.draw.pages")


# Impress Title + Content (title + body). Not "title" (id 0 = title+subtitle).
_DEFAULT_IMPRESS_LAYOUT = "text"
_DEFAULT_ACTIVATE = True


def inherit_master_from_neighbor(pages: Any, new_page: Any, insert_at: int) -> str:
    """Copy MasterPage from a slide that is not the new page.

    ``insertNewByIndex`` can leave a factory Default master even when the deck
    already has an assigned design master. Prefer the previous slide, then the
    following one, so slide_add keeps the deck look.

    ``slide_add(page=0)`` can pass ``insert_at=0`` while the new
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
    name: str | None = "slide_add"
    description: str = "Inserts a new slide (page) at the specified index. Impress defaults to the Title + Content ('text') layout. New slides inherit the deck's assigned master."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based index where to insert the new slide (defaults to appending at the end if omitted)"},
            "activate": {"type": "boolean", "default": _DEFAULT_ACTIVATE, "description": "Whether to switch the view to the new slide"},
            "layout": {"type": "string", "default": _DEFAULT_IMPRESS_LAYOUT, "description": ("Impress layout name ('text' = Title + Content). Use 'blank' or 'none' for an empty slide. Ignored on Draw documents.")},
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
        activate = kwargs.get("activate")
        switch_view = _DEFAULT_ACTIVATE if activate is None else bool(activate)

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

        # Master and layout run before switching the view; create_slide uses switch=False like
        # insert_slide_from_master. Report the landed index, not get_active_page_index after switch.
        new_page, landed = bridge.create_slide(page_idx, switch=False)

        result: dict[str, Any] = {"status": "ok", "message": "Slide added"}

        try:
            # insertNewByIndex can attach factory Default even when the deck already
            # has a designed master (M1′). Copy the neighbor slide's MasterPage.
            # inherit_master_from_neighbor needs the landed index, not the requested insert index.
            if layout_name is not None:
                inherited = inherit_master_from_neighbor(bridge.get_pages(), new_page, landed)
                if inherited:
                    result["master"] = inherited
                result["placeholders_hint"] = "call placeholder_list on this page"
                # blank/none are AUTOLAYOUT_NONE (20). insertNewByIndex already
                # leaves that id with 0 shapes; assigning it keeps the page empty.
                # blank/none still call apply_slide_layout; only PowerPoint blank id 11 is avoided elsewhere.
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
                log.debug("slide_add cleanup remove failed", exc_info=True)
            return self._tool_error(
                f"Failed to set layout or master on new slide: {exc}",
                cleanup_failed=cleanup_failed,
            )

        # When switch_view is true, check set_current_page_index and set view_switch_failed on failure.
        # active_page_index reflects the controller, not the assumed landed slot.
        switched = bridge.set_current_page_index(landed) if switch_view else False
        if switch_view and not switched:
            result["view_switch_failed"] = True
        result["active_page_index"] = landed if switched else bridge.get_active_page_index()
        return result


class _SlideMutationTool(ToolBase):
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    is_mutation: bool | None = True


class DeleteSlide(_SlideMutationTool):
    name: str | None = "slide_delete"
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


def page_entries(pages: Any) -> list[dict[str, Any]]:
    """Return ``[{index, name, title, ...}, ...]`` for every page in *pages*.

    An unnamed slide reports LibreOffice's default ``pageN``, where ``N`` is
    its 1-based position (``sd/source/ui/unoidl/unopage.cxx:getPageApiName``).
    That default shifts when slides are inserted or moved, so entries with a
    blank name or ``page<index + 1>`` are marked ``default_name: True``.
    """
    entries: list[dict[str, Any]] = []
    for i in range(pages.getCount()):
        page = pages.getByIndex(i)
        name = str(page.Name or "").strip()
        entry: dict[str, Any] = {"index": i, "name": name, "title": slide_title(page)}
        if not name or name == f"page{i + 1}":
            entry["default_name"] = True
        entries.append(entry)
    return entries


class ListPages(ToolBase):
    name: str | None = "slide_list"
    description: str = (
        "Lists pages (slides) in deck order as {index, name, title}; index is 0-based. "
        "Unnamed slides have LibreOffice's positional default 'pageN' "
        "(N = 1-based position) and include default_name: true."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = DrawBridge(ctx.doc)
        active_idx = bridge.active_or_ctx_index(None, active_page_index=ctx.active_page_index)
        pages = page_entries(bridge.get_pages())
        return {"status": "ok", "pages": pages, "count": len(pages), "active_page_index": active_idx}


def _shape_tree_address(path: str) -> int | str:
    return int(path) if "." not in path else path


def iter_shape_text_units(page: Any, *, table_mode: Literal["per_cell", "tsv"] = "per_cell"):
    """Yield (address, shape_name, text) for read/search over shape text.

    Skips group containers. Tables use ``table_mode``: one TSV blob (read) or one
    yield per non-empty cell (search). Disposal propagates; other failures skip
    the shape and log at debug.
    """
    for path, shape, _parent in iter_shape_addresses(page):
        if is_group(shape):
            continue
        addr = _shape_tree_address(path)
        shape_name = str(getattr(shape, "Name", "") or "")
        matrix = table_cells_for_shape(shape)
        if matrix is not None:
            if table_mode == "tsv":
                tsv = table_matrix_to_tsv(matrix)
                if tsv:
                    yield addr, shape_name, tsv
            else:
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
            log.debug("iter_shape_text_units getString failed", exc_info=True)
            continue
        if text and str(text).strip():
            yield addr, shape_name, str(text).strip()


class ReadSlideText(ToolBase):
    """Read all text content from a slide plus speaker notes."""

    name: str | None = "slide_read_text"
    description: str = (
        "Read text from shapes on a slide (including grouped descendants and table cells as TSV) "
        "and speaker notes. Returns structured text per shape or table."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": DRAW_PAGE_PROPERTY}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import notes_text

        bridge = DrawBridge(ctx.doc)
        page, actual_idx = bridge.resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        texts: list[dict[str, Any]] = []
        for addr, shape_name, text in iter_shape_text_units(page, table_mode="tsv"):
            texts.append({"index": addr, "text": text, "shape_name": shape_name})

        nt = notes_text(page)
        notes_str = "" if nt is None else nt

        return {"status": "ok", "page": actual_idx, "texts": texts, "notes": notes_str}


class SearchSlides(ToolBase):
    """Search shape text and speaker notes across Draw/Impress slides."""

    name: str | None = "slide_search"
    description: str = (
        "Search text in slide shapes (including grouped descendants and table cells) and speaker notes. "
        "Each match reports the page, shape name, matched text, and shape index when available."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "default": False, "description": "Use regular expression."},
            "case_sensitive": case_sensitive_param(),
            "max_results": {
                "type": "integer",
                "default": DEFAULT_SEARCH_RESULTS,
                "description": "Maximum results to return. Must be a positive integer.",
            },
        },
        "required": ["pattern"],
    }
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import notes_text

        raw_max = kwargs.get("max_results", DEFAULT_SEARCH_RESULTS)
        if type(raw_max) is not int or raw_max < 1:
            return self._tool_error("max_results must be a positive integer.", code="INVALID_PARAM")
        max_results = raw_max

        pattern = kwargs["pattern"]
        assert isinstance(pattern, str)
        if not pattern:
            return self._tool_error("pattern is required.")

        use_regex = kwargs.get("regex", False)
        case_sensitive = resolve_case_sensitive(kwargs.get("case_sensitive"))

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
            for addr, shape_name, unit_text in iter_shape_text_units(page, table_mode="per_cell"):
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
            "matched_case_sensitive": case_sensitive,
        }
        if len(matches) < total_count:
            result["hint"] = search_results_cap_hint(len(matches), total_count)
        return result


class GetPresentationInfo(ToolBase):
    """Get presentation metadata."""

    name: str | None = "get_deck_info"
    description: str = "Get presentation metadata: slide count, slide width and height (1/100 mm, the unit shapes use), master slide names, and whether it is an Impress document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        pages = doc.getDrawPages()
        count = pages.getCount()

        # Dimensions from first page
        width, height = (page_size(pages.getByIndex(0)) or (0, 0)) if count > 0 else (0, 0)

        # Master pages
        masters = [name or ("Master_%d" % i) for i, _m, name in iter_masters(doc)]

        bridge = DrawBridge(doc)
        active_idx = bridge.active_or_ctx_index(None, active_page_index=ctx.active_page_index)

        return {"status": "ok", "slide_count": count, "width": width, "height": height, "master_slides": masters, "is_impress": is_impress(doc), "active_page_index": active_idx}


class SetActivePage(ToolBase):
    name: str | None = "slide_set_active"
    description: str = "Changes the currently active slide (page) in Draw/Impress."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of page to activate"}}, "required": ["page"]}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    # View change only. None would still detect a mutation: the name
    # slide_set_active is not a read prefix. read_only_target and the MCP
    # document lock both follow detects_mutation.
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = DrawBridge(ctx.doc)
        idx = kwargs["page"]
        # Range check only before switching the controller view.
        bridge.resolve_page(idx)
        # Route through set_current_page_index so disposal propagates as DOCUMENT_DISPOSED.
        if not bridge.set_current_page_index(idx):
            return self._tool_error("Failed to set active page.")
        return {"status": "ok", "message": "Active page changed to %d" % idx, "active_page_index": idx}


class DuplicateSlide(_SlideMutationTool):
    name: str | None = "slide_duplicate"
    description: str = "Duplicates the slide at the given 0-based index. The copy is inserted immediately after the source slide."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of the slide to duplicate"}, "activate": {"type": "boolean", "default": _DEFAULT_ACTIVATE, "description": "Whether to switch the view to the new slide"}}, "required": ["page"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs["page"]
        bridge = DrawBridge(ctx.doc)
        bridge.resolve_page(page_idx)  # range check only
        activate = kwargs.get("activate")
        switch_view = _DEFAULT_ACTIVATE if activate is None else bool(activate)
        bridge.duplicate_slide(page_idx, switch=switch_view)
        # The copy is inserted immediately after the source. Do not re-read
        # After duplicate with switch, the active index is page_idx + 1, not getNumber().
        active_idx = page_idx + 1 if switch_view else bridge.get_active_page_index()
        return {"status": "ok", "message": "Slide duplicated", "source_page": page_idx, "active_page_index": active_idx}


class MoveSlide(_SlideMutationTool):
    name: str | None = "slide_move"
    description: str = (
        "Moves the slide at from_page (0-based) so it ends at to_page (the 0-based index the slide ends at). "
        "Returns the final slide order."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "from_page": {"type": "integer", "description": "0-based source slide index"},
            "to_page": {"type": "integer", "description": "0-based index the slide ends at"},
        },
        "required": ["from_page", "to_page"],
    }
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import PageExchangeRollbackError

        from_page = kwargs["from_page"]
        to_page = kwargs["to_page"]
        bridge = DrawBridge(ctx.doc)
        try:
            ok = bridge.move_slide(from_page, to_page)
        except PageExchangeRollbackError as exc:
            # PageExchangeRollbackError carries the real failure; return it without execute_safe prefixing.
            return self._tool_error(str(exc))
        if not ok:
            return self._tool_error("Could not move slide from %s to %s." % (from_page, to_page))
        return {
            "status": "ok",
            "message": "Slide moved",
            "from_page": from_page,
            "to_page": to_page,
            "pages": page_entries(bridge.get_pages()),
            "active_page_index": bridge.get_active_page_index(),
        }


class RenameSlide(_SlideMutationTool):
    name: str | None = "slide_rename"
    description: str = "Sets the Name of a slide (0-based page index). The name must not already be used by another slide; LibreOffice refuses names that start with its default slide label (e.g. 'Slide 3')."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index"}, "name": {"type": "string", "description": "New slide name"}}, "required": ["page", "name"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs["page"]
        wanted = str(kwargs["name"] or "")
        if not wanted.strip():
            return self._tool_error("name is required.")
        bridge = DrawBridge(ctx.doc)
        page, _idx = bridge.resolve_page(page_idx)
        pages = bridge.get_pages()
        # UNO setName does not check uniqueness (the rename dialog does, via
        # sd/source/ui/docshell/docshel2.cxx:DrawDocShell::IsNewPageNameValid);
        # compare against LibreOffice's API names so another unnamed slide counts as its pageN.
        for i in range(pages.getCount()):
            if i != page_idx and str(pages.getByIndex(i).Name or "").strip() == wanted.strip():
                return self._tool_error(
                    "Slide %d is already named %r; slide names must be unique." % (i, wanted.strip()),
                    code="INVALID_PARAM",
                )
        previous = str(page.Name or "")
        if not bridge.rename_slide(page_idx, wanted):
            return self._tool_error("Slide does not support renaming.")
        # sd/source/ui/unoidl/unopage.cxx:SdDrawPage::setName clears names beginning with the
        # localized STR_PAGE + " " and drops the previous name. That label is not exposed over
        # UNO, so read back page.Name to detect refusal; writing back previous is safe because
        # a slide's own default pageN is cleared back to unnamed.
        stored = str(page.Name or "")
        if stored != wanted:
            bridge.rename_slide(page_idx, previous)
            kept = str(page.Name or "")
            return self._tool_error(
                "LibreOffice did not store the name %r (it stores %r instead; names starting with its default slide label such as 'Slide 3', or a slide's own default 'pageN', are not kept). The slide keeps the name %r."
                % (wanted, stored, kept),
                code="INVALID_PARAM",
            )
        return {"status": "ok", "message": "Slide renamed", "page": page_idx, "name": wanted, "active_page_index": bridge.get_active_page_index()}
