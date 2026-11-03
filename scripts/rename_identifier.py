#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rename a Python identifier, or retarget ``from … import`` names, across ``.py`` files.

Identifier mode uses word-boundary substitution so ``openpyxl`` / ``xlrd`` / ``_xlws``
stay intact. Optional ``--string-attr-prefix`` is implied by the same ``old.`` → ``new.``
rule (covered by rewriting ``old`` before a following ``.`` via the identifier pattern).

Import-retarget mode (``--import-from`` / ``--import-to`` / ``--names``) rewrites
``from <from> import …`` via stdlib ``ast``: exclusive statements change module;
mixed statements split (moved names → ``--import-to``, rest stay). Also rewrites
the same exclusive import text inside string literals (generated source).

Does **not** rewrite markdown; update docs separately.

Examples::

  python scripts/rename_identifier.py xl calc --dry-run --paths plugin/framework/constants.py

  python scripts/rename_identifier.py xl calc --paths-file /tmp/files.txt

  python scripts/rename_identifier.py \\
    --import-from plugin.doc.document_helpers \\
    --import-to plugin.framework.doc_type \\
    --names is_writer is_calc \\
    --paths plugin tests

  # Paths file format: one repo-relative path per line; ``#`` comments allowed.
  # ``--paths`` may be files or directories (walked for ``*.py``).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Lines mentioning these stay untouched (third-party / Excel package tokens).
_SKIP_LINE_MARKERS = ("openpyxl", "xlrd", "xlwt", "xlsxwriter", "_xlws", "xlfn")


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p.resolve()


def collect_py_paths(raw_paths: list[Path]) -> list[Path]:
    """Expand files and directories into ``*.py`` paths (skip ``__pycache__``)."""
    out: list[Path] = []
    seen: set[Path] = set()
    for path in raw_paths:
        if path.is_file():
            if path.suffix != ".py":
                print(f"SKIP (not .py): {path}", file=sys.stderr)
                continue
            if path not in seen:
                seen.add(path)
                out.append(path)
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*.py")):
                if "__pycache__" in child.parts:
                    continue
                # Fixtures in this file are the old import text on purpose.
                if child.name == "test_rename_identifier.py":
                    continue
                if child not in seen:
                    seen.add(child)
                    out.append(child)
            continue
        print(f"MISSING: {path}", file=sys.stderr)
    return out


def rewrite_text(original: str, old: str, new: str) -> str | None:
    """Return rewritten text, or None if unchanged.

    Identifier rule: ``old`` not adjacent to other identifier chars.
    Also rewrites ``old.`` attr/string prefixes the same way (``xl.foo`` → ``calc.foo``).
    """
    ident_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])")
    out: list[str] = []
    changed = False
    for line in original.splitlines(keepends=True):
        if any(m in line for m in _SKIP_LINE_MARKERS):
            out.append(line)
            continue
        replaced = ident_re.sub(new, line)
        if replaced != line:
            changed = True
        out.append(replaced)
    if not changed:
        return None
    return "".join(out)


def _alias_name(alias: ast.alias) -> str:
    return alias.name


def _format_from_import(module: str, aliases: list[ast.alias], indent: str) -> str:
    parts: list[str] = []
    for alias in aliases:
        if alias.asname:
            parts.append(f"{alias.name} as {alias.asname}")
        else:
            parts.append(alias.name)
    return f"{indent}from {module} import {', '.join(parts)}\n"


def _statement_span(source: str, node: ast.stmt) -> tuple[int, int]:
    """Return ``[start, end)`` character offsets for *node* (includes leading indent)."""
    lines = source.splitlines(keepends=True)
    start_line = node.lineno - 1
    end_line = (node.end_lineno or node.lineno) - 1
    start = sum(len(lines[i]) for i in range(start_line))
    # Include indent: start at beginning of the first line.
    end = sum(len(lines[i]) for i in range(end_line + 1))
    return start, end


def _line_indent(source: str, lineno: int) -> str:
    lines = source.splitlines(keepends=True)
    line = lines[lineno - 1]
    return line[: len(line) - len(line.lstrip())]


