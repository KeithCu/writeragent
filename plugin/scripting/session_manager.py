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
import os
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
# Desktop component enums. MagicMock.hasMoreElements() never goes false, and a
# stuck UNO enum must not spin. Same cap as
# excel_py_convert.auto_open._record_desktop_calc_sessions.
_ENUM_CAP = 32


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


def _cached_calc_session_matches(doc: Any, cached_sid: str | None) -> bool:
    """True when lookup may return *doc* without minting a Calc session.

    Bugfix: comparing with ``calc_workbook_base_session_id`` wrote
    ``WriterAgentPythonSessionId`` and recorded Writer models as Calc, which
    made off-main ``=PY()`` treat the session as ambiguous.
    """
    if not cached_sid:
        return True
    key = _existing_workbook_session_key(doc)
    if not key:
        return False
    return cached_sid == f"calc:{key}"


def _remember_session_snapshot_locked(
    session_id: str,
    doc: Any | None,
    init_kwargs: dict[str, Any] | None,
) -> None:
    if init_kwargs:
        _SESSION_INIT[session_id] = dict(init_kwargs)
    if doc is None:
        return
    raw = doc
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        raw = _unwrap_uno(doc)
    except Exception:
        raw = doc
    try:
        _SESSION_DOCS[session_id] = weakref.ref(raw)
    except TypeError:
        # PyUNO models often reject weakref. The id below is the snapshot
        # stand-in until this session id is dropped.
        _SESSION_DOCS.pop(session_id, None)
    _SESSION_DOC_IDS[session_id] = id(raw)


def _drop_session_snapshot_locked(session_id: str) -> None:
    _SESSION_DOCS.pop(session_id, None)
    _SESSION_INIT.pop(session_id, None)
    _SESSION_DOC_IDS.pop(session_id, None)


def _recorded_session_doc_is_locked(session_id: str, doc: Any) -> bool:
    """True when *session_id* was recorded for *doc*. Caller holds the lock.

    Prefer the ``_SESSION_DOCS`` snapshot. PyUNO often rejects weakref, so
    that map has no entry; ``_SESSION_DOC_IDS`` is the same object while the
    session id is still recorded (cleared with the snapshot, so a recycled
    id after ``clear_active_calc_session`` does not match).
    """
    if doc is None:
        return False
    raw = doc
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        raw = _unwrap_uno(doc)
    except Exception:
        raw = doc
    ref = _SESSION_DOCS.get(session_id)
    stored = None
    if ref is not None:
        try:
            stored = ref()
        except Exception:
            stored = None
    if stored is not None:
        if stored is doc or stored is raw:
            return True
        try:
            from plugin.framework.uno_context import uno_same

            # Distinct Python wrappers can still be one UNO document.
            return uno_same(stored, raw) is True
        except Exception:
            return False
    token = _SESSION_DOC_IDS.get(session_id)
    return token is not None and token == id(raw)


def _restore_remaining_snapshot_locked(remaining: str | None) -> None:
    global _LAST_ACTIVE_CALC_SESSION_ID, _LAST_ACTIVE_CALC_INIT_KWARGS, _LAST_ACTIVE_CALC_DOC
    global _LAST_ACTIVE_CALC_SCOPED_DIR, _LAST_ACTIVE_CALC_INIT_OWNER
    _LAST_ACTIVE_CALC_SESSION_ID = remaining
    _LAST_ACTIVE_CALC_INIT_OWNER = remaining
    if remaining is None:
        _LAST_ACTIVE_CALC_INIT_KWARGS = {}
        _LAST_ACTIVE_CALC_DOC = None
        _LAST_ACTIVE_CALC_SCOPED_DIR = None
        return
    _LAST_ACTIVE_CALC_INIT_KWARGS = dict(_SESSION_INIT.get(remaining) or {})
    stored = _SESSION_DOCS.get(remaining)
    _LAST_ACTIVE_CALC_DOC = stored
    # Folder lookup calls os.path.isdir. clear_active_calc_session fills this
    # after releasing the lock so a slow stat does not block other recorders.
    _LAST_ACTIVE_CALC_SCOPED_DIR = None


