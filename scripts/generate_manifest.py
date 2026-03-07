#!/usr/bin/env python3
"""Generate _manifest.py and XCS/XCU from module.yaml files.

Reads each module.yaml under plugin/modules/, validates it, and produces:
  - build/generated/_manifest.py     — Python dict for runtime
  - build/generated/registry/*.xcs   — LO config schemas
  - build/generated/registry/*.xcu   — LO config defaults
  - Generates description.xml from description.xml.tpl with version

Usage:
    python3 scripts/generate_manifest.py
    python3 scripts/generate_manifest.py --modules core mcp ai_openai
"""

import argparse
import json
import os
import re
import sys

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml",
          file=sys.stderr)
    sys.exit(1)


def find_modules(modules_dir, filter_names=None):
    """Find all module.yaml files recursively and return parsed manifests.

    Module name comes from the ``name`` field in module.yaml.
    Directory convention: dots map to underscores (tunnel.bore -> tunnel_bore/).
    Falls back to directory-derived name if ``name`` is absent.
    """
    manifests = []
    for dirpath, dirnames, filenames in os.walk(modules_dir):
        if "module.yaml" not in filenames:
            continue
        # Build dotted module name from relative path
        rel = os.path.relpath(dirpath, modules_dir)
        module_name = rel.replace(os.sep, ".")

        if filter_names:
            top_level = module_name.split(".")[0]
            if module_name not in filter_names and top_level not in filter_names:
                continue

        yaml_path = os.path.join(dirpath, "module.yaml")
        with open(yaml_path) as f:
            manifest = yaml.safe_load(f)
        manifest.setdefault("name", module_name)
        manifests.append(manifest)

    return manifests


def topo_sort(modules):
    """Sort modules by dependency order (core first)."""
    by_name = {m["name"]: m for m in modules}
    provides = {}
    for m in modules:
        for svc in m.get("provides_services", []):
            provides[svc] = m["name"]

    visited = set()
    order = []

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        m = by_name.get(name)
        if m is None:
            return
        for req in m.get("requires", []):
            provider = provides.get(req, req)
            if provider in by_name:
                visit(provider)
        order.append(m)

    if "core" in by_name:
        visit("core")
    for name in by_name:
        visit(name)

    return order


def _pretty_name(name):
    """Convert dotted or underscored name to title case with spaces."""
    return name.replace(".", " ").replace("_", " ").title()


def _json_to_python(text):
    """Convert JSON literals to Python literals (true->True, false->False, null->None)."""
    # Only replace JSON keywords when they appear as values, not inside strings
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            result.append(ch)
            escape = False
            i += 1
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            i += 1
            continue
        if in_string:
            result.append(ch)
            i += 1
            continue
        # Outside string: replace JSON keywords
        for jval, pyval in (("true", "True"), ("false", "False"), ("null", "None")):
            if text[i:i+len(jval)] == jval:
                # Check it's a whole word (not part of a larger identifier)
                before_ok = (i == 0 or not text[i-1].isalnum())
                after_ok = (i + len(jval) >= len(text) or not text[i+len(jval)].isalnum())
                if before_ok and after_ok:
                    result.append(pyval)
                    i += len(jval)
                    break
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def generate_manifest_py(modules, output_path):
    """Generate _manifest.py with module descriptors as Python dicts."""
    from plugin.version import EXTENSION_VERSION

    lines = [
        '"""Auto-generated module manifest. DO NOT EDIT."""',
        "",
        "VERSION = %r" % EXTENSION_VERSION,
        "",
        "MODULES = [",
    ]
    for m in modules:
        # Clean repr — only keep runtime-relevant keys
        entry = {
            "name": m["name"],
            "title": m.get("title", ""),
            "requires": m.get("requires", []),
            "provides_services": m.get("provides_services", []),
            "config": m.get("config", {}),
            "actions": list(m.get("actions", {}).keys()),
            "action_icons": {k: v["icon"] for k, v in m.get("actions", {}).items() if v.get("icon")},
        }
        # json.dumps then convert true/false/null to Python True/False/None
        json_text = json.dumps(entry, indent=8)
        lines.append("    %s," % _json_to_python(json_text))
    lines.append("]")
    lines.append("")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print("  Generated %s (%d modules)" % (output_path, len(modules)))


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
    return "{%s}%s" % (_OOR_NS, local)


