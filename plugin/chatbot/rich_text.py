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
from plugin.framework.uno_listeners import (
    BaseDocumentEventListener,
    BaseCloseListener,
    BaseTerminateListener,
)

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

# Legacy plain-sidebar prefix; append_rich_text adds "Assistant:" instead.
_LEGACY_AI_LABEL_RE = re.compile(r"^\s*AI:\s*", re.IGNORECASE)


def strip_legacy_ai_label(text: str) -> str:
    """Remove leading ``AI:`` from greeting/assistant text (avoid ``Assistant: AI:``)."""
    if not text:
        return text
    return _LEGACY_AI_LABEL_RE.sub("", text, count=1)

USER_COLOR = 0x2A6099
ASSISTANT_COLOR = 0x1E293B

_EMBEDDING_STARTED: set[int] = set()

class SidebarDocumentEventListener(BaseDocumentEventListener):
    def __init__(self, listener):
        super().__init__()
        self._listener = listener

    def on_disposing(self, Source):
        try:
            log.info("[RICH-SHUTDOWN] SidebarDocumentEventListener: Host document model is disposing. Cleaning up embedded objects.")
            self._listener.disposing(Source)
        except Exception as e:
            log.info("[RICH-SHUTDOWN] SidebarDocumentEventListener disposing error: %s", e)


_HAVE_UNO_CLOSE_EVENTS = False
try:
    from com.sun.star.util import XCloseListener as _XCloseListener_impl  # noqa: F401
    _HAVE_UNO_CLOSE_EVENTS = True
except ImportError:
    pass

class SidebarCloseListener(BaseCloseListener):
    def __init__(self, listener):
        super().__init__()
        self._listener = listener

    def on_notify_closing(self, Source):
        try:
            log.info("[RICH-SHUTDOWN] SidebarCloseListener: Host frame is closing (notifyClosing). Cleaning up embedded objects.")
            self._listener._initiate_disposal("notifyClosing")
        except Exception as e:
            log.info("[RICH-SHUTDOWN] SidebarCloseListener notifyClosing error: %s", e)

    def on_disposing(self, Source):
        try:
            log.info("[RICH-SHUTDOWN] SidebarCloseListener: Host frame is disposing. Cleaning up embedded objects.")
            self._listener._initiate_disposal("disposing (frame)")
        except Exception as e:
            log.info("[RICH-SHUTDOWN] SidebarCloseListener disposing error: %s", e)


_HAVE_UNO_TERMINATE = False
try:
    from com.sun.star.frame import XTerminateListener as _XTerminateListener_type  # noqa: F401
    _HAVE_UNO_TERMINATE = True
except ImportError:
    pass

class SidebarTerminateListener(BaseTerminateListener):
    """XTerminateListener — fires before VCL teardown on app quit."""

    def __init__(self, listener):
        super().__init__()
        self._listener = listener

    def on_query_termination(self, Event):
        try:
            if self._listener and not self._listener._disposed:
                log.info("[RICH-SHUTDOWN] TerminateListener.queryTermination — disposing embedded objects (peers still alive)")
                self._listener._initiate_disposal("queryTermination")
        except Exception as e:
            log.info("[RICH-SHUTDOWN] TerminateListener.queryTermination error: %s", e)


