#!/usr/bin/env python3
"""Build an .oxt LibreOffice extension from the plugin/ directory.

Two-step process:
  1. Assemble all files into build/bundle/ with final archive paths
  2. Zip build/bundle/ into the .oxt

This lets you tweak files in build/bundle/ and re-zip with --repack.

Usage:
    python3 scripts/build_oxt.py                    # full build
    python3 scripts/build_oxt.py --repack           # re-zip bundle only
    python3 scripts/build_oxt.py --modules core mcp
"""

import argparse
import os
import shutil
import sys
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files/dirs always included from extension/
ALWAYS_INCLUDE_EXTENSION = [
    "extension/description.xml",
    "extension/META-INF/",
    "extension/ProtocolHandler.xcu",
    "extension/Addons.xcu",
    "extension/Accelerators.xcu",
    "extension/XPromptFunction.rdb",
    "extension/registration/",
    "extension/registry/",
    "extension/dialogs/",
    "extension/WriterAgentDialogs/",
    "extension/assets/",
]

ALWAYS_INCLUDE_PLUGIN = [
    "plugin/__init__.py",
    "plugin/main.py",
    "plugin/modules/chatbot/panel_factory.py",
    "plugin/version.py",
    "plugin/prompt_function.py",
    "plugin/_manifest.py",
    "plugin/plugin.yaml",
    "plugin/framework/",
    "plugin/locales/",
    "plugin/lib/",
    "plugin/contrib/",
]

# Only included when --with-tests (make release)
RELEASE_INCLUDE_PLUGIN = [
    "plugin/testing_runner.py",
    "plugin/tests/",
]

ALWAYS_INCLUDE_ROOT = [
    "contrib/",
]

# Auto-discover all top-level module directories
def _discover_modules(base_dir):
    """Return sorted list of top-level module directory names."""
    modules_dir = os.path.join(base_dir, "plugin", "modules")
    if not os.path.isdir(modules_dir):
        return []
    return sorted(
        d for d in os.listdir(modules_dir)
        if os.path.isdir(os.path.join(modules_dir, d))
        and not d.startswith(("_", "."))
    )

EXCLUDE_PATTERNS = (
    ".git",
    ".DS_Store",
    "__pycache__",
    ".pyc",
    ".pyo",
    "tests/",
    "test_",
    ".tpl",
)

# Generated files (XCS/XCU, XDL dialogs)
GENERATED_INCLUDES = [
    "build/generated/dialogs/",
    "build/generated/WriterAgentDialogs/",
    "build/generated/Addons.xcu",
    "build/generated/Accelerators.xcu",
]

BUNDLE_DIR = "build/bundle"


def should_exclude(path, with_tests=False):
    # When with_tests, allow plugin/tests/; otherwise exclude it (smaller default build)
    path_norm = path.replace("\\", "/")
    if path_norm.startswith("plugin/tests/") or path_norm == "plugin/tests":
        return not with_tests
    # gettext source/template only; runtime loads .mo (see plugin/framework/i18n.py)
    if path_norm.startswith("plugin/locales/") and (
        path_norm.endswith(".po") or path_norm.endswith(".pot")
    ):
        return True
    for pat in EXCLUDE_PATTERNS:
        if pat in path:
            return True
    return False


def collect_files(base_dir, include_paths, with_tests=False):
    """Collect all files from a list of paths relative to base_dir."""
    files = []
    for inc in include_paths:
        full = os.path.join(base_dir, inc)
        if os.path.isfile(full):
            if not should_exclude(inc, with_tests):
                files.append(inc)
        elif os.path.isdir(full):
            for root, dirs, filenames in os.walk(full):
                dirs[:] = [d for d in dirs if not should_exclude(d, with_tests)]
                # Add empty directories just in case they are needed for structure
                if not dirs and not filenames:
                    relpath = os.path.relpath(root, base_dir)
                    if not should_exclude(relpath, with_tests):
                        files.append(relpath + "/")
                for fn in filenames:
                    filepath = os.path.join(root, fn)
                    relpath = os.path.relpath(filepath, base_dir)
                    if not should_exclude(relpath, with_tests):
                        files.append(relpath)
        else:
            print("  WARNING: %s not found, skipping" % inc, file=sys.stderr)
    return sorted(set(files))


def remap_path(f):
    """Convert a project-relative path to its .oxt archive path."""
    f = f.replace(os.sep, "/")
    if f.startswith("extension/"):
        return f[len("extension/"):]
    if f.startswith("build/generated/"):
        return f[len("build/generated/"):]
    return f


