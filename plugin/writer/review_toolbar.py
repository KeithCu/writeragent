"""Show the review fast-travel toolbar only while the document has pending agent changes.

The toolbar is declared statically in Addons.xcu and named/hidden-by-default in
WriterWindowState.xcu. Here we flip its visibility at runtime: shown when
``pending_agent_change_count(model) > 0``, hidden when it drops back to zero. All LayoutManager
calls must run on the UNO main thread -- every caller below already does (the agent edit path is
marshalled via execute_on_main_thread; resolve/navigate handlers and document events fire on the
main thread).
"""

import logging
from typing import Any

import uno
import unohelper
from com.sun.star.util import XModifyListener

log = logging.getLogger("writeragent.review_toolbar")

# LO derives this from the OfficeToolBar node oor:name="org.extension.writeragent.toolbar".
TOOLBAR_RESOURCE_URL = "private:resource/toolbar/addon_org.extension.writeragent.toolbar"

_doc_listener = None
_modify_listeners: dict[str, Any] = {}  # RuntimeUID -> _ReviewModifyListener (removed on close)
_docked_uids: set[str] = set()    # RuntimeUIDs whose toolbar has already been docked once (don't re-dock)


def _runtime_uid(model: Any):
    """The document's RuntimeUID, or None if it can't be read."""
    from plugin.doc.document_helpers import get_runtime_uid

    uid = get_runtime_uid(model)
    return uid if uid else None


class _ReviewModifyListener(unohelper.Base, XModifyListener):
    """Hides the toolbar after changes are resolved through LibreOffice's NATIVE UI (Edit ->
    Track Changes -> Accept All, the native context-menu Accept, etc.) -- paths that bypass the
    WriterAgent handlers. Cheap: while the toolbar is hidden (normal typing) it does nothing; it
    only recounts when the toolbar is actually showing (i.e. during a review)."""

    def __init__(self, uid):
        self._uid = uid  # so disposing() can drop the registry entry without re-reading the model

    def modified(self, aEvent) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            model = aEvent.Source
            lm = _layout_manager(model)
            if lm is not None and lm.isElementVisible(TOOLBAR_RESOURCE_URL):
                refresh_review_toolbar(model)
        except Exception:
            log.debug("review_toolbar: modify handler failed", exc_info=True)

    def disposing(self, Source) -> None:  # noqa: N802, N803 -- UNO signature
        # The document we're attached to is being disposed. Drop our registry entry HERE too -- not
        # only via the OnUnload doc event -- so a crash / force-close, or a failed event-listener
        # install, can't leak the entry and block a later document that reuses this RuntimeUID.
        # Only evict if the entry is still OURS: if the RuntimeUID was already recycled for a newer
        # document, a late disposing() must not evict that newer listener.
        if self._uid is not None and _modify_listeners.get(self._uid) is self:
            del _modify_listeners[self._uid]
            _docked_uids.discard(self._uid)


def _register_modify_listener(model: Any) -> None:
    uid = _runtime_uid(model)
    if uid is not None and uid in _modify_listeners:
        return
    listener = _ReviewModifyListener(uid)
    try:
        model.addModifyListener(listener)
        if uid is not None:
            _modify_listeners[uid] = listener
    except Exception:
        log.debug("review_toolbar: modify listener registration failed", exc_info=True)


def _unregister_modify_listener(model: Any) -> None:
    """Remove the per-document modify listener when the document closes (OnUnload). Without this
    the listener and its _modify_listeners entry leak; worse, if LibreOffice recycles the
    RuntimeUID for a later document, _register_modify_listener would skip it and that document
    would never auto-hide the toolbar. Mirrors the OnUnload teardown in grammar_persistence.py."""
    uid = _runtime_uid(model)
    if uid is None:
        return
    _docked_uids.discard(uid)
    listener = _modify_listeners.pop(uid, None)
    if listener is not None:
        try:
            model.removeModifyListener(listener)
        except Exception:
            log.debug("review_toolbar: modify listener removal failed", exc_info=True)


def _layout_manager(model: Any):
    try:
        controller = model.getCurrentController()
        if controller is None:
            return None
        frame = controller.getFrame()
        if frame is None:
            return None
        # XFrame exposes LayoutManager as a property; some builds also have getLayoutManager().
        lm = getattr(frame, "LayoutManager", None)
        if lm is None and hasattr(frame, "getLayoutManager"):
            lm = frame.getLayoutManager()
        return lm
    except Exception:
        log.debug("review_toolbar: layout manager lookup failed", exc_info=True)
        return None


# A docking Y past the existing toolbar rows makes LibreOffice open a NEW full-width row at the
# bottom of the top docking area, instead of squeezing the toolbar into row 0 (which lands it in the
# top-right corner). Any value beyond the real rows works; this is comfortably past them.
_DOCK_NEW_ROW_Y = 30000


