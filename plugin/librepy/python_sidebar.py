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
from plugin.chatbot.dialogs import get_optional as get_optional_control, set_control_text, translate_dialog
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.uno_listeners import BaseActionListener, BaseItemListener
from plugin.scripting.document_scripts import get_calc_document_from_ctx
from plugin.scripting.sandbox import resolve_venv_python
from plugin.scripting.session_manager import calc_workbook_base_session_id, python_session_mode

log = logging.getLogger(__name__)

_FILTER_LABELS: tuple[tuple[str, DiagnosticFilter], ...] = (
    (_("All"), "all"),
    (_("Errors"), "errors"),
    (_("Output"), "output"),
)


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
        self._cells: list[PythonCellInfo] = []
        self._diags: list[DiagnosticEntry] = []
        self._store = get_diagnostics_store()
        self._on_diag = self._schedule_refresh
        try:
            translate_dialog(root_window)
        except Exception:
            log.debug("translate_dialog failed for Python sidebar", exc_info=True)
        self._wire()
        self.refresh()
        try:
            self._store.add_listener(self._on_diag)
        except Exception:
            pass

    def disposing(self) -> None:
        try:
            self._store.remove_listener(self._on_diag)
        except Exception:
            pass

    def _ctrl(self, name: str) -> Any:
        return get_optional_control(self.root, name)

    def _wire(self) -> None:
        filter_combo = self._ctrl("filter_combo")
        if filter_combo is not None:
            try:
                model = filter_combo.getModel()
                model.StringItemList = tuple(label for label, _filt in _FILTER_LABELS)
                filter_combo.setText(_FILTER_LABELS[0][0])
            except Exception:
                log.debug("filter combo init failed", exc_info=True)

        bindings: list[tuple[str, Any]] = [
            ("btn_refresh", self.refresh),
            ("btn_edit_cell", self._on_edit_cell),
            ("btn_run_script", self._on_run_script),
            ("btn_edit_init", self._on_edit_init),
            ("btn_reset", self._on_reset),
            ("btn_settings", self._on_settings),
        ]
        for cid, handler in bindings:
            ctrl = self._ctrl(cid)
            if ctrl is None:
                continue
            try:
                ctrl.addActionListener(_Action(handler))
            except Exception:
                log.debug("wire action %s failed", cid, exc_info=True)

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

    def _schedule_refresh(self, _entry: DiagnosticEntry | None = None) -> None:
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.thread_guard import on_main_thread

        if on_main_thread():
            self.refresh()
            return
        post_to_main_thread(self.refresh)

    def refresh(self) -> None:
        doc = get_calc_document_from_ctx(self.ctx)
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
        doc = get_calc_document_from_ctx(self.ctx)
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
        doc = get_calc_document_from_ctx(self.ctx)
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
    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    def on_action_performed(self, rEvent) -> None:
        self._callback()


class _Item(BaseItemListener):
    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    def on_item_state_changed(self, rEvent) -> None:
        self._callback(rEvent)
