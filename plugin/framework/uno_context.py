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
from typing import TYPE_CHECKING, Any, Generator, cast

if TYPE_CHECKING:
    import threading

    from com.sun.star.awt import FocusEvent, MouseEvent
    from com.sun.star.lang import EventObject

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

# uno.bin / unopkg register helpers have no VCL. Creating Desktop there SEGVs
# (issue #768). pythonloader often rewrites sys.argv, so also read /proc.
_UNO_HELPER_BASENAMES = frozenset({
    "uno",
    "uno.bin",
    "uno.exe",
    "unopkg",
    "unopkg.bin",
    "unopkg.com",
    "unopkg.exe",
})


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
    return _wrap_uno(desktop)


@main_thread_only
def get_active_document(ctx: Any | None = None) -> Any:
    """Return the currently active document model."""
    try:
        desktop = get_desktop(ctx)
        if desktop is None:
            return None
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
def get_package_info(ctx: Any | None = None) -> Any:
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
    return (
        os.path.join(root, "assets", clean),
        os.path.join(root, "extension", "assets", clean),
    )


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


def set_default_focus_restore(control: Any) -> None:
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


def _attach_leave_query_listeners(control: Any) -> None:
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
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def focusLost(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            return

        def focusGained(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_left_query()
            log.debug("stream focus: sidebar control")

    class _LeaveQueryMouse(unohelper.Base, XMouseListener):
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def mousePressed(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_left_query()

        def mouseReleased(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            return

        def mouseEntered(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_left_query()

        def mouseExited(self, e: MouseEvent) -> None:  # noqa: N802 -- UNO signature
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


def install_stream_focus_tracker(ctx: Any, query: Any = None, rich: Any = None, leave_query_controls: Any = None) -> None:
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
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def focusLost(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            return

        def focusGained(self, e: FocusEvent) -> None:  # noqa: N802 -- UNO signature
            note_user_wants_query()
            log.debug("stream focus: query")

    class _DocClick(unohelper.Base, XMouseClickHandler):
        def disposing(self, Source: EventObject) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def mousePressed(self, e: MouseEvent) -> bool:  # noqa: N802 -- UNO signature
            note_user_left_query()
            log.debug("stream focus: document click")
            return False

        def mouseReleased(self, e: MouseEvent) -> bool:  # noqa: N802 -- UNO signature
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
    from plugin.framework.queue_executor import post_to_main_thread

    def _pump() -> None:
        # QueueExecutor.post can fall back onto the caller when AsyncCallback
        # is missing. process_events_to_idle is @main_thread_only — skip.
        if not on_main_thread():
            return
        process_events_to_idle(ctx, force=False)

    post_to_main_thread(_pump)


def wait_while_pumping(
    done: "threading.Event",
    ctx: Any,
    *,
    timeout: float,
    poll_sec: float = 0.075,
) -> bool:
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


def normalize_doc_url(url: Any) -> str:
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
def get_document_from_frame(frame: Any) -> Any:
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
