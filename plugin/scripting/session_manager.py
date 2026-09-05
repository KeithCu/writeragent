# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared-kernel session ids for Calc =PY(), Writer notebooks, and menubar reset."""

from __future__ import annotations

import logging
import threading
import uuid
import weakref
from typing import Any

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
# Headless soffice opens this probe workbook. Recording it next to leftover
# factory Calc makes recorded=2 / Isolated (leftover 11:31 ids=).
_OPENCL_PROBE_MARK = "opencl/cl-test.ods"


def is_opencl_probe_session_id(session_id: str | None) -> bool:
    """True for LibreOffice's OpenCL ``cl-test.ods`` probe workbook."""
    if not session_id:
        return False
    return _OPENCL_PROBE_MARK in str(session_id).replace("\\", "/")


def python_session_mode(ctx: Any) -> str:
    """Return ``isolated`` or ``shared`` from config (default ``isolated``)."""
    mode = (get_config_str(_SESSION_MODE_KEY) or "isolated").strip().lower()
    if mode != "shared":
        mode = "isolated"
    try:
        from plugin.framework.config import _config_path

        log.debug("python_session_mode=%s config=%s", mode, _config_path())
    except Exception:
        log.debug("python_session_mode=%s config=<unresolved>", mode)
    return mode


def _find_document_by_predicate(ctx: Any, predicate: Any) -> Any | None:
    """Find active document matching *predicate*, falling back to desktop component enumeration."""
    # Bugfix (#411): In headless mode or when focus is outside the frame, getCurrentComponent()
    # returns None. Fall back to desktop.getComponents() enumeration so session reset and
    # shared-kernel workbook_session_id always resolve the document model.
    try:
        from plugin.framework.errors import check_disposed
        from plugin.framework.thread_guard import guard_uno, _unwrap_uno

        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
        if doc is not None:
            try:
                # check_disposed is a None check; get_desktop is already @main_thread_only.
                # Unwrap so PropertyBag-style None tests see the real object, then re-wrap on return.
                check_disposed(_unwrap_uno(doc))
                ctrl = getattr(doc, "getCurrentController", lambda: None)()
                if ctrl is not None and getattr(ctrl, "getFrame", lambda: None)() is not None:
                    if predicate(doc):
                        cached_sid = get_cached_calc_session_id()
                        if not cached_sid or calc_workbook_base_session_id(doc) == cached_sid:
                            return guard_uno(doc)
            except Exception:
                pass

        comps = desktop.getComponents()
        if comps is not None and hasattr(comps, "createEnumeration"):
            enum = comps.createEnumeration()
            matches = []
            while enum:
                try:
                    has_more = enum.hasMoreElements()
                except Exception:
                    break
                # MagicMock.hasMoreElements() is always truthy; this is a local
                # enumeration stop, not a general is_mock helper. Skip extracting
                # to deal_shim unless more call sites grow the same check.
                if type(has_more).__name__ in ("Mock", "MagicMock") or not has_more:
                    break
                elem = enum.nextElement()
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and getattr(elem, "getController", lambda: None)():
                    ctrl = elem.getController()
                    model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
                if model is not None:
                    try:
                        check_disposed(_unwrap_uno(model))
                        ctrl = getattr(model, "getCurrentController", lambda: None)()
                        if ctrl is not None and predicate(model):
                            matches.append(model)
                    except Exception:
                        pass

            if matches:
                cached_sid = get_cached_calc_session_id()
                if cached_sid:
                    for m in reversed(matches):
                        try:
                            if calc_workbook_base_session_id(m) == cached_sid:
                                return guard_uno(m)
                        except Exception:
                            pass
                return guard_uno(matches[-1])


    except Exception:
        log.debug("session_manager: document resolution failed", exc_info=True)
    return None



_ACTIVE_CALC_SESSION_LOCK = threading.Lock()
_LAST_ACTIVE_CALC_SESSION_ID: str | None = None
_LAST_ACTIVE_CALC_INIT_KWARGS: dict[str, Any] = {}
# Weakref to the last UI-thread Calc model. Off-main finalize may pass this
# through to deferred spill; do not call UNO on it off-main.
_LAST_ACTIVE_CALC_DOC: weakref.ReferenceType[Any] | None = None
# Session ids recorded while workbooks were on the UI thread. Off-main recalc
# may use the cache only when exactly one workbook is recorded — two open files
# would otherwise run doc B in doc A's shared kernel (XAddIn has no calling doc).
_RECORDED_CALC_SESSION_IDS: set[str] = set()


