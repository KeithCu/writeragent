"""Registry generation: SettingsDialog tabs, manifest.xml, description.xml, update feeds.

Menus and accelerators are hand-written in extension/ and extension-core/ (Addons.xcu, Accelerators.xcu).
"""

import os
import xml.etree.ElementTree as ET

from manifest_xdl import _dlg, _pretty_name, _DLG_NS
from scripts.manifest_common import (
    add_label as _common_add_label,
    add_checkbox as _common_add_checkbox,
    add_textfield as _common_add_textfield,
    add_numericfield as _common_add_numericfield,
    add_combobox as _common_add_combobox,
    add_button as _common_add_button,
    require_widget,
    write_if_changed as _write_if_changed,
)

_SETTINGS_ALLOWED_WIDGETS = frozenset({
    "text",
    "checkbox",
    "password",
    "textarea",
    "number",
    "slider",
    "select",
    "combo",
    "button",
    "separator",
})

# SettingsDialog page separators (widget:separator and config_inline children).
_SETTINGS_SEP_LEFT = 8
_SETTINGS_SEP_WIDTH = 424
_SETTINGS_SEP_HEIGHT = 8
_SETTINGS_SEP_GAP_BEFORE = 2
_SETTINGS_SEP_GAP_AFTER = 10


def _settings_add_separator(board, sep_id, y, label=None):
    """Add a Settings-page fixedline separator. Returns y after the trailing gap.

    Same geometry for labeled (ACP-style) and unlabeled (widget:separator) rules.
    """
    y += _SETTINGS_SEP_GAP_BEFORE
    attrs = {
        _dlg("id"): sep_id,
        _dlg("left"): str(_SETTINGS_SEP_LEFT),
        _dlg("top"): str(y),
        _dlg("width"): str(_SETTINGS_SEP_WIDTH),
        _dlg("height"): str(_SETTINGS_SEP_HEIGHT),
    }
    if label:
        attrs[_dlg("value")] = label
    ET.SubElement(board, _dlg("fixedline"), attrs)
    return y + _SETTINGS_SEP_GAP_AFTER


def _settings_hover_text(schema):
    """Hover tip (``dlg:help-text``) for one Settings field.

    An explicit ``tooltip`` string wins. Otherwise the YAML ``helper`` is the
    tip — the same string ``tooltip: true`` used to opt into. Empty when the
    field has neither, so controls without a helper stay quiet.
    """
    tooltip = schema.get("tooltip")
    if isinstance(tooltip, str) and tooltip.strip():
        return tooltip.strip()
    helper = schema.get("helper")
    if isinstance(helper, str) and helper.strip():
        return helper.strip()
    return ""


