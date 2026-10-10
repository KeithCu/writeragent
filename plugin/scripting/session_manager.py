# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared-kernel session ids for Calc =PY(), Writer notebooks, and menubar reset."""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.doc.doc_type import is_calc, is_draw, is_writer
from plugin.doc.udprops import get_document_property, set_document_property
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_desktop
from plugin.scripting.venv_worker import reset_python_session

log = logging.getLogger(__name__)


def _msgbox(ctx: Any, message: str) -> None:
    """Lazy so first ``=PY()`` does not load the dialog stack."""
    from plugin.chatbot.dialogs import msgbox
    from plugin.framework.uno_context import product_display_name

    msgbox(ctx, product_display_name(ctx), message)


def _has_notebook_registry(doc: Any) -> bool:
    """Writer notebook registry; ImportError only if the notebook package is absent."""
    try:
        from plugin.notebook.cell_registry import has_notebook_registry
    except ImportError:
        return False
    return has_notebook_registry(doc)


PYTHON_WORKBOOK_SESSION_PROP = "WriterAgentPythonSessionId"
_SESSION_MODE_KEY = "scripting.python_session_mode"
# Desktop component enums. A stuck UNO enum must not spin.
_ENUM_CAP = 32


def python_session_mode(ctx: Any) -> str:
    """Return ``isolated`` or ``shared`` from config (default ``isolated``)."""
    mode = (get_config_str(_SESSION_MODE_KEY) or "isolated").strip().lower()
    if mode != "shared":
        mode = "isolated"
    return mode


def _doc_url(raw_doc: Any) -> str:
    """Return the stripped URL string of *raw_doc*, or empty string."""
    try:
        return (getattr(raw_doc, "getURL", lambda: "")() or "").strip()
    except Exception:
        return ""


def _enumeration_should_continue(enum: Any, seen: int, *, label: str) -> bool:
    """False when *enum* is exhausted or past ``_ENUM_CAP``."""
    try:
        has_more = enum.hasMoreElements()
    except Exception:
        return False
    if has_more is not True:
        return False
    if seen >= _ENUM_CAP:
        log.error("%s: desktop enum hit cap=%s; stopping", label, _ENUM_CAP)
        return False
    return True


def _iter_desktop_models(ctx: Any, *, label: str = "session_manager") -> Generator[Any, None, None]:
    """Yield all valid open models from desktop component enumeration."""
    from plugin.framework.errors import check_disposed
    from plugin.framework.thread_guard import _unwrap_uno

    desktop = get_desktop(ctx)
    if desktop is None:
        return
    comps = desktop.getComponents() if hasattr(desktop, "getComponents") else None
    if comps is None or not hasattr(comps, "createEnumeration"):
        return
    enum = comps.createEnumeration()
    seen = 0
    while enum:
        if not _enumeration_should_continue(enum, seen, label=label):
            break
        seen += 1
        try:
            elem = enum.nextElement()
        except Exception:
            break
        model = None
        if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
            model = elem
        elif hasattr(elem, "getController") and getattr(elem, "getController", lambda: None)():
            ctrl = elem.getController()
            model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
        if model is not None:
            try:
                check_disposed(_unwrap_uno(model))
                getattr(model, "getCurrentController", lambda: None)()
                yield model
            except Exception:
                pass


def _find_document_by_predicate(ctx: Any, predicate: Any) -> Any | None:
    """Find active document matching *predicate*, falling back to desktop component enumeration."""
    # Bugfix (#411): In headless mode or when focus is outside the frame, getCurrentComponent()
    # returns None. Fall back to desktop.getComponents() enumeration so UI commands
    # (session reset) still resolve the document model. =PY() never comes here.
    try:
        from plugin.framework.errors import check_disposed
        from plugin.framework.thread_guard import guard_uno, _unwrap_uno

        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent() if desktop else None
        if doc is not None:
            try:
                # check_disposed is a None check; get_desktop is already @main_thread_only.
                # Unwrap so PropertyBag-style None tests see the real object, then re-wrap on return.
                check_disposed(_unwrap_uno(doc))
                ctrl = getattr(doc, "getCurrentController", lambda: None)()
                # Headless soffice returns None from getCurrentController().
                # Requiring a controller skipped the open model, so session reset
                # and shared-kernel lookup never saw it. A controller with no
                # frame is an unfocused window — keep searching in that case.
                if ctrl is None or getattr(ctrl, "getFrame", lambda: None)() is not None:
                    if predicate(doc):
                        return guard_uno(doc)
            except Exception:
                pass

        matches = []
        for model in _iter_desktop_models(ctx, label="session_manager"):
            try:
                if predicate(model):
                    matches.append(model)
            except Exception:
                pass

        if matches:
            return guard_uno(matches[-1])

    except Exception:
        log.debug("session_manager: document resolution failed", exc_info=True)
    return None


