# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wire notebook ▶ buttons to run handlers (form URL buttons do not reach ProtocolHandler).

``controller.getControl(model)`` realizes each control view. On a 144-button import
that was ~2.4s (and ~10s on some machines). PUSH buttons fire ``XActionListener`` on
the *view*; the form controller's ``XControlContainer`` already holds realized views
and notifies ``XContainerListener`` when later views appear (scroll / first paint).

Live check (Writer ``XFormLayerAccess.getFormController``):
``org.openoffice.comp.svx.FormController`` is *not* ``XApproveActionBroadcaster``
and the form model is not either. ``addMouseClickHandler`` / container mouse /
``XScriptListener`` did not see PUSH activation. ``doAccessibleAction`` *did*
notify ``XApproveActionListener`` / ``XActionListener`` on the button view.

So we attach **one** shared ``XActionListener`` to the form controller container
(existing ``getControls()`` plus ``elementInserted``), not N ``getControl(model)``.
One listener pair (NotebookFormRunListener + NotebookFormContainerListener) per
live container; OnUnload and on_disposing drop that pair; a new container adds another pair.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any

import uno

from plugin.framework.errors import best_effort, uno_read
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseActionListener, BaseContainerListener, BaseDocumentEventListener
from plugin.notebook.cell_registry import load_registry
from plugin.notebook.layout import RUN_PREFIX, doc_key, mono_ms, runtime_uid_from_doc_key

log = logging.getLogger("writeragent.notebook")


# ---------------------------------------------------------------------------
# Runtime Mode & Event Wiring
# ---------------------------------------------------------------------------

# Keep listeners alive (UNO holds weak refs). Form-level: one pair per document.
# File Open XFilter.filter() runs on Dummy-2 while main waits (Yellow; same as
# issue #402). GlobalEventBroadcaster OnViewCreated during load is Dummy-3.
# ensure_form_design_mode_off, prune_dead_listeners, and
# wire_all_notebook_run_buttons must run there; decorating them breaks
# WRITERAGENT_UNO_THREAD_GUARD=1 File Open (filter returns False → General I/O
# error). Do not marshal with main.call from the filter (host
# waiting = deadlock #402). ApplyFormDesignMode=False must happen before the
# filter returns. Listener methods stay undecorated (LO already invokes those
# on the UI thread; docs/framework/uno-thread-safety.md §A4). Release OXT still
# strips remaining decorators. Keep @main_thread_only on bootstrap install.
# _lock serializes _listener_refs / _warned_no_container mutations (Dummy-2
# XFilter and Dummy-3 OnViewCreated can overlap) and the one-shot global
# _doc_listener install. Do not hold it across UNO (getFormController,
# addActionListener, uno_same).
_listener_refs: list[Any] = []
_warned_no_container: set[str] = set()

_doc_listener: Any | None = None
_lock = threading.Lock()


def ensure_form_design_mode_off(doc: Any) -> None:
    """Form controls only fire when design mode is off (user mode).

    ▶ are UNO form CommandButtons.
    Design Mode on = click shows move/resize handles; off = click runs the cell.
    File Open import filter runs ensure_form_design_mode_off before the window/controller exists, so setFormDesignMode no-ops.
    ApplyFormDesignMode=False is the load-time switch so the view that attaches after the filter returns is in user mode.
    setFormDesignMode(False) still needed when a controller already exists (Import into an open doc).
    """
    def _off() -> None:
        # Load-time switch for File Open (runs before view/controller exists)
        doc.ApplyFormDesignMode = False

        # Runtime switch for Menu Import (runs when controller already exists)
        controller = doc.getCurrentController()
        if controller is not None and hasattr(controller, "setFormDesignMode"):
            controller.setFormDesignMode(False)

    best_effort(_off, "notebook controls: setFormDesignMode failed")


def _query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    return obj.queryInterface(uno.getTypeByName(typename))


def _hex_id_from_control_name(name: str) -> str | None:
    if not isinstance(name, str) or not name.startswith(RUN_PREFIX):
        return None
    hex_id = name[len(RUN_PREFIX) :]
    return hex_id or None


def _control_name(obj: Any) -> str:
    if obj is None:
        return ""
    model = obj
    get_model = getattr(obj, "getModel", None)
    if callable(get_model):
        maybe = get_model()
        if maybe is not None:
            model = maybe
    return str(getattr(model, "Name", "") or "")


def _hex_id_from_event(rEvent: Any) -> str | None:
    cmd = str(getattr(rEvent, "ActionCommand", "") or "")
    hex_id = _hex_id_from_control_name(cmd)
    if hex_id:
        return hex_id
    return _hex_id_from_control_name(_control_name(getattr(rEvent, "Source", None)))


