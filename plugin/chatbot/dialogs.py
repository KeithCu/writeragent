# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Dialog utilities for LibreOffice UNO.

Provides helpers for message boxes, clipboard operations, rich dialogs
(with buttons, live updates, etc.), and XDL dialog loading.

XDL loading rules (sidebar / extension dialogs):
- Use ``DialogProvider`` (``DialogProvider2`` fallback) + extension ``base_url`` from
  ``PackageInformationProvider`` (see ``load_writeragent_dialog`` /
  ``_load_xdl``). **Never** ``vnd.sun.star.script:…?location=application``
  with sidebar components — that path deadlocks.
- Multi-page dialogs: ``dlg:page`` on controls + ``dlg.getModel().Step``;
  ``tabpagecontainer`` / ``tabpage`` fail silently.
- ListBox/ComboBox: set ``StringItemList``, not only ``.Text``.
- Tab listeners: subclass ``unohelper.Base`` + ``XActionListener`` (see
  ``TabListener`` in this module). Use AppFont for geometry; explicit layout.

Usage from modules::

    from plugin.chatbot.dialogs import msgbox, msgbox_with_copy, copy_to_clipboard
    from plugin.framework.uno_context import get_ctx

    msgbox(get_ctx(), "Title", "Hello world")
    msgbox_with_copy(get_ctx(), "URL", "Server running at:", "https://localhost:8766")

XDL dialog loading (used by ModuleBase helpers)::

    from plugin.chatbot.dialogs import load_module_dialog, load_framework_dialog

    dlg = load_framework_dialog("info_action")
    dlg.getControl("MessageText").getModel().Label = "Hello"
    dlg.execute()
    dlg.dispose()
