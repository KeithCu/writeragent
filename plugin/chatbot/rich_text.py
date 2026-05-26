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
import re
import tempfile
from typing import Any, cast
from plugin.chatbot.listeners import BaseWindowListener

log = logging.getLogger(__name__)

_HAVE_UNO_DOC_EVENTS = False
_XDocumentEventListener: Any = None
_unohelper: Any = None
try:
    import unohelper as _unohelper_impl
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl
    _unohelper = _unohelper_impl
    _XDocumentEventListener = _XDocumentEventListener_impl
    _HAVE_UNO_DOC_EVENTS = True
except ImportError:
    pass


_HTML_TAG_RE = re.compile(
    r"<(?:"
    r"p[>\s/]"
    r"|br[\s/>]"
    r"|/h[1-6]"
    r"|ul[\s/>]"
    r"|ol[\s/>]"
    r"|li[\s/>]"
    r"|strong[\s/>]"
    r"|em[\s/>]"
    r"|code[\s/>]"
    r"|pre[\s/>]"
    r"|div[\s/>]"
    r"|table[\s/>]"
    r")",
    re.IGNORECASE,
)

USER_COLOR = 0x2A6099
ASSISTANT_COLOR = 0x1E293B

_EMBEDDING_STARTED: set[int] = set()

# Threshold in scrollbar units — if within this many units of max, treat as "at bottom"
_SCROLL_BOTTOM_THRESHOLD = 10

# One-time dump guards for scroll debugging
_SCROLL_DEBUG_DUMPED = False
_VCL_SCROLLBAR_CACHE: Any = None
_VCL_SCROLLBAR_SEARCHED = False


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

if _HAVE_UNO_DOC_EVENTS:
    assert _unohelper is not None
    assert _XDocumentEventListener is not None

    class _SidebarDocumentEventListenerImpl(_unohelper.Base, _XDocumentEventListener):
        def __init__(self, listener):
            super().__init__()
            self._listener = listener

        def documentEventOccured(self, Event):
            try:
                name = getattr(Event, "EventName", "") or ""
                if name in ("OnPrepareUnload", "OnUnload"):
                    log.info("[RICH-SHUTDOWN] SidebarDocumentEventListener: %s event triggered. Cleaning up embedded objects.", name)
                    self._listener.disposing(None)
            except Exception as e:
                log.info("[RICH-SHUTDOWN] SidebarDocumentEventListener documentEventOccured error: %s", e)

        def disposing(self, Source):
            try:
                log.info("[RICH-SHUTDOWN] SidebarDocumentEventListener: Host document model is disposing. Cleaning up embedded objects.")
                self._listener.disposing(Source)
            except Exception as e:
                log.info("[RICH-SHUTDOWN] SidebarDocumentEventListener disposing error: %s", e)

    SidebarDocumentEventListener = _SidebarDocumentEventListenerImpl
else:
    class _SidebarDocumentEventListenerStub:
        def __init__(self, listener):
            self._listener = listener

        def documentEventOccured(self, Event):
            pass

        def disposing(self, Source):
            pass

    SidebarDocumentEventListener = _SidebarDocumentEventListenerStub  # type: ignore[assignment, misc]


