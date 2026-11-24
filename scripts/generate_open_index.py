#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Collect unfinished-work bullets from ``## Open`` sections under ``docs/``.

Writes ``docs/OPEN.md`` (tables per kind) and ``docs/OPEN.csv``. Only ``OPEN.md``
is tracked in git; the CSV is a local copy for spreadsheets. A section is
any heading whose text is exactly ``Open``; it ends at the next heading of the
same or higher level, so bullets under a deeper sub-heading are still
collected. Headings inside fenced code blocks are ignored. Each top-level
bullet is one item; wrapped lines are joined, nested bullets dropped and
ticked ``[x]`` bullets skipped. A
leading backtick block such as ``[feature M many post-v1]`` carries tags
(``few`` or ``many`` is a rough guess of how many users want the item).
``--check`` writes nothing and exits 1 when ``OPEN.md`` is missing or stale.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path
from typing import NamedTuple

KINDS = ("feature", "fidelity", "eng", "idea")
SIZES = ("S", "M", "L")
WHENS = ("v1", "post-v1")
# Marks an item nobody has confirmed against the tree as still missing.
UNVERIFIED = "unverified"
# Rough guess of how many users want the item: a niche few or many.
USERS = ("few", "many")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
BULLET = re.compile(r"^([-*])\s+(.*)$")
TAGS = re.compile(r"^`\[([^\]]*)\]`\s*(.*)$")


class Item(NamedTuple):
    kind: str
    size: str
    when: str
    text: str
    source: str  # path relative to the docs directory, POSIX style
    unverified: str = ""
    users: str = ""


def _split_tags(first_line: str, source: str, problems: list[str]) -> tuple[str, str, str, str, str, str]:
    """Return (kind, size, when, unverified, users, text) for a bullet's joined text."""
    match = TAGS.match(first_line)
    if not match:
        return "", "", "", "", "", first_line
    kind = size = when = unverified = users = ""
    for word in match.group(1).split():
        if word in KINDS:
            kind = word
        elif word in SIZES:
            size = word
        elif word in WHENS:
            when = word
        elif word == UNVERIFIED:
            unverified = word
        elif word in USERS:
            users = word
        else:
            problems.append(f"{source}: unrecognised tag '{word}'")
    return kind, size, when, unverified, users, match.group(2)


def parse_file(path: Path, source: str, problems: list[str]) -> list[Item] | None:
    """Return the items of the file's Open section, or None if it has none."""
    items: list[Item] = []
    found = False
    open_level = 0
    in_fence = False
    current: list[str] = []
    nested = False

    def flush() -> None:
        if current:
            kind, size, when, unverified, users, text = _split_tags(" ".join(current), source, problems)
            # A ticked checkbox is finished work kept for the record, so it is not listed.
            if not text.startswith("[x]"):
                items.append(Item(kind, size, when, text, source, unverified, users))
            current.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        if in_fence:
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if open_level and level <= open_level:
                flush()
                open_level = 0
            if not open_level and heading.group(2) == "Open":
                open_level = level
                found = True
            continue
        if not open_level:
            continue
        bullet = BULLET.match(line)
        indented = line[:1] in (" ", "\t")
        if bullet:
            flush()
            current.append(bullet.group(2).strip())
            nested = False
        elif indented and BULLET.match(line.lstrip()):
            nested = True
        elif indented and line.strip():
            if current and not nested:
                current.append(line.strip())
        else:
            flush()
    flush()
    return items if found else None


def collect(docs: Path, output_names: set[str]) -> tuple[list[Item], int, int, list[str]]:
    items: list[Item] = []
    problems: list[str] = []
    scanned = with_open = 0
    for path in sorted(docs.rglob("*.md")):
        rel = path.relative_to(docs)
        if rel.parts[0] == "archive" or rel.as_posix() in output_names:
            continue
        scanned += 1
        found = parse_file(path, rel.as_posix(), problems)
        if found is not None:
            with_open += 1
            items.extend(found)
    return items, scanned, with_open, problems


def _sort_key(item: Item) -> tuple[int, str, str]:
    return (SIZES.index(item.size) if item.size else len(SIZES), item.source, item.text)


def render_md(items: list[Item]) -> str:
    lines = [
        "<!-- Generated by scripts/generate_open_index.py. Do not edit by hand. -->",
        "",
        "# Open work",
        "",
        "Generated by `scripts/generate_open_index.py` from the `## Open` sections in `docs/`. "
        "Do not edit this file by hand.",
        "",
        f"{len(items)} items: " + ", ".join(f"{sum(i.kind == k for i in items)} {k or 'untagged'}" for k in (*KINDS, "")) + ".",
    ]
    for kind in (*KINDS, ""):
        rows = sorted((i for i in items if i.kind == kind), key=_sort_key)
        if not rows:
            continue
        lines += ["", f"## {kind or 'untagged'}", "", "| Size | Users | When | Checked | Item | Source |", "| --- | --- | --- | --- | --- | --- |"]
        for i in rows:
            text = i.text.replace("|", "\\|")
            lines.append(f"| {i.size} | {i.users} | {i.when} | {i.unverified} | {text} | [{i.source}]({i.source}#open) |")
    return "\n".join(lines) + "\n"


def render_csv(items: list[Item]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["kind", "size", "users", "when", "unverified", "item", "source"])
    for kind in (*KINDS, ""):
        for i in sorted((i for i in items if i.kind == kind), key=_sort_key):
            writer.writerow([i.kind, i.size, i.users, i.when, i.unverified, i.text, i.source])
    return buf.getvalue()


def main(argv: list[str] | None = None, docs: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/OPEN.md is missing or stale")
    args = parser.parse_args(argv)
    docs = docs or Path(__file__).resolve().parents[1] / "docs"
    md_path, csv_path = docs / "OPEN.md", docs / "OPEN.csv"
    items, scanned, with_open, problems = collect(docs, {md_path.name})
    md_text = render_md(items)
    if args.check:
        # Only OPEN.md is tracked; a clean checkout has no OPEN.csv.
        if md_path.exists() and md_path.read_text(encoding="utf-8") == md_text:
            return 0
        print(f"{md_path.name} is missing or stale; run `make open-index`", file=sys.stderr)
        return 1
    for path, text in ((md_path, md_text), (csv_path, render_csv(items))):
        path.write_text(text, encoding="utf-8", newline="")
    print(f"Scanned {scanned} files, {with_open} with an Open section, {len(items)} items")
    for kind in (*KINDS, ""):
        print(f"  {kind or 'untagged'}: {sum(i.kind == kind for i in items)}")
    for problem in problems:
        print(problem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