def _dock_top(lm: Any) -> None:
    """Dock the toolbar as its OWN full-width row at the top (the look you get by dragging it to
    the top edge), NOT squeezed into the existing toolbar row. The trick: dock at x=0 with a large
    y so LibreOffice opens a NEW row at the bottom of the top docking area instead of appending to
    row 0 (which pushed it to the top-right corner). Best-effort."""
    try:
        from com.sun.star.awt import Point

        area = uno.Enum("com.sun.star.ui.DockingArea", "DOCKINGAREA_TOP")
        pos = Point()
        pos.X = 0
        pos.Y = _DOCK_NEW_ROW_Y
        lm.dockWindow(TOOLBAR_RESOURCE_URL, area, pos)
    except Exception:
        log.debug("review_toolbar: dock-to-top failed", exc_info=True)


def refresh_review_toolbar(model: Any) -> None:
    """Show the toolbar iff the document has >=1 pending agent change, else hide it.
    Best-effort and silent: a Writer without the toolbar, or a non-Writer doc, is a no-op.

    Visibility is per FRAME (each document's own LayoutManager), and every refresh path targets the
    right document's frame, so multiple open documents each track their own toolbar without a
    frame-activation listener. Known minor limitation: a SECOND window of the SAME document
    (Window > New Window) shares one modify listener that only refreshes the active view, so the
    other view's toolbar can be momentarily stale -- rare; not worth enumerating all frames."""
    if model is None:
        return
    try:
        from plugin.writer.inline_review import pending_agent_change_count

        lm = _layout_manager(model)
        if lm is None:
            return
        count = pending_agent_change_count(model)
        if count > 0:
            lm.showElement(TOOLBAR_RESOURCE_URL)
            # Extension toolbars default to FLOATING; dock to the top the FIRST time THIS document's
            # toolbar appears, so it lands with the other toolbars. Only once per document: if the
            # pending count later cycles 0 -> N -> 0 -> N, we must NOT re-dock and override a user who
            # has since floated/moved it. If the uid can't be read, fall back to docking on show.
            uid = _runtime_uid(model)
            if uid is None or uid not in _docked_uids:
                _dock_top(lm)
                if uid is not None:
                    _docked_uids.add(uid)
        else:
            lm.hideElement(TOOLBAR_RESOURCE_URL)
    except Exception:
        log.debug("review_toolbar: refresh failed", exc_info=True)


def _is_writer(model: Any) -> bool:
    try:
        return bool(model) and model.supportsService("com.sun.star.text.TextDocument")
    except Exception:
        return False


def install_review_toolbar(ctx: Any) -> None:
    """Set initial toolbar visibility on every open Writer view and on views opened later.
    Reopening a document that still has pending agent redlines re-shows the toolbar; a clean
    document keeps it hidden."""
    global _doc_listener
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is not None:
            frames = desktop.getFrames()
            if frames is not None:
                for i in range(frames.getCount()):
                    try:
                        frame = frames.getByIndex(i)
                        controller = frame.getController() if frame is not None else None
                        model = controller.getModel() if controller is not None else None
                        if _is_writer(model):
                            _register_modify_listener(model)
                            refresh_review_toolbar(model)
                    except Exception:
                        log.debug("review_toolbar: frame %s skipped", i, exc_info=True)
    except Exception:
        log.debug("review_toolbar: initial sweep failed", exc_info=True)

    if _doc_listener is not None:
        return
    try:
        from plugin.framework.uno_listeners import BaseDocumentEventListener

        class _ToolbarVisibilityListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
                try:
                    name = getattr(Event, "EventName", "") or ""
                    if name not in ("OnViewCreated", "OnLoadFinished", "OnLoad", "OnNew", "OnUnload"):
                        return
                    # Resolve the document model. For OnUnload the ViewController is usually gone and
                    # Event.Source IS the model; for load events it's the view's model.
                    controller = getattr(Event, "ViewController", None)
                    model = None
                    if controller is not None:
                        try:
                            model = controller.getModel()
                        except Exception:
                            model = None
                    if model is None:
                        source = getattr(Event, "Source", None)
                        if _is_writer(source):
                            model = source
                        elif source is not None and hasattr(source, "getCurrentController"):
                            c = source.getCurrentController()
                            model = c.getModel() if c is not None else None
                    if not _is_writer(model):
                        return
                    if name == "OnUnload":
                        _unregister_modify_listener(model)
                    else:
                        _register_modify_listener(model)
                        refresh_review_toolbar(model)
                except Exception:
                    log.debug("review_toolbar: doc-event handling failed", exc_info=True)

        smgr = ctx.getServiceManager()
        broadcaster = smgr.createInstanceWithContext("com.sun.star.frame.GlobalEventBroadcaster", ctx)
        listener = _ToolbarVisibilityListener()
        broadcaster.addDocumentEventListener(listener)
        _doc_listener = listener
        log.debug("review_toolbar: visibility listener attached")
    except Exception:
        log.debug("review_toolbar: listener install failed", exc_info=True)