class EmbeddedWriterListener(BaseWindowListener):
    """Manages the lifetime of an embedded Writer document used for rich-text
    rendering inside a sidebar panel.

    The embedded content (XFrame + swriter document + container window) must be
    explicitly closed before the parent sidebar dialog is torn down by VCL,
    otherwise LibreOffice tends to crash with Signal 11 during DeInitVCL.

    Disposal strategy (as of 2026-06):
    - Primary reliable trigger: XDocumentEventListener on the host document
      model (OnPrepareUnload / OnUnload). This is registered in __init__ when
      a doc_model is provided.
    - Proactive path: on_window_hidden (when the sidebar panel is hidden).
    - Safety net: the normal XEventListener.disposing path.

    All paths funnel through _initiate_disposal() → _dispose_embedded_objects(),
    which closes the frame first, then the document and container (with a
    peer-alive guard on the container window).
    """

    def __init__(self, ctx, parent_window, placeholder_ctrl, on_ready_callback, doc_model=None):
        self.ctx = ctx
        self.parent_window = parent_window
        self.placeholder_ctrl = placeholder_ctrl
        self.on_ready_callback = on_ready_callback
        self.doc_model = doc_model
        self.initialized = False
        self.container_window = None
        self.doc = None
        self.frame = None          # XFrame — we store it so disposing() can close it first
        self._disposed = False
        self._doc_listener = None
        log.debug("[RICH-LIFECYCLE] EmbeddedWriterListener.__init__ parent_window=%s placeholder=%s doc_model=%s",
                  id(parent_window) if parent_window else None,
                  id(placeholder_ctrl) if placeholder_ctrl else None,
                  id(doc_model) if doc_model else None)

    def disposing(self, Source):
        """XEventListener safety-net disposal path.

        In practice the document model listener (see __init__) is the primary
        reliable trigger. This method remains as a backstop and for cases where
        the listener is torn down directly.
        """
        log.info("[RICH-LIFECYCLE] EmbeddedWriterListener.disposing called (Source=%s, _disposed=%s, initialized=%s)",
                 id(Source) if Source else None, self._disposed, self.initialized)

        if self._disposed:
            log.info("[RICH-SHUTDOWN] EmbeddedWriterListener.disposing called again (already disposed)")
            return

        log.info("[RICH-SHUTDOWN] EmbeddedWriterListener.disposing ENTERED (safety net path) for root_window id=%s", id(self.parent_window))

        self._initiate_disposal("disposing (safety net)")
        log.info("[RICH-SHUTDOWN] EmbeddedWriterListener.disposing COMPLETED")

    def _initiate_disposal(self, reason: str):
        """Centralized disposal trigger.

        This method exists to avoid duplicating the "remove listener + set flag +
        call real disposal" sequence in multiple places (disposing, on_window_hidden,
        and potentially future triggers).

        All real disposal paths should go through here (or call _dispose_embedded_objects
        directly only in exceptional cases).
        """
        if self._disposed:
            return

        log.info("[RICH-SHUTDOWN] Initiating disposal (%s)", reason)

        try:
            if self.parent_window and hasattr(self.parent_window, "removeWindowListener"):
                self.parent_window.removeWindowListener(self)
                log.info("[RICH-SHUTDOWN]   -> removed self as WindowListener (%s)", reason)
        except Exception as e:
            log.info("[RICH-SHUTDOWN]   -> removeWindowListener failed (%s): %s", reason, e)

        self._disposed = True
        self._dispose_embedded_objects()

    def _dispose_embedded_objects(self):
        """Perform the actual best-effort disposal of the embedded Writer objects.

        This method has *no* guard on `_disposed`. It is expected to be called
        only when the caller has already decided it is safe to run.

        Shutdown order (important for avoiding VCL crashes on dialog teardown):
        - Close the XFrame first (detaches the document + container window).
        - Then the document and container window.

        The frame is now stored on this listener precisely so this path can
        close it reliably.
        """
        # Preferred shutdown order for an embedded frame setup:
        # 1. Close the XFrame first (this detaches the document and the container window).
        # 2. Then the document and container window.
        # Closing the frame early is important to avoid the VCL using the
        # container_window after the parent dialog (VclBuilder) has started
        # destroying it — which was a major source of the Signal 11 crashes.
        
        # Check if the container window still has a valid GUI peer. If the peer is
        # already dead/disposed, calling close or dispose on UI components will
        # cause a crash (Signal 11/use-after-free) in late shutdown sequences.
        peer_alive = False
        try:
            if self.container_window and self.container_window.getPeer():
                peer_alive = True
        except Exception:
            pass

        log.info("[RICH-SHUTDOWN] _dispose_embedded_objects starting (frame=%s, container=%s, doc=%s, peer_alive=%s)",
                 bool(self.frame), bool(self.container_window), bool(getattr(self, 'doc', None)), peer_alive)

        for name, obj in (("frame", self.frame),
                          ("container_window", self.container_window),
                          ("doc", getattr(self, "doc", None))):
            if not obj:
                log.info("[RICH-SHUTDOWN]   skipping %s (None)", name)
                continue
            try:
                # Only skip container_window dispose if the GUI peer is dead.
                # frame and doc are logical UNO components and MUST be closed/disposed
                # to prevent LibreOffice from keeping them alive as orphaned objects
                # which causes crashes during global shutdown.
                if name == "container_window" and not peer_alive:
                    log.info("[RICH-SHUTDOWN]   skipping %s close/dispose (GUI peer is already dead)", name)
                    continue

                if hasattr(obj, "close"):
                    obj.close(True)
                    log.info("[RICH-SHUTDOWN]   closed %s (close(True))", name)
                elif hasattr(obj, "dispose"):
                    obj.dispose()
                    log.info("[RICH-SHUTDOWN]   disposed %s", name)
                else:
                    log.info("[RICH-SHUTDOWN]   %s has neither close nor dispose", name)
            except Exception as e:
                log.info("[RICH-SHUTDOWN]   %s close/dispose raised (expected in late shutdown): %s", name, e)

        # Unregister host doc model event listener
        if self._doc_listener and self.doc_model:
            try:
                if hasattr(self.doc_model, "removeDocumentEventListener"):
                    self.doc_model.removeDocumentEventListener(self._doc_listener)
                    log.info("[RICH-SHUTDOWN]   removed self as DocumentEventListener")
            except Exception as e:
                log.info("[RICH-SHUTDOWN]   removeDocumentEventListener failed: %s", e)
            self._doc_listener = None

        self.frame = None
        self.container_window = None
        self.doc = None
        log.info("[RICH-SHUTDOWN] _dispose_embedded_objects finished and refs cleared")

    def on_window_shown(self, rEvent):
        log.debug("[RICH-LIFECYCLE] EmbeddedWriterListener.on_window_shown called _disposed=%s initialized=%s",
                  self._disposed, self.initialized)

        if self._disposed or self.initialized:
            return
            
        parent_id = id(self.parent_window)
        if parent_id in _EMBEDDING_STARTED:
            log.debug("[RICH-LIFECYCLE] on_window_shown: already in _EMBEDDING_STARTED, skipping")
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
        log.debug("[RICH-LIFECYCLE] on_window_resized (disposed=%s)", self._disposed)
        if self._disposed:
            return
        if self.container_window and self.placeholder_ctrl:
            try:
                ps = self.placeholder_ctrl.getPosSize()
                self.container_window.setPosSize(ps.X, ps.Y, ps.Width, ps.Height, 15)
                if hasattr(self, "doc") and self.doc:
                    scroll_to_bottom(self.doc)
            except Exception:
                pass

    def on_window_hidden(self, rEvent):
        log.info("[RICH-LIFECYCLE] EmbeddedWriterListener.on_window_hidden called (disposed=%s, has_frame=%s)",
                 self._disposed, bool(self.frame))

        if self._disposed:
            return

        self._initiate_disposal("on_window_hidden (proactive)")
        log.info("[RICH-SHUTDOWN] EmbeddedWriterListener disposal completed via on_window_hidden path")

    def _deferred_init(self):
        """Perform the actual embedding on a fresh event loop turn."""
        log.info("[RICH-LIFECYCLE] _deferred_init starting (disposed=%s)", self._disposed)
        if self._disposed:
            return
        try:
            doc, frame, container = create_embedded_writer_doc(self.ctx, self.parent_window, self.placeholder_ctrl)
            if doc and frame:
                self.container_window = container
                self.doc = doc
                self.frame = frame
                log.info("[RICH-LIFECYCLE] _deferred_init success — calling on_ready_callback")

                # Register document event listener on the host document model
                if self.doc_model and _HAVE_UNO_DOC_EVENTS:
                    try:
                        self._doc_listener = SidebarDocumentEventListener(self)
                        if hasattr(self.doc_model, "addDocumentEventListener"):
                            self.doc_model.addDocumentEventListener(self._doc_listener)
                            log.info("[RICH-LIFECYCLE] EmbeddedWriterListener: Registered SidebarDocumentEventListener on host doc_model id=%s", id(self.doc_model))
                    except Exception as e:
                        log.warning("[RICH-LIFECYCLE] Failed to register document event listener: %s", e)

                self.on_ready_callback(doc, frame, container)
            else:
                log.error("EmbeddedWriterListener: Failed to create embedded Writer doc.")
        except Exception as e:
            log.exception("Error in deferred rich text init: %s", e)