def _common_attrs(field_name, y, width=None, height=None):
    """Common XDL element attributes."""
    return {
        _dlg("id"): field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_FIELD_X),
        _dlg("top"): str(y),
        _dlg("width"): str(width or _FIELD_WIDTH),
        _dlg("height"): str(height or _ROW_HEIGHT),
    }


def _add_checkbox(board, field_name, schema, y):
    attrs = _common_attrs(field_name, y)
    attrs[_dlg("checked")] = "false"
    ET.SubElement(board, _dlg("checkbox"), attrs)


def _add_textfield(board, field_name, schema, y, echo_char=None, multiline=False):
    h = _ROW_HEIGHT * 3 if multiline else _ROW_HEIGHT
    attrs = _common_attrs(field_name, y, height=h)
    if echo_char:
        attrs[_dlg("echochar")] = str(echo_char)
    if multiline:
        attrs[_dlg("multiline")] = "true"
        attrs[_dlg("vscroll")] = "true"
    ET.SubElement(board, _dlg("textfield"), attrs)


def _add_numericfield(board, field_name, schema, y):
    attrs = _common_attrs(field_name, y)
    attrs[_dlg("spin")] = "true"
    if "min" in schema:
        attrs[_dlg("value-min")] = str(schema["min"])
    if "max" in schema:
        attrs[_dlg("value-max")] = str(schema["max"])
    if "step" in schema:
        attrs[_dlg("value-step")] = str(schema["step"])
    attrs[_dlg("decimal-accuracy")] = "1" if schema.get("type") == "float" else "0"
    ET.SubElement(board, _dlg("numericfield"), attrs)


def _add_menulist(board, field_name, schema, y):
    attrs = _common_attrs(field_name, y)
    attrs[_dlg("spin")] = "true"
    attrs[_dlg("dropdown")] = "true"
    ET.SubElement(board, _dlg("menulist"), attrs)


def _add_combobox(board, field_name, schema, y):
    attrs = _common_attrs(field_name, y)
    attrs[_dlg("dropdown")] = "true"
    attrs[_dlg("autocomplete")] = "true"
    attrs[_dlg("linecount")] = "20"
    combo_el = ET.SubElement(board, _dlg("combobox"), attrs)
    # menupopup child is required for the dropdown to initialize
    ET.SubElement(combo_el, _dlg("menupopup"))


def _add_label(board, field_name, label_text, y):
    attrs = {
        _dlg("id"): "lbl_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(y + 2),
        _dlg("width"): str(_LABEL_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): label_text,
    }
    ET.SubElement(board, _dlg("text"), attrs)


def _add_filefield(board, field_name, schema, y):
    """Add a textfield + browse button for file/folder widgets."""
    field_w = _FIELD_WIDTH - _BROWSE_BTN_WIDTH - _BROWSE_BTN_GAP
    attrs = _common_attrs(field_name, y, width=field_w)
    ET.SubElement(board, _dlg("textfield"), attrs)
    # Browse button "..."
    btn_x = _FIELD_X + field_w + _BROWSE_BTN_GAP
    btn_attrs = {
        _dlg("id"): "btn_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(btn_x),
        _dlg("top"): str(y),
        _dlg("width"): str(_BROWSE_BTN_WIDTH),
        _dlg("height"): str(_ROW_HEIGHT),
        _dlg("value"): "...",
    }
    ET.SubElement(board, _dlg("button"), btn_attrs)


