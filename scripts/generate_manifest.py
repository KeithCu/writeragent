#!/usr/bin/env python3
"""Generate _manifest.py and XCS/XCU from module.yaml files.

Reads each module.yaml under plugin/, validates it, and produces:
  - build/generated/_manifest.py     — Python dict for runtime
  - build/generated/registry/*.xcs   — LO config schemas
  - build/generated/registry/*.xcu   — LO config defaults
  - Generates description.xml from description.xml.tpl with version

Usage:
    python3 scripts/generate_manifest.py
    python3 scripts/generate_manifest.py --modules core mcp ai_openai
"""

import argparse
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


from plugin.framework.module_graph import topo_sort

SKIP_DIRS = {"lib", "contrib", "tests", "__pycache__"}

VALID_WIDGETS = {
    "text", "button", "checkbox", "combo", "number", "password",
    "select", "separator", "textarea", "list_detail",
}


def validate_modules(manifests, project_root=None):
    """Validate parsed module manifests, raising ValueError naming file and key on error."""
    if project_root is None:
        project_root = PROJECT_ROOT

    seen_names = set()
    seen_provides = set()
    known_providers = {"main"}
    module_by_name = {m["name"]: m for m in manifests}

    for m in manifests:
        yp = m.get("_yaml_path", m.get("name", "unknown"))
        name = m["name"]
        if name in seen_names:
            raise ValueError(f"{yp}: name: duplicate module name {name!r}")
        seen_names.add(name)
        known_providers.add(name)
        for svc in m.get("provides_services", []):
            if svc in seen_provides:
                raise ValueError(f"{yp}: provides_services: duplicate service {svc!r}")
            seen_provides.add(svc)
            known_providers.add(svc)

    for m in manifests:
        yp = m.get("_yaml_path", m.get("name", "unknown"))
        name = m["name"]
        for req in m.get("requires", []):
            if req not in known_providers:
                raise ValueError(f"{yp}: requires: unresolved requirement {req!r}")

        inline_target = m.get("config_inline")
        if inline_target is not None:
            if not isinstance(inline_target, str) or not inline_target.strip():
                raise ValueError(f"{yp}: config_inline: must be a non-empty string")
            if inline_target == name:
                raise ValueError(f"{yp}: config_inline: cannot inline into self {inline_target!r}")
            if inline_target not in module_by_name:
                raise ValueError(f"{yp}: config_inline: unknown target module {inline_target!r}")
            if module_by_name[inline_target].get("config_inline"):
                raise ValueError(f"{yp}: config_inline: target module {inline_target!r} cannot itself set config_inline")

        if "config_dialog" in m:
            cd = m["config_dialog"]
            if not isinstance(cd, dict):
                raise ValueError(f"{yp}: config_dialog: must be a dict")
            if "id" in cd:
                cd_id = cd["id"]
                if not isinstance(cd_id, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cd_id):
                    raise ValueError(f"{yp}: config_dialog.id: invalid dialog id {cd_id!r}")
            if "buttons" in cd:
                buttons = cd["buttons"]
                if not isinstance(buttons, list) or len(buttons) == 0:
                    raise ValueError(f"{yp}: config_dialog.buttons: must be a non-empty list")
                valid_buttons = {"apply", "ok", "close"}
                for btn in buttons:
                    if btn not in valid_buttons:
                        raise ValueError(f"{yp}: config_dialog.buttons: invalid button {btn!r}")

        for action_name, action_def in m.get("actions", {}).items():
            if isinstance(action_def, dict) and "icon" in action_def:
                icon = action_def["icon"]
                icon_path = os.path.join(project_root, "extension", "assets", f"{icon}_16.png")
                if not os.path.isfile(icon_path):
                    raise ValueError(f"{yp}: actions.{action_name}.icon: icon file not found at {icon_path}")

        config = m.get("config", {})
        if isinstance(config, dict):
            for fname, schema in config.items():
                if fname in ("__module__", "btn_apply", "btn_ok", "btn_close") or fname.startswith(("btn_tab_", "hlp_")):
                    raise ValueError(f"{yp}: config.{fname}: reserved config key name {fname!r}")
                if not isinstance(schema, dict):
                    continue
                widget = schema.get("widget", "text")
                if widget not in VALID_WIDGETS:
                    raise ValueError(f"{yp}: config.{fname}.widget: invalid widget {widget!r}")
                if widget == "select":
                    if not schema.get("options") and not schema.get("options_provider"):
                        raise ValueError(f"{yp}: config.{fname}.options: widget select requires options or options_provider")
                if "min" in schema and "max" in schema:
                    if schema["min"] > schema["max"]:
                        raise ValueError(f"{yp}: config.{fname}.min: min {schema['min']} > max {schema['max']}")
                stype = schema.get("type")
                if stype == "bool":
                    stype = "boolean"
                if "default" in schema and stype:
                    def_val = schema["default"]
                    if stype == "int":
                        if isinstance(def_val, bool) or not isinstance(def_val, int):
                            raise ValueError(f"{yp}: config.{fname}.default: expected int, got {def_val!r}")
                    elif stype == "float":
                        if isinstance(def_val, bool) or not isinstance(def_val, (int, float)):
                            raise ValueError(f"{yp}: config.{fname}.default: expected float, got {def_val!r}")
                    elif stype == "boolean":
                        if not isinstance(def_val, bool):
                            raise ValueError(f"{yp}: config.{fname}.default: expected bool, got {def_val!r}")

    # Check cycles via shared topo_sort
    topo_sort(manifests)


