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
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import uno

from plugin.framework.uno_listeners import BaseActionListener, BaseContainerListener, BaseDocumentEventListener
from plugin.notebook.cell_registry import has_notebook_registry, load_registry

log = logging.getLogger("writeragent.notebook")

# com.sun.star.form.FormButtonType.PUSH — URL buttons open TargetURL via desktop, not our handler.
_FORM_BUTTON_PUSH = 0

_RUN_PREFIX = "nb_run_"

# Keep listeners alive (UNO holds weak refs). Form-level: one pair per document.
_listener_refs: list[Any] = []
_wired_keys: set[tuple[str, str]] = set()
_wired_form_docs: set[str] = set()

_doc_listener: Any | None = None
_lock = threading.Lock()


def form_button_push_type() -> int:
    return _FORM_BUTTON_PUSH


def ensure_form_design_mode_off(doc: Any) -> None:
    """Form controls only fire when design mode is off (user mode).

    ▶ are UNO form CommandButtons.
    Design Mode on = click shows move/resize handles; off = click runs the cell.
    File Open import filter runs ensure_form_design_mode_off before the window/controller exists, so setFormDesignMode no-ops.
    ApplyFormDesignMode=False is the load-time switch so the view that attaches after the filter returns is in user mode.
    setFormDesignMode(False) still needed when a controller already exists (Import into an open doc).
    """
    try:
        # Load-time switch for File Open (runs before view/controller exists)
        doc.ApplyFormDesignMode = False

        # Runtime switch for Menu Import (runs when controller already exists)
        controller = doc.getCurrentController()
        if controller is not None and hasattr(controller, "setFormDesignMode"):
            controller.setFormDesignMode(False)
    except Exception:
        log.debug("notebook controls: setFormDesignMode failed", exc_info=True)


def _query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    return obj.queryInterface(uno.getTypeByName(typename))


def _doc_key(doc: Any) -> str:
    """Stable id for listener de-dupe across Python wrappers of the same document.

    Untitled Writer docs have an empty URL, so ``id(doc)`` was used. Import wires
    once, ``import_dialog`` wires again, and bootstrap
    ``install_notebook_run_button_wiring`` wires a third time — each with a
    different PyUNO wrapper — so one ▶ click ran the cell three times
    (``[In [4]]`` jumped to ``[In [7]]``). ``RuntimeUID`` is the same object
    for every wrapper of that document.
    """
    from plugin.framework.uno_context import get_runtime_uid

    uid = get_runtime_uid(doc)
    if uid:
        return f"uid:{uid}"
    try:
        url = doc.getURL()
    except Exception:
        url = None
    if isinstance(url, str) and url:
        return f"url:{url}"
    return f"id:{id(doc)}"


def _hex_id_from_control_name(name: str) -> str | None:
    if not isinstance(name, str) or not name.startswith(_RUN_PREFIX):
        return None
    hex_id = name[len(_RUN_PREFIX) :]
    return hex_id or None


def _control_name(obj: Any) -> str:
    if obj is None:
        return ""
    model = obj
    get_model = getattr(obj, "getModel", None)
    if callable(get_model):
        try:
            maybe = get_model()
            if maybe is not None:
                model = maybe
        except Exception:
            pass
    try:
        return str(getattr(model, "Name", "") or "")
    except Exception:
        return ""


def _hex_id_from_event(rEvent: Any) -> str | None:
    cmd = str(getattr(rEvent, "ActionCommand", "") or "")
    hex_id = _hex_id_from_control_name(cmd)
    if hex_id:
        return hex_id
    return _hex_id_from_control_name(_control_name(getattr(rEvent, "Source", None)))


def wired_run_listener_count(hex_id: str) -> int:
    """How many live ▶ handlers would run *hex_id* (form-level counts as one)."""
    n = 0
    for lis in _listener_refs:
        if getattr(lis, "_form_level", False):
            n += 1
        elif getattr(lis, "_hex_id", None) == hex_id:
            n += 1
    return n


def form_run_listeners() -> list[Any]:
    """Shared form-level ▶ listeners (tests fire these with a real ActionEvent)."""
    return [lis for lis in _listener_refs if getattr(lis, "_form_level", False)]


def get_control_view_for_model(doc: Any, model: Any) -> Any | None:
    """Resolve the live control view for a form model (required for listeners)."""
    try:
        controller = doc.getCurrentController()
        if controller is None:
            return None
        # SwXTextView exposes getControl on XControlAccess; PyUNO needs getTypeByName for QI.
        if hasattr(controller, "getControl"):
            try:
                view = controller.getControl(model)
                if view is not None:
                    return view
            except Exception:
                log.debug("notebook controls: controller.getControl failed", exc_info=True)
        access = _query_interface(controller, "com.sun.star.view.XControlAccess")
        if access is None:
            log.debug("notebook controls: controller has no XControlAccess")
            return None
        return access.getControl(model)
    except Exception:
        log.debug("notebook controls: getControl failed", exc_info=True)
        return None


