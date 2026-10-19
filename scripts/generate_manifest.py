#!/usr/bin/env python3
"""Generate _manifest.py, Settings/Vision XDL, dialog.xlb, and description.xml from module manifests.

Menus and accelerators stay hand-written in extension/ and extension-core/.

Usage:
    python3 scripts/generate_manifest.py
    python3 scripts/generate_manifest.py --modules scripting vision
"""

import argparse
import json
import os
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


def find_modules(modules_dir):
    """Find all module.yaml files recursively and return parsed manifests.

    Module name comes from the ``name`` field in module.yaml.
    Directory convention: dots map to underscores (tunnel.bore -> tunnel_bore/).
    Falls back to directory-derived name if ``name`` is absent.
    """
    manifests = []
    for dirpath, dirnames, filenames in os.walk(modules_dir):
        dirnames[:] = [d for d in dirnames if d not in ("lib", "__pycache__") and not d.startswith(".")]
        if "module.yaml" not in filenames:
            continue
        rel = os.path.relpath(dirpath, modules_dir)
        module_name = rel.replace(os.sep, ".")

        yaml_path = os.path.join(dirpath, "module.yaml")
        with open(yaml_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
        manifest["_dir_name"] = module_name
        manifest["_yaml_path"] = yaml_path
        manifest.setdefault("name", module_name)
        manifests.append(manifest)

    manifests.sort(key=lambda m: m["name"])
    return manifests


def filter_modules(manifests, filter_names):
    """Filter modules by name, directory name, or top-level segment, plus inline children."""
    if not filter_names:
        return list(manifests)
    filter_set = set(filter_names)
    kept = []
    kept_names = set()

    for m in manifests:
        name = m.get("name", "")
        dir_name = m.get("_dir_name", "")
        name_top = name.split(".")[0] if name else ""
        dir_top = dir_name.split(".")[0] if dir_name else ""
        if (
            name in filter_set
            or dir_name in filter_set
            or name_top in filter_set
            or dir_top in filter_set
        ):
            kept.append(m)
            kept_names.add(name)

    # Also keep modules whose config_inline targets an already kept module
    for m in manifests:
        if m in kept:
            continue
        inline = m.get("config_inline")
        if isinstance(inline, str) and inline in kept_names:
            kept.append(m)
            kept_names.add(m.get("name", ""))

    return kept


def validate_manifests(manifests):
    """Validate parsed module manifests against structural invariants.

    Returns a list of error strings formatted as 'path: field: message'.
    """
    errors = []

    # 1. Duplicate name
    names_seen = {}
    for m in manifests:
        name = m.get("name")
        path = m.get("_yaml_path", name or "unknown")
        if not name:
            errors.append(f"{path}: name: module name is required")
            continue
        if name in names_seen:
            errors.append(f"{path}: name: duplicate module name '{name}'")
        else:
            names_seen[name] = path

    # 2. Duplicate provides_services
    services_seen = {}
    for m in manifests:
        path = m.get("_yaml_path", m.get("name", "unknown"))
        for svc in m.get("provides_services", []):
            if svc in services_seen:
                errors.append(f"{path}: provides_services: duplicate service '{svc}'")
            else:
                services_seen[svc] = path

    # 3. Requires entry that is neither a module name nor a provides_services in full tree
    all_module_names = set(names_seen.keys())
    all_services = set(services_seen.keys())
    known_targets = all_module_names | all_services

    for m in manifests:
        path = m.get("_yaml_path", m.get("name", "unknown"))
        for req in m.get("requires", []):
            if req not in known_targets:
                errors.append(f"{path}: requires: unknown requirement '{req}'")

    # 4. Dependency cycle, using a visiting set and iterating sorted(by_name)
    by_name = {m["name"]: m for m in manifests if m.get("name")}
    provides = {}
    for m in manifests:
        for svc in m.get("provides_services", []):
            provides[svc] = m.get("name")

    visited = set()
    visiting = set()

    def check_cycle(name, stack):
        if name in visiting:
            cycle = " -> ".join(stack + [name])
            return f"dependency cycle: {cycle}"
        if name in visited:
            return None
        visiting.add(name)
        m = by_name.get(name)
        if m:
            for req in m.get("requires", []):
                target = provides.get(req, req)
                if target in by_name:
                    err = check_cycle(target, stack + [name])
                    if err:
                        return err
        visiting.discard(name)
        visited.add(name)
        return None

    for name in sorted(by_name):
        if name not in visited:
            err = check_cycle(name, [])
            if err:
                m = by_name[name]
                path = m.get("_yaml_path", name)
                errors.append(f"{path}: requires: {err}")

    # 5. config_inline string whose target module is missing; config_inline: true whose name has no dot
    for m in manifests:
        path = m.get("_yaml_path", m.get("name", "unknown"))
        inline = m.get("config_inline")
        if inline is not None:
            if isinstance(inline, str):
                if inline not in by_name:
                    errors.append(f"{path}: config_inline: target module '{inline}' not found")
            elif inline is True:
                if "." not in m.get("name", ""):
                    errors.append(f"{path}: config_inline: module name '{m.get('name')}' has no dot for parent inlining")

    # 6. settings_tab present and not a bool
    for m in manifests:
        path = m.get("_yaml_path", m.get("name", "unknown"))
        if "settings_tab" in m and not isinstance(m["settings_tab"], bool):
            errors.append(f"{path}: settings_tab: must be a boolean")

    # 7-10. Config validations
    valid_widgets = {
        "text", "checkbox", "password", "textarea", "number",
        "slider", "select", "combo", "button", "separator"
    }

    for m in manifests:
        path = m.get("_yaml_path", m.get("name", "unknown"))
        if "config" not in m:
            continue
        cfg = m["config"]
        if not isinstance(cfg, dict):
            errors.append(f"{path}: config: must be a mapping")
            continue
        for fname, schema in cfg.items():
            if not isinstance(schema, dict):
                errors.append(f"{path}: config.{fname}: must be a mapping")
                continue
            widget = schema.get("widget", "text")
            if widget not in valid_widgets:
                errors.append(f"{path}: config.{fname}.widget: invalid widget '{widget}'")
            if widget == "select":
                if not schema.get("options") and not schema.get("options_provider"):
                    errors.append(f"{path}: config.{fname}: select widget requires options or options_provider")
            for int_field in ("x", "width", "height", "label_x", "label_width"):
                if int_field in schema:
                    val = schema[int_field]
                    if not isinstance(val, int) or isinstance(val, bool):
                        errors.append(f"{path}: config.{fname}.{int_field}: must be an integer")
            for num_field in ("min", "max", "step"):
                if num_field in schema:
                    val = schema[num_field]
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        errors.append(f"{path}: config.{fname}.{num_field}: must be a number")

    return errors


def topo_sort(modules):
    """Sort modules by dependency order (core first)."""
    by_name = {m["name"]: m for m in modules}
    provides = {}
    for m in modules:
        for svc in m.get("provides_services", []):
            provides[svc] = m["name"]

    visited = set()
    visiting = set()
    order = []

    def visit(name):
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"Cyclic module requires graph at {name!r}")
        visiting.add(name)
        m = by_name.get(name)
        if m is None:
            visiting.discard(name)
            return
        for req in m.get("requires", []):
            provider = provides.get(req, req)
            if provider in by_name:
                visit(provider)
        visiting.discard(name)
        visited.add(name)
        order.append(m)

    if "core" in by_name:
        visit("core")
    for name in sorted(by_name):
        visit(name)

    return order