def get_theme_colors(doc):
    """Retrieve theme-aware colors based on the document window's StyleSettings.

    Returns (bg_color, user_color, assistant_color).
    """
    try:
        controller = doc.getCurrentController()
        if controller:
            frame = controller.getFrame()
            if frame:
                win = frame.getContainerWindow()
                if win and hasattr(win, "StyleSettings"):
                    style_settings = win.StyleSettings
                    if style_settings:
                        field_color = getattr(style_settings, "FieldColor", 0xFFFFFF)
                        if isinstance(field_color, int):
                            r = (field_color >> 16) & 0xFF
                            g = (field_color >> 8) & 0xFF
                            b = field_color & 0xFF
                            luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b

                            if luminance < 128:
                                # Dark mode colors
                                return field_color, 0x60A5FA, 0xE2E8F0
                            else:
                                # Light mode colors
                                # Dynamically darken DialogColor slightly (by 6%) to create a beautiful, soft contrast
                                dialog_color = getattr(style_settings, "DialogColor", 0xEFF0F1)
                                if isinstance(dialog_color, int):
                                    r = int(((dialog_color >> 16) & 0xFF) * 0.94)
                                    g = int(((dialog_color >> 8) & 0xFF) * 0.94)
                                    b = int((dialog_color & 0xFF) * 0.94)
                                    light_bg = (r << 16) | (g << 8) | b
                                    return light_bg, 0x2A6099, 0x1E293B
                                return 0xE0E1E2, 0x2A6099, 0x1E293B
    except Exception as e:
        log.debug("Failed to resolve theme colors from StyleSettings: %s", e)
    return 0xE0E1E2, 0x2A6099, 0x1E293B