def _add_helper(board, field_name, helper_text, y):
    """Add a small helper text below a field, spanning full page width."""
    helper_width = _PAGE_WIDTH - _MARGIN * 2
    attrs = {
        _dlg("id"): "hlp_%s" % field_name,
        _dlg("tab-index"): "0",
        _dlg("left"): str(_MARGIN),
        _dlg("top"): str(y),
        _dlg("width"): str(helper_width),
        _dlg("height"): str(_HELPER_HEIGHT),
        _dlg("value"): helper_text,
    }
    ET.SubElement(board, _dlg("text"), attrs)


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
        elif widget == "select":
            _add_menulist(board, ctrl_id, item_schema, y)
        elif widget == "combo":
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
            if any(not s.get("internal") and s.get("widget", "text") != "list_detail"
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
        elif widget == "select":
            _add_menulist(board, field_name, schema, y)
        elif widget == "combo":
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
                if not s.get("internal") and s.get("widget", "text") != "list_detail"
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
                elif widget == "select":
                    _add_menulist(board, prefixed, schema, y)
                elif widget == "combo":
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
        elif widget == "select":
            _add_menulist(board, ctrl_id, item_schema, y)
        elif widget == "combo":
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


# ── Addons.xcu Generation ────────────────────────────────────────────

# Context name mapping: short names → LO document service names
_CONTEXT_MAP = {
    "writer": "com.sun.star.text.TextDocument",
    "calc": "com.sun.star.sheet.SpreadsheetDocument",
    "draw": "com.sun.star.drawing.DrawingDocument",
    "impress": "com.sun.star.presentation.PresentationDocument",
    "web": "com.sun.star.text.WebDocument",
    "global": "com.sun.star.text.GlobalDocument",
}

# Default context: all document types
_DEFAULT_CONTEXT = ",".join(sorted(_CONTEXT_MAP.values()))

_PROTOCOL = "org.extension.writeragent"


def _resolve_context(context_list):
    """Convert a list of short context names to a LO context string.

    Returns comma-separated LO service names, or the default (all types)
    if context_list is empty/None.
    """
    if not context_list:
        return _DEFAULT_CONTEXT
    resolved = []
    for name in context_list:
        svc = _CONTEXT_MAP.get(name)
        if svc:
            resolved.append(svc)
        else:
            # Allow raw LO service names
            resolved.append(name)
    return ",".join(sorted(resolved))


def _menu_node(parent, node_name, title=None, url=None, context=None,
               target="_self", has_icon=False):
    """Create a menu <node> element for Addons.xcu.

    Args:
        has_icon: If True, emit an empty ImageIdentifier so LO reserves
                  space for a runtime icon (set via XImageManager API).
    """
    node = ET.SubElement(parent, "node", {
        _oor("name"): node_name,
        _oor("op"): "replace",
    })
    if url:
        url_prop = ET.SubElement(node, "prop", {_oor("name"): "URL"})
        ET.SubElement(url_prop, "value").text = url
    if title:
        title_prop = ET.SubElement(node, "prop", {_oor("name"): "Title"})
        val = ET.SubElement(title_prop, "value")
        val.set("xml:lang", "en-US")
        val.text = title
    if context:
        ctx_prop = ET.SubElement(node, "prop", {
            _oor("name"): "Context",
            _oor("type"): "xs:string",
        })
        ET.SubElement(ctx_prop, "value").text = context
    if url and url != "private:separator":
        tgt_prop = ET.SubElement(node, "prop", {
            _oor("name"): "Target",
            _oor("type"): "xs:string",
        })
        ET.SubElement(tgt_prop, "value").text = target
    if has_icon:
        img_prop = ET.SubElement(node, "prop", {
            _oor("name"): "ImageIdentifier",
            _oor("type"): "xs:string",
        })
        ET.SubElement(img_prop, "value")
    return node


def _build_menu_entries(submenu_el, entries, actions, module_name, counter,
                        icon_entries=None):
    """Recursively build menu entries under a <node oor:name="Submenu">.

    Args:
        submenu_el: Parent Submenu element.
        entries: List of menu entry dicts from YAML.
        actions: Dict of action definitions from YAML.
        module_name: Module name for URL prefix.
        counter: Mutable list [int] for unique node naming.
        icon_entries: Optional list to collect (command_url, module_name,
                      icon_prefix) tuples for the Images section.
    """
    for entry in entries:

        counter[0] += 1
        node_id = "M%d" % counter[0]

        if entry.get("separator"):
            _menu_node(submenu_el, node_id, url="private:separator")
            continue

        action_name = entry.get("action")
        if action_name:
            action_def = actions.get(action_name, {})
            title = entry.get("title") or action_def.get("title", action_name)
            url = "%s:%s.%s" % (_PROTOCOL, module_name, action_name)
            context = _resolve_context(entry.get("context"))
            icon_prefix = action_def.get("icon")
            has_icon = bool(icon_prefix)
            _menu_node(submenu_el, node_id, title=title, url=url,
                       context=context, has_icon=has_icon)
            if has_icon and icon_entries is not None:
                icon_entries.append((url, module_name, icon_prefix))
        elif entry.get("title") and entry.get("submenu"):
            # Submenu container
            title = entry["title"]
            url = "%s:NoOp" % _PROTOCOL
            context = _resolve_context(entry.get("context"))
            node = _menu_node(submenu_el, node_id, title=title, url=url,
                              context=context)
            child_submenu = ET.SubElement(node, "node",
                                         {_oor("name"): "Submenu"})
            _build_menu_entries(child_submenu, entry["submenu"], actions,
                                module_name, counter,
                                icon_entries=icon_entries)
        else:
            continue


def generate_addons_xcu(modules, framework_manifest, output_path):
    """Generate Addons.xcu from module and framework menu/action declarations.

    Args:
        modules: Sorted list of module manifests (topo-sort order).
        framework_manifest: Framework-level manifest (plugin.yaml), or None.
        output_path: Path for the generated Addons.xcu.
    """
    root = ET.Element(_oor("component-data"), {
        _oor("name"): "Addons",
        _oor("package"): "org.openoffice.Office",
    })
    root.set("xmlns:xs", _XS_NS)

    addon_ui = ET.SubElement(root, "node", {_oor("name"): "AddonUI"})
    menubar = ET.SubElement(addon_ui, "node",
                            {_oor("name"): "OfficeMenuBar"})
    top_menu = ET.SubElement(menubar, "node", {
        _oor("name"): "org.extension.writeragent.menubar",
        _oor("op"): "replace",
    })

    # Top-level menu title
    title_prop = ET.SubElement(top_menu, "prop", {
        _oor("name"): "Title",
        _oor("type"): "xs:string",
    })
    val = ET.SubElement(title_prop, "value")
    val.set("xml:lang", "en-US")
    val.text = "WriterAgent"

    # Empty ImageIdentifier — reserves space for runtime XImageManager icons
    img_prop = ET.SubElement(top_menu, "prop", {
        _oor("name"): "ImageIdentifier",
        _oor("type"): "xs:string",
    })
    ET.SubElement(img_prop, "value")

    # Context: all doc types
    ctx_prop = ET.SubElement(top_menu, "prop", {
        _oor("name"): "Context",
        _oor("type"): "xs:string",
    })
    ET.SubElement(ctx_prop, "value").text = _DEFAULT_CONTEXT

    submenu = ET.SubElement(top_menu, "node", {_oor("name"): "Submenu"})

    counter = [0]
    prev_module = False
    icon_entries = []  # (command_url, module_name, icon_prefix)
    # Module entries (in topo-sort order)
    for m in modules:
        menus = m.get("menus")
        if not menus:
            continue
        mod_name = m["name"]
        if mod_name == "main":
            continue  # framework handled separately below
        actions = m.get("actions", {})

        # Auto-separator between module groups
        if prev_module:
            counter[0] += 1
            _menu_node(submenu, "M%d" % counter[0], url="private:separator")

        _build_menu_entries(submenu, menus, actions, mod_name, counter,
                            icon_entries=icon_entries)
        prev_module = True

    # Framework entries (appended last)
    if framework_manifest:
        fw_menus = framework_manifest.get("menus", [])
        fw_actions = framework_manifest.get("actions", {})
        if fw_menus:
            _build_menu_entries(submenu, fw_menus, fw_actions, "main", counter,
                                icon_entries=icon_entries)

    # Images section — static default icons for menu commands
    if icon_entries:
        images_node = ET.SubElement(addon_ui, "node",
                                    {_oor("name"): "Images"})
        for cmd_url, mod_name, icon_prefix in icon_entries:
            # Unique node name from command URL
            safe_name = cmd_url.replace(":", ".") + ".img"
            img_node = ET.SubElement(images_node, "node", {
                _oor("name"): safe_name,
                _oor("op"): "replace",
            })
            url_prop = ET.SubElement(img_node, "prop", {_oor("name"): "URL"})
            ET.SubElement(url_prop, "value").text = cmd_url
            udi_node = ET.SubElement(img_node, "node",
                                     {_oor("name"): "UserDefinedImages"})
            small_prop = ET.SubElement(udi_node, "prop",
                                       {_oor("name"): "ImageSmallURL"})
            icon_path = "%%origin%%/plugin/modules/%s/icons/%s_16.png" % (
                mod_name, icon_prefix)
            ET.SubElement(small_prop, "value").text = icon_path

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=True)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(body)
        f.write("\n")
    print("  Generated %s" % output_path)