def _enumeration_should_continue(enum: Any, seen: int, *, label: str) -> bool:
    """False when *enum* is exhausted, a MagicMock, or past ``_ENUM_CAP``.

    MagicMock.hasMoreElements() is always truthy, so ``while enum.hasMoreElements()``
    never returns. The cap is the same stop sibling desktop walks use.
    """
    try:
        has_more = enum.hasMoreElements()
    except Exception:
        return False
    if type(has_more).__name__ in ("Mock", "MagicMock") or not has_more:
        return False
    if seen >= _ENUM_CAP:
        log.error("%s: desktop enum hit cap=%s; stopping", label, _ENUM_CAP)
        return False
    return True


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
                # Bugfix: headless soffice returns None from getCurrentController().
                # Requiring a controller skipped the open model, so session reset
                # and shared-kernel lookup never saw it. A controller with no
                # frame is an unfocused window — keep searching in that case.
                if ctrl is None or getattr(ctrl, "getFrame", lambda: None)() is not None:
                    if predicate(doc):
                        cached_sid = get_cached_calc_session_id()
                        if _cached_calc_session_matches(doc, cached_sid):
                            return guard_uno(doc)
            except Exception:
                pass

        comps = desktop.getComponents()
        if comps is not None and hasattr(comps, "createEnumeration"):
            enum = comps.createEnumeration()
            matches = []
            seen = 0
            while enum:
                if not _enumeration_should_continue(enum, seen, label="session_manager"):
                    break
                seen += 1
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
                        # Bugfix: `ctrl is not None` skipped headless models
                        # (getCurrentController() is None). Probe so a disposed
                        # controller still raises into the except; None matches.
                        getattr(model, "getCurrentController", lambda: None)()
                        if predicate(model):
                            matches.append(model)
                    except Exception:
                        pass

            if matches:
                cached_sid = get_cached_calc_session_id()
                if cached_sid:
                    for m in reversed(matches):
                        try:
                            if _cached_calc_session_matches(m, cached_sid):
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
# Workbook the cached init kwargs belong to. Focus can move without new kwargs
# (calc_workbook_base_session_id). Closing a different file must not leave
# this cache pointing at the closed workbook.
_LAST_ACTIVE_CALC_INIT_OWNER: str | None = None
# Weakref to the last UI-thread Calc model. Off-main finalize may pass this
# through to deferred spill; do not call UNO on it off-main.
_LAST_ACTIVE_CALC_DOC: weakref.ReferenceType[Any] | None = None
# Document folder captured from a file: session id (no UNO). Off-main =PY()
# injects this as scoped_dir so run_sql file joins do not see None.
_LAST_ACTIVE_CALC_SCOPED_DIR: str | None = None
# Session ids recorded while workbooks were on the UI thread. Off-main recalc
# may use the cache only when exactly one workbook is recorded — two open files
# would otherwise run doc B in doc A's shared kernel (XAddIn has no calling doc).
_RECORDED_CALC_SESSION_IDS: set[str] = set()
# Per-session snapshots so closing one workbook can restore the survivor.
# The last-active weakref alone was cleared even when another id remained.
_SESSION_DOCS: dict[str, weakref.ReferenceType[Any]] = {}
# id(doc) when the model cannot be weak-referenced. Cleared with the session.
_SESSION_DOC_IDS: dict[str, int] = {}
_SESSION_INIT: dict[str, dict[str, Any]] = {}


def _system_dir_from_file_url(url: str) -> str | None:
    """Parent directory of a ``file:`` URL. No UNO — safe off-main."""
    from urllib.parse import unquote, urlparse

    raw = str(url).strip()
    if raw.startswith("file:/") and not raw.startswith("file://"):
        raw = "file://" + raw[len("file:") :]
    parsed = urlparse(raw)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    path = os.path.normpath(path)
    parent = os.path.dirname(path)
    return parent if parent and os.path.isdir(parent) else None


def scoped_dir_from_calc_session_id(session_id: str | None) -> str | None:
    """``calc:file:///path/workbook.xlsx`` → ``/path`` when that folder exists."""
    if not session_id:
        return None
    text = str(session_id)
    if text.startswith("calc:"):
        text = text[5:]
    if text.endswith(":init"):
        text = text[:-5]
    if not text.startswith("file:"):
        return None
    return _system_dir_from_file_url(text)


def record_active_calc_scoped_dir(path: str | None) -> None:
    """Remember the document folder for off-main ``=PY()`` ``scoped_dir`` inject."""
    global _LAST_ACTIVE_CALC_SCOPED_DIR
    with _ACTIVE_CALC_SESSION_LOCK:
        _LAST_ACTIVE_CALC_SCOPED_DIR = path or None


