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
from com.sun.star.awt import Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.framework.i18n import _
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseActionListener, BaseContainerListener, BaseDocumentEventListener
from plugin.notebook.cell_registry import (
    _coerce_notebook_text,
    _prepare_display_text,
    cell_id_to_hex,
    has_notebook_registry,
    load_registry,
)

log = logging.getLogger("writeragent.notebook")

# com.sun.star.form.FormButtonType.PUSH — URL buttons open TargetURL via desktop, not our handler.
_FORM_BUTTON_PUSH = 0

_RUN_PREFIX = "nb_run_"

# 1/100 mm — code field width falls back when page style is unavailable.
_DEFAULT_WIDTH = 14000
_MIN_FIELD_HEIGHT = 500
_LINE_HEIGHT = 450
_FIELD_HEIGHT_PAD = 280
_WRAP_SLACK = _LINE_HEIGHT
_MAX_FIELD_HEIGHT = 24000
_RUN_BUTTON_SIZE = 320
_SLOW_ADD_MS = 2000
_CODE_FONT_NAME = "Liberation Mono"
_CODE_FONT_HEIGHT = 10
_CODE_FIELD_BG = 0xF7F7F7
_CODE_FIELD_BORDER = 0xD0D0D0

__all__ = [
    "form_button_push_type",
    "ensure_form_design_mode_off",
    "get_control_view_for_model",
    "wired_run_listener_count",
    "form_run_listeners",
    "prune_dead_listeners",
    "NotebookRunButtonListener",
    "NotebookFormContainerListener",
    "NotebookFormRunListener",
    "wire_run_button_listener",
    "wire_all_notebook_run_buttons",
    "install_notebook_run_button_wiring",
    "_resolve_para_style",
    "_page_style",
    "_text_area_width_units",
    "_max_field_height_units",
    "_height_for_text",
    "_anchor_control_as_character",
    "_set_model_prop",
    "_style_code_field_model",
    "_style_run_button_model",
    "_style_control_paragraph",
    "_log_shape_add",
    "_insert_run_button_in_flow",
    "_insert_code_input_in_flow",
    "_DEFAULT_WIDTH",
    "_MIN_FIELD_HEIGHT",
    "_LINE_HEIGHT",
    "_FIELD_HEIGHT_PAD",
    "_WRAP_SLACK",
    "_MAX_FIELD_HEIGHT",
    "_RUN_BUTTON_SIZE",
    "_SLOW_ADD_MS",
    "_CODE_FONT_NAME",
    "_CODE_FONT_HEIGHT",
    "_CODE_FIELD_BG",
    "_CODE_FIELD_BORDER",
]


# ---------------------------------------------------------------------------
# Layout & Page Sizing
# ---------------------------------------------------------------------------

def _mono_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _resolve_para_style(doc: Any, style_name: str | None) -> str | None:
    """Map English style label to a name that exists in this document (locale-safe)."""
    if not style_name:
        return None
    try:
        para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        if para_styles.hasByName(style_name):
            return style_name
        lower = style_name.lower()
        for name in para_styles.getElementNames():
            if name.lower() == lower:
                return name
    except Exception:
        log.debug("notebook controls: could not enumerate ParagraphStyles for %r", style_name)
    return None


def _page_style(doc: Any | None) -> Any | None:
    """Current page style, or Standard/Default fallback. None if unavailable."""
    if doc is None:
        return None
    try:
        families = doc.getStyleFamilies().getByName("PageStyles")
        name = ""
        try:
            name = str(doc.getPropertyValue("PageDescName") or "")
        except Exception:
            name = ""
        if name and families.hasByName(name):
            return families.getByName(name)
        for candidate in ("Standard", "Default", "Default Page Style"):
            if families.hasByName(candidate):
                return families.getByName(candidate)
    except Exception:
        log.debug("notebook controls could not read page style", exc_info=True)
    return None


def _text_area_width_units(doc: Any | None) -> int:
    """Page width minus left/right margins (1/100 mm), for code fields and images."""
    style = _page_style(doc)
    if style is None:
        return _DEFAULT_WIDTH
    try:
        page_w = int(style.getPropertyValue("Width"))
        left = int(style.getPropertyValue("LeftMargin"))
        right = int(style.getPropertyValue("RightMargin"))
        return max(6000, page_w - left - right)
    except Exception:
        log.debug("notebook controls could not read page text-area width", exc_info=True)
        return _DEFAULT_WIDTH


