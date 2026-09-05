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
"""Global UNO component context provider.

Prefer the bootstrap ``_fallback_ctx`` set from the extension's ``self.ctx``.
Calling ``uno.getComponentContext()`` first can return a different context
(standalone test runners: a local pyuno context with no VCL, which segfaults
on Desktop). AGENTS.md: use the extension context, not a fresh UNO context.

All services that need UNO access should call ``get_ctx()`` rather than
storing a ctx reference from ``initialize()``.

Concurrency: the component context (``ctx``) must be the one LibreOffice
gave the extension at load, stored in ``_fallback_ctx``. Calling
``uno.getComponentContext()`` from a background thread or a test runner
can return a **different** context with no UI, which then segfaults or
fails to find dialogs. This module does **not** make the Writer/Calc
document model safe from any thread — wrap document access with
``guard_uno`` and marshal UI work through ``QueueExecutor``.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, cast

from plugin.framework.constants import (
    EXTENSION_ID_LIBREHARPER,
    EXTENSION_ID_LIBREPY,
    EXTENSION_ID_WRITERAGENT,
)
from plugin.framework.thread_guard import main_thread_only, on_main_thread, _wrap_uno

log = logging.getLogger("writeragent.context")

_fallback_ctx = None
# Set by main.py / main_core.py bootstrap; auto-detected from installed packages when unset.
_package_extension_id: str | None = None

# Probe order: LibrePy first when both family OXTs are installed (existing behavior).
_KNOWN_EXTENSION_IDS = (
    EXTENSION_ID_LIBREPY,
    EXTENSION_ID_WRITERAGENT,
    EXTENSION_ID_LIBREHARPER,
)

_is_libreharper_cache: bool | None = None


def is_libreharper() -> bool:
    """Return True if running under the LibreHarper extension."""
    global _is_libreharper_cache
    if _is_libreharper_cache is not None:
        return _is_libreharper_cache
    if _package_extension_id == EXTENSION_ID_LIBREHARPER:
        _is_libreharper_cache = True
        return True
    try:
        from plugin import _manifest
        _is_libreharper_cache = any(m.get("title") == "LibreHarper" for m in getattr(_manifest, "MODULES", []))
    except ImportError:
        _is_libreharper_cache = False
    return _is_libreharper_cache



def set_fallback_ctx(ctx):
    """Store a fallback ctx for use when uno module is not available."""
    global _fallback_ctx
    _fallback_ctx = ctx


def set_package_extension_id(extension_id: str) -> None:
    """Pin the OXT package id used by get_extension_url() (LibrePy vs WriterAgent)."""
    global _package_extension_id, _is_libreharper_cache
    _package_extension_id = extension_id
    if extension_id == EXTENSION_ID_LIBREHARPER:
        _is_libreharper_cache = True
    elif extension_id is not None:
        _is_libreharper_cache = False


def reset_package_extension_id_for_tests() -> None:
    """Clear cached extension id (unit tests only)."""
    global _package_extension_id, _is_libreharper_cache
    _package_extension_id = None
    _is_libreharper_cache = None


def resolve_package_extension_id(ctx=None) -> str:
    """Return the installed WriterAgent-family extension id (LibrePy or WriterAgent).

    Cache is pinned at bootstrap (``set_package_extension_id``).
    ``get_package_info`` is main-thread only, so off-main without a cache
    returns the WriterAgent default (same as the last-resort below).
    """
    global _package_extension_id
    if _package_extension_id:
        return _package_extension_id

    if not on_main_thread():
        return EXTENSION_ID_WRITERAGENT

    for extension_id in _KNOWN_EXTENSION_IDS:
        try:
            pip = get_package_info(ctx)
            if pip is None:
                continue
            location = pip.getPackageLocation(extension_id)
            if location:
                _package_extension_id = extension_id
                return extension_id
        except Exception:
            log.debug("getPackageLocation(%s) failed", extension_id, exc_info=True)

    # Last resort: preserve WriterAgent default for older call sites.
    return EXTENSION_ID_WRITERAGENT


def product_display_name(ctx=None) -> str:
    """User-visible product name for dialog titles (LibrePy vs WriterAgent)."""
    if resolve_package_extension_id(ctx) == EXTENSION_ID_LIBREPY:
        return "LibrePy"
    return "WriterAgent"


@main_thread_only
def get_ctx():
    """Return the UNO component context.

    Prefers the bootstrap context stored at extension init. ``uno.getComponentContext()``
    is only used when that fallback is unset (and must not be preferred in test
    runners — see module docstring).
    """
    # BUGFIX: In standalone runner processes (like test runners), uno.getComponentContext()
    # returns a local standalone pyuno context that lacks a VCL instance. Attempting to
    # instantiate com.sun.star.frame.Desktop on this local context causes a segmentation fault.
    # We prefer the explicitly set _fallback_ctx (which holds the remote connection context)
    # to prevent standalone runs from trying to use the local PyUNO context.
    if _fallback_ctx is not None:
        return _wrap_uno(_fallback_ctx)
    try:
        import uno

        if hasattr(uno, "getComponentContext"):
            ctx = uno.getComponentContext()
            if ctx is not None:
                return _wrap_uno(ctx)
    except ImportError:
        pass
    return _wrap_uno(_fallback_ctx)


from plugin.framework.errors import check_disposed, safe_call, UnoObjectError


def get_service_manager(ctx: Any) -> Any | None:
    """Return the UNO ServiceManager from *ctx*, or None."""
    if ctx is None:
        return None
    ctx_any = cast("Any", ctx)
    smgr = getattr(ctx_any, "ServiceManager", None)
    if smgr is None:
        getter = getattr(ctx_any, "getServiceManager", None)
        smgr = getter() if callable(getter) else None
    return smgr


@main_thread_only
def get_desktop(ctx=None):
    """Return the UNO Desktop instance."""
    ctx = ctx or get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    smgr = get_service_manager(ctx_any)
    assert smgr is not None
    desktop = cast("Any", smgr).createInstanceWithContext("com.sun.star.frame.Desktop", ctx_any)
    return _wrap_uno(desktop)


@main_thread_only
def get_active_document(ctx=None):
    """Return the currently active document model."""
    try:
        desktop = get_desktop(ctx)
        check_disposed(desktop, "Desktop")
        doc = safe_call(desktop.getCurrentComponent, "Desktop component resolution")
        return _wrap_uno(doc)
    except UnoObjectError:
        log.exception("get_active_document UnoObjectError")
        return None
    except Exception:
        log.exception("get_active_document unexpected exception")
        return None


@main_thread_only
def get_package_info(ctx=None):
    """Return the PackageInformationProvider singleton."""
    ctx = ctx or get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    gvn = getattr(ctx_any, "getValueByName", None)
    if gvn is None:
        return None
    pip = gvn("/singletons/com.sun.star.deployment.PackageInformationProvider")
    return _wrap_uno(pip)


@main_thread_only
def get_extension_url(ctx=None, extension_id=None):
    """Return the base URL of the extension package."""
    if extension_id is None:
        extension_id = resolve_package_extension_id(ctx)
    try:
        pip = get_package_info(ctx)
        if not pip:
            return ""
        location = pip.getPackageLocation(extension_id)
        if location:
            return location
    except Exception:
        log.debug("get_extension_url(%s) failed", extension_id, exc_info=True)
    return "vnd.sun.star.extension://" + extension_id


def menu_icon_asset_url(ext_url, icon_filename):
    """Return GraphicProvider URL for a menu icon shipped in OXT assets/."""
    return "%s/assets/%s" % (ext_url.rstrip("/"), icon_filename)


def menu_icon_filesystem_paths(icon_filename: str) -> tuple[str, ...]:
    """Local PNG paths for menu icons (OXT layout first, then git checkout).

    ``scripts/build_oxt.py`` remaps ``extension/assets/`` to ``assets/`` at the
    bundle root. ``make release`` pytest/UNO runs against that tree, so looking
    only under ``extension/assets/`` misses ``python_32.png`` and friends.
    """

    from plugin.framework.constants import get_plugin_dir

    clean = icon_filename.replace("assets/", "").lstrip("/")
    root = os.path.dirname(get_plugin_dir())
    return (
        os.path.join(root, "assets", clean),
        os.path.join(root, "extension", "assets", clean),
    )


def get_extension_path(ctx=None, extension_id=None):
    """Return the local filesystem path of the extension package."""
    url = get_extension_url(ctx, extension_id)
    if not url:
        return ""
    if url.startswith("file://"):
        import uno

        return str(uno.fileUrlToSystemPath(url))
    return url


@main_thread_only
def get_toolkit(ctx=None):
    """Safely retrieve the com.sun.star.awt.Toolkit service."""
    ctx = ctx or get_ctx()
    if ctx is None:
        return None
    try:
        from typing import cast

        ctx_any = cast("Any", ctx)
        smgr = get_service_manager(ctx_any)
        if smgr is None:
            return None
        tk = cast("Any", smgr).createInstanceWithContext("com.sun.star.awt.Toolkit", ctx_any)
        return _wrap_uno(tk)
    except Exception:
        log.exception("Failed to create toolkit")
        return None


# Sidebar query field: restore here after RichTextControl setFocus, not
# getFocusWindow() (often the Send button after a click, or the transcript).
# Stock Toolkit has no getFocusWindow (PyUNO hasattr lies → always None).
_default_focus_restore = None
_restore_query_after_scroll = True
_stream_focus_trackers: list[Any] = []
_stream_rich_control = None


def set_default_focus_restore(control) -> None:
    """Pin focus restore to the chat query field (or None on panel dispose)."""
    global _default_focus_restore
    _default_focus_restore = control


def note_user_wants_query() -> None:
    """Mark Ask/instruct as the restore target after a stream SelectAll.

    Called from _do_send next to query.setFocus(), and from query focusGained.
    """
    global _restore_query_after_scroll
    _restore_query_after_scroll = True


def note_user_left_query() -> None:
    """Stop restoring Ask/instruct after stream SelectAll.

    Bug: ``restore_query_if_user_still_there()`` runs on every stream chunk and
    calls ``query.setFocus()``. That aborts an in-flight Stop click so GTK/VCL
    never delivers ``ActionEvent`` (Packet B1: Stop looked enabled, ramble ran
    to word199, no ``STOP_CLICKED`` in the log). Sidebar Stop/Clear/other
    controls call this on mouseEntered/mousePressed/focusGained — earlier than
    ActionEvent — so later chunks no-op the restore and the click can finish.
    Writer page clicks use the same flag (document ``XMouseClickHandler``).
    """
    global _restore_query_after_scroll
    if not _restore_query_after_scroll:
        return
    _restore_query_after_scroll = False
    log.debug("stream focus: left query")


def restore_query_if_user_still_there() -> None:
    """After a stream SelectAll, put the caret back in Ask/instruct unless the user left."""
    if not _restore_query_after_scroll:
        return
    q = _default_focus_restore
    if q is None or not hasattr(q, "setFocus"):
        return
    try:
        q.setFocus()
        log.debug("restore_query_if_user_still_there")
    except Exception as e:
        log.debug("restore_query_if_user_still_there: %s", e)


def _current_document_controller(ctx):
    try:
        smgr = getattr(ctx, "ServiceManager", None)
        if smgr is None:
            return None
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        comp = desktop.getCurrentComponent() if desktop is not None else None
        if comp is None:
            return None
        return comp.getCurrentController()
    except Exception as e:
        log.debug("document controller: %s", e)
        return None


def _attach_leave_query_listeners(control) -> None:
    """Stop restoring Ask/instruct when the user targets this sidebar control.

    Document page clicks are handled by ``XMouseClickHandler``; sidebar Stop
    is not on that path. mouseEntered is earlier than ActionEvent.
    """
    if control is None:
        return
    try:
        import unohelper
        from com.sun.star.awt import XFocusListener, XMouseListener
    except ImportError:
        return

    class _LeaveQueryFocus(unohelper.Base, XFocusListener):
        def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
            return

        def focusLost(self, e):  # noqa: N802 -- UNO signature
            return

        def focusGained(self, e):  # noqa: N802 -- UNO signature
            note_user_left_query()
            log.debug("stream focus: sidebar control")

    class _LeaveQueryMouse(unohelper.Base, XMouseListener):
        def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
            return

        def mousePressed(self, e):  # noqa: N802 -- UNO signature
            note_user_left_query()

        def mouseReleased(self, e):  # noqa: N802 -- UNO signature
            return

        def mouseEntered(self, e):  # noqa: N802 -- UNO signature
            note_user_left_query()

        def mouseExited(self, e):  # noqa: N802 -- UNO signature
            return

    try:
        if hasattr(control, "addMouseListener"):
            mouse_track = _LeaveQueryMouse()
            control.addMouseListener(mouse_track)
            _stream_focus_trackers.append(mouse_track)
        if hasattr(control, "addFocusListener"):
            focus_track = _LeaveQueryFocus()
            control.addFocusListener(focus_track)
            _stream_focus_trackers.append(focus_track)
    except Exception as e:
        log.debug("leave-query listeners: %s", e)


def install_stream_focus_tracker(ctx, query=None, rich=None, leave_query_controls=None) -> None:
    """Query focusGained → keep restoring. Document / sidebar pointer → stop.

    Window focus listeners miss in-frame query→page clicks (same top-level).
    Writer's XUserInputInterception mouse handler sees the page click.
    Sidebar Stop/Clear/other widgets are not on that handler — pass them as
    *leave_query_controls* so stream ``query.setFocus()`` does not steal the
    click (Packet B1).
    """
    global _stream_rich_control, _default_focus_restore
    if query is not None:
        _default_focus_restore = query
    if rich is not None:
        _stream_rich_control = rich
    # Query/doc listeners are process-global (first sidebar wins). A later
    # Writer/Calc panel still needs Stop/Clear mouse listeners or stream
    # setFocus can swallow Stop on that panel.
    if _stream_focus_trackers:
        for ctrl in leave_query_controls or ():
            if ctrl is not None and ctrl is not query:
                _attach_leave_query_listeners(ctrl)
        return
    try:
        import unohelper
        from com.sun.star.awt import XFocusListener, XMouseClickHandler
    except ImportError:
        return

    class _QueryFocus(unohelper.Base, XFocusListener):
        def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
            return

        def focusLost(self, e):  # noqa: N802 -- UNO signature
            return

        def focusGained(self, e):  # noqa: N802 -- UNO signature
            note_user_wants_query()
            log.debug("stream focus: query")

    class _DocClick(unohelper.Base, XMouseClickHandler):
        def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
            return

        def mousePressed(self, e):  # noqa: N802 -- UNO signature
            note_user_left_query()
            log.debug("stream focus: document click")
            return False

        def mouseReleased(self, e):  # noqa: N802 -- UNO signature
            return False

    controller = None
    try:
        if query is not None and hasattr(query, "addFocusListener"):
            q_track = _QueryFocus()
            query.addFocusListener(q_track)
            _stream_focus_trackers.append(q_track)
        controller = _current_document_controller(ctx)
        if controller is not None and hasattr(controller, "addMouseClickHandler"):
            d_track = _DocClick()
            controller.addMouseClickHandler(d_track)
            _stream_focus_trackers.append(d_track)
        for ctrl in leave_query_controls or ():
            if ctrl is not None and ctrl is not query:
                _attach_leave_query_listeners(ctrl)
        log.debug(
            "install_stream_focus_tracker n=%d mouse=%s",
            len(_stream_focus_trackers),
            bool(controller is not None and hasattr(controller, "addMouseClickHandler")),
        )
    except Exception as e:
        log.debug("install_stream_focus_tracker: %s", e)


def _focus_restore_target(explicit=None):
    if explicit is not None:
        return explicit
    return _default_focus_restore


@contextmanager
def focus_preserved(ctx, restore=None):
    """Restore focus after a block that may steal it (RichTextControl reveal).

    If *restore* or :func:`set_default_focus_restore` is set, that control is
    focused on exit (the query field). Otherwise the toolkit focus window at
    entry is restored — which is wrong after Send-button clicks.
    """
    pinned = _focus_restore_target(restore)
    saved = pinned
    if saved is None:
        try:
            tk = get_toolkit(ctx)
            if tk is not None and hasattr(tk, "getFocusWindow"):
                saved = tk.getFocusWindow()
        except Exception as e:
            log.debug("focus_preserved capture: %s", e)
    try:
        yield
    finally:
        if saved is not None:
            try:
                if hasattr(saved, "setFocus"):
                    saved.setFocus()
            except Exception as e:
                log.debug("focus_preserved restore: %s", e)


@main_thread_only
def process_events_to_idle(ctx, rounds: int = 1, force: bool = False) -> bool:
    """Drain the UI event queue *rounds* times via the approved VCL pump chokepoint.

    When a chat/MCP :func:`~plugin.framework.queue_executor.drain_owner_scope` is
    active, skips VCL pumping so secondary progress helpers (grep, Harper status,
    notebook import) cannot nest ``processEventsToIdle`` inside the drain loop.
    Pass force=True (e.g. for RichTextControl caret reveal) to pump VCL even when
    under a drain owner.
    Returns True if at least one VCL pump ran.
    """
    from plugin.framework.queue_executor import _note_suppressed_vcl_pump, _pump_vcl_events, get_drain_owner

    if not force:
        if os.environ.get("WRITERAGENT_TESTING") == "1":
            return False
        owner = get_drain_owner()
        if owner is not None:
            _note_suppressed_vcl_pump(owner)
            return False

    pumped = False
    for _idx in range(max(1, rounds)):
        try:
            tk = get_toolkit(ctx)
            if _pump_vcl_events(tk):
                pumped = True
        except Exception:
            log.debug("process_events_to_idle failed", exc_info=True)
    return pumped


def normalize_doc_url(url):
    """Normalize document URL for comparison (strip, optional trailing slash).

    Shared by resolve-by-URL, MCP doc keys, and document-script stale detection.
    Does not repair ``file:/`` vs ``file:///`` — that is ``text_helpers.normalize_file_url``.
    """
    if not url:
        return ""
    s = str(url).strip()
    if s.endswith("/") and len(s) > 1:
        s = s[:-1]
    return s


def get_runtime_uid(model):
    """Stable per-session id for an open component.

    Unlike the document URL, ``RuntimeUID`` exists even for unsaved/untitled
    documents, so it can address a document that has no file on disk yet.
    Returns "" if unavailable.

    Tries ``getRuntimeUID()``, attribute access, and ``getPropertyValue("RuntimeUID")`` in turn
    because LibreOffice builds expose the id through different UNO surfaces. Only plain ``str`` /
    ``int`` values are accepted so auto-mocked UNO attributes (e.g. ``MagicMock.RuntimeUID``)
    cannot masquerade as a real uid.
    """
    for accessor in (
        lambda m: m.getRuntimeUID() if callable(getattr(m, "getRuntimeUID", None)) else None,
        lambda m: getattr(m, "RuntimeUID", None),
        lambda m: m.getPropertyValue("RuntimeUID"),
    ):
        try:
            raw = accessor(model)
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int):
                return str(raw)
            if isinstance(raw, str) and raw:
                return raw
        except Exception:
            continue
    return ""


@main_thread_only
def resolve_document_by_url(ctx, url):
    """Resolve an open document by URL or RuntimeUID. Must be called on the UNO main thread.

    ``url`` may be a document URL or a ``RuntimeUID`` (as returned by
    ``list_open_documents``); the RuntimeUID also matches unsaved/untitled
    documents that have no URL yet.
    Returns (doc, doc_type) or (None, None) if not found.
    doc_type is one of 'writer', 'calc', 'draw'.
    """
    if not url or not str(url).strip():
        return (None, None)
    from plugin.doc import doc_type as _doc_type

    target = normalize_doc_url(url)
    try:
        desktop = get_desktop(ctx)
        comps = desktop.getComponents()
        if not comps:
            return (None, None)
        enum = comps.createEnumeration()
        if not enum:
            return (None, None)
        while enum and enum.hasMoreElements():
            elem = enum.nextElement()
            try:
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and callable(getattr(elem, "getController")):
                    # Desktop enumeration can yield frames, not models. Frames
                    # expose the document via getController().getModel().
                    controller = elem.getController()
                    if controller is not None and hasattr(controller, "getModel"):
                        model = controller.getModel()
                if model is not None:
                    doc_url = normalize_doc_url(model.getURL()) if hasattr(model, "getURL") else ""
                    uid = get_runtime_uid(model)
                    if (doc_url and doc_url == target) or (uid and uid == target):
                        doc_type_enum = _doc_type.get_document_type(model)
                        doc_type = _doc_type.doc_type_label_for_enum(doc_type_enum, impress_as_draw=True)
                        return (_wrap_uno(model), doc_type)
            except Exception as e:
                logging.getLogger(__name__).debug("resolve_document_by_url element error: %s", type(e).__name__)
                continue
    except Exception:
        logging.getLogger(__name__).exception("resolve_document_by_url enumeration error")
    return (None, None)


@main_thread_only
def get_document_from_frame(frame):
    """Get the document model strictly from the frame controller.

    This is the preferred path for sidebar panels to ensure we resolve
    the document bound to the active window rather than relying on Desktop.
    """
    if not frame:
        return None
    from plugin.framework.errors import suppress_disposed
    from plugin.framework.thread_guard import guard_uno

    with suppress_disposed("resolve document from frame", logger=logging.getLogger(__name__)):
        check_disposed(frame, "Frame")
        controller = frame.getController()
        if not controller:
            return None
        check_disposed(controller, "Controller")
        model = controller.getModel()
        if model is not None:
            return guard_uno(model)
    return None