# ── Accelerators.xcu Generation ─────────────────────────────────────


def generate_accelerators_xcu(modules, output_path):
    """Generate Accelerators.xcu from module shortcut declarations.

    Reads ``shortcuts`` from each module manifest. Each shortcut maps an
    action name to a key spec and optional context list.

    Format in module.yaml::

        shortcuts:
          extend_selection:
            key: Q_MOD1
            context: [writer, calc]
    """
    root = ET.Element(_oor("component-data"), {
        _oor("name"): "Accelerators",
        _oor("package"): "org.openoffice.Office",
    })
    root.set("xmlns:xs", _XS_NS)
    root.set("xmlns:install", "http://openoffice.org/2004/installation")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    primary_keys = ET.SubElement(root, "node",
                                 {_oor("name"): "PrimaryKeys"})

    # Collect shortcuts per context
    # context_shortcuts: { lo_service_name: [(key, command_url)] }
    context_shortcuts = {}

    for m in modules:
        shortcuts = m.get("shortcuts")
        if not shortcuts:
            continue
        mod_name = m["name"]

        for action_name, shortcut_def in shortcuts.items():
            key = shortcut_def.get("key")
            if not key:
                continue
            url = "%s:%s.%s" % (_PROTOCOL, mod_name, action_name)
            contexts = shortcut_def.get("context", [])
            if not contexts:
                # All contexts
                for svc in _CONTEXT_MAP.values():
                    context_shortcuts.setdefault(svc, []).append((key, url))
            else:
                for ctx_name in contexts:
                    svc = _CONTEXT_MAP.get(ctx_name, ctx_name)
                    context_shortcuts.setdefault(svc, []).append((key, url))

    # Build XML
    for lo_svc, shortcuts in sorted(context_shortcuts.items()):
        modules_node = ET.SubElement(primary_keys, "node",
                                     {_oor("name"): "Modules"})
        svc_node = ET.SubElement(modules_node, "node",
                                 {_oor("name"): lo_svc})
        for key, url in shortcuts:
            key_node = ET.SubElement(svc_node, "node", {
                _oor("name"): key,
                _oor("op"): "replace",
            })
            cmd_prop = ET.SubElement(key_node, "prop",
                                     {_oor("name"): "Command"})
            cmd_val = ET.SubElement(cmd_prop, "value")
            cmd_val.set("xml:lang", "en-US")
            cmd_val.text = url

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=True)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(body)
        f.write("\n")
    print("  Generated %s" % output_path)




