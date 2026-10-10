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
# Chat with Document - Sidebar Panel implementation
# Follows the working pattern from LibreOffice's Python ToolPanel example:
# XUIElement wrapper creates panel in getRealInterface() via ContainerWindowProvider + XDL.
# This module owns UNO/XDL wiring only. Chat document context is built on ChatSession
# (mode switch) and send_handlers / tool_loop (each send) — not here.

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakSet
import hashlib
import uuid
import uno
import unohelper

from com.sun.star.lang import IllegalArgumentException
from com.sun.star.container import NoSuchElementException

# Ensure the extension's install directory is on sys.path so that normal
# "import plugin.xxx" statements work when LibreOffice loads this module.
# See plugin/framework/uno_bootstrap.py for the centralized implementation
# and rationale (this used to be duplicated fragile path logic).

# Minimal stdlib-only bootstrap (must run before the "from plugin..." import below)
# because unopkg writeRegistryInfo loads this file before the OXT root is on sys.path.
_this = os.path.abspath(__file__)
for __ in range(3):  # plugin/chatbot/panel_factory.py → plugin/chatbot/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=3, also_add_contrib=True)

# Recording shipped unless built with --no-recording (see scripts/build_oxt.py).
try:
    from plugin.chatbot.audio_recorder import AudioRecorder  # noqa: F401  # pyright: ignore[reportUnusedImport]

    HAS_RECORDING = True
except ImportError:
    HAS_RECORDING = False

from plugin.framework.logging import start_watchdog_thread, init_logging
from plugin.chatbot.dialogs import get_optional as get_optional_control, set_control_text, set_control_enabled
from plugin.framework.uno_context import get_extension_url, get_extension_path
from plugin.chatbot.panel_wiring import _wireControls as wire_chatpanel_controls

# debug-only: omitted in release (thread_guard stub has no _designated_main_thread).
_LIVE_CHAT_PANELS: WeakSet[Any] | None = None


def _debug_live_panels_on() -> bool:
    """True in dev thread-guard builds and mock-sidebar (``WRITERAGENT_TESTING=1``).

    ``make test-mock-sidebar`` sets ``WRITERAGENT_UNO_THREAD_GUARD=0``, so the
    thread_guard stub has no ``_designated_main_thread``. Still track the live
    panel so URP slash/Packet G hooks can find ``SendButtonListener``.
    """
    try:
        from plugin.framework import thread_guard as tg

        if hasattr(tg, "_designated_main_thread"):
            return True
    except Exception:
        pass
    return os.environ.get("WRITERAGENT_TESTING") == "1"


def _live_chat_panels() -> WeakSet[Any] | None:
    global _LIVE_CHAT_PANELS
    if not _debug_live_panels_on():
        return None
    if _LIVE_CHAT_PANELS is None:
        _LIVE_CHAT_PANELS = WeakSet()
    return _LIVE_CHAT_PANELS


def register_debug_live_panel(element: Any) -> None:
    """debug-only: omitted in release. Track a wired ChatPanelElement for mock-LLM tests."""
    panels = _live_chat_panels()
    if panels is not None and element is not None:
        panels.add(element)


def _bind_close_hook(session: Any, panel: Any, send_listener: Any, query_control: Any) -> None:
    """Closing the document window tears down this sidebar's turn.

    FrameSession.dispose alone only removed listeners: the stream kept
    painting into the dead window, the reply was saved to history, and the
    LLM lane stayed held so other documents could not send.
    """

    def on_frame_close() -> None:
        if getattr(session, "panel", None) is panel:
            from plugin.chatbot.tool_loop_actions import current_turn

            turn = current_turn(send_listener)
            if turn is not None:
                turn.closed_by_document = True
            release_live_sidebar(panel, query_control)

    # Kept on the panel so FrameSession.release_panel can drop it (no leaked
    # closures holding old panels across sidebar rebuilds).
    old_hook = getattr(panel, "_frame_close_hook", None)
    if old_hook is not None and hasattr(session, "remove_close_hook"):
        session.remove_close_hook(old_hook)
    panel._frame_close_hook = on_frame_close
    if hasattr(session, "add_close_hook"):
        session.add_close_hook(on_frame_close)


def unregister_debug_live_panel(element: Any) -> None:
    """debug-only: omitted in release."""
    panels = _live_chat_panels()
    if panels is not None and element is not None:
        panels.discard(element)


def release_live_sidebar(panel: Any, query_control: Any = None) -> None:
    """Drop this sidebar from the uid map and the focus pin, then cancel send.

    The uid stored at register time is the slot this panel owns. Looking
    the document up on the frame again and popping that uid unconditionally
    drops the other window of the same model. Clearing the focus pin also
    drops it when another sidebar owns it.
    """
    if getattr(panel, "_released", False):
        return
    panel._released = True
    unregister_debug_live_panel(panel)
    try:
        from plugin.doc.live_panels import unregister_live_panel

        uid = getattr(panel, "_live_panel_uid", "") or ""
        if uid:
            unregister_live_panel(uid, panel)
    except Exception as exc:
        log.debug("live panel unregister on dispose: %s", exc)
    try:
        listener = getattr(panel, "send_listener", None)
        if listener:
            listener.disposing(None)
    except Exception as exc:
        log.info("send_listener.disposing raised from sidebar release: %s", exc)
    with suppress_disposed("frame session release on dispose", logger=log):
        session = getattr(panel, "frame_session", None)
        if session is not None:
            session.release_panel(panel, query_control)


def iter_debug_live_chat_panels() -> list[Any]:
    """debug-only: omitted in release."""
    panels = _live_chat_panels()
    if panels is None:
        return []
    return list(panels)

if TYPE_CHECKING:
    from collections.abc import Callable
    from com.sun.star.uno import XInterface
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel, XSidebarPanel

try:
    from com.sun.star.ui.UIElementType import TOOLPANEL  # type: ignore
except ImportError:
    TOOLPANEL = 3  # Fallback

from plugin.framework.sidebar_column import sidebar_column_width
from plugin.framework.uno_listeners import BaseItemListener, BaseTextListener
from plugin.framework.config import get_config, get_current_endpoint
from plugin.framework.client.model_fetcher import get_text_model, get_image_model, set_image_model, set_text_model
from plugin.framework.i18n import _
from plugin.framework.errors import UnoObjectError, suppress_disposed
from plugin.framework.prompts import get_chat_system_prompt_for_document, get_greeting_for_document
from plugin.doc.doc_type import get_document_type, DocumentType
from plugin.doc.udprops import get_document_property, set_document_property

# Explicit name: LibreOffice loads this file as a UNO component, so __name__
# is not under plugin.* and records would miss the debug log handler.
log = logging.getLogger("plugin.chatbot.panel_factory")

# XDL path inside the .oxt
XDL_PATH = "Dialogs/ChatPanelDialog.xdl"
_PRE_NEGOTIATION_PANEL_WIDTH = 320

# Default system prompt for the chat sidebar (imported from main inside methods to avoid unopkg errors)
DEFAULT_SYSTEM_PROMPT_FALLBACK = "You are a helpful assistant."


def _get_arg(args: Any, name: str) -> Any:
    """Extract PropertyValue from args by Name."""
    for pv in args:
        if hasattr(pv, "Name") and pv.Name == name:
            return pv.Value
    return None


