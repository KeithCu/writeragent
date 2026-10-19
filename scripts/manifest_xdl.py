"""XDL dialog generation for standalone config_dialog XDL plus dialog.xlb."""

import os
import xml.etree.ElementTree as ET

from scripts.manifest_common import (
    require_widget,
    write_if_changed as _write_if_changed,
)

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
    return "{%s}%s" % (_OOR_NS, local)


def _pretty_name(name):
    """Convert dotted or underscored name to title case with spaces."""
    return name.replace(".", " ").replace("_", " ").title()


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




def _xdl_to_string(root):
    """Serialize XDL element tree to string with XML declaration and DOCTYPE."""
    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE dlg:window PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "dialog.dtd">\n'
        + xml_body + "\n"
    )


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


_STANDALONE_ALLOWED_WIDGETS = frozenset({
    "text",
    "checkbox",
    "password",
    "number",
    "slider",
    "select",
    "combo",
})


def _add_standalone_field(board, field_name, schema, y, page, dialog_width, *, where=""):
    widget = require_widget(schema, _STANDALONE_ALLOWED_WIDGETS, where=where or field_name)
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
        # What was wrong: helper width used the field width (default 420) minus
        # the margins, so a 350-wide Vision window drew 404-wide lines. The
        # dialog width is already known here; the line is that width minus the
        # margins, and it starts at the left margin.
        helper_width = int(dialog_width) - _STANDALONE_MARGIN * 2
        ET.SubElement(board, _dlg("text"), {
            _dlg("id"): f"hlp_{field_name}",
            _dlg("tab-index"): "0",
            _dlg("page"): str(page),
            _dlg("left"): str(_STANDALONE_MARGIN),
            _dlg("top"): str(y),
            _dlg("width"): str(helper_width),
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
        if schema.get("internal"):
            continue
        if schema.get("settings_persist") is False:
            continue
        page_key = str(schema.get("page") or "general").strip().lower() or "general"
        pnum = page_num.get(page_key, 1)
        where = f"{module_name}.{field_name}"
        page_y[pnum] = _add_standalone_field(board, field_name, schema, page_y[pnum], pnum, width, where=where)

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
    """Write standalone config_dialog XDL files under output_base/Dialogs/."""
    out_dir = os.path.join(output_base, "Dialogs")
    os.makedirs(out_dir, exist_ok=True)
    dialog_names: list[str] = []
    count = 0
    for m in modules:
        cfg_dialog = m.get("config_dialog")
        if not cfg_dialog:
            continue
        dialog_id = cfg_dialog.get("id") or ("WriterAgent_%sSettings" % m["name"].replace(".", "_"))
        xdl_path = os.path.join(out_dir, "%s.xdl" % dialog_id)
        _write_if_changed(xdl_path, generate_standalone_config_dialog(m))
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
        "PythonCellEditorDialog",
        "PythonSidebarDialog",
        "ServerStatusDialog",
        "WebSearchQueryEditDialog",
        "ShortTextInputDialog",
        "MsgBoxWithCopyDialog",
        "PythonTestProgressDialog",
        "NewScriptDialog",
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
            'library:name="Dialogs" library:readonly="false" library:passwordprotected="false">\n'
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
    _write_if_changed(xlb_path, new_content)


def remove_stale_generated_xdls(dialogs_dir, keep_basenames):
    """Delete any *.xdl in dialogs_dir not present in keep_basenames."""
    if not os.path.isdir(dialogs_dir):
        return 0
    keep = set(keep_basenames)
    count_removed = 0
    for fname in os.listdir(dialogs_dir):
        if fname.endswith(".xdl") and fname not in keep:
            os.remove(os.path.join(dialogs_dir, fname))
            count_removed += 1
    return count_removed
