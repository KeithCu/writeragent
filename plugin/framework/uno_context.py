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
``guard_uno`` and marshal UI work through ``MainThread``.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.parse
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.framework.constants import (
    EXTENSION_ID_LIBREHARPER,
    EXTENSION_ID_LIBREPY,
    EXTENSION_ID_WRITERAGENT,
)
from plugin.framework.errors import (
    _uno_disposal_message,
    reraise_if_disposed,
    suppress_disposed,
    uno_call,
)
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.url_utils import normalize_file_url
from plugin.framework.vcl_pumping import (
    focus_preserved as focus_preserved,
    process_events_to_idle as process_events_to_idle,
    wait_while_pumping as wait_while_pumping,
)

log = logging.getLogger("writeragent.context")

_fallback_ctx = None
# id(target) -> (target, proxy). Holding the target keeps the id from being reused.
_component_context_proxies: dict[int, tuple[Any, Any]] = {}
_logged_component_context_fallback = False
# Set by main.py / main_core.py bootstrap; auto-detected from installed packages when unset.
_package_extension_id: str | None = None

# Probe order: LibrePy first when both family OXTs are installed (existing behavior).
_KNOWN_EXTENSION_IDS = (EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT, EXTENSION_ID_LIBREHARPER)

# Process image does not change. None means not computed yet (False is a real answer).
_desktop_create_unsafe: bool | None = None

# uno.bin / unopkg register helpers have no VCL. Creating Desktop there SEGVs
# (issue #768). pythonloader often rewrites sys.argv, so also read /proc.
_UNO_HELPER_BASENAMES = frozenset({"uno", "uno.bin", "uno.exe", "unopkg", "unopkg.bin", "unopkg.com", "unopkg.exe"})


def _basename_is_uno_helper(name: str) -> bool:
    return os.path.basename(name).strip().lower() in _UNO_HELPER_BASENAMES


def _linux_process_tokens() -> tuple[str, str, list[str]]:
    """Real process image and args. pythonloader may rewrite ``sys.argv`` (#768)."""
    exe = ""
    comm = ""
    cmdline: list[str] = []
    try:
        exe = os.readlink("/proc/self/exe")
    except OSError:
        pass
    try:
        with open("/proc/self/comm", encoding="utf-8") as comm_file:
            comm = comm_file.read().strip()
    except OSError:
        pass
    try:
        with open("/proc/self/cmdline", "rb") as cmdline_file:
            raw = cmdline_file.read().split(b"\0")
        cmdline.extend(part.decode("utf-8", "replace") for part in raw if part)
    except OSError:
        pass
    return exe, comm, cmdline


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
    exe_sys = getattr(sys, "executable", "") or ""
    if exe_sys and _basename_is_uno_helper(exe_sys):
        return True
    exe, comm, cmdline = _linux_process_tokens()
    # We match the exe, comm and cmdline[0] only, not every cmdline token,
    # because a soffice argument such as /home/u/uno would otherwise mark the
    # whole session as a no-VCL helper and disable Desktop.
    if exe and _basename_is_uno_helper(exe):
        return True
    if comm and _basename_is_uno_helper(comm):
        return True
    if cmdline and _basename_is_uno_helper(cmdline[0]):
        return True
    return False


def desktop_create_is_unsafe() -> bool:
    """True in uno.bin / unopkg helpers that have no VCL.

    Do not trust ``sys.argv`` alone: pythonloader inside
    ``uno.bin --singleaccept`` often leaves argv as ``['']`` or a .py path.

    The process image does not change, so the first result is cached.
    ``get_desktop`` calls this on every lookup; re-reading ``/proc/self/exe``,
    ``comm``, and ``cmdline`` each time is the same answer. Tests that patch
    argv or ``_linux_process_tokens`` call
    ``reset_desktop_create_is_unsafe_for_tests``.
    """
    global _desktop_create_unsafe
    if _desktop_create_unsafe is not None:
        return _desktop_create_unsafe
    _desktop_create_unsafe = _desktop_create_is_unsafe_now()
    return _desktop_create_unsafe