def generate_settings_dialog_tabs(modules, tpl_path, output_path):
    """Auto-generate tabs and pages for SettingsDialog.xdl using a template."""
    if not os.path.exists(tpl_path):
        return

    with open(tpl_path, "r", encoding="utf-8") as f:
        content = f.read()

    tab_marker = "<!-- AUTO_GENERATED_TABS -->"
    page_marker = "<!-- AUTO_GENERATED_PAGES -->"

    if tab_marker not in content or page_marker not in content:
        return

    # Calculate targets for inline configs
    inline_targets = {}
    for m in modules:
        inline_val = m.get("config_inline")
        if not inline_val:
            continue
        target = inline_val if isinstance(inline_val, str) else (m["name"].rsplit(".", 1)[0] if "." in m["name"] else None)
        if target:
            inline_targets[m["name"]] = target

    inline_set = set()
    for name, target in inline_targets.items():
        if target not in inline_targets:
            inline_set.add(name)

    by_name = {m["name"]: m for m in modules}
    inline_map = {}
    for name in inline_set:
        target = inline_targets[name]
        inline_map.setdefault(target, []).append((by_name[name], by_name[name].get("config", {})))

    tabs = []
    pages = []
    
    # Start after the 'Image' tab
    tab_x = 131
    page_num = 3

    for m in modules:
        name = m["name"]
        if name == "ai" or name == "main" or name in inline_set:
            continue

        config = m.get("config", {})
        children = inline_map.get(name)

        # Skip modules with no config properties and no inline children
        if not config and not children:
            continue

        # Create tab button
        tab_label = name.title()
        width = min(len(tab_label) * 5 + 15, 60)
        tabs.append(f'  <dlg:button dlg:id="btn_tab_{name.replace(".", "_")}" dlg:left="{tab_x}" dlg:top="5" dlg:width="{width}" dlg:height="14" dlg:value="{tab_label}"/>')
        tab_x += width + 3

        # Create dummy board for fields
        board = ET.Element(_dlg("bulletinboard"))
        y = 26

        def add_fields(prefix, cfg, curr_y):
            for field_name, schema in cfg.items():
                if schema.get("internal") or schema.get("widget") == "list_detail":
                    continue
                
                widget = schema.get("widget", "text")
                ctrl_id = f"{prefix}__{field_name}" if prefix else field_name
                label_text = schema.get("label", field_name.replace("_", " ").title() + ":")
                
                # Support custom positioning
                field_x = str(schema.get("x", 110))
                field_w = str(schema.get("width", 144))
                
                # We render our own UI to avoid the standard XDL layout gap padding, 
                # because SettingsDialog uses slightly tighter spacing
                if not schema.get("inline_no_label"):
                    ET.SubElement(board, _dlg("text"), {
                        _dlg("id"): f"label_{ctrl_id}",
                        _dlg("tab-index"): "0",
                        _dlg("left"): "8",
                        _dlg("top"): str(curr_y + 2),
                        _dlg("width"): "100",
                        _dlg("height"): "10",
                        _dlg("value"): label_text,
                        _dlg("align"): "left",
                    })
                
                field_attrs = {
                    _dlg("id"): ctrl_id,
                    _dlg("tab-index"): "0",
                    _dlg("left"): field_x,
                    _dlg("top"): str(curr_y),
                    _dlg("width"): field_w,
                    _dlg("height"): "14",
                }
                
                if widget == "checkbox":
                    field_attrs.update({
                        _dlg("width"): field_w if "width" in schema else "120",
                        _dlg("height"): "10",
                        _dlg("value"): label_text,
                        _dlg("checked"): "false",
                    })
                    # Remove the duplicate label for checkbox
                    if not schema.get("inline_no_label") and board[-1].get(_dlg("id")) == f"label_{ctrl_id}":
                        board.remove(board[-1])
                    field_attrs[_dlg("top")] = str(curr_y + 2)
                    field_attrs[_dlg("left")] = field_x if "x" in schema else "8"
                    ET.SubElement(board, _dlg("checkbox"), field_attrs)
                elif widget == "password":
                    field_attrs[_dlg("echochar")] = "42"
                    ET.SubElement(board, _dlg("textfield"), field_attrs)
                elif widget in ("number", "slider"):
                    field_attrs.update({_dlg("spin"): "true", _dlg("width"): field_w if "width" in schema else "60"})
                    ET.SubElement(board, _dlg("numericfield"), field_attrs)
                elif widget == "select" or widget == "combo":
                    field_attrs.update({_dlg("dropdown"): "true", _dlg("spin"): "true", _dlg("border"): "1"})
                    el = ET.SubElement(board, _dlg("combobox"), field_attrs)
                    menu = ET.SubElement(el, _dlg("menupopup"))
                    for opt in schema.get("options", []):
                        ET.SubElement(menu, _dlg("menuitem"), {_dlg("value"): str(opt)})
                elif widget == "button":
                    field_attrs.update({
                        _dlg("width"): field_w if "width" in schema else "100",
                        _dlg("height"): "14",
                        _dlg("value"): schema.get("label", "Click"),
                    })
                    # Use a standard button instead of label + textbox
                    if not schema.get("inline_no_label") and board[-1].get(_dlg("id")) == f"label_{ctrl_id}":
                        board.remove(board[-1])
                    field_attrs[_dlg("left")] = field_x if "x" in schema else "8"
                    ET.SubElement(board, _dlg("button"), field_attrs)
                else:
                    ET.SubElement(board, _dlg("textfield"), field_attrs)
                
                if not schema.get("inline"):
                    curr_y += 16
            return curr_y
            
        y = add_fields(name.replace(".", "_"), config, y)
        if children:
            for child_m, child_cfg in children:
                # Skip children with no visible config fields
                visible_child_fields = [
                    (fn, s) for fn, s in child_cfg.items()
                    if not s.get("internal") and s.get("widget") != "list_detail"
                ]
                if not visible_child_fields:
                    continue

                # Add gap and separator line with label
                y += 2
                sep_label = child_m.get("title", _pretty_name(child_m["name"]))
                ET.SubElement(board, _dlg("fixedline"), {
                    _dlg("id"): f"sep_{child_m['name'].replace('.', '_')}",
                    _dlg("left"): "8",
                    _dlg("top"): str(y),
                    _dlg("width"): "424",
                    _dlg("height"): "8",
                    _dlg("value"): sep_label,
                })
                y += 10
                y = add_fields(child_m["name"].replace(".", "_"), child_cfg, y)
        
        # Add dlg:page to all elements in this page
        for el in board.iter():
            if el != board and "menupopup" not in el.tag and "menuitem" not in el.tag:
                el.set(_dlg("page"), str(page_num))

        page_str = ""
        for child in board:
            ET.indent(child, space="  ")
            child_str = ET.tostring(child, encoding="unicode")
            # Remove namespace prefixes from output for clean merge
            child_str = child_str.replace(f"xmlns:ns0=\"{_DLG_NS}\" ", "")
            child_str = child_str.replace("ns0:", "dlg:")
            page_str += "  " + child_str + "\n"
            
        pages.append(f"  <!-- === Page {page_num}: {name.title()} Settings === -->\n{page_str}")
        page_num += 1

    import re
    # Replace markers
    # For tabs, we keep everything before tab_marker, and everything starting with "<!-- === Page 1:"
    # For pages, we keep everything up to page_marker, and everything starting with "\n  <!-- OK Button" or "</dlg:bulletinboard>"
    
    before_tabs, rest1 = content.split(tab_marker, 1)
    idx_p1 = rest1.find("<!-- === Page 1:")
    after_tabs = rest1[idx_p1:] if idx_p1 != -1 else rest1
    
    # We now split the remaining text with page_marker
    if page_marker in after_tabs:
        middle, rest2 = after_tabs.split(page_marker, 1)
        # Find the OK button or </dlg:bulletinboard> to mark the end of generated pages
        idx_ok = rest2.find("<!-- OK Button")
        if idx_ok == -1:
            idx_ok = rest2.find("</dlg:bulletinboard>")
        rest2 = rest2[idx_ok:] if idx_ok != -1 else rest2
    else:
        middle = after_tabs
        rest2 = ""

    new_content = before_tabs + tab_marker + "\n" + "\n".join(tabs) + "\n\n  " + middle + page_marker + "\n" + "\n".join(pages) + "\n\n  " + rest2

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"  Injected {len(tabs)} tabs into {os.path.basename(output_path)}")