def prune_dead_listeners() -> None:
    """Remove listeners whose target document is closed/gone."""
    global _listener_refs, _wired_keys, _wired_form_docs
    survivors: list[Any] = []
    survivor_keys: set[tuple[str, str]] = set()
    survivor_forms: set[str] = set()
    for lis in _listener_refs:
        try:
            doc = lis._resolve_doc()
            if doc is not None:
                survivors.append(lis)
                if getattr(lis, "_form_level", False):
                    survivor_forms.add(lis._doc_key_val)
                else:
                    survivor_keys.add((lis._doc_key_val, lis._hex_id))
        except Exception:
            pass
    _listener_refs = survivors
    _wired_keys = survivor_keys
    _wired_form_docs = survivor_forms


class NotebookRunButtonListener(BaseActionListener):
    """Run one notebook cell when the ▶ push button is pressed."""

    def __init__(self, ctx: Any, doc: Any, hex_id: str) -> None:
        self._ctx = ctx
        self._hex_id = hex_id
        self._form_level = False
        self._doc_key_val = _doc_key(doc)
        try:
            self._doc_url = str(doc.getURL() or "")
        except Exception:
            self._doc_url = ""
        from plugin.framework.uno_context import get_runtime_uid

        # Untitled Writer docs have an empty URL. Hidden native tests (and a
        # notebook that is not Desktop.getCurrentComponent) then failed
        # get_active_document; PyUNO wrappers usually cannot be weakref'd, so
        # ▶ looked wired but actionPerformed returned "document gone".
        self._runtime_uid = get_runtime_uid(doc) or ""
        try:
            import weakref

            self._doc_weak: Any | None = weakref.ref(doc)
        except Exception:
            self._doc_weak = None

    def _resolve_doc(self) -> Any | None:
        from plugin.framework.uno_context import resolve_document_by_url

        if self._doc_url:
            doc, _doc_type = resolve_document_by_url(self._ctx, self._doc_url)
            if doc is not None:
                return doc
        # Prefer a live wrapper before enumerating the desktop (unit tests and
        # prune_dead_listeners). PyUNO often cannot weakref; then use UID.
        weak = getattr(self, "_doc_weak", None)
        if callable(weak):
            ref_doc = weak()
            if ref_doc is not None:
                return ref_doc
        if self._runtime_uid:
            doc, _doc_type = resolve_document_by_url(self._ctx, self._runtime_uid)
            if doc is not None:
                return doc
        from plugin.framework.uno_context import get_active_document

        active = get_active_document(self._ctx)
        if active is not None and _doc_key(active) == self._doc_key_val:
            return active
        return None

    def on_action_performed(self, rEvent: Any) -> None:
        doc = self._resolve_doc()
        if doc is None:
            log.warning("notebook run button: document gone")
            return
        from plugin.notebook.notebook_runner import run_cell_for_doc_hex

        run_cell_for_doc_hex(self._ctx, doc, self._hex_id)


class NotebookFormRunListener(NotebookRunButtonListener):
    """One listener for every ``nb_run_*`` PUSH button on a document."""

    def __init__(self, ctx: Any, doc: Any) -> None:
        super().__init__(ctx, doc, hex_id="")
        self._form_level = True
        self._attached_names: set[str] = set()

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

    def __init__(self, form_listener: NotebookFormRunListener) -> None:
        self._form_listener = form_listener
        self._doc_key_val = form_listener._doc_key_val
        self._form_level = False

    def _resolve_doc(self) -> Any | None:
        return self._form_listener._resolve_doc()

    def on_element_inserted(self, Event: Any) -> None:
        elem = getattr(Event, "Element", None)
        _attach_action_listener(elem, self._form_listener)


def _attach_action_listener(control: Any, listener: NotebookFormRunListener) -> bool:
    if control is None:
        return False
    name = _control_name(control)
    hex_id = _hex_id_from_control_name(name)
    if not hex_id:
        return False
    if name in listener._attached_names:
        return True
    try:
        btn = None
        try:
            btn = _query_interface(control, "com.sun.star.awt.XButton")
        except Exception:
            btn = None
        if btn is not None:
            btn.addActionListener(listener)
        elif hasattr(control, "addActionListener"):
            control.addActionListener(listener)
        else:
            return False
        listener._attached_names.add(name)
        return True
    except Exception:
        log.debug("notebook controls: form attach failed for %s", name, exc_info=True)
        return False


def _form_and_container(doc: Any) -> tuple[Any | None, Any | None]:
    """Return ``(form_controller, view_container)`` or ``(None, None)``."""
    try:
        controller = doc.getCurrentController()
        if controller is None:
            return None, None
        forms = None
        if hasattr(doc, "getDrawPage"):
            try:
                forms = doc.getDrawPage().getForms()
            except Exception:
                forms = None
        if forms is None or getattr(forms, "getCount", lambda: 0)() < 1:
            return None, None
        form = forms.getByIndex(0)
        fc = None
        if hasattr(controller, "getFormController"):
            fc = controller.getFormController(form)
        if fc is None:
            access = _query_interface(controller, "com.sun.star.view.XFormLayerAccess")
            if access is not None:
                fc = access.getFormController(form)
        if fc is None:
            return None, None
        container = fc.getContainer() if hasattr(fc, "getContainer") else None
        return fc, container
    except Exception:
        log.debug("notebook controls: form controller lookup failed", exc_info=True)
        return None, None


