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
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.framework.constants import EXTENSION_ID_LIBREHARPER, EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT
from plugin.framework.thread_guard import main_thread_only, on_main_thread

log = logging.getLogger("writeragent.context")

_fallback_ctx = None
# id(target) -> (target, proxy). Holding the target keeps the id from being reused.
_component_context_proxies: dict[int, tuple[Any, Any]] = {}
_logged_component_context_fallback = False
# Set by main.py / main_core.py bootstrap; auto-detected from installed packages when unset.
_package_extension_id: str | None = None

# Probe order: LibrePy first when both family OXTs are installed (existing behavior).
_KNOWN_EXTENSION_IDS = (EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT, EXTENSION_ID_LIBREHARPER)

_is_libreharper_cache: bool | None = None

# Process image does not change. None means not computed yet (False is a real answer).
_desktop_create_unsafe: bool | None = None

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


def reset_desktop_create_is_unsafe_for_tests() -> None:
    """Drop the cached no-VCL answer.

    ``desktop_create_is_unsafe`` reads argv and ``/proc`` once. Tests patch
    those inputs and must clear the cache or they see the previous process.
    """
    global _desktop_create_unsafe
    _desktop_create_unsafe = None


def _desktop_create_is_unsafe_now() -> bool:
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


def desktop_create_is_unsafe() -> bool:
    """True in uno.bin / unopkg helpers that have no VCL.

    ``createInstanceWithContext("com.sun.star.frame.Desktop")`` and
    ``getValueByName(theDesktop)`` on that ctx take SolarMutexGuard →
    GetYieldMutex and SEGV (issue #768). GUI soffice already has Desktop.

    Do not trust ``sys.argv`` alone: pythonloader inside
    ``uno.bin --singleaccept`` often leaves argv as ``['']`` or a .py path.

    What was wrong: ``get_desktop`` called this on every lookup, and each
    call re-read ``/proc/self/exe``, ``comm``, and ``cmdline``. How: nothing
    remembered the first answer. Why: the process image does not change, so
    the first result is cached. Tests that patch argv or
    ``_linux_process_tokens`` call ``reset_desktop_create_is_unsafe_for_tests``.
    """
    global _desktop_create_unsafe
    if _desktop_create_unsafe is not None:
        return _desktop_create_unsafe
    _desktop_create_unsafe = _desktop_create_is_unsafe_now()
    return _desktop_create_unsafe


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
    if is_libreharper():
        return "LibreHarper"
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


def _stable_component_context(ctx: Any) -> Any:
    """Return one object for this component context.

    What was wrong: every ``get_ctx()`` call ran ``_wrap_uno``, which builds
    a new ``_UnoThreadGuardProxy``. Under GUARD_ON, ``get_ctx() is get_ctx()``
    was False. The release stub returns the raw object, so identity holds.
    How: the bootstrap context is one long-lived PyUNO object and the proxy
    was not remembered.
    Why: a context that is already a guard proxy is returned as that object.
    Otherwise one proxy is cached per target. Mocks and guard-off returns
    stay the raw object, which is already stable. QueueExecutor still
    unwraps before it stores a context; that compare is on the raw target.
    """
    from plugin.framework.thread_guard import _UnoThreadGuardProxy

    if ctx is None or isinstance(ctx, _UnoThreadGuardProxy):
        return ctx
    slot = _component_context_proxies.get(id(ctx))
    if slot is not None and slot[0] is ctx:
        return slot[1]
    wrapped = _guard_returned_uno(ctx)
    if isinstance(wrapped, _UnoThreadGuardProxy):
        _component_context_proxies[id(ctx)] = (ctx, wrapped)
    return wrapped


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
        return _stable_component_context(_fallback_ctx)
    try:
        import uno

        if hasattr(uno, "getComponentContext"):
            ctx = uno.getComponentContext()
            if ctx is not None:
                # Bootstrap-less unit tests still need this branch. Log once:
                # a non-extension context can lack VCL and segfault on Desktop.
                global _logged_component_context_fallback
                if not _logged_component_context_fallback:
                    _logged_component_context_fallback = True
                    log.error(
                        "get_ctx: no extension fallback; using uno.getComponentContext() "
                        "(set_fallback_ctx was not called)"
                    )
                return _stable_component_context(ctx)
    except ImportError:
        pass
    return _stable_component_context(_fallback_ctx)


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
    if ctx is None:
        return None
    ctx_any = cast("Any", ctx)
    smgr = get_service_manager(ctx_any)
    if smgr is None:
        return None
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
    # What was wrong: a failed clear still returned the scratch Writer, so the
    # default-template text this function exists to drop was handed to the
    # caller. How: clear_writer_body logs and returns False on a non-disposal
    # error, and this ignored that. Why: an already-empty body is False too,
    # so only a leftover non-empty string is a failure. Disposal still raises.
    if not clear_writer_body(doc):
        try:
            leftover = doc.getText().getString()
        except Exception as e:
            _reraise_document_disposed(e, "Writer")
            log.debug("new_blank_writer: body unreadable after clear", exc_info=True)
            return None
        if (leftover or "").strip():
            log.debug("new_blank_writer: default template text survived clear_writer_body")
            return None
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


