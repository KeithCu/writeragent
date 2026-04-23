# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Split HTML fragments into alternating prose HTML and verbatim MathML blocks.

Phase 1 only recognizes ``<math>...</math>`` (case-insensitive tag name). We
extract byte-for-byte MathML substrings so LibreOffice's MathML importer sees
the same markup the model sent. Unclosed ``<math`` tails are surfaced as a final
HTML chunk (so nothing is silently dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_MATH_OPEN_RE = re.compile(r"<math\b", re.IGNORECASE)
_MATH_CLOSE_RE = re.compile(r"</math\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class MathSegment:
    """One ordered piece of a mixed HTML + MathML fragment."""

    kind: Literal["html", "math"]
    text: str
    # ``display_block`` is meaningful only when ``kind == "math"``; False for html.
    display_block: bool = False


def html_fragment_contains_mathml(fragment: str) -> bool:
    """Fast check for MathML root elements."""
    if not fragment or not isinstance(fragment, str):
        return False
    return _MATH_OPEN_RE.search(fragment) is not None


def _tag_end(s: str, start: int) -> int:
    """Return index of ``>`` closing the tag that begins at *start*, or -1."""
    i = start
    in_single = False
    in_double = False
    while i < len(s):
        c = s[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == ">" and not in_single and not in_double:
            return i
        i += 1
    return -1


def _display_block_from_open_tag(open_tag: str) -> bool:
    """True for display-style math per MathML / common authoring conventions.

    Rules (documented for reviewers):
    - ``display="block"`` (MathML 3) → block
    - ``display="inline"`` → inline
    - ``mode="display"`` (legacy MathML) → block
    - ``mode="inline"`` → inline
    - otherwise → inline (including missing attributes)
    """
    m = re.search(
        r"\bdisplay\s*=\s*(['\"])(.*?)\1",
        open_tag,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        v = m.group(2).strip().lower()
        if v == "block":
            return True
        if v == "inline":
            return False
    m2 = re.search(
        r"\bmode\s*=\s*(['\"])(.*?)\1",
        open_tag,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m2:
        v = m2.group(2).strip().lower()
        if v == "display":
            return True
        if v == "inline":
            return False
    return False


def _first_math_close(s: str, start: int) -> re.Match[str] | None:
    return _MATH_CLOSE_RE.search(s, pos=start)


def segment_html_with_mathml(fragment: str) -> list[MathSegment]:
    """Split *fragment* into ``html`` / ``math`` segments in document order."""
    if not fragment:
        return []
    out: list[MathSegment] = []
    pos = 0
    while True:
        m_open = _MATH_OPEN_RE.search(fragment, pos)
        if not m_open:
            tail = fragment[pos:]
            if tail:
                out.append(MathSegment(kind="html", text=tail))
            break
        if m_open.start() > pos:
            out.append(MathSegment(kind="html", text=fragment[pos : m_open.start()]))
        open_start = m_open.start()
        gt = _tag_end(fragment, open_start)
        if gt < 0:
            # Malformed: no closing ``>`` for opening tag — keep rest as HTML.
            out.append(MathSegment(kind="html", text=fragment[open_start:]))
            break
        open_tag = fragment[open_start : gt + 1]
        display = _display_block_from_open_tag(open_tag)
        close_m = _first_math_close(fragment, gt + 1)
        if not close_m:
            out.append(MathSegment(kind="html", text=fragment[open_start:]))
            break
        math_xml = fragment[open_start : close_m.end()]
        out.append(MathSegment(kind="math", text=math_xml, display_block=display))
        pos = close_m.end()
    return out
