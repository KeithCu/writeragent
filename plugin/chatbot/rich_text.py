# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""Rich text support for the chat sidebar using an embedded Writer document."""

import logging
import os
import tempfile
from plugin.chatbot.listeners import BaseWindowListener

log = logging.getLogger(__name__)

USER_COLOR = 0x2A6099
ASSISTANT_COLOR = 0x000000

_EMBEDDING_STARTED = set()

# Threshold in scrollbar units — if within this many units of max, treat as "at bottom"
_SCROLL_BOTTOM_THRESHOLD = 10


def find_vertical_scrollbar(frame):
    """Navigate the accessible tree of an embedded frame to find the vertical scrollbar.

    Returns the accessible object supporting XAccessibleValue, or None.
    The caller should cache the result to avoid repeated tree traversal.
    """
    try:
        from com.sun.star.accessibility import AccessibleRole

        comp_window = frame.getComponentWindow()
        if not comp_window:
            log.debug("find_vertical_scrollbar: no component window")
            return None
        accessible = comp_window.getAccessible()
        if not accessible:
            log.debug("find_vertical_scrollbar: no accessible on component window")
            return None
        ctx = accessible.getAccessibleContext()
        top_count = ctx.getAccessibleChildCount()
        log.debug("find_vertical_scrollbar: top-level accessible has %d children", top_count)
        for top_idx in range(top_count):
            top_child = ctx.getAccessibleChild(top_idx)
            top_ctx = top_child.getAccessibleContext()
            child_count = top_ctx.getAccessibleChildCount()
            log.debug("find_vertical_scrollbar: child[%d] has %d sub-children, role=%s name=%r", top_idx, child_count, top_ctx.getAccessibleRole(), top_ctx.getAccessibleName())
            for i in range(child_count):
                child = top_ctx.getAccessibleChild(i)
                child_ctx = child.getAccessibleContext()
                role = child_ctx.getAccessibleRole()
                name = child_ctx.getAccessibleName()
                if role == AccessibleRole.SCROLL_BAR:
                    log.debug("find_vertical_scrollbar: found SCROLL_BAR at [%d][%d] name=%r", top_idx, i, name)
                    try:
                        val = child.getCurrentValue()
                        max_val = child.getMaximumValue()
                        log.debug("find_vertical_scrollbar: current=%s max=%s", val, max_val)
                        return child
                    except AttributeError:
                        log.debug("find_vertical_scrollbar: SCROLL_BAR has no getCurrentValue")
                        continue
    except Exception as e:
        log.debug("find_vertical_scrollbar: %s", e)
    log.debug("find_vertical_scrollbar: no scrollbar found")
    return None


def is_scrolled_to_bottom(scrollbar_accessible):
    """Check if the scrollbar is at or near its maximum (bottom).

    Returns True if at bottom or if the state cannot be determined (safe default).
    """
    if scrollbar_accessible is None:
        return True
    try:
        current = scrollbar_accessible.getCurrentValue()
        maximum = scrollbar_accessible.getMaximumValue()
        at_bottom = current >= maximum - _SCROLL_BOTTOM_THRESHOLD
        log.debug("is_scrolled_to_bottom: current=%s max=%s -> %s", current, maximum, at_bottom)
        return at_bottom
    except Exception as e:
        log.debug("is_scrolled_to_bottom: exception reading scrollbar: %s", e)
        return True

