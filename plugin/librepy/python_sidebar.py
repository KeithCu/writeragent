# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy Python sidebar controller: status, cell list, diagnostics, action buttons."""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any, Callable

from plugin.calc.navigation import navigate_to_cell
from plugin.calc.python.cell_discovery import PythonCellInfo, list_python_cells_in_doc
from plugin.calc.python.diagnostics import DiagnosticEntry, DiagnosticFilter, diagnostics_detail_text, get_diagnostics_store
from plugin.chatbot.dialogs import get_optional as get_optional_control, set_control_text, set_control_visible, translate_dialog
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.uno_listeners import BaseActionListener, BaseActivationEventListener, BaseItemListener, BaseWindowListener
from plugin.doc.doc_type import is_calc
from plugin.scripting.sandbox import resolve_venv_python
from plugin.librepy.sidebar_menus import HEADER_BUTTON_IDS, wire_sidebar_header_buttons
from plugin.scripting.session_manager import calc_workbook_base_session_id, python_session_mode

log = logging.getLogger(__name__)

# What was wrong: _FILTER_LABELS called _() at module import time before locale was initialized.
# How it happened: module-level tuple evaluation called gettext at import.
# Why this change: translate lazily via _filter_choices() and match translated label, then English msgid.
_FILTER_MSGIDS: tuple[tuple[str, DiagnosticFilter], ...] = (("All", "all"), ("Errors", "errors"), ("Output", "output"))


def _filter_choices() -> tuple[tuple[str, DiagnosticFilter], ...]:
    return tuple((_(msgid), filt) for msgid, filt in _FILTER_MSGIDS)


_LEFT_MARGIN = 4
_CONTROL_GAP = 4
# PythonSidebarDialog.xdl: window 376, last button bottom 354.
_BOTTOM_MARGIN = 20
_RIGHT_MARGIN = 12
_MIN_FLEX_HEIGHT = 16
_FLEX_CONTROLS = ("status", "cells_list", "diag_list", "diag_detail")
_CONTROL_IDS = (*HEADER_BUTTON_IDS, "status_label", "status", "btn_refresh", "btn_edit_cell", "btn_run_script", "cells_label", "cells_list", "filter_label", "filter_combo", "diag_label", "diag_list", "diag_detail", "btn_edit_init", "btn_reset", "btn_settings")

# Calc =PY() browser; hidden in Writer so the header + venv status remain.
_CALC_ONLY_IDS = ("btn_refresh", "btn_edit_cell", "btn_run_script", "cells_label", "cells_list", "filter_label", "filter_combo", "diag_label", "diag_list", "diag_detail", "btn_edit_init")


