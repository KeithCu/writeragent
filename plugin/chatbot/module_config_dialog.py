# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless module config dialogs generated from module.yaml config_dialog specs."""

from __future__ import annotations

import logging
from typing import Any, Callable, NamedTuple

from plugin.chatbot.dialogs import (
    TabListener,
    _dialog_model_element_names,
    get_optional,
    load_writeragent_dialog_detail,
    msgbox,
    set_control_enabled,
    set_control_text,
)
from plugin.chatbot.settings_fields import (
    apply_field_specs_result,
    build_module_field_specs,
    field_specs_config_values,
    find_module_manifest,
    populate_settings_control,
    read_settings_control,
)
from plugin.framework.errors import WriterAgentException, best_effort, is_disposal, suppress_disposed
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancellation, SendCancelled
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import (
    BaseActionListener,
    BaseAdjustmentListener,
    BaseTopWindowListener,
    BaseWindowListener,
)
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

_active_dialogs: dict[str, Any] = {}

_POSSIZE, _POS_Y = 15, 2  # com.sun.star.awt.PosSize.POSSIZE / .Y
_SCROLL_LINE_PX = 24
# Fraction of the dialog's growth added to (x, y, width, height) on resize.
_PREVIEW_ANCHORS = {
    "preview_area": (0, 0, 1, 1),
    "sb_preview": (1, 0, 0, 1),
    "lbl_preview_status": (0, 1, 1, 0),
    "btn_run": (1, 1, 0, 0),
    "btn_preview": (1, 1, 0, 0),
    "btn_apply": (1, 1, 0, 0),
    "btn_ok": (1, 1, 0, 0),
    "btn_close": (1, 1, 0, 0),
}


class PreviewImage(NamedTuple):
    graphic: Any  # XGraphic
    width_px: int
    height_px: int
    summary: str  # final status text


PreviewJob = Callable[[Any, dict[str, Any], SendCancellation, Callable[[str], None]], PreviewImage | str]
RunJob = Callable[[Any, dict[str, Any], SendCancellation, Callable[[str], None]], str]