class EmbeddedWriterListener(BaseWindowListener):
    """Wait for the sidebar window to be shown (realized) before embedding Writer."""
    
    def __init__(self, ctx, parent_window, placeholder_ctrl, on_ready_callback):
        self.ctx = ctx
        self.parent_window = parent_window
        self.placeholder_ctrl = placeholder_ctrl
        self.on_ready_callback = on_ready_callback
        self.initialized = False
        self.container_window = None

    def on_window_shown(self, rEvent):
        if self.initialized:
            return
            
        parent_id = id(self.parent_window)
        if parent_id in _EMBEDDING_STARTED:
            return
            
        log.debug("EmbeddedWriterListener.on_window_shown: checking for peer")
        peer = self.parent_window.getPeer()
        if peer:
            log.info("EmbeddedWriterListener: Parent window peer realized. Scheduling deferred embedding.")
            self.initialized = True
            _EMBEDDING_STARTED.add(parent_id)
            
            # Defer the actual embedding to break the synchronous recursion chain
            from plugin.framework.queue_executor import post_to_main_thread
            post_to_main_thread(self._deferred_init)
        else:
            log.debug("EmbeddedWriterListener: windowShown but still no peer.")

    def on_window_resized(self, rEvent):
        """Keep the embedded container synced with the placeholder size."""
        if self.container_window and self.placeholder_ctrl:
            try:
                ps = self.placeholder_ctrl.getPosSize()
                self.container_window.setPosSize(ps.X, ps.Y, ps.Width, ps.Height, 15)
            except Exception:
                pass

    def _deferred_init(self):
        """Perform the actual embedding on a fresh event loop turn."""
        try:
            doc, frame, container = create_embedded_writer_doc(self.ctx, self.parent_window, self.placeholder_ctrl)
            if doc and frame:
                self.container_window = container
                self.on_ready_callback(doc, frame, container)
            else:
                log.error("EmbeddedWriterListener: Failed to create embedded Writer doc.")
        except Exception as e:
            log.exception("Error in deferred rich text init: %s", e)