def record_active_calc_document(doc: Any | None) -> None:
    """Remember the UI-thread Calc model for off-main spill when the session is unambiguous."""
    global _LAST_ACTIVE_CALC_DOC
    if doc is None:
        return
    raw = doc
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        raw = _unwrap_uno(doc)
    except Exception:
        raw = doc
    with _ACTIVE_CALC_SESSION_LOCK:
        try:
            _LAST_ACTIVE_CALC_DOC = weakref.ref(raw)
        except TypeError:
            _LAST_ACTIVE_CALC_DOC = None


def get_cached_calc_document() -> Any | None:
    """Return the cached Calc model when at most one workbook session is recorded.

    Two recorded sessions: XAddIn has no calling document — do not guess.
    Zero recorded sessions (Isolated) still returns the last UI-thread model.
    The object is for *identity / later UI-thread use*. Do not invoke UNO on it
    from a worker or Yellow thread.
    """
    with _ACTIVE_CALC_SESSION_LOCK:
        if len(_RECORDED_CALC_SESSION_IDS) > 1:
            return None
        ref = _LAST_ACTIVE_CALC_DOC
    if ref is None:
        return None
    return ref()


def record_active_calc_session(
    session_id: str | None,
    init_kwargs: dict[str, Any] | None = None,
    doc: Any | None = None,
) -> None:
    """Cache the active Calc session id and init kwargs on the main thread for off-main formula lookups."""
    global _LAST_ACTIVE_CALC_SESSION_ID, _LAST_ACTIVE_CALC_INIT_KWARGS
    if doc is not None:
        record_active_calc_document(doc)
    with _ACTIVE_CALC_SESSION_LOCK:
        if session_id is not None:
            if is_opencl_probe_session_id(session_id):
                if init_kwargs:
                    _LAST_ACTIVE_CALC_INIT_KWARGS = dict(init_kwargs)
                return
            _LAST_ACTIVE_CALC_SESSION_ID = session_id
            _RECORDED_CALC_SESSION_IDS.add(session_id)
            # OnCreate can fall back to ``calc:unsaved:{uuid}`` before the
            # UDProp sticks; a later OnLoadFinished then records the persisted
            # id. Two unsaved: keys (UDProp failed twice) or unsaved+durable
            # both make ``off_main_calc_session_is_unambiguous`` false.
            sid_text = str(session_id)
            if sid_text.startswith("calc:unsaved:"):
                for stale in [
                    other
                    for other in _RECORDED_CALC_SESSION_IDS
                    if other != session_id and str(other).startswith("calc:unsaved:")
                ]:
                    _RECORDED_CALC_SESSION_IDS.discard(stale)
            else:
                for stale in [
                    other
                    for other in _RECORDED_CALC_SESSION_IDS
                    if str(other).startswith("calc:unsaved:")
                ]:
                    _RECORDED_CALC_SESSION_IDS.discard(stale)
        if init_kwargs:
            _LAST_ACTIVE_CALC_INIT_KWARGS = dict(init_kwargs)


def recorded_calc_session_count() -> int:
    """How many distinct Calc workbook sessions are currently recorded."""
    with _ACTIVE_CALC_SESSION_LOCK:
        return len(_RECORDED_CALC_SESSION_IDS)


def recorded_calc_session_ids() -> tuple[str, ...]:
    """Sorted host-side Calc session ids (soffice leftover diag)."""
    with _ACTIVE_CALC_SESSION_LOCK:
        return tuple(sorted(_RECORDED_CALC_SESSION_IDS))


def off_main_calc_session_is_unambiguous() -> bool:
    """True when off-main recalc can safely reuse the cached shared kernel."""
    with _ACTIVE_CALC_SESSION_LOCK:
        return len(_RECORDED_CALC_SESSION_IDS) == 1



def get_cached_calc_session_id() -> str | None:
    """Return the cached active Calc session id without querying the UNO desktop off-main."""
    with _ACTIVE_CALC_SESSION_LOCK:
        return _LAST_ACTIVE_CALC_SESSION_ID


