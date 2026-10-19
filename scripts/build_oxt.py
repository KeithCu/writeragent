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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """Dynamically discover all subdirectories under plugin/ except tests, cache, and lib."""
    plugin_dir = os.path.join(base_dir, "plugin")
    includes = list(ALWAYS_INCLUDE_PLUGIN_FILES)
    if os.path.isdir(plugin_dir):
        for entry in sorted(os.listdir(plugin_dir)):
            if entry in ("tests", "__pycache__", "lib") or entry.startswith("."):
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
]

BUNDLE_DIR = "build/bundle"

# Parent Debug submenu in extension/Addons.xcu (M17a–M17e are children).
DEBUG_MENU_NODE_MARKER = 'oor:name="M17"'


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


def sync_vendor_into_lib(
    vendor_dir: str,
    lib_dir: str,
    *,
    prune_websockets: bool = True,
    only: set[str] | frozenset[str] | None = None,
) -> int:
    """Copy ``vendor/`` package trees into a ``plugin/lib/`` directory.

    Hot-deploy rsyncs project ``plugin/`` into the LibreOffice cache. The OXT
    bundle already received these wheels, but a stale/incomplete project
    ``plugin/lib/`` would wipe them (``rsync --delete``) and break imports such
    as ``isodate`` — Calc cell tools then fail to register.
    """
    if not os.path.isdir(vendor_dir):
        return 0

    entries = sorted(os.listdir(vendor_dir))
    packages_to_copy: list[str] = []
    for entry in entries:
        if entry.startswith(("_", ".")) or entry == "bin" or entry.endswith(".dist-info"):
            continue
        if only is not None and entry not in only:
            continue
        packages_to_copy.append(entry)

    if not packages_to_copy and not os.path.isdir(lib_dir):
        return 0

    os.makedirs(lib_dir, exist_ok=True)
    vendor_count = 0
    written: set[str] = set()
    copied_pkgs: set[str] = set()

    for entry in packages_to_copy:
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
        written.add(entry)
        copied_pkgs.add(entry)

    # Copy METADATA-only *.dist-info: latex2mathml reads its version via importlib.metadata at import.
    for entry in entries:
        if not entry.endswith(".dist-info") or entry.startswith(("_", ".")):
            continue
        stem = entry[:-len(".dist-info")].split("-")[0]
        if stem in copied_pkgs or stem.replace("-", "_") in copied_pkgs:
            src_path = os.path.join(vendor_dir, entry)
            dst_path = os.path.join(lib_dir, entry)
            meta_src = os.path.join(src_path, "METADATA")
            if os.path.isfile(meta_src):
                if os.path.exists(dst_path):
                    shutil.rmtree(dst_path)
                os.makedirs(dst_path)
                shutil.copy2(meta_src, os.path.join(dst_path, "METADATA"))
                written.add(entry)

    # Prune stale packages so deleted vendor dependencies don't survive in lib_dir:
    # Windows cache copy has no vendor overlay and Linux overlay skips *.dist-info.
    if os.path.isdir(lib_dir):
        for existing in os.listdir(lib_dir):
            if existing not in written:
                existing_path = os.path.join(lib_dir, existing)
                if os.path.isdir(existing_path):
                    shutil.rmtree(existing_path)
                else:
                    os.remove(existing_path)

    return vendor_count


def should_exclude(path, with_tests=False):
    # When with_tests, allow tests/ at root and plugin/tests/; otherwise exclude them
    path_norm = path.replace("\\", "/")
    if path_norm.startswith("tests/") or path_norm == "tests":
        return not with_tests
    if path_norm.startswith("plugin/tests/") or path_norm == "plugin/tests":
        return not with_tests
    # Mock-LLM sidebar drivers (press_send / set_query_text). Dev OXTs keep them.
    if path_norm.endswith("plugin/chatbot/sidebar_test_hooks.py"):
        return not with_tests
    # gettext source/template only; runtime loads .mo (see plugin/framework/i18n.py)
    if (
        path_norm.startswith("locales/")
        or path_norm.startswith("build/generated/locales/")
    ) and (path_norm.endswith(".po") or path_norm.endswith(".pot")):
        return True
    # Dev-only logo sources; ship only the 32 px PNGs used by menus in the OXT.
    if path_norm.endswith("assets/python_logo.svg") or path_norm.endswith("assets/python_logo.NOTICE"):
        return True
    if path_norm.endswith("assets/jupyter_logo.svg") or path_norm.endswith("assets/jupyter_logo.NOTICE"):
        return True
    # Attribution only; 48 px PNGs ship. Buttons do not scale graphics (see dialog_views).
    if path_norm.endswith("assets/provider_logos.NOTICE"):
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


