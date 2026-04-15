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

import bisect
import logging

from plugin.framework.errors import ToolExecutionError
from plugin.framework.service_base import ServiceBase
import typing


log = logging.getLogger("writeragent.writer.nav.proximity")


class ProximityService(ServiceBase):
    """Local proximity navigation on Writer documents."""

    name = "writer_proximity"

    def __init__(self, services):
        self._doc_svc = services.document
        self._tree_svc = services.writer_tree
        self._bm_svc = services.writer_bookmarks
        events = services.events
        self._flat_cache = {}  # doc_key -> [flat entries]
        events.subscribe("document:cache_invalidated",
                         self._on_cache_invalidated)

    def _on_cache_invalidated(self, doc=None, **_kw):
        if doc is None:
            self._flat_cache.clear()
        else:
            self._flat_cache.pop(self._doc_svc.doc_key(doc), None)

    # ==================================================================
    # Flattened tree (ordered heading list with parent pointers)
    # ==================================================================

    def _flatten_tree(self, root, doc):
        key = self._doc_svc.doc_key(doc)
        if key in self._flat_cache:
            return self._flat_cache[key]

        flat = []
        self._flatten_recurse(root["children"], None, flat)
        self._flat_cache[key] = flat
        return flat

    def _flatten_recurse(self, children, parent_entry, flat):
        for child in children:
            entry = {"node": child, "parent": parent_entry}
            flat.append(entry)
            if child.get("children"):
                self._flatten_recurse(child["children"], entry, flat)

    # ==================================================================
    # Heading context (binary search)
    # ==================================================================

    def _find_heading_context(self, flat, para_index):
        if not flat:
            return None, {"was_heading": False}

        para_indexes = [e["node"]["para_index"] for e in flat]
        pos = bisect.bisect_right(para_indexes, para_index) - 1

        if pos < 0:
            return None, {"was_heading": False}

        entry = flat[pos]
        node = entry["node"]
        was_heading = (node["para_index"] == para_index)

        return pos, {
            "was_heading": was_heading,
            "heading_node": node,
            "heading_text": node.get("text", ""),
            "heading_level": node.get("level", 0),
        }

    def _get_heading_chain(self, flat, ctx_idx, bookmark_map):
        chain = []
        entry = flat[ctx_idx] if ctx_idx is not None else None
        while entry is not None:
            node = entry["node"]
            chain.append({
                "level": node["level"],
                "text": node["text"],
                "para_index": node["para_index"],
                "bookmark": bookmark_map.get(node["para_index"]),
            })
            entry = entry["parent"]
        chain.reverse()
        return chain

    def _build_heading_result(self, node, bookmark_map):
        return {
            "level": node["level"],
            "text": node["text"],
            "para_index": node["para_index"],
            "bookmark": bookmark_map.get(node["para_index"]),
            "body_paragraphs": node.get("body_paragraphs", 0),
            "children_count": len(node.get("children", [])),
        }

    # ==================================================================
    # navigate_heading
    # ==================================================================

    def navigate_heading(self, doc, locator, direction):
        """Navigate from locator to a related heading."""
        resolved = self._doc_svc.resolve_locator(doc, locator)
        para_index = resolved.get("para_index")
        if para_index is None:
            raise ToolExecutionError("Cannot resolve locator: %s" % locator)

        tree = self._tree_svc.build_heading_tree(doc)
        bookmark_map = self._bm_svc.ensure_heading_bookmarks(doc)
        flat = self._flatten_tree(tree, doc)

        if not flat:
            return {"error": "Document has no headings"}

        ctx_idx, ctx_info = self._find_heading_context(flat, para_index)

        from_info = {
            "para_index": para_index,
            "was_heading": ctx_info["was_heading"],
        }
        if ctx_idx is not None:
            ctx_node = flat[ctx_idx]["node"]
            from_info["context_heading"] = ctx_node["text"]
            from_info["context_level"] = ctx_node["level"]
            from_info["context_bookmark"] = bookmark_map.get(
                ctx_node["para_index"])

        target_entry = typing.cast("typing.Optional[typing.Dict[str, typing.Any]]", None)
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
            elif not ctx_info["was_heading"] and ctx_idx >= 0:
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
            raise ToolExecutionError(
                "Unknown direction: %s. Use: next, previous, parent, "
                "first_child, next_sibling, previous_sibling" % direction)

        if error_msg:
            return {"error": error_msg, "from": from_info}

        if target_entry is None:
            return {"error": "Unexpectedly found no target entry.", "from": from_info}

        return {
            "heading": self._build_heading_result(
                target_entry["node"], bookmark_map),
            "from": from_info,
            "direction": direction,
        }

    def _find_sibling(self, flat, ctx_idx, offset):
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

    def get_surroundings(self, doc, locator, radius=10, include=None):
        """Discover objects within radius paragraphs of locator."""
        resolved = self._doc_svc.resolve_locator(doc, locator)
        center_idx = resolved.get("para_index")
        if center_idx is None:
            raise ToolExecutionError("Cannot resolve locator: %s" % locator)

        radius = max(1, min(50, radius))
        if include is None:
            include = ["paragraphs", "images", "tables",
                       "frames", "comments", "headings"]

        para_ranges = self._doc_svc.get_paragraph_ranges(doc)
        total = len(para_ranges)

        if center_idx >= total:
            raise ToolExecutionError(
                "Paragraph %d out of range (document has %d paragraphs)"
                % (center_idx, total))

        start_idx = max(0, center_idx - radius)
        end_idx = min(total - 1, center_idx + radius)
        text_obj = doc.getText()

        result = {
            "center_para_index": center_idx,
            "range": {"start": start_idx, "end": end_idx},
        }

        bookmark_map = {}
        if "headings" in include:
            tree = self._tree_svc.build_heading_tree(doc)
            bookmark_map = self._bm_svc.ensure_heading_bookmarks(doc)
            flat = self._flatten_tree(tree, doc)
            ctx_idx, _ = self._find_heading_context(flat, center_idx)
            result["heading_chain"] = self._get_heading_chain(
                flat, ctx_idx, bookmark_map)

        if "paragraphs" in include:
            if not bookmark_map:
                bookmark_map = self._bm_svc.get_mcp_bookmark_map(doc)
            paragraphs = []
            for i in range(start_idx, end_idx + 1):
                el = para_ranges[i]
                if not el.supportsService("com.sun.star.text.Paragraph"):
                    continue
                level = 0
                try:
                    level = el.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                text = el.getString()
                entry = {
                    "para_index": i,
                    "text": text[:200] if len(text) > 200 else text,
                    "outline_level": level,
                }
                bm = bookmark_map.get(i)
                if bm:
                    entry["bookmark"] = bm
                paragraphs.append(entry)
            result["paragraphs"] = paragraphs

        if "images" in include and hasattr(doc, "getGraphicObjects"):
            images = []
            graphics = doc.getGraphicObjects()
            for name in graphics.getElementNames():
                try:
                    g = graphics.getByName(name)
                    anchor = g.getAnchor()
                    pi = self._doc_svc.find_paragraph_for_range(
                        anchor, para_ranges, text_obj)
                    if start_idx <= pi <= end_idx:
                        size = g.getPropertyValue("Size")
                        images.append({
                            "name": name,
                            "paragraph_index": pi,
                            "title": g.getPropertyValue("Title"),
                            "width_mm": size.Width // 100,
                            "height_mm": size.Height // 100,
                        })
                except Exception:
                    pass
            result["images"] = images

        if "tables" in include and hasattr(doc, "getTextTables"):
            tables = []
            text_tables = doc.getTextTables()
            for name in text_tables.getElementNames():
                try:
                    t = text_tables.getByName(name)
                    anchor = t.getAnchor()
                    pi = self._doc_svc.find_paragraph_for_range(
                        anchor, para_ranges, text_obj)
                    if start_idx <= pi <= end_idx:
                        tables.append({
                            "name": name,
                            "paragraph_index": pi,
                            "rows": t.getRows().getCount(),
                            "cols": t.getColumns().getCount(),
                        })
                except Exception:
                    pass
            result["tables"] = tables

        if "frames" in include and hasattr(doc, "getTextFrames"):
            frames = []
            text_frames = doc.getTextFrames()
            for fname in text_frames.getElementNames():
                try:
                    fr = text_frames.getByName(fname)
                    anchor = fr.getAnchor()
                    pi = self._doc_svc.find_paragraph_for_range(
                        anchor, para_ranges, text_obj)
                    if start_idx <= pi <= end_idx:
                        size = fr.getPropertyValue("Size")
                        frames.append({
                            "name": fname,
                            "paragraph_index": pi,
                            "width_mm": size.Width // 100,
                            "height_mm": size.Height // 100,
                        })
                except Exception:
                    pass
            result["frames"] = frames

        if "comments" in include:
            comments = []
            try:
                fields = doc.getTextFields()
                enum = fields.createEnumeration()
                while enum.hasMoreElements():
                    field = enum.nextElement()
                    if not field.supportsService(
                            "com.sun.star.text.textfield.Annotation"):
                        continue
                    try:
                        author = field.getPropertyValue("Author")
                    except Exception:
                        author = ""
                    if author == "MCP-AI":
                        continue
                    anchor = field.getAnchor()
                    pi = self._doc_svc.find_paragraph_for_range(
                        anchor, para_ranges, text_obj)
                    if start_idx <= pi <= end_idx:
                        content = field.getPropertyValue("Content")
                        resolved_flag = False
                        try:
                            resolved_flag = field.getPropertyValue("Resolved")
                        except Exception:
                            pass
                        comments.append({
                            "author": author,
                            "content": content[:200],
                            "paragraph_index": pi,
                            "resolved": resolved_flag,
                        })
            except Exception:
                pass
            result["comments"] = comments

        return result
