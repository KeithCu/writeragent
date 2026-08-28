# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native LibreOffice dialog for Calc **Edit Python in Cell…** (Monaco fallback).

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO calc_cell FRONTEND
# If you modify toolbar actions, Data: enablement, status copy, or save modes here,
# you MUST also update:
#   - Native Dialog Layout:     extension/Dialogs/PythonCellEditorDialog.xdl
#   - Monaco HTML / Toolbar:    plugin/contrib/scripting/assets/editor/index.html
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor.js
#   - UI Strings Catalog:       plugin/scripting/editor_ui_strings.py (_calc_cell_ui_strings)
#   - Calc Editor Host:         plugin/calc/python/editor.py
# =========================================================================================
"""

from __future__ import annotations

import logging
from typing import Any

import unohelper
from com.sun.star.awt import XActionListener, XItemListener, XTextListener, XTopWindowListener

from plugin.chatbot.dialogs import (
    get_checkbox_state,
    load_writeragent_dialog_detail,
    set_checkbox_state,
    set_control_text,
)
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.scripting")

_active: NativePythonCellEditorDialog | None = None


def _status_text(body: str) -> str:
    prefix = _("Status:")
    label = body or _("Ready")
    if prefix and not str(label).startswith(prefix):
        return prefix + " " + label
    return str(label)


class NativePythonCellEditorDialog:
    """Modeless native twin of Monaco calc_cell mode. One instance; later opens retarget."""

    def __init__(
        self,
        ctx: Any,
        *,
        doc: Any,
        cell: Any,
        initial_code: str,
        parsed_parts: Any,
        code_cell: Any | None = None,
        code_ref: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._doc = doc
        self._cell = cell
        self._parsed_parts = parsed_parts
        self._code_cell = code_cell if code_cell is not None else cell
        self._code_ref = code_ref
        self._dlg: Any | None = None
        self._closed = False
        self._dirty = False
        self._loading = False
        self._top_listener: Any | None = None
        self._open_failure_detail: str | None = None
        self._opened = self._open(initial_code)

    @property
    def is_open(self) -> bool:
        return not self._closed and self._dlg is not None

    def retarget(
        self,
        *,
        doc: Any,
        cell: Any,
        initial_code: str,
        parsed_parts: Any,
        code_cell: Any | None = None,
        code_ref: str | None = None,
    ) -> None:
        self._doc = doc
        self._cell = cell
        self._parsed_parts = parsed_parts
        self._code_cell = code_cell if code_cell is not None else cell
        self._code_ref = code_ref
        self._apply_load(initial_code)
        self._dirty = False

    def close(self, *, toolkit_teardown: bool = False) -> None:
        """Hide/dispose. Esc/title-bar X already tears the peer down — do not dispose again."""
        global _active
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if _active is self:
            _active = None
        if dlg is None:
            return
        if toolkit_teardown:
            log.debug("native cell editor: windowClosing (no dispose)")
            try:
                dlg.setVisible(False)
            except Exception:
                log.debug("native cell editor: hide after windowClosing failed", exc_info=True)
            return
        log.debug("native cell editor: close dispose")
        try:
            dlg.setVisible(False)
        except Exception:
            log.exception("Failed to hide native Python cell editor")
        try:
            dlg.dispose()
        except Exception:
            log.exception("Failed to dispose native Python cell editor")

    def _ctrl(self, name: str) -> Any:
        dlg = self._dlg
        if dlg is None:
            return None
        return dlg.getControl(name)

    def _mark_dirty(self) -> None:
        if not self._loading:
            self._dirty = True

    def _set_cell_addr(self) -> None:
        from plugin.calc.python.editor import format_cell_a1

        ctrl = self._ctrl("CellAddr")
        if ctrl is None:
            return
        addr = format_cell_a1(self._code_cell if self._following_ref() else self._cell)
        try:
            if hasattr(ctrl, "setText"):
                ctrl.setText(addr)
            model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
            if model is not None and hasattr(model, "Label"):
                model.Label = addr
        except Exception:
            log.debug("native cell editor: CellAddr failed", exc_info=True)

    def _following_ref(self) -> bool:
        from plugin.calc.python.editor import _same_calc_cell

        # UNO often hands back distinct proxies for the same cell; identity (`is`)
        # would treat a self-ref as a follow. Address compare matches editor.py.
        return (
            bool(self._code_ref)
            and self._code_cell is not None
            and not _same_calc_cell(self._code_cell, self._cell)
        )

    def _apply_load(self, initial_code: str) -> None:
        from plugin.calc.python.editor import editor_load_save_as_plain
        from plugin.calc.python.formula_edit import format_data_binding_display

        self._loading = True
        self._set_cell_addr()
        code_ctrl = self._ctrl("CodeEdit")
        if code_ctrl is not None:
            set_control_text(code_ctrl, initial_code or "")
        data_text = ""
        if self._parsed_parts is not None:
            data_text = format_data_binding_display(self._parsed_parts.data_suffix)
        data_ctrl = self._ctrl("DataEdit")
        if data_ctrl is not None:
            set_control_text(data_ctrl, data_text)
            try:
                model = data_ctrl.getModel()
                if model is not None and hasattr(model, "HelpText"):
                    model.HelpText = _("A1:C1  or  A1:C1, C1:C5")
            except Exception:
                log.debug("native cell editor: DataEdit HelpText failed", exc_info=True)
        plain = editor_load_save_as_plain(
            parsed_parts=self._parsed_parts,
            initial_code=initial_code or "",
            follow_code_ref=self._following_ref(),
        )
        set_checkbox_state(self._ctrl("ChkPlainText"), 1 if plain else 0)
        self._sync_data_enabled()
        self._set_status(_("Ready"))
        self._loading = False
        self._dirty = False
        if code_ctrl is not None:
            try:
                code_ctrl.setFocus()
            except Exception:
                pass

    def _sync_data_enabled(self) -> None:
        # Twin of editor.js updateDataBindingEnabled: Data: is off when Save
        # without =PY(), except when following =PY($A$1) (data lives on the formula cell).
        disabled = bool(get_checkbox_state(self._ctrl("ChkPlainText"))) and not self._following_ref()
        for name in ("DataEdit", "DataLbl"):
            ctrl = self._ctrl(name)
            if ctrl is None:
                continue
            try:
                if hasattr(ctrl, "setEnable"):
                    ctrl.setEnable(not disabled)
                model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
                if model is not None and hasattr(model, "Enabled"):
                    model.Enabled = not disabled
            except Exception:
                log.debug("native cell editor: enable %s failed", name, exc_info=True)
        data_ctrl = self._ctrl("DataEdit")
        if data_ctrl is None:
            return
        title = (
            _("Data ranges apply only when saving as a =PY() formula.")
            if disabled
            else _("Calc injects `data` and `ranges` from these range(s) at runtime.")
        )
        try:
            model = data_ctrl.getModel()
            if model is not None and hasattr(model, "HelpText"):
                model.HelpText = title
        except Exception:
            log.debug("native cell editor: DataEdit tooltip failed", exc_info=True)

    def _set_status(self, body: str) -> None:
        set_control_text(self._ctrl("StatusEdit"), _status_text(body))

    def _code_text(self) -> str:
        ctrl = self._ctrl("CodeEdit")
        if ctrl is None:
            return ""
        try:
            if hasattr(ctrl, "getText"):
                return str(ctrl.getText() or "")
            model = ctrl.getModel()
            return str(getattr(model, "Text", "") or "")
        except Exception:
            return ""

    def _data_text(self) -> str:
        ctrl = self._ctrl("DataEdit")
        if ctrl is None:
            return ""
        try:
            if hasattr(ctrl, "getText"):
                return str(ctrl.getText() or "").strip()
            model = ctrl.getModel()
            return str(getattr(model, "Text", "") or "").strip()
        except Exception:
            return ""

    def _save(self) -> None:
        from plugin.calc.python.editor import _apply_cell_save

        self._set_status(_("Saving…"))
        save_as_plain = bool(get_checkbox_state(self._ctrl("ChkPlainText")))
        follow = self._following_ref()
        binding = self._data_text() if follow or not save_as_plain else None
        outcome = _apply_cell_save(
            self._doc,
            self._cell,
            parsed_parts=self._parsed_parts,
            new_code=self._code_text(),
            save_as_plain=save_as_plain,
            data_binding_text=binding,
            code_cell=self._code_cell if follow else None,
            code_ref=self._code_ref if follow else None,
        )
        if outcome.get("type") == "error":
            self._set_status(str(outcome.get("message") or _("Error")))
            return
        self._dirty = False
        ok_text = outcome.get("status_ok_text")
        if not ok_text:
            if outcome.get("save_as_plain"):
                ok_text = _("Saved without =PY().")
            else:
                ok_text = _("Saved.")
        self._set_status(str(ok_text))

    def _open(self, initial_code: str) -> bool:
        try:
            dlg, load_detail = load_writeragent_dialog_detail("PythonCellEditorDialog", self._ctx)
            if dlg is None:
                log.error(
                    "NativePythonCellEditorDialog: XDL load failed:\n%s",
                    load_detail or "(no load detail captured)",
                )
                self._open_failure_detail = load_detail or _(
                    "PythonCellEditorDialog could not be loaded from the extension."
                )
                self.close()
                return False
            self._dlg = dlg
            self._apply_load(initial_code)
            self._wire_listeners(dlg)
            owner = self

            class _TopWindowListener(unohelper.Base, XTopWindowListener):
                def windowClosing(self, e):
                    owner.close(toolkit_teardown=True)

                def windowClosed(self, e):
                    pass

                def windowOpened(self, e):
                    pass

                def windowMinimized(self, e):
                    pass

                def windowNormalized(self, e):
                    pass

                def windowActivated(self, e):
                    pass

                def windowDeactivated(self, e):
                    pass

                def disposing(self, Source):
                    pass

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)
            return True
        except Exception as exc:
            from plugin.scripting.editor_ipc import exception_traceback

            log.exception("NativePythonCellEditorDialog._open failed")
            self._open_failure_detail = exception_traceback(exc)
            self.close()
            return False

    def _wire_listeners(self, dlg: Any) -> None:
        owner = self

        class _SaveListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                try:
                    owner._save()
                except Exception:
                    log.exception("Native Python cell editor Save failed")
                    owner._set_status(_("Error"))

            def disposing(self, Source):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                log.debug("native cell editor: BtnCancel")
                owner.close()

            def disposing(self, Source):
                pass

        class _PlainListener(unohelper.Base, XItemListener):
            def itemStateChanged(self, rEvent):
                owner._sync_data_enabled()
                owner._mark_dirty()

            def disposing(self, Source):
                pass

        class _DirtyTextListener(unohelper.Base, XTextListener):
            def textChanged(self, rEvent):
                owner._mark_dirty()

            def disposing(self, Source):
                pass

        dlg.getControl("BtnSave").addActionListener(_SaveListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())
        chk = dlg.getControl("ChkPlainText")
        if chk is not None:
            chk.addItemListener(_PlainListener())
        dirty_listener = _DirtyTextListener()
        for name in ("CodeEdit", "DataEdit"):
            ctrl = dlg.getControl(name)
            if ctrl is not None and hasattr(ctrl, "addTextListener"):
                try:
                    ctrl.addTextListener(dirty_listener)
                except Exception:
                    log.debug("native cell editor: addTextListener %s failed", name, exc_info=True)


def show_native_python_cell_editor(
    ctx: Any,
    *,
    doc: Any,
    cell: Any,
    initial_code: str,
    parsed_parts: Any,
    code_cell: Any | None = None,
    code_ref: str | None = None,
) -> tuple[bool, str | None]:
    """Open or retarget the native cell editor. Returns (opened, failure_detail)."""
    global _active
    if _active is not None and _active.is_open:
        if _active._dirty:
            from plugin.calc.python.editor import confirm_unsaved_cell_edit, format_cell_a1

            choice = confirm_unsaved_cell_edit(ctx, format_cell_a1(_active._cell))
            if choice == "cancel":
                return True, None
            if choice == "save":
                _active._save()
                if _active._dirty:
                    return True, None
        _active.retarget(
            doc=doc,
            cell=cell,
            initial_code=initial_code,
            parsed_parts=parsed_parts,
            code_cell=code_cell,
            code_ref=code_ref,
        )
        return True, None
    inst = NativePythonCellEditorDialog(
        ctx,
        doc=doc,
        cell=cell,
        initial_code=initial_code,
        parsed_parts=parsed_parts,
        code_cell=code_cell,
        code_ref=code_ref,
    )
    if inst._opened:
        _active = inst
        return True, None
    return False, inst._open_failure_detail


def reset_native_cell_editor_for_tests() -> None:
    """Drop the singleton (unit tests)."""
    global _active
    if _active is not None:
        try:
            _active.close()
        except Exception:
            pass
    _active = None
