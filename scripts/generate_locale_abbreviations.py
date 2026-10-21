#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate plugin/writer/locale/locale_abbrev.py from Unicode CLDR SentenceBreak suppressions.

CLDR ``common/segments/*.xml`` lists abbreviations that should not end a sentence
(ULI / UTS #35 segmentation suppressions). Only a handful of languages ship this
data; the grammar checker keeps heuristics for the rest.

Future work: bump CLDR_TAG and regenerate; do not hand-append tokens.
Full rules are in docs/writer/grammar-checker-plan.md (Abbreviation detection / Future work).

Usage:
    python scripts/generate_locale_abbreviations.py
    python scripts/generate_locale_abbreviations.py --from-dir /path/to/cldr/common/segments
"""

from __future__ import annotations

import argparse
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Pin a released CLDR tag so regenerations are reproducible.
CLDR_TAG = "release-48-2"
CLDR_VERSION_LABEL = "48.2"
CLDR_SEGMENTS_BASE = f"https://raw.githubusercontent.com/unicode-org/cldr/{CLDR_TAG}/common/segments"

# Locales that actually ship SentenceBreak <suppression> lists in CLDR
# (el/ja/zh segment files exist but only tailor break rules, not abbrevs).
CLDR_SEGMENT_FILES: tuple[str, ...] = (
    "en.xml",
    "de.xml",
    "es.xml",
    "fr.xml",
    "it.xml",
    "pt.xml",
    "ru.xml",
)

# Raw CLDR English suppressions include ordinary sentence-final words (To., By., …).
# Matching those as abbreviations over-merges real sentence ends.
_AMBIGUOUS_DENYLIST: frozenset[str] = frozenset({
    # Function / pronoun / light verbs that can end a sentence
    "to", "by", "on", "go", "is", "do", "as", "or", "in", "at", "up", "all", "for",
    "so", "an", "be", "if", "it", "me", "my", "we", "he", "she", "of", "ok",
    # Truncated English words that appear in ULI data but are not safe as global abbrevs
    "job", "long", "link", "hat", "act", "var", "jam", "card", "joe", "lev", "mart",
})

_VOWELS = set("aeiouyаеёиоуыэюяαεηιουωàáâãäåèéêëìíîïòóôõöùúûüýÿæøœ")


def normalize_abbrev(raw: str) -> str | None:
    """Lowercase, strip trailing dots; keep internal dots. Skip multi-word / empty."""
    text = (raw or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    norm = text.lower().rstrip(".")
    if not norm or not any(ch.isalpha() for ch in norm):
        return None
    return norm


def is_ambiguous(norm: str) -> bool:
    if norm in _AMBIGUOUS_DENYLIST:
        return True
    # Single letters are covered by the initials heuristic; skip to shrink the table.
    if len(norm) == 1 and norm.isalpha():
        return True
    # Bare 2-letter Latin words with a vowel are usually real words (to/by/on already listed).
    alpha = [ch for ch in norm if ch.isalpha()]
    if len(norm) == 2 and len(alpha) == 2 and "." not in norm:
        if any(ch in _VOWELS for ch in alpha) and all(ord(ch) < 128 for ch in alpha):
            return True
    return False


def keep_suppression(norm: str) -> bool:
    """Accept high-value CLDR tokens; reject ambiguous sentence-enders."""
    if is_ambiguous(norm):
        return False
    # Internal periods (U.S.A., Ph.D., т.е.) are strong abbrev signals.
    if "." in norm:
        return True
    # Consonant-only (vs, ltd, ст) — same idea as the runtime heuristic.
    alpha = [ch for ch in norm if ch.isalpha()]
    if alpha and not any(ch in _VOWELS for ch in alpha):
        return True
    # Titles / months / orgs: short alpha tokens (2–6 letters) without looking like English stopwords.
    if 2 <= len(alpha) <= 6 and all(ch.isalpha() or ch in ".-'" for ch in norm):
        return True
    # Longer tokens with at least one non-Latin letter (Cyrillic/Greek months, etc.).
    if any(ord(ch) > 127 for ch in norm) and 2 <= len(alpha) <= 12:
        return True
    return False


def parse_suppressions_xml(xml_text: str) -> set[str]:
    """Extract SentenceBreak <suppression> strings from a CLDR segments file.

    These files only contain SentenceBreak suppressions today, so every
    ``<suppression>`` element is relevant. Multi-word entries are skipped in
    ``normalize_abbrev`` (the runtime checker sees only the token before ``.``).
    """
    root = ET.fromstring(xml_text)
    found: set[str] = set()
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag != "suppression":
            continue
        norm = normalize_abbrev(elem.text or "")
        if norm and keep_suppression(norm):
            found.add(norm)
    return found


def fetch_segment_file(filename: str, from_dir: str | None) -> str:
    if from_dir:
        path = os.path.join(from_dir, filename)
        with open(path, encoding="utf-8") as f:
            return f.read()
    url = f"{CLDR_SEGMENTS_BASE}/{filename}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 — fixed HTTPS Unicode CDN
            return resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Failed to fetch {url}: {exc}") from exc


def collect_abbrevs(from_dir: str | None) -> set[str]:
    abbrevs: set[str] = set()
    for filename in CLDR_SEGMENT_FILES:
        print(f"  {filename} …", end=" ", flush=True)
        xml_text = fetch_segment_file(filename, from_dir)
        file_abbrevs = parse_suppressions_xml(xml_text)
        abbrevs.update(file_abbrevs)
        print(f"{len(file_abbrevs)} kept")
    return abbrevs


def escape_py(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def generate_module(abbrevs: set[str]) -> str:
    lines: list[str] = [
        "# WriterAgent - AI Writing Assistant for LibreOffice",
        "# Copyright (c) 2026 KeithCu",
        "#",
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        '"""CLDR SentenceBreak suppression abbreviations for grammar sentence splitting.',
        "",
        f"Generated by scripts/generate_locale_abbreviations.py from Unicode CLDR {CLDR_VERSION_LABEL}",
        f"(git tag {CLDR_TAG}). Data © Unicode, Inc. — Unicode License.",
        "Do not edit by hand; re-run the generator after bumping CLDR_TAG.",
        "",
        "Merged only — per-language subsets are not emitted (unused at runtime).",
        "",
        "Future work: bump CLDR_TAG and regenerate; do not hand-append tokens.",
        "Full rules: docs/writer/grammar-checker-plan.md (Abbreviation detection / Future work).",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "",
        f"# Merged filtered suppressions from CLDR {CLDR_VERSION_LABEL} ({len(abbrevs)} tokens)",
        "CLDR_ABBREVS: frozenset[str] = frozenset({",
    ]
    for abbr in sorted(abbrevs, key=lambda s: (s.encode("utf-8"), s)):
        lines.append(f'    "{escape_py(abbr)}",')
    lines.append("})")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-dir",
        help="Read CLDR segment XML files from a local common/segments directory instead of GitHub",
    )
    parser.add_argument(
        "--output",
        default=os.path.join(REPO_ROOT, "plugin", "writer", "locale", "locale_abbrev.py"),
        help="Output path for locale_abbrev.py",
    )
    args = parser.parse_args()

    print(f"CLDR SentenceBreak suppressions → locale_abbrev.py (tag {CLDR_TAG})")
    print()
    abbrevs = collect_abbrevs(args.from_dir)
    content = generate_module(abbrevs)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(content)

    print()
    print(f"Wrote {args.output}")
    print(f"  files: {', '.join(CLDR_SEGMENT_FILES)}")
    print(f"  unique abbreviations: {len(abbrevs)}")


if __name__ == "__main__":
    main()
