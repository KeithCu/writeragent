"""Shared XML dialog widget creation helpers for LibreOffice XDL generation."""

import xml.etree.ElementTree as ET

_DLG_NS = "http://openoffice.org/2000/dialog"


def _dlg(name):
    """Return namespace-prefixed XDL tag name."""
    return "{%s}%s" % (_DLG_NS, name)


def add_label(board, label_id, label_text, left, top, width, height, align=None):
    """Add a label text element to the board."""
    attrs = {
        _dlg("id"): label_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("value"): label_text,
    }
    if align:
        attrs[_dlg("align")] = align
    return ET.SubElement(board, _dlg("text"), attrs)


def add_checkbox(board, field_id, label_text, left, top, width, height, checked="false"):
    """Add a checkbox element to the board."""
    attrs = {
        _dlg("id"): field_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("value"): label_text,
        _dlg("checked"): str(checked),
    }
    return ET.SubElement(board, _dlg("checkbox"), attrs)


def add_textfield(board, field_id, left, top, width, height, echo_char=None, multiline=False):
    """Add a textfield element to the board."""
    attrs = {
        _dlg("id"): field_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
    }
    if echo_char:
        attrs[_dlg("echochar")] = str(echo_char)
    if multiline:
        attrs[_dlg("multiline")] = "true"
        attrs[_dlg("vscroll")] = "true"
    return ET.SubElement(board, _dlg("textfield"), attrs)


def add_numericfield(board, field_id, left, top, width, height, spin="true", min_val=None, max_val=None, step_val=None, decimal_accuracy="0"):
    """Add a numericfield element with spin button and limits to the board."""
    attrs = {
        _dlg("id"): field_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("spin"): str(spin),
        _dlg("decimal-accuracy"): str(decimal_accuracy),
    }
    if min_val is not None:
        attrs[_dlg("value-min")] = str(min_val)
    if max_val is not None:
        attrs[_dlg("value-max")] = str(max_val)
    if step_val is not None:
        attrs[_dlg("value-step")] = str(step_val)
    return ET.SubElement(board, _dlg("numericfield"), attrs)


def add_combobox(board, field_id, left, top, width, height, options, dropdown="true", spin="true", border="1", autocomplete="true", linecount="20"):
    """Add a combobox element (dropdown style) with pre-populated menu items."""
    attrs = {
        _dlg("id"): field_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("dropdown"): str(dropdown),
        _dlg("spin"): str(spin),
        _dlg("border"): str(border),
        _dlg("autocomplete"): str(autocomplete),
        _dlg("linecount"): str(linecount),
    }
    combo_el = ET.SubElement(board, _dlg("combobox"), attrs)
    
    # menupopup child is required for the dropdown to initialize
    menu_el = ET.SubElement(combo_el, _dlg("menupopup"))
    for opt in options:
        if isinstance(opt, dict):
            menu_val = opt.get("label", opt.get("value", str(opt)))
        else:
            menu_val = str(opt)
        ET.SubElement(menu_el, _dlg("menuitem"), {_dlg("value"): menu_val})
    return combo_el


def add_button(board, field_id, label_text, left, top, width, height):
    """Add a standard button element to the board."""
    attrs = {
        _dlg("id"): field_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("value"): label_text,
    }
    return ET.SubElement(board, _dlg("button"), attrs)


def add_helper(board, helper_id, helper_text, left, top, width, height):
    """Add a small helper text below a field."""
    attrs = {
        _dlg("id"): helper_id,
        _dlg("tab-index"): "0",
        _dlg("left"): str(left),
        _dlg("top"): str(top),
        _dlg("width"): str(width),
        _dlg("height"): str(height),
        _dlg("value"): helper_text,
    }
    return ET.SubElement(board, _dlg("text"), attrs)


def write_if_changed(path, content, mode="w", encoding="utf-8"):
    """Write content to path only if the file does not exist or the content has changed."""
    import os
    if os.path.exists(path):
        try:
            with open(path, "r" if "b" not in mode else "rb", encoding=encoding if "b" not in mode else None) as f:
                if f.read() == content:
                    return
        except Exception:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding=encoding if "b" not in mode else None) as f:
        f.write(content)
