"""XDL dialog generation for module config pages (Settings, list-detail)."""

import os
import xml.etree.ElementTree as ET

from scripts.manifest_common import (
    add_label as _common_add_label,
    add_checkbox as _common_add_checkbox,
    add_textfield as _common_add_textfield,
    add_numericfield as _common_add_numericfield,
    add_combobox as _common_add_combobox,
    add_button as _common_add_button,
    add_helper as _common_add_helper,
)

# ── XDL Generation (using xml.etree.ElementTree) ─────────────────────

import xml.etree.ElementTree as ET

# Layout constants for XDL pages (dialog units)
_PAGE_WIDTH = 260
_PAGE_HEIGHT = 260
_MARGIN = 6
_LABEL_WIDTH = 100
_FIELD_X = 110
_FIELD_WIDTH = 144
_ROW_HEIGHT = 14
_ROW_GAP = 4
_HELPER_HEIGHT = 10
_HELPER_GAP = 1
_BROWSE_BTN_WIDTH = 20
_BROWSE_BTN_GAP = 2

# List-detail layout constants
_LD_LIST_HEIGHT = 80
_LD_INLINE_LIST_HEIGHT = 50
_LD_BTN_WIDTH = 44
_LD_BTN_GAP = 4
_LD_LIST_WIDTH = _PAGE_WIDTH - _MARGIN * 2 - _LD_BTN_WIDTH - _LD_BTN_GAP

_DLG_NS = "http://openoffice.org/2000/dialog"
_SCRIPT_NS = "http://openoffice.org/2000/script"
_OOR_NS = "http://openoffice.org/2001/registry"
_XS_NS = "http://www.w3.org/2001/XMLSchema"

ET.register_namespace("dlg", _DLG_NS)
ET.register_namespace("script", _SCRIPT_NS)
ET.register_namespace("oor", _OOR_NS)
ET.register_namespace("xs", _XS_NS)


def _dlg(local):
    """Qualified name in dlg: namespace."""
    return "{%s}%s" % (_DLG_NS, local)


def _oor(local):
    """Qualified name in oor: namespace."""

def _pretty_name(name):
    """Convert dotted or underscored name to title case with spaces."""
    return name.replace(".", " ").replace("_", " ").title()

def _add_checkbox(board, field_name, schema, y):
    _common_add_checkbox(board, field_name, schema.get("label", ""), _FIELD_X, y, _FIELD_WIDTH, _ROW_HEIGHT)


def _add_textfield(board, field_name, schema, y, echo_char=None, multiline=False):
    h = _ROW_HEIGHT * 3 if multiline else _ROW_HEIGHT
    _common_add_textfield(board, field_name, _FIELD_X, y, _FIELD_WIDTH, h, echo_char=echo_char, multiline=multiline)


def _add_numericfield(board, field_name, schema, y):
    _common_add_numericfield(
        board, field_name, _FIELD_X, y, _FIELD_WIDTH, _ROW_HEIGHT,
        spin="true",
        min_val=schema.get("min"),
        max_val=schema.get("max"),
        step_val=schema.get("step"),
        decimal_accuracy="1" if schema.get("type") == "float" else "0"
    )


def _add_combobox(board, field_name, schema, y):
    _common_add_combobox(
        board, field_name, _FIELD_X, y, _FIELD_WIDTH, _ROW_HEIGHT,
        options=schema.get("options", []),
        dropdown="true",
        spin="true",
        border="1",
        autocomplete="true",
        linecount="20"
    )


def _add_label(board, field_name, label_text, y):
    _common_add_label(board, "lbl_%s" % field_name, label_text, _MARGIN, y + 2, _LABEL_WIDTH, _ROW_HEIGHT)


def _add_filefield(board, field_name, schema, y):
    """Add a textfield + browse button for file/folder widgets."""
    field_w = _FIELD_WIDTH - _BROWSE_BTN_WIDTH - _BROWSE_BTN_GAP
    _common_add_textfield(board, field_name, _FIELD_X, y, field_w, _ROW_HEIGHT)
    btn_x = _FIELD_X + field_w + _BROWSE_BTN_GAP
    _common_add_button(board, "btn_%s" % field_name, "...", btn_x, y, _BROWSE_BTN_WIDTH, _ROW_HEIGHT)


def _add_helper(board, field_name, helper_text, y):
    """Add a small helper text below a field, spanning full page width."""
    helper_width = _PAGE_WIDTH - _MARGIN * 2
    _common_add_helper(board, "hlp_%s" % field_name, helper_text, _MARGIN, y, helper_width, _HELPER_HEIGHT)


