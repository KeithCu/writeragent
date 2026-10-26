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
"""ProximityService — local heading navigation and surroundings discovery.

Ported from mcp-libre services/writer/proximity.py.
Operates on the cached heading tree and bookmark map from TreeService.
"""

from __future__ import annotations

import bisect
import logging
from typing import Any

from plugin.doc.paragraph_search import confirm_paragraph_index, paragraph_entries
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed
from plugin.framework.service import ServiceBase
from plugin.framework.uno_context import uno_same
from plugin.writer.tree import _collect_heading_indices, heading_ref

log = logging.getLogger("writeragent.writer.nav.proximity")


def _anchor_in_body(anchor: Any, text_obj: Any) -> bool:
    """True when *anchor* is a range in the body text, not a header/footer/frame."""
    try:
        return anchor is not None and uno_same(anchor.getText(), text_obj)
    except Exception as exc:
        reraise_if_disposed(exc)
        return False


def _validate_para_index(index: int, total: int) -> None:
    """Reject a paragraph index outside the document before it becomes a wrong window."""
    if index < 0 or index >= total:
        raise ToolExecutionError(
            "Paragraph %d out of range (document has %d paragraphs)" % (index, total)
        )


def _collect_anchored(
    container: Any,
    doc_svc: Any,
    para_ranges: Any,
    text_obj: Any,
    entries: Any,
    start_idx: int,
    end_idx: int,
    builder: Any,
) -> list[dict[str, Any]]:
    items = []
    for name in container.getElementNames():
        try:
            obj = container.getByName(name)
            anchor = obj.getAnchor()
            if not _anchor_in_body(anchor, text_obj):
                continue
            raw_pi = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj, entries=entries)
            pi = confirm_paragraph_index(text_obj, anchor, para_ranges, raw_pi)
            if pi is not None and start_idx <= pi <= end_idx:
                items.append(builder(obj, name, pi))
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("proximity: skipped object %r", name, exc_info=True)
    return items


