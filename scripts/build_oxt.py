#!/usr/bin/env python3
"""Build an .oxt LibreOffice extension from the plugin/ directory.

Two-step process:
  1. Assemble all files into the bundle dir with final archive paths
  2. Zip that tree into the .oxt

Default bundle is build/bundle/ (tweak files there and re-zip with --repack).
``make release`` verification uses ``--bundle-dir`` on a temp tree plus ``--skip-zip``.

Usage:
    python3 scripts/build_oxt.py                    # full build
    python3 scripts/build_oxt.py --repack           # re-zip bundle only
    python3 scripts/build_oxt.py --modules core mcp
    python3 scripts/build_oxt.py --strip --bundle-dir /tmp/wa --skip-zip
"""

import argparse
import os
import shutil
import sys
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.strip_code import strip_production_code
from scripts.prune_vendored_websockets import prune_vendored_websockets

# Files/dirs always included from extension/
ALWAYS_INCLUDE_EXTENSION = [
    "extension/description.xml",
    "extension/META-INF/",
    "extension/ProtocolHandler.xcu",
    "extension/Addons.xcu",
    "extension/Accelerators.xcu",
    "extension/XPythonFunction.rdb",
    "extension/XPromptFunction.rdb",
    "extension/Jobs.xcu",
    "extension/registration/",
    "extension/registry/",
    "extension/Dialogs/",
    "extension/assets/",
]

ALWAYS_INCLUDE_PLUGIN_FILES = [
    "plugin/__init__.py",
    "plugin/main.py",
    "plugin/version.py",
    "plugin/_manifest.py",
    "plugin/plugin.yaml",
]


def get_always_include_plugin(base_dir):
    """Dynamically discover all subdirectories under plugin/ except tests and cache."""
    plugin_dir = os.path.join(base_dir, "plugin")
    includes = list(ALWAYS_INCLUDE_PLUGIN_FILES)
    if os.path.isdir(plugin_dir):
        for entry in sorted(os.listdir(plugin_dir)):
            if entry in ("tests", "__pycache__") or entry.startswith("."):
                continue
            entry_path = os.path.join(plugin_dir, entry)
            if os.path.isdir(entry_path):
                includes.append(f"plugin/{entry}/")
    return includes

# Only included when --with-tests (make release)
RELEASE_INCLUDE_PLUGIN = [
    "plugin/testing_runner.py",
    "plugin/tests/",
    "tests/",
]

ALWAYS_INCLUDE_ROOT = [
    "locales/",
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
    # make pyspector writes under plugin/; never ship in OXT
    ".pyspector_cache",
    ".pyspector_baseline.json",
)

# Generated files (XCS/XCU, XDL dialogs). One Dialogs/ tree only — a separate
# lowercase dialogs/ collides with Dialogs/ on Windows case-insensitive FS and
# wiped ChatPanelDialog.xdl during hot-deploy (empty sidebar).
GENERATED_INCLUDES = [
    "build/generated/Dialogs/",
    "build/generated/Addons.xcu",
    "build/generated/Accelerators.xcu",
]

BUNDLE_DIR = "build/bundle"

# Parent Debug submenu in extension/Addons.xcu (M16a–M16f are children).
DEBUG_MENU_NODE_MARKER = 'oor:name="M16"'


def resolve_bundle_path(base_dir, bundle_dir):
    """Return an absolute bundle directory. Absolute *bundle_dir* is used as-is."""
    if os.path.isabs(bundle_dir):
        return bundle_dir
    return os.path.join(base_dir, bundle_dir)


def resolve_output_path(base_dir, output):
    """Return an absolute .oxt path. Absolute *output* is used as-is."""
    if os.path.isabs(output):
        return output
    return os.path.join(base_dir, output)


