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

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import threading
    from collections.abc import Generator

    from com.sun.star.awt import FocusEvent, MouseEvent
    from com.sun.star.lang import EventObject

from plugin.framework.constants import EXTENSION_ID_LIBREHARPER, EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT
from plugin.framework.thread_guard import main_thread_only, on_main_thread

log = logging.getLogger("writeragent.context")

_fallback_ctx = None
# Set by main.py / main_core.py bootstrap; auto-detected from installed packages when unset.
_package_extension_id: str | None = None

# Probe order: LibrePy first when both family OXTs are installed (existing behavior).
_KNOWN_EXTENSION_IDS = (EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT, EXTENSION_ID_LIBREHARPER)

_is_libreharper_cache: bool | None = None

# uno.bin / unopkg register helpers have no VCL. Creating Desktop there SEGVs
# (issue #768). pythonloader often rewrites sys.argv, so also read /proc.
_UNO_HELPER_BASENAMES = frozenset({"uno", "uno.bin", "uno.exe", "unopkg", "unopkg.bin", "unopkg.com", "unopkg.exe"})


def _basename_is_uno_helper(name: str) -> bool:
    return os.path.basename(name).strip().lower() in _UNO_HELPER_BASENAMES


def _tokens_have_singleaccept(tokens: list[str]) -> bool:
    return any(token == "--singleaccept" or token.startswith("--singleaccept=") for token in tokens)


def _linux_process_tokens() -> list[str]:
    """Real process image and args. pythonloader may rewrite ``sys.argv`` (#768)."""
    tokens: list[str] = []
    try:
        tokens.append(os.readlink("/proc/self/exe"))
    except OSError:
        pass
    try:
        with open("/proc/self/comm", encoding="utf-8") as comm_file:
            comm = comm_file.read().strip()
        if comm:
            tokens.append(comm)
    except OSError:
        pass
    try:
        with open("/proc/self/cmdline", "rb") as cmdline_file:
            raw = cmdline_file.read().split(b"\0")
        tokens.extend(part.decode("utf-8", "replace") for part in raw if part)
    except OSError:
        pass
    return tokens


def desktop_create_is_unsafe() -> bool:
    """True in uno.bin / unopkg helpers that have no VCL.

    ``createInstanceWithContext("com.sun.star.frame.Desktop")`` and
    ``getValueByName(theDesktop)`` on that ctx take SolarMutexGuard →
    GetYieldMutex and SEGV (issue #768). GUI soffice already has Desktop.

    Do not trust ``sys.argv`` alone: pythonloader inside
    ``uno.bin --singleaccept`` often leaves argv as ``['']`` or a .py path.
    """
    argv = [str(arg) for arg in sys.argv]
    if argv and _basename_is_uno_helper(argv[0]):
        return True
    if _tokens_have_singleaccept(argv):
        return True
    exe = getattr(sys, "executable", "") or ""
    if exe and _basename_is_uno_helper(exe):
        return True
    proc_tokens = _linux_process_tokens()
    if any(_basename_is_uno_helper(token) for token in proc_tokens):
        return True
    return _tokens_have_singleaccept(proc_tokens)


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


def set_fallback_ctx(ctx: Any) -> None:
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


def resolve_package_extension_id(ctx: Any | None = None) -> str:
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


def product_display_name(ctx: Any | None = None) -> str:
    """User-visible product name for dialog titles (LibrePy vs WriterAgent)."""
    if resolve_package_extension_id(ctx) == EXTENSION_ID_LIBREPY:
        return "LibrePy"
    return "WriterAgent"


def _guard_returned_uno(obj: Any) -> Any:
    """Wrap a UNO boundary return. Imports ``guard_uno`` at the call.

    What was wrong: ``get_ctx``, ``get_desktop``, ``get_active_document``,
    ``get_package_info``, ``get_toolkit``, and ``resolve_document_by_url``
    called a module-level ``_wrap_uno`` copied in at import. Patching
    ``plugin.framework.thread_guard.guard_uno`` never saw those returns.
    How it happened: ``from thread_guard import _wrap_uno`` binds the
    function object; a later patch of ``guard_uno`` does not replace it.
    Why this change: import ``guard_uno`` here, the same pattern as
    ``get_document_from_frame``. PropertyValue media descriptors are not
    passed through this helper.
    """
    from plugin.framework.thread_guard import guard_uno

    return guard_uno(obj)