def assemble_bundle(base_dir, modules, no_recording=False, with_tests=False):
    """Copy all files into build/bundle/ with final archive paths."""
    bundle_path = os.path.join(base_dir, BUNDLE_DIR)

    # Clean previous bundle
    if os.path.exists(bundle_path):
        shutil.rmtree(bundle_path)

    include = list(ALWAYS_INCLUDE_EXTENSION)
    include.extend(ALWAYS_INCLUDE_PLUGIN)
    if with_tests:
        include.extend(RELEASE_INCLUDE_PLUGIN)
        print("  Dev build: including plugin/tests/ and testing_runner.py")
    include.extend(ALWAYS_INCLUDE_ROOT)

    for mod in modules:
        mod_dir = "plugin/modules/%s/" % mod
        mod_path = os.path.join(base_dir, mod_dir)
        if os.path.isdir(mod_path):
            include.append(mod_dir)
        else:
            print("  WARNING: module '%s' not found at %s" % (mod, mod_dir),
                  file=sys.stderr)

    include.extend(GENERATED_INCLUDES)
    files = collect_files(base_dir, include, with_tests=with_tests)

    if no_recording:
        # Exclude voice recording: audio_recorder.py and entire contrib/audio/
        files = [
            f for f in files
            if f != "plugin/modules/chatbot/audio_recorder.py"
            and not f.startswith("contrib/audio/")
        ]
        print("  No-recording build: excluded audio_recorder.py and contrib/audio/")

    count = 0
    for f in files:
        src = os.path.join(base_dir, f)
        arcname = remap_path(f)
        dst = os.path.join(bundle_path, arcname)
        if f.endswith("/"):
            os.makedirs(dst, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        count += 1

    # Copy vendored pip packages into plugin/lib/ inside the bundle
    vendor_dir = os.path.join(base_dir, "vendor")
    if os.path.isdir(vendor_dir):
        vendor_count = 0
        for entry in sorted(os.listdir(vendor_dir)):
            if entry.endswith(".dist-info") or entry.startswith(("_", ".")):
                continue
            src_path = os.path.join(vendor_dir, entry)
            dst_path = os.path.join(bundle_path, "plugin", "lib", entry)
            if os.path.isdir(src_path):
                if os.path.exists(dst_path):
                    shutil.rmtree(dst_path)
                shutil.copytree(src_path, dst_path)
            elif os.path.isfile(src_path):
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)
            vendor_count += 1
        if vendor_count:
            print("Vendored %d packages into plugin/lib/" % vendor_count)

    # Release build: strip Debug (test) menu and write Addons.xcu to bundle
    if not with_tests:
        src_addons = os.path.join(base_dir, "extension", "Addons.xcu")
        dst_addons = os.path.join(bundle_path, "Addons.xcu")
        if os.path.isfile(src_addons):
            with open(src_addons, "r", encoding="utf-8") as f:
                content = f.read()
            marker = 'oor:name="M_Debug"'
            start = content.find(marker)
            if start != -1:
                tag_start = content.rfind("<node ", 0, start)
                if tag_start != -1:
                    depth = 1
                    pos = content.find(">", start) + 1
                    while depth > 0 and pos < len(content):
                        next_open = content.find("<node ", pos)
                        if next_open == -1:
                            next_open = content.find("<node>", pos)
                        next_close = content.find("</node>", pos)
                        if next_close == -1:
                            break
                        use_open = next_open != -1 and (next_open < next_close)
                        if use_open:
                            depth += 1
                            pos = content.find(">", next_open) + 1
                        else:
                            depth -= 1
                            if depth == 0:
                                end_pos = next_close + len("</node>")
                                content = content[:tag_start] + content[end_pos:].lstrip("\r\n")
                                break
                            pos = next_close + len("</node>")
            with open(dst_addons, "w", encoding="utf-8") as f:
                f.write(content)
            print("  Release build: stripped Debug menu from Addons.xcu")

    print("Assembled %d files in %s" % (count, BUNDLE_DIR))
    return count


def zip_bundle(base_dir, output):
    """Zip build/bundle/ into the .oxt."""
    bundle_path = os.path.join(base_dir, BUNDLE_DIR)
    if not os.path.isdir(bundle_path):
        print("ERROR: %s not found. Run without --repack first." % BUNDLE_DIR,
              file=sys.stderr)
        return 1

    output_path = os.path.join(base_dir, output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    count = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, filenames in os.walk(bundle_path):
            dirs[:] = [d for d in dirs if not should_exclude(d, with_tests=True)]
            for fn in filenames:
                filepath = os.path.join(root, fn)
                arcname = os.path.relpath(filepath, bundle_path)
                if not should_exclude(arcname, with_tests=True):
                    zf.write(filepath, arcname)
                    count += 1

    print("Created %s with %d files" % (output, count))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Build WriterAgent .oxt extension")
    parser.add_argument(
        "--modules", nargs="+", default=None,
        help="Modules to include (default: auto-discover all)")
    parser.add_argument(
        "--output", default="build/WriterAgent.oxt",
        help="Output file (default: build/localwriter.oxt)")
    parser.add_argument(
        "--repack", action="store_true",
        help="Only re-zip build/bundle/ (skip assembly)")
    parser.add_argument(
        "--no-recording", action="store_true",
        help="Exclude voice recording: do not bundle contrib/audio/ or plugin/modules/chatbot/audio_recorder.py")
    parser.add_argument(
        "--no-tests", action="store_true",
        help="Exclude plugin/tests/ and testing_runner.py (for release builds)")
    args = parser.parse_args()

    if not args.repack:
        modules = args.modules or _discover_modules(PROJECT_ROOT)
        assemble_bundle(
            PROJECT_ROOT, modules,
            no_recording=args.no_recording,
            with_tests=not args.no_tests,
        )

    return zip_bundle(PROJECT_ROOT, args.output)


if __name__ == "__main__":
    sys.exit(main())