def is_libreharper() -> bool:
    """Return True if running under the LibreHarper extension."""
    if _package_extension_id is not None:
        return _package_extension_id == EXTENSION_ID_LIBREHARPER
    try:
        from plugin import _manifest

        return any(m.get("title") == "LibreHarper" for m in getattr(_manifest, "MODULES", []))
    except ImportError:
        return False


def set_fallback_ctx(ctx: Any) -> Any | None:
    """Store a fallback ctx for use when uno module is not available.

    Returns the previous fallback ctx so callers can save and restore it.
    """
    global _fallback_ctx
    prev = _fallback_ctx
    _fallback_ctx = ctx
    return prev


def set_package_extension_id(extension_id: str) -> None:
    """Pin the OXT package id used by get_extension_url() (LibrePy vs WriterAgent)."""
    global _package_extension_id
    _package_extension_id = extension_id


def reset_package_extension_id_for_tests() -> None:
    """Clear cached extension id (unit tests only)."""
    global _package_extension_id
    _package_extension_id = None


def resolve_package_extension_id(ctx: Any | None = None) -> str:
    """Return the installed WriterAgent-family extension id (LibrePy or WriterAgent).

    Cache is pinned at bootstrap (``set_package_extension_id``).
    ``get_package_info`` is main-thread only, so off-main without a cache
    returns the WriterAgent default (same as the last-resort below).
    """
    global _package_extension_id
    if _package_extension_id:
        return _package_extension_id

    if not main.is_current():
        return EXTENSION_ID_WRITERAGENT

    pip = get_package_info(ctx)
    if pip is not None:
        for extension_id in _KNOWN_EXTENSION_IDS:
            location = pip.getPackageLocation(extension_id)
            if location:
                _package_extension_id = extension_id
                return extension_id

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

    Import ``guard_uno`` here, the same pattern as
    ``get_document_from_frame``. A module-level ``_wrap_uno`` copied in at
    import is a bound function object; patching
    ``plugin.framework.thread_guard.guard_uno`` never sees those returns
    from ``get_ctx``, ``get_desktop``, ``get_active_document``,
    ``get_package_info``, ``get_toolkit``, and ``resolve_document_by_url``.
    PropertyValue media descriptors are not passed through this helper.
    """
    from plugin.framework.thread_guard import guard_uno

    return guard_uno(obj)


def _stable_component_context(ctx: Any) -> Any:
    """Return one object for this component context.

    A context that is already a guard proxy is returned as that object.
    Otherwise one proxy is cached per target. ``_wrap_uno`` builds a new
    ``_UnoThreadGuardProxy`` on every call, so under GUARD_ON
    ``get_ctx() is get_ctx()`` would be False. The release stub returns the
    raw object, so identity holds. The bootstrap context is one long-lived
    PyUNO object. Mocks and guard-off returns stay the raw object, which
    is already stable. MainThread still unwraps before it stores a
    context; that compare is on the raw target.
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
    # In standalone runner processes (test runners), uno.getComponentContext()
    # returns a local pyuno context with no VCL. Creating
    # com.sun.star.frame.Desktop on that context segfaults. Prefer
    # _fallback_ctx, the remote connection stored at extension init.
    if _fallback_ctx is not None:
        return _stable_component_context(_fallback_ctx)
    try:
        import uno

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
    return None


def resolve_uno_context(ctx: Any) -> Any:
    """Resolve the UNO component context from the tool context or fallback."""
    uno_ctx = getattr(ctx, "ctx", None)
    if uno_ctx is not None:
        return uno_ctx
    return get_ctx()


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


def _create_service(ctx: Any | None, service_name: str) -> Any:
    if ctx is None:
        ctx = get_ctx()
    if ctx is None:
        return None
    smgr = get_service_manager(ctx)
    if smgr is None:
        return None
    return _guard_returned_uno(smgr.createInstanceWithContext(service_name, ctx))


@main_thread_only
def get_desktop(ctx: Any | None = None) -> Any:
    """Return the UNO Desktop instance, or None when creating it would SEGV.

    uno.bin / unopkg register helpers have no VCL. ``createInstanceWithContext("com.sun.star.frame.Desktop")`` and
    ``getValueByName(theDesktop)`` on that ctx take SolarMutexGuard → GetYieldMutex and SEGV (issue #768). GUI
    soffice already has Desktop.
    """
    if desktop_create_is_unsafe():
        log.debug("get_desktop skipped: no-VCL helper process (issue #768)")
        return None
    return _create_service(ctx, "com.sun.star.frame.Desktop")