def generate_settings_dialog_tabs(modules, tpl_path, output_path, *, librepy_flavor=False):
    """Auto-generate tabs and pages for SettingsDialog.xdl using a template."""
    if not os.path.exists(tpl_path):
        raise FileNotFoundError(f"Template not found: {tpl_path}")

    with open(tpl_path, "r", encoding="utf-8") as f:
        content = f.read()

    tab_start = "<!-- AUTO_GENERATED_TABS -->"
    tab_end = "<!-- END AUTO_GENERATED_TABS -->"
    page_start = "<!-- AUTO_GENERATED_PAGES -->"
    page_end = "<!-- END AUTO_GENERATED_PAGES -->"

    for marker in (tab_start, tab_end, page_start, page_end):
        if marker not in content:
            raise ValueError(f"Required marker {marker!r} missing from {tpl_path}")

    from plugin.chatbot.settings_tab_order import _build_inline_maps, iter_settings_tab_modules

    inline_map = _build_inline_maps(modules)[1]

    tabs = []
    pages = []
    
    # LibrePy ships only module tabs (General/Image are hidden at runtime).
    tab_x = 5 if librepy_flavor else 131
    page_num = 3

    for m in iter_settings_tab_modules(modules):
        name = m["name"]
        config = m.get("config", {})
        children = inline_map.get(name)

        # Create tab button
        tab_label = m.get("title", name.title())
        width = min(len(tab_label) * 5 + 15, 60)
        tabs.append(f'  <dlg:button dlg:id="btn_tab_{name.replace(".", "_")}" dlg:left="{tab_x}" dlg:top="5" dlg:width="{width}" dlg:height="14" dlg:value="{tab_label}"/>')
        tab_x += width + 3

        # Create dummy board for fields
        board = ET.Element(_dlg("bulletinboard"))
        y = 26

        def add_fields(prefix, cfg, curr_y):
            row_height = 0
            for field_name, schema in cfg.items():
                if schema.get("internal"):
                    continue
                if librepy_flavor and schema.get("librepy_exclude"):
                    continue
                where = f"{prefix}.{field_name}" if prefix else field_name
                widget = require_widget(schema, _SETTINGS_ALLOWED_WIDGETS, where=where)
                if widget == "separator":
                    row_height = 0
                    sep_id = f"sep_{prefix}_{field_name}" if prefix else f"sep_{field_name}"
                    curr_y = _settings_add_separator(
                        board, sep_id, curr_y, label=schema.get("label"),
                    )
                    continue

                ctrl_id = f"{prefix}__{field_name}" if prefix else field_name
                label_text = schema.get("label", field_name.replace("_", " ").title() + ":")
                
                # Support custom positioning
                field_x = str(schema.get("x", 110))
                field_w = str(schema.get("width", 144))
                
                # Track max control height in current row
                ctrl_h = int(schema.get("height", 14))
                if ctrl_h > row_height:
                    row_height = ctrl_h

                # We render our own UI to avoid the standard XDL layout gap padding,
                # because SettingsDialog uses slightly tighter spacing
                label_x = str(schema.get("label_x", 8))
                label_w = str(schema.get("label_width", 100))
                label_el = None
                if not schema.get("inline_no_label"):
                    if schema.get("label_above"):
                        label_el = _common_add_label(board, f"label_{ctrl_id}", label_text, label_x, curr_y, label_w, 10, align="left")
                        curr_y += 12
                    else:
                        label_el = _common_add_label(board, f"label_{ctrl_id}", label_text, label_x, curr_y + 2, label_w, 10, align="left")

                ctrl_el = None
                if widget == "checkbox":
                    # Checkbox text is the control itself; a second label would repeat it.
                    if label_el is not None and board[-1].get(_dlg("id")) == f"label_{ctrl_id}":
                        board.remove(label_el)
                        label_el = None
                    cb_left = field_x if "x" in schema else "8"
                    cb_width = field_w if "width" in schema else "120"
                    checked = "true" if schema.get("default") else "false"
                    ctrl_el = _common_add_checkbox(
                        board, ctrl_id, label_text, cb_left, curr_y + 2, cb_width, 10, checked=checked,
                    )
                elif widget == "password":
                    ctrl_el = _common_add_textfield(board, ctrl_id, field_x, curr_y, field_w, 14, echo_char=42)
                elif widget == "textarea":
                    field_h = str(schema.get("height", 36))
                    ctrl_el = _common_add_textfield(board, ctrl_id, field_x, curr_y, field_w, field_h, multiline=True)
                    if schema.get("readonly"):
                        ctrl_el.set(_dlg("readonly"), "true")
                elif widget in ("number", "slider"):
                    num_w = field_w if "width" in schema else "60"
                    # Pass schema bounds so spin stays within range; strict save rejects out-of-range values after execute() returns.
                    ctrl_el = _common_add_numericfield(
                        board, ctrl_id, field_x, curr_y, num_w, 14,
                        spin="true",
                        min_val=schema.get("min"),
                        max_val=schema.get("max"),
                        step_val=schema.get("step"),
                        decimal_accuracy="1" if schema.get("type") == "float" else "0",
                    )
                elif widget in ("select", "combo"):
                    ctrl_el = _common_add_combobox(
                        board, ctrl_id, field_x, curr_y, field_w, 14,
                        options=schema.get("options", []),
                        dropdown="true",
                        spin="true",
                        border="1"
                    )
                elif widget == "button":
                    # Use a standard button instead of label + textbox
                    if (
                        label_el is not None
                        and not schema.get("show_button_label")
                        and board[-1].get(_dlg("id")) == f"label_{ctrl_id}"
                    ):
                        board.remove(label_el)
                        label_el = None
                    btn_left = field_x if "x" in schema else "8"
                    btn_width = field_w if "width" in schema else "100"
                    btn_height = str(schema.get("height", 14))
                    btn_label = schema.get("button_text") or schema.get("label", "Click")
                    ctrl_el = _common_add_button(board, ctrl_id, btn_label, btn_left, curr_y, btn_width, btn_height)
                else:
                    ctrl_el = _common_add_textfield(board, ctrl_id, field_x, curr_y, field_w, 14)

                # Helper text is a hover tip, not a second FixedText row. Standalone
                # module XDLs still use always-visible hlp_* lines and do not come
                # through this generator. The label gets the same tip so hovering
                # the caption works; checkboxes already dropped that label.
                hover = _settings_hover_text(schema)
                if hover:
                    if ctrl_el is not None:
                        ctrl_el.set(_dlg("help-text"), hover)
                    if label_el is not None:
                        label_el.set(_dlg("help-text"), hover)
                
                if not schema.get("inline"):
                    curr_y += (row_height + 2) if row_height > 14 else 16
                    row_height = 0
            return curr_y
            
        y = add_fields(name.replace(".", "_"), config, y)
        if children:
            for child_m, child_cfg in children:
                # Skip children with no visible config fields
                visible_child_fields = [
                    (fn, s) for fn, s in child_cfg.items()
                    if not s.get("internal")
                    and not (librepy_flavor and s.get("librepy_exclude"))
                ]
                if not visible_child_fields:
                    continue

                # Labeled separator before each config_inline child (e.g. ACP).
                sep_label = child_m.get("title", _pretty_name(child_m["name"]))
                y = _settings_add_separator(
                    board,
                    f"sep_{child_m['name'].replace('.', '_')}",
                    y,
                    label=sep_label,
                )
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

    # Splicing between begin/end marker pairs
    before_tabs, rest_after_tab_start = content.split(tab_start, 1)
    _old_tabs, after_tab_end = rest_after_tab_start.split(tab_end, 1)

    if page_start not in after_tab_end or page_end not in after_tab_end:
        raise ValueError(f"Page markers missing after tab markers in {tpl_path}")

    between_sections, rest_after_page_start = after_tab_end.split(page_start, 1)
    _old_pages, after_page_end = rest_after_page_start.split(page_end, 1)

    tabs_str = ("\n" + "\n".join(tabs) + "\n  ") if tabs else "\n  "
    pages_str = ("\n" + "\n".join(pages) + "\n\n  ") if pages else "\n  "

    new_content = (
        before_tabs
        + tab_start
        + tabs_str
        + tab_end
        + between_sections
        + page_start
        + pages_str
        + page_end
        + after_page_end
    )

    # Validate that generated XML is well-formed before writing
    ET.fromstring(new_content)

    _write_if_changed(output_path, new_content)
    print(f"  Injected {len(tabs)} tabs into {os.path.basename(output_path)}")