def create_embedded_writer_doc(ctx, parent_window, placeholder_ctrl):
    """Creates an embedded Writer document inside a new window parented to parent_window.
    
    Returns (doc, frame, container_window) or (None, None, None).
    """
    try:
        from com.sun.star.beans import PropertyValue
        from com.sun.star.awt import WindowDescriptor
        from com.sun.star.awt.WindowClass import CONTAINER

        # Resolve initial background color based on StyleSettings
        bg_color = 0xE0E1E2
        if parent_window and hasattr(parent_window, "StyleSettings"):
            style_settings = parent_window.StyleSettings
            if style_settings:
                field_color = getattr(style_settings, "FieldColor", 0xFFFFFF)
                if isinstance(field_color, int):
                    r = (field_color >> 16) & 0xFF
                    g = (field_color >> 8) & 0xFF
                    b = field_color & 0xFF
                    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
                    if luminance < 128:
                        bg_color = field_color
                    else:
                        dialog_color = getattr(style_settings, "DialogColor", 0xEFF0F1)
                        if isinstance(dialog_color, int):
                            r = int(((dialog_color >> 16) & 0xFF) * 0.94)
                            g = int(((dialog_color >> 8) & 0xFF) * 0.94)
                            b = int((dialog_color & 0xFF) * 0.94)
                            bg_color = (r << 16) | (g << 8) | b
                        else:
                            bg_color = 0xE0E1E2

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
                        style.BackColor = bg_color
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
                    std_para.CharFontName = "Liberation Sans"
                    std_para.CharFontNameAsian = "Liberation Sans"
                    std_para.CharFontNameComplex = "Liberation Sans"

            # Default font size
            import uno
            text = doc.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            cursor.CharHeight = 10.0

            # Set language to "none" (zxx) to suppress all spell/grammar checking
            if style_families.hasByName("ParagraphStyles"):
                from typing import cast
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
                config_update.Color = bg_color
                config_update.commitChanges()
        except Exception:
            log.debug("Could not set DocColor via ConfigurationProvider (non-fatal)")

        log.info("[RICH-SHUTDOWN] create_embedded_writer_doc: Successfully initialized embedded Writer (frame id=%s, container id=%s)", id(frame), id(container_window))
        return doc, frame, container_window

    except Exception as e:
        log.exception("Error in create_embedded_writer_doc: %s", e)
        return None, None, None

def _tighten_list_indent(body_range):
    """Tighten indentation on list paragraphs within *body_range*.

    The HTML filter imports <ul>/<ol> as indented paragraphs using ParaLeftMargin
    (not Writer's NumberingRules mechanism). This function detects paragraphs with
    non-zero ParaLeftMargin and reduces them to tight values suitable for the
    narrow sidebar.
    """
    import uno
    try:
        enum = body_range.createEnumeration()
    except Exception as e:
        log.debug("_tighten_list_indent: createEnumeration failed: %s", e)
        return

    para_count = 0
    tightened = 0
    processed_levels = set()
    while enum.hasMoreElements():
        para = enum.nextElement()
        para_count += 1
        try:
            if not para.getPropertyValue("NumberingIsNumber"):
                continue
        except Exception:
            continue

        try:
            level = para.getPropertyValue("NumberingLevel")
            list_id = para.getPropertyValue("ListId")
        except Exception:
            continue

        key = (list_id, level)
        if key in processed_levels:
            continue
        processed_levels.add(key)

        try:
            rules = para.getPropertyValue("NumberingRules")
            props = list(rules.getByIndex(level))
            # Read the existing FirstLineOffset so we can position the bullet
            # with a small left gap while preserving the original bullet-to-text spacing
            flo = 0
            for p in props:
                if p.Name == "FirstLineOffset":
                    flo = p.Value
                    break
            for p in props:
                if p.Name == "LeftMargin":
                    log.debug("_tighten_list_indent: level=%d orig LeftMargin=%s text=%r", level, p.Value, para.getString()[:40])
                    p.Value = abs(flo) + 115 + level * 225
            any_props = uno.Any("[]com.sun.star.beans.PropertyValue", cast("Any", tuple(props)))  # type: ignore[attr-defined]
            uno.invoke(rules, "replaceByIndex", (level, any_props))
            para.NumberingRules = rules
            tightened += 1
        except Exception as e:
            log.debug("_tighten_list_indent: failed for level %d: %s", level, e)

    log.debug("_tighten_list_indent: scanned %d paragraphs, tightened %d", para_count, tightened)


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