def generate_manifest_xml(modules, output_path):
    """Generate META-INF/manifest.xml with XCS/XCU entries for selected modules."""
    MANIFEST_NS = "http://openoffice.org/2001/manifest"
    MF = "manifest:"

    # Static entries (always present)
    entries = [
        ('application/vnd.sun.star.uno-typelibrary;type=RDB', 'XPromptFunction.rdb'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/main.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/prompt_function.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/options_handler.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/modules/chatbot/panel_factory.py'),
        ('application/vnd.sun.star.configuration-data', 'Addons.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Accelerators.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Jobs.xcu'),
        ('application/vnd.sun.star.configuration-data', 'ProtocolHandler.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/UI/Sidebar.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/UI/Factories.xcu'),
    ]

    # Build XML tree
    def _mf(tag):
        return "{%s}%s" % (MANIFEST_NS, tag)

    ET.register_namespace("manifest", MANIFEST_NS)
    root = ET.Element(_mf("manifest"))
    for media_type, full_path in entries:
        ET.SubElement(root, _mf("file-entry"), {
            _mf("media-type"): media_type,
            _mf("full-path"): full_path,
        })

    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="unicode")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write("<!-- GENERATED FILE — do not edit manually. -->\n")
        f.write("<!-- Re-generated by: scripts/generate_manifest.py -->\n")
        f.write(body)
        f.write("\n")
    print("  Generated %s (%d entries)" % (output_path, len(entries)))


