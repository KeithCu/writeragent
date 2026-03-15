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
            "config_inline": m.get("config_inline"),
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


# Ensure scripts/ is on path for manifest_xdl and manifest_registry
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from manifest_xdl import generate_xdl_files
from manifest_registry import (
    generate_addons_xcu,
    generate_accelerators_xcu,
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