def _max_field_height_units(doc: Any | None) -> int:
    """Cap AS_CHARACTER code fields at roughly the page body height."""
    style = _page_style(doc)
    if style is None:
        return _MAX_FIELD_HEIGHT
    try:
        page_h = int(style.getPropertyValue("Height"))
        top = int(style.getPropertyValue("TopMargin"))
        bottom = int(style.getPropertyValue("BottomMargin"))
        # Leave room for the In [n]: gutter on the same page when possible.
        return max(_MIN_FIELD_HEIGHT, page_h - top - bottom - 1500)
    except Exception:
        log.debug("notebook controls could not read page text-area height", exc_info=True)
        return _MAX_FIELD_HEIGHT


def _height_for_text(text: str, doc: Any | None = None) -> int:
    """Shape height in 1/100 mm so every source line is visible (no clipped last line)."""
    lines = 0
    for line in (text or "").split("\n"):
        lines += max(1, (len(line) + 79) // 80)
    lines = max(1, lines)
    raw = lines * _LINE_HEIGHT + _FIELD_HEIGHT_PAD + _WRAP_SLACK
    cap = _max_field_height_units(doc)
    return max(_MIN_FIELD_HEIGHT, min(cap, raw))


# ---------------------------------------------------------------------------
# Form Model Styling & Flow Insertion
# ---------------------------------------------------------------------------

def _anchor_control_as_character(shape: Any) -> None:
    """In-flow control: AS_CHARACTER, top-aligned, no wrap beside the next shape."""
    shape.setPropertyValue("AnchorType", AS_CHARACTER)
    try:
        shape.setPropertyValue("VertOrient", 1)
    except Exception:
        log.debug("notebook controls VertOrient not applied", exc_info=True)
    try:
        shape.setPropertyValue("TextWrap", 0)
    except Exception:
        log.debug("notebook controls TextWrap not applied", exc_info=True)


def _set_model_prop(model: Any, name: str, value: Any) -> None:
    try:
        model.setPropertyValue(name, value)
        return
    except Exception:
        pass
    try:
        setattr(model, name, value)
    except Exception:
        log.debug("notebook controls could not set model %s", name, exc_info=True)


def _style_code_field_model(model: Any) -> None:
    """Jupyter-like code box: light gray fill, hairline border, Liberation Mono."""
    _set_model_prop(model, "MultiLine", True)
    _set_model_prop(model, "FontName", _CODE_FONT_NAME)
    _set_model_prop(model, "FontHeight", _CODE_FONT_HEIGHT)
    _set_model_prop(model, "BackgroundColor", _CODE_FIELD_BG)
    _set_model_prop(model, "Border", 2)
    _set_model_prop(model, "BorderColor", _CODE_FIELD_BORDER)
    _set_model_prop(model, "VScroll", False)
    _set_model_prop(model, "AutoVScroll", False)


def _style_run_button_model(model: Any) -> None:
    """Small ▶ without a fat 3D square around it."""
    _set_model_prop(model, "Border", 0)
    _set_model_prop(model, "BackgroundColor", 0xFFFFFF)
    _set_model_prop(model, "FontHeight", 8)
    _set_model_prop(model, "FocusOnClick", False)
    _set_model_prop(model, "DefaultControl", "com.sun.star.form.control.CommandButton")


def _style_control_paragraph(doc: Any) -> None:
    """Field-only paragraph: full text-area width, no hanging indent for ▶."""
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        body = _resolve_para_style(doc, "Text Body")
        if body:
            cursor.setPropertyValue("ParaStyleName", body)
        cursor.setPropertyValue("ParaFirstLineIndent", 0)
        cursor.setPropertyValue("ParaLeftMargin", 0)
        cursor.setPropertyValue("ParaTopMargin", 0)
        cursor.setPropertyValue("ParaBottomMargin", 150)
        cursor.setPropertyValue("ParaKeepTogether", False)
        cursor.setPropertyValue("ParaKeepWithNext", False)
    except Exception:
        log.debug("notebook controls paragraph style failed", exc_info=True)


def _log_shape_add(
    *,
    step: str,
    name: str = "",
    text_chars: int = 0,
    truncated: bool = False,
    shape_h: int = 0,
    shapes_before: int,
    create_ms: int = 0,
    text_ms: int = 0,
    add_ms: int = 0,
    ok: bool = True,
) -> None:
    total_ms = create_ms + text_ms + add_ms
    log.debug(
        "notebook controls add step=%s name=%s text_chars=%d truncated=%s shape_h=%d shapes_before=%d "
        "create_ms=%d text_ms=%d add_ms=%d ok=%s",
        step,
        name,
        text_chars,
        truncated,
        shape_h,
        shapes_before,
        create_ms,
        text_ms,
        add_ms,
        ok,
    )
    if total_ms >= _SLOW_ADD_MS:
        log.warning(
            "notebook controls slow UNO add step=%s total_ms=%d shapes_before=%d",
            step,
            total_ms,
            shapes_before,
        )


def _insert_run_button_in_flow(
    doc: Any,
    *,
    cell_id: str,
    controls_before: int,
    ctx: Any | None = None,
) -> None:
    """In-flow ▶ on the ``In [n]:`` gutter paragraph (not the tall gray field)."""
    hex_id = cell_id_to_hex(cell_id)
    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.CommandButton")
    if model is None:
        raise RuntimeError("Failed to create form CommandButton")
    model.Name = f"nb_run_{hex_id}"
    model.Label = "\u25b6"
    if hasattr(model, "HelpText"):
        model.HelpText = _("Run code cell")
    model.ButtonType = form_button_push_type()
    _style_run_button_model(model)

    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape for run button")
    shape.setSize(Size(_RUN_BUTTON_SIZE, _RUN_BUTTON_SIZE))
    shape.Control = model
    _anchor_control_as_character(shape)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    t_add = time.monotonic()
    text.insertTextContent(cursor, shape, False)
    _log_shape_add(
        step="run_button",
        name=model.Name,
        shapes_before=controls_before,
        create_ms=_mono_ms(t0),
        add_ms=_mono_ms(t_add),
        shape_h=_RUN_BUTTON_SIZE,
    )


def _insert_code_input_in_flow(
    doc: Any,
    *,
    name: str,
    source: str,
    controls_before: int,
) -> None:
    """Editable code cell: form TextField anchored in document flow at body end."""
    display, truncated = _prepare_display_text(_coerce_notebook_text(source))
    raw_chars = len(source or "")

    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.TextField")
    if model is None:
        raise RuntimeError("Failed to create form TextField")
    model.Name = name
    if hasattr(model, "Label"):
        model.Label = "Code"
    _style_code_field_model(model)
    create_ms = _mono_ms(t0)

    t_text = time.monotonic()
    model.Text = display
    text_ms = _mono_ms(t_text)

    h = _height_for_text(display, doc)
    field_w = _text_area_width_units(doc)
    t_shape = time.monotonic()
    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape")
    shape.setSize(Size(field_w, h))
    shape.Control = model
    _anchor_control_as_character(shape)
    create_ms += _mono_ms(t_shape)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    t_add = time.monotonic()
    text.insertTextContent(cursor, shape, False)
    add_ms = _mono_ms(t_add)
    _log_shape_add(
        step="code_field",
        name=name,
        text_chars=raw_chars,
        truncated=truncated,
        shape_h=h,
        shapes_before=controls_before,
        create_ms=create_ms,
        text_ms=text_ms,
        add_ms=add_ms,
    )


# ---------------------------------------------------------------------------
# Runtime Mode & Event Wiring
# ---------------------------------------------------------------------------

# Keep listeners alive (UNO holds weak refs). Form-level: one pair per document.
# File Open XFilter.filter() runs on Dummy-2 while main waits (Yellow; same as
# issue #402). GlobalEventBroadcaster OnViewCreated during load is Dummy-3.
# ensure_form_design_mode_off, prune_dead_listeners, and
# wire_all_notebook_run_buttons must run there; decorating them breaks
# WRITERAGENT_UNO_THREAD_GUARD=1 File Open (filter returns False → General I/O
# error). Do not marshal with execute_on_main_thread from the filter (host
# waiting = deadlock #402). ApplyFormDesignMode=False must happen before the
# filter returns. Listener methods stay undecorated (LO already invokes those
# on the UI thread; docs/framework/uno-thread-safety.md §A4). Release OXT still
# strips remaining decorators. Keep @main_thread_only on getControl /
# per-button wire / bootstrap install. _lock serializes _listener_refs /
# _wired_keys / _wired_form_docs mutations (Dummy-2 XFilter and Dummy-3
# OnViewCreated can overlap) and the one-shot global _doc_listener install.
# Do not hold it across UNO (getFormController, addActionListener).
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

    Untitled Writer docs have an empty URL, so ``id(doc)`` was used. Import
    and bootstrap ``install_notebook_run_button_wiring`` can wire on
    different PyUNO wrappers of the same document — one ▶ click then ran
    the cell multiple times (``[In [4]]`` jumped to ``[In [7]]``).
    ``RuntimeUID`` is the same object for every wrapper of that document.
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


def wired_run_listener_count(hex_id: str, doc: Any | None = None) -> int:
    """How many live ▶ handlers would run *hex_id* (form-level counts as one).

    GHA 34643210006: leftover HTML-paste Writers left leftover_open>0, so
    ``close_doc`` skipped import-filter ``_wa_notebook`` leftovers. Global
    ``_listener_refs`` then counted those leftover form listeners
    (``duplicate listeners: 3``). Pass *doc* to count only that document.
    A leftover doc's listener does not fire this doc's ▶.
    """
    doc_key = _doc_key(doc) if doc is not None else None
    n = 0
    for lis in _listener_refs:
        if doc_key is not None and getattr(lis, "_doc_key_val", None) != doc_key:
            continue
        if getattr(lis, "_form_level", False):
            n += 1
        elif getattr(lis, "_hex_id", None) == hex_id:
            n += 1
    return n


def form_run_listeners(doc: Any | None = None) -> list[Any]:
    """Shared form-level ▶ listeners (tests fire these with a real ActionEvent).

    Pass *doc* to exclude leftover import-filter / leftover-reuse Writers
    (GHA 34643210006).
    """
    out = [lis for lis in _listener_refs if getattr(lis, "_form_level", False)]
    if doc is None:
        return out
    key = _doc_key(doc)
    return [lis for lis in out if getattr(lis, "_doc_key_val", None) == key]


@main_thread_only
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


def _record_listener_keys(
    lis: Any,
    survivor_keys: set[tuple[str, str]],
    survivor_forms: set[str],
) -> None:
    """Classify a live listener into form-doc vs per-button key sets.

    ``NotebookFormContainerListener`` is not form-level (that flag would
    double-count in ``wired_run_listener_count``) and has ``_hex_id is None``.
    """
    if getattr(lis, "_form_level", False):
        survivor_forms.add(lis._doc_key_val)
        return
    hex_id = getattr(lis, "_hex_id", None)
    if hex_id:
        survivor_keys.add((lis._doc_key_val, hex_id))


def prune_dead_listeners() -> None:
    """Remove listeners whose target document is closed/gone."""
    global _listener_refs, _wired_keys, _wired_form_docs
    with _lock:
        refs = list(_listener_refs)
    survivors: list[Any] = []
    survivor_keys: set[tuple[str, str]] = set()
    survivor_forms: set[str] = set()
    for lis in refs:
        try:
            doc = lis._resolve_doc()
        except Exception:
            # Was ``except Exception: pass`` after survivors.append, which hid
            # AttributeError on the container listener's missing ``_hex_id``
            # and any real ``_resolve_doc`` failure.
            log.debug("notebook controls: prune resolve failed", exc_info=True)
            continue
        if doc is None:
            continue
        survivors.append(lis)
        _record_listener_keys(lis, survivor_keys, survivor_forms)
    with _lock:
        # Dummy-2 prune must not drop a Dummy-3 wire_all append that landed
        # after the snapshot.
        added = [lis for lis in _listener_refs if lis not in refs]
        _listener_refs = survivors + added
        _wired_keys = survivor_keys
        _wired_form_docs = survivor_forms
        for lis in added:
            _record_listener_keys(lis, _wired_keys, _wired_form_docs)


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
        # Not a per-button listener. prune_dead_listeners used to treat the
        # missing attribute as a button key and raise AttributeError.
        self._hex_id: str | None = None

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


@main_thread_only
def wire_run_button_listener(ctx: Any, doc: Any, model: Any, hex_id: str) -> bool:
    """Attach ``XActionListener`` to a ▶ button model's view. Returns True on success.

    Import no longer calls this in a loop (``getControl`` per cell). Kept for unit
    tests and a single-button fallback.
    """
    prune_dead_listeners()
    key = (_doc_key(doc), hex_id)
    with _lock:
        if key in _wired_keys:
            return True
        _wired_keys.add(key)
    control = get_control_view_for_model(doc, model)
    if control is None:
        with _lock:
            _wired_keys.discard(key)
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
            with _lock:
                _wired_keys.discard(key)
            return False
        with _lock:
            _listener_refs.append(listener)
        log.debug("notebook controls: wired nb_run_%s", hex_id)
        return True
    except Exception:
        with _lock:
            _wired_keys.discard(key)
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
    with _lock:
        if doc_key in _wired_form_docs:
            log.debug("notebook controls: form listener already attached doc=%s", doc_key)
            return 1
        _wired_form_docs.add(doc_key)

    t0 = time.monotonic()
    _fc, container = _form_and_container(doc)
    if container is None:
        with _lock:
            _wired_form_docs.discard(doc_key)
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

    with _lock:
        _listener_refs.append(listener)
        if container_lis is not None:
            _listener_refs.append(container_lis)
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


@main_thread_only
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
