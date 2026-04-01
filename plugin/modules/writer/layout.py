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
"""Writer text frame management tools (layout domain, specialized tier).

Frame tool logic adapted from nelson-mcp (MPL 2.0):
nelson-mcp/plugin/modules/writer/tools/frames.py
"""

import logging

from plugin.modules.writer.base import ToolWriterLayoutBase

log = logging.getLogger("writeragent.writer")


class ListTextFrames(ToolWriterLayoutBase):
    """List all text frames in the document."""

    name = "list_text_frames"
    description = "List all text frames in the document."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        text_frames = self.get_collection(doc, "getTextFrames", "Document does not support text frames.")
        if isinstance(text_frames, dict):
            return text_frames

        frames = []
        for name in text_frames.getElementNames():
            try:
                frame = text_frames.getByName(name)
                size = frame.getPropertyValue("Size")

                # Text content preview (first 200 chars)
                content_preview = ""
                try:
                    frame_text = frame.getText()
                    cursor = frame_text.createTextCursor()
                    cursor.gotoStart(False)
                    cursor.gotoEnd(True)
                    full_text = cursor.getString()
                    if len(full_text) > 200:
                        content_preview = full_text[:200] + "..."
                    else:
                        content_preview = full_text
                except Exception:
                    pass

                frames.append({
                    "name": name,
                    "width_mm": size.Width / 100.0,
                    "height_mm": size.Height / 100.0,
                    "width_100mm": size.Width,
                    "height_100mm": size.Height,
                    "content_preview": content_preview,
                })
            except Exception as e:
                log.debug("list_text_frames: skip '%s': %s", name, e)

        return {"status": "ok", "frames": frames, "count": len(frames)}


# ------------------------------------------------------------------
# GetTextFrameInfo
# ------------------------------------------------------------------

class GetTextFrameInfo(ToolWriterLayoutBase):
    """Get detailed info about a text frame."""

    name = "get_text_frame_info"
    description = "Get detailed info about a text frame."
    parameters = {
        "type": "object",
        "properties": {
            "frame_name": {
                "type": "string",
                "description": "Name of the text frame (from list_text_frames).",
            },
        },
        "required": ["frame_name"],
    }

    def execute(self, ctx, **kwargs):
        frame_name = kwargs.get("frame_name", "")
        if not frame_name:
            return self._tool_error("frame_name is required.")

        frame = self.get_item(
            ctx.doc, "getTextFrames", frame_name,
            missing_msg="Document does not support text frames.",
            not_found_msg="Text frame '%s' not found." % frame_name
        )
        if isinstance(frame, dict):
            return frame

        size = frame.getPropertyValue("Size")

        # Anchor type
        anchor_type = None
        try:
            anchor_type = int(frame.getPropertyValue("AnchorType").value)
        except Exception:
            try:
                anchor_type = int(frame.getPropertyValue("AnchorType"))
            except Exception:
                pass

        # Orientation
        hori_orient = None
        vert_orient = None
        try:
            hori_orient = int(frame.getPropertyValue("HoriOrient"))
        except Exception:
            pass
        try:
            vert_orient = int(frame.getPropertyValue("VertOrient"))
        except Exception:
            pass

        # Full text content
        content = ""
        try:
            frame_text = frame.getText()
            cursor = frame_text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            content = cursor.getString()
        except Exception:
            pass

        # Paragraph index via anchor
        paragraph_index = -1
        try:
            anchor = frame.getAnchor()
            doc_svc = ctx.services.document
            para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)
            text_obj = ctx.doc.getText()
            paragraph_index = doc_svc.find_paragraph_for_range(
                anchor, para_ranges, text_obj
            )
        except Exception:
            pass

        return {
            "status": "ok",
            "frame_name": frame_name,
            "width_mm": size.Width / 100.0,
            "height_mm": size.Height / 100.0,
            "width_100mm": size.Width,
            "height_100mm": size.Height,
            "anchor_type": anchor_type,
            "hori_orient": hori_orient,
            "vert_orient": vert_orient,
            "content": content,
            "paragraph_index": paragraph_index,
        }


# ------------------------------------------------------------------
# SetTextFrameProperties
# ------------------------------------------------------------------