@main_thread_only
def get_ctx() -> Any:
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
        return _guard_returned_uno(_fallback_ctx)
    try:
        import uno

        if hasattr(uno, "getComponentContext"):
            ctx = uno.getComponentContext()
            if ctx is not None:
                return _guard_returned_uno(ctx)
    except ImportError:
        pass
    return _guard_returned_uno(_fallback_ctx)


from plugin.framework.errors import DocumentDisposedError, check_disposed, safe_call, UnoObjectError


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
def get_desktop(ctx: Any | None = None) -> Any:
    """Return the UNO Desktop instance, or None when creating it would SEGV.

    uno.bin / unopkg register helpers have no VCL. ``createInstance(Desktop)``
    takes SolarMutexGuard → GetYieldMutex and crashes (issue #768). GUI
    soffice keeps the existing create path.
    """
    if desktop_create_is_unsafe():
        log.debug("get_desktop skipped: no-VCL helper process (issue #768)")
        return None
    ctx = ctx or get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    smgr = get_service_manager(ctx_any)
    assert smgr is not None
    desktop = cast("Any", smgr).createInstanceWithContext("com.sun.star.frame.Desktop", ctx_any)
    return _guard_returned_uno(desktop)


def new_blank_writer(ctx: Any = None, *, target: str = "_blank", flags: int = 0, extra_props: tuple[Any, ...] = ()) -> Any:
    """Hidden, **empty** Writer used as a scratch buffer.

    What was wrong: every scratch document was opened with
    ``private:factory/swriter``, which honours the user's *default template*.
    How it happened: a firm that sets its petition model as the default template
    got that model's text in every scratch doc, and the callers append to it and
    read the whole body back — so the model's header ("AO DOUTO JUIZO DO ...")
    came back glued to the caller's real content, and landed in range reads,
    plain-text conversions and full-document rewrites. Why this change fixes it:
    the factory URL is still used (it is the only way to get a Writer with the
    user's own styles), but the body is emptied before the caller sees it.

    Returns None when the desktop is unavailable (no-VCL helper processes).
    """
    desktop = get_desktop(ctx)
    if desktop is None:
        return None
    import uno

    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    doc = desktop.loadComponentFromURL("private:factory/swriter", target, flags, (hidden,) + tuple(extra_props))
    clear_writer_body(doc)
    # Other document lookups wrap the model so a later off-thread use is
    # caught by the dev thread guard. This factory used to return it raw.
    return _guard_returned_uno(doc)


def _reraise_document_disposed(exc: BaseException, object_type: str) -> None:
    """Re-raise real UNO disposal. Other exceptions stay with the caller.

    What was wrong: scratch cleanup and document lookup caught Exception and
    treated a disposed document as empty or not open. How: DisposedException
    is an Exception, so those handlers swallowed it. Why: only real disposal
    (not a bare RuntimeException) becomes DocumentDisposedError.
    """
    from plugin.framework.errors import _is_real_disposal

    if not _is_real_disposal(exc):
        return
    if isinstance(exc, DocumentDisposedError):
        raise exc
    raise DocumentDisposedError(str(exc) or "UNO object was disposed", object_type=object_type) from exc