def find_modules(modules_dir, filter_names=None, project_root=None):
    """Find all module.yaml files recursively and return parsed manifests.

    Module name comes from the ``name`` field in module.yaml.
    Directory convention: dots map to underscores (tunnel.bore -> tunnel_bore/).
    Falls back to directory-derived name if ``name`` is absent.
    """
    if project_root is None:
        project_root = PROJECT_ROOT

    all_manifests = []
    for dirpath, dirnames, filenames in os.walk(modules_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if "module.yaml" not in filenames:
            continue
        yaml_path = os.path.join(dirpath, "module.yaml")
        with open(yaml_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        if not isinstance(manifest, dict):
            continue
        rel = os.path.relpath(dirpath, modules_dir)
        path_name = rel.replace(os.sep, ".")
        first_segment = rel.split(os.sep)[0]
        declared_name = manifest.get("name")
        module_name = declared_name or path_name
        manifest.setdefault("name", module_name)
        manifest["_yaml_path"] = yaml_path
        manifest["_match_keys"] = {module_name, path_name, first_segment}
        all_manifests.append(manifest)

    # Validate all discovered modules before filtering
    validate_modules(all_manifests, project_root=project_root)

    if filter_names:
        filter_set = set(filter_names)
        matched_tokens = set()
        matched_manifests = []
        for m in all_manifests:
            keys = m.get("_match_keys", {m["name"]})
            intersect = keys & filter_set
            if intersect:
                matched_tokens.update(intersect)
                matched_manifests.append(m)
        unmatched = [tok for tok in filter_names if tok not in matched_tokens]
        if unmatched:
            raise ValueError(f"Unknown module filter token: {unmatched[0]!r}")
        result = matched_manifests
    else:
        result = all_manifests

    result.sort(key=lambda m: m["name"])
    return result



import math
import pprint


def _check_finite(obj, path=""):
    """Reject non-finite floats (NaN/Inf) that cannot be safely serialized."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"Non-finite float {obj} at {path}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _check_finite(item, f"{path}[{idx}]")


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
    """Generate _manifest.py containing the MODULES list."""
    from manifest_common import write_if_changed

    lines = [
        '"""Auto-generated module manifest. DO NOT EDIT.',
        "",
        "Extension version lives in plugin/version.py.",
        '"""',
        "",
        "MODULES = [",
    ]

    for m in modules:
        config = m.get("config", {})
        if librepy_flavor:
            config = _filter_librepy_config(config)

        # Clean dict — only keep runtime-relevant keys
        entry = {
            "name": m["name"],
            "title": m.get("title", ""),
            "requires": m.get("requires", []),
            "provides_services": m.get("provides_services", []),
            "config": config,
            "config_inline": m.get("config_inline"),
            "action_icons": {
                k: v["icon"]
                for k, v in m.get("actions", {}).items()
                if isinstance(v, dict) and v.get("icon")
            },
        }
        if m.get("settings_tab") is False:
            entry["settings_tab"] = False
        if m.get("config_dialog"):
            entry["config_dialog"] = m["config_dialog"]

        _check_finite(entry)
        entry_text = pprint.pformat(entry, indent=4, sort_dicts=False)
        indented = "\n".join("    " + ln for ln in entry_text.splitlines())
        lines.append(f"{indented},")

    lines.append("]")
    lines.append("")

    source = "\n".join(lines)
    compile(source, output_path, "exec")
    write_if_changed(output_path, source)
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
        description="Generate _manifest.py and XCS/XCU from module.yaml files")
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
        with open(plugin_yaml_path) as f:
            framework_manifest = yaml.safe_load(f)
        framework_manifest.setdefault("name", "main")
        print("  Loaded framework config: plugin/plugin.yaml")

    print("Scanning modules in %s..." % modules_dir)
    try:
        manifests = find_modules(modules_dir, args.modules)
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    
    if not manifests:
        print("  No modules found!")
        return 1

    sorted_modules = topo_sort(manifests)

    # Prepend framework manifest (always first, before all modules)
    if framework_manifest:
        sorted_modules.insert(0, framework_manifest)
    names = [m["name"] for m in sorted_modules]
    print("  Module order: %s" % " -> ".join(names))

    librepy_flavor = args.skip_writeragent_extension

    build_dir = os.path.join(PROJECT_ROOT, "build", "generated")

    # 2. _manifest.py
    manifest_path = args.manifest_output or os.path.join(PROJECT_ROOT, "plugin", "_manifest.py")
    generate_manifest_py(sorted_modules, manifest_path, librepy_flavor=librepy_flavor)

    # 4. Standalone config dialogs and dialog.xlb
    standalone_dialog_ids = generate_standalone_config_dialogs(sorted_modules, build_dir)
    wa_dialogs_ext = os.path.join(PROJECT_ROOT, "extension", "Dialogs")
    wa_dialogs_gen = os.path.join(build_dir, "Dialogs")
    update_dialog_xlb(wa_dialogs_ext, standalone_dialog_ids, tpl_path=os.path.join(wa_dialogs_ext, "dialog.xlb.tpl"))
    update_dialog_xlb(wa_dialogs_gen, standalone_dialog_ids, tpl_path=os.path.join(wa_dialogs_ext, "dialog.xlb.tpl"))

    # 6. META-INF/manifest.xml
    if not args.skip_writeragent_extension:
        manifest_xml_path = os.path.join(PROJECT_ROOT, "extension", "META-INF", "manifest.xml")
        generate_manifest_xml(sorted_modules, manifest_xml_path)

    # 7. SettingsDialog Tabs
    generate_settings_dialog_tabs(
        sorted_modules,
        os.path.join(PROJECT_ROOT, "extension", "Dialogs", "SettingsDialog.xdl.tpl"),
        os.path.join(PROJECT_ROOT, "build", "generated", "Dialogs", "SettingsDialog.xdl"),
        librepy_flavor=librepy_flavor,
    )

    # 8. Patch version
    if not args.skip_writeragent_extension:
        patch_description_xml(os.path.join(PROJECT_ROOT, "extension"))

    # 9. Clean stale generated XDLs
    keep_basenames = {"SettingsDialog.xdl"} | {"%s.xdl" % dialog_id for dialog_id in standalone_dialog_ids}
    remove_stale_generated_xdls(os.path.join(build_dir, "Dialogs"), keep_basenames)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