class EmbeddedWriterListener(BaseWindowListener):
    """Manages the lifetime of an embedded Writer document used for rich-text
    rendering inside a sidebar panel.

    Primary disposal trigger: XTerminateListener.queryTermination on the
    Desktop — fires before any VCL teardown on app quit, while peers are alive.
    Secondary: XCloseListener.notifyClosing on the host frame (single-doc close).
    Safety net: disposing() on the listener / doc model.

    on_window_hidden is now used for early safe defang + child-relationship
    logging (user preference). It fires on tab switches etc. and lets us
    make the VCL child less "live" before a later close. We never set
    _disposed or do full disposal here (to avoid the cancel-save bug).
    """

    def __init__(self, ctx, parent_window, placeholder_ctrl, on_ready_callback, doc_model=None, host_frame=None):
        self.ctx = ctx
        self.parent_window = parent_window
        self.placeholder_ctrl = placeholder_ctrl
        self.on_ready_callback = on_ready_callback
        self.doc_model = doc_model
        self.host_frame = host_frame
        self.initialized = False
        self.container_window = None
        self.doc = None
        self.frame = None          # XFrame — we store it so disposing() can close it first
        self._disposed = False
        self._doc_listener = None
        self._close_listener = None
        self._terminate_listener = None
        log.debug("[RICH-LIFECYCLE] EmbeddedWriterListener.__init__ parent_window=%s placeholder=%s doc_model=%s host_frame=%s",
                  id(parent_window) if parent_window else None,
                  id(placeholder_ctrl) if placeholder_ctrl else None,
                  id(doc_model) if doc_model else None,
                  id(host_frame) if host_frame else None)

        if self.host_frame and _HAVE_UNO_CLOSE_EVENTS:
            try:
                self._close_listener = SidebarCloseListener(self)
                if hasattr(self.host_frame, "addCloseListener"):
                    self.host_frame.addCloseListener(self._close_listener)
                    log.info("[RICH-LIFECYCLE] EmbeddedWriterListener: Registered SidebarCloseListener on host_frame id=%s", id(self.host_frame))
            except Exception as e:
                log.warning("[RICH-LIFECYCLE] Failed to register close listener on host_frame: %s", e)

        # XTerminateListener on the Desktop — fires before any VCL teardown on
        # app quit, while peers are still alive.
        self._desktop = None
        try:
            smgr = ctx.getServiceManager()
            desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            if desktop and hasattr(desktop, "addTerminateListener"):
                self._terminate_listener = SidebarTerminateListener(self)
                desktop.addTerminateListener(self._terminate_listener)
                self._desktop = desktop
                log.info("[RICH-LIFECYCLE] EmbeddedWriterListener: Registered terminate listener on Desktop")
        except Exception as e:
            log.debug("[RICH-LIFECYCLE] Failed to register terminate listener: %s", e)

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
        """Full disposal — removes window listener and destroys embedded objects.

        Used by disposing() safety net (final teardown where no recovery is possible).
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

        If peer is alive: close frame first, then container, then doc.
        If peer is dead: VCL already tore down the native windows — just release
        refs. Calling close/dispose on dead VCL objects causes Signal 11.
        """
        peer_alive = False
        try:
            if self.container_window and self.container_window.getPeer():
                peer_alive = True
        except Exception:
            pass

        log.info("[RICH-SHUTDOWN] _dispose_embedded_objects starting (frame=%s, container=%s, doc=%s, peer_alive=%s)",
                 bool(self.frame), bool(self.container_window), bool(getattr(self, 'doc', None)), peer_alive)

        # Pure-Python instrumentation + defang (see method docstring for full rationale).
        # Must run before the close/dispose decision so we have the best snapshot
        # of the VCL parent/child relationship and the child is as "defanged" as
        # Python can make it.
        try:
            self._instrument_vcl_child_relationship_and_defang("inside _dispose_embedded_objects (peer_alive=%s)" % peer_alive)
        except Exception as e:
            log.info("[RICH-SHUTDOWN]   instrumentation/defang raised (swallowed): %s", e)

        if peer_alive:
            for name, obj in (("frame", self.frame),
                              ("container_window", self.container_window),
                              ("doc", getattr(self, "doc", None))):
                if not obj:
                    continue
                try:
                    if hasattr(obj, "close"):
                        obj.close(True)
                        log.info("[RICH-SHUTDOWN]   closed %s (close(True))", name)
                    elif hasattr(obj, "dispose"):
                        obj.dispose()
                        log.info("[RICH-SHUTDOWN]   disposed %s", name)
                except Exception as e:
                    log.info("[RICH-SHUTDOWN]   %s close/dispose raised: %s", name, e)
        else:
            log.info("[RICH-SHUTDOWN]   peer is dead — skipping all close/dispose, just releasing refs")

        if self._doc_listener and self.doc_model:
            try:
                if hasattr(self.doc_model, "removeDocumentEventListener"):
                    self.doc_model.removeDocumentEventListener(self._doc_listener)
                    log.info("[RICH-SHUTDOWN]   removed self as DocumentEventListener")
            except Exception as e:
                log.info("[RICH-SHUTDOWN]   removeDocumentEventListener failed: %s", e)
            self._doc_listener = None

        if self._close_listener and self.host_frame:
            try:
                if hasattr(self.host_frame, "removeCloseListener"):
                    self.host_frame.removeCloseListener(self._close_listener)
                    log.info("[RICH-SHUTDOWN]   removed self as CloseListener")
            except Exception as e:
                log.info("[RICH-SHUTDOWN]   removeCloseListener failed: %s", e)
            self._close_listener = None

        desktop = getattr(self, "_desktop", None)
        if self._terminate_listener and desktop:
            try:
                if hasattr(desktop, "removeTerminateListener"):
                    desktop.removeTerminateListener(self._terminate_listener)
                    log.info("[RICH-SHUTDOWN]   removed self as TerminateListener")
            except Exception as e:
                log.info("[RICH-SHUTDOWN]   removeTerminateListener failed: %s", e)
            self._terminate_listener = None

        self.frame = None
        self.container_window = None
        self.doc = None
        log.info("[RICH-SHUTDOWN] _dispose_embedded_objects finished and refs cleared")

    def _instrument_vcl_child_relationship_and_defang(self, reason: str):
        """Pure-Python diagnostics + hardening for the VclBuilder child-ownership crash.

        WHY THIS EXISTS (bug context):
        - We create the rich-text content area with:
            desc.Parent = sidebar_root.getPeer()
            container = toolkit.createWindow(desc)   # VCL child of the XDL dialog
          then host a full Frame + swriter doc inside it.
        - On close (app quit, "Don't Save", deck close while save dialog is up, etc.)
          the sidebar dialog's VCL peer is destroyed by VclBuilder::disposeBuilder
          (or the parent Window dispose walk) *before* reliable Python hooks fire in
          many sequences. Our still-registered child window ends up in an inconsistent
          state during that walk -> Signal 11 / "object has been disposed".
        - Previous hook chasing (OnPrepareUnload, model disposing, XTerminateListener,
          XCloseListener, XUIElement.disposing, on_window_hidden etc.) all fire at the
          wrong time relative to VCL native teardown (see docs/rich-text-sidebar.md).

        WHAT THIS DOES (pure Python only, no C++):
        1. Instrumentation: Uses the existing UNO-exposed getWindows() on VCL peers
           (the same mechanism already used by our scroll debug helpers) to walk the
           parent's child tree and log whether our container (by object identity on
           the peer) is *still present* as a child at the exact moment we enter
           disposal. This gives concrete runtime proof in writeragent_debug.log of
           the root cause without needing a debugger or C++ build.
        2. Defang (safe hardening): Before the peer-alive check, we aggressively
           hide + zero-size the container from the Python/UNO side. This is
           idempotent, exception-safe, and may reduce the amount of "live" state
           the C++ side sees when it later walks the child list. It is the best
           pure-Python approximation of "make this child uninteresting to the
           parent teardown" we can do.
        3. Drop-ownership attempt surface scan: We enumerate every plausible
           mutating call we can discover via hasattr / queryInterface on the
           container, its peer, and the parent's peer (setParent, removeChild,
           dispose variants, etc.). Results (or the AttributeError/UNO exceptions)
           are logged. As of this implementation, no such call successfully
           orphans the window from VCL's internal list (getWindows is read-only;
           there is no UNO-exposed equivalent of VclBuilder::drop_ownership or
           SetParent on an already-created awt container window). This documents
           why a pure-Python-only path cannot fully solve the problem and why
           the existing peer-alive guard + ordered close remains the best we have.

        Called from _dispose_embedded_objects (the authoritative path) and can be
        called defensively from other shutdown sites. All work is best-effort and
        swallowed; it must never turn a crash into a worse one or a hang.
        """
        if self._disposed:
            return

        log.info("[RICH-SHUTDOWN] _instrument..._and_defang ENTER (%s)", reason)

        parent_peer = None
        container_peer = None
        try:
            if self.parent_window and hasattr(self.parent_window, "getPeer"):
                parent_peer = self.parent_window.getPeer()
            if self.container_window and hasattr(self.container_window, "getPeer"):
                container_peer = self.container_window.getPeer()
        except Exception as e:
            log.info("[RICH-SHUTDOWN]   peer acquisition for instrumentation failed: %s", e)

        # --- Defang (pure-Python hardening, always safe) ---
        if self.container_window:
            try:
                self.container_window.setVisible(False)
                log.info("[RICH-SHUTDOWN]   defang: setVisible(False) on container")
            except Exception as e:
                log.debug("[RICH-SHUTDOWN]   defang setVisible failed (non-fatal): %s", e)
            try:
                # Zero-ish size; use the full PosSize flags (15) to force it.
                self.container_window.setPosSize(0, 0, 0, 0, 15)
                log.info("[RICH-SHUTDOWN]   defang: setPosSize(0,0,0,0) on container")
            except Exception as e:
                log.debug("[RICH-SHUTDOWN]   defang setPosSize failed (non-fatal): %s", e)

        # --- Instrumentation: is our container still a VCL child of the sidebar dialog? ---
        still_child = False
        if parent_peer and hasattr(parent_peer, "getWindows"):
            try:
                def _search_children(w, target, depth=0):
                    if depth > 20:
                        return False
                    if w is target:
                        return True
                    try:
                        for c in (w.getWindows() or []):
                            if _search_children(c, target, depth + 1):
                                return True
                    except Exception:
                        pass
                    return False

                if container_peer:
                    still_child = _search_children(parent_peer, container_peer)
                log.info("[RICH-SHUTDOWN]   VCL child check via getWindows(): container still registered under parent_peer? %s", still_child)
            except Exception as e:
                log.info("[RICH-SHUTDOWN]   VCL child walk failed: %s", e)
        else:
            log.info("[RICH-SHUTDOWN]   parent_peer has no getWindows(); cannot confirm child relationship from pure Python")

        # --- Pure-Python "drop / orphan" surface scan (exhaustive but safe) ---
        # We try every method name that could plausibly remove a child or change
        # parentage on the objects we hold. All are wrapped; failures are expected
        # and informative.
        attempted = []
        for obj, obj_name in ((self.container_window, "container_window"),
                              (container_peer, "container_peer"),
                              (parent_peer, "parent_peer")):
            if not obj:
                continue
            for meth_name in ("setParent", "SetParent", "removeChild", "RemoveChild",
                              "removeWindow", "RemoveWindow", "dropOwnership", "DropOwnership",
                              "orphan", "Orphan", "releaseChild", "ReleaseChild"):
                if hasattr(obj, meth_name):
                    try:
                        m = getattr(obj, meth_name)
                        # Call with plausible no-arg or None; UNO will raise if wrong.
                        try:
                            m(None)
                        except TypeError:
                            try:
                                m()
                            except Exception as call_e:
                                attempted.append(f"{obj_name}.{meth_name}() -> {type(call_e).__name__}")
                                continue
                        attempted.append(f"{obj_name}.{meth_name}(None) -> no exception")
                    except Exception as e:
                        attempted.append(f"{obj_name}.{meth_name} access/call -> {type(e).__name__}: {e}")
        if attempted:
            log.info("[RICH-SHUTDOWN]   drop-surface attempts (pure Python): %s", attempted)
        else:
            log.info("[RICH-SHUTDOWN]   no plausible drop/orphan mutator methods found via hasattr on peers/windows")

        # Also note the implementation names for future LO core correlation.
        for obj, nm in ((parent_peer, "parent"), (container_peer, "container")):
            if obj:
                try:
                    impl = obj.getImplementationName()
                    log.info("[RICH-SHUTDOWN]   %s_peer implName=%s", nm, impl)
                except Exception:
                    pass

        log.info("[RICH-SHUTDOWN] _instrument..._and_defang COMPLETE (still_child=%s)", still_child)

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
        """Early defang + instrumentation when the rich-text sidebar panel is hidden.

        The user prefers this hook. It fires on normal sidebar tab switches
        (and other hide events), giving us an earlier chance to hide/zero-size
        the embedded container window and log whether it is still a registered
        VCL child of the sidebar dialog.

        We deliberately do *not* set self._disposed, do not remove listeners,
        and do not call the full disposal path here. That would re-introduce
        the old bug where showing the save-on-close dialog would hide the
        panel, destroying the editor, and then the user canceling would leave
        a dead editor when the sidebar re-appeared.

        Full coordinated teardown (with the peer-alive guard) still happens
        via the document model / terminate / safety-net paths.
        """
        if self._disposed:
            return

        log.info("[RICH-SHUTDOWN] on_window_hidden — early defang + VCL child check (preferred hook)")

        try:
            # Safe, idempotent defang (same spirit as the big instrumentation helper)
            if self.container_window:
                try:
                    self.container_window.setVisible(False)
                    log.info("[RICH-SHUTDOWN]   on_window_hidden defang: setVisible(False) on container")
                except Exception as e:
                    log.debug("[RICH-SHUTDOWN]   on_window_hidden defang setVisible failed (non-fatal): %s", e)
                try:
                    self.container_window.setPosSize(0, 0, 0, 0, 15)
                    log.info("[RICH-SHUTDOWN]   on_window_hidden defang: setPosSize(0,0,0,0) on container")
                except Exception as e:
                    log.debug("[RICH-SHUTDOWN]   on_window_hidden defang setPosSize failed (non-fatal): %s", e)

            # Read-only VCL child relationship check (using the same getWindows() walk
            # as the scroll debug helpers and the big instrumentation method).
            # This gives us visibility at hide time (tab switch) instead of only at final close.
            parent_peer = None
            container_peer = None
            try:
                if self.parent_window and hasattr(self.parent_window, "getPeer"):
                    parent_peer = self.parent_window.getPeer()
                if self.container_window and hasattr(self.container_window, "getPeer"):
                    container_peer = self.container_window.getPeer()
            except Exception:
                pass

            if parent_peer and container_peer and hasattr(parent_peer, "getWindows"):
                try:
                    def _search(w, target, depth=0):
                        if depth > 20:
                            return False
                        if w is target:
                            return True
                        try:
                            for c in (w.getWindows() or []):
                                if _search(c, target, depth + 1):
                                    return True
                        except Exception:
                            pass
                        return False

                    still_child = _search(parent_peer, container_peer)
                    log.info("[RICH-SHUTDOWN]   on_window_hidden VCL child check via getWindows(): still registered under parent_peer? %s", still_child)
                except Exception as e:
                    log.debug("[RICH-SHUTDOWN]   on_window_hidden child check failed: %s", e)
        except Exception as e:
            log.info("[RICH-SHUTDOWN] on_window_hidden early defang/check raised (swallowed): %s", e)

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