def _run_on_main_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run *fn* on the VCL thread.

    URP dispatch of WriterAgentDeck calls ``ChatPanelElement.getRealInterface``
    off the VCL thread (Dummy-N). ``get_extension_url`` (PackageInformationProvider)
    is ``@main_thread_only``; without this hop, thread_guard aborts ChatPanel
    create. QA used to set ``WRITERAGENT_UNO_THREAD_GUARD=0``; this marshal is
    the product fix so the panel opens with the guard on.
    """
    from plugin.framework.queue_executor import execute_on_main_thread
    from plugin.framework.thread_guard import on_main_thread

    if on_main_thread():
        return fn(*args, **kwargs)
    return execute_on_main_thread(fn, *args, **kwargs)


_paths_initialized = False


def _initialize_extension_paths(ctx: Any) -> None:
    """Initialize extension paths once per session."""
    global _paths_initialized
    if _paths_initialized:
        return

    def _impl() -> None:
        global _paths_initialized
        if _paths_initialized:
            return
        try:
            ext_path = get_extension_path(ctx)
            if not ext_path:
                log.warning("_initialize_extension_paths: get_extension_path returned falsy value, skipping path setup")
                return

            if ext_path not in sys.path:
                sys.path.insert(0, ext_path)

            contrib_dir = os.path.join(ext_path, "contrib")
            if contrib_dir not in sys.path:
                sys.path.insert(0, contrib_dir)

            init_logging(ctx)
            log.info("Initialized extension paths for session: %s" % ext_path)
            try:
                from plugin.writer.locale.ai_grammar_proofreader import ensure_writeragent_proofreader_configured

                ensure_writeragent_proofreader_configured(ctx)
            except Exception as e:
                log.warning("[grammar] sidebar init: could not load or run grammar proofreader bootstrap: %s", e, exc_info=True)
            # Only after success: a falsy path or an exception retries on the next sidebar.
            _paths_initialized = True
        except Exception:
            init_logging(ctx)
            log.exception("_initialize_extension_paths failed")

    # Hop the body, not this function: WRITERAGENT_TESTING=1 inlines
    # execute_on_main_thread on Dummy-N, and a self-call would recurse.
    # get_extension_path → get_extension_url (PIP) is @main_thread_only.
    _run_on_main_thread(_impl)


# ---------------------------------------------------------------------------
# ChatToolPanel, ChatPanelElement, ChatPanelFactory (sidebar plumbing)
# ---------------------------------------------------------------------------


class ChatToolPanel(unohelper.Base, XToolPanel, XSidebarPanel):
    """Holds the panel window; implements XToolPanel and XSidebarPanel."""

    ctx: Any
    PanelWindow: Any
    Window: Any
    parent_window: Any
    resize_listener: Any
    _in_hfw: bool

    def __init__(self, panel_window: Any, parent_window: Any, ctx: Any) -> None:
        self.ctx = ctx
        self.PanelWindow = panel_window
        self.Window = panel_window
        self.parent_window = parent_window
        # Set by panel wiring after _PanelResizeListener is created.
        self.resize_listener = None

    def getWindow(self) -> Any:
        return self.Window

    def createAccessible(self, ParentAccessible: Any) -> Any:
        return self.PanelWindow

    def getHeightForWidth(self, nWidth: int) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Return LayoutSize and fill the deck viewport.

        nWidth is rContentBox.GetWidth(). The GTK ChildFrame size-request can
        stick (Keith: parent 992 while deck_hint 806). Sync width-only.
        """
        width = nWidth
        if not self.parent_window or not self.PanelWindow or width <= 0:
            return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)
        if getattr(self, "_in_hfw", False):
            return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)
        self._in_hfw = True
        try:
            return self._hfw_body(width)
        finally:
            self._in_hfw = False

    def _hfw_body(self, width: int) -> Any:
        parent_rect = self.parent_window.getPosSize()
        parent_w = parent_rect.Width
        parent_h = parent_rect.Height
        deck_w = width

        # Read current actual size *before* we decide.
        before = None
        current_h = 0
        with suppress_disposed("getHeightForWidth getPosSize", logger=log):
            before = self.PanelWindow.getPosSize()
            current_h = before.Height if before else 0

        # Width is negotiated here; height stays whatever LO/deck already allocated.
        if current_h <= 0:
            current_h = parent_h if parent_h > 0 else 400

        # Fill the content box. min(nWidth, parent); 180 AppFont is a leak.
        min_w = self.getMinimalWidth()
        eff_w = sidebar_column_width(deck_w, parent_w, min_w=min_w)

        log.debug("getHeightForWidth deck_hint=%s parent=%sx%s current_root=%s eff_W=%s" % (deck_w, parent_w, parent_h, "%sx%s" % (before.Width, before.Height) if before else None, eff_w))
        rl = getattr(self, "resize_listener", None)
        if rl is not None and hasattr(rl, "note_width_negotiated"):
            with suppress_disposed("getHeightForWidth note_width_negotiated", logger=log):
                rl.note_width_negotiated(eff_w)
        with suppress_disposed("getHeightForWidth setPosSize", logger=log):
            # Size the AWT dialog only, like last month. ChildFrame setPosSize is
            # gtk_widget_set_size_request (a minimum); typing grew past it and we
            # filled the new width (Keith: 995 → 1019).
            self.PanelWindow.setPosSize(0, 0, eff_w, current_h, 15)
            after = self.PanelWindow.getPosSize()
            parent_after = self.parent_window.getPosSize()
            log.info(
                "getHeightForWidth root_after=%sx%s parent_after=%sx%s",
                after.Width,
                after.Height,
                parent_after.Width,
                parent_after.Height,
            )

        if rl is not None:
            with suppress_disposed("getHeightForWidth relayout_now", logger=log):
                from plugin.chatbot.rich_text_control import log_rich_scroll

                rich = rl._c.get("response_rich") if hasattr(rl, "_c") else None
                log_rich_scroll("getHeightForWidth_before", control=rich, eff_w=eff_w)
                rl.relayout_now(self.PanelWindow)
                log_rich_scroll("getHeightForWidth_after", control=rich, eff_w=eff_w)

        return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)

    def getMinimalWidth(self) -> int:
        # XDL dlg:width=180 is AppFont, ~300px on this machine (Clear right=304).
        return 320


def _is_sha256_hex(value: Any) -> bool:
    """True for a lowercase SHA-256 hex digest (url-derived chat session id)."""
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _fork_doc_chat_history(old_session_id: str, new_session_id: str) -> None:
    """Copy document-chat rows onto *new_session_id*.

    Snapshot the source first and replace the destination in one write.
    ``clear()`` then ``get_messages()`` wipes the only store when the
    regenerated id is the stored id, and a JSON ``clear`` that swallows
    ``OSError`` then appends onto the rows it failed to remove. Save As
    re-enters setup after a partial property write, or ``os.remove`` fails
    and the copy still runs. The caller skips this when the two ids are
    equal.
    """
    from plugin.chatbot.history_db import get_chat_history

    # If the destination already has a conversation, leave it intact.
    # We do not overwrite the destination's chat when "Save As" targets
    # an existing file that already has its own conversation. A seeded
    # system prompt alone (Clear writes one) is not a conversation.
    dest_history = get_chat_history(new_session_id)
    if any(msg.get("role") != "system" for msg in dest_history.get_messages()):
        return

    messages = list(get_chat_history(old_session_id).get_messages())
    dest_history.replace_messages(messages)


def header_third_button_kind(model: Any) -> str:
    """Shared btn_latex slot: ``python_cell``, ``latex``, or ``""`` (hide).

    Draw and Impress used to get LatexButtonListener because the slot only
    special-cased Calc. The hamburger already shows Insert LaTeX for Writer
    only, so other documents hide the button instead of opening that dialog.
    """
    from plugin.doc.doc_type import is_calc, is_writer

    if is_calc(model):
        return "python_cell"
    if is_writer(model):
        return "latex"
    return ""