# Style IDs for dlg:styles block
_STYLE_BOLD = "0"       # font-weight 150 = bold (for titles)
_STYLE_SEMIBOLD = "1"   # font-weight 110 = semibold (for separator labels)


def _add_styles(window):
    """Add a dlg:styles block with bold/semibold styles to the window element.

    Must be called before the bulletinboard, as LO expects styles first.
    """
    styles = ET.SubElement(window, _dlg("styles"))
    ET.SubElement(styles, _dlg("style"), {
        _dlg("style-id"): _STYLE_BOLD,
        _dlg("font-weight"): "150",
    })
    ET.SubElement(styles, _dlg("style"), {
        _dlg("style-id"): _STYLE_SEMIBOLD,
        _dlg("font-weight"): "110",
    })


def _add_title(board, title_id, text, y):
    """Add a bold title at the top of a config page. Returns new y."""
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): title_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(y),
        _dlg("width"): str(_PAGE_WIDTH - _MARGIN * 2),
        _dlg("height"): "8",
        _dlg("value"): text,
        _dlg("style-id"): _STYLE_BOLD,
    })
    return y + 8 + _ROW_GAP


def _add_page_helper(board, helper_id, text, y):
    """Add a helper text below a title or separator. Returns new y."""
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): helper_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(y),
        _dlg("width"): str(_PAGE_WIDTH - _MARGIN * 2),
        _dlg("height"): str(_HELPER_HEIGHT),
        _dlg("value"): text,
    })
    return y + _HELPER_HEIGHT + _ROW_GAP


def _xdl_to_string(root):
    """Serialize XDL element tree to string with XML declaration and DOCTYPE."""
    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE dlg:window PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "dialog.dtd">\n'
        + xml_body + "\n"
    )


_SEPARATOR_HEIGHT = 1
_LABELED_SEPARATOR_HEIGHT = 8  # like LO's own fixedline labels
_SEPARATOR_GAP = 4


def _add_separator(board, sep_id, y, label=None):
    """Add a horizontal separator line. Returns new y after the separator.

    If *label* is given, the fixedline uses height 8 (standard LO convention)
    and a semibold style so the label renders above the line.
    """
    if label:
        h = _LABELED_SEPARATOR_HEIGHT
        attrs = {
            _dlg("id"): sep_id,
            _dlg("tab-index"): "0",
            _dlg("left"): str(_MARGIN),
            _dlg("top"): str(y),
            _dlg("width"): str(_PAGE_WIDTH - _MARGIN * 2),
            _dlg("height"): str(h),
            _dlg("value"): label,
            _dlg("style-id"): _STYLE_SEMIBOLD,
        }
    else:
        h = _SEPARATOR_HEIGHT
        attrs = {
            _dlg("id"): sep_id,
            _dlg("tab-index"): "0",
            _dlg("left"): str(_MARGIN),
            _dlg("top"): str(y),
            _dlg("width"): str(_PAGE_WIDTH - _MARGIN * 2),
            _dlg("height"): str(h),
        }
    ET.SubElement(board, _dlg("fixedline"), attrs)
    return y + h + _SEPARATOR_GAP