def get_cached_calc_scoped_dir() -> str | None:
    """Cached document folder when at most one workbook session is recorded."""
    with _ACTIVE_CALC_SESSION_LOCK:
        if len(_RECORDED_CALC_SESSION_IDS) > 1:
            return None
        return _LAST_ACTIVE_CALC_SCOPED_DIR


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


def _assign_calc_init_kwargs_locked(owner: str | None, init_kwargs: dict[str, Any]) -> None:
    """Store init kwargs and the workbook id they belong to.

    *owner* is already resolved. None means no workbook — callers that want
    the focused id pass ``_LAST_ACTIVE_CALC_SESSION_ID`` themselves.
    """
    global _LAST_ACTIVE_CALC_INIT_KWARGS, _LAST_ACTIVE_CALC_INIT_OWNER
    _LAST_ACTIVE_CALC_INIT_KWARGS = dict(init_kwargs)
    _LAST_ACTIVE_CALC_INIT_OWNER = owner


def _existing_calc_session_id(doc: Any) -> str | None:
    """``calc:`` id from a URL or stored prop. Does not mint a new prop."""
    try:
        key = _existing_workbook_session_key(doc)
    except Exception:
        log.debug("record_active_calc_session: init owner lookup failed", exc_info=True)
        return None
    if not key:
        return None
    return f"calc:{key}"


def record_active_calc_session(
    session_id: str | None,
    init_kwargs: dict[str, Any] | None = None,
    doc: Any | None = None,
) -> None:
    """Cache the active Calc session id and init kwargs on the main thread for off-main formula lookups."""
    global _LAST_ACTIVE_CALC_SESSION_ID, _LAST_ACTIVE_CALC_SCOPED_DIR
    probe = is_opencl_probe_session_id(session_id)
    # Bugfix: record_active_calc_document ran before the OpenCL-probe return.
    # The probe model became _LAST_ACTIVE_CALC_DOC while its session id was
    # discarded, so off-main spill wrote into cl-test.ods.
    if doc is not None and not probe:
        record_active_calc_document(doc)
    # Bugfix: scoped_dir_from_calc_session_id calls os.path.isdir. Doing that
    # under _ACTIVE_CALC_SESSION_LOCK blocked UI-thread record/clear on a
    # slow stat. Resolve the folder first; store the string under the lock.
    scoped_dir = None
    if session_id is not None and not probe:
        scoped_dir = scoped_dir_from_calc_session_id(session_id)
    # None means "use the focused id once the lock is held". The document key
    # is resolved here so getURL() does not run under the lock.
    # Bugfix: session_id=None attributed the snapshot to
    # _LAST_ACTIVE_CALC_SESSION_ID. set_calc_init_script then cleared whichever
    # workbook was focused, not the document whose INIT changed. Pass
    # calc_workbook_base_session_id(doc). A doc argument still names the
    # snapshot when the caller only has the model.
    explicit_owner: str | None = None
    if init_kwargs is not None and not probe:
        if session_id is not None:
            explicit_owner = session_id
        elif doc is not None:
            explicit_owner = _existing_calc_session_id(doc)
    with _ACTIVE_CALC_SESSION_LOCK:
        previous_sid = _LAST_ACTIVE_CALC_SESSION_ID
        if session_id is not None:
            if probe:
                # Same rule as the assignment below: {} clears, None does not.
                if init_kwargs is not None:
                    _LAST_ACTIVE_CALC_INIT_KWARGS = dict(init_kwargs)
                return
            _LAST_ACTIVE_CALC_SESSION_ID = session_id
            _RECORDED_CALC_SESSION_IDS.add(session_id)
            _remember_session_snapshot_locked(session_id, doc, init_kwargs)
            # OnCreate can fall back to ``calc:unsaved:{uuid}`` before the
            # UDProp sticks; a later OnLoadFinished then records the persisted
            # id. Two unsaved: keys (UDProp failed twice) or unsaved+durable
            # both make ``off_main_calc_session_is_unambiguous`` false.
            # No doc: that replacement is one workbook (callers that omit doc).
            # With doc: calc:unsaved:{uuid} is a real workbook id after the
            # unsaved:{uuid} prefix. Drop only an unsaved id whose snapshot
            # is this same document.
            if doc is None:
                # Callers that omit doc model one workbook replacing its own id.
                sid_text = str(session_id)
                if sid_text.startswith("calc:unsaved:"):
                    for stale in [
                        other
                        for other in _RECORDED_CALC_SESSION_IDS
                        if other != session_id and isinstance(other, str) and other.startswith("calc:unsaved:")
                    ]:
                        _RECORDED_CALC_SESSION_IDS.discard(stale)
                        _drop_session_snapshot_locked(stale)
                else:
                    for stale in [
                        other
                        for other in _RECORDED_CALC_SESSION_IDS
                        if isinstance(other, str) and other.startswith("calc:unsaved:")
                    ]:
                        _RECORDED_CALC_SESSION_IDS.discard(stale)
                        _drop_session_snapshot_locked(stale)
            else:
                # Bugfix: recording calc:<url> for a different open book
                # discarded every calc:unsaved: id. The recorded count became
                # 1, off-main =PY() looked unambiguous, and it ran in the
                # wrong workbook. How: the GC could not tell which unsaved
                # id belonged to the document being recorded. Why: drop an
                # unsaved id only when its _SESSION_DOCS snapshot is this
                # document (that book's own id promotion).
                for stale in [
                    other
                    for other in _RECORDED_CALC_SESSION_IDS
                    if other != session_id and isinstance(other, str) and other.startswith("calc:unsaved:")
                ]:
                    if _recorded_session_doc_is_locked(stale, doc):
                        _RECORDED_CALC_SESSION_IDS.discard(stale)
                        _drop_session_snapshot_locked(stale)
            _LAST_ACTIVE_CALC_SCOPED_DIR = scoped_dir
        # Bugfix: ``if init_kwargs`` treated {} like "not passed". Clearing the
        # workbook init passes {} (``set_calc_init_script``), and the previous
        # script stayed in the off-main cache. None still means the caller did
        # not supply kwargs.
        # Omitting kwargs on the *same* workbook leaves the cache. Switching
        # workbooks (``calc_workbook_base_session_id`` always passes None)
        # must not keep the previous file's init: closing that file then left
        # its script on the survivor's unambiguous off-main ``=PY()``.
        if init_kwargs is not None:
            owner = explicit_owner if explicit_owner is not None else _LAST_ACTIVE_CALC_SESSION_ID
            focused = _LAST_ACTIVE_CALC_SESSION_ID
            # A different document's INIT updates its snapshot only. Replacing
            # the focused cache would make that file's next unambiguous =PY()
            # run the other workbook's script.
            if session_id is not None or owner == focused or focused is None:
                _assign_calc_init_kwargs_locked(owner, init_kwargs)
            if owner:
                if init_kwargs:
                    _SESSION_INIT[owner] = dict(init_kwargs)
                else:
                    _SESSION_INIT.pop(owner, None)
        elif session_id is not None and session_id != previous_sid:
            _assign_calc_init_kwargs_locked(session_id, _SESSION_INIT.get(session_id) or {})


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