def _json_to_python(text):
    """Convert JSON literals to Python literals (true->True, false->False, null->None)."""
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
        for jval, pyval in (("true", "True"), ("false", "False"), ("null", "None")):
            if text[i:i+len(jval)] == jval:
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


def _filter_librepy_config(config):
    """Drop WriterAgent-only settings keys from LibrePy manifest/XDL generation."""
    if not isinstance(config, dict):
        return config
    return {
        field_name: schema
        for field_name, schema in config.items()
        if not (isinstance(schema, dict) and schema.get("librepy_exclude"))
    }


def generate_manifest_py(modules, output_path, *, librepy_flavor=False):
    """Generate _manifest.py with module descriptors and pre-computed config defaults."""
    from plugin.version import EXTENSION_VERSION
    from scripts.manifest_common import write_if_changed

    lines = [
        '"""Auto-generated module manifest. DO NOT EDIT."""',
        "",
        "VERSION = %r" % EXTENSION_VERSION,
        "",
        "MODULES = [",
    ]
    config_defaults = {}
    config_schemas = {}
    dotted_fallbacks = {}

    for m in modules:
        config = m.get("config", {})
        if librepy_flavor:
            config = _filter_librepy_config(config)
        mod_name = m.get("name", "")
        if isinstance(config, dict) and mod_name:
            for fname, schema in config.items():
                if isinstance(schema, dict):
                    full_key = "%s.%s" % (mod_name, fname)
                    if "default" in schema:
                        config_defaults[full_key] = schema["default"]
                        if fname not in config_defaults:
                            config_defaults[fname] = schema["default"]
                    config_schemas[full_key] = schema
                    if fname not in config_schemas:
                        config_schemas[fname] = schema
                    dotted_fallbacks.setdefault(fname, []).append(full_key)

        # Clean repr — only keep runtime-relevant keys
        entry = {
            "name": m["name"],
            "title": m.get("title", ""),
            "requires": m.get("requires", []),
            "provides_services": m.get("provides_services", []),
            "config": config,
            "config_inline": m.get("config_inline"),
            "actions": list(m.get("actions", {}).keys()),
            "action_icons": {k: v["icon"] for k, v in m.get("actions", {}).items() if v.get("icon")},
        }
        if m.get("settings_tab") is False:
            entry["settings_tab"] = False
        if m.get("config_dialog"):
            entry["config_dialog"] = m["config_dialog"]
        json_text = json.dumps(entry, indent=8, ensure_ascii=False)
        lines.append("    %s," % _json_to_python(json_text))
    lines.append("]")
    lines.append("")
    lines.append("CONFIG_DEFAULTS = %s" % _json_to_python(json.dumps(config_defaults, indent=4, ensure_ascii=False)))
    lines.append("")
    lines.append("CONFIG_SCHEMAS = %s" % _json_to_python(json.dumps(config_schemas, indent=4, ensure_ascii=False)))
    lines.append("")
    lines.append("DOTTED_FALLBACKS = %s" % _json_to_python(json.dumps(dotted_fallbacks, indent=4, ensure_ascii=False)))
    lines.append("")

    write_if_changed(output_path, "\n".join(lines))
    print("  Generated %s (%d modules)" % (output_path, len(modules)))