class SetTextFrameProperties(ToolWriterLayoutBase):
    """Resize or reposition a text frame."""

    name = "set_text_frame_properties"
    description = "Resize or reposition a text frame."
    parameters = {
        "type": "object",
        "properties": {
            "frame_name": {
                "type": "string",
                "description": "Name of the text frame (from list_text_frames).",
            },
            "width_mm": {
                "type": "number",
                "description": "New width in millimetres.",
            },
            "height_mm": {
                "type": "number",
                "description": "New height in millimetres.",
            },
            "anchor_type": {
                "type": "integer",
                "description": (
                    "Anchor type: 0=AT_PARAGRAPH, 1=AS_CHARACTER, "
                    "2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER."
                ),
            },
            "hori_orient": {
                "type": "integer",
                "description": "Horizontal orientation constant.",
            },
            "vert_orient": {
                "type": "integer",
                "description": "Vertical orientation constant.",
            },
        },
        "required": ["frame_name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        frame_name = kwargs.get("frame_name", "")
        if not frame_name:
            return self._tool_error("frame_name is required.")

        frame = self.get_item(
            ctx.doc, "getTextFrames", frame_name,
            missing_msg="Document does not support text frames.",
            not_found_msg="Text frame '%s' not found." % frame_name
        )
        if isinstance(frame, dict):
            return frame

        updated = []

        # Size
        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size
            current = frame.getPropertyValue("Size")
            new_size = Size()
            new_size.Width = int(width_mm * 100) if width_mm is not None else current.Width
            new_size.Height = int(height_mm * 100) if height_mm is not None else current.Height
            frame.setPropertyValue("Size", new_size)
            updated.append("size")

        # Anchor type
        anchor_type = kwargs.get("anchor_type")
        if anchor_type is not None:
            from com.sun.star.text.TextContentAnchorType import (
                AT_PARAGRAPH, AS_CHARACTER, AT_PAGE, AT_FRAME, AT_CHARACTER,
            )
            anchor_map = {
                0: AT_PARAGRAPH,
                1: AS_CHARACTER,
                2: AT_PAGE,
                3: AT_FRAME,
                4: AT_CHARACTER,
            }
            if anchor_type in anchor_map:
                frame.setPropertyValue("AnchorType", anchor_map[anchor_type])
                updated.append("anchor_type")

        # Orientation
        hori_orient = kwargs.get("hori_orient")
        if hori_orient is not None:
            frame.setPropertyValue("HoriOrient", hori_orient)
            updated.append("hori_orient")

        vert_orient = kwargs.get("vert_orient")
        if vert_orient is not None:
            frame.setPropertyValue("VertOrient", vert_orient)
            updated.append("vert_orient")

        return {
            "status": "ok",
            "frame_name": frame_name,
            "updated": updated,
        }

# ------------------------------------------------------------------
# GetPageStyleProperties
# ------------------------------------------------------------------

class GetPageStyleProperties(ToolWriterLayoutBase):
    """Get dimensions, margins, and header/footer states of a page style."""

    name = "get_page_style_properties"
    description = "Get dimensions, margins, and header/footer states of a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'.",
            },
        },
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "Standard")
        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            props = {
                "style_name": style_name,
                "width_mm": style.getPropertyValue("Width") / 100.0,
                "height_mm": style.getPropertyValue("Height") / 100.0,
                "is_landscape": style.getPropertyValue("IsLandscape"),
                "left_margin_mm": style.getPropertyValue("LeftMargin") / 100.0,
                "right_margin_mm": style.getPropertyValue("RightMargin") / 100.0,
                "top_margin_mm": style.getPropertyValue("TopMargin") / 100.0,
                "bottom_margin_mm": style.getPropertyValue("BottomMargin") / 100.0,
                "header_is_on": style.getPropertyValue("HeaderIsOn"),
                "footer_is_on": style.getPropertyValue("FooterIsOn"),
            }
            return {"status": "ok", "properties": props}
        except Exception as e:
            return self._tool_error(f"Error reading properties from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# SetPageStyleProperties
# ------------------------------------------------------------------

class SetPageStyleProperties(ToolWriterLayoutBase):
    """Modify dimensions, margins, and header/footer toggles of a page style."""

    name = "set_page_style_properties"
    description = "Modify dimensions, margins, and header/footer toggles of a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'.",
            },
            "width_mm": {"type": "number", "description": "New width in mm."},
            "height_mm": {"type": "number", "description": "New height in mm."},
            "is_landscape": {"type": "boolean", "description": "Set orientation to landscape."},
            "left_margin_mm": {"type": "number", "description": "Left margin in mm."},
            "right_margin_mm": {"type": "number", "description": "Right margin in mm."},
            "top_margin_mm": {"type": "number", "description": "Top margin in mm."},
            "bottom_margin_mm": {"type": "number", "description": "Bottom margin in mm."},
            "header_is_on": {"type": "boolean", "description": "Enable or disable header."},
            "footer_is_on": {"type": "boolean", "description": "Enable or disable footer."},
        },
        "required": [],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "Standard")
        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        updated = []
        try:
            if "width_mm" in kwargs:
                style.setPropertyValue("Width", int(kwargs["width_mm"] * 100))
                updated.append("width")
            if "height_mm" in kwargs:
                style.setPropertyValue("Height", int(kwargs["height_mm"] * 100))
                updated.append("height")
            if "is_landscape" in kwargs:
                style.setPropertyValue("IsLandscape", kwargs["is_landscape"])
                updated.append("is_landscape")
            if "left_margin_mm" in kwargs:
                style.setPropertyValue("LeftMargin", int(kwargs["left_margin_mm"] * 100))
                updated.append("left_margin")
            if "right_margin_mm" in kwargs:
                style.setPropertyValue("RightMargin", int(kwargs["right_margin_mm"] * 100))
                updated.append("right_margin")
            if "top_margin_mm" in kwargs:
                style.setPropertyValue("TopMargin", int(kwargs["top_margin_mm"] * 100))
                updated.append("top_margin")
            if "bottom_margin_mm" in kwargs:
                style.setPropertyValue("BottomMargin", int(kwargs["bottom_margin_mm"] * 100))
                updated.append("bottom_margin")
            if "header_is_on" in kwargs:
                style.setPropertyValue("HeaderIsOn", kwargs["header_is_on"])
                updated.append("header_is_on")
            if "footer_is_on" in kwargs:
                style.setPropertyValue("FooterIsOn", kwargs["footer_is_on"])
                updated.append("footer_is_on")
        except Exception as e:
            return self._tool_error(f"Error setting properties on page style '{style_name}': {e}")

        return {"status": "ok", "style_name": style_name, "updated": updated}


