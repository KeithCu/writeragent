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

Usage from modules::

    from plugin.framework.dialogs import msgbox, msgbox_with_copy, copy_to_clipboard
    from plugin.framework.uno_context import get_ctx

    msgbox(get_ctx(), "Title", "Hello world")
    msgbox_with_copy(get_ctx(), "URL", "Server running at:", "https://localhost:8766")

XDL dialog loading (used by ModuleBase helpers)::

    from plugin.framework.dialogs import load_module_dialog, load_framework_dialog

    dlg = load_framework_dialog("info_action")
    dlg.getControl("MessageText").getModel().Label = "Hello"
    dlg.execute()
    dlg.dispose()
"""

import logging
from typing import Any, cast
import unohelper
from plugin.framework.listeners import BaseActionListener
from plugin.framework.worker_pool import run_in_background
from com.sun.star.awt import XActionListener
from plugin.framework.uno_context import get_desktop, get_extension_url
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.dialogs")

EXTENSION_ID = "org.extension.writeragent"


# ── Simple message box ──────────────────────────────────────────────


def msgbox(ctx, title, message):
    """Show an info message box."""
    from plugin.framework.i18n import _
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
        toolkit = smgr.createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
        box = toolkit.createMessageBox(
            window, 1, 1, _(title), _(message))  # INFOBOX, OK button
        box.execute()
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
            log.warning(
                "show_approval_dialog: no frame (parent_frame=%r, getCurrentFrame=None)",
                parent_frame,
            )
            return False
        window = frame.getContainerWindow()
        if window is None:
            log.warning("show_approval_dialog: frame has no container window")
            return False
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        title = _("Agent requests approval")
        message = (description or _("Proceed with this action?")) + (
            "\n\n" + _("Tool: %s") % tool_name if tool_name else ""
        )
        log.debug(
            "show_approval_dialog: tool_name=%s parent_frame=%s",
            tool_name,
            parent_frame is not None,
        )
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
        frame = parent_frame
        if frame is None:
            desktop = get_desktop(ctx)
            frame = desktop.getCurrentFrame()
        if frame is None:
            log.warning("show_web_search_query_edit_dialog: no frame")
            return None
        parent_window = frame.getContainerWindow()
        if parent_window is None:
            log.warning("show_web_search_query_edit_dialog: no container window")
            return None

        smgr = ctx.getServiceManager()
        dlg_model = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialogModel", ctx
        )
        dlg_model.Title = _("Edit search query")
        dlg_model.Width = 280
        dlg_model.Height = 140

        add_dialog_label(
            dlg_model, "PromptLbl", _("Search query:"), 8, 8, 264, 10, multiline=False
        )
        edit = add_dialog_edit(
            dlg_model, "QueryEdit", initial_text or "", 8, 22, 264, 72, readonly=False
        )
        edit.MultiLine = True
        edit.VScroll = True

        add_dialog_button(dlg_model, "BtnOK", _("OK"), 150, 102, 60, 14)
        add_dialog_button(dlg_model, "BtnCancel", _("Cancel"), 216, 102, 56, 14)

        dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, parent_window)

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

        dlg.getControl("BtnOK").addActionListener(_OkListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())
        dlg.execute()
        dlg.dispose()
        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_web_search_query_edit_dialog failed")
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
        clip = smgr.createInstanceWithContext(
            "com.sun.star.datatransfer.clipboard.SystemClipboard", ctx)

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
    from plugin.framework.i18n import _
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
    from plugin.framework.i18n import _
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
    from plugin.framework.i18n import _
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
    from plugin.framework.i18n import _
    if not ctx:
        log.info("MSGBOX_COPY (no ctx) - %s: %s", title, message)
        return
    try:

        smgr = ctx.ServiceManager

        dlg_model = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialogModel", ctx)
        dlg_model.Title = _(title)
        dlg_model.Width = 250
        dlg_model.Height = 80

        add_dialog_label(dlg_model, "Msg", _(message), 10, 6, 230, 42)
        add_dialog_button(dlg_model, "CopyBtn", _("Copy"), 10, 56, 50, 14)
        add_dialog_button(dlg_model, "OKBtn", _("OK"), 190, 56, 50, 14, push_button_type=1)

        dlg = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, None)

        class _CopyListener(BaseActionListener):
            def __init__(self, dialog, context, text):
                self._dlg = dialog
                self._ctx = context
                self._text = text

            def on_action_performed(self, rEvent):
                if copy_to_clipboard(self._ctx, self._text):
                    try:
                        self._dlg.getModel().getByName("CopyBtn").Label = \
                                _("Copied!")
                    except Exception as e:
                        log.debug("Failed to set CopyBtn Label: %s", e)

        dlg.getControl("CopyBtn").addActionListener(
            _CopyListener(dlg, ctx, copy_text))

        dlg.execute()
        dlg.dispose()
    except Exception:
        log.exception("Copy dialog error")
        msgbox(ctx, title, message)


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

        smgr = ctx.ServiceManager
        initial_text = build_status_fn()

        dlg_model = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialogModel", ctx)
        dlg_model.Title = title
        dlg_model.Width = 230
        dlg_model.Height = 110

        add_dialog_label(dlg_model, "StatusText", initial_text, 10, 6, 210, 72)

        # Copy button (disabled until copy_url_fn returns something)
        has_copy = copy_url_fn is not None
        if has_copy:
            add_dialog_button(dlg_model, "CopyBtn", _("Copy URL"), 10, 88, 65, 14,
                              enabled=bool(copy_url_fn() if copy_url_fn else False))

        add_dialog_button(dlg_model, "OKBtn", _("OK"), 170, 88, 50, 14, push_button_type=1)

        dlg = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, None)

        # Wire copy button
        if has_copy:
            class _CopyListener(BaseActionListener):
                def __init__(self, dialog, context, url_fn):
                    self._dlg = dialog
                    self._ctx = context
                    self._url_fn = url_fn

                def on_action_performed(self, rEvent):
                    url = self._url_fn()
                    if url and copy_to_clipboard(self._ctx, url):
                        try:
                            self._dlg.getModel().getByName("CopyBtn").Label = \
                                _("Copied!")
                        except Exception as e:
                            log.debug("Failed to set CopyBtn Label: %s", e)

            dlg.getControl("CopyBtn").addActionListener(
                _CopyListener(dlg, ctx, copy_url_fn))

        # Background update
        import time

        def _probe_update():
            time.sleep(0.05)
            try:
                updated = build_status_fn()
                dlg_model.getByName("StatusText").Label = updated
                if has_copy:
                    url = copy_url_fn() if copy_url_fn else None
                    dlg_model.getByName("CopyBtn").Enabled = bool(url)
            except Exception:
                pass  # dialog already closed

        run_in_background(_probe_update, daemon=True, name="status-dialog-probe")

        dlg.execute()
        dlg.dispose()
    except Exception:
        log.exception("Status dialog error")
        msgbox(ctx, title, build_status_fn())


# ── About dialog ─────────────────────────────────────────────────────


def about_dialog(ctx):
    """Show the WriterAgent About dialog with a clickable GitHub link."""
    try:
        from plugin.version import EXTENSION_VERSION
    except ImportError:
        from typing import cast
        EXTENSION_VERSION = cast("Any", "?")

    if not ctx:
        log.info("ABOUT (no ctx)")
        return

    try:
        smgr = ctx.ServiceManager

        dlg_model = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialogModel", ctx)
        dlg_model.Title = _("About WriterAgent")
        dlg_model.Width = 220
        dlg_model.Height = 90

        # Info text
        info_text = (
            "WriterAgent\n"
            + _("Version: %s") % EXTENSION_VERSION + "\n"
            + _("AI-powered extension for LibreOffice")
        )
        add_dialog_label(dlg_model, "Info", info_text, 10, 8, 200, 36)

        # Clickable hyperlink
        add_dialog_hyperlink(dlg_model, "GitHubLink", _("GitHub: quazardous/localwriter"),
                             "https://github.com/quazardous/localwriter", 10, 48, 200, 12)

        add_dialog_button(dlg_model, "OKBtn", _("OK"), 160, 68, 50, 14, push_button_type=1)

        dlg = smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, None)
        dlg.execute()
        dlg.dispose()
    except Exception:
        log.exception("About dialog error")
        msgbox(ctx, _("About WriterAgent"),
           _("WriterAgent {0}").format(EXTENSION_VERSION) + "\nhttps://github.com/quazardous/localwriter")


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
    from plugin.framework.i18n import _

    # Map control types to their translatable properties
    control_types = {
        'FixedText': ('Text', 'Label'),
        'Button': ('Label',),
        'CheckBox': ('Label',),
        'RadioButton': ('Label',),
        'ListBox': ('StringItemList',),
        'ComboBox': ('StringItemList',),
        'GroupBox': ('Label',),
        'FixedLine': ('Label',),
    }

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
                    if prop == 'StringItemList':
                        items = ctrl.getStringItemList()
                        if items:
                            translated = tuple(_(item) if item else "" for item in items)
                            ctrl.setStringItemList(translated)
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
    if (
        _xcc_root is None or root_child_count == 0
    ) and hasattr(dlg, "getControl") and hasattr(dlg, "getModel"):
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
    xdl_path = "plugin/modules/%s/%s.xdl" % (module_dir, dialog_name)
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


def _load_xdl(relative_path):
    """Load an XDL file from the extension bundle via DialogProvider2."""
    from plugin.framework.uno_context import get_ctx

    ctx = get_ctx()
    assert ctx is not None
    ctx_any = cast("Any", ctx)
    smgr = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
    assert smgr is not None
    base = get_extension_url()
    url = base + "/" + relative_path
    dp = cast("Any", smgr).createInstanceWithContext(
        "com.sun.star.awt.DialogProvider2", ctx_any)
    return dp.createDialog(url)


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
