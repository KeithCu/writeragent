# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Split HTML fragments into prose HTML, verbatim MathML, and TeX math runs.

We extract byte-for-byte ``<math>...</math>`` so LibreOffice's MathML importer sees
the same markup the model sent. TeX islands use ``$...$``, ``$$...$$``,
``\\(...\\)``, and ``\\[...\\]`` (conservative rules for ``$`` vs currency).
Unclosed ``<math`` tails are surfaced as a final HTML chunk (nothing silently
dropped). Incomplete TeX delimiters are left as HTML by advancing the scan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_MATH_OPEN_RE = re.compile(r"<math\b", re.IGNORECASE)
_MATH_CLOSE_RE = re.compile(r"</math\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class MathSegment:
    """One ordered piece of a mixed HTML + math fragment."""

    kind: Literal["html", "math", "tex"]
    text: str
    # Meaningful for ``math`` / ``tex``; False for ``html``.
    display_block: bool = False


def html_fragment_contains_mathml(fragment: str) -> bool:
    """Fast check for MathML root elements."""
    if not fragment or not isinstance(fragment, str):
        return False
    return _MATH_OPEN_RE.search(fragment) is not None


def _preceding_backslashes(s: str, idx: int) -> int:
    """Count consecutive ``\\`` characters immediately before *idx*."""
    n = 0
    j = idx - 1
    while j >= 0 and s[j] == "\\":
        n += 1
        j -= 1
    return n


def _is_escaped(s: str, idx: int) -> bool:
    return _preceding_backslashes(s, idx) % 2 == 1


def html_fragment_contains_tex_math(fragment: str) -> bool:
    """Fast check for common TeX math delimiters (conservative ``$`` rules)."""
    if not fragment or not isinstance(fragment, str):
        return False
    if "$$" in fragment or "\\[" in fragment or "\\(" in fragment:
        return True
    i = 0
    while True:
        j = fragment.find("$", i)
        if j < 0:
            return False
        if _is_escaped(fragment, j):
            i = j + 1
            continue
        if j + 1 < len(fragment) and fragment[j + 1] == "$":
            return True
        if j + 1 < len(fragment) and fragment[j + 1].isdigit():
            i = j + 1
            continue
        return True


def html_fragment_contains_mixed_math(fragment: str) -> bool:
    """True if the fragment should use the mixed HTML + math insert path."""
    return html_fragment_contains_mathml(fragment) or html_fragment_contains_tex_math(
        fragment
    )


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
    """True for display-style math per MathML / common authoring conventions."""
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


def _scan_next_tex_open(
    s: str, pos: int
) -> tuple[int, Literal["$$", "$", "\\[", "\\("], bool] | None:
    """Return ``(index, delimiter, display_block)`` for the next TeX opener, or None."""
    n = len(s)
    i = pos
    while i < n:
        if _is_escaped(s, i):
            i += 1
            continue
        if i + 1 < n and s[i : i + 2] == "$$":
            return (i, "$$", True)
        if i + 1 < n and s[i : i + 2] == "\\[":
            return (i, "\\[", True)
        if i + 1 < n and s[i : i + 2] == "\\(":
            return (i, "\\(", False)
        if s[i] == "$":
            if i + 1 < n and s[i + 1].isdigit():
                i += 1
                continue
            return (i, "$", False)
        i += 1
    return None


def _try_close_tex(
    s: str, open_start: int, delim: Literal["$$", "$", "\\[", "\\("]
) -> tuple[int, str] | None:
    """If delimiters close, return ``(end_exclusive, inner_latex)`` else None."""
    n = len(s)
    if delim == "$$":
        body_start = open_start + 2
        j = body_start
        while j < n - 1:
            if s[j : j + 2] == "$$" and not _is_escaped(s, j):
                inner = s[body_start:j]
                return (j + 2, inner)
            j += 1
        return None
    if delim == "\\[":
        body_start = open_start + 2
        j = body_start
        while j < n - 1:
            if s[j : j + 2] == "\\]" and not _is_escaped(s, j):
                return (j + 2, s[body_start:j])
            j += 1
        return None
    if delim == "\\(":
        body_start = open_start + 2
        j = body_start
        while j < n - 1:
            if s[j : j + 2] == "\\)" and not _is_escaped(s, j):
                return (j + 2, s[body_start:j])
            j += 1
        return None
    # single $
    body_start = open_start + 1
    j = body_start
    while j < n:
        if _is_escaped(s, j):
            j += 1
            continue
        if s[j] == "$":
            return (j + 1, s[body_start:j])
        j += 1
    return None


def _next_complete_tex_region(
    s: str, pos: int,
) -> tuple[int, int, str, bool] | None:
    """First closed TeX region at/after *pos*: ``(start, end, inner, display)``."""
    while True:
        opened = _scan_next_tex_open(s, pos)
        if opened is None:
            return None
        open_start, delim, display_block = opened
        closed = _try_close_tex(s, open_start, delim)
        if closed is not None:
            end_exc, inner = closed
            return (open_start, end_exc, inner, display_block)
        # Skip past this opener so e.g. unclosed ``$$`` does not re-scan the 2nd ``$``.
        if delim == "$$" or delim in ("\\[", "\\("):
            pos = open_start + 2
        else:
            pos = open_start + 1


def segment_html_with_mixed_math(fragment: str) -> list[MathSegment]:
    """Split *fragment* into ``html``, ``math``, and ``tex`` segments in order."""
    if not fragment:
        return []
    out: list[MathSegment] = []
    pos = 0
    while pos < len(fragment):
        m_open = _MATH_OPEN_RE.search(fragment, pos)
        tex_region = _next_complete_tex_region(fragment, pos)

        math_start = m_open.start() if m_open else len(fragment) + 1
        tex_start = tex_region[0] if tex_region else len(fragment) + 1

        if not m_open and not tex_region:
            tail = fragment[pos:]
            if tail:
                out.append(MathSegment(kind="html", text=tail))
            break

        if m_open and (not tex_region or math_start <= tex_start):
            if math_start > pos:
                out.append(MathSegment(kind="html", text=fragment[pos:math_start]))
            open_start = math_start
            gt = _tag_end(fragment, open_start)
            if gt < 0:
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
            continue

        # TeX strictly before <math, or no math at this step
        assert tex_region is not None
        t_start, t_end, inner, display_block = tex_region
        if t_start > pos:
            out.append(MathSegment(kind="html", text=fragment[pos:t_start]))
        out.append(
            MathSegment(kind="tex", text=inner, display_block=display_block)
        )
        pos = t_end

    return out


def segment_html_with_mathml(fragment: str) -> list[MathSegment]:
    """Split *fragment* into segments; same as :func:`segment_html_with_mixed_math`."""
    return segment_html_with_mixed_math(fragment)