def _add_inline_list_detail(board, field_name, schema, y):
    """Add list_detail controls inline on the main page. Returns new y."""
    section_label = schema.get("label", field_name.replace("_", " ").title())
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): "lbl_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(y),
        _dlg("width"): str(_PAGE_WIDTH - _MARGIN * 2),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): section_label,
    })
    y += _ROW_HEIGHT + _ROW_GAP

    # Listbox
    list_y = y
    ET.SubElement(board, _dlg("menulist"), {
        _dlg("id"): "lst_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(list_y),
        _dlg("width"): str(_LD_LIST_WIDTH),
        _dlg("height"): str(_LD_INLINE_LIST_HEIGHT),
    })

    # Add button
    btn_x = _MARGIN + _LD_LIST_WIDTH + _LD_BTN_GAP
    ET.SubElement(board, _dlg("button"), {
        _dlg("id"): "add_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(list_y),
        _dlg("width"): str(_LD_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "Add",
    })

    # Remove button
    ET.SubElement(board, _dlg("button"), {
        _dlg("id"): "del_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(list_y + _ROW_HEIGHT + _ROW_GAP),
        _dlg("width"): str(_LD_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "Remove",
    })

    # Apply button
    ET.SubElement(board, _dlg("button"), {
        _dlg("id"): "apply_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(list_y + (_ROW_HEIGHT + _ROW_GAP) * 2),
        _dlg("width"): str(_LD_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "Apply",
    })

    y = list_y + _LD_INLINE_LIST_HEIGHT + _ROW_GAP

    # Field-level helper (below the list, above item fields)
    helper_text = schema.get("helper")
    if helper_text:
        _add_helper(board, field_name, helper_text, y)
        y += _HELPER_HEIGHT + _ROW_GAP

    # Detail fields
    item_fields = schema.get("item_fields", {})
    for item_fname, item_schema in item_fields.items():
        ctrl_id = "%s__%s" % (field_name, item_fname)
        label_text = item_schema.get("label", item_fname)
        widget = item_schema.get("widget", "text")

        _add_label(board, ctrl_id, label_text, y)

        if widget == "checkbox":
            _add_checkbox(board, ctrl_id, item_schema, y)
        elif widget == "password":
            _add_textfield(board, ctrl_id, item_schema, y, echo_char=42)
        elif widget in ("number", "slider"):
            _add_numericfield(board, ctrl_id, item_schema, y)
        elif widget in ("select", "combo"):
            _add_combobox(board, ctrl_id, item_schema, y)
        else:
            _add_textfield(board, ctrl_id, item_schema, y)

        y += _ROW_HEIGHT

        helper_text = item_schema.get("helper")
        if helper_text:
            y += _HELPER_GAP
            _add_helper(board, ctrl_id, helper_text, y)
            y += _HELPER_HEIGHT

        y += _ROW_GAP

    return y


def generate_xdl(module_name, config_fields, title=None,
                  page_helper=None, inline_children=None):
    """Generate an XDL dialog page for a module's config fields.

    Args:
        module_name: Dotted module name (e.g. "tunnel").
        config_fields: Ordered dict of field_name -> schema.
        title: Bold title rendered at the top (typically module description).
        page_helper: Optional helper text below the title.
        inline_children: Optional list of (child_manifest, child_config) tuples
            whose fields are appended after the parent's, each preceded by a
            labeled separator.
    """
    page_id = "WriterAgent_%s" % module_name.replace(".", "_")

    window = ET.Element(_dlg("window"), {
        _dlg("id"): page_id,
        _dlg("left"): "0",
        _dlg("top"): "0",
        _dlg("width"): str(_PAGE_WIDTH),
        _dlg("height"): str(_PAGE_HEIGHT),
        _dlg("closeable"): "true",
        _dlg("withtitlebar"): "false",
    })
    # Force namespace declarations on root
    window.set("xmlns:script", _SCRIPT_NS)

    # Styles must come before bulletinboard
    _add_styles(window)

    board = ET.SubElement(window, _dlg("bulletinboard"))

    # Hidden control to identify the module
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): "__module__",
        _dlg("tab-index"): "0",
        _dlg("left"): "0", _dlg("top"): "0",
        _dlg("width"): "0", _dlg("height"): "0",
        _dlg("value"): module_name,
    })

    # Hidden control listing inline module names (comma-separated)
    # Only include children that have visible config fields
    if inline_children:
        inline_names = ",".join(
            child_m["name"] for child_m, child_cfg in inline_children
            if any(not s.get("internal") and s.get("widget", "text") != "list_detail" and s.get("settings_persist") is not False
                   for s in child_cfg.values()))
        ET.SubElement(board, _dlg("text"), {
            _dlg("id"): "__inline_modules__",
            _dlg("tab-index"): "0",
            _dlg("left"): "0", _dlg("top"): "0",
            _dlg("width"): "0", _dlg("height"): "0",
            _dlg("value"): inline_names,
        })

    y = _MARGIN

    # Bold title (module description)
    safe = module_name.replace(".", "_")
    if title:
        y = _add_title(board, "title_%s" % safe, title, y)

    # Optional page helper below title
    if page_helper:
        y = _add_page_helper(board, "phlp_%s" % safe, page_helper, y)

    # Build ordered list of (field_name, schema) for separator logic
    field_items = list(config_fields.items())
    sep_counter = [0]

    for fi, (field_name, schema) in enumerate(field_items):
        # Internal fields are stored in registry but not shown in UI
        if schema.get("internal"):
            continue
        # Action-only fields (e.g. Settings Test) live only on SettingsDialog.xdl (manifest_registry).
        if schema.get("settings_persist") is False:
            continue

        widget = schema.get("widget", "text")

        # list_detail: embed inline or skip for separate page
        if widget == "list_detail":
            if schema.get("inline"):
                # Separator before if not the first visible field
                if fi > 0:
                    sep_counter[0] += 1
                    y = _add_separator(board, "sep_%d" % sep_counter[0], y)
                y = _add_inline_list_detail(board, field_name, schema, y)
                # Separator after if not the last field
                if fi < len(field_items) - 1:
                    sep_counter[0] += 1
                    y = _add_separator(board, "sep_%d" % sep_counter[0], y)
            continue

        label_text = schema.get("label", field_name)

        _add_label(board, field_name, label_text, y)

        if widget == "checkbox":
            _add_checkbox(board, field_name, schema, y)
        elif widget == "password":
            _add_textfield(board, field_name, schema, y, echo_char=42)
        elif widget == "textarea":
            _add_textfield(board, field_name, schema, y, multiline=True)
            y += _ROW_HEIGHT * 2
        elif widget in ("number", "slider"):
            _add_numericfield(board, field_name, schema, y)
        elif widget in ("select", "combo"):
            _add_combobox(board, field_name, schema, y)
        elif widget in ("file", "folder"):
            _add_filefield(board, field_name, schema, y)
        else:
            _add_textfield(board, field_name, schema, y)

        y += _ROW_HEIGHT

        helper_text = schema.get("helper")
        if helper_text:
            y += _HELPER_GAP
            _add_helper(board, field_name, helper_text, y)
            y += _HELPER_HEIGHT

        y += _ROW_GAP

    # ── Inline children sections ─────────────────────────────────────
    if inline_children:
        for child_m, child_config in inline_children:
            # Skip children with no visible config fields
            visible_fields = [
                (fn, s) for fn, s in child_config.items()
                if not s.get("internal") and s.get("widget", "text") != "list_detail" and s.get("settings_persist") is not False
            ]
            if not visible_fields:
                continue

            child_name = child_m["name"]
            child_safe = child_name.replace(".", "_")

            # Labeled separator (uses module title as label)
            sep_counter[0] += 1
            sep_label = child_m.get("title", _pretty_name(child_name))
            y = _add_separator(
                board, "sep_%d" % sep_counter[0], y, label=sep_label)

            # Optional helper below separator
            child_helper = child_m.get("helper")
            if child_helper:
                y = _add_page_helper(
                    board, "phlp_%s" % child_safe, child_helper, y)

            # Child config fields with prefixed IDs
            for field_name, schema in child_config.items():
                if schema.get("internal"):
                    continue
                widget = schema.get("widget", "text")
                if widget == "list_detail":
                    continue  # not supported inline-in-inline

                prefixed = "%s__%s" % (child_safe, field_name)
                label_text = schema.get("label", field_name)

                _add_label(board, prefixed, label_text, y)

                if widget == "checkbox":
                    _add_checkbox(board, prefixed, schema, y)
                elif widget == "password":
                    _add_textfield(board, prefixed, schema, y, echo_char=42)
                elif widget == "textarea":
                    _add_textfield(board, prefixed, schema, y, multiline=True)
                    y += _ROW_HEIGHT * 2
                elif widget in ("number", "slider"):
                    _add_numericfield(board, prefixed, schema, y)
                elif widget in ("select", "combo"):
                    _add_combobox(board, prefixed, schema, y)
                elif widget in ("file", "folder"):
                    _add_filefield(board, prefixed, schema, y)
                else:
                    _add_textfield(board, prefixed, schema, y)

                y += _ROW_HEIGHT

                helper_text = schema.get("helper")
                if helper_text:
                    y += _HELPER_GAP
                    _add_helper(board, prefixed, helper_text, y)
                    y += _HELPER_HEIGHT

                y += _ROW_GAP

    return _xdl_to_string(window)


