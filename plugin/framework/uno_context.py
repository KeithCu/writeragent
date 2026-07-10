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

from plugin.framework.thread_guard import main_thread_only, _wrap_uno

log = logging.getLogger("writeragent.context")

_fallback_ctx = None
# Set by main.py / main_core.py bootstrap; auto-detected from installed packages when unset.
_package_extension_id: str | None = None

_KNOWN_EXTENSION_IDS = (
    "org.extension.librepy",
    "org.extension.writeragent",
)


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
    return "org.extension.writeragent"


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


@contextmanager
def focus_preserved(ctx):
    """Capture the current focus window, yield, then restore it.

    RichTextControl append/paste/nudge must not steal focus from the chat query field;
    callers wrap UNO mutations that might move focus to the transcript control.
    """
    saved = None
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
def process_events_to_idle(ctx, rounds: int = 1) -> None:
    """Drain the UI event queue *rounds* times; no-op when toolkit is unavailable."""
    for idx in range(max(1, rounds)):
        try:
            tk = get_toolkit(ctx)
            if tk and hasattr(tk, "processEventsToIdle"):
                tk.processEventsToIdle()
        except Exception:
            pass