def get_theme_colors(doc=None, style_window=None):
    """Retrieve theme-aware colors based on StyleSettings from *style_window* or *doc*'s frame.

    Returns (bg_color, user_color, assistant_color).
    """
    win = style_window
    if win is None and doc is not None:
        try:
            controller = doc.getCurrentController()
            if controller:
                frame = controller.getFrame()
                if frame:
                    win = frame.getContainerWindow()
        except Exception as e:
            log.debug("get_theme_colors: doc frame lookup failed: %s", e)
    try:
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


def scroll_to_bottom(doc, aggressive: bool = False):
    """Scroll the embedded document view to the bottom (Online/Browse layout).

    The "aggressive" path uses a screenDown loop with repeated
    processEventsToIdle calls to let the layout engine settle between
    rounds. This is the only mechanism that reliably moves the viewport
    in embedded Browse-mode frames where MakeVisible/cursor-follow is
    broken. Only enabled on actual text insertion paths
    (append_text_chunk / append_rich_text when auto_scroll=True).

    All other callers (resize listener, deferred rerender timer, etc.)
    use the lightweight path to avoid re-entrancy / infinite loops.
    """
    if not doc:
        return
    try:
        controller = doc.getCurrentController()
        if not controller:
            return

        # 1. Core lightweight work that every caller gets (cursor)
        view_cursor = controller.getViewCursor()
        if view_cursor:
            try:
                view_cursor.gotoEnd(False)
            except Exception as e:
                log.debug("view_cursor.gotoEnd failed: %s", e)

        # 2. Aggressive path: screenDown loop with layout settling.
        #    All other callers (on_window_resized, deferred timers, debug force-scroll, etc.)
        #    take the lightweight path below to prevent infinite re-entrancy loops.
        if aggressive:
            # screenDown loop with layout settling: HTML imports trigger
            # multi-stage layout recalculation — a single processEventsToIdle
            # flushes one wave but those events schedule more layout work.
            # We iterate: flush events, then screenDown until exhausted,
            # repeating up to 3 times so the layout can fully settle.
            try:
                from plugin.framework.uno_context import get_toolkit as _get_layout_tk
                _layout_tk = _get_layout_tk()
                vc = controller.getViewCursor()
                if vc and hasattr(vc, "screenDown") and _layout_tk and hasattr(_layout_tk, "processEventsToIdle"):
                    total_pages = 0
                    for _attempt in range(3):
                        _layout_tk.processEventsToIdle()
                        pages = 0
                        while pages < 50 and vc.screenDown():
                            pages += 1
                        total_pages += pages
                    log.info("scroll_to_bottom[aggressive]: screenDown paged %d screens", total_pages)
            except Exception as e:
                log.debug("scroll_to_bottom[aggressive]: screenDown loop failed: %s", e)
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


def append_rich_text(doc, text, role="assistant", auto_scroll=True, style_window=None):
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

        bg_color, user_color, assistant_color = get_theme_colors(doc, style_window=style_window)

        if text and text.strip():
            text = strip_legacy_ai_label(text) if role == "assistant" else text

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
        cursor.CharWeight = 100.0  # Reset to normal after bold prefix
        pre_len = doc.CharacterCount

        if text and text.strip():
            looks_html = bool(_HTML_TAG_RE.search(text))
            log.debug("append_rich_text: looks_html=%s len=%d snippet=%r", looks_html, len(text), text[:120])

            used_html_import = False
            if looks_html:
                try:
                    _insert_html_at_cursor(doc, cursor, text)
                    used_html_import = True
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
            # Plain text (and HTML-import fallback) get the role tint; successful HTML import
            # keeps per-span CharColor from the filter (red/blue runs, etc.).
            if not used_html_import:
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


def finalize_sidebar_assistant_response(listener) -> None:
    """Re-import the last assistant message as HTML when rich sidebar is active."""
    listener.rerender_rich_text_session()