# Chat run_venv_python_script pins ctx.doc for one execute. PyUNO models
# often reject weakref, so this is a strong reference and must be released
# when the call returns. The token is not a worker namespace id.
_SCRIPT_DOC_PINS: dict[str, Any] = {}
_SCRIPT_DOC_PIN_LOCK = threading.Lock()


def existing_calc_session_id(doc: Any) -> str | None:
    """``calc:`` id from a URL or stored prop. Does not mint a new prop."""
    try:
        key = _existing_workbook_session_key(doc)
    except Exception:
        log.debug("existing_calc_session_id: lookup failed", exc_info=True)
        return None
    if not key:
        return None
    return f"calc:{key}"


def _calc_document(ctx: Any) -> Any | None:
    return _find_document_by_predicate(ctx, is_calc)


def _writer_document(ctx: Any) -> Any | None:
    return _find_document_by_predicate(ctx, is_writer)


def _existing_workbook_session_key(doc: Any) -> str | None:
    """URL or already-stored session prop. Does not create a prop."""
    from plugin.framework.thread_guard import _unwrap_uno

    raw_doc = _unwrap_uno(doc)
    url = _doc_url(raw_doc)
    if url:
        return url
    try:
        existing = get_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
        if existing:
            return str(existing)
    except Exception:
        log.debug("session_manager: reading session property failed", exc_info=True)
    return None


def _workbook_session_key(doc: Any) -> str:
    from plugin.framework.thread_guard import _unwrap_uno

    raw_doc = _unwrap_uno(doc)
    url = _doc_url(raw_doc)

    existing = None
    try:
        existing = get_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
    except Exception:
        log.debug("session_manager: reading session property failed", exc_info=True)

    if url:
        if existing:
            try:
                # Remove the property so next Save As continues to follow URL
                from plugin.doc.udprops import remove_document_property

                remove_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
                # Reset orphaned unsaved worker sessions so old state does not leak
                if str(existing).startswith("unsaved:"):
                    from plugin.framework.uno_context import get_ctx
                    try:
                        ctx = get_ctx()
                        reset_python_session(ctx, f"calc:{existing}")
                        reset_python_session(ctx, f"rps:{existing}")
                        reset_python_session(ctx, f"notebook:{existing}")
                    except Exception:
                        log.debug("session_manager: cleanup of unsaved session failed", exc_info=True)
            except Exception:
                log.debug("session_manager: removing session property failed", exc_info=True)
        return url

    if existing:
        return str(existing)
    new_id = f"unsaved:{uuid.uuid4()}"
    try:
        set_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP, new_id)
        # set_document_property returns without writing when the document has
        # no UserDefinedProperties bag, and it does not raise. Returning the
        # minted id made the next call mint a different key. Read it back;
        # otherwise use the unsaved:uuid fallback.
        stored = get_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
        if stored is not None and str(stored) == new_id:
            return new_id
    except Exception:
        log.debug("session_manager: writing session property failed", exc_info=True)
    # Do not use id(raw_doc): CPython recycles ids after GC, so two unsaved
    # docs opened in sequence could collide on a stale worker session.
    return f"unsaved:{uuid.uuid4()}"


def calc_workbook_base_session_id(doc: Any) -> str:
    """Worker session id for shared-kernel ``=PY()`` (not the ``:init`` session)."""
    return f"calc:{_workbook_session_key(doc)}"