# What was wrong: clear_writer_body is a public UNO entry and touched the
# document with no thread check. A raw model reached PyUNO off the main
# thread. How: sibling getters use @main_thread_only and this helper did not.
# Why: the decorator raises before any attribute access when the guard is on.
# In-tree callers already pass a guarded doc on the main thread; the
# decorator does not unwrap that argument.
@main_thread_only
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
    # A vnd.sun.star.extension:// URL is not a filesystem path. Callers join
    # this with os.path; returning the URL made that join look like a file.
    return ""


@main_thread_only
def get_toolkit(ctx: Any | None = None) -> Any:
    """Safely retrieve the com.sun.star.awt.Toolkit service."""
    ctx = ctx or get_ctx()
    if ctx is None:
        return None
    try:
        ctx_any = cast("Any", ctx)
        smgr = get_service_manager(ctx_any)
        if smgr is None:
            return None
        tk = cast("Any", smgr).createInstanceWithContext("com.sun.star.awt.Toolkit", ctx_any)
        return _guard_returned_uno(tk)
    except Exception:
        log.exception("Failed to create toolkit")
        return None


@contextmanager
def focus_preserved(ctx: Any, restore: Any = None) -> Generator[None, None, None]:
    """Restore focus after a block that may steal it (RichTextControl reveal).

    *restore* is the query field of the panel that is running this block.
    There is no process-wide pin: a second window's stream must not call
    ``setFocus`` here. When *restore* is omitted, the toolkit focus window
    at entry is restored — which is the Send button after a click, so
    callers that own an Ask field pass it.
    """
    saved = restore
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


# One in-flight secondary-idle post. ``_SECONDARY_IDLE_RESERVING`` covers the
# window inside ``post_to_main_thread`` before the callable is visible on the
# queue. The stored callable is cleared on the next tick once it is no longer
# scheduled, so a drop is not sticky.
_SECONDARY_IDLE_RESERVING = object()
_secondary_idle_lock = threading.Lock()
_secondary_idle_posted: object | None = None


def _post_secondary_idle(ctx: Any) -> None:
    """Enqueue one PE2I tick on the VCL thread. Must not run PE2I on the waiter."""
    global _secondary_idle_posted
    from plugin.framework.queue_executor import default_executor, post_to_main_thread

    def _pump() -> None:
        # QueueExecutor.post can fall back onto the caller when AsyncCallback
        # is missing. process_events_to_idle is @main_thread_only — skip.
        if not on_main_thread():
            return
        process_events_to_idle(ctx, force=False)

    with _secondary_idle_lock:
        posted = _secondary_idle_posted
        if posted is _SECONDARY_IDLE_RESERVING:
            return
        if posted is not None and default_executor.callable_is_scheduled(posted):
            return
        # What was wrong: each 75ms tick could enqueue another no-op pump, and
        # the guard for that skipped the post whenever ``pending_work_count()``
        # was non-zero. How: that count is the whole process-wide marshal
        # queue. A leftover item from another test (pytest-xdist) or unrelated
        # UI work looked like "our pump is already queued", so a Dummy-*
        # linguistic wait never posted. The lint then ran out its own timeout
        # (CI: ``posts["n"] == 0``, slow result elapsed_ms=2000). Why: coalesce
        # only this pump. ``post`` dropping the callable, or a test double that
        # does not enqueue it, leaves nothing scheduled, so the next tick tries
        # again.
        _secondary_idle_posted = _SECONDARY_IDLE_RESERVING

    try:
        post_to_main_thread(_pump)
    except Exception:
        with _secondary_idle_lock:
            if _secondary_idle_posted is _SECONDARY_IDLE_RESERVING:
                _secondary_idle_posted = None
        raise

    with _secondary_idle_lock:
        if _secondary_idle_posted is not _SECONDARY_IDLE_RESERVING:
            return
        if default_executor.callable_is_scheduled(_pump):
            _secondary_idle_posted = _pump
        else:
            _secondary_idle_posted = None