def patch_description_xml(extension_dir):
    """Generate description.xml from .tpl with version from plugin/version.py."""
    from plugin.version import EXTENSION_VERSION

    tpl_path = os.path.join(extension_dir, "description.xml.tpl")
    desc_path = os.path.join(extension_dir, "description.xml")

    if not os.path.exists(tpl_path):
        print("  WARNING: description.xml.tpl not found, skipping")
        return

    with open(tpl_path) as f:
        content = f.read()

    content = content.replace("{{VERSION}}", EXTENSION_VERSION)

    with open(desc_path, "w") as f:
        f.write(content)
    print("  Generated description.xml with version %s" % EXTENSION_VERSION)


def main():
    parser = argparse.ArgumentParser(
        description="Generate _manifest.py and XCS/XCU from module.yaml files")
    parser.add_argument(
        "--modules", nargs="*", default=None,
        help="Only process these modules (default: all)")
    args = parser.parse_args()

    modules_dir = os.path.join(PROJECT_ROOT, "plugin", "modules")
    if not os.path.isdir(modules_dir):
        print("ERROR: plugin/modules/ not found at %s" % modules_dir,
              file=sys.stderr)
        return 1

    # Load framework-level plugin.yaml (if present)
    plugin_yaml_path = os.path.join(PROJECT_ROOT, "plugin", "plugin.yaml")
    framework_manifest = None
    if os.path.isfile(plugin_yaml_path):
        with open(plugin_yaml_path) as f:
            framework_manifest = yaml.safe_load(f)
        framework_manifest.setdefault("name", "main")
        print("  Loaded framework config: plugin/plugin.yaml")

    print("Scanning modules in %s and framework..." % modules_dir)
    manifests = find_modules(modules_dir, args.modules)
    framework_dir = os.path.join(PROJECT_ROOT, "plugin", "framework")
    if os.path.isdir(framework_dir):
        manifests.extend(find_modules(framework_dir, args.modules))
    
    if not manifests:
        print("  No modules found!")
        return 1

    sorted_modules = topo_sort(manifests)

    # Prepend framework manifest (always first, before all modules)
    if framework_manifest:
        sorted_modules.insert(0, framework_manifest)
    names = [m["name"] for m in sorted_modules]
    print("  Module order: %s" % " -> ".join(names))

    build_dir = os.path.join(PROJECT_ROOT, "build", "generated")

    # Read Tools -> Options enable flag
    enable_options = os.environ.get("LOCALWRITER_ENABLE_OPTIONS", "1") == "1"
    if not enable_options:
        print("  LOCALWRITER_ENABLE_OPTIONS is false. Skipping Tools -> Options generation.")

    # 1. Addons.xcu (menus) — run first to collect conditional menus
    addons_xcu_path = os.path.join(build_dir, "Addons.xcu")
    generate_addons_xcu(
        sorted_modules, framework_manifest, addons_xcu_path)

    # 2. _manifest.py
    manifest_path = os.path.join(PROJECT_ROOT, "plugin", "_manifest.py")
    generate_manifest_py(sorted_modules, manifest_path)

    # 4. XDL dialog pages
    dialogs_dir = os.path.join(build_dir, "dialogs")
    generate_xdl_files(sorted_modules, dialogs_dir)

    # 5. Accelerators.xcu (shortcuts)
    accel_xcu_path = os.path.join(build_dir, "Accelerators.xcu")
    generate_accelerators_xcu(sorted_modules, accel_xcu_path)

    # 6. META-INF/manifest.xml
    manifest_xml_path = os.path.join(PROJECT_ROOT, "extension", "META-INF", "manifest.xml")
    generate_manifest_xml(sorted_modules, manifest_xml_path)

    # 7. SettingsDialog Tabs
    generate_settings_dialog_tabs(
        sorted_modules,
        os.path.join(PROJECT_ROOT, "extension", "WriterAgentDialogs", "SettingsDialog.xdl.tpl"),
        os.path.join(PROJECT_ROOT, "build", "generated", "WriterAgentDialogs", "SettingsDialog.xdl")
    )

    # 8. Patch version
    patch_description_xml(os.path.join(PROJECT_ROOT, "extension"))

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
