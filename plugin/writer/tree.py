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
"""TreeService — heading tree and content strategies.

Ported from mcp-libre services/writer/tree.py.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, cast

from plugin.framework.errors import ToolExecutionError
from plugin.framework.service import ServiceBase
from plugin.doc.document_helpers import is_cacheable_doc_key
from plugin.doc.paragraph_search import confirm_paragraph_index
from plugin.doc.text_helpers import (
    build_heading_tree,
    find_heading_by_chapter_number,
    get_string_without_tracked_deletions,
    iter_heading_nodes,
    with_view_cursor_left_body_locked,
)


log = logging.getLogger("writeragent.writer.nav.tree")


def _bookmark_anchor_not_placed(bookmark_name: str) -> ToolExecutionError:
    """Bookmark name exists, but its anchor is not a paragraph in this document."""
    return ToolExecutionError(
        "Bookmark '%s' anchor is not in the document. "
        "It may be stale after an edit, save, or bookmark_cleanup. "
        "Use heading_text:<text> or call get_document_tree to refresh bookmarks."
        % bookmark_name
    )


def _collect_heading_indices(node: dict[str, Any]) -> list[int]:
    """Heading paragraph indices in document order (iter_heading_nodes yields them ordered)."""
    return [n["para_index"] for n in iter_heading_nodes(node) if n.get("para_index", -1) >= 0]


def _preview_text(text: str, max_chars: int = 100) -> str:
    """Return text truncated with ellipsis when longer than max_chars."""
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _iter_body_paragraphs(para_ranges: list[Any], start_idx: int, end_idx: int):
    for idx, element in enumerate(para_ranges[start_idx:end_idx], start_idx):
        if element.supportsService("com.sun.star.text.Paragraph"):
            yield idx, element


def _named_doc_item(doc: Any, getter: str, unsupported: str, missing: str, name: str) -> Any:
    if not hasattr(doc, getter):
        raise ToolExecutionError(unsupported)
    container = getattr(doc, getter)()
    if not container.hasByName(name):
        raise ToolExecutionError(missing % name)
    return container.getByName(name)


def heading_ref(
    node: dict[str, Any],
    bookmark_map: dict[Any, Any] | None = None,
    *,
    include_body: bool = False,
    include_children_count: bool = False,
    include_chapter: bool = False,
) -> dict[str, Any]:
    para_index = node["para_index"]
    ref: dict[str, Any] = {}
    ref["level"] = node["level"]
    ref["text"] = node["text"]
    ref["para_index"] = para_index
    ref["bookmark"] = (bookmark_map or {}).get(para_index)
    if include_children_count:
        ref["children_count"] = len(node.get("children") or [])
    if include_body:
        ref["body_paragraphs"] = node.get("body_paragraphs", 0)
    if include_chapter and node.get("chapter_number"):
        ref["chapter_number"] = node["chapter_number"]
    return ref


class _TreeContext(NamedTuple):
    para_ranges: list[Any]
    tree: dict[str, Any]
    heading_indices: list[int]
    next_heading_map: dict[int, int]
    bookmark_map: dict[int, str]


class TreeService(ServiceBase):
    """Heading tree navigation with per-document caching."""

    name: str | None = "writer_tree"

    _doc_svc: Any
    _bm_svc: Any

    def __init__(self, services: Any) -> None:
        self._doc_svc = services.document
        self._bm_svc = services.writer_bookmarks
        events = services.events
        self._tree_cache: dict[Any, tuple[int | None, dict[str, Any]]] = {}  # doc_key -> (fingerprint, root)
        events.subscribe("document:cache_invalidated", self._on_cache_invalidated)

    def _on_cache_invalidated(self, doc: Any | None = None, key: Any | None = None, **_kw: Any) -> None:
        # Prefer the stored key so close/unload can pop without calling
        # doc_key() on a disposed model. key= must be checked before
        # doc is None (emit(key=...) leaves doc defaulted to None).
        if key is None and doc is not None:
            key = self._doc_svc.doc_key(doc)
        if key is None:
            self._tree_cache.clear()
        else:
            self._tree_cache.pop(key, None)


    # ── Tree building ──────────────────────────────────────────────

    def build_heading_tree(self, doc: Any, elements: list[Any] | None = None) -> dict[str, Any]:
        """Build heading tree from paragraph enumeration. Single pass.

        Returns root node dict:
            {"level": 0, "text": "root", "para_index": -1,
             "children": [...], "body_paragraphs": N}

        Heading children may include ``chapter_number`` (Chapter Numbering
        paint label). The key is omitted when numbering is off.
        """
        key = self._doc_svc.doc_key(doc)
        # Cheap stale-cache check when XModifyListener misses a programmatic edit.
        # Windows leftover Writer reuse (GHA 35466498641) can leave a live
        # RuntimeUID listener that does not fire on ``insertString``;
        # ``CharacterCount`` still moves. ``None`` means no signal — keep
        # listener-only caching (WriterDocStub / missing property).
        count = getattr(doc, "CharacterCount", None)
        fingerprint = int(count) if count is not None else None
        if is_cacheable_doc_key(key) and key in self._tree_cache:
            cached_fp, cached_root = self._tree_cache[key]
            # No fingerprint (stubs) → listener-only. Same count → still valid.
            # A moved CharacterCount means the modify listener missed the edit.
            if fingerprint is None or fingerprint == cached_fp:
                return cached_root

        root = build_heading_tree(doc, elements=elements)
        if is_cacheable_doc_key(key):
            self._tree_cache[key] = (fingerprint, root)
        return root

    def heading_block_body_count(self, node: dict[str, Any]) -> int:
        count = len(node.get("children", []))
        for child in node.get("children", []):
            count += self.heading_block_body_count(child)
        return count + node.get("body_paragraphs", 0)

    def find_heading_by_para_index(self, node: dict[str, Any], para_index: int) -> dict[str, Any] | None:
        if node.get("para_index") == para_index:
            return node
        found = next((n for n in iter_heading_nodes(node) if n.get("para_index") == para_index), None)
        return cast("dict[str, Any]", found) if found is not None else None

    # ── Content strategies ─────────────────────────────────────────

    def _apply_content_strategy(
        self,
        node: dict[str, Any],
        strategy: str,
        *,
        para_ranges: list[Any],
        next_heading_map: dict[int, int],
    ) -> None:
        if strategy in ("first_lines", "full"):
            para_idx = node.get("para_index", -1)
            next_idx = next_heading_map.get(para_idx, len(para_ranges))
            if strategy == "first_lines":
                preview_parts = []
                current_len = 0
                for _idx, element in _iter_body_paragraphs(para_ranges, para_idx + 1, next_idx):
                    para_text = get_string_without_tracked_deletions(element).strip()
                    if para_text:
                        preview_parts.append(para_text)
                        current_len += len(para_text)
                        if current_len >= 100:
                            break
                full_preview = " ".join(preview_parts)
                node["body_preview"] = _preview_text(full_preview)
            else:
                parts = [
                    get_string_without_tracked_deletions(element)
                    for _idx, element in _iter_body_paragraphs(para_ranges, para_idx + 1, next_idx)
                ]
                node["body_text"] = "\n".join(parts)

    def _serialize_tree_node(
        self,
        child: dict[str, Any],
        content_strategy: str,
        depth: int,
        current_depth: int = 1,
        bookmark_map: dict[Any, Any] | None = None,
        *,
        para_ranges: list[Any],
        next_heading_map: dict[int, int],
    ) -> dict[str, Any]:
        node = {
            "type": "heading",
            **heading_ref(
                child,
                bookmark_map,
                include_body=True,
                include_children_count=True,
                include_chapter=True,
            ),
        }
        self._apply_content_strategy(node, content_strategy, para_ranges=para_ranges, next_heading_map=next_heading_map)
        if depth == 0 or current_depth < depth:
            if child.get("children"):
                node["children"] = [
                    self._serialize_tree_node(
                        sub,
                        content_strategy,
                        depth,
                        current_depth + 1,
                        bookmark_map,
                        para_ranges=para_ranges,
                        next_heading_map=next_heading_map,
                    )
                    for sub in child["children"]
                ]
        return node

    # ── Public tree API ────────────────────────────────────────────

    def _load_tree_context(self, doc: Any) -> _TreeContext:
        para_ranges = self._doc_svc.get_paragraph_ranges(doc)
        tree = self.build_heading_tree(doc, elements=para_ranges)
        heading_indices = _collect_heading_indices(tree)
        total = len(para_ranges)
        next_heading_map = {
            h_idx: (heading_indices[i + 1] if i + 1 < len(heading_indices) else total)
            for i, h_idx in enumerate(heading_indices)
        }
        bookmark_map = self._bm_svc.ensure_heading_bookmarks(
            doc, heading_para_indices=heading_indices, para_ranges=para_ranges
        )
        return _TreeContext(para_ranges, tree, heading_indices, next_heading_map, bookmark_map)

    def get_document_tree(self, doc: Any, content_strategy: str = "first_lines", depth: int = 1) -> dict[str, Any]:
        """Get serialized document tree with content strategies."""
        ctx = self._load_tree_context(doc)

        children = [
            self._serialize_tree_node(
                child,
                content_strategy,
                depth,
                bookmark_map=ctx.bookmark_map,
                para_ranges=ctx.para_ranges,
                next_heading_map=ctx.next_heading_map,
            )
            for child in ctx.tree["children"]
        ]

        page_count = self._doc_svc.get_page_count(doc)

        return {
            "status": "ok",
            "content_strategy": content_strategy,
            "depth": depth,
            "children": children,
            "body_before_first_heading": ctx.tree["body_paragraphs"],
            "total_paragraphs": len(ctx.para_ranges),
            "page_count": page_count,
        }

    def get_heading_children(self, doc: Any, heading_para_index: int | None = None, locator: str | None = None, content_strategy: str = "first_lines", depth: int = 1) -> dict[str, Any]:
        """Get children of a heading (body paragraphs + sub-headings)."""
        if locator is not None and heading_para_index is None:
            resolved = self._doc_svc.resolve_locator(doc, locator)
            heading_para_index = resolved.get("para_index")

        if heading_para_index is None:
            raise ToolExecutionError("Provide locator, heading_para_index, or heading_bookmark")

        ctx = self._load_tree_context(doc)
        target = self.find_heading_by_para_index(ctx.tree, heading_para_index)
        if target is None:
            raise ToolExecutionError("Heading at paragraph %d not found" % heading_para_index)

        children = []
        total = len(ctx.para_ranges)
        next_heading_para_index = ctx.next_heading_map.get(heading_para_index, total)

        for idx, element in _iter_body_paragraphs(ctx.para_ranges, heading_para_index + 1, next_heading_para_index):
            if content_strategy == "heading_only":
                children.append({"type": "body", "para_index": idx})
            elif content_strategy == "full":
                para_text = get_string_without_tracked_deletions(element)
                children.append({"type": "body", "para_index": idx, "text": para_text})
            else:
                para_text = get_string_without_tracked_deletions(element)
                children.append({"type": "body", "para_index": idx, "preview": _preview_text(para_text)})

        for child in target["children"]:
            node = self._serialize_tree_node(
                child,
                content_strategy,
                depth,
                bookmark_map=ctx.bookmark_map,
                para_ranges=ctx.para_ranges,
                next_heading_map=ctx.next_heading_map,
            )
            children.append(node)

        parent = heading_ref(target, ctx.bookmark_map, include_chapter=True)
        return {"status": "ok", "parent": parent, "content_strategy": content_strategy, "depth": depth, "children": children}

    def _paragraph_at_anchor(self, doc: Any, anchor: Any) -> tuple[int | None, list[Any], Any]:
        para_ranges = self._doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = self._doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)
        return para_idx, para_ranges, text_obj

    def _confirm_anchor_placement(
        self, doc: Any, obj: Any, not_placed_err: Exception, accept_name: str | None = None
    ) -> int:
        try:
            anchor = obj.getAnchor()
        except Exception as exc:
            raise not_placed_err from exc
        if anchor is None:
            raise not_placed_err
        para_idx, para_ranges, text_obj = self._paragraph_at_anchor(doc, anchor)
        placed = confirm_paragraph_index(text_obj, anchor, para_ranges, para_idx)
        if placed is None:
            if (
                accept_name is not None
                and isinstance(para_idx, int)
                and 0 <= para_idx < len(para_ranges)
                and getattr(para_ranges[para_idx], "getName", lambda: None)() == accept_name
            ):
                return para_idx
            raise not_placed_err
        return placed

    # ── Locator resolution (called by document.resolve_locator) ────

    def resolve_writer_locator(self, doc: Any, loc_type: str, loc_value: Any) -> dict[str, Any]:
        """Resolve Writer-specific locators to {para_index: N}."""
        if loc_type == "bookmark":
            return self._resolve_bookmark_locator(doc, loc_value)

        if loc_type == "page":
            page_num = int(loc_value)
            try:
                controller = doc.getCurrentController()
                vc = controller.getViewCursor()

                def _jump() -> Any:
                    vc.jumpToPage(page_num)
                    vc.jumpToStartOfPage()
                    return vc.getStart()

                # Why-comment: restoring inside the lock leaves a view cursor that
                # started in a table cell or frame on the target page. Do not add a
                # second save/restore.
                anchor = with_view_cursor_left_body_locked(doc, vc, _jump)
                para_idx, _para_ranges, _text_obj = self._paragraph_at_anchor(doc, anchor)
                if para_idx is None:
                    raise ToolExecutionError("page start is not in a body paragraph")
                return {"para_index": para_idx}
            except Exception as e:
                raise ToolExecutionError("Cannot resolve page:%s — %s" % (loc_value, e)) from e

        if loc_type == "section":
            section = _named_doc_item(
                doc,
                "getTextSections",
                "Document does not support sections",
                "Section '%s' not found",
                loc_value,
            )
            anchor = section.getAnchor()
            para_idx, _para_ranges, _text_obj = self._paragraph_at_anchor(doc, anchor)
            if para_idx is None:
                raise ToolExecutionError("Section '%s' anchor is not in the document" % loc_value)
            return {"para_index": para_idx, "section_name": loc_value}

        if loc_type == "heading":
            parts = [int(p) for p in loc_value.split(".")]
            tree = self.build_heading_tree(doc)
            node = tree
            for part in parts:
                children = node.get("children", [])
                if part < 1 or part > len(children):
                    raise ToolExecutionError("Heading index %d out of range (1..%d) in 'heading:%s'" % (part, len(children), loc_value))
                node = children[part - 1]
            return {"para_index": node["para_index"]}

        if loc_type == "chapter_number":
            # Exact match on the emitted field — never invent from sibling ordinals.
            tree = self.build_heading_tree(doc)
            found = find_heading_by_chapter_number(tree, str(loc_value))
            if found is None:
                raise ToolExecutionError(
                    "No heading with chapter_number:%s. Chapter Numbering may be off "
                    "(writer_tree omits chapter_number when the outline label is empty), "
                    "or no heading has that label. heading: is the sibling-ordinal path, "
                    "not the chapter label." % loc_value
                )
            return {"para_index": found["para_index"]}

        if loc_type == "heading_text":
            para_index = self._find_heading_by_text(doc, loc_value)
            if para_index is None:
                raise ToolExecutionError("No heading matching '%s' found" % loc_value)
            return {"para_index": para_index}

        if loc_type == "table":
            table = _named_doc_item(
                doc,
                "getTextTables",
                "Document does not support tables",
                "Table '%s' not found",
                loc_value,
            )
            placed = self._confirm_anchor_placement(
                doc,
                table,
                ToolExecutionError(
                    "Table '%s' anchor is not in the document" % loc_value
                ),
                accept_name=loc_value,
            )
            return {"para_index": placed, "table_name": loc_value}

        raise ToolExecutionError("Unknown Writer locator type: '%s'" % loc_type)

    def _resolve_bookmark_locator(self, doc: Any, bookmark_name: str) -> dict[str, Any]:
        if not hasattr(doc, "getBookmarks"):
            raise ToolExecutionError("Document doesn't support bookmarks")
        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(bookmark_name):
            hint = "Bookmark '%s' not found." % bookmark_name
            if bookmark_name.startswith("_mcp_"):
                hint += " Use heading_text:<text> locator for resilient heading addressing, or call get_document_tree to refresh bookmarks."
                existing = [n for n in bookmarks.getElementNames() if n.startswith("_mcp_")]
                if existing:
                    hint += " Existing bookmarks: " + ", ".join(existing[:10])
            raise ToolExecutionError(hint)
        bm = bookmarks.getByName(bookmark_name)
        para_idx = self._paragraph_for_bookmark(doc, bookmark_name, bm)
        return {"para_index": para_idx}

    def _paragraph_for_bookmark(self, doc: Any, bookmark_name: str, bookmark: Any) -> int:
        """Paragraph index for a bookmark that still has a name.

        ``find_paragraph_for_range`` returns None when the anchor cannot be
        placed. A stale ``_mcp_`` mark after save/reopen or
        ``bookmark_cleanup`` still has a name, so it must not resolve
        to a paragraph.
        ``confirm_paragraph_index`` keeps 0 only when the anchor start
        is inside that paragraph. Anything else is an error, not a
        location.
        """
        return self._confirm_anchor_placement(
            doc, bookmark, _bookmark_anchor_not_placed(bookmark_name)
        )

    def _find_heading_by_text(self, doc: Any, search_text: str) -> int | None:
        """Find heading by text (case-insensitive, fuzzy)."""
        search_lower = search_text.lower().strip()
        if not search_lower:
            return None

        tree = self.build_heading_tree(doc)

        best_prefix = None
        best_substring = None
        matched = None

        for h in iter_heading_nodes(tree):
            text_lower = h["text"].lower()
            text_stripped = text_lower.strip()

            # Exact match - highest priority, return immediately
            if text_stripped == search_lower:
                matched = h
                break

            # Prefix match - second priority
            if best_prefix is None and text_stripped.startswith(search_lower):
                best_prefix = h

            # Substring match - third priority
            if best_substring is None and search_lower in text_lower:
                best_substring = h
        else:
            matched = best_prefix or best_substring

        if matched is None:
            return None

        return matched["para_index"]