def compute_python_sidebar_layout(width: int, height: int, snapshot: dict[str, tuple[int, int, int, int]], *, bottom_margin: int = _BOTTOM_MARGIN, min_flex_height: int = _MIN_FLEX_HEIGHT, right_margin: int = _RIGHT_MARGIN) -> dict[str, tuple[int, int, int, int]]:
    """Distribute height among flex fields and stretch/tile controls across the width."""
    if width <= 0 or height <= 0 or not snapshot:
        return {}
    flex_names = [name for name in _FLEX_CONTROLS if name in snapshot]
    if not flex_names:
        return {}

    content_bottom = max(rect[1] + rect[3] for rect in snapshot.values())
    flex_sum = sum(snapshot[name][3] for name in flex_names)
    leftover = height - bottom_margin - (content_bottom - flex_sum)
    # What was wrong: short sidebar panels overflowed the bottom margin because clamping to min flex height exceeded leftover space.
    # How it happened: each flex control clamped independently to min_flex_height without checking the total deck budget.
    # Why this change: scale against leftover budget and shrink from the bottom controls backward so controls fit within height.
    budget = max(0, leftover)
    new_heights: dict[str, int]
    if flex_sum <= 0:
        new_heights = {name: max(min_flex_height, snapshot[name][3]) for name in flex_names}
    else:
        new_heights = {}
        remaining = budget
        last = len(flex_names) - 1
        for i, name in enumerate(flex_names):
            if i == last:
                share = remaining
            else:
                share = budget * snapshot[name][3] // flex_sum
                remaining -= share
            new_heights[name] = max(min_flex_height, share)

    excess = sum(new_heights.values()) - budget
    if excess > 0:
        for name in reversed(flex_names):
            cur = new_heights[name]
            if cur > min_flex_height:
                reducible = cur - min_flex_height
                reduce_by = min(excess, reducible)
                new_heights[name] = cur - reduce_by
                excess -= reduce_by
                if excess <= 0:
                    break
        if excess > 0:
            for name in reversed(flex_names):
                cur = new_heights[name]
                reduce_by = min(excess, cur)
                new_heights[name] = cur - reduce_by
                excess -= reduce_by
                if excess <= 0:
                    break

    left_margin = _LEFT_MARGIN
    content_right = max(20, width - right_margin)
    content_width = max(20, content_right - left_margin)
    gap = _CONTROL_GAP

    # 3-button row width
    bw3 = max(10, (content_width - 2 * gap) // 3)
    # 2-button row width
    bw2 = max(10, (content_width - gap) // 2)
    # Filter label width
    filter_label_w = min(40, max(10, content_width // 4))

    layouts: dict[str, tuple[int, int, int, int]] = {}
    for name, (origin_x, oy, origin_w, oh) in snapshot.items():
        shift = 0
        for fname in flex_names:
            if snapshot[fname][1] < oy:
                shift += new_heights[fname] - snapshot[fname][3]

        if name == "btn_refresh":
            nx = left_margin
            nw = bw3
        elif name == "btn_edit_cell":
            nx = left_margin + bw3 + gap
            nw = bw3
        elif name == "btn_run_script":
            nx = left_margin + 2 * (bw3 + gap)
            nw = max(10, content_right - nx)
        elif name == "btn_edit_init":
            nx = left_margin
            nw = bw2
        elif name == "btn_reset":
            if "btn_edit_init" not in snapshot:
                nx = left_margin
                nw = content_width
            else:
                nx = left_margin + bw2 + gap
                nw = max(10, content_right - nx)
        elif name == "filter_label":
            nx = left_margin
            nw = filter_label_w
        elif name == "filter_combo":
            nx = left_margin + filter_label_w + gap
            nw = max(10, content_right - nx)
        elif name in HEADER_BUTTON_IDS:
            nx = origin_x
            nw = origin_w
        else:
            nx = left_margin
            nw = content_width

        if nx >= content_right:
            nx = max(0, content_right - 10)
            nw = 10
        elif nx + nw > content_right:
            nw = max(10, content_right - nx)

        layouts[name] = (nx, oy + shift, nw, new_heights.get(name, oh))
    return layouts


class _PanelResizeListener(BaseWindowListener):
    """Repositions Python sidebar controls when the panel root is resized."""

    _c: dict[str, Any]
    _on_dispose: Any
    _in_relayout: bool
    _root_window: Any

    def __init__(self, controls: dict[str, Any], on_dispose: Any = None) -> None:
        self._c = controls
        self._on_dispose = on_dispose
        self._snapshot: dict[str, tuple[int, int, int, int]] | None = None
        self._in_relayout = False
        self._root_window = None

    def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
        # LibreOffice queries XComponent for dispose. This element is
        # XUIElement only, so PythonPanelElement.disposing never runs and
        # deck close would leak the controller's listeners. VCL does call
        # this window listener when the root window goes; clean up from here.
        # Do not removeWindowListener; this listener is already disposing
        # (same as the chat panel listener).
        callback = self._on_dispose
        self._on_dispose = None
        self._root_window = None
        if callable(callback):
            try:
                callback()
            except Exception:
                log.exception("python sidebar window dispose callback failed")

    def relayout_now(self, win: Any) -> None:
        if not win or self._in_relayout:
            return
        try:
            self._in_relayout = True
            self._relayout(win)
        except Exception:
            log.exception("python sidebar relayout_now failed")
        finally:
            self._in_relayout = False

    def on_window_resized(self, rEvent: Any) -> None:
        self.relayout_now(rEvent.Source)

    def _capture_snapshot(self, win: Any) -> None:
        r = win.getPosSize()
        if r.Width <= 0 or r.Height <= 0:
            return
        snapshot: dict[str, tuple[int, int, int, int]] = {}
        for name, ctrl in self._c.items():
            if not ctrl:
                continue
            cr = ctrl.getPosSize()
            snapshot[name] = (int(cr.X), int(cr.Y), int(cr.Width), int(cr.Height))
        if not any(name in snapshot for name in _FLEX_CONTROLS):
            return
        self._snapshot = snapshot

    def _relayout(self, win: Any) -> None:
        r = win.getPosSize()
        w, h = int(r.Width), int(r.Height)
        if w <= 0 or h <= 0:
            return
        if self._snapshot is None:
            self._capture_snapshot(win)
        snapshot = self._snapshot
        if not snapshot:
            log.warning("python sidebar _relayout: no snapshot, skip")
            return
        layouts = compute_python_sidebar_layout(w, h, snapshot)
        if not layouts:
            return
        for name, (nx, ny, nw, nh) in layouts.items():
            ctrl = self._c.get(name)
            if ctrl is None:
                continue
            cur = ctrl.getPosSize()
            if cur.X != nx or cur.Y != ny or cur.Width != nw or cur.Height != nh:
                ctrl.setPosSize(nx, ny, nw, nh, 15)

        max_right = max((entry[0] + entry[2] for entry in layouts.values()), default=0)
        log.debug("[LIBREPY LAYOUT] relayout w=%d h=%d max_child_right=%d overflow=%s", w, h, max_right, "YES" if max_right > w - 2 else "no")


class _Activation(BaseActivationEventListener):
    """Sheet-activation listener that calls handler() whenever the active sheet changes."""

    _handler: Any

    def __init__(self, handler: Any) -> None:
        super().__init__()
        self._handler = handler

    def on_active_spreadsheet_changed(self, aEvent: object) -> None:
        self._handler()


def workbook_key_for_doc(doc: Any) -> str:
    if doc is None:
        return "unknown"
    try:
        return calc_workbook_base_session_id(doc)
    except Exception:
        log.debug("workbook key lookup failed", exc_info=True)
        return "unknown"


def format_runtime_status(ctx: Any) -> str:
    """Compact status text: session mode + venv path resolution (no package probe)."""
    mode = python_session_mode(ctx)
    mode_label = _("Shared kernel") if mode == "shared" else _("Isolated")
    venv = (get_config_str("scripting.python_venv_path") or "").strip()
    if not venv:
        return _("{mode}\nVenv: (LibreOffice embedded Python)").format(mode=mode_label)
    exe = resolve_venv_python(venv)
    if exe:
        return _("{mode}\nVenv: {path}").format(mode=mode_label, path=venv)
    return _("{mode}\nVenv: missing python at {path}").format(mode=mode_label, path=venv)


def _populate_listbox(control: Any, lines: list[str]) -> None:
    if control is None:
        return
    model = control.getModel() if hasattr(control, "getModel") else None
    if model is None:
        return
    model.StringItemList = tuple(lines)


def _selected_index(control: Any) -> int:
    if control is None:
        return -1
    return int(control.getSelectedItemPos())


def _filter_from_combo(control: Any) -> DiagnosticFilter:
    if control is None:
        return "all"
    text = str(control.getText() or "").strip()
    for label, filt in _filter_choices():
        if text == label:
            return filt
    for msgid, filt in _FILTER_MSGIDS:
        if text == msgid:
            return filt
    return "all"


def _select_diag_row(control: Any, index: int) -> None:
    if control is None or not hasattr(control, "selectItemPos"):
        return
    control.selectItemPos(index, True)


class PythonSidebarController:
    """Wires XDL controls for the LibrePy Python sidebar panel."""

    ctx: Any
    root: Any
    frame: Any
    _calc_panel: bool
    _store: Any
    _on_diag: Any
    _activation_listener: Any
    _disposed: bool
    _refresh_pending: bool
    _suppress_diag_nav: bool

    def __init__(self, ctx: Any, root_window: Any, frame: Any = None) -> None:
        self.ctx = ctx
        self.root = root_window
        self.frame = frame
        self._calc_panel = self._frame_is_calc()
        self._cells: list[PythonCellInfo] = []
        self._diags: list[DiagnosticEntry] = []
        self._store = get_diagnostics_store()
        self._on_diag = self._schedule_refresh
        self._disposed = False
        self._refresh_pending = False
        self._suppress_diag_nav = False
        translate_dialog(root_window)
        if not self._calc_panel:
            self._hide_calc_only_controls()
        self._wire()
        self.resize_listener: _PanelResizeListener | None = None
        self._attach_resize_listener()
        self._activation_listener = None
        if self._calc_panel and self.frame is not None:
            try:
                controller = self.frame.getController()
                if controller is not None:
                    self._activation_listener = _Activation(self._schedule_refresh)
                    controller.addActivationEventListener(self._activation_listener)
            except Exception:
                log.debug("sidebar activation listener add failed", exc_info=True)
        # What was wrong: an exception during initial refresh aborted __init__, leaking activation listeners and breaking panel disposal.
        # How it happened: self.refresh() ran unprotected before controller assignment in PythonPanelElement.
        # Why this change: catch and log exceptions from initial refresh so construction completes and listeners can be cleaned up.
        try:
            self.refresh()
        except Exception:
            log.exception("python sidebar initial refresh failed")
        if self._calc_panel:
            self._store.add_listener(self._on_diag)

    def disposing(self) -> None:
        self._disposed = True
        # Deck close never calls PythonPanelElement.disposing: the element is
        # XUIElement, not XComponent, so the sidebar dispose query fails.
        # The root window listener's on_dispose calls this method (an explicit
        # element.disposing still does too). Drop the window listener while
        # the root is still held, then the diagnostics and Calc activation
        # listeners; otherwise they run against a dead frame. Clear on_dispose
        # first so the window hook cannot re-enter.
        rl = getattr(self, "resize_listener", None)
        if rl is not None:
            self.resize_listener = None
            rl._on_dispose = None
            root = getattr(rl, "_root_window", None)
            rl._root_window = None
            if root is not None and hasattr(root, "removeWindowListener"):
                root.removeWindowListener(rl)
        self._store.remove_listener(self._on_diag)
        activation = getattr(self, "_activation_listener", None)
        self._activation_listener = None
        if activation is not None and self.frame is not None:
            try:
                controller = self.frame.getController()
                if controller is not None:
                    controller.removeActivationEventListener(activation)
            except Exception:
                log.debug("sidebar activation listener remove failed", exc_info=True)

    def _release_on_window_dispose(self) -> None:
        """Deck close never calls PythonPanelElement.disposing (not an XComponent)."""
        self.disposing()

    def _ctrl(self, name: str) -> Any:
        return get_optional_control(self.root, name)

    def _attach_resize_listener(self) -> None:
        """Snapshot XDL geometry and stretch content fields when the deck height changes."""
        try:
            ids = _CONTROL_IDS if self._calc_panel else tuple(cid for cid in _CONTROL_IDS if cid not in _CALC_ONLY_IDS)
            controls = {cid: self._ctrl(cid) for cid in ids}
            listener = _PanelResizeListener(controls, on_dispose=self._release_on_window_dispose)
            listener._root_window = self.root
            if self.root is not None and hasattr(self.root, "addWindowListener"):
                self.root.addWindowListener(listener)
            self.resize_listener = listener
            listener._capture_snapshot(self.root)
            listener.relayout_now(self.root)
        except Exception:
            log.debug("sidebar resize listener attach failed", exc_info=True)

    def _wire(self) -> None:
        filter_combo = self._ctrl("filter_combo") if self._calc_panel else None
        if filter_combo is not None:
            model = filter_combo.getModel()
            choices = _filter_choices()
            model.StringItemList = tuple(label for label, _filt in choices)
            filter_combo.setText(choices[0][0])

        bindings: list[tuple[str, Any]] = [("btn_reset", self._on_reset), ("btn_settings", self._on_settings)]
        if self._calc_panel:
            bindings[0:0] = [("btn_refresh", self.refresh), ("btn_edit_cell", self._on_edit_cell), ("btn_run_script", self._on_run_script), ("btn_edit_init", self._on_edit_init)]
        for cid, handler in bindings:
            ctrl = self._ctrl(cid)
            if ctrl is None:
                continue
            ctrl.addActionListener(_Action(handler))

        if self._calc_panel:
            cells = self._ctrl("cells_list")
            if cells is not None:
                cells.addItemListener(_Item(self._on_cell_selected))

            diags = self._ctrl("diag_list")
            if diags is not None:
                diags.addItemListener(_Item(self._on_diag_selected))

            if filter_combo is not None:
                filter_combo.addItemListener(_Item(self._on_filter_changed))

        try:
            header = {cid: self._ctrl(cid) for cid in HEADER_BUTTON_IDS}
            wire_sidebar_header_buttons(self.ctx, self.frame, header, calc_doc=self._calc_panel)
        except Exception:
            log.debug("wire header toolbar failed", exc_info=True)

    def _schedule_refresh(self, _entry: DiagnosticEntry | None = None) -> None:
        # What was wrong: inline refresh during diagnostic events scanned the sheet on every =PY() recalc.
        # How it happened: record() invokes listeners synchronously, triggering full sheet cell scans inline.
        # Why this change: coalesce refreshes and post to main thread, skipping if already disposed or pending.
        if self._disposed or self._refresh_pending:
            return
        self._refresh_pending = True
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(self._run_scheduled_refresh)

    def _run_scheduled_refresh(self) -> None:
        self._refresh_pending = False
        if self._disposed:
            return
        self.refresh()

    # What was wrong: _calc_document fell back to desktop active Calc document, binding wrong documents or Writer frames to Calc.
    # How it happened: get_calc_document_from_ctx was called when frame resolve failed or when resolving outside the frame.
    # Why this change: resolve only via frame.getController().getModel(), returning None if not Calc or resolve fails.
    def _frame_calc_model(self) -> Any | None:
        frame = self.frame
        if frame is None:
            return None
        try:
            controller = frame.getController()
            if controller is None:
                return None
            model = controller.getModel()
            if model is None:
                return None
            from plugin.framework.thread_guard import guard_uno

            guarded = guard_uno(model)
            return guarded if is_calc(guarded) else None
        except Exception:
            log.debug("LibrePy sidebar: frame document resolve failed", exc_info=True)
            return None

    def _frame_is_calc(self) -> bool:
        return self._frame_calc_model() is not None

    def _hide_calc_only_controls(self) -> None:
        for cid in _CALC_ONLY_IDS:
            set_control_visible(self._ctrl(cid), False)

    def _calc_document(self) -> Any | None:
        """Return the Calc model for this sidebar frame, or None for Writer frames and failed resolves."""
        return self._frame_calc_model()

    def _refresh_diagnostics(self, doc: Any) -> None:
        diag_ctrl = self._ctrl("diag_list")
        prev_idx = _selected_index(diag_ctrl)
        prev_entry: DiagnosticEntry | None = None
        if 0 <= prev_idx < len(self._diags):
            prev_entry = self._diags[prev_idx]

        key = workbook_key_for_doc(doc)
        filt = _filter_from_combo(self._ctrl("filter_combo"))
        self._diags = self._store.list_entries(key, filt=filt, newest_first=True)
        # Attach addresses from cell list when codes match.
        code_to_addr = {c.code[:240]: c.address for c in self._cells if c.code}
        enriched: list[DiagnosticEntry] = []
        for entry in self._diags:
            if not entry.address and entry.code in code_to_addr:
                enriched.append(replace(entry, address=code_to_addr[entry.code]))
            else:
                enriched.append(entry)
        self._diags = enriched

        diag_lines = [e.summary_line() for e in self._diags]
        if not diag_lines:
            diag_lines = [_("(no diagnostics yet)")]
        _populate_listbox(diag_ctrl, diag_lines)

        detail = self._ctrl("diag_detail")
        if self._diags:
            target_idx = 0
            if prev_entry is not None:
                prev_key = (prev_entry.timestamp, prev_entry.code, prev_entry.address, prev_entry.status, prev_entry.message)
                for idx, entry in enumerate(self._diags):
                    if (entry.timestamp, entry.code, entry.address, entry.status, entry.message) == prev_key:
                        target_idx = idx
                        break
            selected_entry = self._diags[target_idx]
            set_control_text(detail, diagnostics_detail_text(selected_entry))
            self._suppress_diag_nav = True
            try:
                _select_diag_row(diag_ctrl, target_idx)
            finally:
                self._suppress_diag_nav = False
        else:
            set_control_text(detail, "")

    def _on_filter_changed(self, _event: Any = None) -> None:
        if self._disposed:
            return
        doc = self._calc_document()
        if doc is None:
            return
        self._refresh_diagnostics(doc)

    def refresh(self) -> None:
        if self._disposed:
            return
        doc = self._calc_document()
        set_control_text(self._ctrl("status"), format_runtime_status(self.ctx))
        if doc is None:
            return

        self._cells = list_python_cells_in_doc(doc, active_sheet_only=True)
        cell_lines = [c.address for c in self._cells]
        if not cell_lines:
            cell_lines = [_("(no =PY() cells on active sheet)")]
        _populate_listbox(self._ctrl("cells_list"), cell_lines)

        self._refresh_diagnostics(doc)

    def _on_cell_selected(self, _event: Any = None) -> None:
        idx = _selected_index(self._ctrl("cells_list"))
        if idx < 0 or idx >= len(self._cells):
            return
        cell = self._cells[idx]
        doc = self._calc_document()
        if doc is None:
            return
        navigate_to_cell(doc, self.ctx, cell.address)
        latest = self._store.latest_for_code(workbook_key_for_doc(doc), cell.code)
        if latest is not None:
            set_control_text(self._ctrl("diag_detail"), diagnostics_detail_text(latest))

    def _on_diag_selected(self, _event: Any = None) -> None:
        if self._suppress_diag_nav:
            return
        idx = _selected_index(self._ctrl("diag_list"))
        if idx < 0 or idx >= len(self._diags):
            return
        entry = self._diags[idx]
        set_control_text(self._ctrl("diag_detail"), diagnostics_detail_text(entry))
        if not entry.address:
            return
        doc = self._calc_document()
        if doc is not None:
            navigate_to_cell(doc, self.ctx, entry.address)

    def _run_action(self, command: str, fallback: Callable[[], None]) -> None:
        from plugin.framework.main_shared import get_action_handler

        handler = get_action_handler(command)
        if handler:
            from plugin.librepy.sidebar_menus import invoke_action_handler

            invoke_action_handler(handler, command=command)
            return
        fallback()

    def _on_edit_cell(self) -> None:
        # Prefer selecting the highlighted cell first.
        self._on_cell_selected()

        def _fallback() -> None:
            from plugin.calc.python.editor import open_python_cell_editor

            open_python_cell_editor(self.ctx)

        self._run_action("scripting.edit_python_cell", _fallback)

    def _on_run_script(self) -> None:
        def _fallback() -> None:
            from plugin.scripting.python_runner import run_python_dialog

            run_python_dialog(self.ctx)

        self._run_action("scripting.run_python_dialog", _fallback)

    def _on_edit_init(self) -> None:
        from plugin.calc.python.init_script_editor import open_init_script_editor

        open_init_script_editor(self.ctx)

    def _on_reset(self) -> None:
        def _fallback() -> None:
            from plugin.scripting.session_manager import reset_workbook_python_session

            reset_workbook_python_session(self.ctx)

        self._run_action("scripting.reset_python_session", _fallback)
        self.refresh()

    def _on_settings(self) -> None:
        def _fallback() -> None:
            try:
                from plugin.framework.main_shared import open_dialog_safely
                from plugin.librepy.settings import open_librepy_settings

                open_dialog_safely(open_librepy_settings, "Failed to open settings")
            except Exception:
                log.debug("open settings failed", exc_info=True)

        self._run_action("main.settings", _fallback)


class _Action(BaseActionListener):
    _callback: Any

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self._callback = callback

    def on_action_performed(self, rEvent: Any) -> None:
        self._callback()


class _Item(BaseItemListener):
    _callback: Any

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self._callback = callback

    def on_item_state_changed(self, rEvent: Any) -> None:
        self._callback(rEvent)