def clear_writer_body(doc: Any) -> bool:
    """Empty *doc* of everything a template can put in it. True when something was removed.

    Not just the body text: a letterhead template is often an empty table or a logo
    anchored to the page, whose body string is "" -- testing the text alone left that
    table in the scratch doc, and it came back in range reads. So tables, text frames
    and drawing shapes are disposed explicitly, then the text is cleared.

    Split out so callers that open (or reuse) a scratch Writer their own way can
    still drop a default template's content.
    """
    if doc is None:
        return False
    removed = False
    for supplier in ("getTextTables", "getTextFrames"):
        try:
            container = getattr(doc, supplier)()
            names = list(container.getElementNames())
        except Exception as e:
            _reraise_document_disposed(e, "Writer")
            continue
        for name in names:
            try:
                if container.hasByName(name):  # a nested table goes with its parent
                    container.getByName(name).dispose()
                    removed = True
            except Exception as e:
                _reraise_document_disposed(e, "Writer")
                log.debug("clear_writer_body: could not dispose %s %r", supplier, name, exc_info=True)
    try:
        page = doc.getDrawPage()
        # Bounded, never `while getCount()`: if a remove silently fails the count never
        # drops, and an unbounded loop here would freeze the main thread.
        # The cap is the count at entry, not a live getCount() check.
        removal_budget = int(page.getCount())
        removal_attempt = 0
        while removal_attempt < removal_budget:
            before = page.getCount()
            page.remove(page.getByIndex(0))
            removal_attempt += 1
            if page.getCount() >= before:
                break
            removed = True
    except Exception as e:
        _reraise_document_disposed(e, "Writer")
        log.debug("clear_writer_body: could not empty the draw page", exc_info=True)
    try:
        text = doc.getText()
        if (text.getString() or "").strip():
            removed = True
        text.setString("")
    except Exception as e:
        _reraise_document_disposed(e, "Writer")
        log.debug("clear_writer_body failed", exc_info=True)
    if removed:
        log.debug("clear_writer_body: dropped default-template content from a scratch Writer")
    return removed


@main_thread_only
def get_active_document(ctx: Any | None = None) -> Any:
    """Return the currently active document model."""
    try:
        desktop = get_desktop(ctx)
        if desktop is None:
            return None
        check_disposed(desktop, "Desktop")
        doc = safe_call(desktop.getCurrentComponent, "Desktop component resolution")
        return _guard_returned_uno(doc)
    except DocumentDisposedError:
        # What was wrong: a document that died mid-call looked like nothing
        # was open. How it happened: DocumentDisposedError subclasses
        # UnoObjectError, and this handler returned None for every
        # UnoObjectError (safe_call wraps DisposedException that way).
        # Why this change: re-raise disposal so callers cannot treat a dying
        # document as "no document". None stays the answer when get_desktop()
        # is None or the component itself is missing.
        raise
    except UnoObjectError:
        log.exception("get_active_document UnoObjectError")
        return None
    except Exception as e:
        # What was wrong: DisposedException from get_desktop() is a plain
        # Exception, so a dying desktop looked like nothing open. safe_call
        # already re-raises disposal from getCurrentComponent.
        _reraise_document_disposed(e, "Desktop")
        log.exception("get_active_document unexpected exception")
        return None


@main_thread_only
def get_package_info(ctx: Any | None = None) -> Any:
    """Return the PackageInformationProvider singleton."""
    ctx = ctx or get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    gvn = getattr(ctx_any, "getValueByName", None)
    if gvn is None:
        return None
    pip = gvn("/singletons/com.sun.star.deployment.PackageInformationProvider")
    return _guard_returned_uno(pip)


@main_thread_only
def get_extension_url(ctx: Any | None = None, extension_id: str | None = None) -> str:
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


def menu_icon_asset_url(ext_url: str, icon_filename: str) -> str:
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
    return (os.path.join(root, "assets", clean), os.path.join(root, "extension", "assets", clean))


def get_extension_path(ctx: Any | None = None, extension_id: str | None = None) -> str:
    """Return the local filesystem path of the extension package."""
    url = get_extension_url(ctx, extension_id)
    if not url:
        return ""
    if url.startswith("file://"):
        import uno

        return str(uno.fileUrlToSystemPath(url))
    return url


@main_thread_only
def get_toolkit(ctx: Any | None = None) -> Any:
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
        return _guard_returned_uno(tk)
    except Exception:
        log.exception("Failed to create toolkit")
        return None