def generate_list_detail_xdl(module_name, field_name, schema):
    """Generate a full-page XDL for a list_detail widget.

    Layout: listbox (left) + add/remove buttons (right),
    then detail fields below the listbox.
    """
    safe_mod = module_name.replace(".", "_")
    page_id = "WriterAgent_%s__%s" % (safe_mod, field_name)

    window = ET.Element(_dlg("window"), {
        _dlg("id"): page_id,
        _dlg("left"): "0",
        _dlg("top"): "0",
        _dlg("width"): str(_PAGE_WIDTH),
        _dlg("height"): str(_PAGE_HEIGHT),
        _dlg("closeable"): "true",
        _dlg("withtitlebar"): "false",
    })
    window.set("xmlns:script", _SCRIPT_NS)

    _add_styles(window)

    board = ET.SubElement(window, _dlg("bulletinboard"))

    # Hidden __module__ control
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): "__module__",
        _dlg("tab-index"): "0",
        _dlg("left"): "0", _dlg("top"): "0",
        _dlg("width"): "0", _dlg("height"): "0",
        _dlg("value"): module_name,
    })

    # Hidden __list_detail__ control (identifies the field)
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): "__list_detail__",
        _dlg("tab-index"): "0",
        _dlg("left"): "0", _dlg("top"): "0",
        _dlg("width"): "0", _dlg("height"): "0",
        _dlg("value"): field_name,
    })

    y = _MARGIN

    # Section label
    section_label = schema.get("label", field_name.replace("_", " ").title())
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): "lbl_section",
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(y),
        _dlg("width"): str(_PAGE_WIDTH - _MARGIN * 2),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): section_label,
    })
    y += _ROW_HEIGHT + _ROW_GAP

    # Listbox (no dropdown = full list)
    list_y = y
    ET.SubElement(board, _dlg("menulist"), {
        _dlg("id"): "lst_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(list_y),
        _dlg("width"): str(_LD_LIST_WIDTH),
        _dlg("height"): str(_LD_LIST_HEIGHT),
    })

    # Add button
    btn_x = _MARGIN + _LD_LIST_WIDTH + _LD_BTN_GAP
    ET.SubElement(board, _dlg("button"), {
        _dlg("id"): "add_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(list_y),
        _dlg("width"): str(_LD_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "Add",
    })

    # Remove button
    ET.SubElement(board, _dlg("button"), {
        _dlg("id"): "del_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(list_y + _ROW_HEIGHT + _ROW_GAP),
        _dlg("width"): str(_LD_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "Remove",
    })

    # Apply button
    ET.SubElement(board, _dlg("button"), {
        _dlg("id"): "apply_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(list_y + (_ROW_HEIGHT + _ROW_GAP) * 2),
        _dlg("width"): str(_LD_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "Apply",
    })

    y = list_y + _LD_LIST_HEIGHT + _ROW_GAP

    # Detail fields
    item_fields = schema.get("item_fields", {})
    for item_fname, item_schema in item_fields.items():
        ctrl_id = "%s__%s" % (field_name, item_fname)
        label_text = item_schema.get("label", item_fname)
        widget = item_schema.get("widget", "text")

        _add_label(board, ctrl_id, label_text, y)

        if widget == "checkbox":
            _add_checkbox(board, ctrl_id, item_schema, y)
        elif widget == "password":
            _add_textfield(board, ctrl_id, item_schema, y, echo_char=42)
        elif widget in ("number", "slider"):
            _add_numericfield(board, ctrl_id, item_schema, y)
        elif widget in ("select", "combo"):
            _add_combobox(board, ctrl_id, item_schema, y)
        else:
            _add_textfield(board, ctrl_id, item_schema, y)

        y += _ROW_HEIGHT

        helper_text = item_schema.get("helper")
        if helper_text:
            y += _HELPER_GAP
            _add_helper(board, ctrl_id, helper_text, y)
            y += _HELPER_HEIGHT

        y += _ROW_GAP

    return _xdl_to_string(window)


# ── Standalone modeless config dialogs (SettingsDialog alternative) ───

_STANDALONE_MARGIN = 8
_STANDALONE_LABEL_WIDTH = 120
_STANDALONE_FIELD_X = 130
_STANDALONE_FIELD_WIDTH = 200
_STANDALONE_ROW_HEIGHT = 14
_STANDALONE_ROW_GAP = 4
_STANDALONE_HELPER_HEIGHT = 10
_STANDALONE_HELPER_GAP = 1
_STANDALONE_TAB_TOP = 5
_STANDALONE_CONTENT_TOP = 26
_STANDALONE_FOOTER_MARGIN = 28

_DEFAULT_PAGE_ORDER = ["general", "ocr", "tables", "advanced"]
_PAGE_LABELS = {
    "general": "General",
    "ocr": "OCR",
    "tables": "Tables",
    "advanced": "Advanced",
}


def _standalone_common_attrs(field_name, y, width=None, height=None):
    return {
        _dlg("id"): field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_STANDALONE_FIELD_X),
        _dlg("top"): str(y),
        _dlg("width"): str(width or _STANDALONE_FIELD_WIDTH),
        _dlg("height"): str(height or _STANDALONE_ROW_HEIGHT),
    }


def _add_standalone_label(board, field_name, label_text, y, page):
    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): f"label_{field_name}",
        _dlg("tab-index"): "0",
        _dlg("page"): str(page),
        _dlg("left"): str(_STANDALONE_MARGIN),
        _dlg("top"): str(y + 2),
        _dlg("width"): str(_STANDALONE_LABEL_WIDTH),
        _dlg("height"): str(_STANDALONE_ROW_HEIGHT),
        _dlg("value"): label_text,
    })