def wire_run_button_listener(ctx: Any, doc: Any, model: Any, hex_id: str) -> bool:
    """Attach ``XActionListener`` to a ▶ button model's view. Returns True on success.

    Import no longer calls this in a loop (``getControl`` per cell). Kept for unit
    tests and a single-button fallback.
    """
    prune_dead_listeners()
    key = (_doc_key(doc), hex_id)
    if key in _wired_keys:
        return True
    control = get_control_view_for_model(doc, model)
    if control is None:
        log.debug("notebook controls: no view for button nb_run_%s", hex_id)
        return False
    try:
        listener = NotebookRunButtonListener(ctx, doc, hex_id)
        btn = _query_interface(control, "com.sun.star.awt.XButton")
        if btn is not None:
            btn.addActionListener(listener)
        elif hasattr(control, "addActionListener"):
            control.addActionListener(listener)
        else:
            log.warning("notebook controls: control has no addActionListener for nb_run_%s", hex_id)
            return False
        _listener_refs.append(listener)
        _wired_keys.add(key)
        log.debug("notebook controls: wired nb_run_%s", hex_id)
        return True
    except Exception:
        log.exception("notebook controls: wire failed for nb_run_%s", hex_id)
        return False


def wire_all_notebook_run_buttons(ctx: Any, doc: Any) -> int:
    """Attach the shared form-level ▶ listener if missing. Returns 1 when wired.

    Does **not** call ``controller.getControl(model)`` per code cell.
    """
    if not has_notebook_registry(doc):
        return 0
    state = load_registry(doc)
    if state is None:
        return 0
    prune_dead_listeners()
    doc_key = _doc_key(doc)
    ensure_form_design_mode_off(doc)
    if doc_key in _wired_form_docs:
        log.debug("notebook controls: form listener already attached doc=%s", doc_key)
        return 1

    t0 = time.monotonic()
    _fc, container = _form_and_container(doc)
    if container is None:
        log.warning(
            "notebook controls: no form controller container; ▶ clicks will not run (%d code cells)",
            len(state.code_cells),
        )
        return 0

    listener = NotebookFormRunListener(ctx, doc)
    attached = 0
    try:
        controls = container.getControls() if hasattr(container, "getControls") else ()
        for control in controls or ():
            if _attach_action_listener(control, listener):
                attached += 1
    except Exception:
        log.debug("notebook controls: getControls attach failed", exc_info=True)

    container_lis: NotebookFormContainerListener | None = NotebookFormContainerListener(listener)
    try:
        container.addContainerListener(container_lis)
    except Exception:
        log.debug("notebook controls: addContainerListener failed", exc_info=True)
        container_lis = None

    _listener_refs.append(listener)
    if container_lis is not None:
        _listener_refs.append(container_lis)
    _wired_form_docs.add(doc_key)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "notebook import attach_form_listener elapsed_ms=%d attached_views=%d code_cells=%d",
        elapsed_ms,
        attached,
        len(state.code_cells),
    )
    return 1


def _install_doc_event_listener(ctx: Any) -> None:
    """Attach a global listener so documents/views opened AFTER bootstrap get the menu too."""
    global _doc_listener
    with _lock:
        if _doc_listener is not None:
            return
    try:
        class NotebookDocumentEventListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            def __init__(self, ctx: Any) -> None:
                self._ctx = ctx

            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
                try:
                    name = getattr(Event, "EventName", "") or ""
                    if name not in ("OnViewCreated", "OnLoadFinished", "OnLoad", "OnNew"):
                        return

                    controller = getattr(Event, "ViewController", None)
                    doc = controller.getModel() if controller else getattr(Event, "Source", None)

                    if doc is not None and has_notebook_registry(doc):
                        # File Open wire_all runs inside XFilter.filter() before XFormLayerAccess.getFormController exists (_form_and_container returns None,None).
                        # Menu import has a live controller so the same call works.
                        # This listener is the retry once the view exists. wire_all is idempotent (_wired_form_docs).
                        ensure_form_design_mode_off(doc)
                        wire_all_notebook_run_buttons(self._ctx, doc)
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


def install_notebook_run_button_wiring(ctx: Any) -> None:
    """Bootstrap: wire ▶ buttons on the active Writer document (if any)."""
    try:
        from plugin.doc.doc_type import is_writer
        from plugin.framework.uno_context import get_active_document

        doc = get_active_document(ctx)
        if doc is not None and is_writer(doc):
            wire_all_notebook_run_buttons(ctx, doc)

        _install_doc_event_listener(ctx)
    except Exception:
        log.debug("notebook controls: install wiring failed", exc_info=True)