# Ensure scripts/ is on path for manifest_xdl and manifest_registry
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from manifest_xdl import (
    generate_standalone_config_dialogs,
    remove_stale_generated_xdls,
    update_dialog_xlb,
)
from manifest_registry import (
    generate_settings_dialog_tabs,
    generate_manifest_xml,
    patch_description_xml,
)


def main():
    parser = argparse.ArgumentParser(
        description="Generate _manifest.py, Settings/Vision XDL, dialog.xlb, and description.xml from module manifests")
    parser.add_argument(
        "--modules", nargs="*", default=None,
        help="Only process these modules (default: all)")
    parser.add_argument(
        "--manifest-output", default=None,
        help="Write _manifest.py to this path (default: plugin/_manifest.py)")
    parser.add_argument(
        "--skip-writeragent-extension", action="store_true",
        help="Do not update extension/ description.xml or META-INF")
    args = parser.parse_args()

    modules_dir = os.path.join(PROJECT_ROOT, "plugin")
    if not os.path.isdir(modules_dir):
        print("ERROR: plugin/ not found at %s" % modules_dir,
              file=sys.stderr)
        return 1

    # Load framework-level plugin.yaml (if present)
    plugin_yaml_path = os.path.join(PROJECT_ROOT, "plugin", "plugin.yaml")
    framework_manifest = None
    if os.path.isfile(plugin_yaml_path):
        with open(plugin_yaml_path, encoding="utf-8") as f:
            framework_manifest = yaml.safe_load(f)
        framework_manifest.setdefault("name", "main")
        print("  Loaded framework config: plugin/plugin.yaml")

    print("Scanning modules in %s..." % modules_dir)
    manifests = find_modules(modules_dir)

    if not manifests:
        print("  No modules found!")
        return 1

    # Validate the full tree before applying any module filter
    errors = validate_manifests(manifests)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    emit_modules = filter_modules(manifests, args.modules)
    if not emit_modules:
        print("  No modules found matching filter!")
        return 1

    sorted_modules = topo_sort(emit_modules)

    # Strip temporary metadata keys before writing manifests
    for m in sorted_modules:
        m.pop("_yaml_path", None)
        m.pop("_dir_name", None)

    # Prepend framework manifest (always first, before all modules)
    if framework_manifest:
        framework_manifest.pop("_yaml_path", None)
        framework_manifest.pop("_dir_name", None)
        sorted_modules.insert(0, framework_manifest)
    names = [m["name"] for m in sorted_modules]
    print("  Module order: %s" % " -> ".join(names))

    librepy_flavor = args.skip_writeragent_extension
    build_dir = os.path.join(PROJECT_ROOT, "build", "generated")

    # 1. _manifest.py
    manifest_path = args.manifest_output or os.path.join(PROJECT_ROOT, "plugin", "_manifest.py")
    generate_manifest_py(sorted_modules, manifest_path, librepy_flavor=librepy_flavor)

    # 2. Standalone config dialogs and dialog.xlb
    standalone_dialog_ids = generate_standalone_config_dialogs(sorted_modules, build_dir)
    wa_dialogs_ext = os.path.join(PROJECT_ROOT, "extension", "Dialogs")
    wa_dialogs_gen = os.path.join(build_dir, "Dialogs")
    update_dialog_xlb(wa_dialogs_ext, standalone_dialog_ids, tpl_path=os.path.join(wa_dialogs_ext, "dialog.xlb.tpl"))
    update_dialog_xlb(wa_dialogs_gen, standalone_dialog_ids, tpl_path=os.path.join(wa_dialogs_ext, "dialog.xlb.tpl"))

    # 3. META-INF/manifest.xml
    if not args.skip_writeragent_extension:
        manifest_xml_path = os.path.join(PROJECT_ROOT, "extension", "META-INF", "manifest.xml")
        generate_manifest_xml(manifest_xml_path)

    # 4. SettingsDialog Tabs
    generate_settings_dialog_tabs(
        sorted_modules,
        os.path.join(PROJECT_ROOT, "extension", "Dialogs", "SettingsDialog.xdl.tpl"),
        os.path.join(PROJECT_ROOT, "build", "generated", "Dialogs", "SettingsDialog.xdl"),
        librepy_flavor=librepy_flavor,
    )

    # 5. Patch version
    if not args.skip_writeragent_extension:
        patch_description_xml(os.path.join(PROJECT_ROOT, "extension"))

    # 6. Clean stale generated XDLs
    keep_basenames = {"SettingsDialog.xdl"} | {"%s.xdl" % dialog_id for dialog_id in standalone_dialog_ids}
    remove_stale_generated_xdls(os.path.join(build_dir, "Dialogs"), keep_basenames)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
