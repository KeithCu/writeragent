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
import unicodedata

import uno
from com.sun.star.awt import Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.framework.errors import best_effort, uno_probe
from plugin.framework.i18n import _
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseActionListener, BaseContainerListener, BaseDocumentEventListener
from plugin.notebook.cell_registry import _coerce_notebook_text, cell_id_to_hex, load_registry

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
    "form_run_listeners",
    "prune_dead_listeners",
    "forget_document_wiring",
    "NotebookRunButtonListener",
    "NotebookFormContainerListener",
    "NotebookFormRunListener",
    "wire_all_notebook_run_buttons",
    "install_notebook_run_button_wiring",
    "_page_style",
    "_text_width_from_style",
    "_cap_from_style",
    "_text_area_width_units",
    "_max_field_height_units",
    "_height_for_text",
    "_raw_height_for_text",
    "_anchor_control_as_character",
    "_set_model_props",
    "_style_code_field_model",
    "_style_run_button_model",
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


def _page_style(doc: Any | None) -> Any | None:
    """Page style of the first body paragraph, else Standard/Default.

    A cursor at the start of the body text carries the first paragraph's
    PageDescName; the document model does not have this property.
    File Open has no view yet, so this is not the view cursor.
    """
    if doc is None:
        return None

    def _get() -> Any | None:
        families = doc.getStyleFamilies().getByName("PageStyles")
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        name = str(cursor.getPropertyValue("PageDescName") or "")
        if name and families.hasByName(name):
            return families.getByName(name)
        for candidate in ("Standard", "Default", "Default Page Style"):
            if families.hasByName(candidate):
                return families.getByName(candidate)
        return None

    return best_effort(_get, "notebook controls page style lookup failed")


def _text_width_from_style(style: Any | None) -> int:
    """Page width minus left/right margins (1/100 mm), falling back to _DEFAULT_WIDTH."""
    if style is None:
        return _DEFAULT_WIDTH

    def _calc() -> int:
        page_w = int(style.getPropertyValue("Width"))
        left = int(style.getPropertyValue("LeftMargin"))
        right = int(style.getPropertyValue("RightMargin"))
        return max(6000, page_w - left - right)

    val = best_effort(_calc, "notebook controls width from style failed")
    return val if val is not None else _DEFAULT_WIDTH


def _text_area_width_units(doc: Any | None) -> int:
    """Page width minus left/right margins (1/100 mm), for code fields and images."""
    style = _page_style(doc)
    return _text_width_from_style(style)


def _cap_from_style(style: Any | None) -> int:
    """Cap AS_CHARACTER code fields at roughly body height, falling back to _MAX_FIELD_HEIGHT."""
    if style is None:
        return _MAX_FIELD_HEIGHT

    def _calc() -> int:
        page_h = int(style.getPropertyValue("Height"))
        top = int(style.getPropertyValue("TopMargin"))
        bottom = int(style.getPropertyValue("BottomMargin"))
        # Leave room for the In [n]: gutter on the same page when possible.
        return max(_MIN_FIELD_HEIGHT, page_h - top - bottom - 1500)

    val = best_effort(_calc, "notebook controls cap from style failed")
    return val if val is not None else _MAX_FIELD_HEIGHT


def _max_field_height_units(doc: Any | None) -> int:
    """Cap AS_CHARACTER code fields at roughly the page body height."""
    style = _page_style(doc)
    return _cap_from_style(style)


def _line_display_cols(line: str, *, max_cols: int | None = None) -> int:
    if line.endswith("\r"):
        line = line[:-1]
    line = line.expandtabs(4)
    cols = 0
    for ch in line:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            cols += 2
        else:
            cols += 1
        if max_cols is not None and cols >= max_cols:
            return cols
    return cols