def calc_init_session_id(doc: Any) -> str:
    """Persistent worker session that runs the workbook init script once."""
    return f"{calc_workbook_base_session_id(doc)}:init"


def workbook_session_id(ctx: Any, doc: Any | None = None) -> str | None:
    """Return ``calc:…`` session id when shared mode and *doc* is Calc, else ``None``.

    *doc* is the caller's document (for ``=PY()``, the add-in caller argument).
    With no doc there is no session: never guess from the front window.
    """
    if python_session_mode(ctx) != "shared" or doc is None:
        return None

    try:
        if not is_calc(doc):
            return None
        from plugin.framework.thread_guard import guard_uno

        return calc_workbook_base_session_id(guard_uno(doc))
    except Exception:
        log.debug("workbook_session_id: guarded calc session id lookup failed", exc_info=True)

    try:
        return calc_workbook_base_session_id(doc)
    except Exception:
        log.debug("workbook_session_id: fallback session id lookup failed", exc_info=True)
    return None


def rps_session_id(ctx: Any, doc: Any | None = None) -> str | None:
    """Document-keyed shared kernel for Run Python Script (library cache + user globals).

    Calc uses the same ``calc:…`` id as ``=PY()``. Writer/Draw use ``rps:…`` from
    the same UDProp so two Writer files do not share a namespace. Isolated mode
    returns ``None`` (in-run library cache only).
    """
    if python_session_mode(ctx) != "shared" or doc is None:
        return None
    try:
        if is_calc(doc):
            return workbook_session_id(ctx, doc)
    except Exception:
        log.debug("rps_session_id: is_calc failed", exc_info=True)
    return f"rps:{_workbook_session_key(doc)}"


def pin_script_document(doc: Any) -> str | None:
    """Return a host-only id that :func:`document_for_script_session` resolves to *doc*.

    Chat ``run_venv_python_script`` forwards this so ``wa.draw`` / ``wa.shape``
    bind to ``ctx.doc`` instead of the focused window. The id is not the
    worker namespace (Isolated mode still starts a fresh kernel). Call
    :func:`release_script_document` when the execute returns.
    """
    if doc is None:
        return None
    token = f"doc:{uuid.uuid4()}"
    with _SCRIPT_DOC_PIN_LOCK:
        _SCRIPT_DOC_PINS[token] = doc
    return token


def release_script_document(session_id: str | None) -> None:
    """Drop a pin from :func:`pin_script_document`. Other session ids are ignored."""
    if not isinstance(session_id, str) or not session_id.startswith("doc:"):
        return
    with _SCRIPT_DOC_PIN_LOCK:
        _SCRIPT_DOC_PINS.pop(session_id, None)


@contextlib.contextmanager
def pinned_script_document(doc: Any) -> Generator[str | None, None, None]:
    """Context manager for pinning a document for the duration of a script run."""
    token = pin_script_document(doc)
    try:
        yield token
    finally:
        if token:
            release_script_document(token)


def document_for_script_session(ctx: Any, session_id: str | None) -> Any | None:
    """Open document whose workbook key matches *session_id*.

    ``wa.doc`` used to call ``get_active_document``, so with two files open the
    focused library was eval'd into whichever executor was running. The host
    is single-flight and already has the in-flight session id.

    ``ppt_master:{url}`` uses that same URL key. A long PPT-Master turn then
    exports into the sidebar frame's deck. ``ppt_master:active`` (no URL)
    does not match and the caller falls back to the focused document.

    ``doc:{uuid}`` is a host pin from :func:`pin_script_document` (chat
    ``run_venv_python_script``). It returns that object and does not walk
    the desktop. A pin that was already released does not match.
    """
    if isinstance(session_id, str) and session_id.startswith("doc:"):
        with _SCRIPT_DOC_PIN_LOCK:
            pinned = _SCRIPT_DOC_PINS.get(session_id)
        if pinned is None:
            return None
        from plugin.framework.thread_guard import guard_uno

        # Same main-thread wrap as the desktop enumeration path below.
        return guard_uno(pinned)
    if not isinstance(session_id, str) or ":" not in session_id:
        return None
    prefix, key = session_id.split(":", 1)
    if prefix not in {"calc", "rps", "notebook", "ppt_master"}:
        return None
    if prefix == "calc" and key.endswith(":init"):
        key = key[: -len(":init")]
    if not key:
        return None
    try:
        for model in _iter_desktop_models(ctx, label="document_for_script_session"):
            try:
                # Read-only: _workbook_session_key would mint a UDProp on docs
                # that have never run Python.
                if _existing_workbook_session_key(model) == key:
                    from plugin.framework.thread_guard import guard_uno

                    # Desktop enumeration hands back the component itself. Returning
                    # it raw let a worker that resolved wa.doc / tool RPC call
                    # UNO off the main thread. guard_uno asserts on later
                    # access unless the caller is already on the main thread.
                    return guard_uno(model)
            except Exception:
                log.debug("document_for_script_session: key read failed", exc_info=True)
    except Exception:
        log.debug("document_for_script_session: enumeration failed", exc_info=True)
    return None