def get_cached_calc_init_kwargs() -> dict[str, Any]:
    """Return the cached active Calc init kwargs without querying the UNO desktop off-main."""
    with _ACTIVE_CALC_SESSION_LOCK:
        return dict(_LAST_ACTIVE_CALC_INIT_KWARGS)


def clear_active_calc_session(session_id: str | None = None) -> None:
    """Clear cached Calc session on document unload or reset."""
    global _LAST_ACTIVE_CALC_SESSION_ID, _LAST_ACTIVE_CALC_INIT_KWARGS, _LAST_ACTIVE_CALC_DOC
    with _ACTIVE_CALC_SESSION_LOCK:
        if session_id is None:
            _RECORDED_CALC_SESSION_IDS.clear()
            _LAST_ACTIVE_CALC_SESSION_ID = None
            _LAST_ACTIVE_CALC_INIT_KWARGS = {}
            _LAST_ACTIVE_CALC_DOC = None
        else:
            _RECORDED_CALC_SESSION_IDS.discard(session_id)
            if _LAST_ACTIVE_CALC_SESSION_ID == session_id:
                _LAST_ACTIVE_CALC_SESSION_ID = next(iter(_RECORDED_CALC_SESSION_IDS), None)
                # Remaining workbook's init is unknown; do not keep the closed file's kwargs.
                _LAST_ACTIVE_CALC_INIT_KWARGS = {}
                _LAST_ACTIVE_CALC_DOC = None
    try:
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()
    except Exception:
        pass



def _calc_document(ctx: Any) -> Any | None:
    return _find_document_by_predicate(ctx, is_calc)


def _writer_document(ctx: Any) -> Any | None:
    return _find_document_by_predicate(ctx, is_writer)


def _workbook_session_key(doc: Any) -> str:
    from plugin.framework.thread_guard import _unwrap_uno

    raw_doc = _unwrap_uno(doc)
    url = ""
    try:
        url = (getattr(raw_doc, "getURL", lambda: "")() or "").strip()
    except Exception:
        pass
    if url:
        return url
    try:
        existing = get_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
        if existing:
            return str(existing)
    except Exception:
        pass
    new_id = str(uuid.uuid4())
    try:
        set_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP, new_id)
        return new_id
    except Exception:
        pass
    # Do not use id(raw_doc): CPython recycles ids after GC, so two unsaved
    # docs opened in sequence could collide on a stale worker session.
    return f"unsaved:{uuid.uuid4()}"


def calc_workbook_base_session_id(doc: Any) -> str:
    """Worker session id for shared-kernel ``=PY()`` (not the ``:init`` session)."""
    sid = f"calc:{_workbook_session_key(doc)}"
    record_active_calc_session(sid, doc=doc)
    return sid


def calc_init_session_id(doc: Any) -> str:
    """Persistent worker session that runs the workbook init script once."""
    return f"{calc_workbook_base_session_id(doc)}:init"


def workbook_session_id(ctx: Any, doc: Any | None = None) -> str | None:
    """Return ``calc:…`` session id when shared mode and target doc is Calc, else ``None``."""
    if python_session_mode(ctx) != "shared":
        return None

    if doc is not None:
        try:
            if is_calc(doc):
                from plugin.framework.thread_guard import guard_uno

                return calc_workbook_base_session_id(guard_uno(doc))
        except Exception:
            pass
        # Fallback to URL/props directly from doc if is_calc check failed or raised
        try:
            return calc_workbook_base_session_id(doc)
        except Exception:
            pass

    from plugin.framework.thread_guard import on_main_thread

    # Off-main threads without an explicit doc must not query the desktop (Yellow contract #402, #411).
    # XAddIn never names the recalculating workbook: reuse the cache only when a
    # single workbook is recorded. Two open files would bleed shared-kernel state.
    if not on_main_thread():
        if not off_main_calc_session_is_unambiguous():
            return None
        return get_cached_calc_session_id()

    target = _calc_document(ctx)
    if target is None:
        return None
    return calc_workbook_base_session_id(target)


