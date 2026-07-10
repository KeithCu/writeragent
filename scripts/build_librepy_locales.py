#!/usr/bin/env python3
"""Build slim gettext catalogs for the LibrePy OXT (strings in the core bundle only).

Writes build/generated/librepy.pot and filtered locale trees under
build/generated/locales/<lang>/LC_MESSAGES/writeragent.{po,mo}.

Run via ``make compile-translations-core`` (part of ``make build-core``).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import polib

from scripts.build_librepy_oxt import LIBREPY_DIALOG_FILES
from scripts.generate_manifest import _filter_librepy_config
from scripts.librepy_bundle_paths import collect_librepy_plugin_paths

LIBREPY_POT = os.path.join(PROJECT_ROOT, "build", "generated", "librepy.pot")
LIBREPY_LOCALES_OUT = os.path.join(PROJECT_ROOT, "build", "generated", "locales")
SOURCE_LOCALES = os.path.join(PROJECT_ROOT, "locales")
XDL_STUB = os.path.join(PROJECT_ROOT, "plugin", "xdl_strings_librepy.py")

_LIBREPY_MODULE_YAMLS = (
    os.path.join(PROJECT_ROOT, "plugin", "scripting", "module.yaml"),
    os.path.join(PROJECT_ROOT, "plugin", "vision", "module.yaml"),
)

_XDL_ATTR_PATTERNS = (
    r'dlg:value="([^"]+)"',
    r'dlg:title="([^"]+)"',
    r'dlg:label="([^"]+)"',
    r'dlg:stringitem="([^"]+)"',
)


def _xdl_paths() -> list[str]:
    paths: list[str] = []
    for rel in LIBREPY_DIALOG_FILES:
        if rel.endswith(".xdl"):
            paths.append(os.path.join(PROJECT_ROOT, rel))
    for pattern in (
        os.path.join(PROJECT_ROOT, "build", "generated", "WriterAgentDialogs", "*.xdl"),
        os.path.join(PROJECT_ROOT, "build", "generated", "dialogs", "*.xdl"),
    ):
        paths.extend(sorted(glob.glob(pattern)))
    return [p for p in paths if os.path.isfile(p)]


def _extract_xdl_strings(xdl_files: list[str]) -> set[str]:
    strings: set[str] = set()
    for filepath in xdl_files:
        try:
            with open(filepath, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            print(f"Warning: could not read {filepath}: {exc}", file=sys.stderr)
            continue
        for pattern in _XDL_ATTR_PATTERNS:
            for match in re.findall(pattern, content):
                if not match.isdigit() and match.strip():
                    strings.add(match)
    return strings


def _write_xdl_stub(strings: set[str]) -> None:
    with open(XDL_STUB, "w", encoding="utf-8") as fh:
        fh.write("# Auto-generated for LibrePy xgettext extraction. Do not commit.\n\n")
        for text in sorted(strings):
            escaped = text.replace('"', '\\"')
            fh.write(f'_("{escaped}")\n')


def _merge_librepy_yaml_into_pot(pot_path: str) -> int:
    """Add scripting/vision module.yaml strings (librepy_exclude keys omitted)."""
    import yaml

    msgids: list[str] = []
    for ypath in _LIBREPY_MODULE_YAMLS:
        if not os.path.isfile(ypath):
            print(f"Warning: missing module.yaml: {ypath}", file=sys.stderr)
            continue
        with open(ypath, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            continue
        if "config" in data and isinstance(data["config"], dict):
            data = dict(data)
            data["config"] = _filter_librepy_config(data["config"])
        msgids.extend(_collect_strings_from_module_yaml_from_data(data))

    pot = polib.pofile(pot_path)
    existing = {e.msgid for e in pot}
    added = 0
    for msgid in msgids:
        if not msgid or msgid in existing:
            continue
        pot.append(polib.POEntry(msgid=msgid, msgstr=""))
        existing.add(msgid)
        added += 1
    pot.save(pot_path)
    return added


def _collect_strings_from_module_yaml_from_data(data: dict) -> list[str]:
    """Like merge_module_yaml_into_pot._collect_strings_from_module_yaml but from dict."""
    results: list[str] = []
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        results.append(title.strip())
    config = data.get("config")
    if not isinstance(config, dict):
        return results
    for _, schema in config.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("internal"):
            continue
        for key in ("label", "helper"):
            val = schema.get(key)
            if isinstance(val, str) and val.strip():
                results.append(val.strip())
        opts = schema.get("options")
        if isinstance(opts, list):
            for opt in opts:
                if isinstance(opt, dict):
                    lab = opt.get("label")
                    if isinstance(lab, str) and lab.strip():
                        results.append(lab.strip())
    return results


def _run_xgettext(py_files: list[str], pot_path: str) -> None:
    os.makedirs(os.path.dirname(pot_path), exist_ok=True)
    cmd = [
        "xgettext",
        "--add-location=file",
        "-d",
        "writeragent",
        "-o",
        pot_path,
        *py_files,
    ]
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)


def _pot_msgids(pot_path: str) -> set[str]:
    pot = polib.pofile(pot_path)
    return {e.msgid for e in pot if e.msgid}


def _filter_and_compile_locales(allow_msgids: set[str]) -> int:
    if not os.path.isdir(SOURCE_LOCALES):
        print(f"error: source locales dir not found: {SOURCE_LOCALES}", file=sys.stderr)
        return 1

    if os.path.isdir(LIBREPY_LOCALES_OUT):
        shutil.rmtree(LIBREPY_LOCALES_OUT)

    compiled = 0
    for lang in sorted(os.listdir(SOURCE_LOCALES)):
        po_src = os.path.join(SOURCE_LOCALES, lang, "LC_MESSAGES", "writeragent.po")
        if not os.path.isfile(po_src):
            continue
        src_po = polib.pofile(po_src)
        out_po = polib.POFile()
        out_po.metadata = dict(src_po.metadata)
        for entry in src_po:
            if entry.msgid == "" or entry.msgid in allow_msgids:
                out_po.append(entry)

        out_dir = os.path.join(LIBREPY_LOCALES_OUT, lang, "LC_MESSAGES")
        os.makedirs(out_dir, exist_ok=True)
        po_out = os.path.join(out_dir, "writeragent.po")
        mo_out = os.path.join(out_dir, "writeragent.mo")
        out_po.save(po_out)
        subprocess.run(["msgfmt", "-o", mo_out, po_out], check=True)
        compiled += 1

    return compiled


def build_librepy_locales() -> int:
    if not shutil.which("xgettext") or not shutil.which("msgfmt"):
        print(
            "error: xgettext and msgfmt required (install gettext)",
            file=sys.stderr,
        )
        return 1

    py_files = [
        os.path.join(PROJECT_ROOT, rel)
        for rel in collect_librepy_plugin_paths(PROJECT_ROOT)
        if rel.endswith(".py")
    ]
    xdl_files = _xdl_paths()
    xdl_strings = _extract_xdl_strings(xdl_files)
    stub_written = False
    if xdl_strings:
        _write_xdl_stub(xdl_strings)
        py_files.append(XDL_STUB)
        stub_written = True

    try:
        _run_xgettext(py_files, LIBREPY_POT)
        yaml_added = _merge_librepy_yaml_into_pot(LIBREPY_POT)
        allow = _pot_msgids(LIBREPY_POT)
        locale_count = _filter_and_compile_locales(allow)
    finally:
        if stub_written and os.path.isfile(XDL_STUB):
            os.remove(XDL_STUB)

    pot_count = len(allow)
    print(
        "build_librepy_locales: %s (%d msgids, +%d from YAML, %d locales)"
        % (LIBREPY_POT, pot_count, yaml_added, locale_count)
    )
    if locale_count == 0:
        print("error: no locale catalogs compiled", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return build_librepy_locales()


if __name__ == "__main__":
    sys.exit(main())