def _raw_height_for_text(
    text: str,
    doc: Any | None = None,
    *,
    field_w: int | None = None,
    cap: int | None = None,
) -> int:
    if field_w is None and doc is not None:
        field_w = _text_area_width_units(doc)
    if field_w is None:
        cols = 80
    else:
        char_width = int(_CODE_FONT_HEIGHT * 2540 * 0.6 / 72)
        cols = max(8, field_w // char_width)
    lines = 0
    base_pad = _FIELD_HEIGHT_PAD + _WRAP_SLACK
    # Stop line walk once height strictly passes cap so raw_h > cap remains true.
    max_lines_needed = max(1, (cap - base_pad) // _LINE_HEIGHT + 1) if cap is not None else None

    for line in (text or "").split("\n"):
        rem_lines = (max_lines_needed - lines) if max_lines_needed is not None else None
        max_cols = (rem_lines * cols) if rem_lines is not None and rem_lines > 0 else None
        cols_count = _line_display_cols(line, max_cols=max_cols)
        lines += max(1, (cols_count + cols - 1) // cols)
        if max_lines_needed is not None and lines >= max_lines_needed:
            break
    lines = max(1, lines)
    return lines * _LINE_HEIGHT + base_pad


def _height_for_text(text: str, doc: Any | None = None, *, field_w: int | None = None, cap: int | None = None) -> int:
    """Shape height in 1/100 mm so every source line is visible (no clipped last line)."""
    raw = _raw_height_for_text(text, doc, field_w=field_w, cap=cap)
    if cap is None:
        cap = _max_field_height_units(doc)
    return max(_MIN_FIELD_HEIGHT, min(cap, raw))


# ---------------------------------------------------------------------------
# Form Model Styling & Flow Insertion
# ---------------------------------------------------------------------------


def _anchor_control_as_character(shape: Any) -> None:
    """In-flow control: AS_CHARACTER, top-aligned, no wrap beside the next shape."""
    shape.setPropertyValue("AnchorType", AS_CHARACTER)
    shape.setPropertyValue("VertOrient", 1)
    shape.setPropertyValue("TextWrap", 0)


def _set_model_props(model: Any, pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...] | dict[str, Any]) -> None:
    """Set form-model properties querying getPropertySetInfo once. Skip names this model lacks.

    CommandButton has no ``Border``; the form TextField has no ``AutoVScroll``.
    A rejected value must not abort insertion of the code field.
    """
    info = best_effort(lambda: model.getPropertySetInfo(), "notebook controls getPropertySetInfo failed")
    if info is None:
        return
    items = pairs.items() if isinstance(pairs, dict) else pairs
    for name, value in items:
        if info.hasPropertyByName(name):
            best_effort(functools.partial(model.setPropertyValue, name, value), f"notebook controls could not set model {name}")


def _style_code_field_model(model: Any, *, vscroll: bool = False) -> None:
    """Jupyter-like code box: light gray fill, hairline border, Liberation Mono."""
    _set_model_props(
        model,
        [
            ("MultiLine", True),
            ("FontName", _CODE_FONT_NAME),
            ("FontHeight", _CODE_FONT_HEIGHT),
            ("BackgroundColor", _CODE_FIELD_BG),
            ("Border", 2),
            ("BorderColor", _CODE_FIELD_BORDER),
            ("VScroll", vscroll),
        ],
    )


def _style_run_button_model(model: Any) -> None:
    """Small ▶ without a fat 3D square around it."""
    _set_model_props(
        model,
        [
            ("Border", 0),
            ("BackgroundColor", 0xFFFFFF),
            ("FontHeight", 8),
            ("FocusOnClick", False),
            ("DefaultControl", "com.sun.star.form.control.CommandButton"),
        ],
    )


def _log_shape_add(*, step: str, name: str = "", text_chars: int = 0, truncated: bool = False, shape_h: int = 0, shapes_before: int, create_ms: int = 0, text_ms: int = 0, add_ms: int = 0, ok: bool = True) -> None:
    total_ms = create_ms + text_ms + add_ms
    log.debug("notebook controls add step=%s name=%s text_chars=%d truncated=%s shape_h=%d shapes_before=%d create_ms=%d text_ms=%d add_ms=%d ok=%s", step, name, text_chars, truncated, shape_h, shapes_before, create_ms, text_ms, add_ms, ok)
    if total_ms >= _SLOW_ADD_MS:
        log.warning("notebook controls slow UNO add step=%s total_ms=%d shapes_before=%d", step, total_ms, shapes_before)


def _insert_run_button_in_flow(doc: Any, *, cell_id: str, controls_before: int) -> None:
    """In-flow ▶ on the ``In [n]:`` gutter paragraph (not the tall gray field)."""
    hex_id = cell_id_to_hex(cell_id)
    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.CommandButton")
    if model is None:
        raise RuntimeError("Failed to create form CommandButton")
    model.Name = f"{_RUN_PREFIX}{hex_id}"
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
    _log_shape_add(step="run_button", name=model.Name, shapes_before=controls_before, create_ms=_mono_ms(t0), add_ms=_mono_ms(t_add), shape_h=_RUN_BUTTON_SIZE)


def _insert_code_input_in_flow(
    doc: Any,
    *,
    name: str,
    source: str,
    controls_before: int,
    field_w: int | None = None,
    cap: int | None = None,
) -> None:
    """Editable code cell: form TextField anchored in document flow at body end."""
    source_text = _coerce_notebook_text(source)
    raw_chars = len(source_text)

    if field_w is None:
        field_w = _text_area_width_units(doc)
    if cap is None:
        cap = _max_field_height_units(doc)

    raw_h = _raw_height_for_text(source_text, field_w=field_w, cap=cap)
    h = max(_MIN_FIELD_HEIGHT, min(cap, raw_h))
    vscroll = raw_h > cap

    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.TextField")
    if model is None:
        raise RuntimeError("Failed to create form TextField")
    model.Name = name
    if hasattr(model, "Label"):
        model.Label = _("Code")
    _style_code_field_model(model, vscroll=vscroll)
    create_ms = _mono_ms(t0)

    t_text = time.monotonic()
    # Full source is the runnable program; display cap is for body text only.
    # MaxTextLen defaults to 0 (unlimited); do not set it or non-zero caps at 32k.
    model.Text = source_text
    text_ms = _mono_ms(t_text)

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
    _log_shape_add(step="code_field", name=name, text_chars=raw_chars, truncated=False, shape_h=h, shapes_before=controls_before, create_ms=create_ms, text_ms=text_ms, add_ms=add_ms)


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
# strips remaining decorators. Keep @main_thread_only on bootstrap install.
# _lock serializes _listener_refs / _warned_no_container mutations (Dummy-2
# XFilter and Dummy-3 OnViewCreated can overlap) and the one-shot global
# _doc_listener install. Do not hold it across UNO (getFormController,
# addActionListener).
_listener_refs: list[Any] = []
_warned_no_container: set[str] = set()

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


def _doc_key(doc: Any) -> str:
    """Stable id for listener de-dupe across Python wrappers of the same document.

    Untitled Writer docs have an empty URL, so ``id(doc)`` was used. Import
    and bootstrap ``install_notebook_run_button_wiring`` can wire on
    different PyUNO wrappers of the same document — one ▶ click then ran
    the cell multiple times (``[In [4]]`` jumped to ``[In [7]]``).
    ``RuntimeUID`` is the same object for every wrapper of that document.

    Read it with ``_read_runtime_uid``, the same ladder without
    ``@main_thread_only``. ``get_runtime_uid`` raises off the main thread
    when the dev UNO thread guard is on. File Open ``XFilter.filter`` runs
    on Dummy-2 and the extensionless detect reload on Dummy-3 (see the
    wire_all comment above). ``filter()`` would catch that ``RuntimeError``
    and return False, and ``loadComponentFromURL`` would return None.
    Decorating this path or hopping to the main thread deadlocks the
    waiting host (#402).
    """
    from plugin.framework.uno_context import _read_runtime_uid

    uid = _read_runtime_uid(doc)
    if uid:
        return f"uid:{uid}"
    url = uno_probe(lambda: doc.getURL(), "")
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
            view = best_effort(lambda: controller.getControl(model), "notebook controls: controller.getControl failed")
            if view is not None:
                return view
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
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return
    global _listener_refs
    with _lock:
        refs = list(_listener_refs)
    survivors: list[Any] = []
    for lis in refs:
        doc = best_effort(lis._resolve_doc, "notebook controls: prune resolve failed")
        if doc is None:
            continue
        survivors.append(lis)
    with _lock:
        # Dummy-2 prune must not drop a Dummy-3 wire_all append that landed
        # after the snapshot.
        added = [lis for lis in _listener_refs if lis not in refs]
        _listener_refs = survivors + added


class NotebookRunButtonListener(BaseActionListener):
    """Run one notebook cell when the ▶ push button is pressed."""

    _ctx: Any
    _hex_id: str
    _form_level: bool
    _doc_key_val: str
    _doc_url: str
    _runtime_uid: str
    _doc_weak: Any | None

    def __init__(self, ctx: Any, doc: Any, hex_id: str) -> None:
        self._ctx = ctx
        self._hex_id = hex_id
        self._form_level = False
        self._doc_key_val = _doc_key(doc)
        self._doc_url = str(uno_probe(lambda: doc.getURL(), "") or "")
        from plugin.framework.uno_context import _read_runtime_uid

        # Untitled Writer docs have an empty URL. Hidden native tests (and a
        # notebook that is not Desktop.getCurrentComponent) then failed
        # get_active_document; PyUNO wrappers usually cannot be weakref'd, so
        # ▶ looked wired but actionPerformed returned "document gone".
        self._runtime_uid = _read_runtime_uid(doc) or ""
        import weakref

        self._doc_weak = uno_probe(lambda: weakref.ref(doc), None)

    def _resolve_doc(self) -> Any | None:
        from plugin.framework.uno_context import get_active_document, resolve_document_by_url

        weak = self._doc_weak
        if callable(weak):
            ref_doc: Any = weak()
            if ref_doc is not None:
                def _probe() -> bool:
                    try:
                        if callable(getattr(ref_doc, "getRuntimeUID", None)):
                            ref_doc.getRuntimeUID()
                            return True
                    except Exception:
                        log.debug("notebook controls: getRuntimeUID liveness probe failed", exc_info=True)
                    try:
                        if callable(getattr(ref_doc, "getURL", None)):
                            ref_doc.getURL()
                            return True
                    except Exception:
                        log.debug("notebook controls: getURL liveness probe failed", exc_info=True)
                    return False

                if _probe():
                    return ref_doc
            self._doc_weak = None

        active = best_effort(
            lambda: get_active_document(self._ctx),
            "notebook controls: active document resolution failed",
        )
        if active is not None and _doc_key(active) == self._doc_key_val:
            return active

        if self._doc_url:
            resolved = best_effort(
                lambda: resolve_document_by_url(self._ctx, self._doc_url),
                "notebook controls: doc resolution failed",
            )
            if resolved is not None:
                doc, _doc_type = resolved
                if doc is not None:
                    return doc

        if self._runtime_uid:
            resolved = best_effort(
                lambda: resolve_document_by_url(self._ctx, self._runtime_uid),
                "notebook controls: runtime uid resolution failed",
            )
            if resolved is not None:
                doc, _doc_type = resolved
                if doc is not None:
                    return doc

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

    _form_level: bool

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

    _form_listener: NotebookFormRunListener
    _doc_key_val: str
    _form_level: bool
    _container: Any

    def __init__(self, form_listener: NotebookFormRunListener, container: Any) -> None:
        self._form_listener = form_listener
        self._doc_key_val = form_listener._doc_key_val
        self._form_level = False
        self._container = container
        # Not a per-button listener. prune_dead_listeners used to treat the
        # missing attribute as a button key and raise AttributeError.
        self._hex_id: str | None = None

    def _resolve_doc(self) -> Any | None:
        return self._form_listener._resolve_doc()

    def on_element_inserted(self, Event: Any) -> None:
        elem = getattr(Event, "Element", None)
        _attach_action_listener(elem, self._form_listener)

    def on_element_removed(self, Event: Any) -> None:
        elem = getattr(Event, "Element", None)
        if elem is None:
            return
        name = best_effort(lambda: _control_name(elem), "notebook controls: failed to read removed control name")
        if name:
            self._form_listener._attached_names.discard(name)

    def on_element_replaced(self, Event: Any) -> None:
        old_elem = getattr(Event, "ReplacedElement", None)
        if old_elem is not None:
            old_name = best_effort(lambda: _control_name(old_elem), "notebook controls: failed to read replaced control name")
            if old_name:
                self._form_listener._attached_names.discard(old_name)
        new_elem = getattr(Event, "Element", None)
        _attach_action_listener(new_elem, self._form_listener)

    def on_disposing(self, Source: Any) -> None:
        global _listener_refs
        with _lock:
            pair = {self, self._form_listener}
            _listener_refs = [lis for lis in _listener_refs if lis not in pair]


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
            # check for elements with nb_run_ prefix
            has_nb = False
            elem_count = getattr(form, "getCount", lambda: 0)()
            for j in range(elem_count):
                elem = form.getByIndex(j)
                name = getattr(elem, "Name", "")
                if name and name.startswith(_RUN_PREFIX):
                    has_nb = True
                    break
            if has_nb:
                target_form = form
                break

        if target_form is None and count > 0:
            target_form = forms.getByIndex(0)  # fallback

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
    global _listener_refs
    doc_key = _doc_key(doc)
    with _lock:
        _warned_no_container.discard(doc_key)
        to_drop = [lis for lis in _listener_refs if getattr(lis, "_doc_key_val", None) == doc_key]
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
        _listener_refs = [lis for lis in _listener_refs if lis not in drop_set]


def wire_all_notebook_run_buttons(ctx: Any, doc: Any, new_view: bool = True) -> int:
    """Attach the shared form-level ▶ listener if missing. Returns 1 when wired.

    Does **not** call ``controller.getControl(model)`` per code cell.
    """
    doc_key = _doc_key(doc)
    if not new_view:
        with _lock:
            already_wired = any(
                isinstance(lis, NotebookFormContainerListener) and lis._doc_key_val == doc_key
                for lis in _listener_refs
            )
        if already_wired:
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
            warn = doc_key not in _warned_no_container
            if warn:
                _warned_no_container.add(doc_key)
        if warn:
            log.warning("notebook controls: no form controller container; ▶ clicks will not run (%d code cells)", len(state.code_cells))
        return 0

    from plugin.framework.uno_context import uno_same
    already_wired = False
    with _lock:
        for lis in _listener_refs:
            if isinstance(lis, NotebookFormContainerListener) and lis._doc_key_val == doc_key:
                if uno_same(lis._container, container):
                    already_wired = True
                    break

    if already_wired:
        log.debug("notebook controls: form listener already attached doc=%s", doc_key)
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

        def _rollback() -> None:
            controls = container.getControls() if hasattr(container, "getControls") else ()
            for control in controls or ():
                if hasattr(control, "removeActionListener"):
                    control.removeActionListener(listener)

        best_effort(_rollback, "notebook controls: removeActionListener during failed attach failed")
        return 0

    with _lock:
        already_wired = False
        for lis in _listener_refs:
            if isinstance(lis, NotebookFormContainerListener) and lis._doc_key_val == doc_key:
                if uno_same(lis._container, container):
                    already_wired = True
                    break
        if not already_wired:
            _listener_refs.append(listener)
            _listener_refs.append(container_lis)
            _warned_no_container.discard(doc_key)
    if already_wired:
        # File Open (Dummy-2) and view creation can both get past the first
        # check. The other call kept its listener; take ours back off so one
        # ▶ click does not run the cell twice.
        _detach_wiring(container, listener, container_lis)
        log.debug("notebook controls: lost wiring race; detached duplicate doc=%s", doc_key)
        return 1
    elapsed_ms = _mono_ms(t0)
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
                        is_text_doc = uno_probe(
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
        from plugin.doc.doc_type import is_writer
        from plugin.framework.uno_context import get_active_document

        doc = get_active_document(ctx)
        if doc is not None and is_writer(doc):
            wire_all_notebook_run_buttons(ctx, doc)

        _install_doc_event_listener(ctx)
    except Exception:
        log.debug("notebook controls: install wiring failed", exc_info=True)