def wait_while_pumping(done: "threading.Event", ctx: Any, *, timeout: float, poll_sec: float = 0.075) -> bool:
    """Wait for *done* while pumping VCL as a secondary caller.

    On the LibreOffice main thread, each tick calls :func:`process_events_to_idle`
    with ``force=False`` so a chat/MCP drain owner suppresses nested VCL.
    Off the main thread (Writer ``doProofreading`` linguistic workers are
    ``Dummy-*``, not VCL) PE2I is **posted** to the main thread — never called
    on the waiter. Calling PE2I on Dummy-21 popped a UNO thread-violation
    dialog every poll tick (the wait loop from #778). Repeated off-main ticks
    coalesce to one outstanding secondary-idle pump; other marshal items do
    not count. Drain-owner wait loops must keep using
    :func:`~plugin.framework.queue_executor.pump_ui_idle` /
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


def _read_runtime_uid(model: Any) -> str:
    """RuntimeUID ladder with no thread check.

    File Open ``XFilter.filter`` runs on Dummy-2 (detect reload on Dummy-3),
    not ``threading.main_thread()``. Notebook ``_doc_key`` must use this so
    the guard on ``get_runtime_uid`` does not make ``filter()`` return False.
    Same acceptance rules as ``get_runtime_uid``: plain ``str`` / ``int`` only.
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


@main_thread_only
def get_runtime_uid(model: Any) -> str:
    """Stable per-session id for an open component.

    Unlike the document URL, ``RuntimeUID`` exists even for unsaved/untitled
    documents, so it can address a document that has no file on disk yet.
    Returns "" if unavailable.

    Tries ``getRuntimeUID()``, attribute access, and ``getPropertyValue("RuntimeUID")`` in turn
    because LibreOffice builds expose the id through different UNO surfaces. Only plain ``str`` /
    ``int`` values are accepted so auto-mocked UNO attributes (e.g. ``MagicMock.RuntimeUID``)
    cannot masquerade as a real uid.

    What was wrong: every accessor sat in ``except Exception``, so an
    off-thread call swallowed ``assert_main_thread``'s ``RuntimeError`` and
    returned ``""`` (an untitled document with no id). How: the same ladder
    ``uno_same`` used before it was decorated. Why: ``@main_thread_only``
    raises before the loop when the guard is on. On-thread disposal still
    returns ``""``. Callers that LibreOffice invokes on Dummy-N (notebook
    File Open) use ``_read_runtime_uid`` instead of this guard.
    """
    return _read_runtime_uid(model)


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
        # What was wrong: ``while enum is not None`` never ended the loop.
        # How: nothing in the body assigns ``enum = None``; the exits are
        # ``break``. Why: ``while True`` matches those breaks.
        # Same ceiling as the paragraph walks in html_import and format.
        # Open desktops are far smaller; the cap only matters when
        # hasMoreElements() never goes false.
        walk_limit = 200000
        seen = 0
        while True:
            try:
                more = enum.hasMoreElements()
            except Exception as e:
                # What was wrong: a disposed desktop enumeration broke the
                # loop and the caller was told the document was not open.
                # How: this except swallowed DisposedException before the
                # outer handler could re-raise it. Why: one dead window
                # still continues below; disposal of the enumeration does not.
                _reraise_document_disposed(e, "Desktop")
                break
            if more is not True and more != 1:
                break
            seen += 1
            if seen > walk_limit:
                log.debug("resolve_document_by_url stopped at walk cap")
                break
            try:
                elem = enum.nextElement()
            except Exception as e:
                # What was wrong: this failure ``continue``d while
                # hasMoreElements() stayed true. How: UNO does not always
                # advance the enumeration when nextElement fails, so the
                # loop never saw a false hasMoreElements and froze the
                # main thread. Why: stop, as html_import and format do on
                # a failed nextElement. A model fetched successfully that
                # then raises is still skipped below.
                log.debug("resolve_document_by_url nextElement error: %s", type(e).__name__)
                break
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
                log.debug("resolve_document_by_url element error: %s", type(e).__name__)
                continue
    except DocumentDisposedError:
        raise
    except Exception as e:
        _reraise_document_disposed(e, "Desktop")
        log.exception("resolve_document_by_url enumeration error")
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

    with suppress_disposed("resolve document from frame", logger=log):
        check_disposed(frame, "Frame")
        controller = frame.getController()
        if not controller:
            return None
        check_disposed(controller, "Controller")
        model = controller.getModel()
        if model is not None:
            return _guard_returned_uno(model)
    return None