def resolve_archive_sources(
    base_dir: str,
    files: list[str],
    remap_fn: "Callable[[str], str]" = remap_path,
) -> list[tuple[str, str]]:
    """Map source files to archive paths, collapsing identical duplicates and exiting on differing collisions."""
    resolved: dict[str, str] = {}
    for rel in files:
        arcname = remap_fn(rel)
        if arcname in resolved:
            prev_rel = resolved[arcname]
            if prev_rel == rel:
                continue
            src_prev = prev_rel if os.path.isabs(prev_rel) else os.path.join(base_dir, prev_rel)
            src_curr = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
            if os.path.isfile(src_prev) and os.path.isfile(src_curr):
                with open(src_prev, "rb") as f1, open(src_curr, "rb") as f2:
                    if f1.read() == f2.read():
                        continue
            elif rel.endswith("/") and prev_rel.endswith("/"):
                continue
            print(
                f"ERROR: Archive collision on '{arcname}': '{prev_rel}' and '{rel}' differ in content",
                file=sys.stderr,
            )
            sys.exit(1)
        resolved[arcname] = rel
    return [(src_rel, arcname) for arcname, src_rel in resolved.items()]


def strip_debug_menu_xml(content: str) -> str:
    """Remove the M17 Debug submenu node and its children from Addons.xcu content."""
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
    return content


def preflight_writeragent(base_dir: str) -> None:
    # Fail fast when required inputs or generated files are missing.
    # Hot-deploy depends on these: Windows cache copy has no vendor overlay and Linux overlay skips *.dist-info.
    manifest_files = [
        "plugin/_manifest.py",
        "plugin/main.py",
        "extension/META-INF/manifest.xml",
        "extension/description.xml",
        "extension/Addons.xcu",
        "extension/Accelerators.xcu",
        "extension/Jobs.xcu",
        "extension/ProtocolHandler.xcu",
        "extension/XPythonFunction.rdb",
        "extension/XPromptFunction.rdb",
        "build/generated/Dialogs/SettingsDialog.xdl",
    ]
    for rel in manifest_files:
        full = os.path.join(base_dir, rel)
        if not os.path.isfile(full):
            raise RuntimeError(f"Missing required input '{rel}'. Run: make manifest")

    locales_dir = os.path.join(base_dir, "locales")
    has_mo = False
    if os.path.isdir(locales_dir):
        for root, _, filenames in os.walk(locales_dir):
            if any(f.endswith(".mo") for f in filenames):
                has_mo = True
                break
    if not has_mo:
        raise RuntimeError("Missing compiled translations (locales/*/*.mo). Run: make compile-translations")

    vendor_dir = os.path.join(base_dir, "vendor")
    required_vendor_pkgs = [
        "isodate",
        "json_repair",
        "latex2mathml",
        "mathml_to_latex",
        "snowballstemmer",
        "websockets",
        "defusedxml",
    ]
    for pkg in required_vendor_pkgs:
        full = os.path.join(vendor_dir, pkg)
        if not os.path.isdir(full):
            raise RuntimeError(f"Missing vendor package directory '{pkg}'. Run: make vendor")

    has_l2m_meta = False
    if os.path.isdir(vendor_dir):
        for entry in os.listdir(vendor_dir):
            if entry.startswith("latex2mathml-") and entry.endswith(".dist-info"):
                if os.path.isfile(os.path.join(vendor_dir, entry, "METADATA")):
                    has_l2m_meta = True
                    break
    if not has_l2m_meta:
        raise RuntimeError("Missing latex2mathml-*.dist-info/METADATA. Run: make vendor")


def verify_bundle_manifest(bundle_path: str, *, require_addons: bool = True) -> None:
    """Verify that all files declared in META-INF/manifest.xml exist in bundle_path."""
    manifest_xml = os.path.join(bundle_path, "META-INF", "manifest.xml")
    if not os.path.isfile(manifest_xml):
        raise RuntimeError(f"Missing META-INF/manifest.xml in {bundle_path}")

    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(manifest_xml)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse {manifest_xml}: {exc}") from exc

    root = tree.getroot()
    ns = {"manifest": "http://openoffice.org/2001/manifest"}
    for elem in root.findall(".//manifest:file-entry", ns):
        full_path = elem.attrib.get(f"{{{ns['manifest']}}}full-path")
        if not full_path:
            full_path = elem.attrib.get("manifest:full-path") or elem.attrib.get("full-path")
        if not full_path or full_path in ("/", ""):
            continue
        entry_file = os.path.join(bundle_path, full_path.replace("/", os.sep))
        if not os.path.exists(entry_file):
            raise RuntimeError(f"Bundle manifest points at missing file: {full_path}")

    manifest_py = os.path.join(bundle_path, "plugin", "_manifest.py")
    if not os.path.isfile(manifest_py):
        raise RuntimeError(f"Bundle missing required plugin/_manifest.py: {bundle_path}")

    if require_addons:
        addons_xcu = os.path.join(bundle_path, "Addons.xcu")
        if not os.path.isfile(addons_xcu):
            raise RuntimeError(f"Bundle missing required Addons.xcu: {bundle_path}")