def rps_session_id(ctx: Any, doc: Any | None = None) -> str | None:
    """Document-keyed shared kernel for Run Python Script (library cache + user globals).

    Calc uses the same ``calc:…`` id as ``=PY()``. Writer/Draw use ``rps:…`` from
    the same UDProp so two Writer files do not share a namespace. Isolated mode
    returns ``None`` (in-run library cache only).
    """
    if python_session_mode(ctx) != "shared" or doc is None:
        return None
    if is_calc(doc):
        return workbook_session_id(ctx, doc)
    return f"rps:{_workbook_session_key(doc)}"


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
        _msgbox(
            ctx,
            _(
                "Reset Python Session for notebooks applies to LibreOffice Writer. "
                "Open a Writer document with an imported Jupyter notebook and try again."
            ),
        )
        return
    if not _has_notebook_registry(target):
        _msgbox(
            ctx,
            _(
                "This Writer document has no imported notebook registry. "
                "File → Open a Jupyter notebook (.ipynb) first."
            ),
        )
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
        _msgbox(
            ctx,
            _(
                "Reset Python Session applies to Calc spreadsheets. "
                "Open a Calc workbook and try again."
            ),
        )
        return

    from plugin.scripting.document_scripts import build_python_eval_init_kwargs, get_calc_init_script

    session_id = calc_workbook_base_session_id(target)
    res = reset_python_session(ctx, session_id)
    try:
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()
    except Exception:
        pass
    if res.get("status") != "ok":

        msg = res.get("message") or _("Could not reset Python session.")
        _msgbox(ctx, _("Error: {0}").format(msg))
        return

    # Re-seed init script immediately after reset (C2.2.3) so helper functions (e.g. def double(x): ...)
    # and init variables are re-populated in the worker for both shared and isolated sessions.
    init_kwargs = build_python_eval_init_kwargs(target)
    record_active_calc_session(session_id, init_kwargs)
    if init_kwargs:
        from plugin.scripting.venv_worker import run_code_in_user_venv

        run_code_in_user_venv(
            ctx,
            "None",
            session_id=session_id if python_session_mode(ctx) == "shared" else None,
            **init_kwargs,
        )

    has_init = bool((get_calc_init_script(target) or "").strip())
    if python_session_mode(ctx) == "shared":
        _msgbox(ctx, _("Python session reset for this workbook."))
    elif has_init:
        _msgbox(
            ctx,
            _(
                "Initialization script and any in-memory init state were reset for this workbook. "
                "Cell variables were already isolated per cell."
            ),
        )
    else:
        _msgbox(
            ctx,
            _(
                "Python session mode is Isolated (each =PY() cell uses its own variables). "
                "There is no shared cell session to reset. Add an initialization script if you "
                "need to clear expensive one-time workbook setup."
            ),
        )


def _reset_rps_python_session(ctx: Any, doc: Any, *, notify: bool = True) -> None:
    """Drop the Run Python Script shared executor (``rps:`` / Writer-Draw library cache)."""
    sid = f"rps:{_workbook_session_key(doc)}"
    res = reset_python_session(ctx, sid)
    if not notify:
        return
    if res.get("status") == "ok":
        _msgbox(ctx, _("Python session reset for this document."))
        return
    msg = res.get("message") or _("Could not reset Python session.")
    _msgbox(ctx, _("Error: {0}").format(msg))


def reset_workbook_python_session(ctx: Any, doc: Any | None = None) -> None:
    """Menubar handler: reset notebook kernel (Writer) or shared Calc workbook session."""
    if doc is not None:
        if is_writer(doc):
            if _has_notebook_registry(doc):
                reset_notebook_python_session(ctx, doc)
                _reset_rps_python_session(ctx, doc, notify=False)
            else:
                _reset_rps_python_session(ctx, doc)
            return
        if is_draw(doc):
            _reset_rps_python_session(ctx, doc)
            return
        _reset_calc_python_sessions(ctx, doc)
        return

    # Prioritize Calc document if one is active/open
    calc_doc = _calc_document(ctx)
    if calc_doc is not None:
        _reset_calc_python_sessions(ctx, calc_doc)
        return

    writer_doc = _writer_document(ctx)
    if writer_doc is not None:
        if _has_notebook_registry(writer_doc):
            reset_notebook_python_session(ctx, writer_doc)
            _reset_rps_python_session(ctx, writer_doc, notify=False)
        else:
            _reset_rps_python_session(ctx, writer_doc)
        return

    _reset_calc_python_sessions(ctx, None)
