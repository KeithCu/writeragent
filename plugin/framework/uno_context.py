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

Services are singletons that outlive the UNO component that created them.
The ctx passed during bootstrap (from MainJob.__init__) can become stale.

``uno.getComponentContext()`` always returns the current, valid global
context — this is the same call the fallback autostart thread uses.

All services that need UNO access should call ``get_ctx()`` rather than
storing a ctx reference from ``initialize()``.
"""

import logging
from contextlib import contextmanager
from typing import Any, cast

from plugin.framework.constants import (
    EXTENSION_ID_LIBREHARPER,
    EXTENSION_ID_LIBREPY,
    EXTENSION_ID_WRITERAGENT,
)
from plugin.framework.thread_guard import main_thread_only, _wrap_uno

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
    global _package_extension_id
    _package_extension_id = extension_id


def reset_package_extension_id_for_tests() -> None:
    """Clear cached extension id (unit tests only)."""
    global _package_extension_id
    _package_extension_id = None


def resolve_package_extension_id(ctx=None) -> str:
    """Return the installed WriterAgent-family extension id (LibrePy or WriterAgent)."""
    global _package_extension_id
    if _package_extension_id:
        return _package_extension_id

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
    """Return the current valid UNO component context.

    Prefers ``uno.getComponentContext()`` (always fresh).
    Falls back to the stored bootstrap ctx if uno is not importable.
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


@main_thread_only
def get_desktop(ctx=None):
    """Return the UNO Desktop instance."""
    ctx = ctx or get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    smgr = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
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
        smgr = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
        if smgr is None:
            return None
        tk = cast("Any", smgr).createInstanceWithContext("com.sun.star.awt.Toolkit", ctx_any)
        return _wrap_uno(tk)
    except Exception:
        log.exception("Failed to create toolkit")
        return None


# Sidebar query field: restore here after RichTextControl setFocus, not
# getFocusWindow() (often the Send button after a click, or the transcript).
_default_focus_restore = None


def set_default_focus_restore(control) -> None:
    """Pin focus restore to the chat query field (or None on panel dispose)."""
    global _default_focus_restore
    _default_focus_restore = control


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


def _normalize_doc_url(url):
    """Normalize document URL for comparison (strip, optional trailing slash)."""
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

    target = _normalize_doc_url(url)
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
                elif hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model is not None:
                    doc_url = _normalize_doc_url(model.getURL()) if hasattr(model, "getURL") else ""
                    uid = get_runtime_uid(model)
                    if (doc_url and doc_url == target) or (uid and uid == target):
                        doc_type_enum = _doc_type.get_document_type(model)
                        doc_type = "writer"
                        if doc_type_enum == _doc_type.DocumentType.CALC:
                            doc_type = "calc"
                        elif doc_type_enum in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
                            doc_type = "draw"
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