def create_embedded_writer_doc(ctx, parent_window, placeholder_ctrl):
    """Creates an embedded Writer document inside a new window parented to parent_window.
    
    Returns (doc, frame, container_window) or (None, None, None).
    """
    try:
        from com.sun.star.beans import PropertyValue
        from com.sun.star.awt import WindowDescriptor
        from com.sun.star.awt.WindowClass import CONTAINER

        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        
        # 1. Create a container window that will host the frame
        desc = WindowDescriptor()
        desc.Type = CONTAINER
        desc.WindowServiceName = ""
        desc.Parent = parent_window.getPeer()
        desc.WindowAttributes = 4 # VCL_WINDOWATTR_CLIPCHILDREN
        
        log.debug("create_embedded_writer_doc: desc.Type=%s desc.Parent=%s", desc.Type, desc.Parent)
        
        container_window = toolkit.createWindow(desc)
        if not container_window:
            log.error("create_embedded_writer_doc: toolkit.createWindow failed")
            return None, None, None
            
        # 2. Position it exactly over the placeholder (the old response TextField)
        ps = placeholder_ctrl.getPosSize()
        container_window.setPosSize(ps.X, ps.Y, ps.Width, ps.Height, 15) # X|Y|W|H
        container_window.setVisible(True)
        
        # 3. Create a Frame and plug it into our container window
        frame = smgr.createInstanceWithContext("com.sun.star.frame.Frame", ctx)
        frame.initialize(container_window)
        frame.setName("WriterAgent_ChatSidebar_EmbeddedWriter")
        
        # 4. Load an empty Writer document into this frame
        args = (
            PropertyValue("ReadOnly", 0, False, 0),
            PropertyValue("Hidden", 0, False, 0),
        )
        
        doc = frame.loadComponentFromURL("private:factory/swriter", "_self", 0, args)
        
        if not doc:
            log.error("create_embedded_writer_doc: loadComponentFromURL returned None")
            return None, None, None
            
        # 5. UI Polish: Hide toolbars and status bar
        layout = frame.LayoutManager
        if layout:
            layout.setVisible(False)

        # 6. Page and paragraph styles FIRST (before view settings, so zoom calculates correctly)
        try:
            style_families = doc.getStyleFamilies()

            # Page styles: zero margins, width matching the container
            if style_families.hasByName("PageStyles"):
                page_styles = style_families.getByName("PageStyles")
                pixel_w = placeholder_ctrl.getPosSize().Width
                mm100_w = int(pixel_w * 26.458)  # 1px ~ 0.26458mm at 96 DPI
                for i in range(page_styles.getCount()):
                    style = page_styles.getByIndex(i)
                    try:
                        style.Width = max(2000, mm100_w)
                        style.LeftMargin = 0
                        style.RightMargin = 0
                        style.TopMargin = 0
                        style.BottomMargin = 0
                        style.HeaderIsOn = False
                        style.FooterIsOn = False
                        style.BackColor = 0xFFFFFF
                    except Exception:
                        pass

            # Paragraph styles: zero indents
            if style_families.hasByName("ParagraphStyles"):
                para_styles = style_families.getByName("ParagraphStyles")
                if para_styles.hasByName("Standard"):
                    std_para = para_styles.getByName("Standard")
                    std_para.ParaLeftMargin = 0
                    std_para.ParaRightMargin = 0
                    std_para.ParaFirstLineIndent = 0
                    std_para.ParaTopMargin = 0
                    std_para.ParaBottomMargin = 200

            # Default font size
            import uno
            text = doc.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            cursor.CharHeight = 10.0

            # Set language to "none" (zxx) to suppress all spell/grammar checking
            if style_families.hasByName("ParagraphStyles"):
                from typing import cast, Any
                ps = style_families.getByName("ParagraphStyles")
                no_lang = cast("Any", uno.createUnoStruct("com.sun.star.lang.Locale"))
                no_lang.Language = "zxx"
                no_lang.Country = ""
                if ps.hasByName("Standard"):
                    std_para = ps.getByName("Standard")
                    std_para.CharLocale = no_lang
                    std_para.CharLocaleAsian = no_lang
                    std_para.CharLocaleComplex = no_lang


        except Exception as e:
            log.debug("Failed to set document styles: %s", e)

        # 7. View settings — enable Web/Browse mode so text reflows to fill the window
        controller = doc.getCurrentController()
        if controller:
            try:
                vs = controller.getViewSettings()

                # Web/Browse mode: text reflows to window width, no page boundaries.
                # Canonical property name in LO source is "ShowOnlineLayout".
                online_set = False
                for prop in ("ShowOnlineLayout", "IsOnlineLayout", "OnlineLayout"):
                    if hasattr(vs, prop):
                        setattr(vs, prop, True)
                        online_set = True
                        log.debug("Set %s = True", prop)
                        break
                if not online_set:
                    log.warning("Could not enable web/browse mode — no OnlineLayout property found")

                # Fixed 100% zoom
                if hasattr(vs, "ZoomType"):
                    vs.ZoomType = 3  # BY_VALUE
                if hasattr(vs, "ZoomValue"):
                    vs.ZoomValue = 100

                # Hide visual clutter
                for prop in ("ShowShadows", "ShowTextBoundaries", "ShowTableBoundaries", "ShowObjectBoundaries"):
                    if hasattr(vs, prop):
                        setattr(vs, prop, False)

                for prop in ("ShowRulers", "ShowHoriRuler", "ShowVertRuler", "ShowRuler"):
                    if hasattr(vs, prop):
                        setattr(vs, prop, False)

                if hasattr(vs, "ShowHoriScrollBar"):
                    vs.ShowHoriScrollBar = False
                if hasattr(vs, "ShowVertScrollBar"):
                    vs.ShowVertScrollBar = True

            except Exception as e:
                log.debug("Failed to set view settings: %s", e)

        # 8. Set document background color to white (area around the page in web view)
        try:
            config_provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
            node_args = (PropertyValue("nodepath", 0, "/org.openoffice.Office.UI/ColorScheme/ColorSchemes/org.openoffice.Office.UI:ColorScheme['LibreOffice']/DocColor", 0),)
            config_update = config_provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationUpdateAccess", node_args)
            if config_update and hasattr(config_update, "Color"):
                config_update.Color = 0xFFFFFF
                config_update.commitChanges()
        except Exception:
            log.debug("Could not set DocColor via ConfigurationProvider (non-fatal)")

        log.info("create_embedded_writer_doc: Successfully initialized embedded Writer")
        return doc, frame, container_window

    except Exception as e:
        log.exception("Error in create_embedded_writer_doc: %s", e)
        return None, None, None