def form_run_listeners(doc: Any | None = None) -> list[Any]:
    """Shared form-level ▶ listeners (tests fire these with a real ActionEvent).

    Pass *doc* to exclude leftover import-filter / leftover-reuse Writers
    (GHA 34643210006).
    """
    out = [lis for lis in _listener_refs if isinstance(lis, NotebookFormRunListener)]
    if doc is None:
        return out
    key = doc_key(doc)
    return [lis for lis in out if getattr(lis, "_doc_key_val", None) == key]


def prune_dead_listeners() -> None:
    """Remove listeners whose target document is closed/gone."""
    from plugin.framework.queue_executor import main

    if not main.is_current():
        return
    with _lock:
        refs = list(_listener_refs)
    survivors = [
        lis
        for lis in refs
        if best_effort(lis._resolve_doc, "notebook controls: prune resolve failed") is not None
    ]
    with _lock:
        # Dummy-2 prune must not drop a Dummy-3 wire_all append that landed
        # after the snapshot.
        added = [lis for lis in _listener_refs if lis not in refs]
        _listener_refs[:] = survivors + added


class NotebookFormRunListener(BaseActionListener):
    """One listener for every ``nb_run_*`` PUSH button on a document."""

    _ctx: Any
    _doc_key_val: str
    _runtime_uid: str
    _attached_names: set[str]

    def __init__(self, ctx: Any, doc: Any) -> None:
        self._ctx = ctx
        self._doc_key_val = doc_key(doc)
        self._runtime_uid = runtime_uid_from_doc_key(self._doc_key_val)
        self._attached_names = set()

    def _resolve_doc(self) -> Any | None:
        from plugin.framework.uno_context import resolve_document_by_url

        if not self._runtime_uid:
            return None
        resolved = best_effort(
            functools.partial(resolve_document_by_url, self._ctx, self._runtime_uid),
            "notebook controls: runtime uid resolution failed",
        )
        if resolved is not None:
            doc, _doc_type = resolved
            if doc is not None:
                return doc

        return None

    def on_action_performed(self, rEvent: Any) -> None:
        hex_id = _hex_id_from_event(rEvent)
        if not hex_id:
            return
        doc = self._resolve_doc()
        if doc is None:
            log.warning("notebook run button: document gone")
            return
        from plugin.notebook.notebook_runner import run_cell_for_doc_hex

        run_cell_for_doc_hex(self._ctx, doc, hex_id)


class NotebookFormContainerListener(BaseContainerListener):
    """When the form view realizes another control, attach the shared ▶ listener."""

    _form_listener: NotebookFormRunListener
    _doc_key_val: str
    _container: Any

    def __init__(self, form_listener: NotebookFormRunListener, container: Any) -> None:
        self._form_listener = form_listener
        self._doc_key_val = form_listener._doc_key_val
        self._container = container

    def _resolve_doc(self) -> Any | None:
        return self._form_listener._resolve_doc()

    def on_element_inserted(self, Event: Any) -> None:
        elem = getattr(Event, "Element", None)
        _attach_action_listener(elem, self._form_listener)

    def _forget_attached_name(self, elem: Any, what: str) -> None:
        if elem is None:
            return
        name = best_effort(lambda: _control_name(elem), what)
        if name:
            self._form_listener._attached_names.discard(name)

    def on_element_removed(self, Event: Any) -> None:
        self._forget_attached_name(getattr(Event, "Element", None), "notebook controls: failed to read removed control name")

    def on_element_replaced(self, Event: Any) -> None:
        self._forget_attached_name(getattr(Event, "ReplacedElement", None), "notebook controls: failed to read replaced control name")
        _attach_action_listener(getattr(Event, "Element", None), self._form_listener)

    def on_disposing(self, Source: Any) -> None:
        with _lock:
            pair = {self, self._form_listener}
            _listener_refs[:] = [lis for lis in _listener_refs if lis not in pair]


def _attach_action_listener(control: Any, listener: NotebookFormRunListener) -> bool:
    if control is None:
        return False

    def _attach() -> bool:
        # Tip: read name inside best_effort so a disposed control does not abort wiring the remaining controls.
        name = _control_name(control)
        hex_id = _hex_id_from_control_name(name)
        if not hex_id:
            return False
        if name in listener._attached_names:
            return True
        btn = _query_interface(control, "com.sun.star.awt.XButton")
        if btn is not None:
            btn.addActionListener(listener)
        elif hasattr(control, "addActionListener"):
            control.addActionListener(listener)
        else:
            return False
        listener._attached_names.add(name)
        return True

    return best_effort(_attach, "notebook controls: form attach failed") is True