def notebook_session_id(ctx: Any, doc: Any | None = None) -> str | None:
    """Return ``notebook:…`` for a Writer document (always shared when interactive notebook is used)."""
    target = doc if doc is not None else _writer_document(ctx)
    if target is None or not is_writer(target):
        return None
    return f"notebook:{_workbook_session_key(target)}"


def reset_notebook_python_session(ctx: Any, doc: Any | None = None) -> None:
    """Menubar path: reset shared Python namespace for the active Writer notebook document."""
    target = doc if doc is not None else _writer_document(ctx)
    if target is None:
        _msgbox(ctx, _("Reset Python Session for notebooks applies to LibreOffice Writer. Open a Writer document with an imported Jupyter notebook and try again."))
        return
    if not _has_notebook_registry(target):
        _msgbox(ctx, _("This Writer document has no imported notebook registry. File → Open a Jupyter notebook (.ipynb) first."))
        return

    session_id = notebook_session_id(ctx, target)
    if not session_id:
        _msgbox(ctx, _("Could not resolve notebook Python session."))
        return

    res = reset_python_session(ctx, session_id)
    # Restart Kernel: next In count is 1 even if the worker reset fails (timeout).
    try:
        from plugin.notebook.cell_registry import load_registry, save_registry

        state = load_registry(target)
        if state is not None:
            state.next_execution_count = 1
            save_registry(target, state)
    except Exception:
        log.debug("notebook reset: could not reset execution counter", exc_info=True)
    if res.get("status") == "ok":
        _msgbox(ctx, _("Notebook Python session reset for this document."))
        return

    msg = res.get("message") or _("Could not reset Python session.")
    _msgbox(ctx, _("Error: {0}").format(msg))


def _reset_calc_python_sessions(ctx: Any, doc: Any | None = None) -> None:
    target = doc if doc is not None else _calc_document(ctx)
    if target is None:
        _msgbox(ctx, _("Reset Python Session applies to Calc spreadsheets. Open a Calc workbook and try again."))
        return

    key = _existing_workbook_session_key(target)
    if not key:
        return

    from plugin.scripting.document_scripts import build_python_eval_init_kwargs, get_calc_init_script

    session_id = f"calc:{key}"
    res = reset_python_session(ctx, session_id)
    # Also reset the persistent :init session so worker state from previous init script is cleared
    reset_python_session(ctx, f"{session_id}:init")
    try:
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()
    except Exception:
        # A failed clear used to leave cached =PY() scalars with no traceback.
        log.debug("session_manager: clear_python_addin_cache failed", exc_info=True)
    if res.get("status") != "ok":
        msg = res.get("message") or _("Could not reset Python session.")
        _msgbox(ctx, _("Error: {0}").format(msg))
        return

    # Re-seed init script immediately after reset (C2.2.3) so helper functions (e.g. def double(x): ...)
    # and init variables are re-populated in the worker for both shared and isolated sessions.
    init_kwargs = build_python_eval_init_kwargs(target)
    if init_kwargs:
        from plugin.scripting.venv_worker import run_code_in_user_venv

        seed = run_code_in_user_venv(ctx, "None", session_id=session_id if python_session_mode(ctx) == "shared" else None, **init_kwargs)
        if seed.get("status") != "ok":
            msg = seed.get("message") or _("Could not restore the initialization script.")
            _msgbox(ctx, _("Error: {0}").format(msg))
            return

    has_init = bool((get_calc_init_script(target) or "").strip())
    if python_session_mode(ctx) == "shared":
        _msgbox(ctx, _("Python session reset for this workbook."))
    elif has_init:
        _msgbox(ctx, _("Initialization script and any in-memory init state were reset for this workbook. Cell variables were already isolated per cell."))
    else:
        _msgbox(ctx, _("Python session mode is Isolated (each =PY() cell uses its own variables). There is no shared cell session to reset. Add an initialization script if you need to clear expensive one-time workbook setup."))