class _PreviewPane:
    """Clipped image container + vertical scrollbar inside a config dialog.

    Built manually instead of using native dialog scrolling: ``ScrollableDialog::SetScrollTop``
    moves children the wrong way without clipping (``scrollabledialog.cxx``), and toggling
    ``VScroll`` on a live control recreates the peer (``unocontrol.cxx:502``). Mouse-wheel
    scrolling is absent because UNO exposes no wheel listener and VCL ``ScrollBar`` has no
    ``Command`` handler.
    """

    _dlg: Any
    _container: Any
    _image: Any
    _bar: Any
    _base_size: Any
    _aspect: float

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._dlg = dlg
        smgr = ctx.ServiceManager
        c_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlContainerModel", ctx)
        container = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlContainer", ctx)
        container.setModel(c_model)
        peer = dlg.getPeer()
        container.createPeer(peer.getToolkit(), peer)

        img_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlImageControlModel", ctx)
        img_model.ScaleImage = True
        img_model.Border = 0
        image = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlImageControl", ctx)
        image.setModel(img_model)
        container.addControl("preview_image", image)

        self._container = container
        self._image = image
        self._bar = get_optional(dlg, "sb_preview")
        pane = self

        class _ScrollListener(BaseAdjustmentListener):
            def on_adjustment_value_changed(self, e: Any) -> None:
                pane.scroll_to(int(e.Value))

        self._adj_listener: Any | None = None
        if self._bar is not None:
            self._adj_listener = _ScrollListener()
            self._bar.addAdjustmentListener(self._adj_listener)

        self._base_size = dlg.getPosSize()
        self._base: dict[str, Any] = {}
        for name in _PREVIEW_ANCHORS:
            ctrl = get_optional(dlg, name)
            if ctrl is not None:
                self._base[name] = ctrl.getPosSize()
        # preview_area exists only to carry the XDL rect; removing it prevents tab switches
        # (Model.Step changes) from re-showing it on top of our container.
        if "preview_area" in self._base:
            dlg.getModel().removeByName("preview_area")
        self._aspect = 0.0
        self.relayout()
        container.setVisible(True)

    def relayout(self) -> None:
        sz = self._dlg.getPosSize()
        dw = max(0, sz.Width - self._base_size.Width)
        dh = max(0, sz.Height - self._base_size.Height)
        for name, rect in self._base.items():
            fx, fy, fw, fh = _PREVIEW_ANCHORS[name]
            ctrl = get_optional(self._dlg, name)
            if ctrl is not None:
                ctrl.setPosSize(rect.X + fx * dw, rect.Y + fy * dh, rect.Width + fw * dw, rect.Height + fh * dh, _POSSIZE)
        area_rect = self._base.get("preview_area")
        if area_rect is not None:
            fx, fy, fw, fh = _PREVIEW_ANCHORS["preview_area"]
            aw = area_rect.Width + fw * dw
            ah = area_rect.Height + fh * dh
            self._container.setPosSize(area_rect.X + fx * dw, area_rect.Y + fy * dh, aw, ah, _POSSIZE)
            self._fit(aw, ah)

    def _fit(self, w: int, h: int) -> None:
        if not self._aspect or w <= 0 or h <= 0:
            return
        img_h = max(1, round(w * self._aspect))
        bar = self._bar
        if bar is not None:
            # Preserve scroll position as a fraction of the scrollable range; the raw bar value
            # becomes stale once the image is rescaled. Set max and visible size before value
            # so the peer clamps against the new range, then read back the clamped value.
            old_range = max(0, bar.getMaximum() - bar.getVisibleSize())
            frac = (bar.getValue() / old_range) if old_range > 0 else 0.0
            new_range = max(0, img_h - h)
            top = min(new_range, max(0, round(frac * new_range)))
            bar.setMaximum(img_h)
            bar.setVisibleSize(h)
            bar.setValue(top)
            bar.setLineIncrement(_SCROLL_LINE_PX)
            bar.setBlockIncrement(max(1, h - _SCROLL_LINE_PX))
            top = int(bar.getValue())
        else:
            top = 0
        self._image.setPosSize(0, -top, w, img_h, _POSSIZE)

    def scroll_to(self, top: int) -> None:
        self._image.setPosSize(0, -top, 0, 0, _POS_Y)

    def show(self, img: PreviewImage) -> None:
        self._image.getModel().Graphic = img.graphic
        self._aspect = img.height_px / img.width_px
        if self._bar is not None:
            self._bar.setValue(0)
        self.relayout()

    def dispose(self) -> None:
        with suppress_disposed("module config: dispose preview container", logger=log):
            self._container.dispose()


class _CallbackActionListener(BaseActionListener):
    def __init__(self, callback: Callable[[Any], None]) -> None:
        self._callback: Callable[[Any], None] = callback

    def on_action_performed(self, rEvent: Any) -> None:
        self._callback(rEvent)

def _tab_buttons_in_order(dlg: Any) -> list[Any]:
    """btn_tab_* controls in dialog-model order (the order the XDL generator writes)."""
    buttons: list[Any] = []
    for name in _dialog_model_element_names(dlg):
        if not str(name).startswith("btn_tab_"):
            continue
        btn = get_optional(dlg, str(name))
        if btn is not None:
            buttons.append(btn)
    return buttons


def get_module_config_dialog_id(module_name: str) -> str | None:
    manifest = find_module_manifest(module_name)
    if not manifest:
        return None
    cfg_dialog = manifest.get("config_dialog") or {}
    dialog_id = str(cfg_dialog.get("id") or "").strip()
    return dialog_id or None