# ------------------------------------------------------------------
# GetHeaderFooterText
# ------------------------------------------------------------------

class GetHeaderFooterText(ToolWriterLayoutBase):
    """Retrieve the text content of a page style's header or footer."""

    name = "get_header_footer_text"
    description = "Retrieve the text content of a page style's header or footer."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'.",
            },
            "region": {
                "type": "string",
                "enum": ["header", "footer"],
                "description": "Whether to get the header or footer text.",
            },
        },
        "required": ["region"],
    }

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "Standard")
        region = kwargs.get("region")
        if not region:
            return self._tool_error("region is required ('header' or 'footer').")

        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            if region == "header":
                if not style.getPropertyValue("HeaderIsOn"):
                    return {"status": "ok", "style_name": style_name, "region": region, "content": "", "is_on": False}
                text_obj = style.getPropertyValue("HeaderText")
            else:
                if not style.getPropertyValue("FooterIsOn"):
                    return {"status": "ok", "style_name": style_name, "region": region, "content": "", "is_on": False}
                text_obj = style.getPropertyValue("FooterText")

            content = text_obj.getString() if text_obj else ""
            return {"status": "ok", "style_name": style_name, "region": region, "content": content, "is_on": True}
        except Exception as e:
            return self._tool_error(f"Error reading {region} text from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# SetHeaderFooterText
# ------------------------------------------------------------------

class SetHeaderFooterText(ToolWriterLayoutBase):
    """Set the text content of a page style's header or footer."""

    name = "set_header_footer_text"
    description = "Set the text content of a page style's header or footer. Automatically enables the header/footer if not already on."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'.",
            },
            "region": {
                "type": "string",
                "enum": ["header", "footer"],
                "description": "Whether to set the header or footer text.",
            },
            "content": {
                "type": "string",
                "description": "The text to insert into the header or footer.",
            },
        },
        "required": ["region", "content"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "Standard")
        region = kwargs.get("region")
        content = kwargs.get("content", "")

        if not region:
            return self._tool_error("region is required ('header' or 'footer').")

        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            if region == "header":
                style.setPropertyValue("HeaderIsOn", True)
                text_obj = style.getPropertyValue("HeaderText")
            else:
                style.setPropertyValue("FooterIsOn", True)
                text_obj = style.getPropertyValue("FooterText")

            if text_obj:
                text_obj.setString(content)
                return {"status": "ok", "style_name": style_name, "region": region, "updated": True}
            else:
                return self._tool_error(f"Could not retrieve text object for {region} on style '{style_name}'.")
        except Exception as e:
            return self._tool_error(f"Error writing to {region} text on page style '{style_name}': {e}")


