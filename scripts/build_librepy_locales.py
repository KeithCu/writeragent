#!/usr/bin/env python3
"""Build slim gettext catalogs for the LibrePy OXT (strings in the core bundle only).

Writes build/generated/librepy.pot and filtered locale trees under
build/generated/locales/<lang>/LC_MESSAGES/writeragent.{po,mo}.

Run via ``make compile-translations-core`` (part of ``make build-core``).

GNU ``xgettext`` / ``msgfmt`` are not required. Windows PR CI installs
gettext via chocolatey with ``|| true`` and does not put those tools on
PATH (same reason ``compile_translations.py`` uses polib — GHA 33453184665
/ 33780509372). Extract ``_()`` with ast and compile ``.mo`` with polib.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import polib

from scripts.build_librepy_oxt import LIBREPY_DIALOG_FILES
from scripts.generate_manifest import _filter_librepy_config
from scripts.librepy_bundle_paths import collect_librepy_plugin_paths

from scripts.compile_translations import compile_po

LIBREPY_POT = os.path.join(PROJECT_ROOT, "build", "generated", "librepy.pot")
LIBREPY_LOCALES_OUT = os.path.join(PROJECT_ROOT, "build", "generated", "locales")
SOURCE_LOCALES = os.path.join(PROJECT_ROOT, "locales")

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
        os.path.join(PROJECT_ROOT, "build", "generated", "Dialogs", "*.xdl"),
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


def _joined_string_constant(node: ast.AST) -> str | None:
    """Return a static string from a Constant or implicit ``"a" "b"`` join."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _joined_string_constant(node.left)
        right = _joined_string_constant(node.right)
        if left is None or right is None:
            return None
        return left + right
    values = getattr(node, "values", None)
    if values:
        for item in values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            else:
                return None
        if parts:
            return "".join(parts)
    return None


def _extract_python_msgids(py_files: list[str]) -> set[str]:
    """Collect ``_()`` string arguments. Same keyword xgettext used by default."""
    msgids: set[str] = set()
    for filepath in py_files:
        try:
            with open(filepath, encoding="utf-8") as fh:
                source = fh.read()
        except OSError as exc:
            print(f"Warning: could not read {filepath}: {exc}", file=sys.stderr)
            continue
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError as exc:
            print(f"Warning: could not parse {filepath}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "_"):
                continue
            if not node.args:
                continue
            msgid = _joined_string_constant(node.args[0])
            if msgid:
                msgids.add(msgid)
    return msgids


def _write_pot(msgids: set[str], pot_path: str) -> None:
    pot = polib.POFile()
    pot.metadata = {
        "Content-Type": "text/plain; charset=UTF-8",
        "Language": "",
    }
    for msgid in sorted(msgids):
        pot.append(polib.POEntry(msgid=msgid, msgstr=""))
    os.makedirs(os.path.dirname(pot_path), exist_ok=True)
    pot.save(pot_path)


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
        compile_po(Path(po_out), Path(mo_out))
        compiled += 1

    return compiled


def build_librepy_locales() -> int:
    py_files = [
        os.path.join(PROJECT_ROOT, rel)
        for rel in collect_librepy_plugin_paths(PROJECT_ROOT)
        if rel.endswith(".py")
    ]
    xdl_strings = _extract_xdl_strings(_xdl_paths())
    py_msgids = _extract_python_msgids(py_files)
    _write_pot(py_msgids | xdl_strings, LIBREPY_POT)
    yaml_added = _merge_librepy_yaml_into_pot(LIBREPY_POT)
    allow = _pot_msgids(LIBREPY_POT)
    locale_count = _filter_and_compile_locales(allow)

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
