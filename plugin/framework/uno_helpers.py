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
import uno
import unohelper
from com.sun.star.awt import XActionListener
from com.sun.star.beans import PropertyValue


def get_desktop(ctx=None):
    """Return the UNO Desktop instance."""
    from plugin.framework.uno_context import get_ctx
    ctx = ctx or get_ctx()
    smgr = ctx.getServiceManager()
    return smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)


def get_active_document(ctx=None):
    """Return the currently active document model."""
    try:
        desktop = get_desktop(ctx)
        return desktop.getCurrentComponent()
    except Exception:
        return None


def get_package_info(ctx=None):
    """Return the PackageInformationProvider singleton."""
    from plugin.framework.uno_context import get_ctx
    ctx = ctx or get_ctx()
    return ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")


def get_extension_url(ctx=None, extension_id="org.extension.writeragent"):
    """Return the base URL of the extension package."""
    pip = get_package_info(ctx)
    if not pip:
        return ""
    return pip.getPackageLocation(extension_id)


def get_extension_path(ctx=None, extension_id="org.extension.writeragent"):
    """Return the local filesystem path of the extension package."""
    url = get_extension_url(ctx, extension_id)
    if not url:
        return ""
    if url.startswith("file://"):
        return str(uno.fileUrlToSystemPath(url))
    return url


def is_writer(model):
    """Return True if model is a Writer document."""
    try:
        return model.supportsService("com.sun.star.text.TextDocument")
    except Exception:
        return False


def is_calc(model):
    """Return True if model is a Calc document."""
    try:
        return model.supportsService("com.sun.star.sheet.SpreadsheetDocument")
    except Exception:
        return False


def is_draw(model):
    """Return True if model is a Draw/Impress document."""
    try:
        return (model.supportsService("com.sun.star.drawing.DrawingDocument") or 
                model.supportsService("com.sun.star.presentation.PresentationDocument"))
    except Exception:
        return False




def get_optional(root_window, name):
    """Return control by name or None if missing. Use for optional XDL controls.
    
    Useful for backward-compatible dialogs where controls may not exist in all versions.
    """
    try:
        return root_window.getControl(name)
    except Exception:
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
    except Exception:
        pass
    return False


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
    except Exception:
        pass
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
    except Exception:
        pass


class TabListener(unohelper.Base, XActionListener):
    """Listener for tab buttons in multi-page XDL dialogs.
    
    Usage: dlg.getControl("btn_tab_name").addActionListener(TabListener(dlg, page_number))
    
    The XDL dialog must use dlg:page attributes on controls, and the dialog's Step
    property controls which page is visible.
    """
    def __init__(self, dialog, page):
        self._dlg = dialog
        self._page = page
    
    def actionPerformed(self, ev):
        """Switch to the specified page when button is clicked."""
        self._dlg.getModel().Step = self._page
    
    def disposing(self, ev):
        """Required by XActionListener interface."""
        pass


def get_document_property(model, name, default=None):
    """Get a custom document property from the model."""
    try:
        if hasattr(model, "getDocumentProperties"):
            props = model.getDocumentProperties().UserDefinedProperties
            if props.hasByName(name):
                return props.getPropertyValue(name)
    except Exception:
        pass
    return default


def set_document_property(model, name, value):
    """Set a custom document property in the model."""
    try:
        if hasattr(model, "getDocumentProperties"):
            props = model.getDocumentProperties().UserDefinedProperties
            if props is not None and hasattr(props, "hasByName") and not props.hasByName(name):
                # Using a fixed type (string) for session IDs
                from com.sun.star.beans.PropertyAttribute import REMOVABLE
                props.addProperty(name, REMOVABLE, str(value))
            elif props is not None and hasattr(props, "setPropertyValue"):
                props.setPropertyValue(name, str(value))
    except Exception as e:
        # Fallback to debug log if available, but avoid circular imports
        try:
            from plugin.framework.logging import debug_log
            debug_log("set_document_property error: %s" % e, context="Chat")
        except Exception:
            pass
