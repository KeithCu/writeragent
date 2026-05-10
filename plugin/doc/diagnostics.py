# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Document diagnostics tools: health checks, protection."""

import logging

from plugin.framework.tool import ToolBaseDummy

log = logging.getLogger("nelson.common")


class DocumentHealthCheck(ToolBaseDummy):
    """Run structural health checks on a Writer document."""

    name = "document_health_check"
    intent = "review"
    description = "Run structural health checks on the document. Detects empty headings, heading level jumps, orphan images, large unstructured blocks."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)

        issues = []
        last_heading_level = 0
        paragraphs_since_heading = 0
        total_headings = 0

        for i, para in enumerate(para_ranges):
            text = ""
            outline_level = 0
            try:
                text = para.getString()
                outline_level = para.getPropertyValue("OutlineLevel")
            except Exception:
                pass

            if outline_level > 0:
                total_headings += 1
                heading_preview = text.strip()[:80]

                # --- Empty heading check ---
                if not text.strip():
                    issues.append({"type": "empty_heading", "severity": "warning", "paragraph_index": i, "message": ("Empty heading level %d at paragraph %d." % (outline_level, i)), "detail": "Heading level %d is empty." % outline_level})

                # --- Heading level jump check ---
                if last_heading_level > 0 and outline_level > last_heading_level + 1:
                    issues.append(
                        {
                            "type": "heading_level_skip",
                            "severity": "info",
                            "paragraph_index": i,
                            "message": ("Heading jumps from level %d to %d at paragraph %d: '%s'" % (last_heading_level, outline_level, i, heading_preview)),
                            "detail": ("Heading jumps from level %d to %d (skips %s)." % (last_heading_level, outline_level, ", ".join(str(lv) for lv in range(last_heading_level + 1, outline_level)))),
                        }
                    )

                last_heading_level = outline_level

                # --- Large unstructured block check ---
                if paragraphs_since_heading > 50:
                    issues.append(
                        {
                            "type": "large_block",
                            "severity": "info",
                            "paragraph_index": i - paragraphs_since_heading,
                            "message": ("%d paragraphs without a heading before paragraph %d." % (paragraphs_since_heading, i)),
                            "detail": ("%d paragraphs without a heading before paragraph %d." % (paragraphs_since_heading, i)),
                        }
                    )
                paragraphs_since_heading = 0
            else:
                paragraphs_since_heading += 1

        # Check trailing large block (after last heading to end of doc).
        if paragraphs_since_heading > 50:
            issues.append(
                {
                    "type": "large_block",
                    "severity": "info",
                    "paragraph_index": len(para_ranges) - paragraphs_since_heading,
                    "message": ("%d paragraphs without a heading at end of document." % paragraphs_since_heading),
                    "detail": ("%d paragraphs without a heading at end of document." % paragraphs_since_heading),
                }
            )

        # --- Broken bookmarks ---
        try:
            if hasattr(doc, "getBookmarks"):
                bookmarks = doc.getBookmarks()
                names = bookmarks.getElementNames()
                for name in names:
                    try:
                        bm = bookmarks.getByName(name)
                        anchor = bm.getAnchor()
                        if anchor is None or not anchor.getString():
                            issues.append({"type": "broken_bookmark", "severity": "warning", "paragraph_index": -1, "message": ("Bookmark '%s' has an empty anchor." % name), "detail": ("Bookmark '%s' has an empty anchor." % name)})
                    except Exception:
                        issues.append({"type": "broken_bookmark", "severity": "warning", "paragraph_index": -1, "message": ("Bookmark '%s' could not be read." % name), "detail": ("Bookmark '%s' could not be read." % name)})
        except Exception:
            pass

        # --- Orphan images (graphic objects without a URL) ---
        try:
            if hasattr(doc, "getGraphicObjects"):
                graphics = doc.getGraphicObjects()
                names = graphics.getElementNames()
                for name in names:
                    try:
                        obj = graphics.getByName(name)
                        graphic_url = ""
                        try:
                            graphic_url = obj.getPropertyValue("GraphicURL")
                        except Exception:
                            pass
                        if not graphic_url:
                            # Also check the Graphic property (LO 7.1+).
                            has_graphic = False
                            try:
                                g = obj.getPropertyValue("Graphic")
                                has_graphic = g is not None
                            except Exception:
                                pass
                            if not has_graphic:
                                issues.append({"type": "orphan_image", "severity": "warning", "paragraph_index": -1, "message": ("Graphic object '%s' has no image data." % name), "detail": ("Graphic object '%s' has no image data." % name)})
                    except Exception:
                        pass
        except Exception:
            pass

        return {"status": "ok", "issues": issues, "issue_count": len(issues), "paragraph_count": len(para_ranges), "total_headings": total_headings}


class SetDocumentProtection(ToolBaseDummy):
    """Set or remove document section protection."""

    name = "set_document_protection"
    intent = "review"
    description = "Set or remove document section protection."
    parameters = {"type": "object", "properties": {"enabled": {"type": "boolean", "description": "True to protect sections, False to unprotect."}, "password": {"type": "string", "description": "Optional protection password."}}, "required": ["enabled"]}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        enabled = kwargs["enabled"]
        password = kwargs.get("password")
        doc = ctx.doc

        try:
            sections = doc.getTextSections()
        except Exception as exc:
            return self._tool_error(str(exc))

        count = sections.getCount()
        if count == 0:
            return {"status": "ok", "protected": enabled, "sections_count": 0, "message": "No text sections found in document."}

        for i in range(count):
            section = sections.getByIndex(i)
            try:
                section.setPropertyValue("IsProtected", enabled)
                if password and enabled:
                    # setProtectionPassword expects a sequence of bytes
                    # encoded from the password string.
                    try:
                        section.setProtectionPassword(password)
                    except AttributeError:
                        # Older LO versions may not support per-section
                        # passwords; silently skip.
                        pass
            except Exception as exc:
                log.warning("Could not set protection on section %d: %s", i, exc)

        return {"status": "ok", "protected": enabled, "sections_count": count}