def _single_recorded_calc_session_locked() -> str | None:
    """Cached session id when exactly one workbook is recorded; else None.

    Caller holds ``_ACTIVE_CALC_SESSION_LOCK``.
    """
    if len(_RECORDED_CALC_SESSION_IDS) != 1:
        return None
    return _LAST_ACTIVE_CALC_SESSION_ID


def unambiguous_cached_calc_session_id() -> str | None:
    """Off-main Calc session id, or None unless exactly one workbook is recorded.

    Bugfix: ``workbook_session_id`` checked ``off_main_calc_session_is_unambiguous``
    and then ``get_cached_calc_session_id`` under two lock acquisitions. A
    UI-thread ``record_active_calc_session`` could add a second workbook in
    that gap, so off-main ``=PY()`` of workbook A ran in B's kernel. The
    len==1 test and the id read now share this lock.
    """
    with _ACTIVE_CALC_SESSION_LOCK:
        return _single_recorded_calc_session_locked()


def get_cached_calc_session_id() -> str | None:
    """Return the cached active Calc session id without querying the UNO desktop off-main."""
    with _ACTIVE_CALC_SESSION_LOCK:
        return _LAST_ACTIVE_CALC_SESSION_ID


def get_cached_calc_init_kwargs() -> dict[str, Any]:
    """Init kwargs when exactly one workbook is recorded; otherwise {}.

    Bugfix: ``get_python_init_kwargs`` used the same two-lock check as
    ``workbook_session_id``. A second workbook recorded in the gap left the
    last-focused file's init on the other file's ``=PY()``. The len==1 test
    and the kwargs copy share ``_single_recorded_calc_session_locked``.
    """
    with _ACTIVE_CALC_SESSION_LOCK:
        if _single_recorded_calc_session_locked() is None:
            return {}
        return dict(_LAST_ACTIVE_CALC_INIT_KWARGS)


