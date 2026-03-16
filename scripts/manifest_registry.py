"""Registry/XCU generation: Addons.xcu, Accelerators.xcu, SettingsDialog tabs, manifest.xml, description.xml."""

import os
import re
import xml.etree.ElementTree as ET

from manifest_xdl import _dlg, _oor, _pretty_name, _DLG_NS, _XS_NS



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
        if name in ("ai", "main", "core") or name in inline_set:
            continue
        if name in ("tunnel", "launcher"):
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
                        # Support dict options: use label for display, value for stored
                        if isinstance(opt, dict):
                            menu_val = opt.get("label", opt.get("value", str(opt)))
                        else:
                            menu_val = str(opt)
                        ET.SubElement(menu, _dlg("menuitem"), {_dlg("value"): menu_val})
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
        ('application/vnd.sun.star.uno-component;type=Python', 'plugin/modules/chatbot/panel_factory.py'),
        ('application/vnd.sun.star.configuration-data', 'Addons.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Accelerators.xcu'),
        ('application/vnd.sun.star.configuration-data', 'Jobs.xcu'),
        ('application/vnd.sun.star.configuration-data', 'ProtocolHandler.xcu'),
        ('application/vnd.sun.star.configuration-data', 'registry/org/openoffice/Office/CalcAddIns.xcu'),
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