def _insert_html_at_cursor(doc, cursor, html_fragment):
    """Import an HTML fragment into *doc* at *cursor* using Writer's HTML filter.

    Writes the fragment to a temp file and imports via ``insertDocumentFromURL``
    with the ``HTML (StarWriter)`` filter -- the same mechanism used by
    ``apply_document_content`` for document edits.
    """
    import uno
    from com.sun.star.beans import PropertyValue

    css = "ul, ol { margin-left: 0.2cm; padding-left: 0.3cm; }"
    wrapped = '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>' % (css, html_fragment)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as tmp:
            tmp.write(wrapped)
            tmp_path = tmp.name

        url = uno.systemPathToFileUrl(tmp_path)
        filter_props = (PropertyValue("FilterName", 0, "HTML (StarWriter)", 0),)
        cursor.insertDocumentFromURL(url, filter_props)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def scroll_to_bottom(doc):
    """Attempt to scroll the embedded document view to the bottom.

    NOTE: In an embedded Writer with Web/Online layout, standard UNO cursor and
    dispatch approaches do not reliably scroll the view. This is a best-effort
    implementation — `gotoEnd` on the view cursor is the least harmful approach
    that positions the caret at the end without jumping the view to the top.
    """
    try:
        controller = doc.getCurrentController()
        if not controller:
            return
        view_cursor = controller.getViewCursor()
        if view_cursor:
            view_cursor.gotoEnd(False)
    except Exception:
        pass


def append_rich_text(doc, text, role="assistant", auto_scroll=True):
    """Append a complete message to the embedded Writer document.

    Inserts a bold, colored role prefix (``You:`` / ``Assistant:``) then
    imports *text* as HTML via Writer's StarWriter HTML filter so that
    ``<strong>``, ``<em>``, ``<code>``, ``<ul>`` etc. render natively.

    If *auto_scroll* is False, the view is not moved after inserting content.
    """
    try:
        should_scroll = True

        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)

        if text_obj.getString():
            text_obj.insertString(cursor, "\n\n", False)

        # Bold colored role prefix
        start_pos = cursor.getStart()
        prefix = "You: " if role == "user" else "Assistant: "
        text_obj.insertString(cursor, prefix, False)

        prefix_range = text_obj.createTextCursorByRange(start_pos)
        prefix_range.gotoRange(cursor.getStart(), True)
        prefix_range.CharHeight = 10.0
        prefix_range.CharWeight = 150.0  # BOLD
        prefix_range.CharColor = USER_COLOR if role == "user" else ASSISTANT_COLOR

        # Body content via HTML import
        cursor.gotoEnd(False)
        content_start = cursor.getStart()

        if text and text.strip():
            html_tags = ("<p>", "<br", "</h", "<ul", "<ol", "<li", "<strong", "<em", "<code", "<pre", "<div", "<table")
            looks_html = any(tag in text.lower() for tag in html_tags)

            if looks_html:
                try:
                    _insert_html_at_cursor(doc, cursor, text)
                except Exception:
                    log.debug("HTML import failed, falling back to plain text insert")
                    cursor.gotoEnd(False)
                    text_obj.insertString(cursor, text, False)
            else:
                text_obj.insertString(cursor, text, False)

            # Apply role color to the inserted body text
            cursor.gotoEnd(False)
            body_range = text_obj.createTextCursorByRange(content_start)
            body_range.gotoRange(cursor.getStart(), True)
            body_range.CharColor = USER_COLOR if role == "user" else ASSISTANT_COLOR

        if should_scroll:
            scroll_to_bottom(doc)

    except Exception as e:
        log.exception("Error in append_rich_text: %s", e)


def append_text_chunk(doc, text, auto_scroll=True):
    """Append a plain-text chunk during streaming (no prefix, no HTML import).

    If *auto_scroll* is False, the view is not moved after inserting content.
    """
    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)
        cursor.CharColor = ASSISTANT_COLOR
        text_obj.insertString(cursor, text, False)
        log.debug("append_text_chunk: inserted %d chars, auto_scroll=%s", len(text), auto_scroll)

        if auto_scroll:
            scroll_to_bottom(doc)
    except Exception as e:
        log.exception("Error in append_text_chunk: %s", e)