def _form_and_container(doc: Any) -> tuple[Any | None, Any | None]:
    """Return ``(form_controller, view_container)`` or ``(None, None)``."""
    try:
        controller = doc.getCurrentController()
        if controller is None:
            return None, None
        forms = None
        if hasattr(doc, "getDrawPage"):
            forms = doc.getDrawPage().getForms()
        if forms is None:
            return None, None
        count = getattr(forms, "getCount", lambda: 0)()
        if count < 1:
            return None, None

        target_form = None
        for i in range(count):
            form = forms.getByIndex(i)
            elem_count = getattr(form, "getCount", lambda: 0)()
            for j in range(elem_count):
                name = getattr(form.getByIndex(j), "Name", "")
                if name and name.startswith(RUN_PREFIX):
                    target_form = form
                    break
            if target_form is not None:
                break
        if target_form is None:
            target_form = forms.getByIndex(0)

        if target_form is not None:
            fc = None
            if hasattr(controller, "getFormController"):
                fc = controller.getFormController(target_form)
            if fc is None:
                access = _query_interface(controller, "com.sun.star.view.XFormLayerAccess")
                if access is not None:
                    fc = access.getFormController(target_form)
            if fc is not None:
                container = fc.getContainer() if hasattr(fc, "getContainer") else None
                if container is not None:
                    return fc, container
        return None, None
    except Exception:
        log.debug("notebook controls: form controller lookup failed", exc_info=True)
        return None, None


def _detach_wiring(container: Any, listener: Any, container_lis: Any) -> None:
    best_effort(
        lambda: container.removeContainerListener(container_lis),
        "notebook controls: removeContainerListener failed",
    )

    def _remove_actions() -> None:
        controls = container.getControls() if hasattr(container, "getControls") else ()
        for control in controls or ():
            if hasattr(control, "removeActionListener"):
                control.removeActionListener(listener)

    best_effort(_remove_actions, "notebook controls: removeActionListener failed")


def forget_document_wiring(doc: Any) -> None:
    """Drop ▶ listeners and registry bookkeeping for *doc* on unload."""
    if doc is None:
        return
    key = doc_key(doc)
    with _lock:
        _warned_no_container.discard(key)
        to_drop = [lis for lis in _listener_refs if getattr(lis, "_doc_key_val", None) == key]
    if not to_drop:
        return
    for lis in to_drop:
        if isinstance(lis, NotebookFormContainerListener):
            best_effort(
                functools.partial(_detach_wiring, lis._container, lis._form_listener, lis),
                "notebook controls: detach wiring on unload failed",
            )
    with _lock:
        drop_set = set(to_drop)
        _listener_refs[:] = [lis for lis in _listener_refs if lis not in drop_set]


def _container_wired(refs: list[Any], key: str, container: Any | None = None) -> list[Any]:
    """Return matching container listeners from ``refs``.

    Does not acquire ``_lock`` and does not read ``_listener_refs``.
    ``container is None`` skips ``uno_same``.
    """
    if container is None:
        return [
            lis
            for lis in refs
            if isinstance(lis, NotebookFormContainerListener) and lis._doc_key_val == key
        ]
    from plugin.framework.uno_context import uno_same

    return [
        lis
        for lis in refs
        if isinstance(lis, NotebookFormContainerListener)
        and lis._doc_key_val == key
        and uno_same(lis._container, container)
    ]


def _claim_container(
    key: str,
    container: Any | None = None,
    listener: Any | None = None,
    container_lis: Any | None = None,
) -> bool:
    """True if a snapshot match is still in ``_listener_refs``.

    Bugfix: Previously, container comparisons ran under ``_lock``.
    ``uno_same`` falls back to ``==`` and ``uno.isSame`` UNO calls, while
    ``on_disposing`` and document-event handlers take ``_lock`` when already
    holding SolarMutex, creating a lock-order inversion risk.
    To fix this, we snapshot ``_listener_refs`` under ``_lock``, compare
    containers outside ``_lock`` with ``uno_same``, and recheck listener
    identity under ``_lock``. If another caller appended to ``_listener_refs``
    during comparison, we retry; otherwise, if ``listener`` and ``container_lis``
    are provided and no live match was found, we append both before returning False.
    """
    while True:
        with _lock:
            refs = list(_listener_refs)
        matches = _container_wired(refs, key, container)
        with _lock:
            if any(lis in _listener_refs for lis in matches):
                return True
            if any(lis not in refs for lis in _listener_refs):
                continue
            if listener is not None and container_lis is not None:
                _listener_refs.append(listener)
                _listener_refs.append(container_lis)
                _warned_no_container.discard(key)
            return False