"""

import logging
from typing import Any, cast
import unohelper
from .listeners import BaseActionListener
from plugin.framework.worker_pool import run_in_background
from com.sun.star.awt import XActionListener
from plugin.framework.uno_context import get_ctx, get_desktop, get_extension_url
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.dialogs")


EXTENSION_ID = "org.extension.writeragent"


# ── Simple message box ──────────────────────────────────────────────


def msgbox(ctx, title, message, *, box_type=1):
    """Show a message box.

    Args:
        box_type: LO message box type (1=INFO, 2=WARNING, 3=ERROR, 4=QUERY).
    """
    if not ctx:
        log.info("MSGBOX (no ctx) - %s: %s", title, message)
        return
    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame()
        if frame is None:
            log.info("MSGBOX (no frame) - %s: %s", title, message)
            return
        window = frame.getContainerWindow()
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        box = toolkit.createMessageBox(window, box_type, 1, _(title), _(message))  # OK button
        log.debug("msgbox execute start title=%s", title)
        try:
            box.execute()
        finally:
            try:
                box.dispose()
            except Exception:
                log.debug("msgbox dispose failed", exc_info=True)
        if hasattr(toolkit, "processEventsToIdle"):
            toolkit.processEventsToIdle()
        log.debug("msgbox execute done title=%s", title)
    except Exception:
        log.exception("MSGBOX fallback - %s: %s", title, message)


def show_approval_dialog(ctx, description, tool_name="", parent_frame=None):
    """Show HITL approval dialog: description + Approve (Yes) / Reject (No). Runs on main thread.
    Returns True if user chose Approve, False if Reject or on error.

    If ``parent_frame`` is set (e.g. sidebar ``XFrame``), it is used as the dialog parent so the
    box is anchored correctly when ``getCurrentFrame()`` is wrong (e.g. focus in sidebar).
    """
    if not ctx:
        log.warning("show_approval_dialog: no ctx")
        return False
    try:
        frame = parent_frame
        if frame is None:
            desktop = get_desktop(ctx)
            frame = desktop.getCurrentFrame()
        if frame is None:
            log.warning("show_approval_dialog: no frame (parent_frame=%r, getCurrentFrame=None)", parent_frame)
            return False
        window = frame.getContainerWindow()
        if window is None:
            log.warning("show_approval_dialog: frame has no container window")
            return False
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        title = _("Agent requests approval")
        message = (description or _("Proceed with this action?")) + ("\n\n" + _("Tool: %s") % tool_name if tool_name else "")
        log.debug("show_approval_dialog: tool_name=%s parent_frame=%s", tool_name, parent_frame is not None)
        # 4 = QUERYBOX, 3 = BUTTONS_YES_NO. Result 2 = Yes (Approve), 3 = No (Reject), 1 = OK
        box = toolkit.createMessageBox(window, 4, 3, title, message)
        result = box.execute()
        return result in (1, 2)
    except Exception as e:
        log.exception("Approval dialog failed")
        if "com.sun.star" in str(type(e)):
            log.debug("Approval dialog UNO failure: %s", e)
        return False


def show_web_search_query_edit_dialog(ctx, parent_frame, initial_text) -> str | None:
    """Modal multiline edit for a web-search query before DuckDuckGo runs.

    ``parent_frame`` is typically the sidebar ``XFrame`` so the dialog parents correctly.

    Returns the edited text (stripped) if the user clicks OK, or ``None`` if Cancel/close/error.
    """
    if not ctx:
        log.warning("show_web_search_query_edit_dialog: no ctx")
        return None
    try:
        dlg = load_writeragent_dialog("WebSearchQueryEditDialog", ctx)
        if dlg is None:
            return None

        edit = dlg.getControl("QueryEdit")
        if edit is not None:
            edit.setText(initial_text or "")

        # Single-slot outcome: None = dialog closed without OK/Cancel; else [str] or [None].
        _outcome: list[str | None] | None = None

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                try:
                    ec = dlg.getControl("QueryEdit")
                    t = (ec.getModel().Text or "").strip() if ec and ec.getModel() else ""
                except Exception:
                    t = ""
                _outcome = [t]
                dlg.endDialog(1)

            def disposing(self, Source):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                _outcome = [None]
                dlg.endDialog(0)

            def disposing(self, Source):
                pass

        btn_ok = dlg.getControl("BtnOK")
        if btn_ok is not None:
            btn_ok.addActionListener(_OkListener())
        btn_cancel = dlg.getControl("BtnCancel")
        if btn_cancel is not None:
            btn_cancel.addActionListener(_CancelListener())

        dlg.execute()
        dlg.dispose()
        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_web_search_query_edit_dialog failed")
        return None


def show_text_input_dialog(ctx, message: str, title: str = "", default: str = "") -> str | None:
    """Modal single-line text input (no LLM / prompt controls).

    Used for short names (e.g. Save Script As). Returns stripped text on OK, ``None`` on Cancel.
    """
    if not ctx:
        log.warning("show_text_input_dialog: no ctx")
        return None
    try:
        dlg = load_writeragent_dialog("ShortTextInputDialog", ctx)
        if dlg is None:
            return None

        if title:
            dlg.getModel().Title = _(title)

        lbl = dlg.getControl("PromptLbl")
        if lbl is not None:
            lbl.getModel().Label = message

        edit = dlg.getControl("TextEdit")
        if edit is not None:
            edit.setText(default or "")

        _outcome: list[str | None] | None = None

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                try:
                    ec = dlg.getControl("TextEdit")
                    t = (ec.getModel().Text or "").strip() if ec and ec.getModel() else ""
                except Exception:
                    t = ""
                _outcome = [t]
                dlg.endDialog(1)

            def disposing(self, Source):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                _outcome = [None]
                dlg.endDialog(0)

            def disposing(self, Source):
                pass

        btn_ok = dlg.getControl("BtnOK")
        if btn_ok is not None:
            btn_ok.addActionListener(_OkListener())
        btn_cancel = dlg.getControl("BtnCancel")
        if btn_cancel is not None:
            btn_cancel.addActionListener(_CancelListener())

        if edit is not None:
            edit.setFocus()
        dlg.execute()
        dlg.dispose()
        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_text_input_dialog failed")
        return None


# ── Clipboard ────────────────────────────────────────────────────────


def copy_to_clipboard(ctx, text):
    """Copy text to system clipboard via LO API. Returns True on success."""
    if not ctx:
        return False
    try:
        import uno
        import unohelper
        from com.sun.star.datatransfer import XTransferable, DataFlavor

        smgr = ctx.ServiceManager
        clip = smgr.createInstanceWithContext("com.sun.star.datatransfer.clipboard.SystemClipboard", ctx)

        class _TextTransferable(unohelper.Base, XTransferable):
            def __init__(self, txt):
                self._text = txt

            def getTransferData(self, aFlavor):
                return self._text

            def getTransferDataFlavors(self):
                f = DataFlavor()
                f.MimeType = "text/plain;charset=utf-16"
                f.HumanPresentableName = "Unicode Text"
                f.DataType = uno.getTypeByName("string")
                return (f,)

            def isDataFlavorSupported(self, aFlavor):
                return "text/plain" in aFlavor.MimeType

        clip.setContents(_TextTransferable(text), None)
        log.info("Copied to clipboard: %s", text)
        return True
    except Exception:
        log.exception("Clipboard copy failed")
        return False


# ── Dialog control helpers ──────────────────────────────────────────


def add_dialog_button(dlg_model, name, label, x, y, width, height, push_button_type=None, enabled=True):
    """Add a button to a dialog model."""
    btn = dlg_model.createInstance("com.sun.star.awt.UnoControlButtonModel")
    btn.Name = name
    btn.PositionX = x
    btn.PositionY = y
    btn.Width = width
    btn.Height = height
    btn.Label = _(label)
    btn.Enabled = enabled
    if push_button_type is not None:
        btn.PushButtonType = push_button_type
    dlg_model.insertByName(name, btn)
    return btn


def add_dialog_label(dlg_model, name, label, x, y, width, height, multiline=True):
    """Add a fixed text label to a dialog model."""
    lbl = dlg_model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
    lbl.Name = name
    lbl.PositionX = x
    lbl.PositionY = y
    lbl.Width = width
    lbl.Height = height
    lbl.MultiLine = multiline
    lbl.Label = _(label)
    dlg_model.insertByName(name, lbl)
    return lbl


def add_dialog_edit(dlg_model, name, text, x, y, width, height, readonly=False):
    """Add an edit (text field) control to a dialog model."""
    edit = dlg_model.createInstance("com.sun.star.awt.UnoControlEditModel")
    edit.Name = name
    edit.PositionX = x
    edit.PositionY = y
    edit.Width = width
    edit.Height = height
    edit.Text = text
    edit.ReadOnly = readonly
    dlg_model.insertByName(name, edit)
    return edit


def add_dialog_hyperlink(dlg_model, name, label, url, x, y, width, height):
    """Add a clickable hyperlink to a dialog model."""
    link = dlg_model.createInstance("com.sun.star.awt.UnoControlFixedHyperlinkModel")
    link.Name = name
    link.PositionX = x
    link.PositionY = y
    link.Width = width
    link.Height = height
    link.Label = _(label)
    link.URL = url
    link.TextColor = 0x0563C1  # standard link blue
    dlg_model.insertByName(name, link)
    return link


# ── Message box with Copy button ─────────────────────────────────────


def msgbox_with_copy(ctx, title, message, copy_text):
    """Show a dialog with a message and a Copy button."""
    if not ctx:
        log.info("MSGBOX_COPY (no ctx) - %s: %s", title, message)
        return
    try:
        dlg = load_writeragent_dialog("MsgBoxWithCopyDialog", ctx)
        if dlg is None:
            msgbox(ctx, title, message)
            return

        if title:
            dlg.getModel().Title = _(title)

        msg_ctrl = dlg.getControl("Msg")
        if msg_ctrl is not None:
            msg_ctrl.getModel().Label = _(message)

        class _CopyListener(BaseActionListener):
            def __init__(self, dialog, context, text):
                self._dlg = dialog
                self._ctx = context
                self._text = text

            def on_action_performed(self, rEvent):
                if copy_to_clipboard(self._ctx, self._text):
                    try:
                        self._dlg.getModel().getByName("CopyBtn").Label = _("Copied!")
                    except Exception as e:
                        log.debug("Failed to set CopyBtn Label: %s", e)

        copy_btn = dlg.getControl("CopyBtn")
        if copy_btn is not None:
            copy_btn.addActionListener(_CopyListener(dlg, ctx, copy_text))

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                dlg.endDialog(1)
            def disposing(self, Source):
                pass

        ok_btn = dlg.getControl("OKBtn")
        if ok_btn is not None:
            ok_btn.addActionListener(_OkListener())

        dlg.execute()
        dlg.dispose()
    except Exception:
        log.exception("Copy dialog error")
        msgbox(ctx, title, message)


def msgbox_with_report(ctx, title, message, *, reportable=False, report_title="", report_extra="", box_type=3):
    """Show a message box; when ``reportable``, offer Copy URL and Report bug buttons."""
    if not reportable:
        msgbox(ctx, title, message, box_type=box_type)
        return
    if not ctx:
        log.info("MSGBOX_REPORT (no ctx) - %s: %s", title, message)
        return
    try:
        from plugin.framework.bug_report import build_github_issue_url, open_bug_report_in_browser

        dlg = load_writeragent_dialog("ErrorReportDialog", ctx)
        if dlg is None:
            msgbox(ctx, title, message, box_type=box_type)
            return

        issue_title = (report_title or title or "").strip()
        msg = (message or "").strip()
        rep = (report_extra or "").strip()
        if msg and rep and rep != msg:
            extra = f"{msg}\n\n{rep}"
        else:
            extra = rep or msg
        report_url = build_github_issue_url(title=issue_title, extra_body=extra, ctx=ctx)

        if title:
            dlg.getModel().Title = _(title)

        msg_ctrl = dlg.getControl("Msg")
        if msg_ctrl is not None:
            msg_ctrl.getModel().Label = _(message)

        class _CopyListener(BaseActionListener):
            def __init__(self, dialog, context, text):
                self._dlg = dialog
                self._ctx = context
                self._text = text

            def on_action_performed(self, rEvent):
                if copy_to_clipboard(self._ctx, self._text):
                    try:
                        self._dlg.getModel().getByName("CopyBtn").Label = _("Copied!")
                    except Exception as e:
                        log.debug("Failed to set CopyBtn Label: %s", e)

        copy_btn = dlg.getControl("CopyBtn")
        if copy_btn is not None:
            copy_btn.getModel().Label = _("Copy URL")
            copy_btn.addActionListener(_CopyListener(dlg, ctx, report_url))

        class _ReportListener(BaseActionListener):
            def __init__(self, context, dlg_title, dlg_extra):
                self._ctx = context
                self._title = dlg_title
                self._extra = dlg_extra

            def on_action_performed(self, rEvent):
                open_bug_report_in_browser(self._ctx, title=self._title, extra_body=self._extra)

        report_btn = dlg.getControl("ReportBtn")
        if report_btn is not None:
            report_btn.getModel().Label = _("Report bug...")
            report_btn.addActionListener(_ReportListener(ctx, issue_title, extra))

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                dlg.endDialog(1)

            def disposing(self, Source):
                pass

        ok_btn = dlg.getControl("OKBtn")
        if ok_btn is not None:
            ok_btn.addActionListener(_OkListener())

        dlg.execute()
        dlg.dispose()
    except Exception:
        log.exception("Report dialog error")
        msgbox(ctx, title, message, box_type=box_type)


# ── Status dialog with live updates ──────────────────────────────────


def status_dialog(ctx, title, build_status_fn, copy_url_fn=None):
    """Show a status dialog that updates live via a background thread.

    Args:
        ctx: UNO component context.
        title: Dialog title.
        build_status_fn: Callable() -> str returning the status text.
            Called once immediately, then once more after a short delay
            for live probe results.
        copy_url_fn: Optional callable() -> str returning a URL to copy.
            If provided and returns non-empty, a Copy button is shown.
    """
    if not ctx:
        log.info("STATUS (no ctx) - %s", title)
        return
    try:
        dlg = load_writeragent_dialog("StatusUpdateDialog", ctx)
        if dlg is None:
            msgbox(ctx, title, build_status_fn())
            return

        if title:
            dlg.getModel().Title = _(title)

        initial_text = build_status_fn()
        status_ctrl = dlg.getControl("StatusText")
        if status_ctrl is not None:
            status_ctrl.getModel().Label = initial_text

        # Copy button (disabled until copy_url_fn returns something)
        has_copy = copy_url_fn is not None
        copy_btn = dlg.getControl("CopyBtn")
        if copy_btn is not None:
            if has_copy:
                copy_btn.getModel().Enabled = bool(copy_url_fn() if copy_url_fn else False)
                
                class _CopyListener(BaseActionListener):
                    def __init__(self, dialog, context, url_fn):
                        self._dlg = dialog
                        self._ctx = context
                        self._url_fn = url_fn

                    def on_action_performed(self, rEvent):
                        url = self._url_fn()
                        if url and copy_to_clipboard(self._ctx, url):
                            try:
                                self._dlg.getModel().getByName("CopyBtn").Label = _("Copied!")
                            except Exception as e:
                                log.debug("Failed to set CopyBtn Label: %s", e)

                copy_btn.addActionListener(_CopyListener(dlg, ctx, copy_url_fn))
            else:
                copy_btn.getModel().Visible = False

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                dlg.endDialog(1)
            def disposing(self, Source):
                pass

        ok_btn = dlg.getControl("OKBtn")
        if ok_btn is not None:
            ok_btn.addActionListener(_OkListener())

        # Background update
        import time

        def _probe_update():
            time.sleep(0.05)
            try:
                updated = build_status_fn()
                if status_ctrl is not None:
                    status_ctrl.getModel().Label = updated
                if has_copy and copy_btn is not None:
                    url = copy_url_fn() if copy_url_fn else None
                    copy_btn.getModel().Enabled = bool(url)
            except Exception:
                pass  # dialog already closed

        run_in_background(_probe_update, daemon=True, name="status-dialog-probe")

        dlg.execute()
        dlg.dispose()
    except Exception:
        log.exception("Status dialog error")
        msgbox(ctx, title, build_status_fn())


# ── XDL dialog loading ──────────────────────────────────────────────


def _xcc(ctrl):
    """Return ``XControlContainer`` for ``ctrl``, or None.

    LibreOffice pyuno expects ``obj.queryInterface(iface)``. The ``uno`` module
    does not provide ``queryInterface`` (unlike some Java examples), which
    previously broke recursion in ``translate_dialog``.
    """
    if ctrl is None:
        return None
    from com.sun.star.awt import XControlContainer

    try:
        return ctrl.queryInterface(XControlContainer)
    except Exception:
        return None


def _uno_impl_to_control_type(impl_name):
    """Map ``stardiv.Toolkit.UnoButtonControl``-style names to ``control_types`` keys.

    VCL uses ``Uno`` + ``FixedText``/``Button``/… + ``Control``, not ``UnoControl``
    + ``Button``; the old strip only matched names starting with ``UnoControl``.
    """
    seg = impl_name.split(".")[-1]
    if seg.startswith("Uno") and seg.endswith("Control") and len(seg) > 10:
        return seg[3:-7]
    if seg.startswith("UnoControl"):
        return seg[10:]
    return seg


def _dialog_model_element_names(dlg):
    """Return control name strings from the dialog model (``ElementNames``), or ``()``."""
    try:
        dm = dlg.getModel()
        if dm is None:
            return ()
        en = getattr(dm, "ElementNames", None)
        if en is not None:
            return tuple(en)
    except Exception:
        pass
    return ()


def translate_dialog(dlg):
    """Translate all controls in a dialog at runtime.

    Walks the full control tree. XDL dialogs typically wrap fields in a
    bulletinboard child; only iterating top-level ``getControls()`` misses
    every label inside the container.
    """

    # Map control types to their translatable properties
    control_types = {"Dialog": ("Title",), "FixedText": ("Text", "Label"), "Button": ("Label",), "CheckBox": ("Label",), "RadioButton": ("Label",), "ListBox": ("StringItemList",), "ComboBox": ("StringItemList",), "GroupBox": ("Label",), "FixedLine": ("Label",)}

    _xcc_root = None
    root_child_count = 0
    try:
        _xcc_root = _xcc(dlg)
        if _xcc_root is not None:
            root_child_count = len(_xcc_root.getControls())
    except Exception:
        root_child_count = 0

    def translate_one(ctrl):
        try:
            impl_name = ctrl.getImplementationName()
            short_type = _uno_impl_to_control_type(impl_name)

            name = ctrl.getModel().Name if ctrl.getModel() else "?"

            for prop in control_types.get(short_type, ()):
                try:
                    if prop == "StringItemList":
                        # ListBox often exposes getStringItemList on the view; ComboBox
                        # typically only has StringItemList on the model.
                        model = ctrl.getModel()
                        used_control = False
                        try:
                            if hasattr(ctrl, "getStringItemList"):
                                items = ctrl.getStringItemList()
                                if items:
                                    translated = tuple(_(item) if item else "" for item in items)
                                    ctrl.setStringItemList(translated)
                                used_control = True
                        except Exception:
                            pass
                        if not used_control and model is not None and hasattr(model, "StringItemList"):
                            try:
                                items = model.StringItemList
                                if items:
                                    translated = tuple(_(item) if item else "" for item in items)
                                    model.StringItemList = translated
                            except Exception as e:
                                log.debug("Failed to translate %s.%s: %s", name, prop, e)
                    else:
                        model = ctrl.getModel()
                        if hasattr(model, prop):
                            current = getattr(model, prop)
                            if current:
                                setattr(model, prop, _(current))
                except Exception as e:
                    log.debug("Failed to translate %s.%s: %s", name, prop, e)

            xcc = _xcc(ctrl)
            if xcc:
                for child in xcc.getControls():
                    translate_one(child)
        except Exception as e:
            log.debug("Failed to inspect control for translation: %s", e)

    try:
        translate_one(dlg)
    except Exception as e:
        log.debug("Failed to translate dialog: %s", e)

    # ContainerWindow + XDL: root is often ``UnoDialogControl``, which does not
    # implement ``XControlContainer``, so ``getControls()`` is never reached.
    # Fall back to dialog model ``ElementNames`` + ``getControl(name)``.
    if (_xcc_root is None or root_child_count == 0) and hasattr(dlg, "getControl") and hasattr(dlg, "getModel"):
        names = _dialog_model_element_names(dlg)
        if names:
            for nm in names:
                try:
                    c = dlg.getControl(nm)
                    if c:
                        translate_one(c)
                except Exception as e:
                    log.debug("translate_dialog ElementNames id=%s: %s", nm, e)


def load_module_dialog(module_name, dialog_name):
    """Load an XDL dialog from a module's directory.

    Returns an XDialog ready for execute()/dispose().
    """
    module_dir = module_name.replace(".", "_")
    xdl_path = "plugin/%s/%s.xdl" % (module_dir, dialog_name)
    dlg = _load_xdl(xdl_path)
    if dlg:
        translate_dialog(dlg)
    return dlg


def load_framework_dialog(dialog_name):
    """Load an XDL dialog from the framework's directory.

    Returns an XDialog ready for execute()/dispose().
    """
    xdl_path = "plugin/framework/%s.xdl" % dialog_name
    dlg = _load_xdl(xdl_path)
    if dlg:
        translate_dialog(dlg)
    return dlg


def _create_xdl_dialog(smgr: Any, ctx: Any, dialog_url: str) -> Any | None:
    """Load an XDL dialog URL; prefer DialogProvider (Linux), fall back to DialogProvider2."""
    last_error: Exception | None = None
    for service in ("com.sun.star.awt.DialogProvider", "com.sun.star.awt.DialogProvider2"):
        try:
            dp = smgr.createInstanceWithContext(service, ctx)
            dlg = dp.createDialog(dialog_url)
            if dlg is not None:
                return dlg
        except Exception as exc:
            last_error = exc
            log.debug("createDialog via %s failed for %s: %s", service, dialog_url, exc)
    if last_error is not None:
        log.warning("Failed to load XDL dialog %s: %s", dialog_url, last_error)
    return None


def load_writeragent_dialog(dialog_name, ctx=None):
    """Load an XDL dialog from the WriterAgentDialogs/ directory."""
    if ctx is None:
        ctx = get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    if hasattr(ctx_any, "getServiceManager"):
        smgr = ctx_any.getServiceManager()
    else:
        smgr = getattr(ctx_any, "ServiceManager", None)
    assert smgr is not None

    # Fallback for unit testing when ServiceManager or context is mocked
    if "Mock" in type(smgr).__name__ or "Mock" in type(ctx).__name__:
        return smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx_any)

    base = get_extension_url(ctx)
    url = base + "/WriterAgentDialogs/" + dialog_name + ".xdl"
    dlg = _create_xdl_dialog(smgr, ctx_any, url)
    if dlg:
        translate_dialog(dlg)
    return dlg


def _load_xdl(relative_path):
    """Load an XDL file from the extension bundle via DialogProvider (+ DP2 fallback)."""

    ctx = get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    if hasattr(ctx_any, "getServiceManager"):
        smgr = ctx_any.getServiceManager()
    else:
        smgr = getattr(ctx_any, "ServiceManager", None)
    assert smgr is not None
    base = get_extension_url(ctx)
    url = base + "/" + relative_path
    return _create_xdl_dialog(smgr, ctx_any, url)


def get_optional(root_window, name):
    """Return control by name or None if missing. Use for optional XDL controls.

    Useful for backward-compatible dialogs where controls may not exist in all versions.
    """
    try:
        return root_window.getControl(name)
    except Exception as e:
        # Expected exception from UNO when an element is not found,
        # but catch Exception broadly since LibreOffice Python bridges
        # raise varying error types across platforms when missing names.
        if "DisposedException" in str(type(e)):
            log.warning("get_optional %s error: control disposed %s", name, e)
        else:
            log.debug("get_optional %s error: %s", name, e)
        return None


def is_checkbox_control(ctrl):
    """Return True if the control is a checkbox (UnoControlCheckBox or has State/setState).

    Handles LibreOffice checkbox quirks: checks service type, control methods, and model properties.
    """
    if not ctrl:
        return False
    try:
        if ctrl.supportsService("com.sun.star.awt.UnoControlCheckBox"):
            return True
        if hasattr(ctrl, "setState") or hasattr(ctrl, "getState"):
            return True
        if hasattr(ctrl.getModel(), "State"):
            return True
    except Exception as e:
        log.debug("is_checkbox_control exception: %s", e)
    return False


def set_control_enabled(ctrl, enabled):
    """Safely set the enabled state of a control or its model.
    Logs instead of crashing if the capability is missing."""
    if not ctrl:
        return
    try:
        if hasattr(ctrl, "setEnable"):
            ctrl.setEnable(enabled)
        elif hasattr(ctrl, "getModel") and hasattr(ctrl.getModel(), "Enabled"):
            ctrl.getModel().Enabled = enabled
    except Exception as e:
        log.debug("set_control_enabled exception: %s", e)


def set_control_visible(ctrl, visible):
    """Safely set the visibility state of a control or its model.
    Logs instead of crashing if the capability is missing."""
    if not ctrl:
        return
    try:
        if hasattr(ctrl, "setVisible"):
            ctrl.setVisible(visible)
        elif hasattr(ctrl, "getModel") and hasattr(ctrl.getModel(), "Visible"):
            ctrl.getModel().Visible = visible
    except Exception as e:
        log.debug("set_control_visible exception: %s", e)


def get_control_text(ctrl, default=""):
    """Safely get the text of a control.
    Returns default if missing or on error."""
    if not ctrl:
        return default
    try:
        if hasattr(ctrl, "getText"):
            return ctrl.getText()
        elif hasattr(ctrl, "getModel") and hasattr(ctrl.getModel(), "Text"):
            return ctrl.getModel().Text
    except Exception as e:
        log.debug("get_control_text exception: %s", e)
    return default


def set_control_text(ctrl, text):
    """Safely set the text of a control.
    Logs instead of crashing if the capability is missing.

    For FixedText, some LibreOffice builds render ``model.Text`` and others ``model.Label``;
    set both when present so labels (e.g. chat ``backend_indicator``) update visibly.
    """
    if not ctrl:
        return
    try:
        if hasattr(ctrl, "setText"):
            ctrl.setText(text)
        model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
        if model is not None:
            if hasattr(model, "Text"):
                model.Text = text
            if hasattr(model, "Label"):
                model.Label = text
    except Exception as e:
        log.debug("set_control_text exception: %s", e)


def get_checkbox_state(ctrl):
    """Return checkbox state 0 or 1. Prefer control getState(), else model.State.

    Handles both control-level getState() and model-level State property.
    """
    if not ctrl:
        return 0
    try:
        if hasattr(ctrl, "getState"):
            return ctrl.getState()
        if hasattr(ctrl.getModel(), "State"):
            return ctrl.getModel().State
    except Exception as e:
        log.debug("get_checkbox_state exception: %s", e)
    return 0


def set_checkbox_state(ctrl, value):
    """Set checkbox state to 0 or 1. Prefer control setState(), else model.State.

    Handles both control-level setState() and model-level State property.
    """
    if not ctrl:
        return
    try:
        if hasattr(ctrl, "setState"):
            ctrl.setState(value)
        elif hasattr(ctrl.getModel(), "State"):
            ctrl.getModel().State = value
    except Exception as e:
        log.debug("set_checkbox_state error: %s", e)


class TabListener(BaseActionListener):
    """Listener for tab buttons in multi-page XDL dialogs.

    Usage: dlg.getControl("btn_tab_name").addActionListener(TabListener(dlg, page_number))

    The XDL dialog must use dlg:page attributes on controls, and the dialog's Step
    property controls which page is visible.
    """

    def __init__(self, dialog, page):
        self._dlg = dialog
        self._page = page

    def on_action_performed(self, rEvent):
        """Switch to the specified page when button is clicked."""
        self._dlg.getModel().Step = self._page