def _get_source_date_epoch_dt() -> tuple[int, int, int, int, int, int]:
    epoch_str = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch_str:
        try:
            import datetime
            dt = datetime.datetime.fromtimestamp(int(epoch_str), tz=datetime.timezone.utc)
            if dt.year >= 1980:
                return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        except (ValueError, OverflowError, OSError):
            pass
    return (1980, 1, 1, 0, 0, 0)


def write_oxt_zip(bundle_path: str, output_path: str) -> int:
    """Write an .oxt zip archive atomically with deterministic entry order and timestamps."""
    if not os.path.isdir(bundle_path):
        raise RuntimeError(f"Bundle directory not found: {bundle_path}")

    entries: list[tuple[str, str]] = []
    for root, dirs, filenames in os.walk(bundle_path):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            filepath = os.path.join(root, fn)
            arcname = os.path.relpath(filepath, bundle_path).replace(os.sep, "/")
            entries.append((arcname, filepath))

    entries.sort(key=lambda item: item[0])
    fixed_dt = _get_source_date_epoch_dt()

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = f"{output_path}.{os.getpid()}.tmp"

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, filepath in entries:
                with open(filepath, "rb") as fh:
                    data = fh.read()
                zinfo = zipfile.ZipInfo(filename=arcname, date_time=fixed_dt)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                st = os.stat(filepath)
                zinfo.external_attr = (st.st_mode & 0xFFFF) << 16
                zf.writestr(zinfo, data)
        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return len(entries)


def assemble_bundle(
    base_dir,
    modules=None,
    no_recording=False,
    with_tests=False,
    dry_run_strip=False,
    strip=False,
    bundle_dir=BUNDLE_DIR,
):
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

    sources = resolve_archive_sources(base_dir, files, remap_path)
    count = 0
    for f, arcname in sources:
        src = f if os.path.isabs(f) else os.path.join(base_dir, f)
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
            with open(src_addons, "r", encoding="utf-8") as fh:
                content = fh.read()
            stripped_content = strip_debug_menu_xml(content)
            with open(dst_addons, "w", encoding="utf-8") as fh:
                fh.write(stripped_content)
            print("  Release build: stripped Debug menu from Addons.xcu")

    if strip or not with_tests or dry_run_strip:
        strip_production_code(bundle_path, dry_run=dry_run_strip)

    print("Assembled %d files in %s" % (count, bundle_path))
    return count


def zip_bundle(base_dir, output, bundle_dir=BUNDLE_DIR):
    """Zip the bundle directory into the .oxt."""
    bundle_path = resolve_bundle_path(base_dir, bundle_dir)
    if not os.path.isdir(bundle_path):
        print("ERROR: %s not found. Run without --repack first." % bundle_path,
              file=sys.stderr)
        return 1

    try:
        verify_bundle_manifest(bundle_path, require_addons=True)
    except RuntimeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    output_path = resolve_output_path(base_dir, output)
    try:
        count = write_oxt_zip(bundle_path, output_path)
    except RuntimeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print("Created %s with %d files" % (output, count))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Build WriterAgent .oxt extension")
    parser.add_argument(
        "--output", default="build/WriterAgent.oxt",
        help="Output file (default: build/WriterAgent.oxt)")
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

    bundle_path = resolve_bundle_path(PROJECT_ROOT, args.bundle_dir)

    if not args.repack:
        try:
            preflight_writeragent(PROJECT_ROOT)
        except RuntimeError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1

        assemble_bundle(
            PROJECT_ROOT,
            no_recording=args.no_recording,
            with_tests=not args.no_tests,
            dry_run_strip=args.dry_run_strip,
            strip=args.strip,
            bundle_dir=args.bundle_dir,
        )
        try:
            verify_bundle_manifest(bundle_path, require_addons=True)
        except RuntimeError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1

    if args.skip_zip:
        return 0
    return zip_bundle(PROJECT_ROOT, args.output, bundle_dir=args.bundle_dir)


if __name__ == "__main__":
    sys.exit(main())