_LOGGED_ACCESSIBLE_TREE = False


def find_accessible_window_recursive(win):
    """Recursively search for a window that supports XAccessible."""
    if not win:
        return None
    try:
        import uno
        accessible = win.queryInterface(uno.getTypeByName("com.sun.star.accessibility.XAccessible"))
        if accessible:
            return accessible
    except Exception:
        pass
        
    try:
        if hasattr(win, "getWindows"):
            for child in win.getWindows():
                res = find_accessible_window_recursive(child)
                if res:
                    return res
    except Exception:
        pass
    return None


def traverse_window_tree(win, results, depth=0):
    """Recursively map the entire VCL window peer hierarchy."""
    if not win:
        return
    try:
        impl_name = "?"
        try:
            impl_name = win.getImplementationName()
        except Exception:
            pass
        has_acc = "No"
        try:
            import uno
            acc = win.queryInterface(uno.getTypeByName("com.sun.star.accessibility.XAccessible"))
            if acc:
                has_acc = "Yes"
        except Exception:
            pass
        results.append((depth, impl_name, has_acc))
        
        if hasattr(win, "getWindows"):
            for child in win.getWindows():
                traverse_window_tree(child, results, depth + 1)
    except Exception as e:
        results.append((depth, "ERROR", str(e)))


def traverse_accessible_tree(accessible_obj, results, depth=0):
    """Recursively traverse the accessible tree and record role names and names."""
    if not accessible_obj:
        return
    try:
        ctx = accessible_obj.getAccessibleContext()
        if not ctx:
            return
        role = ctx.getAccessibleRole()
        name = ctx.getAccessibleName()
        
        # Translate role to constant string name if possible
        role_name = str(role)
        try:
            from com.sun.star.accessibility import AccessibleRole
            for attr in dir(AccessibleRole):
                if getattr(AccessibleRole, attr) == role:
                    role_name = attr
                    break
        except Exception:
            pass
            
        results.append((depth, role_name, name))
        
        if depth > 12:
            return
            
        count = ctx.getAccessibleChildCount()
        for i in range(count):
            child = ctx.getAccessibleChild(i)
            traverse_accessible_tree(child, results, depth + 1)
    except Exception as e:
        results.append((depth, "ERROR", str(e)))


def find_vcl_scrollbar(frame, container_window=None):
    """Traverse VCL window peers to find a scrollbar supporting XScrollBar.

    Walks getWindows() recursion from the frame's component window and optional
    container_window, looking for objects whose implementation name contains
    "Scroll" or that support com.sun.star.awt.XScrollBar.

    Returns the scrollbar peer or None.
    """
    candidates = []
    try:
        comp_window = frame.getComponentWindow() if frame else None
        roots = [w for w in (comp_window, container_window) if w]

        for root in roots:
            _find_scrollbar_in_tree(root, candidates, depth=0)

        if candidates:
            log.info("find_vcl_scrollbar: found %d candidate(s)", len(candidates))
            for impl, obj in candidates:
                log.info("  candidate impl=%s", impl)
            return candidates[0][1]
    except Exception as e:
        log.debug("find_vcl_scrollbar: %s", e)
    return None