def clear_active_calc_session(session_id: str | None = None) -> None:
    """Clear cached Calc session on document unload or reset."""
    global _LAST_ACTIVE_CALC_SESSION_ID, _LAST_ACTIVE_CALC_INIT_KWARGS, _LAST_ACTIVE_CALC_DOC
    global _LAST_ACTIVE_CALC_SCOPED_DIR, _LAST_ACTIVE_CALC_INIT_OWNER
    restored_sid: str | None = None
    with _ACTIVE_CALC_SESSION_LOCK:
        if session_id is None:
            _RECORDED_CALC_SESSION_IDS.clear()
            _SESSION_DOCS.clear()
            _SESSION_DOC_IDS.clear()
            _SESSION_INIT.clear()
            _LAST_ACTIVE_CALC_SESSION_ID = None
            _LAST_ACTIVE_CALC_INIT_KWARGS = {}
            _LAST_ACTIVE_CALC_INIT_OWNER = None
            _LAST_ACTIVE_CALC_DOC = None
            _LAST_ACTIVE_CALC_SCOPED_DIR = None
        else:
            _RECORDED_CALC_SESSION_IDS.discard(session_id)
            _drop_session_snapshot_locked(session_id)
            if _LAST_ACTIVE_CALC_SESSION_ID == session_id:
                # Bugfix: closing the focused workbook used to drop the other
                # file's cached model even though its id stayed recorded.
                # Restore that snapshot when we have one; otherwise leave the
                # model unset so off-main spill does not guess.
                restored_sid = next(iter(_RECORDED_CALC_SESSION_IDS), None)
                _restore_remaining_snapshot_locked(restored_sid)
            elif _LAST_ACTIVE_CALC_INIT_OWNER == session_id:
                # Bugfix: B was last-active, but the cache still held A's init
                # (focus moved with init_kwargs=None). Closing A left that
                # script for B's unambiguous off-main =PY().
                survivor = _LAST_ACTIVE_CALC_SESSION_ID
                snapshot = _SESSION_INIT.get(survivor) if survivor else None
                _assign_calc_init_kwargs_locked(survivor, snapshot or {})
    if restored_sid:
        # isdir stays outside the lock. Re-check the id so a record that
        # landed after we released does not lose its own folder.
        folder = scoped_dir_from_calc_session_id(restored_sid)
        with _ACTIVE_CALC_SESSION_LOCK:
            if _LAST_ACTIVE_CALC_SESSION_ID == restored_sid:
                _LAST_ACTIVE_CALC_SCOPED_DIR = folder
    try:
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()
    except Exception:
        # A failed clear used to leave cached =PY() scalars with no traceback.
        log.debug("session_manager: clear_python_addin_cache failed", exc_info=True)



def _calc_document(ctx: Any) -> Any | None:
    return _find_document_by_predicate(ctx, is_calc)


def _writer_document(ctx: Any) -> Any | None:
    return _find_document_by_predicate(ctx, is_writer)


def _existing_workbook_session_key(doc: Any) -> str | None:
    """URL or already-stored session prop. Does not create a prop."""
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
    return None


def _workbook_session_key(doc: Any) -> str:
    from plugin.framework.thread_guard import _unwrap_uno

    raw_doc = _unwrap_uno(doc)

    url = ""
    try:
        url = (getattr(raw_doc, "getURL", lambda: "")() or "").strip()
    except Exception:
        pass

    existing = None
    try:
        existing = get_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
    except Exception:
        pass

    if url:
        if existing:
            try:
                # Remove the property so next Save As continues to follow URL
                from plugin.doc.udprops import remove_document_property
                remove_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
            except Exception:
                pass
        return url

    if existing:
        return str(existing)
    new_id = f"unsaved:{uuid.uuid4()}"
    try:
        set_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP, new_id)
        # Bugfix: set_document_property returns without writing when the
        # document has no UserDefinedProperties bag, and it does not raise.
        # Returning the minted id made the next call mint a different key.
        # Read it back; otherwise use the unsaved:uuid fallback.
        stored = get_document_property(raw_doc, PYTHON_WORKBOOK_SESSION_PROP)
        if stored is not None and str(stored) == new_id:
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
            calc = is_calc(doc)
        except Exception:
            calc = None
        # A present Writer or Draw model is not a Calc session. The old
        # fall-through recorded it whenever is_calc returned false.
        if calc is False:
            return None
        if calc is True:
            try:
                from plugin.framework.thread_guard import guard_uno

                return calc_workbook_base_session_id(guard_uno(doc))
            except Exception:
                pass
        # is_calc raised, or the guarded call failed: try the workbook key.
        try:
            return calc_workbook_base_session_id(doc)
        except Exception:
            pass
        return None

    from plugin.framework.thread_guard import on_main_thread

    # Off-main threads without an explicit doc must not query the desktop (Yellow contract #402, #411).
    # XAddIn never names the recalculating workbook: reuse the cache only when a
    # single workbook is recorded. Two open files would bleed shared-kernel state.
    if not on_main_thread():
        return unambiguous_cached_calc_session_id()

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


