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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.build_oxt import (  # noqa: E402
    collect_files,
    remap_path,
    should_exclude,
    strip_production_code,
    _vendor_copy_ignore,
)
from scripts.libreharper_bundle_paths import (  # noqa: E402
    collect_libreharper_plugin_paths,
    iter_libreharper_vendor_packages,
    slim_libreharper_package_inits,
)
from scripts.manifest_registry import patch_description_xml  # noqa: E402

LIBREHARPER_BUNDLE_DIR = "build/bundle-libreharper"
LIBREHARPER_EXTENSION_ID = "org.extension.libreharper"
LIBREHARPER_MANIFEST = "build/generated/_manifest_libreharper.py"

LIBREHARPER_EXTENSION_INCLUDES = [
    "extension-harper/description.xml",
    "extension-harper/META-INF/",
    "extension-harper/registry/",
    "extension/assets/",
]


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


def assemble_libreharper_bundle(base_dir: str, *, with_tests: bool = False, strip: bool = True) -> int:
    bundle_path = os.path.join(base_dir, LIBREHARPER_BUNDLE_DIR)
    if os.path.exists(bundle_path):
        shutil.rmtree(bundle_path)

    _ensure_libreharper_manifest(base_dir)
    patch_description_xml(os.path.join(base_dir, "extension-harper"))

    plugin_paths = collect_libreharper_plugin_paths(base_dir)
    manifest_src = os.path.join(base_dir, LIBREHARPER_MANIFEST)
    if not os.path.isfile(manifest_src):
        print("ERROR: %s not found after manifest generation" % manifest_src, file=sys.stderr)
        return 0

    include = list(LIBREHARPER_EXTENSION_INCLUDES)
    include.extend(plugin_paths)

    files = collect_files(base_dir, include, with_tests=with_tests)

    count = 0
    for rel in files:
        src = os.path.join(base_dir, rel)
        arcname = _libreharper_remap_path(rel)
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
    vendor_packages = iter_libreharper_vendor_packages(vendor_dir)
    if vendor_packages:
        vendor_count = 0
        for entry in vendor_packages:
            src_path = os.path.join(vendor_dir, entry)
            dst_path = os.path.join(bundle_path, "plugin", "lib", entry)
            if os.path.exists(dst_path):
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path, ignore=_vendor_copy_ignore)
            vendor_count += 1
        print(
            "Vendored %d packages into plugin/lib/ (%s)"
            % (vendor_count, ", ".join(vendor_packages))
        )

    if strip and not with_tests:
        strip_production_code(bundle_path, dry_run=False)

    slim_libreharper_package_inits(os.path.join(bundle_path, "plugin"))
    print("  Slimmed writer/doc/client package inits for LibreHarper")

    print(
        "Assembled %d files in %s (%d plugin modules)"
        % (count, LIBREHARPER_BUNDLE_DIR, len(plugin_paths))
    )
    return count


def zip_libreharper_bundle(base_dir: str, output: str) -> int:
    bundle_path = os.path.join(base_dir, LIBREHARPER_BUNDLE_DIR)
    if not os.path.isdir(bundle_path):
        print("ERROR: %s not found. Run without --repack first." % LIBREHARPER_BUNDLE_DIR, file=sys.stderr)
        return 1

    output_path = os.path.join(base_dir, output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    count = 0
    import zipfile

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, filenames in os.walk(bundle_path):
            dirs[:] = [d for d in dirs if not should_exclude(d, with_tests=True)]
            for fn in filenames:
                filepath = os.path.join(root, fn)
                arcname = os.path.relpath(filepath, bundle_path)
                if not should_exclude(arcname, with_tests=True):
                    zf.write(filepath, arcname)
                    count += 1

    print("Created %s with %d files (%s)" % (output, count, LIBREHARPER_EXTENSION_ID))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LibreHarper .oxt extension")
    parser.add_argument("--output", default="build/LibreHarper.oxt", help="Output .oxt path")
    parser.add_argument("--repack", action="store_true", help="Only re-zip bundle-libreharper/")
    parser.add_argument("--with-tests", action="store_true", help="Include test trees in bundle")
    parser.add_argument("--no-strip", action="store_true", help="Skip production code stripping")
    args = parser.parse_args()

    if not args.repack:
        assembled = assemble_libreharper_bundle(
            PROJECT_ROOT,
            with_tests=args.with_tests,
            strip=not args.no_strip,
        )
        if assembled == 0:
            return 1

    return zip_libreharper_bundle(PROJECT_ROOT, args.output)


if __name__ == "__main__":
    sys.exit(main())