def _vendor_copy_ignore(_dir: str, names: list[str]) -> list[str]:
    """Skip bytecode when copying vendor packages into plugin/lib/."""
    ignored: list[str] = []
    for name in names:
        if name == "__pycache__":
            ignored.append(name)
        elif name.endswith((".pyc", ".pyo")):
            ignored.append(name)
    return ignored


def sync_vendor_into_lib(vendor_dir: str, lib_dir: str, *, prune_websockets: bool = True) -> int:
    """Copy ``vendor/`` package trees into a ``plugin/lib/`` directory.

    Hot-deploy rsyncs project ``plugin/`` into the LibreOffice cache. The OXT
    bundle already received these wheels, but a stale/incomplete project
    ``plugin/lib/`` would wipe them (``rsync --delete``) and break imports such
    as ``isodate`` — Calc cell tools then fail to register.
    """
    if not os.path.isdir(vendor_dir):
        return 0
    os.makedirs(lib_dir, exist_ok=True)
    vendor_count = 0
    for entry in sorted(os.listdir(vendor_dir)):
        if entry.endswith(".dist-info") or entry.startswith(("_", ".")):
            continue
        src_path = os.path.join(vendor_dir, entry)
        dst_path = os.path.join(lib_dir, entry)
        if os.path.isdir(src_path):
            if os.path.exists(dst_path):
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path, ignore=_vendor_copy_ignore)
            if prune_websockets and entry == "websockets":
                pruned = prune_vendored_websockets(dst_path)
                if pruned:
                    print("  Pruned websockets for CDP client (%d paths)" % len(pruned))
        elif os.path.isfile(src_path):
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)
        vendor_count += 1
    return vendor_count


def should_exclude(path, with_tests=False):
    # When with_tests, allow tests/ at root and plugin/tests/; otherwise exclude them
    path_norm = path.replace("\\", "/")
    if path_norm.startswith("tests/") or path_norm == "tests":
        return not with_tests
    if path_norm.startswith("plugin/tests/") or path_norm == "plugin/tests":
        return not with_tests
    # gettext source/template only; runtime loads .mo (see plugin/framework/i18n.py)
    if (
        path_norm.startswith("locales/")
        or path_norm.startswith("build/generated/locales/")
    ) and (path_norm.endswith(".po") or path_norm.endswith(".pot")):
        return True
    # Dev-only Python logo sources; ship only python_32.png in the OXT.
    if path_norm.endswith("assets/python_logo.svg") or path_norm.endswith("assets/python_logo.NOTICE"):
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


def assemble_bundle(base_dir, modules, no_recording=False, with_tests=False, dry_run_strip=False, strip=False, bundle_dir=BUNDLE_DIR):
    """Copy all files into the bundle dir with final archive paths."""
    bundle_path = resolve_bundle_path(base_dir, bundle_dir)

    # Clean previous bundle
    if os.path.exists(bundle_path):
        shutil.rmtree(bundle_path)

    # Refresh project plugin/lib before collect/hot-deploy so rsync --delete does
    # not strip wheels that only lived in the OXT bundle (see sync_vendor_into_lib).
    vendor_dir = os.path.join(base_dir, "vendor")
    vendor_count = sync_vendor_into_lib(vendor_dir, os.path.join(base_dir, "plugin", "lib"))
    if vendor_count:
        print("Vendored %d packages into plugin/lib/" % vendor_count)

    include = list(ALWAYS_INCLUDE_EXTENSION)
    include.extend(get_always_include_plugin(base_dir))
    if with_tests:
        include.extend(RELEASE_INCLUDE_PLUGIN)
        print("  Dev build: including plugin/tests/ and testing_runner.py")
    include.extend(ALWAYS_INCLUDE_ROOT)

    for mod in modules:
        mod_dir = "plugin/%s/" % mod
        mod_path = os.path.join(base_dir, mod_dir)
        if os.path.isdir(mod_path):
            include.append(mod_dir)
        else:
            print("  WARNING: module '%s' not found at %s" % (mod, mod_dir),
                  file=sys.stderr)

    include.extend(GENERATED_INCLUDES)
    files = collect_files(base_dir, include, with_tests=with_tests)

    if no_recording:
        # Exclude voice recording UI and venv capture modules.
        _recording_prefixes = (
            "plugin/chatbot/audio_recorder.py",
            "plugin/scripting/venv/audio_recorder.py",
            "plugin/scripting/venv/audio_record_main.py",
        )
        files = [f for f in files if f not in _recording_prefixes]
        print("  No-recording build: excluded sidebar voice recording modules")

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

    # Overlay again into the bundle (keeps prune messaging; safe if collect already copied lib/)
    bundle_vendor_count = sync_vendor_into_lib(vendor_dir, os.path.join(bundle_path, "plugin", "lib"))
    if bundle_vendor_count and not vendor_count:
        print("Vendored %d packages into plugin/lib/" % bundle_vendor_count)

    # Release build: strip Debug (test) menu and write Addons.xcu to bundle
    if not with_tests:
        src_addons = os.path.join(base_dir, "extension", "Addons.xcu")
        dst_addons = os.path.join(bundle_path, "Addons.xcu")
        if os.path.isfile(src_addons):
            with open(src_addons, "r", encoding="utf-8") as f:
                content = f.read()
            start = content.find(DEBUG_MENU_NODE_MARKER)
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

    if strip or not with_tests or dry_run_strip:
        strip_production_code(bundle_path, dry_run=dry_run_strip)

    print("Assembled %d files in %s" % (count, bundle_path))
    return count