def _add_standalone_field(board, field_name, schema, y, page):
    widget = schema.get("widget", "text")
    label_text = schema.get("label", field_name.replace("_", " ").title())

    if widget != "checkbox":
        _add_standalone_label(board, field_name, label_text, y, page)

    if widget == "checkbox":
        attrs = _standalone_common_attrs(field_name, y)
        attrs[_dlg("page")] = str(page)
        attrs[_dlg("left")] = str(_STANDALONE_MARGIN)
        attrs[_dlg("width")] = str(schema.get("width", 280))
        attrs[_dlg("height")] = "10"
        attrs[_dlg("value")] = label_text
        attrs[_dlg("checked")] = "true" if schema.get("default") else "false"
        ET.SubElement(board, _dlg("checkbox"), attrs)
    elif widget == "password":
        attrs = _standalone_common_attrs(field_name, y)
        attrs[_dlg("page")] = str(page)
        attrs[_dlg("echochar")] = "42"
        ET.SubElement(board, _dlg("textfield"), attrs)
    elif widget in ("number", "slider"):
        attrs = _standalone_common_attrs(field_name, y, width=80)
        attrs[_dlg("page")] = str(page)
        attrs[_dlg("spin")] = "true"
        if "min" in schema:
            attrs[_dlg("value-min")] = str(schema["min"])
        if "max" in schema:
            attrs[_dlg("value-max")] = str(schema["max"])
        attrs[_dlg("decimal-accuracy")] = "1" if schema.get("type") == "float" else "0"
        ET.SubElement(board, _dlg("numericfield"), attrs)
    elif widget in ("select", "combo"):
        attrs = _standalone_common_attrs(field_name, y)
        attrs[_dlg("page")] = str(page)
        attrs[_dlg("dropdown")] = "true"
        attrs[_dlg("spin")] = "true"
        attrs[_dlg("border")] = "1"
        attrs[_dlg("autocomplete")] = "true"
        attrs[_dlg("linecount")] = "20"
        el = ET.SubElement(board, _dlg("combobox"), attrs)
        menu = ET.SubElement(el, _dlg("menupopup"))
        for opt in schema.get("options", []):
            if isinstance(opt, dict):
                menu_val = opt.get("label", opt.get("value", str(opt)))
            else:
                menu_val = str(opt)
            ET.SubElement(menu, _dlg("menuitem"), {_dlg("value"): menu_val})
    else:
        attrs = _standalone_common_attrs(field_name, y)
        attrs[_dlg("page")] = str(page)
        ET.SubElement(board, _dlg("textfield"), attrs)

    y += _STANDALONE_ROW_HEIGHT
    helper_text = schema.get("helper")
    if helper_text:
        y += _STANDALONE_HELPER_GAP
        helper_width = int(schema.get("width", 420)) - _STANDALONE_MARGIN * 2
        ET.SubElement(board, _dlg("text"), {
            _dlg("id"): f"hlp_{field_name}",
            _dlg("tab-index"): "0",
            _dlg("page"): str(page),
            _dlg("left"): str(_STANDALONE_MARGIN),
            _dlg("top"): str(y),
            _dlg("width"): str(helper_width if helper_width > 100 else 420),
            _dlg("height"): str(_STANDALONE_HELPER_HEIGHT),
            _dlg("value"): helper_text,
        })
        y += _STANDALONE_HELPER_HEIGHT
    return y + _STANDALONE_ROW_GAP


