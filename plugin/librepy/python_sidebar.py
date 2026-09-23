# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy Python sidebar controller: status, cell list, diagnostics, action buttons."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.navigation import navigate_to_cell
from plugin.calc.python.cell_discovery import PythonCellInfo, list_python_cells_in_doc
from plugin.calc.python.diagnostics import (
    DiagnosticEntry,
    DiagnosticFilter,
    diagnostics_detail_text,
    get_diagnostics_store,
)
from plugin.chatbot.dialogs import (
    get_optional as get_optional_control,
    set_control_text,
    set_control_visible,
    translate_dialog,
)
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.uno_listeners import (
    BaseActionListener,
    BaseActivationEventListener,
    BaseItemListener,
    BaseWindowListener,
)
from plugin.doc.doc_type import is_calc
from plugin.scripting.document_scripts import get_calc_document_from_ctx
from plugin.scripting.sandbox import resolve_venv_python
from plugin.librepy.sidebar_menus import HEADER_BUTTON_IDS, wire_sidebar_header_buttons
from plugin.scripting.session_manager import calc_workbook_base_session_id, python_session_mode

log = logging.getLogger(__name__)

_FILTER_LABELS: tuple[tuple[str, DiagnosticFilter], ...] = (
    (_("All"), "all"),
    (_("Errors"), "errors"),
    (_("Output"), "output"),
)

# PythonSidebarDialog.xdl: window 376, last button bottom 354.
_BOTTOM_MARGIN = 20
_RIGHT_MARGIN = 12
_MIN_FLEX_HEIGHT = 16
_MIN_CONTROL_WIDTH = 20
_FLEX_CONTROLS = ("status", "cells_list", "diag_list", "diag_detail")
_CONTROL_IDS = (
    *HEADER_BUTTON_IDS,
    "status_label",
    "status",
    "btn_refresh",
    "btn_edit_cell",
    "btn_run_script",
    "cells_label",
    "cells_list",
    "filter_label",
    "filter_combo",
    "diag_label",
    "diag_list",
    "diag_detail",
    "btn_edit_init",
    "btn_reset",
    "btn_settings",
)

_ROW3_BUTTONS = ("btn_refresh", "btn_edit_cell", "btn_run_script")
_ROW2_BUTTONS = ("btn_edit_init", "btn_reset")

# Calc =PY() browser; hidden in Writer so the header + venv status remain.
_CALC_ONLY_IDS = (
    "btn_refresh",
    "btn_edit_cell",
    "btn_run_script",
    "cells_label",
    "cells_list",
    "filter_label",
    "filter_combo",
    "diag_label",
    "diag_list",
    "diag_detail",
    "btn_edit_init",
)