# ------------------------------------------------------------------
# GetPageColumns
# ------------------------------------------------------------------

class GetPageColumns(ToolWriterLayoutBase):
    """Get the column layout for a page style."""

    name = "get_page_columns"
    description = "Get the column layout for a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "The name of the page style. Defaults to 'Standard'.",
            },
        },
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "Standard")
        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            text_columns = style.getPropertyValue("TextColumns")
            if not text_columns:
                return self._tool_error(f"TextColumns property not found on style '{style_name}'.")

            column_count = text_columns.getColumnCount()
            cols = text_columns.getColumns()

            columns_data = []
            for col in cols:
                columns_data.append({
                    "width": col.Width,
                    "left_margin_mm": col.LeftMargin / 100.0,
                    "right_margin_mm": col.RightMargin / 100.0,
                })

            return {"status": "ok", "style_name": style_name, "column_count": column_count, "columns": columns_data}
        except Exception as e:
            return self._tool_error(f"Error reading columns from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# SetPageColumns
# ------------------------------------------------------------------

class SetPageColumns(ToolWriterLayoutBase):
    """Set the number of columns and spacing for a page style."""

    name = "set_page_columns"
    description = "Set the number of columns and spacing for a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "The name of the page style. Defaults to 'Standard'.",
            },
            "column_count": {
                "type": "integer",
                "description": "Number of columns (e.g., 2).",
            },
            "spacing_mm": {
                "type": "number",
                "description": "Spacing between columns in mm. Defaults to 0.",
            },
        },
        "required": ["column_count"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "Standard")
        column_count = kwargs.get("column_count")
        spacing_mm = kwargs.get("spacing_mm", 0)

        if column_count is None or column_count < 1:
            return self._tool_error("column_count must be at least 1.")

        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            text_columns = style.getPropertyValue("TextColumns")
            if not text_columns:
                return self._tool_error(f"TextColumns property not found on style '{style_name}'.")

            text_columns.setColumnCount(column_count)
            cols = list(text_columns.getColumns())

            spacing = int(spacing_mm * 100)

            # Divide spacing between adjacent columns
            # Column 1 right margin gets half spacing, Column 2 left margin gets half, etc.
            if column_count > 1 and spacing > 0:
                half_spacing = spacing // 2
                for i in range(column_count - 1):
                    cols[i].RightMargin = half_spacing
                    cols[i + 1].LeftMargin = half_spacing

            text_columns.setColumns(tuple(cols))
            style.setPropertyValue("TextColumns", text_columns)

            return {"status": "ok", "style_name": style_name, "column_count": column_count, "spacing_mm": spacing_mm}
        except Exception as e:
            return self._tool_error(f"Error setting columns on page style '{style_name}': {e}")


# ------------------------------------------------------------------
# InsertPageBreak
# ------------------------------------------------------------------

class InsertPageBreak(ToolWriterLayoutBase):
    """Insert a page break at the current cursor position."""

    name = "insert_page_break"
    description = "Insert a page break at the current cursor position."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc

        try:
            view_cursor = doc.getCurrentController().getViewCursor()
            if not view_cursor:
                return self._tool_error("Could not obtain view cursor.")

            text = view_cursor.getText()
            cursor = text.createTextCursorByRange(view_cursor)

            from com.sun.star.style.BreakType import PAGE_BEFORE
            cursor.setPropertyValue("BreakType", PAGE_BEFORE)

            # Optionally insert a paragraph break so the break actually applies cleanly
            text.insertControlCharacter(cursor, 0, False) # 0 = PARAGRAPH_BREAK

            return {"status": "ok", "message": "Page break inserted."}
        except Exception as e:
            return self._tool_error(f"Error inserting page break: {e}")