def _find_scrollbar_in_tree(win, candidates, depth=0):
    """Recursively search VCL window tree for scrollbar-like objects."""
    if not win or depth > 15:
        return
    try:
        impl_name = ""
        try:
            impl_name = win.getImplementationName() or ""
        except Exception:
            pass

        # Check by implementation name
        if "scroll" in impl_name.lower():
            candidates.append((impl_name, win))
            log.info("_find_scrollbar_in_tree: depth=%d impl=%s (name match)", depth, impl_name)

        # Check XScrollBar interface
        try:
            import uno
            xsb = win.queryInterface(uno.getTypeByName("com.sun.star.awt.XScrollBar"))
            if xsb:
                candidates.append((impl_name + " [XScrollBar]", xsb))
                log.info("_find_scrollbar_in_tree: depth=%d impl=%s supports XScrollBar", depth, impl_name)
        except Exception:
            pass

        # Recurse into children
        if hasattr(win, "getWindows"):
            try:
                children = win.getWindows()
                for child in children:
                    _find_scrollbar_in_tree(child, candidates, depth + 1)
            except Exception:
                pass
    except Exception as e:
        log.debug("_find_scrollbar_in_tree: depth=%d error: %s", depth, e)


def _dump_scroll_debug_once(doc, frame):
    """One-time dump of VCL window tree and accessible tree for scroll debugging."""
    global _SCROLL_DEBUG_DUMPED
    if _SCROLL_DEBUG_DUMPED:
        return
    _SCROLL_DEBUG_DUMPED = True

    log.info("=== SCROLL DEBUG: One-time VCL/Accessible tree dump ===")
    try:
        comp_window = frame.getComponentWindow() if frame else None
        container_window = frame.getContainerWindow() if frame else None

        # VCL window tree
        if comp_window:
            results = []
            traverse_window_tree(comp_window, results, depth=0)
            log.info("VCL Window Tree (componentWindow):")
            for depth, impl, has_acc in results:
                log.info("  %s impl=%s accessible=%s", "  " * depth, impl, has_acc)

        if container_window and container_window != comp_window:
            results = []
            traverse_window_tree(container_window, results, depth=0)
            log.info("VCL Window Tree (containerWindow):")
            for depth, impl, has_acc in results:
                log.info("  %s impl=%s accessible=%s", "  " * depth, impl, has_acc)

        # Accessible tree
        if comp_window:
            try:
                accessible = comp_window.getAccessible()
                if accessible:
                    results = []
                    traverse_accessible_tree(accessible, results, depth=0)
                    log.info("Accessible Tree (componentWindow):")
                    for depth, role_name, name in results:
                        log.info("  %s role=%s name=%r", "  " * depth, role_name, name)
            except Exception as e:
                log.info("  (accessible tree error: %s)", e)

        # View settings dump
        try:
            controller = doc.getCurrentController()
            if controller:
                vs = controller.getViewSettings()
                scroll_props = []
                for prop_name in dir(vs):
                    if any(k in prop_name.lower() for k in ("scroll", "visible", "caret", "online", "zoom")):
                        try:
                            val = getattr(vs, prop_name)
                            if not callable(val):
                                scroll_props.append((prop_name, val))
                        except Exception:
                            pass
                if scroll_props:
                    log.info("ViewSettings scroll/visible props:")
                    for pn, pv in scroll_props:
                        log.info("  %s = %s", pn, pv)
        except Exception as e:
            log.info("  (view settings dump error: %s)", e)

        # === Critical: Log the raw ViewData string (this is the viewport scroll state) ===
        # We have never seen the actual payload on this machine because previous
        # write attempts masked the successful read. Log it at INFO the first time.
        try:
            ctl = doc.getCurrentController() if doc else None
            if ctl:
                vd = getattr(ctl, "ViewData", None)
                if vd and isinstance(vd, str):
                    log.info("RAW CONTROLLER VIEWDATA (first seen): %s", vd)
                elif vd is not None:
                    log.info("controller.ViewData present but not str: type=%s repr=%r", type(vd), vd)
                else:
                    log.info("controller has no ViewData attribute or it is None")
        except Exception as e:
            log.info("  (failed to read controller.ViewData during dump: %s)", e)

    except Exception as e:
        log.info("_dump_scroll_debug_once error: %s", e)
    log.info("=== END SCROLL DEBUG DUMP ===")