class ChatPanelElement(unohelper.Base, XUIElement):
    """XUIElement wrapper; creates panel window in getRealInterface() via ContainerWindowProvider."""

    ctx: Any
    xFrame: Any
    xParentWindow: Any
    ResourceURL: Any
    Frame: Any
    Type: Any
    toolpanel: ChatToolPanel | None
    m_panelRootWindow: Any
    rich_text_widget: Any
    _in_refresh_controls: bool
    _released: bool
    _current_mode: str
    doc_session: Any
    web_session: Any
    librarian_session: Any
    send_listener: Any
    _live_panel_uid: str
    frame_session: Any
    _frame_close_hook: Callable[[], None] | None

    def __init__(self, ctx: Any, frame: Any, parent_window: Any, resource_url: str, frame_session: Any = None) -> None:
        self.ctx = ctx
        self.xFrame = frame
        self.xParentWindow = parent_window
        self.ResourceURL = resource_url
        self.Frame = frame
        self.Type = TOOLPANEL
        self.toolpanel = None
        self.m_panelRootWindow = None
        self._frame_close_hook = None
        self.session: Any = None  # Created in _wireControls
        self._released = False
        self._current_mode = ""
        self._in_refresh_controls = False
        # Document id from the frame session, not from whichever component is current.
        self.frame_session = frame_session
        self._live_panel_uid = ""
        if frame_session is not None:
            frame_session.bind_panel(self)
            self._live_panel_uid = str(getattr(frame_session, "doc_uid", "") or "")
        self.rich_text_widget = None
        log.debug("[RICH-LIFECYCLE] ChatPanelElement.__init__ resource_url=%s parent_window=%s",
                  resource_url, id(parent_window) if parent_window else None)

    def _on_config_changed(self, **kwargs: Any) -> None:
        """Event bus listener for config changes."""
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework.queue_executor import post_to_main_thread

        if getattr(self, "_released", False):
            return
        if not on_main_thread():
            post_to_main_thread(self._refresh_controls_from_config)
            return
        self._refresh_controls_from_config()

    def getRealInterface(self) -> XInterface:  # pyright: ignore[reportIncompatibleMethodOverride]
        log.debug("[RICH-LIFECYCLE] ChatPanelElement.getRealInterface called (toolpanel already exists=%s)", bool(self.toolpanel))
        if not self.toolpanel:
            try:
                # Dummy-N URP getRealInterface: hop path init + window/wiring
                # (get_extension_url and later @main_thread_only getters) to VCL.
                def _create_panel() -> None:
                    # Reset _released so the rebuilt panel's lifecycle is
                    # tracked. A failed getRealInterface calls
                    # release_live_sidebar, which sets _released = True. A
                    # retry rebuilds the panel, and disposal then returns
                    # early on that flag, skipping cleanup and leaking
                    # listeners.
                    self._released = False
                    # Ensure extension on path early so _wireControls imports work
                    _initialize_extension_paths(self.ctx)
                    root_window = self._getOrCreatePanelRootWindow()
                    log.info("[RICH-LIFECYCLE] root_window created: %s", bool(root_window))
                    self.toolpanel = ChatToolPanel(root_window, self.xParentWindow, self.ctx)
                    wire_chatpanel_controls(self, root_window, HAS_RECORDING, _initialize_extension_paths)
                    log.info("[RICH-LIFECYCLE] getRealInterface completed successfully (rich_text wiring done)")

                _run_on_main_thread(_create_panel)
            except Exception as e:
                # Assign toolpanel only after wiring. Assigning it first
                # leaves a half-built panel latched when a later getControl
                # fails, so the next getRealInterface returns it and never
                # retries.
                log.exception("getRealInterface failed [resource_url=%s]", self.ResourceURL)
                with suppress_disposed("frame session release on getRealInterface fail", logger=log):
                    release_live_sidebar(self)
                    root = getattr(self, "m_panelRootWindow", None)
                    rl = getattr(self.toolpanel, "resize_listener", None) if self.toolpanel else None
                    if rl and root and hasattr(root, "removeWindowListener"):
                        root.removeWindowListener(rl)
                self.toolpanel = None
                raise UnoObjectError("Failed to create ChatPanel UI element", details={"resource": self.ResourceURL}) from e
        # Panel is a Python UNO component; stubs do not overlap XInterface.
        return cast("XInterface", cast("object", self.toolpanel))

    def _getOrCreatePanelRootWindow(self) -> Any:
        log.debug("[RICH-LIFECYCLE] _getOrCreatePanelRootWindow entered (xParentWindow=%s)",
                  id(self.xParentWindow) if self.xParentWindow else None)
        base_url = get_extension_url(self.ctx)
        dialog_url = base_url + "/" + XDL_PATH
        # INFO so missing-XDL failures are visible at default WARN when we escalate below.
        log.info("[RICH-LIFECYCLE] dialog_url=%s", dialog_url)
        # The extension context, not a fresh bootstrap context. get_ctx() can
        # be a different component context and the dialog URL lookup misses.
        ctx = self.ctx
        provider = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.ContainerWindowProvider", ctx)
        log.info("[RICH-LIFECYCLE] calling createContainerWindow for chat sidebar...")
        self.m_panelRootWindow = provider.createContainerWindow(dialog_url, "", self.xParentWindow, None)
        log.info("[RICH-LIFECYCLE] createContainerWindow returned root_window=%s", bool(self.m_panelRootWindow))
        if not self.m_panelRootWindow:
            # Empty white sidebar: ContainerWindowProvider returns null when the XDL
            # URL cannot be loaded (e.g. Dialogs/ wiped by Windows dialogs/ case collision).
            xdl_fs_path = ""
            xdl_exists = False
            try:
                ext_path = get_extension_path(self.ctx)
                if ext_path:
                    xdl_fs_path = os.path.join(ext_path, *XDL_PATH.split("/"))
                    xdl_exists = os.path.isfile(xdl_fs_path)
            except Exception as e:
                log.debug("[RICH-LIFECYCLE] could not resolve XDL filesystem path: %s", e)
            log.error(
                "[RICH-LIFECYCLE] createContainerWindow returned no window url=%s xdl_path=%s exists=%s",
                dialog_url,
                xdl_fs_path or "(unknown)",
                xdl_exists,
            )
            raise UnoObjectError(
                "ChatPanel createContainerWindow returned no window",
                details={"dialog_url": dialog_url, "xdl_path": xdl_fs_path, "xdl_exists": xdl_exists},
            )
        # Sidebar does not show the panel content without this (framework does not make it visible).
        if hasattr(self.m_panelRootWindow, "setVisible"):
            with suppress_disposed("set panel root window visible", logger=log):
                self.m_panelRootWindow.setVisible(True)
        # Bug fix: on restored-wide startup, createContainerWindow can leave the root
        # at a stale frame-sized width before DeckLayouter calls getHeightForWidth.
        # Briefly cap that pre-negotiation size so sfx2 does not seed an H-scroll
        # range from the temporary root; getHeightForWidth expands to deck width.
        with suppress_disposed("constrain panel window", logger=log):
            parent_rect = self.xParentWindow.getPosSize()
            current_rect = self.m_panelRootWindow.getPosSize()
            # Cap to 320, not parent. sidebar_column_width(0, 1115) would fill
            # the HiDPI ChildFrame request and seed the default H-bar.
            target_w = _PRE_NEGOTIATION_PANEL_WIDTH
            target_h = current_rect.Height if current_rect.Height > 0 else (
                parent_rect.Height if parent_rect.Height > 0 else 400
            )
            if target_w > 0 and target_h > 0:
                self.m_panelRootWindow.setPosSize(0, 0, target_w, target_h, 15)
                log.debug("panel pre-negotiation constrained to W=%s H=%s" % (target_w, target_h))
        return self.m_panelRootWindow

    def disposing(self, Source: Any = None) -> None:
        """Best-effort lifecycle hook for sidebar resources (and future use).

        The LO sidebar framework does not automatically call this on XUIElement
        teardown for tool panels, but having it (and calling the SendButtonListener
        path) documents the intent and provides an explicit cleanup entry point.
        """
        log.info("[RICH-LIFECYCLE] ChatPanelElement.disposing called Source=%s has_send_listener=%s",
                 id(Source) if Source else None,
                 hasattr(self, "send_listener") and bool(self.send_listener))
        listener = getattr(self, "send_listener", None)
        query = getattr(listener, "query_control", None) if listener is not None else None
        release_live_sidebar(self, query)

        from plugin.framework.event_bus import global_event_bus

        global_event_bus.unsubscribe("config:changed", self._on_config_changed)

        # Clean up the always-present resize listener.
        # This listener is attached unconditionally in panel_wiring. Failing to
        # remove it during late VCL/sidebar teardown can contribute to crashes.
        with suppress_disposed("removeWindowListener on dispose", logger=log):
            tp = getattr(self, "toolpanel", None)
            rl = getattr(tp, "resize_listener", None) if tp else None
            root = getattr(self, "m_panelRootWindow", None)
            if rl and root and hasattr(root, "removeWindowListener"):
                root.removeWindowListener(rl)
            if tp:
                tp.resize_listener = None

        self.rich_text_widget = None

    def _render_session_history(self, session: Any, response_ctrl: Any, model: Any, greeting: str = "") -> None:
        """Update the response control with the contents of the given session."""
        try:
            if self.rich_text_widget:
                self.rich_text_widget.render_session_history(session, greeting)
                return

            if response_ctrl and response_ctrl.getModel():
                from plugin.chatbot.rich_text_paste import plain_transcript_text

                text = plain_transcript_text(session, greeting)

                set_control_text(response_ctrl, text)
                # Scroll to bottom
                if hasattr(response_ctrl, "setSelection"):
                    length = len(text)
                    response_ctrl.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", length, length))
        except Exception:
            # Pass greeting. Logger.exception interpolates only when args
            # are supplied, so a format that names greeting and passes
            # nothing keeps a literal %s and the failed history render does
            # not record which greeting was in use.
            log.exception("_render_session_history failed [greeting=%s]", greeting)

    def _refresh_controls_from_config(self) -> None:
        """Reload sidebar controls from config (e.g. after user changes Settings).

        Refreshes model/prompt/image/mode selectors, Voice (TTS) checkbox, and backend indicator.
        Does not re-run ``translate_dialog`` — sidebar strings are translated once at wire/load.

        Bugfix / Re-entrancy Guard:
        Populating combobox controls below (via populate_combobox_with_lru -> ctrl.setText,
        removeItems, addItems) synchronously fires UNO listeners (ModelSyncListener,
        ModelTextSyncListener, ImageModelSyncListener). Without ``_in_refresh_controls``,
        those listeners treat programmatic UI updates as user edits, calling
        sync_sidebar_text_model -> update_lru_history -> set_config -> event_bus
        emit('config_changed') -> _refresh_controls_from_config in an infinite synchronous
        recursion loop on the main UI thread that freezes LibreOffice.
        """
        # No-op once released. A config refresh queued by _on_config_changed
        # can run after teardown and touch disposed controls.
        if getattr(self, "_released", False):
            return
        if getattr(self, "_in_refresh_controls", False):
            return
        self._in_refresh_controls = True
        try:
            root = self.m_panelRootWindow
            if not root or not hasattr(root, "getControl"):
                return
            from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru, populate_image_model_selector

            def get_optional(name: str) -> Any:
                return get_optional_control(root, name)

            model_selector = get_optional("model_selector")
            prompt_selector = get_optional("prompt_selector")
            image_model_selector = get_optional("image_model_selector")

            current_model = get_text_model()
            extra_instructions = get_config("additional_instructions")

            current_endpoint = get_current_endpoint()

            # LRU plus provider defaults only, no catalog HTTP on this
            # thread. populate_combobox_with_lru fetches /v1/models unless
            # skip_remote_fetch is set. A dead endpoint then hangs the
            # editor on every settings apply, and failures are not memoized.
            # Match Settings/eval.
            if model_selector:
                set_val = populate_combobox_with_lru(
                    self.ctx, model_selector, current_model, "model_lru", current_endpoint,
                    skip_remote_fetch=True,
                )
                if set_val != current_model:
                    set_text_model(set_val, update_lru=False)
            if prompt_selector:
                populate_combobox_with_lru(self.ctx, prompt_selector, extra_instructions, "prompt_lru", "")

            # Refresh visual (image) model via shared helper; persist correction if strict replaced value
            if image_model_selector:
                current_image = get_image_model()
                set_image_val = populate_image_model_selector(
                    self.ctx, image_model_selector, skip_remote_fetch=True,
                )
                if set_image_val != current_image:
                    set_image_model(set_image_val, update_lru=False)
            chat_mode_selector = get_optional("chat_mode_selector")
            if chat_mode_selector:
                with suppress_disposed("refresh chat_mode_selector from config", logger=log):
                    from plugin.chatbot.chat_sidebar_mode import populate_mode_selector_with_flags, sidebar_mode_flags_for_doc_type

                    model = self._get_document_model()
                    cached = getattr(getattr(self, "send_listener", None), "cached_doc_type", None)
                    from plugin.doc.doc_type import doc_type_label_for_enum, get_document_type

                    dt = cached or doc_type_label_for_enum(get_document_type(model))
                    from plugin.chatbot.chat_sidebar_mode import (
                        mode_from_selector_with_flags,
                        set_selector_mode_with_flags,
                    )

                    flags = sidebar_mode_flags_for_doc_type(dt)
                    # removeItems/addItems resets the combo to index 0 (Chat) and
                    # fires ChatModeListener. Capturing first, then restoring after
                    # populate, keeps Librarian/Web selected. The listener also
                    # bails out while _in_refresh_controls is set; both are required
                    # because a guarded listener still leaves the combo on Chat.
                    prior_mode = mode_from_selector_with_flags(chat_mode_selector, flags)
                    populate_mode_selector_with_flags(chat_mode_selector, flags)
                    set_selector_mode_with_flags(chat_mode_selector, prior_mode, flags)
            # Keep sidebar Voice checkbox in sync with Settings → Speech (audio.tts_enabled).
            # Settings apply emits config:changed; without this, chk_voice stays at wire-time state.
            chk_voice = get_optional("chk_voice")
            if chk_voice is not None and hasattr(chk_voice, "setState"):
                with suppress_disposed("refresh chk_voice from audio.tts_enabled", logger=log):
                    from plugin.framework.config import get_config_bool_safe

                    want = 1 if get_config_bool_safe("audio.tts_enabled") else 0
                    cur = None
                    if hasattr(chk_voice, "getState"):
                        try:
                            cur = int(chk_voice.getState())
                        except Exception:
                            cur = None
                    if cur != want:
                        chk_voice.setState(want)

            try:
                # Backend indicator: show "Aider" / "Hermes" when external agent backend is enabled
                self._update_backend_indicator(root)
            except Exception:
                log.exception("_refresh_controls_from_config backend indicator failed")
        finally:
            self._in_refresh_controls = False

    def _update_backend_indicator(self, root_window: Any = None) -> None:
        """Set backend indicator label from config (visible when external backend enabled) and gray out controls."""
        try:
            from plugin.acp.registry import AGENT_BACKEND_REGISTRY, normalize_backend_id

            root = root_window or (getattr(self, "m_panelRootWindow", None))
            if not root or not hasattr(root, "getControl"):
                return

            backend_id = normalize_backend_id(get_config("agent_backend.backend_id"))
            is_external = bool(backend_id and backend_id != "builtin")

            ctrl = get_optional_control(root, "backend_indicator")
            if ctrl:
                if is_external:
                    entry = AGENT_BACKEND_REGISTRY.get(backend_id)
                    display_en = entry[0] if entry else backend_id.capitalize()
                    set_control_text(ctrl, _(display_en))
                    if hasattr(ctrl, "setVisible"):
                        ctrl.setVisible(True)
                else:
                    set_control_text(ctrl, "")
                    if hasattr(ctrl, "setVisible"):
                        ctrl.setVisible(False)

            # Enable/disable the LLM model selector based on the agent backend
            model_selector = get_optional_control(root, "model_selector")
            if model_selector and hasattr(model_selector, "getModel"):
                set_control_enabled(model_selector, not is_external)

            chat_mode_selector = get_optional_control(root, "chat_mode_selector")
            if chat_mode_selector and hasattr(chat_mode_selector, "getModel"):
                set_control_enabled(chat_mode_selector, not is_external)

        except Exception:
            log.exception("_update_backend_indicator failed")

    def _get_document_model(self) -> Any | None:
        """Helper to get the current document model strictly from the frame."""
        from plugin.framework.uno_context import get_document_from_frame

        return get_document_from_frame(self.xFrame)

    def _wire_model_selectors(self, model_selector: Any, image_model_selector: Any) -> None:
        """Initializes model selectors and their sync listeners."""
        from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru, populate_image_model_selector

        current_model = get_text_model()
        current_endpoint = get_current_endpoint()

        # Keep catalog refresh off the main thread. The same populate path
        # as config:changed, without skip_remote_fetch, fetches the model
        # catalog on the UI thread and an unreachable endpoint freezes
        # LibreOffice for the fetch timeout.
        if model_selector:
            set_model_val = populate_combobox_with_lru(
                self.ctx, model_selector, current_model, "model_lru", current_endpoint,
                skip_remote_fetch=True,
            )
            if set_model_val != current_model:
                set_text_model(set_model_val, update_lru=False)

        if image_model_selector:
            current_image = get_image_model()
            set_image_val = populate_image_model_selector(
                self.ctx, image_model_selector, skip_remote_fetch=True,
            )
            if set_image_val != current_image:
                set_image_model(set_image_val, update_lru=False)

        if model_selector:

            class ModelSyncListener(BaseItemListener):
                panel: Any
                ctx: Any

                def __init__(self, panel: Any, ctx: Any) -> None:
                    self.panel = panel
                    self.ctx = ctx

                def on_item_state_changed(self, rEvent: Any) -> None:
                    if getattr(self.panel, "_in_refresh_controls", False):
                        return
                    from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model

                    sync_sidebar_text_model(self.ctx, model_selector)

            class ModelTextSyncListener(BaseTextListener):
                panel: Any
                ctx: Any

                def __init__(self, panel: Any, ctx: Any) -> None:
                    self.panel = panel
                    self.ctx = ctx

                def on_text_changed(self, rEvent: Any) -> None:
                    if getattr(self.panel, "_in_refresh_controls", False):
                        return
                    from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model

                    sync_sidebar_text_model(self.ctx, model_selector)

            if hasattr(model_selector, "addItemListener"):
                model_selector.addItemListener(ModelSyncListener(self, self.ctx))
            if hasattr(model_selector, "addTextListener"):
                model_selector.addTextListener(ModelTextSyncListener(self, self.ctx))

        if image_model_selector:

            class ImageModelSyncListener(BaseItemListener):
                panel: Any
                ctx: Any

                def __init__(self, panel: Any, ctx: Any) -> None:
                    self.panel = panel
                    self.ctx = ctx

                def on_item_state_changed(self, rEvent: Any) -> None:
                    if getattr(self.panel, "_in_refresh_controls", False):
                        return
                    from plugin.chatbot.config_ui_helpers import sync_sidebar_image_model

                    sync_sidebar_image_model(image_model_selector, update_lru=True)

            class ImageModelTextSyncListener(BaseTextListener):
                panel: Any
                ctx: Any

                def __init__(self, panel: Any, ctx: Any) -> None:
                    self.panel = panel
                    self.ctx = ctx

                def on_text_changed(self, rEvent: Any) -> None:
                    if getattr(self.panel, "_in_refresh_controls", False):
                        return
                    from plugin.chatbot.config_ui_helpers import sync_sidebar_image_model

                    sync_sidebar_image_model(image_model_selector, update_lru=False)

            if hasattr(image_model_selector, "addItemListener"):
                image_model_selector.addItemListener(ImageModelSyncListener(self, self.ctx))
            if hasattr(image_model_selector, "addTextListener"):
                image_model_selector.addTextListener(ImageModelTextSyncListener(self, self.ctx))

    def _sidebar_include_brainstorming(self, model: Any, *, cached_doc_type: str | None = None) -> bool:
        if cached_doc_type is not None:
            return cached_doc_type == "writer"
        return get_document_type(model) == DocumentType.WRITER

    def _sidebar_mode_flags(self, model: Any, *, cached_doc_type: str | None = None) -> SidebarModeFlags:
        from plugin.chatbot.chat_sidebar_mode import sidebar_mode_flags_for_doc_type
        from plugin.doc.doc_type import doc_type_label_for_enum

        if cached_doc_type is not None:
            return sidebar_mode_flags_for_doc_type(cached_doc_type)
        return sidebar_mode_flags_for_doc_type(doc_type_label_for_enum(get_document_type(model)))

    def _greeting_for_sidebar_mode(self, mode: str, model: Any) -> str:
        # Literals inside _() so xgettext extracts them (_(CONST) is invisible).
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_BRAINSTORMING, CHAT_MODE_DEEP_RESEARCH, CHAT_MODE_LIBRARIAN, CHAT_MODE_PPT_MASTER, CHAT_MODE_WEB_RESEARCH, CHAT_MODE_WRITING_PLAN

        if mode == CHAT_MODE_WEB_RESEARCH:
            return _("AI: I can do web research to answer any question, or summarize a web page, without seeing or changing your document. Let's chat.")
        if mode == CHAT_MODE_DEEP_RESEARCH:
            return _("AI: Deep Research mode runs a multi-step web investigation (planning, several searches, synthesis) and can insert a formatted report into your document. It takes longer but produces more thorough results.")
        if mode == CHAT_MODE_BRAINSTORMING:
            return _("AI: Let's explore and design your idea together. I'll ask questions, suggest approaches, and help you build an approved spec in your document when you're ready.")
        if mode == CHAT_MODE_WRITING_PLAN:
            return _("AI: Let's draft your document section-by-section. I'll help you create a writing plan outline, and then implement it incrementally with your approval.")
        if mode == CHAT_MODE_PPT_MASTER:
            return _("AI: PPT-Master mode — I'll run the ppt-master workflow in your configured Python venv (scripts + export to Impress). Describe your topic or point me at a project folder.")
        if mode == CHAT_MODE_LIBRARIAN:
            return _("AI: I'm the WriterAgent Librarian — a host who can learn your name, favorite colors, and give a short tour. Pick Chat in the dropdown whenever you want to work on the document.")
        return get_greeting_for_document(model)

    def _wire_chat_mode_ui(
        self,
        aspect_ratio_selector: Any,
        base_size_input: Any,
        base_size_label: Any,
        chat_mode_selector: Any,
        model_label: Any,
        model_selector: Any,
        image_model_selector: Any,
        model: Any,
        toggle_image_ui: Callable[[bool], None] | None = None,
    ) -> tuple[str, SidebarModeFlags, Callable[[bool], None]]:
        """Initializes sidebar mode dropdown and image-related controls; returns (initial_mode, include_brainstorming, toggle_image_ui)."""
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_LIBRARIAN, is_image_mode, librarian_default_mode, mark_librarian_invoked, populate_mode_selector_with_flags, set_selector_mode_with_flags

        if aspect_ratio_selector:
            from plugin.chatbot.settings_dialog import IMAGE_ASPECT_RATIO_LABELS, aspect_label_gettext, canonical_aspect_label

            # This runs after translate_dialog. Refilling the combo with
            # the English tuple leaves Square in English when the catalog
            # has 正方形 / Cuadrado. Use the translated labels.
            aspect_ratio_selector.addItems(
                tuple(aspect_label_gettext(label) for label in IMAGE_ASPECT_RATIO_LABELS),
                0,
            )
            stored_aspect = canonical_aspect_label(str(get_config("image_default_aspect") or "Square"))
            aspect_ratio_selector.setText(aspect_label_gettext(stored_aspect))

        if base_size_input:
            from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru

            populate_combobox_with_lru(self.ctx, base_size_input, str(get_config("image_base_size")), "image_base_size_lru", "")

        def update_base_size_label(aspect_str: str) -> None:

            if not base_size_label:
                return
            # Combo text may be translated; Height/Width still key off the English label.
            from plugin.chatbot.settings_dialog import canonical_aspect_label

            canonical = canonical_aspect_label(aspect_str)
            txt = _("Size:")
            if "Landscape" in canonical:
                txt = _("Height:")
            elif "Portrait" in canonical:
                txt = _("Width:")
            if hasattr(base_size_label, "setText"):
                base_size_label.setText(txt)
            elif hasattr(base_size_label.getModel(), "Label"):
                base_size_label.getModel().Label = txt

        if aspect_ratio_selector:
            update_base_size_label(aspect_ratio_selector.getText())
            if hasattr(aspect_ratio_selector, "addItemListener"):

                class AspectListener(BaseItemListener):
                    def on_item_state_changed(self, rEvent: Any) -> None:
                        ev = rEvent
                        idx = getattr(ev, "Selected", -1)
                        if idx >= 0:
                            update_base_size_label(aspect_ratio_selector.getItem(idx))

                aspect_ratio_selector.addItemListener(AspectListener())

        if toggle_image_ui is None:
            from plugin.chatbot.panel_wiring import make_toggle_image_ui

            toggle_image_ui = make_toggle_image_ui(
                self,
                {
                    "model_label": model_label,
                    "model_selector": model_selector,
                    "image_model_selector": image_model_selector,
                    "aspect_ratio_selector": aspect_ratio_selector,
                    "base_size_input": base_size_input,
                    "base_size_label": base_size_label,
                },
            )

        mode_flags = self._sidebar_mode_flags(model)
        initial_mode = librarian_default_mode(self.ctx)
        if initial_mode == CHAT_MODE_LIBRARIAN:
            mark_librarian_invoked()

        if chat_mode_selector:
            with suppress_disposed("chat_mode_selector wire", logger=log, exc_info=True):
                populate_mode_selector_with_flags(chat_mode_selector, mode_flags)
                set_selector_mode_with_flags(chat_mode_selector, initial_mode, mode_flags)
                toggle_image_ui(is_image_mode(initial_mode))

        return initial_mode, mode_flags, toggle_image_ui

    def _apply_sidebar_mode(self, mode: str, model: Any, response_ctrl: Any, send_listener: Any, clear_listener: Any, toggle_image_ui: Any) -> str:
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_BRAINSTORMING,
            CHAT_MODE_CHAT,
            CHAT_MODE_DEEP_RESEARCH,
            CHAT_MODE_LIBRARIAN,
            CHAT_MODE_PPT_MASTER,
            CHAT_MODE_WEB_RESEARCH,
            clear_brainstorming_session,
            clear_librarian_session,
            clear_ppt_master_session,
            is_image_mode,
        )

        if send_listener is not None:
            from plugin.chatbot.tool_loop_actions import abort_turn

            # The in-flight turn keeps the session it started with. Later
            # chunks must not paint onto the transcript this switch shows.
            abort_turn(send_listener)
        if mode != CHAT_MODE_BRAINSTORMING and send_listener:
            clear_brainstorming_session(send_listener)
        if mode != CHAT_MODE_PPT_MASTER and send_listener:
            clear_ppt_master_session(send_listener)
        if mode != CHAT_MODE_LIBRARIAN and send_listener:
            # Flag only — librarian ChatSession history is global and must survive mode switches.
            clear_librarian_session(send_listener)
        self._current_mode = mode
        if mode == CHAT_MODE_LIBRARIAN:
            self.session = self.librarian_session
        elif mode in (CHAT_MODE_WEB_RESEARCH, CHAT_MODE_DEEP_RESEARCH):
            self.session = self.web_session
        else:
            self.session = self.doc_session
        if mode == CHAT_MODE_CHAT:
            # Session owns the builder; factory only asks for a fresh snapshot.
            session = getattr(self, "doc_session", None)
            if session is not None and model is not None:
                try:
                    session.refresh_document_context(model, self.ctx)
                except Exception:
                    log.debug("refresh_document_context failed", exc_info=True)
        toggle_image_ui(is_image_mode(mode))
        greeting = self._greeting_for_sidebar_mode(mode, model)
        if send_listener:
            send_listener.set_session(self.session)
        if clear_listener:
            clear_listener.set_session(self.session, greeting=greeting)
        if response_ctrl:
            self._render_session_history(self.session, response_ctrl, model, greeting)
        return greeting

    def _wire_chat_mode_listener(self, chat_mode_selector: Any, model: Any, response_ctrl: Any, send_listener: Any, clear_listener: Any, toggle_image_ui: Any, mode_flags: Any) -> Callable[[str], None]:
        from plugin.chatbot.chat_sidebar_mode import mode_from_selector_with_flags

        def apply_mode(mode: str) -> None:
            self._apply_sidebar_mode(mode, model, response_ctrl, send_listener, clear_listener, toggle_image_ui)

        # Librarian switch_to_document_mode must apply Chat even if ComboBox
        # selectItemPos does not fire the item listener (UNO is inconsistent).
        if send_listener is not None:
            send_listener._apply_sidebar_mode_fn = apply_mode

        if not chat_mode_selector or not hasattr(chat_mode_selector, "addItemListener"):
            return apply_mode

        class ChatModeListener(BaseItemListener):
            panel: Any
            ctx: Any
            selector: Any
            mode_flags: Any
            apply_target: Any
            _in_revert: bool

            def __init__(self, panel: Any, ctx: Any, selector: Any, flags: Any, apply_target: Any) -> None:
                self.panel = panel
                self.ctx = ctx
                self.selector = selector
                self.mode_flags = flags
                self.apply_target = apply_target
                self._in_revert = False

            def on_item_state_changed(self, rEvent: Any) -> None:
                if self._in_revert:
                    return
                # Settings refresh rebuilds this combo. Model/image listeners
                # already ignore that; without the same guard, removeItems
                # applies Chat and render_session_history clears the transcript.
                if getattr(self.panel, "_in_refresh_controls", False):
                    return
                # A click during processEventsToIdle used to swap host.session
                # and write [DOCUMENT CONTENT] onto the other chat. Librarian
                # handoff calls apply_mode directly while the send is busy;
                # only this combo listener is ignored.
                send_state = getattr(getattr(send_listener, "sidebar_state", None), "send", None)
                if send_state is not None and send_state.is_busy:
                    # Revert the selector to the applied mode under a
                    # re-entrancy guard. The combobox has already updated, so
                    # returning here leaves the dropdown on the new mode while
                    # self.session and the UI stay on the old one.
                    applied_mode = getattr(self.panel, "_current_mode", None)
                    if applied_mode and self.selector:
                        self._in_revert = True
                        try:
                            from plugin.chatbot.chat_sidebar_mode import set_selector_mode_with_flags

                            set_selector_mode_with_flags(self.selector, applied_mode, self.mode_flags)
                        finally:
                            self._in_revert = False
                    return
                mode = mode_from_selector_with_flags(self.selector, self.mode_flags)
                self.apply_target(mode)

        chat_mode_selector.addItemListener(ChatModeListener(self, self.ctx, chat_mode_selector, mode_flags, apply_mode))
        return apply_mode

    def _setup_sessions(self, model: Any, extra_instructions: Any) -> None:
        """Creates the document and web research chat sessions."""
        # Deferred: importing panel.py at module load breaks unopkg (writeRegistryInfo) — heavy stack.
        from plugin.chatbot.panel import ChatSession

        # Pass the panel ctx. refresh_document_context does; omitting it
        # here skips vision, peer, and memory/humanizer in the seeded prompt.
        # ChatPanelElement already holds that context.
        system_prompt = get_chat_system_prompt_for_document(model, extra_instructions or "", ctx=self.ctx)

        session_id = get_document_property(model, "WriterAgentSessionID")
        url = ""
        if model is not None and hasattr(model, "getURL"):
            try:
                raw_url = model.getURL()
            except Exception:
                raw_url = ""
            url = raw_url if isinstance(raw_url, str) else ""
        fork_failed = False
        if session_id:
            session_url = get_document_property(model, "WriterAgentSessionURL")
            url_id = hashlib.sha256(url.encode("utf-8")).hexdigest() if url else ""
            # Fork only when the id has to change. Snapshot and replace
            # the destination. When the id already names this file, update
            # the stored URL and leave the rows alone. A regenerated sha256
            # equal to the stored id that calls clear() before the copy
            # reads the rows wipes the only history. A copied file whose
            # stored id is a 64-hex digest but has no WriterAgentSessionURL
            # skips the URL-change branch, then stamps the new URL onto that
            # old id so the new file shares the old chat. A UUID from the
            # first save of an untitled document is not a url-hash and must
            # keep its history.
            fork_to = ""
            if session_url and url and session_url != url:
                fork_to = url_id
            elif (not session_url) and url_id and _is_sha256_hex(session_id) and session_id != url_id:
                fork_to = url_id
            if fork_to and fork_to != session_id:
                log.info("Document URL changed from %s to %s. Regenerating session ID for copy isolation.", session_url, url)
                old_session_id = session_id
                try:
                    _fork_doc_chat_history(str(old_session_id), fork_to)
                    _fork_doc_chat_history(str(old_session_id) + "_web", fork_to + "_web")
                    session_id = fork_to
                    if model:
                        set_document_property(model, "WriterAgentSessionID", session_id)
                        if url:
                            set_document_property(model, "WriterAgentSessionURL", url)
                except Exception:
                    # Keep the old id and do not stamp the new URL below, so the
                    # next open of this file retries the fork.
                    fork_failed = True
                    log.exception("Failed to copy chat history from %s to %s", old_session_id, fork_to)
            elif fork_to == session_id and model and url:
                set_document_property(model, "WriterAgentSessionURL", url)

        if not session_id:
            if url:
                session_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
            else:
                session_id = str(uuid.uuid4())
            if model:
                set_document_property(model, "WriterAgentSessionID", session_id)
                if url:
                    set_document_property(model, "WriterAgentSessionURL", url)
        elif not fork_failed:
            if model and url:
                session_url = get_document_property(model, "WriterAgentSessionURL")
                if not session_url:
                    set_document_property(model, "WriterAgentSessionURL", url)

        self.doc_session = ChatSession(system_prompt, session_id=session_id)
        self.web_session = ChatSession("Observe: Always use the web_search tool to answer questions.", session_id=session_id + "_web")
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_LIBRARIAN, LIBRARIAN_HISTORY_SESSION_ID

        self.librarian_session = ChatSession(
            self._greeting_for_sidebar_mode(CHAT_MODE_LIBRARIAN, model),
            session_id=LIBRARIAN_HISTORY_SESSION_ID,
        )
        self.session = self.doc_session

    def _wire_buttons(self, controls: dict[str, Any], model: Any, initial_mode: str, mode_flags: Any, toggle_image_ui: Any) -> None:
        """Wires up the Send, Stop, Clear, Settings, Python, LaTeX, Search, and chat mode selector."""
        if mode_flags is None:
            from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

            # Default flags keep Send/Stop and the mode listener wired.
            # include_brainstorming raising AttributeError inside the
            # Send/Stop try hits the broad except, skips addActionListener,
            # and leaves mode_flags as None.
            log.warning("mode_flags missing; wiring Send/Stop with default sidebar mode flags")
            mode_flags = SidebarModeFlags()
        from plugin.chatbot.panel import (
            ActionHandlerButtonListener,
            ClearButtonListener,
            HamburgerButtonListener,
            SendButtonListener,
            SettingsButtonListener,
            StopButtonListener,
            attach_record_mouse_listener,
            attach_stop_mouse_listener,
        )
        from plugin.framework.uno_context import get_extension_url

        ext_url = get_extension_url(self.ctx)

        from plugin.framework.menu_icon_dpi import menu_icon_asset_rel

        third_kind = header_third_button_kind(model)
        third_btn: tuple[str, Any, Any, Any, Any] | None
        if third_kind == "python_cell":
            third_btn = (
                "btn_latex",
                ActionHandlerButtonListener("scripting.edit_python_cell", self.ctx),
                _("Edit Python in Cell..."),
                menu_icon_asset_rel("python_cell", ctx=self.ctx),
                "",
            )
        elif third_kind == "latex":
            # Keep √x glyph; PNG toolbar icons use the DPI resolver.
            third_btn = ("btn_latex", ActionHandlerButtonListener("writer.insert_latex_dialog", self.ctx), _("Insert LaTeX Math..."), None, "√x")
        else:
            third_btn = None
            latex_ctrl = controls.get("btn_latex")
            if latex_ctrl is not None and hasattr(latex_ctrl, "setVisible"):
                latex_ctrl.setVisible(False)

        header_buttons: list[tuple[str, Any, Any, Any, Any]] = [
            ("btn_settings", SettingsButtonListener(self.ctx), _("Settings"), menu_icon_asset_rel("gear", ctx=self.ctx), ""),
            ("btn_python", ActionHandlerButtonListener("scripting.run_python_dialog", self.ctx), _("Run Python Script..."), menu_icon_asset_rel("python", ctx=self.ctx), ""),
        ]
        if third_btn is not None:
            header_buttons.append(third_btn)
        header_buttons.extend(
            (
            ("btn_search", ActionHandlerButtonListener("embeddings.search_dialog", self.ctx), _("Search Nearby Files..."), menu_icon_asset_rel("search", ctx=self.ctx), ""),
            # Hamburger stays ☰ — no shipped hamburger PNG yet.
            ("btn_hamburger", HamburgerButtonListener(self.ctx, self.xFrame), _("More actions..."), None, None),
            )
        )
        for btn_id, listener_obj, tooltip_text, icon_rel_path, label_text in header_buttons:
            if controls.get(btn_id):
                try:
                    btn_ctrl = controls[btn_id]
                    if hasattr(btn_ctrl, "getModel"):
                        btn_m = btn_ctrl.getModel()
                        if btn_m:
                            if hasattr(btn_m, "HelpText"):
                                btn_m.HelpText = tooltip_text
                            if label_text is not None and hasattr(btn_m, "Label"):
                                btn_m.Label = label_text
                            if icon_rel_path and ext_url and hasattr(btn_m, "ImageURL"):
                                btn_m.ImageURL = ext_url.rstrip("/") + "/" + icon_rel_path
                    btn_ctrl.addActionListener(listener_obj)
                except Exception as e:
                    log.exception("Button %s wiring error: %s", btn_id, e)

        send_listener = None
        try:
            send_listener = SendButtonListener(
                self.ctx,
                self.xFrame,
                controls["send"],
                controls["stop"],
                controls["query"],
                controls["response"],
                controls["image_model_selector"],
                controls["model_selector"],
                controls["status"],
                self.session,
                chat_mode_selector=controls["chat_mode_selector"],
                aspect_ratio_selector=controls["aspect_ratio_selector"],
                base_size_input=controls["base_size_input"],
                sidebar_include_brainstorming=mode_flags.include_brainstorming,
                ensure_path_fn=_initialize_extension_paths,
                clear_control=controls.get("clear"),
            )

            # Save it to the instance so panel_wiring can use it for QueryTextListener
            self.send_listener = send_listener
            register_debug_live_panel(self)
            from plugin.doc.live_panels import register_live_panel

            # The id was stored when the frame session opened. Fill it from
            # this frame's model only when construction could not read it
            # (off the main thread). Do not ask Desktop which component is current.
            session = getattr(self, "frame_session", None)
            uid = self._live_panel_uid
            if not uid and model is not None:
                from plugin.framework.uno_context import get_runtime_uid

                uid = get_runtime_uid(model)
            self._live_panel_uid = uid
            if session is not None:
                if uid and not session.doc_uid:
                    session.doc_uid = uid
                session.bind_panel(self)
                send_listener.frame_session = session
                _bind_close_hook(session, self, send_listener, controls.get("query"))

            register_live_panel(self._live_panel_uid, self)



            try:
                from plugin.doc.doc_type import (
                    doc_type_label_for_enum,
                    doc_type_title_for_label,
                    get_document_type,
                    get_document_uno_services,
                )

                doc_type = get_document_type(model)
                send_listener.cached_doc_type = doc_type_label_for_enum(doc_type)
                send_listener.initial_doc_type = doc_type_title_for_label(send_listener.cached_doc_type)
                send_listener.cached_uno_services = get_document_uno_services(model)
                send_listener.sidebar_include_brainstorming = send_listener.cached_doc_type == "writer"
            except Exception as e:
                # Isolate the document-service query so Send and Stop
                # listeners are still attached. An exception here used to
                # abort the try that also called addActionListener.
                log.exception("Failed to query document type / UNO services for Send listener: %s", e)
                send_listener.cached_uno_services = frozenset()
                if not getattr(send_listener, "cached_doc_type", None):
                    send_listener.cached_doc_type = "writer"
                    send_listener.initial_doc_type = "Writer"
                    send_listener.sidebar_include_brainstorming = True

            send_listener.sidebar_mode_flags = mode_flags

            if controls.get("send"):
                controls["send"].addActionListener(send_listener)
                attach_record_mouse_listener(controls["send"], send_listener)
            start_watchdog_thread(self.ctx, controls.get("status"))

            if controls.get("stop"):
                controls["stop"].addActionListener(StopButtonListener(send_listener))
                attach_stop_mouse_listener(controls["stop"], send_listener)
            send_listener._set_button_states(send_enabled=True, stop_enabled=False)
            # Stop Rec (send) and Stop (leave hands-free / cancel) look alike;
            # tooltips explain them without longer labels at 1x.
            for btn_id, tip in (
                ("send", _("Send. Record: click to record one message, hold 2 seconds for hands-free. Stop Rec: send the recording (hands-free keeps listening).")),
                ("stop", _("Stop the reply or speech (also ends hands-free). While recording: the first click leaves hands-free, the next click cancels the recording.")),
            ):
                ctrl = controls.get(btn_id)
                btn_model = ctrl.getModel() if ctrl and hasattr(ctrl, "getModel") else None
                if btn_model is not None and hasattr(btn_model, "HelpText"):
                    btn_model.HelpText = tip
        except Exception:
            log.exception("Send/Stop button wiring failed")

        clear_listener = None
        active_greeting = self._greeting_for_sidebar_mode(initial_mode, model)
        if controls["clear"]:
            try:
                clear_listener = ClearButtonListener(self.session, controls["response"], controls["status"], greeting=active_greeting, send_listener=send_listener)
                controls["clear"].addActionListener(clear_listener)
                if send_listener is not None:
                    send_listener.clear_listener = clear_listener
            except Exception:
                log.exception("Clear button wiring failed")

        if controls.get("chk_voice"):
            try:
                chk_ctrl = controls["chk_voice"]
                from plugin.framework.config import get_config_bool_safe, set_config
                from plugin.framework.uno_listeners import BaseItemListener

                is_voice = get_config_bool_safe("audio.tts_enabled")
                if hasattr(chk_ctrl, "setState"):
                    chk_ctrl.setState(1 if is_voice else 0)
                if hasattr(chk_ctrl, "getModel") and hasattr(chk_ctrl.getModel(), "HelpText"):
                    chk_ctrl.getModel().HelpText = _("Speak responses aloud (TTS)")

                class VoiceCheckboxListener(BaseItemListener):
                    panel: Any

                    def __init__(self, panel: Any) -> None:
                        self.panel = panel

                    def on_item_state_changed(self, rEvent: Any) -> None:
                        # Programmatic setState during _refresh_controls_from_config must not
                        # re-enter set_config → config:changed → refresh (UNO fires listeners).
                        if getattr(self.panel, "_in_refresh_controls", False):
                            return
                        # Prefer the control state: ItemEvent.Selected is not always set on
                        # programmatic setState, which would wrongly persist False.
                        src = getattr(rEvent, "Source", None)
                        if src is not None and hasattr(src, "getState"):
                            try:
                                val = bool(int(src.getState()) == 1)
                            except Exception:
                                val = bool(getattr(rEvent, "Selected", 0) == 1)
                        else:
                            val = bool(getattr(rEvent, "Selected", 0) == 1)
                        set_config("audio.tts_enabled", val)
                        log.info("Voice checkbox toggled: tts_enabled=%s", val)

                if hasattr(chk_ctrl, "addItemListener"):
                    chk_ctrl.addItemListener(VoiceCheckboxListener(self))
            except Exception:
                log.exception("Voice checkbox wiring failed")

        self._apply_sidebar_mode(initial_mode, model, controls["response"], send_listener, clear_listener, toggle_image_ui)
        self._wire_chat_mode_listener(
            controls["chat_mode_selector"],
            model,
            controls["response"],
            send_listener,
            clear_listener,
            toggle_image_ui,
            mode_flags,
        )


class ChatPanelFactory(unohelper.Base, XUIElementFactory):
    """Factory that creates ChatPanelElement instances for the sidebar."""

    ctx: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # Called externally by LibreOffice UNO framework; do not remove.
    def createUIElement(self, ResourceURL: str, Args: Any) -> XUIElement:
        resource_url = ResourceURL
        args = Args
        log.debug("createUIElement: %s" % resource_url)
        if "ChatPanel" not in resource_url:
            raise NoSuchElementException("Unknown resource: " + resource_url)
        frame = _get_arg(args, "Frame")
        parent_window = _get_arg(args, "ParentWindow")
        log.debug("ParentWindow: %s" % (parent_window is not None))
        if not parent_window:
            raise IllegalArgumentException("ParentWindow is required")

        from plugin.framework.frame_session import document_id_for_frame, open_frame_session

        # One session per frame, opened with the document id of that frame.
        session = open_frame_session(frame, document_id_for_frame(frame))
        return ChatPanelElement(self.ctx, frame, parent_window, resource_url, session)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(ChatPanelFactory, "org.extension.writeragent.ChatPanelFactory", ("com.sun.star.ui.UIElementFactory",))