# strip_production_code moved to scripts/strip_code.py


def zip_bundle(base_dir, output, bundle_dir=BUNDLE_DIR):
    """Zip the bundle directory into the .oxt."""
    bundle_path = resolve_bundle_path(base_dir, bundle_dir)
    if not os.path.isdir(bundle_path):
        print("ERROR: %s not found. Run without --repack first." % bundle_path,
              file=sys.stderr)
        return 1

    output_path = resolve_output_path(base_dir, output)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    count = 0
    # strict_timestamps=False: freshly vendored wheels and reused Cloud Agent
    # snapshot checkouts can carry pre-1980 (epoch-0) mtimes, which the default
    # rejects with "ZIP does not support timestamps before 1980" and aborts the
    # build. False clamps such entries to 1980-01-01 instead of failing.
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
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
        help="Output file (default: build/writeragent.oxt)")
    parser.add_argument(
        "--repack", action="store_true",
        help="Only re-zip the bundle dir (skip assembly; see --bundle-dir)")
    parser.add_argument(
        "--no-recording", action="store_true",
        help="Exclude voice recording: sidebar Record button and venv capture modules")
    parser.add_argument(
        "--no-tests", action="store_true",
        help="Exclude plugin/tests/ and testing_runner.py (for release builds)")
    parser.add_argument(
        "--dry-run-strip", action="store_true",
        help="Show what code would be stripped without actually removing it")
    parser.add_argument(
        "--strip", action="store_true",
        help="Force stripping debug/obs code even if tests are included")
    parser.add_argument(
        "--bundle-dir", default=BUNDLE_DIR,
        help="Bundle directory (default: build/bundle). Absolute path is used as-is.")
    parser.add_argument(
        "--skip-zip", action="store_true",
        help="Assemble the bundle only; do not write an .oxt")
    args = parser.parse_args()

    if not args.repack:
        modules = args.modules or _discover_modules(PROJECT_ROOT)
        assemble_bundle(
            PROJECT_ROOT, modules,
            no_recording=args.no_recording,
            with_tests=not args.no_tests,
            dry_run_strip=args.dry_run_strip,
            strip=args.strip,
            bundle_dir=args.bundle_dir,
        )

    if args.skip_zip:
        return 0
    return zip_bundle(PROJECT_ROOT, args.output, bundle_dir=args.bundle_dir)


if __name__ == "__main__":
    sys.exit(main())
