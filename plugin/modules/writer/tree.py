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
"""TreeService — heading tree, content strategies, AI annotations.

Ported from mcp-libre services/writer/tree.py.
"""

import logging

from plugin.framework.errors import ToolExecutionError

log = logging.getLogger("writeragent.writer.nav.tree")


class TreeService:
    """Heading tree navigation with per-document caching."""

    def __init__(self, doc_svc, bm_svc, events):
        self._doc_svc = doc_svc
        self._bm_svc = bm_svc
        self._tree_cache = {}  # doc_key -> root node
        self._ai_summary_cache = {}  # doc_key -> {para_index: summary}
        events.subscribe("document:cache_invalidated",
                         self._on_cache_invalidated)

    def _on_cache_invalidated(self, doc=None, **_kw):
        if doc is None:
            self._tree_cache.clear()
            self._ai_summary_cache.clear()
        else:
            key = self._doc_svc.doc_key(doc)
            self._tree_cache.pop(key, None)
            self._ai_summary_cache.pop(key, None)

    # ── Tree building ──────────────────────────────────────────────

    def build_heading_tree(self, doc):
        """Build heading tree from paragraph enumeration. Single pass.

        Returns root node dict:
            {"level": 0, "text": "root", "para_index": -1,
             "children": [...], "body_paragraphs": N}
        """
        key = self._doc_svc.doc_key(doc)
        if key in self._tree_cache:
            return self._tree_cache[key]

        text = doc.getText()
        enum = text.createEnumeration()
        root = {"level": 0, "text": "root", "para_index": -1,
                "children": [], "body_paragraphs": 0}
        stack = [root]
        para_index = 0

        while enum.hasMoreElements():
            element = enum.nextElement()
            is_para = element.supportsService(
                "com.sun.star.text.Paragraph")
            is_table = element.supportsService(
                "com.sun.star.text.TextTable")

            if is_para:
                outline_level = 0
                try:
                    outline_level = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                if outline_level > 0:
                    while (len(stack) > 1
                           and stack[-1]["level"] >= outline_level):
                        stack.pop()
                    node = {"level": outline_level,
                            "text": element.getString(),
                            "para_index": para_index,
                            "children": [], "body_paragraphs": 0}
                    stack[-1]["children"].append(node)
                    stack.append(node)
                else:
                    stack[-1]["body_paragraphs"] += 1
            elif is_table:
                stack[-1]["body_paragraphs"] += 1

            para_index += 1
            self._doc_svc.yield_to_gui()

        self._tree_cache[key] = root
        return root

    def _count_all_children(self, node):
        count = len(node.get("children", []))
        for child in node.get("children", []):
            if "children" in child:
                count += self._count_all_children(child)
        return count + node.get("body_paragraphs", 0)

    def _find_node_by_para_index(self, node, para_index):
        if node.get("para_index") == para_index:
            return node
        for child in node.get("children", []):
            found = self._find_node_by_para_index(child, para_index)
            if found is not None:
                return found
        return None

    # ── Content strategies ─────────────────────────────────────────

    def _get_body_preview(self, doc, heading_para_index, max_chars=100):
        text = doc.getText()
        enum = text.createEnumeration()
        idx = 0
        preview_parts = []
        found_heading = (heading_para_index == -1)

        while enum.hasMoreElements():
            element = enum.nextElement()
            is_para = element.supportsService(
                "com.sun.star.text.Paragraph")
            if idx == heading_para_index:
                found_heading = True
                idx += 1
                continue
            if found_heading and is_para:
                outline_level = 0
                try:
                    outline_level = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                if outline_level > 0:
                    break
                para_text = element.getString().strip()
                if para_text:
                    preview_parts.append(para_text)
                    if sum(len(p) for p in preview_parts) >= max_chars:
                        break
            idx += 1

        full_preview = " ".join(preview_parts)
        if len(full_preview) > max_chars:
            full_preview = full_preview[:max_chars] + "..."
        return full_preview

    def _get_full_body_text(self, doc, heading_para_index):
        text = doc.getText()
        enum = text.createEnumeration()
        idx = 0
        parts = []
        found_heading = (heading_para_index == -1)

        while enum.hasMoreElements():
            element = enum.nextElement()
            is_para = element.supportsService(
                "com.sun.star.text.Paragraph")
            if idx == heading_para_index:
                found_heading = True
                idx += 1
                continue
            if found_heading and is_para:
                outline_level = 0
                try:
                    outline_level = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                if outline_level > 0:
                    break
                parts.append(element.getString())
            idx += 1

        return "\n".join(parts)

    def get_ai_summaries_map(self, doc):
        """Build {para_index: summary} map from MCP-AI annotations."""
        key = self._doc_svc.doc_key(doc)
        if key in self._ai_summary_cache:
            return self._ai_summary_cache[key]

        summaries = {}
        try:
            fields_supplier = doc.getTextFields()
            enum = fields_supplier.createEnumeration()
            para_ranges = self._doc_svc.get_paragraph_ranges(doc)

            while enum.hasMoreElements():
                field = enum.nextElement()
                if not field.supportsService(
                        "com.sun.star.text.textfield.Annotation"):
                    continue
                try:
                    author = field.getPropertyValue("Author")
                except Exception:
                    continue
                if author != "MCP-AI":
                    continue
                content = field.getPropertyValue("Content")
                anchor = field.getAnchor()
                para_idx = self._doc_svc.find_paragraph_for_range(
                    anchor, para_ranges, doc.getText())
                if para_idx >= 0:
                    summaries[para_idx] = content
        except Exception as e:
            log.error("Failed to get AI summaries: %s", e)

        self._ai_summary_cache[key] = summaries
        return summaries

    def _apply_content_strategy(self, node, doc, ai_summaries, strategy,
                                max_chars=100):
        para_idx = node.get("para_index", -1)
        if strategy in ("none", "heading_only"):
            pass
        elif strategy == "ai_summary_first":
            if para_idx in ai_summaries:
                node["ai_summary"] = ai_summaries[para_idx]
            else:
                node["body_preview"] = self._get_body_preview(
                    doc, para_idx, max_chars)
        elif strategy == "first_lines":
            node["body_preview"] = self._get_body_preview(
                doc, para_idx, max_chars)
            if para_idx in ai_summaries:
                node["ai_summary"] = ai_summaries[para_idx]
        elif strategy == "full":
            node["body_text"] = self._get_full_body_text(doc, para_idx)

    def _serialize_tree_node(self, child, doc, ai_summaries,
                             content_strategy, depth, current_depth=1,
                             bookmark_map=None):
        node = {
            "type": "heading",
            "level": child["level"],
            "text": child["text"],
            "para_index": child["para_index"],
            "bookmark": (bookmark_map or {}).get(child["para_index"]),
            "children_count": self._count_all_children(child),
            "body_paragraphs": child["body_paragraphs"],
        }
        self._apply_content_strategy(
            node, doc, ai_summaries, content_strategy)
        if depth == 0 or current_depth < depth:
            if child.get("children"):
                node["children"] = [
                    self._serialize_tree_node(
                        sub, doc, ai_summaries, content_strategy,
                        depth, current_depth + 1, bookmark_map)
                    for sub in child["children"]
                ]
        return node

    # ── Public tree API ────────────────────────────────────────────

    def get_document_tree(self, doc, content_strategy="first_lines",
                          depth=1):
        """Get serialized document tree with content strategies."""
        tree = self.build_heading_tree(doc)
        bookmark_map = self._bm_svc.ensure_heading_bookmarks(doc)
        ai_summaries = (
            self.get_ai_summaries_map(doc)
            if content_strategy in ("ai_summary_first", "first_lines")
            else {})

        children = [
            self._serialize_tree_node(
                child, doc, ai_summaries, content_strategy,
                depth, bookmark_map=bookmark_map)
            for child in tree["children"]
        ]

        # Count total paragraphs
        text = doc.getText()
        enum = text.createEnumeration()
        total = 0
        while enum.hasMoreElements():
            enum.nextElement()
            total += 1

        try:
            self._doc_svc.annotate_pages(children, doc)
        except Exception:
            pass

        page_count = self._doc_svc.get_page_count(doc)

        return {
            "status": "ok",
            "content_strategy": content_strategy,
            "depth": depth,
            "children": children,
            "body_before_first_heading": tree["body_paragraphs"],
            "total_paragraphs": total,
            "page_count": page_count,
        }

    def get_heading_children(self, doc, heading_para_index=None,
                             heading_bookmark=None, locator=None,
                             content_strategy="first_lines", depth=1):
        """Get children of a heading (body paragraphs + sub-headings)."""
        if locator is not None and heading_para_index is None:
            resolved = self._doc_svc.resolve_locator(doc, locator)
            heading_para_index = resolved.get("para_index")
        elif heading_bookmark is not None and heading_para_index is None:
            if not hasattr(doc, "getBookmarks"):
                raise ToolExecutionError("Document doesn't support bookmarks")
            bm_sup = doc.getBookmarks()
            if not bm_sup.hasByName(heading_bookmark):
                raise ToolExecutionError("Bookmark '%s' not found"
                                 % heading_bookmark)
            bm = bm_sup.getByName(heading_bookmark)
            anchor = bm.getAnchor()
            para_ranges = self._doc_svc.get_paragraph_ranges(doc)
            heading_para_index = self._doc_svc.find_paragraph_for_range(
                anchor, para_ranges, doc.getText())

        if heading_para_index is None:
            raise ToolExecutionError("Provide locator, heading_para_index, "
                             "or heading_bookmark")

        tree = self.build_heading_tree(doc)
        bookmark_map = self._bm_svc.ensure_heading_bookmarks(doc)
        target = self._find_node_by_para_index(tree, heading_para_index)
        if target is None:
            raise ToolExecutionError("Heading at paragraph %d not found"
                             % heading_para_index)

        ai_summaries = (
            self.get_ai_summaries_map(doc)
            if content_strategy in ("ai_summary_first", "first_lines")
            else {})

        children = []
        text = doc.getText()
        enum = text.createEnumeration()
        idx = 0
        found_heading = False
        parent_level = target["level"]

        while enum.hasMoreElements():
            element = enum.nextElement()
            is_para = element.supportsService(
                "com.sun.star.text.Paragraph")
            if idx == heading_para_index:
                found_heading = True
                idx += 1
                continue
            if found_heading and is_para:
                outline_level = 0
                try:
                    outline_level = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                if outline_level > 0 and outline_level <= parent_level:
                    break
                if outline_level > 0:
                    break
                para_text = element.getString()
                preview = (para_text[:100] + "..."
                           if len(para_text) > 100 else para_text)
                if content_strategy == "full":
                    children.append({"type": "body",
                                     "para_index": idx,
                                     "text": para_text})
                elif content_strategy not in ("none", "heading_only"):
                    children.append({"type": "body",
                                     "para_index": idx,
                                     "preview": preview})
                else:
                    children.append({"type": "body",
                                     "para_index": idx})
            idx += 1
            self._doc_svc.yield_to_gui()

        for child in target["children"]:
            node = self._serialize_tree_node(
                child, doc, ai_summaries, content_strategy,
                depth, bookmark_map=bookmark_map)
            children.append(node)

        return {
            "status": "ok",
            "parent": {
                "level": target["level"],
                "text": target["text"],
                "para_index": target["para_index"],
                "bookmark": bookmark_map.get(target["para_index"]),
            },
            "content_strategy": content_strategy,
            "depth": depth,
            "children": children,
        }

    # ── AI annotations ─────────────────────────────────────────────

    def add_ai_summary(self, doc, para_index=None, summary="",
                       locator=None):
        """Add an MCP-AI annotation at a paragraph."""
        if locator is not None and para_index is None:
            resolved = self._doc_svc.resolve_locator(doc, locator)
            para_index = resolved.get("para_index")
        if para_index is None:
            raise ToolExecutionError("Provide locator or para_index")

        doc_text = doc.getText()
        self._remove_ai_annotation_at(doc, para_index)

        target, _ = self._doc_svc.find_paragraph_element(doc, para_index)
        if target is None:
            raise ToolExecutionError("Paragraph %d not found" % para_index)

        annotation = doc.createInstance(
            "com.sun.star.text.textfield.Annotation")
        annotation.setPropertyValue("Author", "MCP-AI")
        annotation.setPropertyValue("Content", summary)
        cursor = doc_text.createTextCursorByRange(target.getStart())
        doc_text.insertTextContent(cursor, annotation, False)

        self._ai_summary_cache.pop(
            self._doc_svc.doc_key(doc), None)

        try:
            if doc.hasLocation():
                doc.store()
        except Exception:
            pass

        return {"status": "ok",
                "message": "Added AI summary at paragraph %d" % para_index,
                "para_index": para_index,
                "summary_length": len(summary)}

    def get_ai_summaries(self, doc):
        """List all MCP-AI annotations."""
        summaries_map = self.get_ai_summaries_map(doc)
        summaries = [{"para_index": idx, "summary": text}
                     for idx, text in sorted(summaries_map.items())]
        return {"status": "ok", "summaries": summaries,
                "count": len(summaries)}

    def remove_ai_summary(self, doc, para_index=None, locator=None):
        """Remove MCP-AI annotation at a paragraph."""
        if locator is not None and para_index is None:
            resolved = self._doc_svc.resolve_locator(doc, locator)
            para_index = resolved.get("para_index")
        if para_index is None:
            raise ToolExecutionError("Provide locator or para_index")
        removed = self._remove_ai_annotation_at(doc, para_index)
        self._ai_summary_cache.pop(
            self._doc_svc.doc_key(doc), None)
        if removed:
            try:
                if doc.hasLocation():
                    doc.store()
            except Exception:
                pass
        return {"status": "ok", "removed": removed,
                "para_index": para_index}

    def _remove_ai_annotation_at(self, doc, para_index):
        try:
            fields = doc.getTextFields()
            enum = fields.createEnumeration()
            para_ranges = self._doc_svc.get_paragraph_ranges(doc)
            text_obj = doc.getText()
            while enum.hasMoreElements():
                field = enum.nextElement()
                if not field.supportsService(
                        "com.sun.star.text.textfield.Annotation"):
                    continue
                try:
                    author = field.getPropertyValue("Author")
                except Exception:
                    continue
                if author != "MCP-AI":
                    continue
                anchor = field.getAnchor()
                idx = self._doc_svc.find_paragraph_for_range(
                    anchor, para_ranges, text_obj)
                if idx == para_index:
                    text_obj.removeTextContent(field)
                    return True
        except Exception as e:
            log.error("Failed to remove AI annotation: %s", e)
        return False

    # ── Locator resolution (called by document.resolve_locator) ────

    def resolve_writer_locator(self, doc, loc_type, loc_value):
        """Resolve Writer-specific locators to {para_index: N}."""
        if loc_type == "bookmark":
            return self._resolve_bookmark_locator(doc, loc_value)

        if loc_type == "page":
            page_num = int(loc_value)
            try:
                controller = doc.getCurrentController()
                vc = controller.getViewCursor()
                saved = doc.getText().createTextCursorByRange(
                    vc.getStart())
                doc.lockControllers()
                try:
                    vc.jumpToPage(page_num)
                    vc.jumpToStartOfPage()
                    anchor = vc.getStart()
                finally:
                    vc.gotoRange(saved, False)
                    doc.unlockControllers()
                para_ranges = self._doc_svc.get_paragraph_ranges(doc)
                text_obj = doc.getText()
                para_idx = self._doc_svc.find_paragraph_for_range(
                    anchor, para_ranges, text_obj)
                return {"para_index": para_idx}
            except Exception as e:
                raise ToolExecutionError(
                    "Cannot resolve page:%s — %s" % (loc_value, e))

        if loc_type == "section":
            if not hasattr(doc, "getTextSections"):
                raise ToolExecutionError("Document does not support sections")
            sections = doc.getTextSections()
            if not sections.hasByName(loc_value):
                raise ToolExecutionError("Section '%s' not found" % loc_value)
            section = sections.getByName(loc_value)
            anchor = section.getAnchor()
            para_ranges = self._doc_svc.get_paragraph_ranges(doc)
            text_obj = doc.getText()
            para_idx = self._doc_svc.find_paragraph_for_range(
                anchor, para_ranges, text_obj)
            return {"para_index": para_idx, "section_name": loc_value}

        if loc_type == "heading":
            parts = [int(p) for p in loc_value.split(".")]
            tree = self.build_heading_tree(doc)
            node = tree
            for part in parts:
                children = node.get("children", [])
                if part < 1 or part > len(children):
                    raise ToolExecutionError(
                        "Heading index %d out of range (1..%d) "
                        "in 'heading:%s'" % (part, len(children), loc_value))
                node = children[part - 1]
            return {"para_index": node["para_index"]}

        if loc_type == "heading_text":
            result = self._find_heading_by_text(doc, loc_value)
            if result is None:
                raise ToolExecutionError(
                    "No heading matching '%s' found" % loc_value)
            return {"para_index": result["para_index"]}

        raise ToolExecutionError("Unknown Writer locator type: '%s'" % loc_type)

    def _resolve_bookmark_locator(self, doc, bookmark_name):
        if not hasattr(doc, "getBookmarks"):
            raise ToolExecutionError("Document doesn't support bookmarks")
        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(bookmark_name):
            hint = "Bookmark '%s' not found." % bookmark_name
            if bookmark_name.startswith("_mcp_"):
                hint += (" Use heading_text:<text> locator for resilient "
                         "heading addressing, or call get_document_tree "
                         "to refresh bookmarks.")
                existing = [n for n in bookmarks.getElementNames()
                            if n.startswith("_mcp_")]
                if existing:
                    hint += (" Existing bookmarks: "
                             + ", ".join(existing[:10]))
            raise ToolExecutionError(hint)
        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()
        para_ranges = self._doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = self._doc_svc.find_paragraph_for_range(
            anchor, para_ranges, text_obj)
        return {"para_index": para_idx}

    def _find_heading_by_text(self, doc, search_text):
        """Find heading by text (case-insensitive, fuzzy)."""
        tree = self.build_heading_tree(doc)
        bookmark_map = self._bm_svc.get_mcp_bookmark_map(doc)
        headings = self._flatten_headings(tree)

        search_lower = search_text.lower().strip()
        if not search_lower:
            return None

        best_prefix = None
        best_substring = None

        for h in headings:
            text_lower = h["text"].lower()
            text_stripped = text_lower.strip()

            # Exact match - highest priority, return immediately
            if text_stripped == search_lower:
                h["bookmark"] = bookmark_map.get(h["para_index"])
                return h

            # Prefix match - second priority
            if best_prefix is None and text_stripped.startswith(search_lower):
                best_prefix = h

            # Substring match - third priority
            if best_substring is None and search_lower in text_lower:
                best_substring = h

        if best_prefix:
            best_prefix["bookmark"] = bookmark_map.get(best_prefix["para_index"])
            return best_prefix

        if best_substring:
            best_substring["bookmark"] = bookmark_map.get(best_substring["para_index"])
            return best_substring

        return None

    def _flatten_headings(self, node):
        result = []
        for child in node.get("children", []):
            result.append({
                "text": child["text"],
                "para_index": child["para_index"],
                "level": child["level"],
            })
            result.extend(self._flatten_headings(child))
        return result