class ModuleConfigDialog:
    """Modeless settings dialog for one MODULES entry with config_dialog metadata."""

    _ctx: Any
    _module_name: str
    _closed: bool
    _specs: list[dict[str, Any]]

    def __init__(
        self,
        ctx: Any,
        module_name: str,
        preview_job: PreviewJob | None = None,
        run_job: RunJob | None = None,
    ) -> None:
        self._ctx = ctx
        self._module_name = module_name
        self._preview_job: PreviewJob | None = preview_job
        self._run_job: RunJob | None = run_job
        self._dlg: Any | None = None
        self._closed = False
        self._top_listener: Any | None = None
        self._window_listener: Any | None = None
        self._pane: _PreviewPane | None = None
        self._run_cancel: SendCancellation | None = None
        self._specs = []

    @classmethod
    @main_thread_only
    def show(
        cls,
        ctx: Any,
        module_name: str,
        *,
        preview_job: PreviewJob | None = None,
        run_job: RunJob | None = None,
    ) -> ModuleConfigDialog:
        # Re-raising a live dialog with toFront preserves unsaved edits instead of destroying it.
        existing = _active_dialogs.get(module_name)
        if existing is not None:
            try:
                to_front = getattr(existing._dlg, "toFront", None)
                if callable(to_front):
                    to_front()
                set_focus = getattr(existing._dlg, "setFocus", None)
                if callable(set_focus):
                    set_focus()
                return existing
            except Exception as exc:
                if not is_disposal(exc):
                    raise
                existing.close()

        dialog = cls(ctx, module_name, preview_job=preview_job, run_job=run_job)
        dialog._open()
        _active_dialogs[module_name] = dialog
        return dialog

    def _forget_active(self) -> None:
        # Pop only if this instance is still registered; windowClosing returns before
        # toolkit disposing, so a show() in between must stay registered.
        if _active_dialogs.get(self._module_name) is self:
            _active_dialogs.pop(self._module_name, None)

    def _stop_preview(self) -> None:
        if self._run_cancel is not None:
            self._run_cancel.cancel()
        self._run_cancel = None
        if self._pane is not None:
            self._pane.dispose()
        self._pane = None

    def close(self, *, toolkit_teardown: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_preview()
        self._forget_active()
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        if toolkit_teardown:
            with suppress_disposed("module config: hide after windowClosing", logger=log):
                dlg.setVisible(False)
            return
        with suppress_disposed("module config: hide before dispose", logger=log):
            dlg.setVisible(False)
        with suppress_disposed("module config: dispose", logger=log):
            dlg.dispose()

    def _open(self) -> None:
        ctx = self._ctx
        dialog_id = get_module_config_dialog_id(self._module_name)
        if not dialog_id:
            raise RuntimeError("No config_dialog.id for module %s" % self._module_name)

        dlg, load_detail = load_writeragent_dialog_detail(dialog_id, ctx)
        # Raise, not log-and-return: a silent return left a dialog with _dlg None in _active_dialogs.
        if dlg is None:
            raise RuntimeError("load_detail: failed to load module dialog %s: %s" % (dialog_id, load_detail))

        self._dlg = dlg
        try:
            self._specs = build_module_field_specs(self._module_name, ctx=self._ctx, control_ids="flat")
            self._setup_tabs()
            self._wire_buttons()
            self._populate_fields()

            owner = self

            if self._preview_job is not None:
                self._pane = _PreviewPane(ctx, dlg)

                class _ResizeListener(BaseWindowListener):
                    def on_window_resized(self, rEvent: Any) -> None:
                        # Only move child peers; writing dialog model Width/Height here causes a resize loop.
                        if owner._pane is not None:
                            owner._pane.relayout()

                self._window_listener = _ResizeListener()
                dlg.addWindowListener(self._window_listener)

            class _TopWindowListener(BaseTopWindowListener):
                def on_window_closing(self, rEvent: Any) -> None:
                    owner.close(toolkit_teardown=True)

                def on_disposing(self, Source: Any) -> None:
                    owner._closed = True
                    owner._stop_preview()
                    owner._forget_active()
                    owner._dlg = None

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)
        except Exception:
            self._stop_preview()
            self._dlg = None
            best_effort(dlg.dispose, "Failed to dispose module config dialog")
            raise

    def _setup_tabs(self) -> None:
        assert self._dlg is not None
        # Step numbers follow control order, which is the page order the
        # generator writes. A new page: in module.yaml then gets a listener
        # without a second hardcoded map.
        for page_num, btn in enumerate(_tab_buttons_in_order(self._dlg), start=1):
            btn.addActionListener(TabListener(self._dlg, page_num))

    def _wire_buttons(self) -> None:
        assert self._dlg is not None
        for name, callback in (
            ("btn_run", lambda ev: self._start_job(self._run_job, "run")),
            ("btn_preview", lambda ev: self._start_job(self._preview_job, "preview")),
            ("btn_apply", lambda ev: self._apply(close=False)),
            ("btn_ok", lambda ev: self._apply(close=True)),
            ("btn_close", lambda ev: self.close()),
        ):
            btn = get_optional(self._dlg, name)
            if btn:
                btn.addActionListener(_CallbackActionListener(callback))

    def _populate_fields(self) -> None:
        assert self._dlg is not None
        for field in self._specs:
            ctrl = get_optional(self._dlg, field["name"])
            if ctrl is None:
                log.warning(
                    "Module config dialog %s missing control %r",
                    self._module_name,
                    field["name"],
                )
                continue
            populate_settings_control(ctrl, field)

    def _extract_result(self) -> dict[str, Any]:
        assert self._dlg is not None
        result: dict[str, Any] = {}
        for field in self._specs:
            name = field["name"]
            ctrl = get_optional(self._dlg, name)
            if ctrl is None:
                continue
            value = read_settings_control(ctrl, field)
            if value is None:
                continue
            result[name] = value
        return result

    def _apply(self, *, close: bool) -> None:
        try:
            result = self._extract_result()
            apply_field_specs_result(result, self._specs)
        except Exception as exc:
            # A failed save stays open. Closing here dismisses the dialog
            # after the values were not stored.
            log.exception("Failed to apply module config for %s", self._module_name)
            msgbox(self._ctx, _("Invalid Setting"), str(exc))
            return
        if close:
            self.close()

    def _config_values(self) -> dict[str, Any]:
        prefix = f"{self._module_name}."
        return {
            k.removeprefix(prefix): v
            for k, v in field_specs_config_values(self._extract_result(), self._specs).items()
        }

    def _set_status(self, text: str) -> None:
        if self._dlg is None:
            return
        lbl = get_optional(self._dlg, "lbl_preview_status")
        if lbl is None:
            return
        set_control_text(lbl, text)
        best_effort(lambda: setattr(lbl.getModel(), "HelpText", text), "module config: set status tooltip")

    def _set_job_buttons_enabled(self, enabled: bool) -> None:
        for name in ("btn_run", "btn_preview"):
            set_control_enabled(get_optional(self._dlg, name), enabled)

    def _start_job(self, job: PreviewJob | RunJob | None, kind: str) -> None:
        if self._run_cancel is not None or job is None:
            return
        values = self._config_values()
        cancel = SendCancellation()
        self._run_cancel = cancel
        self._set_job_buttons_enabled(False)
        self._set_status(_("Running…"))
        ctx = self._ctx

        def report(text: str) -> None:
            main.post(self._on_run_status, cancel, text)

        def _worker() -> None:
            try:
                outcome: PreviewImage | str = job(ctx, values, cancel, report)
            except SendCancelled:
                outcome = _("Cancelled.")
            except WriterAgentException as exc:  # ToolExecutionError, DocumentDisposedError
                outcome = str(exc)
            except Exception as exc:
                log.exception("Module config %s failed for %s", kind, self._module_name)
                if kind == "preview":
                    outcome = _("Preview failed: {error}").format(error=exc)
                else:
                    outcome = _("Run failed: {error}").format(error=exc)
            best_effort(lambda: main.post(self._finish_run, cancel, outcome), f"module config: post {kind} result")

        run_in_background(_worker, name=f"{self._module_name}-{kind}", dedicated=True)  # OCR can block for minutes

    def _on_run_status(self, token: SendCancellation, text: str) -> None:
        if self._closed or self._run_cancel is not token:
            return
        self._set_status(text)

    def _finish_run(self, token: SendCancellation, outcome: PreviewImage | str) -> None:
        if self._closed or self._run_cancel is not token:
            return
        self._run_cancel = None
        self._set_job_buttons_enabled(True)
        if isinstance(outcome, PreviewImage):
            if self._pane is not None:
                self._pane.show(outcome)
            self._set_status(outcome.summary)
        else:
            self._set_status(outcome)


def show_vision_settings_dialog(ctx: Any) -> None:
    """Open Vision / OCR settings (vision module config_dialog)."""
    # Weight: the preview/run jobs pull vision_runner, html_import and page rendering; import only when the dialog opens.
    from plugin.vision.vision_preview import run_vision_insert, run_vision_preview

    ModuleConfigDialog.show(ctx, "vision", preview_job=run_vision_preview, run_job=run_vision_insert)
