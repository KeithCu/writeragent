#!/usr/bin/env python3
"""Build the LibrePy standalone .oxt (scientific Python core prototype).

Copies only the plugin files required for Layers 0–6 (see scripts/librepy_bundle_paths.py).

Usage:
    python3 scripts/build_librepy_oxt.py
    python3 scripts/build_librepy_oxt.py --repack
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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
from scripts.librepy_bundle_paths import (  # noqa: E402
    collect_librepy_plugin_paths,
    iter_librepy_vendor_packages,
    slim_librepy_smolagents_init,
)
from scripts.manifest_registry import patch_description_xml  # noqa: E402

LIBREPY_BUNDLE_DIR = "build/bundle-librepy"
LIBREPY_EXTENSION_ID = "org.extension.librepy"
LIBREPY_MANIFEST = "build/generated/_manifest_librepy.py"

LIBREPY_EXTENSION_INCLUDES = [
    "extension-core/description.xml",
    "extension-core/META-INF/",
    "extension-core/ProtocolHandler.xcu",
    "extension-core/Addons.xcu",
    "extension-core/XPythonFunction.rdb",
    "extension-core/Jobs.xcu",
    "extension-core/registry/",
    "extension/assets/",
    "extension/WriterAgentDialogs/",
    "build/generated/WriterAgentDialogs/",
    "build/generated/dialogs/",
]

LIBREPY_DIALOG_FILES = (
    "extension/WriterAgentDialogs/PythonScriptDialog.xdl",
    "extension/WriterAgentDialogs/PythonTestProgressDialog.xdl",
    "extension/WriterAgentDialogs/TextAnalyticsDialog.xdl",
    "extension/WriterAgentDialogs/LatexInputDialog.xdl",
    "extension/WriterAgentDialogs/MsgBoxWithCopyDialog.xdl",
    "extension/WriterAgentDialogs/ErrorReportDialog.xdl",
    "extension/WriterAgentDialogs/ShortTextInputDialog.xdl",
    "extension/WriterAgentDialogs/EditInputDialog.xdl",
    "extension/WriterAgentDialogs/dialog.xlb",
    "extension/WriterAgentDialogs/script.xlb",
)


def _librepy_remap_path(path: str) -> str:
    path = path.replace(os.sep, "/")
    if path.startswith("extension-core/"):
        return path[len("extension-core/") :]
    if path.startswith("extension/assets/"):
        return path[len("extension/") :]
    if path.startswith("extension/WriterAgentDialogs/"):
        return path[len("extension/") :]
    return remap_path(path)


def _ensure_librepy_manifest(base_dir: str) -> None:
    manifest_out = os.path.join(base_dir, LIBREPY_MANIFEST)
    cmd = [
        sys.executable,
        os.path.join(base_dir, "scripts", "generate_manifest.py"),
        "--modules",
        "scripting",
        "vision",
        "--manifest-output",
        manifest_out,
        "--skip-writeragent-extension",
        "--skip-addons",
    ]
    print("  Generating slim LibrePy manifest...")
    subprocess.run(cmd, cwd=base_dir, check=True)


def assemble_librepy_bundle(base_dir: str, *, with_tests: bool = False, strip: bool = True) -> int:
    bundle_path = os.path.join(base_dir, LIBREPY_BUNDLE_DIR)
    if os.path.exists(bundle_path):
        shutil.rmtree(bundle_path)

    _ensure_librepy_manifest(base_dir)
    patch_description_xml(os.path.join(base_dir, "extension-core"))

    rdb_path = os.path.join(base_dir, "extension-core", "XPythonFunction.rdb")
    if not os.path.isfile(rdb_path):
        print(
            "ERROR: %s not found. Run: make rdb-core (requires LibreOffice SDK)"
            % rdb_path,
            file=sys.stderr,
        )
        return 0

    plugin_paths = collect_librepy_plugin_paths(base_dir)
    manifest_src = os.path.join(base_dir, LIBREPY_MANIFEST)
    if not os.path.isfile(manifest_src):
        print("ERROR: %s not found after manifest generation" % manifest_src, file=sys.stderr)
        return 0

    include = list(LIBREPY_EXTENSION_INCLUDES)
    include.extend(LIBREPY_DIALOG_FILES)
    include.extend(plugin_paths)
    locales_dir = os.path.join(base_dir, "build", "generated", "locales")
    if not os.path.isdir(locales_dir):
        print(
            "ERROR: %s not found. Run: make compile-translations-core (or make build-core)"
            % locales_dir,
            file=sys.stderr,
        )
        return 0
    include.append("build/generated/locales/")

    files = collect_files(base_dir, include, with_tests=with_tests)

    count = 0
    for rel in files:
        src = os.path.join(base_dir, rel)
        arcname = _librepy_remap_path(rel)
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
    vendor_packages = iter_librepy_vendor_packages(vendor_dir)
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

    slim_librepy_smolagents_init(os.path.join(bundle_path, "plugin"))
    print("  Slimmed plugin/contrib/smolagents/__init__.py for venv worker")

    print(
        "Assembled %d files in %s (%d plugin modules)"
        % (count, LIBREPY_BUNDLE_DIR, len(plugin_paths))
    )
    return count


def zip_librepy_bundle(base_dir: str, output: str) -> int:
    bundle_path = os.path.join(base_dir, LIBREPY_BUNDLE_DIR)
    if not os.path.isdir(bundle_path):
        print("ERROR: %s not found. Run without --repack first." % LIBREPY_BUNDLE_DIR, file=sys.stderr)
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

    print("Created %s with %d files (%s)" % (output, count, LIBREPY_EXTENSION_ID))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LibrePy .oxt extension")
    parser.add_argument("--output", default="build/LibrePy.oxt", help="Output .oxt path")
    parser.add_argument("--repack", action="store_true", help="Only re-zip bundle-librepy/")
    parser.add_argument("--with-tests", action="store_true", help="Include test trees in bundle")
    parser.add_argument("--no-strip", action="store_true", help="Skip production code stripping")
    args = parser.parse_args()

    if not args.repack:
        assembled = assemble_librepy_bundle(
            PROJECT_ROOT,
            with_tests=args.with_tests,
            strip=not args.no_strip,
        )
        if assembled == 0:
            return 1

    return zip_librepy_bundle(PROJECT_ROOT, args.output)


if __name__ == "__main__":
    sys.exit(main())