def generate_standalone_config_dialog(module):
    """Generate a tall, tabbed, modeless-ready settings XDL for *module*."""
    cfg_dialog = module.get("config_dialog") or {}
    config = module.get("config") or {}
    module_name = module["name"]
    dialog_id = cfg_dialog.get("id") or ("WriterAgent_%sSettings" % module_name.replace(".", "_"))
    width = int(cfg_dialog.get("width") or 440)
    height = int(cfg_dialog.get("height") or 480)
    title = cfg_dialog.get("title") or module.get("title") or module_name.title()

    pages_in_use: list[str] = []
    for _fname, schema in config.items():
        if not isinstance(schema, dict) or schema.get("internal"):
            continue
        if schema.get("settings_persist") is False:
            continue
        page = str(schema.get("page") or "general").strip().lower() or "general"
        if page not in pages_in_use:
            pages_in_use.append(page)
    ordered_pages = [p for p in _DEFAULT_PAGE_ORDER if p in pages_in_use]
    for p in pages_in_use:
        if p not in ordered_pages:
            ordered_pages.append(p)
    if not ordered_pages:
        ordered_pages = ["general"]

    page_num = {name: idx + 1 for idx, name in enumerate(ordered_pages)}

    window = ET.Element(_dlg("window"), {
        _dlg("id"): dialog_id,
        _dlg("left"): "100",
        _dlg("top"): "50",
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("closeable"): "true",
        _dlg("moveable"): "true" if cfg_dialog.get("moveable", True) else "false",
        _dlg("resizeable"): "true" if cfg_dialog.get("resizeable", True) else "false",
        _dlg("title"): title,
        _dlg("page"): "1",
    })
    window.set("xmlns:script", _SCRIPT_NS)
    _add_styles(window)
    board = ET.SubElement(window, _dlg("bulletinboard"))

    ET.SubElement(board, _dlg("text"), {
        _dlg("id"): "__module__",
        _dlg("tab-index"): "0",
        _dlg("left"): "0", _dlg("top"): "0",
        _dlg("width"): "0", _dlg("height"): "0",
        _dlg("value"): module_name,
    })

    tab_x = _STANDALONE_MARGIN
    for page_name in ordered_pages:
        tab_id = "btn_tab_%s" % page_name
        tab_label = _PAGE_LABELS.get(page_name, page_name.replace("_", " ").title())
        tab_w = min(len(tab_label) * 5 + 14, 72)
        ET.SubElement(board, _dlg("button"), {
            _dlg("id"): tab_id,
            _dlg("left"): str(tab_x),
            _dlg("top"): str(_STANDALONE_TAB_TOP),
            _dlg("width"): str(tab_w),
            _dlg("height"): "14",
            _dlg("value"): tab_label,
        })
        tab_x += tab_w + 3

    page_y: dict[int, int] = {page_num[p]: _STANDALONE_CONTENT_TOP for p in ordered_pages}
    for field_name, schema in config.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("internal") or schema.get("widget") == "list_detail":
            continue
        if schema.get("settings_persist") is False:
            continue
        page_key = str(schema.get("page") or "general").strip().lower() or "general"
        pnum = page_num.get(page_key, 1)
        page_y[pnum] = _add_standalone_field(board, field_name, schema, page_y[pnum], pnum)

    footer_y = height - _STANDALONE_FOOTER_MARGIN
    buttons = cfg_dialog.get("buttons") or ["apply", "ok", "close"]
    
    btn_w = 70
    btn_gap = 10
    visible_keys = [k for k in ["apply", "ok", "close"] if k in buttons]
    btn_specs = []
    for idx, key in enumerate(visible_keys):
        label = "Apply" if key == "apply" else ("OK" if key == "ok" else "Close")
        btn_id = f"btn_{key}"
        left = width - _STANDALONE_MARGIN - (len(visible_keys) - idx) * btn_w - (len(visible_keys) - 1 - idx) * btn_gap
        btn_specs.append((key, btn_id, label, footer_y, left))

    for key, btn_id, label, top, left in btn_specs:
        btn_attrs = {
            _dlg("id"): btn_id,
            _dlg("left"): str(left),
            _dlg("top"): str(top),
            _dlg("width"): "70",
            _dlg("height"): "18",
            _dlg("value"): label,
            _dlg("tabstop"): "true",
        }
        if key == "ok":
            btn_attrs[_dlg("button-type")] = "ok"
            btn_attrs[_dlg("default")] = "true"
        elif key == "close":
            btn_attrs[_dlg("button-type")] = "cancel"
        ET.SubElement(board, _dlg("button"), btn_attrs)

    return _xdl_to_string(window)