def _get_cached_vcl_scrollbar(frame, container_window=None):
    """Return the cached VCL scrollbar, searching on first call."""
    global _VCL_SCROLLBAR_CACHE, _VCL_SCROLLBAR_SEARCHED
    if not _VCL_SCROLLBAR_SEARCHED:
        _VCL_SCROLLBAR_SEARCHED = True
        _VCL_SCROLLBAR_CACHE = find_vcl_scrollbar(frame, container_window)
        if _VCL_SCROLLBAR_CACHE:
            log.info("_get_cached_vcl_scrollbar: cached scrollbar found")
        else:
            log.info("_get_cached_vcl_scrollbar: no VCL scrollbar found")
    return _VCL_SCROLLBAR_CACHE


def _sample_viewdata(controller, tag=""):
    """Read and log the compact ViewData for the embedded Online Writer view.

    Format observed on this machine: "679;784;100;284;284;5369;5884;0;0"
    We treat field[1] as the primary Y scroll position and the later fields
    as growing document size. This is called on every scroll_to_bottom so we
    can watch the numbers evolve (or stay stuck) during streaming.
    """
    try:
        vd = getattr(controller, "ViewData", None)
        if vd and isinstance(vd, str):
            parts = vd.split(";")
            # Log a compact human-readable sample at INFO so it is always visible
            # in a normal writeragent_debug.log without needing DEBUG level.
            log.info("VIEWDATA sample%s: %s  (Y~%s  doc~%s/%s)",
                     f"[{tag}]" if tag else "",
                     vd,
                     parts[1] if len(parts) > 1 else "?",
                     parts[5] if len(parts) > 5 else "?",
                     parts[6] if len(parts) > 6 else "?")
            return parts
    except Exception as e:
        log.debug("_sample_viewdata failed: %s", e)
    return None


def scroll_to_bottom(doc, aggressive: bool = False):
    """Scroll the embedded document view to the bottom (Online/Browse layout).

    The "aggressive" path (repeated zoom flicker + extra invalidate + per-call
    ViewData sampling) is only enabled on actual text insertion paths
    (append_text_chunk / append_rich_text when auto_scroll=True). All other
    callers (resize listener, debug menu, deferred rerender timer, etc.) use
    the lightweight path to avoid re-entrancy / infinite loops.

    The one-time tree + first ViewData dump still happens on the very first
    call regardless of the flag.
    """
    if not doc:
        return
    try:
        controller = doc.getCurrentController()
        if not controller:
            return

        frame = controller.getFrame()

        # One-time debug dump (tree + first raw ViewData) — happens only on the very first call
        if frame:
            _dump_scroll_debug_once(doc, frame)

        # 1. Core lightweight work that every caller gets (cursor + dispatch)
        view_cursor = controller.getViewCursor()
        if view_cursor:
            try:
                view_cursor.gotoEnd(False)
            except Exception as e:
                log.debug("view_cursor.gotoEnd failed: %s", e)

        if frame:
            try:
                from plugin.framework.uno_context import get_ctx
                ctx = get_ctx()
                if ctx:
                    smgr = getattr(ctx, "ServiceManager", getattr(ctx, "getServiceManager", lambda: None)())
                    if smgr:
                        dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
                        if dispatcher:
                            dispatcher.executeDispatch(frame, ".uno:GoToEndOfDoc", "", 0, ())
            except Exception as e:
                log.debug("scroll_to_bottom: GoToEndOfDoc dispatch failed: %s", e)

        # 2. Aggressive path (zoom flicker + extra invalidate + per-call sampling) ONLY on text inserts.
        #    All other callers (on_window_resized, deferred timers, debug force-scroll, etc.)
        #    take the lightweight path below to prevent infinite re-entrancy loops.
        if aggressive:
            _sample_viewdata(controller, tag="pre")

            # "Tell it to scroll to the end after doing an insert."
            # The plain-text path does this with response_control.setSelection(length, length)
            # (a collapsed caret at the absolute buffer end), which causes the AWT control
            # to scroll the view to make that caret visible.
            #
            # For the embedded Writer we do the exact analogue on the controller
            # (which implements XSelectionSupplier): create a fresh collapsed TextCursor
            # at the absolute document end and select() it. This must be done *after*
            # the insert so the document length is up-to-date. We do it here because
            # the aggressive path is only entered from the real append_text_chunk /
            # append_rich_text call sites (right after their insertString / HTML import).
            #
            # If a prior select() attempt scrolled to the top, it was because the
            # range passed had its *start* earlier in the document; a freshly
            # created .gotoEnd(False) caret is guaranteed collapsed at the very end.
            try:
                text = doc.getText()
                caret = text.createTextCursor()
                caret.gotoEnd(False)  # collapsed zero-width at absolute end
                controller.select(caret)
                log.info("scroll_to_bottom[aggressive]: controller.select(collapsed-end-caret) — the 'tell it' step")
                # Give VCL a chance to react to the selection change (the thing that
                # actually drives scrolling in the view).
                from plugin.framework.uno_context import get_toolkit as _get_sel_tk
                _sel_tk = _get_sel_tk()
                if _sel_tk and hasattr(_sel_tk, "processEventsToIdle"):
                    _sel_tk.processEventsToIdle()
            except Exception as e:
                log.debug("scroll_to_bottom[aggressive]: select(collapsed end) failed: %s", e)

            try:
                vs = controller.getViewSettings()
                if vs and hasattr(vs, "ZoomValue"):
                    orig = vs.ZoomValue
                    delta = 1 if orig < 150 else -1
                    vs.ZoomValue = orig + delta
                    from plugin.framework.uno_context import get_toolkit as _get_tk
                    tk = _get_tk()
                    if tk and hasattr(tk, "processEventsToIdle"):
                        tk.processEventsToIdle()
                    vs.ZoomValue = orig
                    if tk and hasattr(tk, "processEventsToIdle"):
                        tk.processEventsToIdle()
                    log.info("scroll_to_bottom[aggressive]: zoom flicker (%d->%d->%d)", orig, orig + delta, orig)
            except Exception as e:
                log.debug("scroll_to_bottom[aggressive]: zoom flicker failed: %s", e)

            try:
                if frame:
                    comp = frame.getComponentWindow()
                    if comp and hasattr(comp, "invalidate"):
                        comp.invalidate(15)
                from plugin.framework.uno_context import get_toolkit
                toolkit = get_toolkit()
                if toolkit and hasattr(toolkit, "processEventsToIdle"):
                    toolkit.processEventsToIdle()
            except Exception as e:
                log.debug("scroll_to_bottom[aggressive]: final invalidate/idle failed: %s", e)

            _sample_viewdata(controller, tag="post")
        else:
            # Lightweight path: one final idle so the basic cursor movement has a chance,
            # but nothing that can trigger resize listeners and cause loops.
            try:
                from plugin.framework.uno_context import get_toolkit
                toolkit = get_toolkit()
                if toolkit and hasattr(toolkit, "processEventsToIdle"):
                    toolkit.processEventsToIdle()
            except Exception:
                pass

    except Exception as e:
        log.debug("scroll_to_bottom error: %s", e)