def rewrite_import_from(
    original: str,
    from_module: str,
    to_module: str,
    names: frozenset[str],
) -> str | None:
    """Retarget ``from from_module import …`` names listed in *names*.

    Exclusive statements change module. Mixed statements split into two imports.
    Also rewrites exclusive import text inside string literals.
    """
    if from_module == to_module or not names:
        return None
    try:
        tree = ast.parse(original)
    except SyntaxError:
        return _rewrite_import_strings(original, from_module, to_module, names)

    replacements: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level != 0 or node.module != from_module or not node.names:
            continue
        moved = [a for a in node.names if _alias_name(a) in names]
        kept = [a for a in node.names if _alias_name(a) not in names]
        if not moved:
            continue
        start, end = _statement_span(original, node)
        indent = _line_indent(original, node.lineno)
        if not kept:
            stmt_text = original[start:end]
            new_stmt = stmt_text.replace(
                f"from {from_module} import",
                f"from {to_module} import",
                1,
            )
            if not new_stmt.endswith("\n") and original[end - 1 : end] == "\n":
                new_stmt += "\n"
            replacements.append((start, end, new_stmt))
            continue
        new_block = _format_from_import(to_module, moved, indent) + _format_from_import(
            from_module, kept, indent
        )
        replacements.append((start, end, new_block))

    text = original
    if replacements:
        replacements.sort(key=lambda item: item[0], reverse=True)
        for start, end, new_stmt in replacements:
            text = text[:start] + new_stmt + text[end:]

    string_updated = _rewrite_import_strings(text, from_module, to_module, names)
    if string_updated is not None:
        text = string_updated
    if text == original:
        return None
    return text


_IMPORT_IN_TEXT_RE = re.compile(
    r"from (?P<mod>[A-Za-z_][A-Za-z0-9_.]*) import (?P<names>[A-Za-z0-9_, ]+)"
)


def _imported_root_names(names_blob: str) -> list[str]:
    out: list[str] = []
    for part in names_blob.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(part.split()[0])
    return out


def _rewrite_import_strings(
    original: str,
    from_module: str,
    to_module: str,
    names: frozenset[str],
) -> str | None:
    """Rewrite exclusive ``from <from> import …`` text (real imports already handled, plus strings)."""

    def repl(match: re.Match[str]) -> str:
        if match.group("mod") != from_module:
            return match.group(0)
        imported = _imported_root_names(match.group("names"))
        if imported and all(name in names for name in imported):
            return f"from {to_module} import {match.group('names')}"
        return match.group(0)

    updated = _IMPORT_IN_TEXT_RE.sub(repl, original)
    if updated == original:
        return None
    return updated


def process_file(path: Path, old: str, new: str, *, dry_run: bool) -> bool:
    original = path.read_text(encoding="utf-8")
    updated = rewrite_text(original, old, new)
    if updated is None:
        return False
    rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
    if dry_run:
        print(f"WOULD UPDATE: {rel}")
        return True
    path.write_text(updated, encoding="utf-8")
    print(f"UPDATED: {rel}")
    return True


def process_import_file(
    path: Path,
    from_module: str,
    to_module: str,
    names: frozenset[str],
    *,
    dry_run: bool,
) -> bool:
    original = path.read_text(encoding="utf-8")
    updated = rewrite_import_from(original, from_module, to_module, names)
    if updated is None:
        return False
    rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
    if dry_run:
        print(f"WOULD UPDATE: {rel}")
        return True
    path.write_text(updated, encoding="utf-8")
    print(f"UPDATED: {rel}")
    return True


def _collect_cli_paths(args: argparse.Namespace) -> list[Path]:
    raw: list[Path] = [_resolve_path(p) for p in args.paths]
    if args.paths_file:
        for line in args.paths_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(_resolve_path(line))
    return collect_py_paths(raw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", nargs="?", help="Old identifier (e.g. xl)")
    ap.add_argument("new", nargs="?", help="New identifier (e.g. calc)")
    ap.add_argument("--import-from", dest="import_from", help="Existing import module (import-retarget mode)")
    ap.add_argument("--import-to", dest="import_to", help="New import module (import-retarget mode)")
    ap.add_argument("--names", nargs="+", default=[], help="Names to move in import-retarget mode")
    ap.add_argument("--paths", nargs="*", default=[], help="Files or directories relative to repo root")
    ap.add_argument("--paths-file", type=Path, help="File with one path per line")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    paths = _collect_cli_paths(args)
    if not paths:
        print("No paths given", file=sys.stderr)
        return 2

    if args.import_from or args.import_to or args.names:
        if not args.import_from or not args.import_to or not args.names:
            print("--import-from, --import-to, and --names are required together", file=sys.stderr)
            return 2
        if args.import_from == args.import_to:
            print("--import-from and --import-to are identical", file=sys.stderr)
            return 2
        names = frozenset(args.names)
        n = 0
        for path in paths:
            if process_import_file(
                path,
                args.import_from,
                args.import_to,
                names,
                dry_run=args.dry_run,
            ):
                n += 1
        print(f"{'Would update' if args.dry_run else 'Updated'} {n} file(s)")
        return 0

    if not args.old or not args.new:
        print("old/new identifiers required unless using --import-from", file=sys.stderr)
        return 2
    if not args.old.isidentifier() or not args.new.isidentifier():
        print("old/new must be valid Python identifiers", file=sys.stderr)
        return 2
    if args.old == args.new:
        print("old and new are identical", file=sys.stderr)
        return 2

    n = 0
    for path in paths:
        if process_file(path, args.old, args.new, dry_run=args.dry_run):
            n += 1
    print(f"{'Would update' if args.dry_run else 'Updated'} {n} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