@main_thread_only
def get_active_document(ctx: Any | None = None) -> Any:
    """Return the currently active document model."""
    try:
        desktop = get_desktop(ctx)
        if desktop is None:
            return None
        doc = uno_call(desktop.getCurrentComponent, "Desktop component resolution")
        return _guard_returned_uno(doc)
    except Exception as e:
        # DisposedException from get_desktop() is a plain Exception, so a
        # dying desktop looks like nothing open. uno_call already re-raises
        # disposal from getCurrentComponent.
        reraise_if_disposed(e, _uno_disposal_message(e), object_type="Desktop")
        log.exception("get_active_document unexpected exception")
        return None


@main_thread_only
def get_package_info(ctx: Any | None = None) -> Any:
    """Return the PackageInformationProvider singleton."""
    if ctx is None:
        ctx = get_ctx()
    if ctx is None:
        return None
    ctx_any = cast("Any", ctx)
    gvn = getattr(ctx_any, "getValueByName", None)
    if gvn is None:
        return None
    pip = gvn("/singletons/com.sun.star.deployment.PackageInformationProvider")
    return _guard_returned_uno(pip)


@main_thread_only
def get_extension_url(ctx: Any | None = None, extension_id: str | None = None) -> str:
    """Return the base URL of the extension package, or "" on failure.

    Return "" when there is no provider or the location is empty, not a
    synthetic ``vnd.sun.star.extension://<id>`` URL.
    """
    if extension_id is None:
        extension_id = resolve_package_extension_id(ctx)
    pip = get_package_info(ctx)
    if pip is None:
        log.debug("get_extension_url(%s) failed: no PackageInformationProvider", extension_id)
        return ""
    location = pip.getPackageLocation(extension_id)
    if location:
        return location
    log.debug("get_extension_url(%s) failed: empty package location", extension_id)
    return ""


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
    """Return the ``com.sun.star.awt.Toolkit`` service, or None when ctx or ServiceManager is missing."""
    return _create_service(ctx, "com.sun.star.awt.Toolkit")


def _doc_identity_url(url: Any) -> str:
    """Comparison key for resolve-by-URL.

    Repairs ``file:/`` to ``file:///`` and re-encodes a ``file:`` URL one way before
    :func:`normalize_doc_url`, so LibreOffice's ``Report%20(1).odt`` equals
    ``path_to_file_url``'s ``Report%20%281%29.odt``. :func:`normalize_doc_url` itself
    stays unrepaired so MCP and script keys do not change.
    """
    raw = str(url or "").strip()
    if raw.startswith("file:"):
        raw = urllib.parse.quote(urllib.parse.unquote_to_bytes(normalize_file_url(raw)))
    return normalize_doc_url(raw)


def _file_stat(url: Any) -> os.stat_result | None:
    """Stat of the existing file a ``file:`` URL names, following symlinks, else None.

    st_ino 0 means the OS gave no file identity (Python on Windows when it cannot open the
    file), and two such files would compare equal.
    """
    raw = str(url or "").strip()
    if not raw.startswith("file:"):
        return None
    import uno  # unit-test and child processes import this module without LibreOffice

    try:
        st = os.stat(uno.fileUrlToSystemPath(normalize_file_url(raw)))
    except Exception:
        # Missing or unreachable file, or a URL LibreOffice cannot convert.
        return None
    return st if st.st_ino else None