def append_rich_text(doc, text, role="assistant", auto_scroll=True):
    """Append a complete message to the embedded Writer document.

    Inserts a bold, colored role prefix (``You:`` / ``Assistant:``) then
    imports *text* as HTML via Writer's StarWriter HTML filter so that
    ``<strong>``, ``<em>``, ``<code>``, ``<ul>`` etc. render natively.

    If *auto_scroll* is False, the view is not moved after inserting content.
    """
    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)

        bg_color, user_color, assistant_color = get_theme_colors(doc)

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
        prefix_range.CharColor = user_color if role == "user" else assistant_color

        # Body content via HTML import
        cursor.gotoEnd(False)
        pre_len = doc.CharacterCount

        if text and text.strip():
            looks_html = bool(_HTML_TAG_RE.search(text))
            log.debug("append_rich_text: looks_html=%s len=%d snippet=%r", looks_html, len(text), text[:120])

            if looks_html:
                try:
                    _insert_html_at_cursor(doc, cursor, text)
                except Exception:
                    log.debug("HTML import failed, falling back to plain text insert")
                    cursor.gotoEnd(False)
                    text_obj.insertString(cursor, text, False)
            else:
                text_obj.insertString(cursor, text, False)

            # Build a range covering only the newly inserted content
            body_range = text_obj.createTextCursor()
            body_range.gotoStart(False)
            body_range.goRight(pre_len, False)
            body_range.gotoEnd(True)
            body_range.CharColor = user_color if role == "user" else assistant_color
            _tighten_list_indent(body_range)

        if auto_scroll:
            scroll_to_bottom(doc, aggressive=True)

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
        bg_color, user_color, assistant_color = get_theme_colors(doc)
        cursor.CharColor = assistant_color
        text_obj.insertString(cursor, text, False)
        log.debug("append_text_chunk: inserted %d chars, auto_scroll=%s", len(text), auto_scroll)

        if auto_scroll:
            scroll_to_bottom(doc, aggressive=True)
    except Exception as e:
        log.exception("Error in append_text_chunk: %s", e)
