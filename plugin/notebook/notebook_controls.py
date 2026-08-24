# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wire notebook ▶ buttons to run handlers (form URL buttons do not reach ProtocolHandler)."""

from __future__ import annotations

import logging
from typing import Any

import uno

from plugin.framework.uno_listeners import BaseActionListener
from plugin.notebook.cell_registry import cell_id_to_hex, has_notebook_registry, load_registry
from plugin.notebook.form_lookup import index_form_control_models

log = logging.getLogger("writeragent.notebook")

# com.sun.star.form.FormButtonType.PUSH — URL buttons open TargetURL via desktop, not our handler.
_FORM_BUTTON_PUSH = 0

# Keep listeners alive (UNO holds weak refs). Key: (doc_id, hex_id).
_listener_refs: list[Any] = []
_wired_keys: set[tuple[int, str]] = set()


def form_button_push_type() -> int:
    return _FORM_BUTTON_PUSH


def ensure_form_design_mode_off(doc: Any) -> None:
    """Form controls only fire when design mode is off (user mode)."""
    try:
        controller = doc.getCurrentController()
        if controller is not None and hasattr(controller, "setFormDesignMode"):
            controller.setFormDesignMode(False)
    except Exception:
        log.debug("notebook controls: setFormDesignMode failed", exc_info=True)


def _query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    return obj.queryInterface(uno.getTypeByName(typename))


def _doc_key(doc: Any) -> int:
    try:
        url = doc.getURL()
        if url:
            return hash(url)
    except Exception:
        pass
    return id(doc)


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


class NotebookRunButtonListener(BaseActionListener):
    """Run one notebook cell when the ▶ push button is pressed."""

    def __init__(self, ctx: Any, doc: Any, hex_id: str) -> None:
        self._ctx = ctx
        self._hex_id = hex_id
        # PyUNO document components do not support weakref; keep a strong ref (listeners live in _listener_refs).
        self._doc = doc
        try:
            self._doc_url = str(doc.getURL() or "")
        except Exception:
            self._doc_url = ""

    def _resolve_doc(self) -> Any | None:
        if self._doc_url:
            from plugin.framework.uno_context import resolve_document_by_url

            doc, _doc_type = resolve_document_by_url(self._ctx, self._doc_url)
            if doc is not None:
                return doc
        return self._doc

    def on_action_performed(self, rEvent: Any) -> None:
        doc = self._resolve_doc()
        if doc is None:
            log.warning("notebook run button: document gone")
            return
        from plugin.notebook.notebook_runner import run_cell_for_doc_hex

        run_cell_for_doc_hex(self._ctx, doc, self._hex_id)


def wire_run_button_listener(ctx: Any, doc: Any, model: Any, hex_id: str) -> bool:
    """Attach ``XActionListener`` to a ▶ button model's view. Returns True on success."""
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
    """Wire every ``nb_run_*`` control listed in the notebook registry. Returns count wired."""
    if not has_notebook_registry(doc):
        return 0
    state = load_registry(doc)
    if state is None:
        return 0
    from plugin.notebook.writer_importer import flush_ui_idle

    ensure_form_design_mode_off(doc)
    flush_ui_idle(ctx)
    models_by_name = index_form_control_models(doc)
    log.debug("notebook controls: indexed %d form control model(s)", len(models_by_name))
    wired = 0
    missing_model = 0
    no_view = 0
    for cell in state.code_cells:
        hex_id = cell_id_to_hex(cell.cell_id)
        name = f"nb_run_{hex_id}"
        model = models_by_name.get(name)
        if model is None:
            missing_model += 1
            log.debug("notebook controls: model %r not found in document", name)
            continue
        if wire_run_button_listener(ctx, doc, model, hex_id):
            wired += 1
        else:
            no_view += 1
    code_cells = len(state.code_cells)
    if wired:
        log.info("notebook controls: wired %d/%d run button(s)", wired, code_cells)
    elif code_cells:
        log.warning(
            "notebook controls: wired 0/%d run buttons (missing_model=%d no_view=%d); re-import after deploy",
            code_cells,
            missing_model,
            no_view,
        )
    return wired


def install_notebook_run_button_wiring(ctx: Any) -> None:
    """Bootstrap: wire ▶ buttons on the active Writer document (if any)."""
    try:
        from plugin.doc.doc_type import is_writer
        from plugin.framework.uno_context import get_active_document

        doc = get_active_document(ctx)
        if doc is None or not is_writer(doc):
            return
        wire_all_notebook_run_buttons(ctx, doc)
    except Exception:
        log.debug("notebook controls: install wiring failed", exc_info=True)
