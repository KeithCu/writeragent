"""Writer content tools — read, apply, find, and paragraph operations."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.writer import format_support

log = logging.getLogger("localwriter.writer")


# ------------------------------------------------------------------
# GetDocumentContent
# ------------------------------------------------------------------

class GetDocumentContent(ToolBase):
    """Export the document (or a portion) as formatted content."""

    name = "get_document_content"
    description = (
        "Get document (or selection/range) content. "
        "Result includes document_length. "
        "scope: full, selection, or range (requires start, end)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["full", "selection", "range"],
                "description": (
                    "Return full document (default), current "
                    "selection/cursor region, or a character range "
                    "(requires start and end)."
                ),
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return.",
            },
            "start": {
                "type": "integer",
                "description": "Start character offset (0-based). Required for scope 'range'.",
            },
            "end": {
                "type": "integer",
                "description": "End character offset (exclusive). Required for scope 'range'.",
            },
        },
        "required": [],
    }
    doc_types = ["writer"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        range_start = kwargs.get("start") if scope == "range" else None
        range_end = kwargs.get("end") if scope == "range" else None

        if scope == "range" and (range_start is None or range_end is None):
            return {"status": "error", "message": "scope 'range' requires start and end."}

        content = format_support.document_to_content(
            ctx.doc, ctx.ctx, ctx.services,
            max_chars=max_chars, scope=scope,
            range_start=range_start, range_end=range_end,
        )
        doc_len = ctx.services.document.get_document_length(ctx.doc)
        result = {
            "status": "ok",
            "content": content,
            "length": len(content),
            "document_length": doc_len,
        }
        if scope == "range" and range_start is not None:
            result["start"] = int(range_start)
            result["end"] = int(range_end)
        return result


# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------

class ApplyDocumentContent(ToolBase):
    """Insert or replace content in the document."""

    name = "apply_document_content"
    description = (
        "Insert or replace content. Preferred for partial edits: "
        "target='search' with search= and content=. "
        "For whole doc: target='full'. "
        "Use target='range' with start/end."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The new content (HTML or plain text). Do not use Markdown.",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection", "search", "full", "range"],
                "description": (
                    "Where to apply: full, range (start+end), "
                    "search (needs search), beginning, end, selection."
                ),
            },
            "start": {
                "type": "integer",
                "description": "Start character offset. Required for target 'range'.",
            },
            "end": {
                "type": "integer",
                "description": "End character offset. Required for target 'range'.",
            },
            "search": {
                "type": "string",
                "description": "Text to find. Required for target 'search'.",
            },
            "all_matches": {
                "type": "boolean",
                "description": "Replace all occurrences (true) or first only. Default false.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search. Default true.",
            },
        },
        "required": ["content", "target"],
    }
    doc_types = ["writer"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        content = kwargs.get("content", "")
        target = kwargs.get("target")

        # Normalize list input.
        if isinstance(content, list):
            content = "\n".join(str(x) for x in content)
        if isinstance(content, str):
            content = content.replace("\\n", "\n").replace("\\t", "\t")

        if not target:
            return {"status": "error", "message": "target is required."}

        # Detect markup BEFORE any HTML wrapping.
        raw_content = content
        use_preserve = isinstance(content, str) and not format_support.content_has_markup(content)

        config_svc = ctx.services.get("config")

        # -- search -------------------------------------------------
        if target == "search":
            search = kwargs.get("search")
            if not search and search != "":
                return {"status": "error", "message": "search is required when target is 'search'."}
            all_matches = kwargs.get("all_matches", False)
            case_sensitive = kwargs.get("case_sensitive", True)
            try:
                if use_preserve:
                    count = format_support._preserving_search_replace(
                        ctx.doc, ctx.ctx, raw_content, search,
                        all_matches=all_matches,
                        case_sensitive=case_sensitive,
                    )
                else:
                    count = format_support.apply_content_at_search(
                        ctx.doc, ctx.ctx, content, search,
                        all_matches=all_matches,
                        case_sensitive=case_sensitive,
                        config_svc=config_svc,
                    )
                msg = "Replaced %d occurrence(s)." % count
                if use_preserve and count > 0:
                    msg += " (formatting preserved)"
                if count == 0:
                    msg += (
                        " No matches found. Try search_in_document first, then "
                        "use target='range'."
                    )
                return {"status": "ok", "message": msg}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}

        # -- full ---------------------------------------------------
        if target == "full":
            try:
                if use_preserve:
                    # Select entire document directly
                    from plugin.modules.core.services.document import get_document_length
                    doc_len = get_document_length(ctx.doc)
                    rng = ctx.doc.getText().createTextCursor()
                    rng.gotoStart(False)
                    remaining = doc_len
                    while remaining > 0:
                        n = min(remaining, 8192)
                        rng.goRight(n, True)
                        remaining -= n
                    format_support.replace_preserving_format(
                        ctx.doc, rng, raw_content, ctx.ctx
                    )
                    return {"status": "ok", "message": "Replaced entire document. (formatting preserved)"}
                else:
                    format_support.replace_full_document(
                        ctx.doc, ctx.ctx, content, config_svc=config_svc
                    )
                    return {"status": "ok", "message": "Replaced entire document."}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}

        # -- range --------------------------------------------------
        if target == "range":
            start_val = kwargs.get("start")
            end_val = kwargs.get("end")
            if start_val is None or end_val is None:
                return {"status": "error", "message": "target 'range' requires start and end."}
            try:
                if use_preserve:
                    from plugin.modules.writer.ops import get_text_cursor_at_range
                    rng = get_text_cursor_at_range(
                        ctx.doc, int(start_val), int(end_val)
                    )
                    format_support.replace_preserving_format(
                        ctx.doc, rng, raw_content, ctx.ctx
                    )
                    return {
                        "status": "ok",
                        "message": "Replaced range [%s, %s). (formatting preserved)"
                        % (start_val, end_val),
                    }
                else:
                    format_support.apply_content_at_range(
                        ctx.doc, ctx.ctx, content,
                        int(start_val), int(end_val),
                        config_svc=config_svc,
                    )
                    return {
                        "status": "ok",
                        "message": "Replaced range [%s, %s)." % (start_val, end_val),
                    }
            except Exception as exc:
                return {"status": "error", "message": str(exc)}

        # -- beginning / end / selection ----------------------------
        if target in ("beginning", "end", "selection"):
            try:
                format_support.insert_content_at_position(
                    ctx.doc, ctx.ctx, content, target,
                    config_svc=config_svc,
                )
                return {
                    "status": "ok",
                    "message": "Inserted content at %s." % target,
                }
            except Exception as exc:
                return {"status": "error", "message": str(exc)}

        return {"status": "error", "message": "Unknown target: %s" % target}




'''
# ------------------------------------------------------------------
# ReadParagraphs
# ------------------------------------------------------------------

class ReadParagraphs(ToolBase):
    """Read a range of paragraphs by index."""

    name = "read_paragraphs"
    description = (
        "Read a range of paragraphs by index or locator. "
        "Useful for scanning text between headings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start_index": {
                "type": "integer",
                "description": "Starting paragraph index (0-based).",
            },
            "locator": {
                "type": "string",
                "description": (
                    "Locator for start position: 'paragraph:N', "
                    "'bookmark:_mcp_x', 'heading_text:Title', etc. "
                    "Overrides start_index."
                ),
            },
            "count": {
                "type": "integer",
                "description": "Number of paragraphs to read (default 10).",
            },
        },
        "required": [],
    }
    doc_types = ["writer"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        start = kwargs.get("start_index", 0)
        locator = kwargs.get("locator")
        count = kwargs.get("count", 10)

        if locator is not None:
            doc_svc = ctx.services.document
            resolved = doc_svc.resolve_locator(ctx.doc, locator)
            start = resolved.get("para_index", start)

        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)
        end = min(start + count, len(para_ranges))

        paragraphs = []
        for i in range(start, end):
            p = para_ranges[i]
            text = p.getString() if hasattr(p, "getString") else "[Object]"
            paragraphs.append({"index": i, "text": text})

        return {
            "status": "ok",
            "paragraphs": paragraphs,
            "total": len(para_ranges),
        }


# ------------------------------------------------------------------
# InsertAtParagraph
# ------------------------------------------------------------------

class InsertAtParagraph(ToolBase):
    """Insert text at a specific paragraph index."""

    name = "insert_at_paragraph"
    description = "Insert text at a specific paragraph index or locator."
    parameters = {
        "type": "object",
        "properties": {
            "paragraph_index": {
                "type": "integer",
                "description": "0-based paragraph index.",
            },
            "locator": {
                "type": "string",
                "description": (
                    "Locator: 'paragraph:N', 'bookmark:_mcp_x', "
                    "'heading_text:Title', etc. Overrides paragraph_index."
                ),
            },
            "text": {
                "type": "string",
                "description": "Text to insert.",
            },
            "style": {
                "type": "string",
                "description": (
                    "Paragraph style to apply to the inserted text "
                    "(e.g. 'Heading 1', 'Text Body')."
                ),
            },
            "position": {
                "type": "string",
                "enum": ["before", "after", "replace"],
                "description": "Position relative to the target paragraph (default: 'before').",
            },
        },
        "required": ["text"],
    }
    doc_types = ["writer"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        para_index = _resolve_para_index(ctx, kwargs)
        text_to_insert = kwargs.get("text", "")
        style = kwargs.get("style")
        position = kwargs.get("position", "before")

        if para_index is None:
            return {"status": "error", "message": "Provide locator or paragraph_index."}

        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)

        if para_index < 0 or para_index >= len(para_ranges):
            return {
                "status": "error",
                "message": "Paragraph index %d out of range (0..%d)."
                % (para_index, len(para_ranges) - 1),
            }

        target_para = para_ranges[para_index]
        text = ctx.doc.getText()
        cursor = text.createTextCursorByRange(target_para.getStart())

        if position == "after":
            cursor.gotoRange(target_para.getEnd(), False)
            text.insertString(cursor, "\n" + text_to_insert, False)
        elif position == "replace":
            cursor.gotoRange(target_para.getEnd(), True)
            cursor.setString(text_to_insert)
        else:  # before
            text.insertString(cursor, text_to_insert + "\n", False)

        # Apply style if requested
        if style:
            resolved_style = _resolve_style_name(ctx.doc, style)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", resolved_style)

        return {
            "status": "ok",
            "message": "Inserted text at paragraph %d." % para_index,
        }


# ------------------------------------------------------------------
# ModifyParagraph
# ------------------------------------------------------------------

class ModifyParagraph(ToolBase):
    """Change paragraph text and/or style. Provide at least one of text or style."""

    name = "modify_paragraph"
    intent = "edit"
    description = (
        "Modify a paragraph: set its text (preserves style), its style "
        "(e.g. 'Heading 1', 'Text Body', 'List Bullet'), or both. "
        "Provide at least one of text or style. Returns paragraph_index and "
        "bookmark (if heading) for stable addressing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": (
                    "Locator: 'paragraph:N', 'bookmark:_mcp_x', "
                    "'heading_text:Title', etc."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Target paragraph index (0-based).",
            },
            "text": {
                "type": "string",
                "description": "New text content for the paragraph (optional).",
            },
            "style": {
                "type": "string",
                "description": "Paragraph style to apply, e.g. 'Heading 1', 'Text Body' (optional).",
            },
        },
        "required": [],
    }
    doc_types = ["writer"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        text = kwargs.get("text")
        style = kwargs.get("style")
        if text is None and style is None:
            return {"status": "error",
                    "message": "Provide at least one of text or style."}

        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return {"status": "error",
                    "message": "Provide locator or paragraph_index."}

        doc_svc = ctx.services.document
        target, _ = doc_svc.find_paragraph_element(ctx.doc, para_index)
        if target is None:
            return {"status": "error",
                    "message": "Paragraph %d not found." % para_index}

        result = {"status": "ok", "paragraph_index": para_index}

        if text is not None:
            old_text = target.getString()
            target.setString(text)
            result["old_length"] = len(old_text)
            result["new_length"] = len(text)

        if style is not None:
            resolved_style = _resolve_style_name(ctx.doc, style)
            old_style = target.getPropertyValue("ParaStyleName")
            target.setPropertyValue("ParaStyleName", resolved_style)
            result["old_style"] = old_style
            result["new_style"] = resolved_style

        bm_svc = ctx.services.get("writer_bookmarks")
        if bm_svc:
            bm_map = bm_svc.get_mcp_bookmark_map(ctx.doc)
            if para_index in bm_map:
                result["bookmark"] = bm_map[para_index]

        return result


# ------------------------------------------------------------------
# DeleteParagraph
# ------------------------------------------------------------------

class DeleteParagraph(ToolBase):
    """Delete a paragraph from the document."""

    name = "delete_paragraph"
    intent = "edit"
    description = "Delete a paragraph from the document."
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": (
                    "Locator: 'paragraph:N', 'bookmark:_mcp_x', "
                    "'heading_text:Title', etc."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Target paragraph index (0-based).",
            },
        },
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return {"status": "error",
                    "message": "Provide locator or paragraph_index."}

        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        idx = 0
        target = None
        while enum.hasMoreElements():
            element = enum.nextElement()
            if idx == para_index:
                target = element
                break
            idx += 1

        if target is None:
            return {"status": "error",
                    "message": "Paragraph %d not found." % para_index}

        cursor = doc_text.createTextCursorByRange(target)
        cursor.gotoStartOfParagraph(False)
        cursor.gotoEndOfParagraph(True)
        # Extend selection to include the paragraph break
        if enum.hasMoreElements():
            cursor.goRight(1, True)
        cursor.setString("")

        return {
            "status": "ok",
            "message": "Deleted paragraph %d." % para_index,
        }


# ------------------------------------------------------------------
# DuplicateParagraph
# ------------------------------------------------------------------

class DuplicateParagraph(ToolBase):
    """Duplicate a paragraph (with its style) after itself."""

    name = "duplicate_paragraph"
    intent = "edit"
    description = (
        "Duplicate a paragraph (with its style) after itself. "
        "Use count > 1 to duplicate multiple consecutive paragraphs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": (
                    "Locator: 'paragraph:N', 'bookmark:_mcp_x', "
                    "'heading_text:Title', etc."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Target paragraph index (0-based).",
            },
            "count": {
                "type": "integer",
                "description": (
                    "Number of consecutive paragraphs to duplicate "
                    "(default: 1)."
                ),
            },
        },
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return {"status": "error",
                    "message": "Provide locator or paragraph_index."}

        count = kwargs.get("count", 1)
        if count < 1:
            return {"status": "error", "message": "count must be >= 1."}

        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        elements = []
        idx = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            if para_index <= idx < para_index + count:
                elements.append(el)
            if idx >= para_index + count - 1:
                break
            idx += 1

        if not elements:
            return {"status": "error",
                    "message": "Paragraph %d not found." % para_index}

        last = elements[-1]
        cursor = doc_text.createTextCursorByRange(last)
        cursor.gotoEndOfParagraph(False)

        for el in elements:
            txt = el.getString()
            sty = el.getPropertyValue("ParaStyleName")
            doc_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            doc_text.insertString(cursor, txt, False)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", sty)
            cursor.gotoEndOfParagraph(False)

        return {
            "status": "ok",
            "message": "Duplicated %d paragraph(s) at %d."
                       % (count, para_index),
            "duplicated_count": count,
        }


# ------------------------------------------------------------------
# CloneHeadingBlock
# ------------------------------------------------------------------

class CloneHeadingBlock(ToolBase):
    """Clone an entire heading block (heading + all sub-headings + body)."""

    name = "clone_heading_block"
    intent = "edit"
    description = (
        "Clone an entire heading block (heading + all sub-headings + body). "
        "The clone is inserted right after the original block."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": (
                    "Locator of the heading to clone "
                    "(e.g. 'bookmark:_mcp_abc123', "
                    "'heading_text:Introduction')."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Paragraph index of the heading (0-based).",
            },
        },
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return {"status": "error",
                    "message": "Provide locator or paragraph_index."}

        # Use writer_tree service to find the heading node and block size
        tree_svc = ctx.services.get("writer_tree")
        if tree_svc is None:
            return {"status": "error",
                    "message": "writer_nav module not loaded; "
                               "cannot resolve heading block."}

        tree = tree_svc.build_heading_tree(ctx.doc)
        node = tree_svc._find_node_by_para_index(tree, para_index)
        if node is None:
            return {"status": "error",
                    "message": "No heading found at paragraph %d."
                               % para_index}

        # Total paragraphs in the block: heading + body + all children
        total = 1 + tree_svc._count_all_children(node)

        # Collect elements for the block
        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        elements = []
        idx = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            if para_index <= idx < para_index + total:
                elements.append(el)
            if idx >= para_index + total - 1:
                break
            idx += 1

        if not elements:
            return {"status": "error",
                    "message": "Could not collect heading block paragraphs."}

        # Insert duplicates after the last element of the block
        last = elements[-1]
        cursor = doc_text.createTextCursorByRange(last)
        cursor.gotoEndOfParagraph(False)

        for el in elements:
            txt = el.getString()
            sty = el.getPropertyValue("ParaStyleName")
            doc_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            doc_text.insertString(cursor, txt, False)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", sty)
            cursor.gotoEndOfParagraph(False)

        return {
            "status": "ok",
            "message": "Cloned heading block '%s' (%d paragraphs)."
                       % (node.get("text", ""), total),
            "heading_text": node.get("text", ""),
            "block_size": total,
        }


# ------------------------------------------------------------------
# InsertParagraphsBatch
# ------------------------------------------------------------------

class InsertParagraphsBatch(ToolBase):
    """Insert multiple paragraphs in one call."""

    name = "insert_paragraphs_batch"
    intent = "edit"
    description = (
        "Insert multiple paragraphs in a single operation. "
        "Each item in paragraphs is {\"text\": \"...\", \"style\": \"...\"}. "
        "Style is optional."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": (
                    "Locator: 'paragraph:N', 'bookmark:_mcp_x', "
                    "'heading_text:Title', etc."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Target paragraph index (0-based).",
            },
            "paragraphs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "style": {"type": "string"},
                    },
                    "required": ["text"],
                },
                "description": "List of {text, style?} objects to insert.",
            },
            "position": {
                "type": "string",
                "enum": ["before", "after"],
                "description": "'before' or 'after' (default: after).",
            },
        },
        "required": ["paragraphs"],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

        paragraphs = kwargs.get("paragraphs")
        if not paragraphs:
            return {"status": "error", "message": "Empty paragraphs list."}

        position = kwargs.get("position", "after")
        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return {"status": "error",
                    "message": "Provide locator or paragraph_index."}

        doc_svc = ctx.services.document
        target, _ = doc_svc.find_paragraph_element(ctx.doc, para_index)
        if target is None:
            return {"status": "error",
                    "message": "Paragraph %d not found." % para_index}

        doc_text = ctx.doc.getText()
        cursor = doc_text.createTextCursorByRange(target)

        if position == "before":
            cursor.gotoStartOfParagraph(False)
            for item in paragraphs:
                txt = item.get("text", "")
                sty = item.get("style")
                if sty:
                    sty = _resolve_style_name(ctx.doc, sty)
                doc_text.insertString(cursor, txt, False)
                doc_text.insertControlCharacter(
                    cursor, PARAGRAPH_BREAK, False)
                if sty:
                    cursor.gotoPreviousParagraph(False)
                    cursor.gotoStartOfParagraph(False)
                    cursor.gotoEndOfParagraph(True)
                    cursor.setPropertyValue("ParaStyleName", sty)
                    cursor.gotoNextParagraph(False)
        elif position == "after":
            cursor.gotoEndOfParagraph(False)
            for item in paragraphs:
                txt = item.get("text", "")
                sty = item.get("style")
                if sty:
                    sty = _resolve_style_name(ctx.doc, sty)
                doc_text.insertControlCharacter(
                    cursor, PARAGRAPH_BREAK, False)
                doc_text.insertString(cursor, txt, False)
                if sty:
                    cursor.gotoStartOfParagraph(False)
                    cursor.gotoEndOfParagraph(True)
                    cursor.setPropertyValue("ParaStyleName", sty)
                    cursor.gotoEndOfParagraph(False)
        else:
            return {"status": "error",
                    "message": "Invalid position: %s" % position}

        n = len(paragraphs)
        return {
            "status": "ok",
            "message": "Inserted %d paragraph(s) %s paragraph %d."
                       % (n, position, para_index),
            "count": n,
        }
'''


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _resolve_para_index(ctx, kwargs):
    """Resolve locator or paragraph_index from tool kwargs.

    Returns an integer paragraph index, or None if neither is provided.
    """
    locator = kwargs.get("locator")
    para_index = kwargs.get("paragraph_index")

    if locator is not None and para_index is None:
        doc_svc = ctx.services.document
        resolved = doc_svc.resolve_locator(ctx.doc, locator)
        para_index = resolved.get("para_index")

    return para_index


def _resolve_style_name(doc, style_name):
    """Resolve a style name case-insensitively against the document styles."""
    try:
        families = doc.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if para_styles.hasByName(style_name):
            return style_name
        lower = style_name.lower()
        for name in para_styles.getElementNames():
            if name.lower() == lower:
                return name
    except Exception:
        pass
    return style_name



class GetDocumentStats(ToolBase):
    """Return basic statistics about the current Writer document."""

    name = "get_document_stats"
    description = (
        "Returns document statistics: character count, word count, "
        "paragraph count, page count, and heading count."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    doc_types = ["writer"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        doc_svc = ctx.services.document

        # Character and word count via full text.
        try:
            text_obj = doc.getText()
            cursor = text_obj.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            full_text = cursor.getString()
            char_count = len(full_text)
            word_count = len(full_text.split())
        except Exception:
            char_count = doc_svc.get_document_length(doc)
            word_count = 0

        # Paragraph count.
        try:
            para_ranges = doc_svc.get_paragraph_ranges(doc)
            para_count = len(para_ranges)
        except Exception:
            para_count = 0

        # Heading count from tree.
        try:
            tree = doc_svc.build_heading_tree(doc)
            heading_count = _count_headings(tree)
        except Exception:
            heading_count = 0

        # Page count (approximate via view cursor).
        page_count = 0
        try:
            vc = doc.getCurrentController().getViewCursor()
            vc.jumpToLastPage()
            page_count = vc.getPage()
        except Exception:
            pass

        return {
            "status": "ok",
            "character_count": char_count,
            "word_count": word_count,
            "paragraph_count": para_count,
            "page_count": page_count,
            "heading_count": heading_count,
        }


def _count_headings(nodes):
    """Recursively count heading nodes in a nested list."""
    count = 0
    for node in nodes:
        count += 1
        count += _count_headings(node.get("children", []))
    return count