# Sidebar query field: restore here after RichTextControl setFocus, not
# getFocusWindow() (often the Send button after a click, or the transcript).
# Stock Toolkit has no getFocusWindow (PyUNO hasattr lies → always None).
_default_focus_restore = None
_restore_query_after_scroll = True
_stream_focus_trackers: list[Any] = []
# The query focus listener, separate from leave-query and document-click
# entries in _stream_focus_trackers. Those other entries stay after the
# sidebar closes; keying "already installed" on the whole list meant a
# reopened panel never got focusGained.
_query_focus_listener: Any = None
# (query control, listener). One focus listener per Ask field. A second
# sidebar must not be skipped because the first window's listener is live.
_query_focus_bindings: list[tuple[Any, Any]] = []
# (control, mouse listener, focus listener). Leave-query listeners used to
# accumulate: disposing() was a no-op and every install attached again.
_leave_query_bindings: list[tuple[Any, Any, Any]] = []
# (controller, handler). One click handler per document controller, not one
# for the process: the first sidebar used to subscribe only the document that
# was current at install time.
_doc_click_bindings: list[tuple[Any, Any]] = []


def set_default_focus_restore(control: Any) -> None:
    """Pin focus restore to the chat query field (or None on panel dispose)."""
    global _default_focus_restore
    _default_focus_restore = control


def clear_default_focus_restore_if(control: Any) -> None:
    """Clear the restore pin only when it still points at *control*.

    What was wrong: every sidebar dispose set the pin to None, including a
    second window closing while another sidebar's query field was the pin.
    """
    global _default_focus_restore
    if control is not None and _default_focus_restore is control:
        _default_focus_restore = None


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


def restore_query_if_user_still_there(query: Any = None) -> None:
    """After a stream SelectAll, put the caret back in Ask/instruct unless the user left.

    *query* is that panel's Ask field. Stream chunks pass it so a second
    sidebar's pin does not steal setFocus. Callers without a control still
    use the process-wide pin.
    """
    if not _restore_query_after_scroll:
        return
    q = query if query is not None else _default_focus_restore
    if q is None or not hasattr(q, "setFocus"):
        return
    try:
        q.setFocus()
        log.debug("restore_query_if_user_still_there")
    except Exception as e:
        log.debug("restore_query_if_user_still_there: %s", e)


def _current_document_controller(ctx: Any) -> Any:
    try:
        # Same no-VCL fail-soft as get_desktop (issue #768). Do not create
        # Desktop via ServiceManager here — that bypassed the choke point.
        desktop = get_desktop(ctx)
        if desktop is None:
            return None
        comp = desktop.getCurrentComponent()
        if comp is None:
            return None
        return comp.getCurrentController()
    except Exception as e:
        log.debug("document controller: %s", e)
        return None


def _release_leave_query_binding(control: Any, mouse: Any, focus: Any) -> None:
    """Drop Stop/Clear/Send listeners so a disposed control is not pinned."""
    try:
        if mouse is not None and control is not None and hasattr(control, "removeMouseListener"):
            control.removeMouseListener(mouse)
    except Exception as e:
        log.debug("removeMouseListener: %s", e)
    try:
        if focus is not None and control is not None and hasattr(control, "removeFocusListener"):
            control.removeFocusListener(focus)
    except Exception as e:
        log.debug("removeFocusListener: %s", e)
    _leave_query_bindings[:] = [row for row in _leave_query_bindings if row[1] is not mouse and row[2] is not focus]
    for listener in (mouse, focus):
        if listener is None:
            continue
        try:
            _stream_focus_trackers.remove(listener)
        except ValueError:
            pass


