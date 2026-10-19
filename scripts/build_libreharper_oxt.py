#!/usr/bin/env python3
"""Build the LibreHarper standalone .oxt (Harper grammar Linguistic2 proofreader).

Copies only the plugin files required for offline Harper (see scripts/libreharper_bundle_paths.py).

Usage:
    python3 scripts/build_libreharper_oxt.py
    python3 scripts/build_libreharper_oxt.py --repack
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import xml.etree.ElementTree as ET

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
_SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

from plugin.version import EXTENSION_VERSION  # noqa: E402
from scripts.build_oxt import (  # noqa: E402
    collect_files,
    remap_path,
    resolve_archive_sources,
    strip_production_code,
    sync_vendor_into_lib,
    verify_bundle_manifest,
    write_oxt_zip,
)
from scripts.libreharper_bundle_paths import (  # noqa: E402
    LIBREHARPER_CLIENT_INIT,
    LIBREHARPER_DOC_INIT,
    LIBREHARPER_VENDOR_PACKAGES,
    LIBREHARPER_WRITER_INIT,
    collect_libreharper_plugin_paths,
    is_libreharper_forbidden_chat_ui,
    iter_libreharper_vendor_packages,
    slim_libreharper_package_inits,
)
from scripts.manifest_registry import patch_description_xml  # noqa: E402
from scripts.plugin_import_graph import (  # noqa: E402
    missing_dual_install_imports,
    missing_module_level_plugin_imports,
)

LIBREHARPER_BUNDLE_DIR = "build/bundle-libreharper"
LIBREHARPER_EXTENSION_ID = "org.extension.libreharper"
LIBREHARPER_MANIFEST = "build/generated/_manifest_libreharper.py"

LIBREHARPER_EXTENSION_INCLUDES = [
    "extension-harper/description.xml",
    "extension-harper/META-INF/",
    "extension-harper/registry/",
    "extension/assets/logo.jpg",
]

REQUIRED_LIBREHARPER_ARCHIVE_PATHS: tuple[str, ...] = (
    "META-INF/manifest.xml",
    "description.xml",
    "registry/org/openoffice/Office/LinguisticLibreHarperGrammar.xcu",
    "plugin/writer/locale/harper_proofreader.py",
    "plugin/_manifest.py",
    "plugin/__init__.py",
    "plugin/lib/json_repair/__init__.py",
    "assets/logo.jpg",
    "plugin/framework/module_base.py",
    "plugin/writer/__init__.py",
    "plugin/doc/__init__.py",
    "plugin/framework/client/__init__.py",
)

FORBIDDEN_LIBREHARPER_ARCHIVE_PATHS: tuple[str, ...] = (
    "plugin/chatbot/panel_factory.py",
    "plugin/chatbot/__init__.py",
    "Dialogs/ChatPanelDialog.xdl",
    "registry/org/openoffice/Office/UI/Factories.xcu",
    "registry/org/openoffice/Office/UI/Sidebar.xcu",
    "Addons.xcu",
    "Jobs.xcu",
)


class LibreHarperBuildError(Exception):
    """Raised when LibreHarper OXT assembly or validation fails."""


def _libreharper_remap_path(path: str) -> str:
    path = path.replace(os.sep, "/")
    if path.startswith("extension-harper/"):
        return path[len("extension-harper/") :]
    if path.startswith("extension/assets/"):
        return path[len("extension/") :]
    return remap_path(path)


def _ensure_libreharper_manifest(base_dir: str) -> None:
    """Build slim MODULES from extension-harper/module.yaml (doc.* keys, default harper)."""
    import yaml

    from scripts.generate_manifest import generate_manifest_py

    yaml_path = os.path.join(base_dir, "extension-harper", "module.yaml")
    with open(yaml_path, encoding="utf-8") as fh:
        module = yaml.safe_load(fh)
    module.setdefault("name", "doc")
    manifest_out = os.path.join(base_dir, LIBREHARPER_MANIFEST)
    os.makedirs(os.path.dirname(manifest_out), exist_ok=True)
    print("  Generating slim LibreHarper manifest...")
    generate_manifest_py([module], manifest_out)


def validate_libreharper_bundle(bundle_path: str) -> None:
    """Validate that bundle_path contains all required LibreHarper files and no forbidden UI."""
    for rel in REQUIRED_LIBREHARPER_ARCHIVE_PATHS:
        full = os.path.join(bundle_path, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            raise LibreHarperBuildError(f"LibreHarper bundle missing required file: {rel}")

    stub_checks = (
        ("plugin/writer/__init__.py", LIBREHARPER_WRITER_INIT),
        ("plugin/doc/__init__.py", LIBREHARPER_DOC_INIT),
        ("plugin/framework/client/__init__.py", LIBREHARPER_CLIENT_INIT),
    )
    for rel, expected in stub_checks:
        full = os.path.join(bundle_path, rel.replace("/", os.sep))
        with open(full, encoding="utf-8") as fh:
            content = fh.read()
        if content != expected:
            raise LibreHarperBuildError(f"LibreHarper stub init mismatch in {rel}")

    for rel in FORBIDDEN_LIBREHARPER_ARCHIVE_PATHS:
        full = os.path.join(bundle_path, rel.replace("/", os.sep))
        if os.path.exists(full):
            raise LibreHarperBuildError(f"LibreHarper bundle contains forbidden file: {rel}")

    manifest_xml = os.path.join(bundle_path, "META-INF", "manifest.xml")
    try:
        tree = ET.parse(manifest_xml)
        root = tree.getroot()
    except Exception as exc:
        raise LibreHarperBuildError(f"Malformed META-INF/manifest.xml: {exc}") from exc

    ns = {"manifest": "http://openoffice.org/2001/manifest"}
    for elem in root.findall(".//manifest:file-entry", ns):
        full_path = elem.attrib.get(f"{{{ns['manifest']}}}full-path")
        if not full_path:
            full_path = elem.attrib.get("manifest:full-path") or elem.attrib.get("full-path")
        if not full_path or full_path in ("/", ""):
            continue
        entry_file = os.path.join(bundle_path, full_path.replace("/", os.sep))
        if not os.path.isfile(entry_file):
            raise LibreHarperBuildError(f"META-INF/manifest.xml entry not in bundle: {full_path}")

    desc_xml = os.path.join(bundle_path, "description.xml")
    try:
        tree = ET.parse(desc_xml)
        d_root = tree.getroot()
    except Exception as exc:
        raise LibreHarperBuildError(f"Malformed description.xml: {exc}") from exc

    id_elem = None
    ver_elem = None
    for el in d_root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "identifier":
            id_elem = el
        elif tag == "version":
            ver_elem = el

    id_val = id_elem.attrib.get("value") if id_elem is not None else None
    if id_val != LIBREHARPER_EXTENSION_ID:
        raise LibreHarperBuildError(
            f"description.xml identifier '{id_val}' != '{LIBREHARPER_EXTENSION_ID}'"
        )
    ver_val = ver_elem.attrib.get("value") if ver_elem is not None else None
    if ver_val != EXTENSION_VERSION:
        raise LibreHarperBuildError(
            f"description.xml version '{ver_val}' != '{EXTENSION_VERSION}'"
        )

    manifest_py = os.path.join(bundle_path, "plugin", "_manifest.py")
    with open(manifest_py, encoding="utf-8") as fh:
        code = fh.read()
    ns_dict: dict[str, object] = {}
    try:
        exec(code, ns_dict)
    except Exception as exc:
        raise LibreHarperBuildError(f"Failed to evaluate plugin/_manifest.py: {exc}") from exc

    modules = ns_dict.get("MODULES", [])
    doc_mod = None
    if isinstance(modules, list):
        for m in modules:
            if isinstance(m, dict) and m.get("name") == "doc":
                doc_mod = m
                break
    if doc_mod is None:
        raise LibreHarperBuildError("plugin/_manifest.py missing 'doc' module")

    cfg = doc_mod.get("config", {}) if isinstance(doc_mod, dict) else {}
    grammar_cfg = cfg.get("grammar_proofreader_enabled", {}) if isinstance(cfg, dict) else {}
    default_provider = grammar_cfg.get("default") if isinstance(grammar_cfg, dict) else None
    if default_provider != "harper":
        raise LibreHarperBuildError(
            f"plugin/_manifest.py grammar_proofreader_enabled default is '{default_provider}', expected 'harper'"
        )


def _preflight_libreharper(base_dir: str, plugin_paths: list[str]) -> None:
    tpl_path = os.path.join(base_dir, "extension-harper", "description.xml.tpl")
    if not os.path.isfile(tpl_path):
        raise LibreHarperBuildError(f"Missing description template: {tpl_path}")

    yaml_path = os.path.join(base_dir, "extension-harper", "module.yaml")
    if not os.path.isfile(yaml_path):
        raise LibreHarperBuildError(f"Missing module specification: {yaml_path}")

    vendor_dir = os.path.join(base_dir, "vendor")
    try:
        iter_libreharper_vendor_packages(vendor_dir)
    except FileNotFoundError as exc:
        raise LibreHarperBuildError(str(exc)) from exc

    for inc in LIBREHARPER_EXTENSION_INCLUDES:
        if inc == "extension-harper/description.xml":
            continue
        full = os.path.join(base_dir, inc)
        if not os.path.exists(full):
            raise LibreHarperBuildError(f"Missing extension include: {inc}")

    for rel in plugin_paths:
        full = os.path.join(base_dir, rel)
        if not os.path.isfile(full):
            raise LibreHarperBuildError(f"Missing allowlist plugin file: {rel}")

    missing_imports = missing_module_level_plugin_imports(base_dir, plugin_paths)
    if missing_imports:
        raise LibreHarperBuildError(
            "LibreHarper bundle missing required plugin imports:\n  " + "\n  ".join(missing_imports)
        )

    dual_missing = missing_dual_install_imports(base_dir, plugin_paths)
    if dual_missing:
        raise LibreHarperBuildError(
            "LibreHarper dual-install missing required imports from plugin/chatbot/__init__.py:\n  "
            + "\n  ".join(dual_missing)
        )


def assemble_libreharper_bundle(base_dir: str, *, strip: bool = True) -> int:
    bundle_path = os.path.join(base_dir, LIBREHARPER_BUNDLE_DIR)
    if os.path.exists(bundle_path):
        shutil.rmtree(bundle_path)

    try:
        plugin_paths = collect_libreharper_plugin_paths(base_dir)
    except FileNotFoundError as exc:
        raise LibreHarperBuildError(str(exc)) from exc

    _preflight_libreharper(base_dir, plugin_paths)

    _ensure_libreharper_manifest(base_dir)
    patch_description_xml(os.path.join(base_dir, "extension-harper"))

    manifest_src = os.path.join(base_dir, LIBREHARPER_MANIFEST)
    if not os.path.isfile(manifest_src):
        raise LibreHarperBuildError("%s not found after manifest generation" % manifest_src)

    include = list(LIBREHARPER_EXTENSION_INCLUDES)
    include.extend(plugin_paths)

    files = collect_files(base_dir, include, with_tests=False)
    # Harper must not register ChatPanelFactory / WriterAgentDeck / ChatPanel XDL.
    leaked = [
        rel
        for rel in files
        if is_libreharper_forbidden_chat_ui(rel)
        or is_libreharper_forbidden_chat_ui(_libreharper_remap_path(rel))
    ]
    if leaked:
        raise LibreHarperBuildError(
            "LibreHarper bundle must not include WriterAgent ChatPanel UI: %s"
            % ", ".join(leaked)
        )

    sources = resolve_archive_sources(base_dir, files, _libreharper_remap_path)
    count = 0
    for rel, arcname in sources:
        src = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
        dst = os.path.join(bundle_path, arcname)
        if rel.endswith("/"):
            os.makedirs(dst, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        count += 1

    shutil.copy2(
        manifest_src,
        os.path.join(bundle_path, "plugin", "_manifest.py"),
    )
    count += 1

    vendor_dir = os.path.join(base_dir, "vendor")
    vendor_count = sync_vendor_into_lib(
        vendor_dir,
        os.path.join(bundle_path, "plugin", "lib"),
        prune_websockets=False,
        only=LIBREHARPER_VENDOR_PACKAGES,
    )
    if vendor_count:
        print(
            "Vendored %d packages into plugin/lib/ (%s)"
            % (vendor_count, ", ".join(sorted(LIBREHARPER_VENDOR_PACKAGES)))
        )

    if strip:
        strip_production_code(bundle_path, dry_run=False)

    slim_libreharper_package_inits(os.path.join(bundle_path, "plugin"))
    print("  Slimmed writer/doc/client package inits for LibreHarper")

    try:
        verify_bundle_manifest(bundle_path, require_addons=False)
    except RuntimeError as err:
        raise LibreHarperBuildError(str(err)) from err

    validate_libreharper_bundle(bundle_path)

    print(
        "Assembled %d files in %s (%d plugin modules)"
        % (count, LIBREHARPER_BUNDLE_DIR, len(plugin_paths))
    )
    return count


def zip_libreharper_bundle(base_dir: str, output: str) -> int:
    bundle_path = os.path.join(base_dir, LIBREHARPER_BUNDLE_DIR)
    if not os.path.isdir(bundle_path):
        raise LibreHarperBuildError(
            f"{LIBREHARPER_BUNDLE_DIR} not found. Run without --repack first."
        )

    validate_libreharper_bundle(bundle_path)

    try:
        verify_bundle_manifest(bundle_path, require_addons=False)
    except RuntimeError as err:
        raise LibreHarperBuildError(str(err)) from err

    output_path = os.path.join(base_dir, output)
    try:
        count = write_oxt_zip(bundle_path, output_path)
    except RuntimeError as err:
        raise LibreHarperBuildError(str(err)) from err

    print("Created %s with %d files (%s)" % (output, count, LIBREHARPER_EXTENSION_ID))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LibreHarper .oxt extension")
    parser.add_argument("--output", default="build/LibreHarper.oxt", help="Output .oxt path")
    parser.add_argument("--repack", action="store_true", help="Only re-zip bundle-libreharper/")
    parser.add_argument("--no-strip", action="store_true", help="Skip production code stripping")
    args = parser.parse_args()

    try:
        if not args.repack:
            assembled = assemble_libreharper_bundle(
                PROJECT_ROOT,
                strip=not args.no_strip,
            )
            if assembled == 0:
                return 1

        return zip_libreharper_bundle(PROJECT_ROOT, args.output)
    except (LibreHarperBuildError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