def wire_all_notebook_run_buttons(ctx: Any, doc: Any, new_view: bool = True) -> int:
    """Attach the shared form-level ▶ listener if missing. Returns 1 when wired.

    Does **not** call ``controller.getControl(model)`` per code cell.
    """
    key = doc_key(doc)
    if not new_view:
        if _claim_container(key):
            ensure_form_design_mode_off(doc)
            return 1

    state = load_registry(doc)
    if state is None or not state.code_cells:
        return 0

    prune_dead_listeners()
    ensure_form_design_mode_off(doc)
    t0 = time.monotonic()
    _fc, container = _form_and_container(doc)
    if container is None:
        with _lock:
            warn = key not in _warned_no_container
            if warn:
                _warned_no_container.add(key)
        if warn:
            log.warning("notebook controls: no form controller container; ▶ clicks will not run (%d code cells)", len(state.code_cells))
        return 0

    if _claim_container(key, container):
        log.debug("notebook controls: form listener already attached doc=%s", key)
        return 1

    listener = NotebookFormRunListener(ctx, doc)
    attached = 0
    def _attach_existing() -> None:
        nonlocal attached
        controls = container.getControls() if hasattr(container, "getControls") else ()
        for control in controls or ():
            if _attach_action_listener(control, listener):
                attached += 1

    best_effort(_attach_existing, "notebook controls: getControls attach failed")

    container_lis = NotebookFormContainerListener(listener, container)
    try:
        container.addContainerListener(container_lis)
    except Exception:
        log.warning("notebook controls: addContainerListener failed", exc_info=True)
        _detach_wiring(container, listener, container_lis)
        return 0

    if _claim_container(key, container, listener, container_lis):
        # File Open (Dummy-2) and view creation can both get past the first
        # check. The other call kept its listener; take ours back off so one
        # ▶ click does not run the cell twice.
        _detach_wiring(container, listener, container_lis)
        log.debug("notebook controls: lost wiring race; detached duplicate doc=%s", key)
        return 1
    elapsed_ms = mono_ms(t0)
    log.info("notebook import attach_form_listener elapsed_ms=%d attached_views=%d code_cells=%d", elapsed_ms, attached, len(state.code_cells))
    return 1


def _install_doc_event_listener(ctx: Any) -> None:
    """Attach a global listener so documents/views opened AFTER bootstrap get the menu too."""
    global _doc_listener
    with _lock:
        if _doc_listener is not None:
            return
    try:

        class NotebookDocumentEventListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            _ctx: Any

            def __init__(self, ctx: Any) -> None:
                self._ctx = ctx

            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
                try:
                    name = getattr(Event, "EventName", "") or ""
                    if name not in ("OnViewCreated", "OnLoadFinished", "OnLoad", "OnNew", "OnUnload"):
                        return

                    controller = getattr(Event, "ViewController", None)
                    doc = controller.getModel() if controller else getattr(Event, "Source", None)

                    if name == "OnUnload":
                        if doc is not None:
                            forget_document_wiring(doc)
                        return

                    if doc is not None:
                        is_text_doc = uno_read(
                            lambda: bool(doc.supportsService("com.sun.star.text.TextDocument")),
                            True,
                        )
                        if not is_text_doc:
                            return
                        # Tip: File Open wires inside XFilter.filter() before the form controller
                        # exists, so this is the retry. These events fire again after a wire
                        # (load completion, Save As); wire_all must stay idempotent or one ▶ click
                        # runs the cell twice.
                        wire_all_notebook_run_buttons(self._ctx, doc, new_view=(name == "OnViewCreated"))
                except Exception:
                    log.warning("notebook controls: doc-event handling failed", exc_info=True)

        smgr = ctx.getServiceManager()
        broadcaster = smgr.createInstanceWithContext("com.sun.star.frame.GlobalEventBroadcaster", ctx)
        listener = NotebookDocumentEventListener(ctx)
        broadcaster.addDocumentEventListener(listener)
        with _lock:
            _doc_listener = listener
        log.debug("notebook controls: global doc-event listener attached")
    except Exception:
        log.warning("notebook controls: doc-event listener install failed", exc_info=True)


@main_thread_only
def install_notebook_run_button_wiring(ctx: Any) -> None:
    """Bootstrap: wire ▶ buttons on the active Writer document (if any)."""
    try:
        from plugin.framework.doc_type import is_writer
        from plugin.framework.uno_context import get_active_document

        doc = get_active_document(ctx)
        if doc is not None and is_writer(doc):
            wire_all_notebook_run_buttons(ctx, doc)

        _install_doc_event_listener(ctx)
    except Exception:
        log.debug("notebook controls: install wiring failed", exc_info=True)