def _attach_leave_query_listeners(control: Any) -> None:
    """Stop restoring Ask/instruct when the user targets this sidebar control.

    Document page clicks are handled by ``XMouseClickHandler``; sidebar Stop
    is not on that path. mouseEntered is earlier than ActionEvent.

    What was wrong: disposing() returned immediately and every later install
    attached another pair, so Stop/Clear/Send accumulated listeners.
    Why: skip a control already tracked, and remove the listeners on dispose.
    """
    if control is None:
        return
    for existing, _mouse, _focus in _leave_query_bindings:
        if existing is control:
            return
    try:
        import unohelper
        from com.sun.star.awt import XFocusListener, XMouseListener
    except ImportError:
        return

    class _LeaveQueryFocus(unohelper.Base, XFocusListener):
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            _release_leave_query_binding(control, mouse_track, self)

        def focusLost(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            return

        def focusGained(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_left_query()
            log.debug("stream focus: sidebar control")

    class _LeaveQueryMouse(unohelper.Base, XMouseListener):
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            _release_leave_query_binding(control, self, focus_track)

        def mousePressed(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_left_query()

        def mouseReleased(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            return

        def mouseEntered(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_left_query()

        def mouseExited(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            return

    mouse_track: Any = None
    focus_track: Any = None
    try:
        if hasattr(control, "addMouseListener"):
            mouse_track = _LeaveQueryMouse()
            control.addMouseListener(mouse_track)
            _stream_focus_trackers.append(mouse_track)
        if hasattr(control, "addFocusListener"):
            focus_track = _LeaveQueryFocus()
            control.addFocusListener(focus_track)
            _stream_focus_trackers.append(focus_track)
        if mouse_track is not None or focus_track is not None:
            _leave_query_bindings.append((control, mouse_track, focus_track))
    except Exception as e:
        log.debug("leave-query listeners: %s", e)


def _release_doc_click_binding(controller: Any, handler: Any) -> None:
    """Drop a document click handler so a closed controller is not pinned."""
    try:
        if controller is not None and hasattr(controller, "removeMouseClickHandler"):
            controller.removeMouseClickHandler(handler)
    except Exception as e:
        log.debug("removeMouseClickHandler: %s", e)
    _doc_click_bindings[:] = [pair for pair in _doc_click_bindings if pair[1] is not handler]
    try:
        _stream_focus_trackers.remove(handler)
    except ValueError:
        pass


def _ensure_document_click_handler(ctx: Any) -> None:
    """Page click on the current document calls ``note_user_left_query``.

    What was wrong: the ``XMouseClickHandler`` was added once, to whichever
    controller was current the first time a sidebar installed, and never
    removed. How it happened: ``install_stream_focus_tracker`` returned as
    soon as ``_stream_focus_trackers`` was non-empty, so a later document
    never subscribed. Why this fixes it: every install attaches a handler to
    the controller that is current now (and skips one that already has it).
    ``disposing`` removes it, so a closed document is not kept alive.
    """
    try:
        import unohelper
        from com.sun.star.awt import XMouseClickHandler
    except ImportError:
        return

    try:
        controller = _current_document_controller(ctx)
        if controller is None or not hasattr(controller, "addMouseClickHandler"):
            return
        for existing, _handler in _doc_click_bindings:
            if uno_same(existing, controller):
                return

        class _DocClick(unohelper.Base, XMouseClickHandler):
            def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
                _release_doc_click_binding(controller, self)

            def mousePressed(self, e: MouseEvent) -> bool:  # noqa: N802 -- UNO signature
                note_user_left_query()
                log.debug("stream focus: document click")
                return False

            def mouseReleased(self, e: MouseEvent) -> bool:  # noqa: N802 -- UNO signature
                return False

        handler = _DocClick()
        controller.addMouseClickHandler(handler)
        _doc_click_bindings.append((controller, handler))
        _stream_focus_trackers.append(handler)
    except Exception as e:
        log.debug("document click handler: %s", e)


def _drop_query_focus_listener(listener: Any) -> None:
    """Forget the query listener once its control is disposed."""
    global _query_focus_listener
    control = None
    kept: list[tuple[Any, Any]] = []
    for ctrl, bound in _query_focus_bindings:
        if bound is listener:
            control = ctrl
            continue
        kept.append((ctrl, bound))
    _query_focus_bindings[:] = kept
    if control is not None:
        try:
            if hasattr(control, "removeFocusListener"):
                control.removeFocusListener(listener)
        except Exception as e:
            log.debug("removeFocusListener: %s", e)
    if listener is not None and _query_focus_listener is listener:
        _query_focus_listener = kept[-1][1] if kept else None
    try:
        _stream_focus_trackers.remove(listener)
    except ValueError:
        pass


def _attach_query_focus_listener(query: Any) -> None:
    """focusGained on this Ask field keeps caret restore. One listener per control."""
    global _query_focus_listener
    if query is None or not hasattr(query, "addFocusListener"):
        return
    for existing, _listener in _query_focus_bindings:
        if existing is query:
            return
    try:
        import unohelper
        from com.sun.star.awt import XFocusListener
    except ImportError:
        return

    class _QueryFocus(unohelper.Base, XFocusListener):
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            _drop_query_focus_listener(self)

        def focusLost(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            return

        def focusGained(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_wants_query()
            log.debug("stream focus: query")

    try:
        q_track = _QueryFocus()
        query.addFocusListener(q_track)
        _stream_focus_trackers.append(q_track)
        _query_focus_bindings.append((query, q_track))
        _query_focus_listener = q_track
    except Exception as e:
        log.debug("query focus listener: %s", e)


def install_stream_focus_tracker(ctx: Any, query: Any = None, leave_query_controls: Any = None) -> None:
    """Query focusGained → keep restoring. Document / sidebar pointer → stop.

    Window focus listeners miss in-frame query→page clicks (same top-level).
    Writer's XUserInputInterception mouse handler sees the page click.
    Sidebar Stop/Clear/other widgets are not on that handler — pass them as
    *leave_query_controls* so stream ``query.setFocus()`` does not steal the
    click (Packet B1).

    What was wrong: a second sidebar replaced the process-wide focus pin and
    returned without a focusGained listener, because the first window's
    listener was still set. Stream chunks then called setFocus on the other
    window's Ask field. Why: attach a listener per query control, and leave
    the pin to set_default_focus_restore. The streaming widget passes its
    own query into restore.
    """
    _attach_query_focus_listener(query)
    for ctrl in leave_query_controls or ():
        if ctrl is not None and ctrl is not query:
            _attach_leave_query_listeners(ctrl)
    _ensure_document_click_handler(ctx)
    log.debug("install_stream_focus_tracker n=%d", len(_stream_focus_trackers))


def _focus_restore_target(explicit: Any = None) -> Any:
    if explicit is not None:
        return explicit
    return _default_focus_restore


@contextmanager
def focus_preserved(ctx: Any, restore: Any = None) -> Generator[None, None, None]:
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
def process_events_to_idle(ctx: Any, rounds: int = 1, force: bool = False) -> bool:
    """Drain the UI event queue *rounds* times via the approved VCL pump chokepoint.

    When a chat/MCP :func:`~plugin.framework.queue_executor.drain_owner_scope` is
    active, skips VCL pumping so secondary progress helpers (grep, Harper status,
    notebook import) cannot nest ``processEventsToIdle`` inside the drain loop.
    Pass force=True (e.g. for RichTextControl caret reveal) to pump VCL even when
    under a drain owner.
    Returns True if at least one VCL pump ran. Blocking secondary waits should
    use :func:`wait_while_pumping` rather than a local PE2I loop.
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


def _post_secondary_idle(ctx: Any) -> None:
    """Enqueue one PE2I tick on the VCL thread. Must not run PE2I on the waiter."""
    from plugin.framework.queue_executor import default_executor, post_to_main_thread

    # What was wrong: each 75ms tick posted another marshal item, so a 15s
    # wait could enqueue ~200 no-op pumps ahead of a real execute_on_main_thread.
    # How it happened: wait_while_pumping called this on every poll whether or
    # not the previous pump was still sitting in the work queue.
    # Why this change: skip while default_executor's queue is non-empty. One
    # outstanding pump is enough. qsize() is not a sticky flag — if post()
    # drops the callback the queue stays empty and the next tick tries again.
    if default_executor._work_queue.qsize() != 0:
        return

    def _pump() -> None:
        # QueueExecutor.post can fall back onto the caller when AsyncCallback
        # is missing. process_events_to_idle is @main_thread_only — skip.
        if not on_main_thread():
            return
        process_events_to_idle(ctx, force=False)

    post_to_main_thread(_pump)


def wait_while_pumping(done: "threading.Event", ctx: Any, *, timeout: float, poll_sec: float = 0.075) -> bool:
    """Wait for *done* while pumping VCL as a secondary caller.

    On the LibreOffice main thread, each tick calls :func:`process_events_to_idle`
    with ``force=False`` so a chat/MCP drain owner suppresses nested VCL.
    Off the main thread (Writer ``doProofreading`` linguistic workers are
    ``Dummy-*``, not VCL) PE2I is **posted** to the main thread — never called
    on the waiter. Calling PE2I on Dummy-21 popped a UNO thread-violation
    dialog every poll tick (the wait loop from #778). Drain-owner wait loops
    must keep using :func:`~plugin.framework.queue_executor.pump_ui_idle` /
    ``run_blocking_in_thread``, not this helper.

    Default *poll_sec* is 75ms (stay inside 50–100ms; same band as the
    linguistic PE2I-in-proofread wait). Returns True if *done* was set, False
    if *timeout* elapsed first. Post/PE2I failures are swallowed so a pump
    miss cannot abort the wait.
    """
    pump_on_caller = on_main_thread()
    deadline = time.monotonic() + max(0.0, timeout)
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            if pump_on_caller:
                process_events_to_idle(ctx, force=False)
            else:
                _post_secondary_idle(ctx)
        except Exception:
            log.debug("wait_while_pumping process_events_to_idle failed", exc_info=True)
        if done.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        done.wait(timeout=min(poll_sec, remaining))
    return True


def _doc_identity_url(url: Any) -> str:
    """Comparison key for resolve-by-URL.

    Repairs ``file:/`` to ``file:///`` before :func:`normalize_doc_url`. The
    identity function itself stays unrepaired so MCP and script keys do not change.
    """
    raw = str(url or "").strip()
    if raw.startswith("file:"):
        from plugin.doc.text_helpers import normalize_file_url

        raw = normalize_file_url(raw)
    return normalize_doc_url(raw)


def normalize_doc_url(url: Any) -> str:
    """Normalize document URL for comparison (strip, optional trailing slash).

    Shared by resolve-by-URL, MCP doc keys, and document-script stale detection.
    Does not repair ``file:/`` vs ``file:///`` — that is ``text_helpers.normalize_file_url``.
    Resolve compares via :func:`_doc_identity_url`, which repairs first.
    """
    if not url:
        return ""
    s = str(url).strip()
    if s.endswith("/") and len(s) > 1:
        s = s[:-1]
    return s


def get_runtime_uid(model: Any) -> str:
    """Stable per-session id for an open component.

    Unlike the document URL, ``RuntimeUID`` exists even for unsaved/untitled
    documents, so it can address a document that has no file on disk yet.
    Returns "" if unavailable.

    Tries ``getRuntimeUID()``, attribute access, and ``getPropertyValue("RuntimeUID")`` in turn
    because LibreOffice builds expose the id through different UNO surfaces. Only plain ``str`` /
    ``int`` values are accepted so auto-mocked UNO attributes (e.g. ``MagicMock.RuntimeUID``)
    cannot masquerade as a real uid.
    """
    for accessor in (lambda m: m.getRuntimeUID() if callable(getattr(m, "getRuntimeUID", None)) else None, lambda m: getattr(m, "RuntimeUID", None), lambda m: m.getPropertyValue("RuntimeUID")):
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


# What was wrong: off-thread, proxy __eq__ raises RuntimeError from
# assert_main_thread, the bare except Exception swallowed it, then uno.isSame
# ran on unwrapped PyUNO. How it happened: the identity ladder treats any
# comparison error as "try the next step", and the guard's RuntimeError is an
# Exception. Why this change: @main_thread_only (same decorator as
# resolve_document_by_url) never enters the ladder off the main thread. The
# on-thread ladder, including unwrap before uno.isSame, stays.
@main_thread_only
def uno_same(a: Any, b: Any) -> bool:
    """True when *a* and *b* are the same underlying UNO object.

    PyUNO often hands out **distinct Python wrappers** for one UNO identity.
    Bare ``is`` / ``==`` / ``!=`` can then miss that a draw shape's
    ``shape.getAnchor().getText()`` is the same header ``XText`` as
    ``style.getPropertyValue("HeaderText")``. That false miss hid logos from
    ``_scan_region_content`` (get/metadata wrong; historically a wipe could
    look "safe").

    This is **not** a requirement of the debug viral UNO thread proxy
    (``_UnoThreadGuardProxy`` in ``thread_guard.py``). That proxy is a
    separate GUARD_ON tool; release OXTs stub it off. The flaky identity is a
    LibreOffice / PyUNO wrapper issue and exists with the proxy stripped.

    It still works when proxying is on: ``_UnoThreadGuardProxy.__eq__``
    unwraps ``_target`` and compares ``self._target == _unwrap_uno(other)``
    (see ``thread_guard.py``), so step 2 (``==``) succeeds for
    proxy↔unwrapped. ``uno.isSame`` is a UNO/C++ identity test and must see
    real PyUNO objects, so step 3 unwraps via ``_unwrap_uno`` first.

    Ladder (a false miss is still wrong for get/metadata, and was the
    disaster when wipe used this scan as a refuse gate):

    1. ``a is b``
    2. try ``a == b`` (covers viral-proxy ``__eq__`` unwrap when GUARD_ON)
    3. try ``uno.isSame`` on unwrapped objects when the function exists
       (not all LibreOffice Python-UNO builds ship it; same fallback as
       ``_page_index_for`` historically)
    4. else False
    """
    if a is b:
        return True
    try:
        if a == b:
            return True
    except Exception:
        pass
    try:
        import uno

        is_same = getattr(uno, "isSame", None)
        if not callable(is_same):
            return False
        from plugin.framework.thread_guard import _unwrap_uno

        # ``is True``: mocked ``uno.isSame`` (unit tests) returns a MagicMock,
        # which is truthy. Real PyUNO returns a bool.
        return is_same(_unwrap_uno(a), _unwrap_uno(b)) is True
    except Exception:
        return False


@main_thread_only
def resolve_document_by_url(ctx: Any, url: Any) -> tuple[Any, str | None]:
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

    target = _doc_identity_url(url)
    try:
        desktop = get_desktop(ctx)
        if desktop is None:
            return (None, None)
        comps = desktop.getComponents()
        if not comps:
            return (None, None)
        enum = comps.createEnumeration()
        if not enum:
            return (None, None)
        # Real UNO hasMoreElements() is bool. A MagicMock is always truthy,
        # so ``while enum.hasMoreElements()`` spun the main thread in pytest.
        # Same guard as get_open_documents.
        while enum is not None:
            try:
                more = enum.hasMoreElements()
            except Exception:
                break
            if more is not True and more != 1:
                break
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
                    doc_url = _doc_identity_url(model.getURL()) if hasattr(model, "getURL") else ""
                    uid = get_runtime_uid(model)
                    if (doc_url and doc_url == target) or (uid and uid == target):
                        doc_type_enum = _doc_type.get_document_type(model)
                        doc_type = _doc_type.doc_type_label_for_enum(doc_type_enum, impress_as_draw=True)
                        return (_guard_returned_uno(model), doc_type)
            except Exception as e:
                # One dead window must not hide the rest of the desktop.
                # Disposal of the enumeration itself is the outer handler.
                logging.getLogger(__name__).debug("resolve_document_by_url element error: %s", type(e).__name__)
                continue
    except DocumentDisposedError:
        raise
    except Exception as e:
        _reraise_document_disposed(e, "Desktop")
        logging.getLogger(__name__).exception("resolve_document_by_url enumeration error")
    return (None, None)


@main_thread_only
def get_document_from_frame(frame: Any) -> Any:
    """Get the document model strictly from the frame controller.

    This is the preferred path for sidebar panels to ensure we resolve
    the document bound to the active window rather than relying on Desktop.
    """
    if not frame:
        return None
    from plugin.framework.errors import suppress_disposed

    with suppress_disposed("resolve document from frame", logger=logging.getLogger(__name__)):
        check_disposed(frame, "Frame")
        controller = frame.getController()
        if not controller:
            return None
        check_disposed(controller, "Controller")
        model = controller.getModel()
        if model is not None:
            return _guard_returned_uno(model)
    return None