class ProximityService(ServiceBase):
    """Local proximity navigation on Writer documents."""

    name: str | None = "writer_proximity"

    _doc_svc: Any
    _tree_svc: Any
    _bm_svc: Any

    def __init__(self, services: Any) -> None:
        self._doc_svc = services.document
        self._tree_svc = services.writer_tree
        self._bm_svc = services.writer_bookmarks

    # ==================================================================
    # Flattened tree (ordered heading list with parent pointers)
    # ==================================================================

    def _flatten_tree(self, root: dict[str, Any]) -> list[Any]:
        flat: list[Any] = []
        self._flatten_recurse(root["children"], None, flat)
        return flat

    def _flatten_recurse(self, children: list[Any], parent_entry: dict[str, Any] | None, flat: list[Any]) -> None:
        for child in children:
            entry = {"node": child, "parent": parent_entry}
            flat.append(entry)
            if child.get("children"):
                self._flatten_recurse(child["children"], entry, flat)

    # ==================================================================
    # Heading context (binary search)
    # ==================================================================

    def _find_heading_context(self, flat: list[Any], para_index: int) -> tuple[int | None, bool]:
        if not flat:
            return None, False

        para_indexes = [e["node"]["para_index"] for e in flat]
        pos = bisect.bisect_right(para_indexes, para_index) - 1

        if pos < 0:
            return None, False

        entry = flat[pos]
        node = entry["node"]
        was_heading = node["para_index"] == para_index

        return pos, was_heading

    def _get_heading_chain(self, flat: list[Any], ctx_idx: int | None, bookmark_map: dict[Any, Any]) -> list[dict[str, Any]]:
        chain = []
        entry = flat[ctx_idx] if ctx_idx is not None else None
        while entry is not None:
            node = entry["node"]
            chain.append(heading_ref(node, bookmark_map))
            entry = entry["parent"]
        chain.reverse()
        return chain

    def _build_heading_result(self, node: dict[str, Any], bookmark_map: dict[Any, Any]) -> dict[str, Any]:
        return heading_ref(node, bookmark_map, include_body=True, include_children_count=True, body_default=True)

    # ==================================================================
    # navigate_heading
    # ==================================================================

    def navigate_heading(self, doc: Any, locator: str, direction: str) -> dict[str, Any]:
        """Navigate from locator to a related heading."""
        resolved = self._doc_svc.resolve_locator(doc, locator)
        para_index = resolved.get("para_index")
        if para_index is None:
            raise ToolExecutionError("Cannot resolve locator: %s" % locator)

        para_ranges = self._doc_svc.get_paragraph_ranges(doc)
        _validate_para_index(para_index, len(para_ranges))
        tree = self._tree_svc.build_heading_tree(doc, elements=para_ranges)
        indices = _collect_heading_indices(tree)
        bookmark_map = self._bm_svc.ensure_heading_bookmarks(
            doc, heading_para_indices=indices, para_ranges=para_ranges
        )
        flat = self._flatten_tree(tree)

        if not flat:
            return {"error": "Document has no headings"}

        ctx_idx, was_heading = self._find_heading_context(flat, para_index)

        from_info = {"para_index": para_index, "was_heading": was_heading}
        if ctx_idx is not None:
            ctx_node = flat[ctx_idx]["node"]
            from_info["context_heading"] = ctx_node["text"]
            from_info["context_level"] = ctx_node["level"]
            from_info["context_bookmark"] = bookmark_map.get(ctx_node["para_index"])

        target_entry: dict[str, Any] | None = None
        error_msg = None

        if direction == "next":
            next_idx = (ctx_idx + 1) if ctx_idx is not None else 0
            if next_idx < len(flat):
                target_entry = flat[next_idx]
            else:
                error_msg = "No next heading — already at last heading"

        elif direction == "previous":
            if ctx_idx is None:
                error_msg = "No previous heading — position is before any heading"
            elif not was_heading:
                target_entry = flat[ctx_idx]
            elif ctx_idx > 0:
                target_entry = flat[ctx_idx - 1]
            else:
                error_msg = "No previous heading — already at first heading"

        elif direction == "parent":
            if ctx_idx is None:
                error_msg = "No parent heading — position is before any heading"
            else:
                parent_entry = flat[ctx_idx]["parent"]
                if parent_entry is not None:
                    target_entry = parent_entry
                else:
                    error_msg = "No parent heading — already at top level"

        elif direction == "first_child":
            if ctx_idx is None:
                error_msg = "No child headings — position is before any heading"
            else:
                children = flat[ctx_idx]["node"].get("children", [])
                if children:
                    child_pi = children[0]["para_index"]
                    for entry in flat:
                        if entry["node"]["para_index"] == child_pi:
                            target_entry = entry
                            break
                if target_entry is None and error_msg is None:
                    error_msg = "No child headings under this heading"

        elif direction == "next_sibling":
            if ctx_idx is None:
                error_msg = "No sibling headings — position is before any heading"
            else:
                target_entry = self._find_sibling(flat, ctx_idx, offset=1)
                if target_entry is None:
                    error_msg = "No next sibling heading"

        elif direction == "previous_sibling":
            if ctx_idx is None:
                error_msg = "No sibling headings — position is before any heading"
            else:
                target_entry = self._find_sibling(flat, ctx_idx, offset=-1)
                if target_entry is None:
                    error_msg = "No previous sibling heading"

        else:
            raise ToolExecutionError("Unknown direction: %s. Use: next, previous, parent, first_child, next_sibling, previous_sibling" % direction)

        if error_msg:
            return {"error": error_msg, "from": from_info}

        if target_entry is None:
            return {"error": "Unexpectedly found no target entry.", "from": from_info}

        return {"heading": self._build_heading_result(target_entry["node"], bookmark_map), "from": from_info, "direction": direction}

    def _find_sibling(self, flat: list[Any], ctx_idx: int, offset: int) -> Any | None:
        entry = flat[ctx_idx]
        parent = entry["parent"]
        if parent is None:
            top_level = [e for e in flat if e["parent"] is None]
            for i, e in enumerate(top_level):
                if e is entry:
                    sib_idx = i + offset
                    if 0 <= sib_idx < len(top_level):
                        return top_level[sib_idx]
                    return None
            return None

        siblings = parent["node"].get("children", [])
        current_pi = entry["node"]["para_index"]
        for i, sib in enumerate(siblings):
            if sib["para_index"] == current_pi:
                sib_idx = i + offset
                if 0 <= sib_idx < len(siblings):
                    target_pi = siblings[sib_idx]["para_index"]
                    for e in flat:
                        if e["node"]["para_index"] == target_pi:
                            return e
                return None
        return None

    # ==================================================================
    # get_surroundings
    # ==================================================================

    def get_surroundings(self, doc: Any, locator: str, radius: int = 10, include: list[str] | None = None) -> dict[str, Any]:
        """Discover objects within radius paragraphs of locator."""
        resolved = self._doc_svc.resolve_locator(doc, locator)
        center_idx = resolved.get("para_index")
        if center_idx is None:
            raise ToolExecutionError("Cannot resolve locator: %s" % locator)

        try:
            radius = int(radius)
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("radius must be an integer") from exc
        radius = max(0, min(50, radius))
        if include is None:
            include = ["paragraphs", "images", "tables", "frames", "comments", "headings"]

        para_ranges = self._doc_svc.get_paragraph_ranges(doc)
        total = len(para_ranges)

        _validate_para_index(center_idx, total)

        start_idx = max(0, center_idx - radius)
        end_idx = min(total - 1, center_idx + radius)
        text_obj = doc.getText()

        result = {"center_para_index": center_idx, "range": {"start": start_idx, "end": end_idx}}

        bookmark_map = {}
        if "headings" in include:
            tree = self._tree_svc.build_heading_tree(doc, elements=para_ranges)
            indices = _collect_heading_indices(tree)
            bookmark_map = self._bm_svc.ensure_heading_bookmarks(
                doc, heading_para_indices=indices, para_ranges=para_ranges
            )
            flat = self._flatten_tree(tree)
            ctx_idx, _unused = self._find_heading_context(flat, center_idx)
            result["heading_chain"] = self._get_heading_chain(flat, ctx_idx, bookmark_map)

        if "paragraphs" in include:
            if not bookmark_map:
                bookmark_map = self._bm_svc.get_mcp_bookmark_map(doc, para_ranges=para_ranges)
            paragraphs = []
            for i in range(start_idx, end_idx + 1):
                el = para_ranges[i]
                if not el.supportsService("com.sun.star.text.Paragraph"):
                    continue
                level = el.getPropertyValue("OutlineLevel")
                text = el.getString()
                entry = {"para_index": i, "text": text[:200] if len(text) > 200 else text, "outline_level": level}
                bm = bookmark_map.get(i)
                if bm:
                    entry["bookmark"] = bm
                paragraphs.append(entry)
            result["paragraphs"] = paragraphs

        entries = None
        if any(k in include for k in ("images", "tables", "frames", "comments")):
            entries = paragraph_entries(para_ranges)

        if "images" in include:
            def _image_entry(g: Any, name: str, pi: int) -> dict[str, Any]:
                size = g.getPropertyValue("Size")
                return {"name": name, "paragraph_index": pi, "title": g.getPropertyValue("Title"), "width_mm": size.Width // 100, "height_mm": size.Height // 100}

            result["images"] = _collect_anchored(
                doc.getGraphicObjects(), self._doc_svc, para_ranges, text_obj, entries, start_idx, end_idx, _image_entry
            )

        if "tables" in include:
            result["tables"] = _collect_anchored(
                doc.getTextTables(),
                self._doc_svc,
                para_ranges,
                text_obj,
                entries,
                start_idx,
                end_idx,
                lambda t, name, pi: {"name": name, "paragraph_index": pi, "rows": t.getRows().getCount(), "cols": t.getColumns().getCount()},
            )

        if "frames" in include:
            def _frame_entry(fr: Any, name: str, pi: int) -> dict[str, Any]:
                size = fr.getPropertyValue("Size")
                return {"name": name, "paragraph_index": pi, "width_mm": size.Width // 100, "height_mm": size.Height // 100}

            result["frames"] = _collect_anchored(
                doc.getTextFrames(), self._doc_svc, para_ranges, text_obj, entries, start_idx, end_idx, _frame_entry
            )

        if "comments" in include:
            comments = []
            try:
                fields = doc.getTextFields()
                enum = fields.createEnumeration()
                while enum.hasMoreElements() is True:
                    try:
                        field = enum.nextElement()
                        if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                            continue
                        anchor = field.getAnchor()
                        if not _anchor_in_body(anchor, text_obj):
                            continue
                        raw_pi = self._doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj, entries=entries)
                        pi = confirm_paragraph_index(text_obj, anchor, para_ranges, raw_pi)
                        if pi is None or not (start_idx <= pi <= end_idx):
                            continue
                        comments.append(
                            {
                                "author": field.getPropertyValue("Author"),
                                "content": (field.getPropertyValue("Content") or "")[:200],
                                "paragraph_index": pi,
                                "resolved": field.getPropertyValue("Resolved"),
                            }
                        )
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        log.debug("proximity: skipped annotation", exc_info=True)
            except Exception as exc:
                reraise_if_disposed(exc)
                log.debug("proximity: comment enumeration failed", exc_info=True)
            result["comments"] = comments

        return result