def _reset_rps_python_session(ctx: Any, doc: Any, *, notify: bool = True) -> None:
    """Drop the Run Python Script shared executor (``rps:`` / Writer-Draw library cache)."""
    key = _existing_workbook_session_key(doc)
    if not key:
        return
    sid = f"rps:{key}"
    res = reset_python_session(ctx, sid)
    if not notify:
        return
    if res.get("status") == "ok":
        _msgbox(ctx, _("Python session reset for this document."))
        return
    msg = res.get("message") or _("Could not reset Python session.")
    _msgbox(ctx, _("Error: {0}").format(msg))


def _reset_writer_sessions(ctx: Any, doc: Any) -> None:
    """Reset notebook session (if notebook registry present) and rps session for Writer doc."""
    if _has_notebook_registry(doc):
        reset_notebook_python_session(ctx, doc)
        _reset_rps_python_session(ctx, doc, notify=False)
    else:
        _reset_rps_python_session(ctx, doc)


def reset_workbook_python_session(ctx: Any, doc: Any | None = None) -> None:
    """Menubar handler: reset notebook kernel (Writer) or shared Calc workbook session."""
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        from plugin.framework.queue_executor import execute_on_main_thread

        return execute_on_main_thread(reset_workbook_python_session, ctx, doc)

    # Resetting a session on an unsaved/clean document must not mint a key:
    # _workbook_session_key wrote a UserDefinedProperties entry when none
    # existed. Check _existing_workbook_session_key and return when it is
    # None. A focused Base, Math, or Start Center window is not Writer, Draw,
    # or Calc; falling through to desktop enumeration cleared a background
    # Calc document. Tell the user and return.
    if doc is not None:
        if is_writer(doc):
            if not _has_notebook_registry(doc) and _existing_workbook_session_key(doc) is None:
                return
            _reset_writer_sessions(ctx, doc)
            return
        if is_draw(doc):
            if _existing_workbook_session_key(doc) is None:
                return
            _reset_rps_python_session(ctx, doc)
            return
        if is_calc(doc):
            if _existing_workbook_session_key(doc) is None:
                return
            _reset_calc_python_sessions(ctx, doc)
            return
        _msgbox(ctx, _("Reset Python Session applies to Writer, Calc, and Draw documents."))
        return

    # The menubar handler is registered with no document. Reset the focused
    # window. Searching every open component used to clear a background Calc
    # kernel when the user pressed reset from Writer.
    try:
        desktop = get_desktop(ctx)
        current = desktop.getCurrentComponent() if desktop else None
    except Exception:
        current = None

    if current is not None:
        if not (is_writer(current) or is_draw(current) or is_calc(current)):
            _msgbox(ctx, _("Reset Python Session applies to Writer, Calc, and Draw documents."))
            return
        reset_workbook_python_session(ctx, current)
        return

    # No current component (headless): keep the open-document search.
    calc_doc = _calc_document(ctx)
    if calc_doc is not None:
        _reset_calc_python_sessions(ctx, calc_doc)
        return

    writer_doc = _writer_document(ctx)
    if writer_doc is not None:
        if not _has_notebook_registry(writer_doc) and _existing_workbook_session_key(writer_doc) is None:
            return
        _reset_writer_sessions(ctx, writer_doc)
        return

    _reset_calc_python_sessions(ctx, None)