def document_for_script_session(ctx: Any, session_id: str | None) -> Any | None:
    """Open document whose workbook key matches *session_id*.

    ``wa.doc`` used to call ``get_active_document``, so with two files open the
    focused library was eval'd into whichever executor was running. The host
    is single-flight and already has the in-flight session id.

    ``ppt_master:{url}`` uses that same URL key. A long PPT-Master turn then
    exports into the sidebar frame's deck. ``ppt_master:active`` (no URL)
    does not match and the caller falls back to the focused document.
    """
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
        desktop = get_desktop(ctx)
        comps = desktop.getComponents() if desktop is not None else None
        if not comps:
            return None
        enum = comps.createEnumeration()
        seen = 0
        while enum is not None:
            if not _enumeration_should_continue(enum, seen, label="document_for_script_session"):
                break
            seen += 1
            elem = enum.nextElement()
            model = None
            if hasattr(elem, "getURL"):
                model = elem
            elif hasattr(elem, "getController"):
                controller = elem.getController()
                if controller is not None and hasattr(controller, "getModel"):
                    model = controller.getModel()
            if model is None:
                continue
            try:
                # Read-only: _workbook_session_key would mint a UDProp on docs
                # that have never run Python.
                if _existing_workbook_session_key(model) == key:
                    from plugin.framework.thread_guard import guard_uno

                    # What was wrong: the open model was returned raw. A worker
                    # that resolved wa.doc / tool RPC could then call UNO off
                    # the main thread.
                    # How: desktop enumeration hands back the component itself.
                    # Why this works: guard_uno asserts on later access unless
                    # the caller is already on the main thread.
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
        # A failed clear used to leave cached =PY() scalars with no traceback.
        log.debug("session_manager: clear_python_addin_cache failed", exc_info=True)
    if res.get("status") != "ok":

        msg = res.get("message") or _("Could not reset Python session.")
        _msgbox(ctx, _("Error: {0}").format(msg))
        return

    # Re-seed init script immediately after reset (C2.2.3) so helper functions (e.g. def double(x): ...)
    # and init variables are re-populated in the worker for both shared and isolated sessions.
    init_kwargs = build_python_eval_init_kwargs(target)
    # Bugfix: this re-record omitted *target*, so it took the no-doc path that
    # drops every ``calc:unsaved:`` id. Reset of one saved workbook then left
    # ``recorded==1``. Off-main ``=PY()`` treated the still-open sibling as
    # unambiguous and ran it in this kernel.
    # How: ``record_active_calc_session(session_id, init_kwargs)`` models "one
    # workbook replacing its own id" only when the caller has no document.
    # Why: pass *target*. The same-document GC still drops this book's own
    # promoted unsaved id. Sibling unsaved ids and other workbooks stay.
    record_active_calc_session(session_id, init_kwargs, doc=target)
    if init_kwargs:
        from plugin.scripting.venv_worker import run_code_in_user_venv

        seed = run_code_in_user_venv(
            ctx,
            "None",
            session_id=session_id if python_session_mode(ctx) == "shared" else None,
            **init_kwargs,
        )
        if seed.get("status") != "ok":
            msg = seed.get("message") or _("Could not restore the initialization script.")
            _msgbox(ctx, _("Error: {0}").format(msg))
            return

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

    # The menubar handler is registered with no document. Reset the focused
    # window. Searching every open component used to clear a background Calc
    # kernel when the user pressed reset from Writer.
    try:
        current = get_desktop(ctx).getCurrentComponent()
    except Exception:
        current = None
    if is_writer(current) or is_draw(current) or is_calc(current):
        reset_workbook_python_session(ctx, current)
        return

    # No current component (headless): keep the open-document search.
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
