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
from com.sun.star.beans import PropertyValue
from com.sun.star.awt import WindowDescriptor
from com.sun.star.awt.WindowClass import CONTAINER
from plugin.chatbot.listeners import BaseWindowListener

log = logging.getLogger(__name__)

_EMBEDDING_STARTED = set()

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
            text = doc.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            cursor.CharHeight = 10.0

            # Disable spellcheck/grammar markers
            settings = doc.getSettings()
            if hasattr(settings, "ShowSpellErrors"):
                settings.ShowSpellErrors = False
            if hasattr(settings, "ShowGrammarErrors"):
                settings.ShowGrammarErrors = False

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

def append_rich_text(doc, text, role="assistant"):
    """Appends text to the embedded Writer document with role-based formatting and code blocks."""
    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)
        
        # Add a double newline if not empty
        if text_obj.getString():
            text_obj.insertString(cursor, "\n\n", False)
            
        # 1. Insert and format role prefix
        start_pos = cursor.getStart()
        prefix = "You: " if role == "user" else "Assistant: "
        text_obj.insertString(cursor, prefix, False)
        
        # Create a range for the prefix to apply formatting
        prefix_range = text_obj.createTextCursorByRange(start_pos)
        prefix_range.gotoRange(cursor.getStart(), True)
        
        # Formatting
        prefix_range.CharHeight = 10.0
        prefix_range.CharWeight = 150.0 # BOLD (com.sun.star.awt.FontWeight.BOLD)
        if role == "user":
            prefix_range.CharColor = 0x2A6099 # Modern Blue
        else:
            prefix_range.CharColor = 0x2E7D32 # Modern Green
            
        # 2. Insert content (with basic code block detection)
        cursor.gotoEnd(False)
        
        import re
        # Split by triple-backtick blocks
        parts = re.split(r'(```[\s\S]*?```)', text)
        for part in parts:
            if part.startswith('```') and part.endswith('```'):
                # Code block
                code_content = part[3:-3].strip()
                # Strip language hint if present (e.g. ```python\n...)
                code_content = re.sub(r'^[a-zA-Z0-9+#-]+\n', '', code_content)
                
                code_start = cursor.getStart()
                # Wrap code in newlines for better spacing
                text_obj.insertString(cursor, "\n" + code_content + "\n", False)
                
                code_range = text_obj.createTextCursorByRange(code_start)
                code_range.gotoRange(cursor.getStart(), True)
                
                # Apply "Code" look
                code_range.CharFontName = "Liberation Mono"
                code_range.CharHeight = 9.0
                code_range.CharBackColor = 0xF0F0F0 # Very light gray
            else:
                start_content = cursor.getStart()
                text_obj.insertString(cursor, part, False)
                content_range = text_obj.createTextCursorByRange(start_content)
                content_range.gotoRange(cursor.getStart(), True)
                content_range.CharHeight = 10.0
        
        # Ensure scroll to bottom (roughly)
        controller = doc.getCurrentController()
        if controller:
            controller.select(cursor)
            
    except Exception as e:
        log.exception("Error in append_rich_text: %s", e)
