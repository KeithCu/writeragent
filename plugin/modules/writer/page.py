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
"""Writer page tools (page domain, specialized tier).

Page styles, margins, headers/footers, columns, and page breaks.
"""

from plugin.modules.writer.base import ToolWriterPageBase

# ------------------------------------------------------------------
# GetPageStyleProperties
# ------------------------------------------------------------------


class GetPageStyleProperties(ToolWriterPageBase):
    """Get dimensions, margins, and header/footer states of a page style."""

    name = "get_page_style_properties"
    description = "Get dimensions, margins, and header/footer states of a page style."
    parameters = {
        "type": "object",
        "properties": {"style_name": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."}},
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
                "gutter_margin_mm": style.getPropertyValue("GutterMargin") / 100.0,
                "header_is_on": style.getPropertyValue("HeaderIsOn"),
                "footer_is_on": style.getPropertyValue("FooterIsOn"),
                "header_is_shared": style.getPropertyValue("HeaderIsShared"),
                "footer_is_shared": style.getPropertyValue("FooterIsShared"),
                "header_height_mm": style.getPropertyValue("HeaderHeight") / 100.0,
                "footer_height_mm": style.getPropertyValue("FooterHeight") / 100.0,
                "header_body_distance_mm": style.getPropertyValue("HeaderBodyDistance") / 100.0,
                "footer_body_distance_mm": style.getPropertyValue("FooterBodyDistance") / 100.0,
                "back_color": style.getPropertyValue("BackColor"),
                "back_transparent": style.getPropertyValue("BackTransparent"),
                "numbering_type": style.getPropertyValue("NumberingType"),
                "footnote_height_mm": style.getPropertyValue("FootnoteHeight") / 100.0,
                "register_paragraph_style": style.getPropertyValue("RegisterParagraphStyle"),
            }
            # Attempt to safely fetch PageStyleLayout enum
            try:
                psl = style.getPropertyValue("PageStyleLayout")
                props["page_style_layout"] = psl.value if hasattr(psl, "value") else int(psl)
            except Exception:
                pass
            return {"status": "ok", "properties": props}
        except Exception as e:
            return self._tool_error(f"Error reading properties from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# SetPageStyleProperties
# ------------------------------------------------------------------


class SetPageStyleProperties(ToolWriterPageBase):
    """Modify dimensions, margins, and header/footer toggles of a page style."""

    name = "set_page_style_properties"
    description = "Modify dimensions, margins, and header/footer toggles of a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."},
            "width_mm": {"type": "number", "description": "New width in mm."},
            "height_mm": {"type": "number", "description": "New height in mm."},
            "is_landscape": {"type": "boolean", "description": "Set orientation to landscape."},
            "left_margin_mm": {"type": "number", "description": "Left margin in mm."},
            "right_margin_mm": {"type": "number", "description": "Right margin in mm."},
            "top_margin_mm": {"type": "number", "description": "Top margin in mm."},
            "bottom_margin_mm": {"type": "number", "description": "Bottom margin in mm."},
            "gutter_margin_mm": {"type": "number", "description": "Gutter margin in mm (for binding)."},
            "header_is_on": {"type": "boolean", "description": "Enable or disable header."},
            "footer_is_on": {"type": "boolean", "description": "Enable or disable footer."},
            "header_is_shared": {"type": "boolean", "description": "Share header between left/right pages."},
            "footer_is_shared": {"type": "boolean", "description": "Share footer between left/right pages."},
            "header_height_mm": {"type": "number", "description": "Absolute header height in mm."},
            "footer_height_mm": {"type": "number", "description": "Absolute footer height in mm."},
            "header_body_distance_mm": {"type": "number", "description": "Spacing from header to body in mm."},
            "footer_body_distance_mm": {"type": "number", "description": "Spacing from footer to body in mm."},
            "back_color": {"type": "integer", "description": "Background color (RGB long)."},
            "back_transparent": {"type": "boolean", "description": "Make background transparent."},
            "numbering_type": {"type": "integer", "description": "Numbering type enum (4=Arabic, 0=Roman)."},
            "footnote_height_mm": {"type": "number", "description": "Max footnote area height in mm."},
            "register_paragraph_style": {"type": "string", "description": "Register true reference style."},
            "page_style_layout": {"type": "integer", "description": "0=ALL, 1=LEFT, 2=RIGHT, 3=MIRRORED"},
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
            if "gutter_margin_mm" in kwargs:
                style.setPropertyValue("GutterMargin", int(kwargs["gutter_margin_mm"] * 100))
                updated.append("gutter_margin")
            if "header_is_on" in kwargs:
                style.setPropertyValue("HeaderIsOn", kwargs["header_is_on"])
                updated.append("header_is_on")
            if "footer_is_on" in kwargs:
                style.setPropertyValue("FooterIsOn", kwargs["footer_is_on"])
                updated.append("footer_is_on")
            if "header_is_shared" in kwargs:
                style.setPropertyValue("HeaderIsShared", kwargs["header_is_shared"])
                updated.append("header_is_shared")
            if "footer_is_shared" in kwargs:
                style.setPropertyValue("FooterIsShared", kwargs["footer_is_shared"])
                updated.append("footer_is_shared")
            if "header_height_mm" in kwargs:
                style.setPropertyValue("HeaderHeight", int(kwargs["header_height_mm"] * 100))
                updated.append("header_height")
            if "footer_height_mm" in kwargs:
                style.setPropertyValue("FooterHeight", int(kwargs["footer_height_mm"] * 100))
                updated.append("footer_height")
            if "header_body_distance_mm" in kwargs:
                style.setPropertyValue("HeaderBodyDistance", int(kwargs["header_body_distance_mm"] * 100))
                updated.append("header_body_distance")
            if "footer_body_distance_mm" in kwargs:
                style.setPropertyValue("FooterBodyDistance", int(kwargs["footer_body_distance_mm"] * 100))
                updated.append("footer_body_distance")
            if "back_color" in kwargs:
                style.setPropertyValue("BackColor", kwargs["back_color"])
                updated.append("back_color")
            if "back_transparent" in kwargs:
                style.setPropertyValue("BackTransparent", kwargs["back_transparent"])
                updated.append("back_transparent")
            if "numbering_type" in kwargs:
                style.setPropertyValue("NumberingType", kwargs["numbering_type"])
                updated.append("numbering_type")
            if "footnote_height_mm" in kwargs:
                style.setPropertyValue("FootnoteHeight", int(kwargs["footnote_height_mm"] * 100))
                updated.append("footnote_height")
            if "register_paragraph_style" in kwargs:
                style.setPropertyValue("RegisterParagraphStyle", kwargs["register_paragraph_style"])
                updated.append("register_paragraph_style")
            if "page_style_layout" in kwargs:
                from com.sun.star.style.PageStyleLayout import ALL, LEFT, RIGHT, MIRRORED

                m = {0: ALL, 1: LEFT, 2: RIGHT, 3: MIRRORED}
                val = m.get(kwargs["page_style_layout"])
                if val is not None:
                    style.setPropertyValue("PageStyleLayout", val)
                    updated.append("page_style_layout")
        except Exception as e:
            return self._tool_error(f"Error setting properties on page style '{style_name}': {e}")

        return {"status": "ok", "style_name": style_name, "updated": updated}


# ------------------------------------------------------------------
# GetHeaderFooterText
# ------------------------------------------------------------------


class GetHeaderFooterText(ToolWriterPageBase):
    """Retrieve the text content of a page style's header or footer."""

    name = "get_header_footer_text"
    description = "Retrieve the text content of a page style's header or footer."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."},
            "region": {"type": "string", "enum": ["header", "footer"], "description": "Whether to get the header or footer text."},
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


class SetHeaderFooterText(ToolWriterPageBase):
    """Set the text content of a page style's header or footer."""

    name = "set_header_footer_text"
    description = "Set the text content of a page style's header or footer. Automatically enables the header/footer if not already on."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."},
            "region": {"type": "string", "enum": ["header", "footer"], "description": "Whether to set the header or footer text."},
            "content": {"type": "string", "description": "The text to insert into the header or footer."},
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


class GetPageColumns(ToolWriterPageBase):
    """Get the column layout for a page style."""

    name = "get_page_columns"
    description = "Get the column layout for a page style."
    parameters = {"type": "object", "properties": {"style_name": {"type": "string", "description": "The name of the page style. Defaults to 'Standard'."}}, "required": []}

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
                columns_data.append({"width": col.Width, "left_margin_mm": col.LeftMargin / 100.0, "right_margin_mm": col.RightMargin / 100.0})

            return {"status": "ok", "style_name": style_name, "column_count": column_count, "columns": columns_data}
        except Exception as e:
            return self._tool_error(f"Error reading columns from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# SetPageColumns
# ------------------------------------------------------------------


class SetPageColumns(ToolWriterPageBase):
    """Set the number of columns and spacing for a page style."""

    name = "set_page_columns"
    description = "Set the number of columns and spacing for a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "The name of the page style. Defaults to 'Standard'."},
            "column_count": {"type": "integer", "description": "Number of columns (e.g., 2)."},
            "spacing_mm": {"type": "number", "description": "Spacing between columns in mm. Defaults to 0."},
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


class InsertPageBreak(ToolWriterPageBase):
    """Insert a page break at the current cursor position."""

    name = "insert_page_break"
    description = "Insert a page break at the current cursor position."
    parameters = {"type": "object", "properties": {}, "required": []}
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
            text.insertControlCharacter(cursor, 0, False)  # 0 = PARAGRAPH_BREAK

            return {"status": "ok", "message": "Page break inserted."}
        except Exception as e:
            return self._tool_error(f"Error inserting page break: {e}")