def generate_manifest_xml(modules, output_path):
    """Generate META-INF/manifest.xml with XCS/XCU entries for selected modules."""
    MANIFEST_NS = "http://openoffice.org/2001/manifest"

    # Static entries (always present)
    entries = [
        ('application/vnd.sun.star.uno-typelibrary;type=RDB', 'XPythonFunction.rdb'),
        ('application/vnd.sun.star.uno-typelibrary;type=RDB', 'XPromptFunction.rdb'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/main.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/calc/python/addin.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/calc/prompt_addin.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/chatbot/panel_factory.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/librepy/panel_factory.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/writer/locale/ai_grammar_proofreader.py'),
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/notebook/import_filter.py'),
        (
            'application/vnd.sun.star.configuration-data',
            'registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu',
        ),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/TypeDetection/Types.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/TypeDetection/Filters.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/TypeDetection/Misc.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Addons.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Accelerators.xcu'),
        ('application/vnd.sun.star.configuration-data', 'ProtocolHandler.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Jobs.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/CalcAddIns.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/UI/Sidebar.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/UI/Factories.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/UI/WriterWindowState.xcu'),
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

    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<!-- GENERATED FILE — do not edit manually. -->\n<!-- Re-generated by: scripts/generate_manifest.py -->\n' + body + '\n'
    _write_if_changed(output_path, xml_content)
    print("  Generated %s (%d entries)" % (output_path, len(entries)))


def patch_description_xml(extension_dir):
    """Generate description.xml from .tpl with version from plugin/version.py."""
    from plugin.version import EXTENSION_VERSION

    tpl_path = os.path.join(extension_dir, "description.xml.tpl")
    desc_path = os.path.join(extension_dir, "description.xml")

    if not os.path.exists(tpl_path):
        raise FileNotFoundError(f"description.xml.tpl not found in {extension_dir}")

    with open(tpl_path) as f:
        content = f.read()

    content = content.replace("{{VERSION}}", EXTENSION_VERSION)

    _write_if_changed(desc_path, content)
    print("  Generated description.xml with version %s" % EXTENSION_VERSION)


def generate_update_xml(root_dir, feed="update.xml"):
    """Generate WriterAgent / LibrePy / LibreHarper update.xml feeds from .tpl files."""
    from plugin.version import EXTENSION_VERSION

    feed_map = {
        "update.xml": ("update.xml.tpl", "update.xml"),
        "writeragent": ("update.xml.tpl", "update.xml"),
        "update-librepy.xml": ("update-librepy.xml.tpl", "update-librepy.xml"),
        "librepy": ("update-librepy.xml.tpl", "update-librepy.xml"),
        "update-libreharper.xml": ("update-libreharper.xml.tpl", "update-libreharper.xml"),
        "libreharper": ("update-libreharper.xml.tpl", "update-libreharper.xml"),
    }
    if feed == "all":
        feeds = [
            ("update.xml.tpl", "update.xml"),
            ("update-librepy.xml.tpl", "update-librepy.xml"),
            ("update-libreharper.xml.tpl", "update-libreharper.xml"),
        ]
    elif isinstance(feed, (list, tuple)):
        feeds = [feed_map[f] for f in feed if f in feed_map]
    else:
        feeds = [feed_map.get(feed, (f"{feed}.tpl", feed))]

    for tpl_name, out_name in feeds:
        tpl_path = os.path.join(root_dir, "plugin", tpl_name)
        update_path = os.path.join(root_dir, out_name)
        if not os.path.exists(tpl_path):
            # Optional: feed might not be present in every project setup
            continue
        with open(tpl_path) as f:
            content = f.read()
        content = content.replace("{{VERSION}}", EXTENSION_VERSION)
        _write_if_changed(update_path, content)
        print("  Generated %s with version %s" % (out_name, EXTENSION_VERSION))