def compute_python_sidebar_layout(
    width: int,
    height: int,
    snapshot: dict[str, tuple[int, int, int, int]],
    *,
    bottom_margin: int = _BOTTOM_MARGIN,
    min_flex_height: int = _MIN_FLEX_HEIGHT,
    right_margin: int = _RIGHT_MARGIN,
) -> dict[str, tuple[int, int, int, int]]:
    """Distribute height among flex fields and stretch/tile controls across the width."""
    if width <= 0 or height <= 0 or not snapshot:
        return {}
    flex_names = [name for name in _FLEX_CONTROLS if name in snapshot]
    if not flex_names:
        return {}

    content_bottom = max(rect[1] + rect[3] for rect in snapshot.values())
    flex_sum = sum(snapshot[name][3] for name in flex_names)
    leftover = height - bottom_margin - (content_bottom - flex_sum)
    new_heights: dict[str, int]
    if flex_sum <= 0:
        new_heights = {name: max(min_flex_height, snapshot[name][3]) for name in flex_names}
    else:
        new_heights = {}
        remaining = leftover
        last = len(flex_names) - 1
        for i, name in enumerate(flex_names):
            if i == last:
                new_h = remaining
            else:
                new_h = leftover * snapshot[name][3] // flex_sum
                remaining -= new_h
            new_heights[name] = max(min_flex_height, new_h)

    left_margin = 4
    content_right = max(20, width - right_margin)
    content_width = max(20, content_right - left_margin)
    gap = 4

    # 3-button row width
    bw3 = max(10, (content_width - 2 * gap) // 3)
    # 2-button row width
    bw2 = max(10, (content_width - gap) // 2)
    # Filter label width
    filter_label_w = min(40, max(10, content_width // 4))

    layouts: dict[str, tuple[int, int, int, int]] = {}
    for name, (_ox, oy, _ow, oh) in snapshot.items():
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
            nx = _ox
            nw = _ow
        elif name in (
            "status_label",
            "status",
            "cells_label",
            "cells_list",
            "diag_label",
            "diag_list",
            "diag_detail",
            "btn_settings",
        ):
            nx = left_margin
            nw = content_width
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

    def __init__(self, controls: dict[str, Any]) -> None:
        self._c = controls
        self._snapshot: dict[str, tuple[int, int, int, int]] | None = None
        self._in_relayout = False
        self._root_window = None

    def disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
        if self._root_window and hasattr(self._root_window, "removeWindowListener"):
            try:
                self._root_window.removeWindowListener(self)
            except Exception:
                pass
        self._root_window = None

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
        log.info(
            "[LIBREPY LAYOUT] relayout w=%d h=%d max_child_right=%d overflow=%s",
            w,
            h,
            max_right,
            "YES" if max_right > w - 2 else "no",
        )


class _Activation(BaseActivationEventListener):
    """Sheet-activation listener that calls handler() whenever the active sheet changes."""

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
        return "unknown"


def format_runtime_status(ctx: Any, doc: Any | None) -> str:
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
    try:
        model.StringItemList = tuple(lines)
    except Exception:
        try:
            # Some UNO builds want a sequence assignment via remove/insert
            while model.getItemCount():
                model.removeItem(0)
            for line in lines:
                model.insertItem(model.getItemCount(), line, "")
        except Exception:
            log.debug("populate listbox failed", exc_info=True)


def _selected_index(control: Any) -> int:
    if control is None:
        return -1
    try:
        return int(control.getSelectedItemPos())
    except Exception:
        try:
            sels = control.getSelectedItemsPos()
            if sels:
                return int(sels[0])
        except Exception:
            pass
    return -1


def _filter_from_combo(control: Any) -> DiagnosticFilter:
    if control is None:
        return "all"
    try:
        text = str(control.getText() or "").strip()
    except Exception:
        text = ""
    for label, filt in _FILTER_LABELS:
        if text == label:
            return filt
    lower = text.lower()
    if "error" in lower:
        return "errors"
    if "output" in lower:
        return "output"
    return "all"


class PythonSidebarController:
    """Wires XDL controls for the LibrePy Python sidebar panel."""

    def __init__(self, ctx: Any, root_window: Any, frame: Any = None) -> None:
        self.ctx = ctx
        self.root = root_window
        self.frame = frame
        self._calc_panel = self._frame_is_calc()
        self._cells: list[PythonCellInfo] = []
        self._diags: list[DiagnosticEntry] = []
        self._store = get_diagnostics_store()
        self._on_diag = self._schedule_refresh
        try:
            translate_dialog(root_window)
        except Exception:
            log.debug("translate_dialog failed for Python sidebar", exc_info=True)
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
        self.refresh()
        if self._calc_panel:
            try:
                self._store.add_listener(self._on_diag)
            except Exception:
                log.debug("sidebar diagnostics listener add failed", exc_info=True)

    def disposing(self) -> None:
        rl = getattr(self, "resize_listener", None)
        if rl is not None:
            try:
                rl.disposing(None)
            except Exception:
                log.debug("sidebar resize listener remove failed", exc_info=True)
            self.resize_listener = None
        try:
            self._store.remove_listener(self._on_diag)
        except Exception:
            log.debug("sidebar diagnostics listener remove failed", exc_info=True)
        # Remove activation listener if it was added
        if getattr(self, "_activation_listener", None) is not None and self.frame is not None:
            try:
                controller = self.frame.getController()
                if controller is not None:
                    controller.removeActivationEventListener(self._activation_listener)
            except Exception:
                log.debug("sidebar activation listener remove failed", exc_info=True)

    def _ctrl(self, name: str) -> Any:
        return get_optional_control(self.root, name)

    def _attach_resize_listener(self) -> None:
        """Snapshot XDL geometry and stretch content fields when the deck height changes."""
        try:
            ids = _CONTROL_IDS if self._calc_panel else tuple(
                cid for cid in _CONTROL_IDS if cid not in _CALC_ONLY_IDS
            )
            controls = {cid: self._ctrl(cid) for cid in ids}
            listener = _PanelResizeListener(controls)
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
            try:
                model = filter_combo.getModel()
                model.StringItemList = tuple(label for label, _filt in _FILTER_LABELS)
                filter_combo.setText(_FILTER_LABELS[0][0])
            except Exception:
                log.debug("filter combo init failed", exc_info=True)

        bindings: list[tuple[str, Any]] = [
            ("btn_reset", self._on_reset),
            ("btn_settings", self._on_settings),
        ]
        if self._calc_panel:
            bindings[0:0] = [
                ("btn_refresh", self.refresh),
                ("btn_edit_cell", self._on_edit_cell),
                ("btn_run_script", self._on_run_script),
                ("btn_edit_init", self._on_edit_init),
            ]
        for cid, handler in bindings:
            ctrl = self._ctrl(cid)
            if ctrl is None:
                continue
            try:
                ctrl.addActionListener(_Action(handler))
            except Exception:
                log.debug("wire action %s failed", cid, exc_info=True)

        if self._calc_panel:
            cells = self._ctrl("cells_list")
            if cells is not None:
                try:
                    cells.addItemListener(_Item(self._on_cell_selected))
                except Exception:
                    log.debug("wire cells_list failed", exc_info=True)

            diags = self._ctrl("diag_list")
            if diags is not None:
                try:
                    diags.addItemListener(_Item(self._on_diag_selected))
                except Exception:
                    log.debug("wire diag_list failed", exc_info=True)

            if filter_combo is not None:
                try:
                    filter_combo.addItemListener(_Item(lambda _e: self.refresh()))
                except Exception:
                    log.debug("wire filter_combo failed", exc_info=True)

        try:
            header = {cid: self._ctrl(cid) for cid in HEADER_BUTTON_IDS}
            wire_sidebar_header_buttons(
                self.ctx, self.frame, header, calc_doc=self._calc_panel
            )
        except Exception:
            log.debug("wire header toolbar failed", exc_info=True)

    def _schedule_refresh(self, _entry: DiagnosticEntry | None = None) -> None:
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.thread_guard import on_main_thread

        if on_main_thread():
            self.refresh()
            return
        post_to_main_thread(self.refresh)

    def _frame_is_calc(self) -> bool:
        frame = self.frame
        if frame is None:
            return False
        try:
            controller = frame.getController()
            model = controller.getModel() if controller is not None else None
            return bool(model is not None and is_calc(model))
        except Exception:
            log.debug("LibrePy sidebar: frame type resolve failed", exc_info=True)
            return False

    def _hide_calc_only_controls(self) -> None:
        for cid in _CALC_ONLY_IDS:
            set_control_visible(self._ctrl(cid), False)

    def _calc_document(self) -> Any | None:
        """Prefer the Calc model bound to this sidebar frame, not Desktop current.

        Writer panels must not fall back to some other open Calc document.
        """
        if not getattr(self, "_calc_panel", True):
            return None
        frame = self.frame
        if frame is not None:
            try:
                controller = frame.getController()
                model = controller.getModel() if controller is not None else None
                if model is not None and is_calc(model):
                    from plugin.framework.thread_guard import guard_uno

                    return guard_uno(model)
            except Exception:
                log.debug("LibrePy sidebar: frame document resolve failed", exc_info=True)
        return get_calc_document_from_ctx(self.ctx)

    def refresh(self) -> None:
        if not self._calc_panel:
            set_control_text(self._ctrl("status"), format_runtime_status(self.ctx, None))
            return
        doc = self._calc_document()
        set_control_text(self._ctrl("status"), format_runtime_status(self.ctx, doc))

        self._cells = list_python_cells_in_doc(doc, active_sheet_only=True) if doc else []
        cell_lines = [c.address for c in self._cells]
        if not cell_lines:
            cell_lines = [_("(no =PY() cells on active sheet)")]
        _populate_listbox(self._ctrl("cells_list"), cell_lines)

        key = workbook_key_for_doc(doc)
        filt = _filter_from_combo(self._ctrl("filter_combo"))
        self._diags = self._store.list_entries(key, filt=filt, newest_first=True)
        # Attach addresses from cell list when codes match.
        code_to_addr = {c.code[:240]: c.address for c in self._cells if c.code}
        enriched: list[DiagnosticEntry] = []
        for entry in self._diags:
            if not entry.address and entry.code in code_to_addr:
                enriched.append(
                    DiagnosticEntry(
                        workbook_key=entry.workbook_key,
                        code=entry.code,
                        status=entry.status,
                        message=entry.message,
                        stdout=entry.stdout,
                        traceback=entry.traceback,
                        timestamp=entry.timestamp,
                        sheet=entry.sheet,
                        address=code_to_addr[entry.code],
                    )
                )
            else:
                enriched.append(entry)
        self._diags = enriched

        diag_lines = [e.summary_line() for e in self._diags]
        if not diag_lines:
            diag_lines = [_("(no diagnostics yet)")]
        _populate_listbox(self._ctrl("diag_list"), diag_lines)

        detail = self._ctrl("diag_detail")
        if self._diags:
            set_control_text(detail, diagnostics_detail_text(self._diags[0]))
        else:
            set_control_text(detail, "")

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

    def _on_edit_cell(self) -> None:
        from plugin.framework.main_shared import get_action_handler

        # Prefer selecting the highlighted cell first.
        self._on_cell_selected()
        handler = get_action_handler("scripting.edit_python_cell")
        if handler:
            handler()
            return
        from plugin.calc.python.editor import open_python_cell_editor

        open_python_cell_editor(self.ctx)

    def _on_run_script(self) -> None:
        from plugin.framework.main_shared import get_action_handler

        handler = get_action_handler("scripting.run_python_dialog")
        if handler:
            handler()
            return
        from plugin.scripting.python_runner import run_python_dialog

        run_python_dialog(self.ctx)

    def _on_edit_init(self) -> None:
        from plugin.calc.python.init_script_editor import open_init_script_editor

        open_init_script_editor(self.ctx)

    def _on_reset(self) -> None:
        from plugin.framework.main_shared import get_action_handler

        handler = get_action_handler("scripting.reset_python_session")
        if handler:
            handler()
        else:
            from plugin.scripting.session_manager import reset_workbook_python_session

            reset_workbook_python_session(self.ctx)
        self.refresh()

    def _on_settings(self) -> None:
        from plugin.framework.main_shared import get_action_handler, open_dialog_safely

        handler = get_action_handler("main.settings")
        if handler:
            handler()
            return
        try:
            from plugin.librepy.settings import open_librepy_settings

            open_dialog_safely(open_librepy_settings, "Failed to open settings")
        except Exception:
            log.debug("open settings failed", exc_info=True)


class _Action(BaseActionListener):
    def __init__(self, callback: Any) -> None:
        super().__init__()
        self._callback = callback

    def on_action_performed(self, rEvent: Any) -> None:
        self._callback()


class _Item(BaseItemListener):
    def __init__(self, callback: Any) -> None:
        super().__init__()
        self._callback = callback

    def on_item_state_changed(self, rEvent: Any) -> None:
        self._callback(rEvent)