def generate_standalone_config_dialogs(modules, output_base):
    """Write standalone config_dialog XDL files under output_base/WriterAgentDialogs/."""
    out_dir = os.path.join(output_base, "WriterAgentDialogs")
    os.makedirs(out_dir, exist_ok=True)
    dialog_names: list[str] = []
    count = 0
    for m in modules:
        cfg_dialog = m.get("config_dialog")
        if not cfg_dialog:
            continue
        dialog_id = cfg_dialog.get("id") or ("WriterAgent_%sSettings" % m["name"].replace(".", "_"))
        xdl_path = os.path.join(out_dir, "%s.xdl" % dialog_id)
        with open(xdl_path, "w", encoding="utf-8") as f:
            f.write(generate_standalone_config_dialog(m))
        dialog_names.append(dialog_id)
        count += 1
    if count:
        print("  Generated %d standalone config dialog(s) in %s" % (count, out_dir))
    return dialog_names


def update_dialog_xlb(library_dir, dialog_names, tpl_path=None):
    """Ensure dialog.xlb lists *dialog_names* (preserves static entries)."""
    tpl_path = tpl_path or os.path.join(library_dir, "dialog.xlb.tpl")
    xlb_path = os.path.join(library_dir, "dialog.xlb")
    marker = "<!-- AUTO_GENERATED_DIALOGS -->"
    static_entries = [
        "SettingsDialog",
        "EditInputDialog",
        "ChatPanelDialog",
        "EvalDialog",
        "LatexInputDialog",
        "SearchDialog",
        "SpreadsheetImportDialog",
        "PythonScriptDialog",
        "ServerStatusDialog",
        "WebSearchQueryEditDialog",
        "ShortTextInputDialog",
        "MsgBoxWithCopyDialog",
        "StatusUpdateDialog",
        "PythonTestProgressDialog",
    ]
    if os.path.isfile(tpl_path):
        with open(tpl_path, encoding="utf-8") as f:
            content = f.read()
    elif os.path.isfile(xlb_path):
        with open(xlb_path, encoding="utf-8") as f:
            content = f.read()
    else:
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE library:library PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "library.dtd">\n'
            '<library:library xmlns:library="http://openoffice.org/2000/library" '
            'library:name="WriterAgentDialogs" library:readonly="false" library:passwordprotected="false">\n'
            f"{marker}\n"
            "</library:library>\n"
        )

    generated_lines = []
    seen = set(static_entries)
    for name in static_entries:
        generated_lines.append(f' <library:element library:name="{name}"/>')
    for name in dialog_names:
        if name in seen:
            continue
        seen.add(name)
        generated_lines.append(f' <library:element library:name="{name}"/>')
    block = "\n".join(generated_lines)

    if marker in content:
        before, _after = content.split(marker, 1)
        new_content = before + marker + "\n" + block + "\n</library:library>\n"
    else:
        new_content = content.replace(
            "</library:library>",
            marker + "\n" + block + "\n</library:library>",
        )
    os.makedirs(library_dir, exist_ok=True)
    with open(xlb_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def generate_xdl_files(modules, output_dir):
    """Generate XDL dialog files for modules with config."""
    os.makedirs(output_dir, exist_ok=True)
    count = 0
    count_removed = 0
    generated_paths = set()

    # Build map: target_name -> [(child_manifest, child_config)] for
    # modules that opt into config_inline.
    # config_inline: true  -> inlined into dotted parent (tunnel.bore -> tunnel)
    # config_inline: "foo" -> inlined into module "foo"
    by_name = {m["name"]: m for m in modules}
    inline_map = {}   # target_name -> list of (child_manifest, child_config)
    inline_set = set()  # names of modules that are inlined

    # First pass: collect all inline targets
    inline_targets = {}  # name -> target
    for m in modules:
        inline_val = m.get("config_inline")
        if not inline_val:
            continue
        name = m["name"]
        if isinstance(inline_val, str):
            target = inline_val
        else:
            if "." not in name:
                continue
            target = name.rsplit(".", 1)[0]
        inline_targets[name] = target

    # Second pass: build map, skip if target is itself inlined
    for name, target in inline_targets.items():
        if target in inline_targets:
            continue  # target is itself inlined — ignore
        m = by_name[name]
        child_config = m.get("config", {})
        inline_map.setdefault(target, []).append((m, child_config))
        inline_set.add(name)

    for m in modules:
        name = m["name"]

        # Skip modules that are inlined into their parent
        if name in inline_set:
            continue

        # Standalone config_dialog modules use WriterAgentDialogs/ instead.
        if m.get("config_dialog"):
            continue

        config = m.get("config", {})

        # Gather inline children for this module (if any)
        children = inline_map.get(name)

        # Skip if no own config AND no inline children
        if not config and not children:
            continue

        safe = name.replace(".", "_")
        title = m.get("title")
        page_helper = m.get("helper")

        # Main page (regular fields, skips list_detail)
        xdl_path = os.path.join(output_dir, "%s.xdl" % safe)
        with open(xdl_path, "w") as f:
            f.write(generate_xdl(name, config,
                                 title=title,
                                 page_helper=page_helper,
                                 inline_children=children))
        generated_paths.add(xdl_path)
        count += 1

        # Separate pages for each non-inline list_detail field
        for field_name, schema in config.items():
            if schema.get("widget") != "list_detail":
                continue
            if schema.get("inline"):
                continue  # inline list_detail is on the main page
            ld_safe = "%s__%s" % (safe, field_name)
            ld_path = os.path.join(output_dir, "%s.xdl" % ld_safe)
            with open(ld_path, "w") as f:
                f.write(generate_list_detail_xdl(name, field_name, schema))
            generated_paths.add(ld_path)
            count += 1

    # Clean stale XDL files (e.g. modules that became inlined)
    for stale in os.listdir(output_dir):
        if stale.endswith(".xdl"):
            stale_path = os.path.join(output_dir, stale)
            if stale_path not in generated_paths:
                os.remove(stale_path)
                count_removed += 1

    if count:
        msg = "  Generated %d XDL dialog pages in %s" % (count, output_dir)
        if count_removed:
            msg += " (removed %d stale)" % count_removed
        print(msg)