def normalize_doc_url(url: Any) -> str:
    """Normalize document URL for comparison (strip, optional trailing slash).

    Shared by resolve-by-URL, MCP doc keys, and document-script stale detection.
    Does not repair ``file:/`` vs ``file:///`` — that is ``url_utils.normalize_file_url``.
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
            log.debug("runtime uid accessor failed", exc_info=True)
            continue
    return ""


# @main_thread_only (same decorator as resolve_document_by_url) never
# enters the ladder off the main thread. Off-thread, proxy __eq__ raises
# RuntimeError from assert_main_thread. A bare except Exception swallows
# that and then uno.isSame runs on unwrapped PyUNO: the identity ladder
# treats any comparison error as "try the next step", and the guard's
# RuntimeError is an Exception. The on-thread ladder, including unwrap
# before uno.isSame, stays.
@main_thread_only
def uno_same(a: Any, b: Any) -> bool:
    """True when *a* and *b* are the same underlying UNO object.

    PyUNO often hands out **distinct Python wrappers** for one UNO identity.
    Bare ``is`` / ``==`` / ``!=`` can then miss that a draw shape's
    ``shape.getAnchor().getText()`` is the same header ``XText`` as
    ``style.getPropertyValue("HeaderText")``.

    This is **not** a requirement of the debug viral UNO thread proxy
    (``_UnoThreadGuardProxy`` in ``thread_guard.py``). That proxy is a
    separate GUARD_ON tool; release OXTs stub it off. The flaky identity is a
    LibreOffice / PyUNO wrapper issue and exists with the proxy stripped.

    It still works when proxying is on: ``_UnoThreadGuardProxy.__eq__``
    unwraps ``_target`` and compares ``self._target == unwrap_uno(other)``
    (see ``thread_guard.py``), so step 2 (``==``) succeeds for
    proxy↔unwrapped. ``uno.isSame`` is a UNO/C++ identity test and must see
    real PyUNO objects, so step 3 unwraps via ``unwrap_uno`` first.

    Ladder:

    1. ``a is b``
    2. try ``a == b`` (covers viral-proxy ``__eq__`` unwrap when GUARD_ON)
    3. try ``uno.isSame`` on unwrapped objects when the function exists
       (not all LibreOffice Python-UNO builds ship it)
    4. else False
    """
    if a is b:
        return True
    try:
        if a == b:
            return True
    except Exception:
        log.debug("uno_same equality probe failed", exc_info=True)
    try:
        import uno

        is_same = getattr(uno, "isSame", None)
        if not callable(is_same):
            return False
        from plugin.framework.thread_guard import unwrap_uno

        # ``is True``: mocked ``uno.isSame`` (unit tests) returns a MagicMock,
        # which is truthy. Real PyUNO returns a bool.
        return is_same(unwrap_uno(a), unwrap_uno(b)) is True
    except Exception:
        return False


def enum_has_more(enum: Any) -> bool:
    """True when a UNO XEnumeration reports more elements.

    PyUNO maps ``hasMoreElements()`` sal_Bool to Python ``bool`` (``True`` /
    ``False``), not integer ``1``. The ``is True`` check stops pytest
    ``MagicMock`` enums whose default return is another truthy mock.
    """
    return enum.hasMoreElements() is True


def iter_open_models(desktop: Any, *, strict: bool = True, label: str = "iter_open_models") -> Generator[Any, None, None]:
    """Iterate open document models from desktop components enumeration.

    LibreOffice's desktop enumeration is a finite snapshot
    (``OComponentAccess::createEnumeration``, ``OComponentEnumeration::nextElement``
    in ``framework/source/helper``), so the walk needs no limit. A failed
    ``nextElement`` ends the walk.

    Handles frame controller unwrapping and guards against truthy mock loops.
    Registered scratch documents (see ``plugin.framework.scratch_writer``) are
    skipped: the user did not open them, so they must not be a lookup target,
    count as an open file, or make a single match ambiguous.
    An element that fails to resolve is skipped; an enumeration failure ends
    the walk. With *strict* a real disposal is re-raised; without it the walk
    also skips models whose ``getCurrentController`` raises.
    """
    if desktop is None:
        return
    # Breaks uno_context <-> scratch_writer import cycle.
    from plugin.framework.scratch_writer import is_scratch_document

    try:
        comps = desktop.getComponents()
        if comps is None:
            return
        enum = comps.createEnumeration()
        if enum is None:
            return
        while enum_has_more(enum):
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
                if model is not None and is_scratch_document(model):
                    continue
                if model is not None and not strict:
                    getattr(model, "getCurrentController", lambda: None)()
            except Exception as e:
                log.debug("%s element error: %s", label, type(e).__name__)
                continue
            if model is not None:
                yield model
    except Exception as e:
        if strict:
            reraise_if_disposed(e, _uno_disposal_message(e), object_type="Desktop")
        return


@main_thread_only
def resolve_document_by_url(ctx: Any, url: Any) -> tuple[Any, str | None]:
    """Resolve an open document by URL or RuntimeUID (registered scratch documents excluded). Must be called on the UNO main thread.

    ``url`` may be a document URL or a ``RuntimeUID`` (as returned by
    ``document_list_open``); the RuntimeUID also matches unsaved/untitled
    documents that have no URL yet. A ``file:`` URL also matches an open
    document that is the same file on disk, and an exact URL or uid match wins.
    Returns (doc, doc_type) or (None, None) if not found.
    doc_type is one of 'writer', 'calc', 'draw', or 'unknown'.
    """
    if not url or not str(url).strip():
        return (None, None)
    from plugin.framework import doc_type as _doc_type

    def resolved(model: Any) -> tuple[Any, str]:
        return (_guard_returned_uno(model), _doc_type.doc_type_family_label(_doc_type.get_document_type(model)))

    target = _doc_identity_url(url)
    try:
        desktop = get_desktop(ctx)
        if desktop is None:
            return (None, None)
        others: list[tuple[Any, Any]] = []
        for model in iter_open_models(desktop):
            try:
                model_url = model.getURL() if hasattr(model, "getURL") else ""
                doc_url = _doc_identity_url(model_url)
                uid = get_runtime_uid(model)
                if (doc_url and doc_url == target) or (uid and uid == target):
                    return resolved(model)
                others.append((model, model_url))
            except Exception as e:
                # One dead window must not hide the rest of the desktop.
                log.debug("resolve_document_by_url element error: %s", type(e).__name__)
                continue
        # A symlink, a hard link or different letter case can name an open file with another
        # URL. LibreOffice writes through links (IsFileMovable in sfx2/source/doc/docfile.cxx).
        target_stat = _file_stat(url)
        if target_stat is not None:
            for model, model_url in others:
                model_stat = _file_stat(model_url)
                if model_stat is not None and os.path.samestat(model_stat, target_stat):
                    return resolved(model)
    except Exception as e:
        reraise_if_disposed(e, _uno_disposal_message(e), object_type="Desktop")
        log.exception("resolve_document_by_url enumeration error")
    return (None, None)


@main_thread_only
def get_document_from_frame(frame: Any) -> Any:
    """Get the document model strictly from the frame controller.

    This is the preferred path for sidebar panels to ensure we resolve
    the document bound to the active window rather than relying on Desktop.
    """
    if frame is None:
        return None

    with suppress_disposed("resolve document from frame", logger=log):
        controller = frame.getController()
        if controller is None:
            return None
        model = controller.getModel()
        if model is not None:
            return _guard_returned_uno(model)
    return None


def document_id_for_frame(frame: Any) -> str:
    """RuntimeUID of the model on *frame*, or ``""``.

    The id comes from the frame's controller. ``getCurrentComponent()`` is a
    different window when focus has moved.
    """
    if frame is None:
        return ""
    if not main.is_current():
        return ""
    model = get_document_from_frame(frame)
    if model is None:
        return ""
    return get_runtime_uid(model)


def create_property_value(name: str, value: Any) -> Any:
    """Create a ``com.sun.star.beans.PropertyValue``."""
    import uno

    p = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    p.Name = name
    p.Value = value
    return p


def property_arg(args: Any, name: str) -> Any:
    """Extract PropertyValue from args by Name."""
    for pv in args or ():
        if hasattr(pv, "Name") and pv.Name == name:
            return pv.Value
    return None


def execute_dispatch(ctx: Any, controller: Any, command: str, args: tuple[Any, ...] = ()) -> Any:
    """Run one ``.uno:`` command on the controller's frame through DispatchHelper and return its result.

    ``args`` is a tuple of ``com.sun.star.beans.PropertyValue`` passed to ``executeDispatch``.
    Returns an empty Any (None) when no dispatcher handled it
    (``framework/source/services/dispatchhelper.cxx DispatchHelper::executeDispatch``).
    ``tracking_manage`` reads that on Calc.
    """
    return _create_service(ctx, "com.sun.star.frame.DispatchHelper").executeDispatch(
        controller.getFrame(), command, "", 0, args
    )
