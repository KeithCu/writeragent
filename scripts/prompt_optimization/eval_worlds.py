# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory Writer / Draw / Calc worlds for string-harness eval (no LibreOffice).

FROZEN: Do not add new simulated LibreOffice behavior, parity patches, or
in-memory workarounds to this file. Only changes that delegate directly to
production ``plugin/`` code (or fix a broken production import/signature) are
allowed. New tool fidelity belongs in the real-LibreOffice UNO eval lane
(``--backend lo`` via ``tools_lo.py`` and ``tests/.../test_*_uno.py``).

HTML apply/search stays on a string so existing oracles keep working. Blocks,
connections, and formula dests are parsed or recorded so style, comments,
flowchart edges, and =PY placement can be scored honestly.
"""
from __future__ import annotations

import math
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

from plugin.calc.address_utils import split_sheet_prefix
from plugin.calc.formula_fill import expand_single_formula
from plugin.calc.inspector import grid_to_rows
from plugin.calc.manipulator import (
    SortKey,
    _delimited_text_message,
    _first_ragged_row,
    _grid_mismatch_message,
    _parse_json_array_string,
    _sort_done_message,
    _sort_keys_width_error,
    _sort_unchanged_message,
    _values_length_mismatch_message,
    _write_values_grid_cell_error,
    parse_then_by,
)
from plugin.writer.search import case_ladder, parse_non_negative_int, prepare_old_content
from plugin.draw.tree import (
    build_shape_tree,
    custom_shape_geometry,
    ensure_shape_name,
    find_shape_on_page,
    iter_shape_addresses,
    normalize_negative_span,
    page_shape_names,
    resolve_shape_ref,
    short_shape_type,
)
from plugin.framework.errors import available_miss_details, make_tool_error
from plugin.framework.json_utils import parse_html_fragment_list

_COMMENT_RE = re.compile(r"\[([^\]]+)\]")
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_TAGGED_BLOCK_RE = re.compile(r"<(p|h[1-6])(\s[^>]*)?>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_LINE_RE = re.compile(r"[^\n]+")
_HEADING_STYLE_RE = re.compile(r"^heading\s*([1-6])$", re.IGNORECASE)


def heading_level_from_style(style_name: str) -> int | None:
    compact = re.sub(r"[\s_]+", " ", (style_name or "").strip())
    m = _HEADING_STYLE_RE.match(compact)
    return int(m.group(1)) if m else None


def _style_blocks(html: str) -> list[tuple[int, int, str, str]]:
    """Return (start, end, attrs, inner) per paragraph: each <p>/<hN>, and each bare line between them."""
    blocks: list[tuple[int, int, str, str]] = []
    pos = 0
    for m in _TAGGED_BLOCK_RE.finditer(html):
        blocks += [(g.start(), g.end(), "", g.group().strip()) for g in _LINE_RE.finditer(html, pos, m.start())]
        blocks.append((m.start(), m.end(), m.group(2) or "", m.group(3)))
        pos = m.end()
    blocks += [(g.start(), g.end(), "", g.group().strip()) for g in _LINE_RE.finditer(html, pos)]
    return blocks


def _restyle_block(attrs: str, inner: str, style_name: str) -> str:
    """Rewrite a paragraph block's tag or style attribute to match ``style_name``."""
    heading_level = heading_level_from_style(style_name)
    if heading_level is not None:
        return f"<h{heading_level}>{inner}</h{heading_level}>"
    token = "Quotations" if style_name.casefold() == "quotations" else style_name
    if re.search(r"data-lo-style\s*=", attrs, re.I):
        attrs = re.sub(r'data-lo-style\s*=\s*["\'][^"\']*["\']', f'data-lo-style="{token}"', attrs, flags=re.I)
    elif re.search(r"class\s*=", attrs, re.I):
        attrs = re.sub(r'class\s*=\s*["\'][^"\']*["\']', f'class="{token}"', attrs, flags=re.I)
    else:
        attrs = f'{attrs} data-lo-style="{token}"'
    return f"<p{attrs}>{inner}</p>"


def _find_spans(text: str, needle: str, case_sensitive: bool | None) -> list[tuple[int, int]]:
    """Return non-overlapping literal spans; omitted case_sensitive tries exact then ignore-case."""
    escaped = re.escape(needle)
    for cs in case_ladder(case_sensitive):
        flags = 0 if cs else re.IGNORECASE
        # re.finditer resumes after each match, matching live findNext(found.getEnd(), ...).
        spans = [(m.start(), m.end()) for m in re.finditer(escaped, text, flags)]
        if spans:
            return spans
    return []


def _old_content_not_found(many: bool, **extra: Any) -> dict[str, Any]:
    msg = (
        "Replaced 0 occurrence(s). No matches found. Try a shorter substring."
        if many
        else "old_content not found in document. Try a shorter, unique substring."
    )
    return make_tool_error(msg, code="OLD_CONTENT_NOT_FOUND", top_level={"replaced_count": 0, **extra})


def _occurrence_message(index: int, count: int) -> str:
    return f"occurrence {index} out of range (found {count} match(es), use 0..{count - 1})."


def a1_to_col_row(addr: str) -> tuple[int, int]:
    """Parse A1 / Sheet1.C2 / 'My Sheet'!C2 into 0-based (col, row)."""
    unused_sheet, text = split_sheet_prefix(addr or "")
    letters: list[str] = []
    digits: list[str] = []
    for ch in text:
        if ch.isalpha():
            letters.append(ch.upper())
        elif ch.isdigit():
            digits.append(ch)
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    col = max(0, col - 1)
    row = max(0, int("".join(digits) or "1") - 1)
    return col, row


def col_row_to_a1(col: int, row: int) -> str:
    """0-based col/row to A1."""
    n = col + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"{letters}{row + 1}"


def parse_a1_range(rng: str) -> tuple[int, int, int, int]:
    """Return (c0, r0, c1, r1) inclusive from ``A1:H500``, ``J1``, or sheet-qualified ranges."""
    unused_sheet, raw = split_sheet_prefix(rng or "")
    start, end = (raw.split(":", 1) + [raw])[:2]
    c0, r0 = a1_to_col_row(start)
    c1, r1 = a1_to_col_row(end)
    if c1 < c0:
        c0, c1 = c1, c0
    if r1 < r0:
        r0, r1 = r1, r0
    return c0, r0, c1, r1


def cell_in_a1_range(addr: str, rng: str) -> bool:
    c, r = a1_to_col_row(addr)
    c0, r0, c1, r1 = parse_a1_range(rng)
    return c0 <= c <= c1 and r0 <= r <= r1


def range_cell_count(rng: str) -> int:
    c0, r0, c1, r1 = parse_a1_range(rng)
    return max(0, (c1 - c0 + 1) * (r1 - r0 + 1))


def normalize_apply_content(content: Any) -> str:
    """Mirror ApplyDocumentContent list/string normalization (apply_document_content.py)."""
    if isinstance(content, str):
        parsed = parse_html_fragment_list(content)
        if parsed is not None:
            content = parsed
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    if isinstance(content, str):
        content = content.replace("\\n", "\n").replace("\\t", "\t")
    return content if isinstance(content, str) else ""


def unsupported_in_eval(name: str) -> dict[str, Any]:
    return {
        "status": "error",
        "code": "unsupported_in_eval",
        "message": f"{name} is not implemented in the string harness",
    }


class _BlockParser(HTMLParser):
    """Shallow HTML → blocks. Export still uses the raw HTML string."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[dict[str, Any]] = []
        self._kind = ""
        self._level = 0
        self._style = "Default"
        self._buf: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k: (v or "") for k, v in attrs}
        cls = attrs_d.get("class") or ""
        if tag in _HEADING_TAGS:
            self._flush()
            self._kind = "heading"
            self._level = int(tag[1])
            self._style = cls or f"Heading {tag[1]}"
            self._depth = 1
        elif tag == "p" and self._depth == 0:
            self._flush()
            self._kind = "paragraph"
            self._level = 0
            self._style = cls or "Default"
            self._depth = 1
        elif tag == "table" and self._depth == 0:
            self._flush()
            self._kind = "table"
            self._level = 0
            self._style = cls or "Default"
            self._depth = 1
        elif tag in ("ul", "ol") and self._depth == 0:
            self._flush()
            self._kind = "list"
            self._level = 0
            self._style = cls or "Default"
            self._depth = 1
        elif self._depth:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        unused = tag
        del unused
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                self._flush()

    def handle_data(self, data: str) -> None:
        if self._kind or data.strip():
            self._buf.append(data)

    def _flush(self) -> None:
        if not self._kind and not "".join(self._buf).strip():
            self._buf = []
            return
        text = "".join(self._buf)
        comments = [{"anchor": "", "text": m.group(1)} for m in _COMMENT_RE.finditer(text)]
        self.blocks.append(
            {
                "type": self._kind or "paragraph",
                "text": text,
                "style": self._style or "Default",
                "level": self._level,
                "comments": comments,
            }
        )
        self._kind = ""
        self._level = 0
        self._style = "Default"
        self._buf = []

    def close(self) -> None:  # type: ignore[override]
        self._flush()
        super().close()


def parse_html_blocks(html: str) -> list[dict[str, Any]]:
    raw = html or ""
    if "<" not in raw:
        comments = [{"anchor": "", "text": m.group(1)} for m in _COMMENT_RE.finditer(raw)]
        return [
            {
                "type": "paragraph",
                "text": raw,
                "style": "Default",
                "level": 0,
                "comments": comments,
            }
        ]
    parser = _BlockParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return [
            {
                "type": "paragraph",
                "text": raw,
                "style": "Default",
                "level": 0,
                "comments": [],
            }
        ]
    return parser.blocks or [
        {
            "type": "paragraph",
            "text": raw,
            "style": "Default",
            "level": 0,
            "comments": [],
        }
    ]


class WriterWorld:
    """Writer document: HTML string plus parsed blocks (style / comments)."""

    __slots__ = ("_html", "blocks")

    def __init__(self, initial: str) -> None:
        self._html = initial or ""
        self.blocks: list[dict[str, Any]] = []
        self._sync_blocks()

    def _sync_blocks(self) -> None:
        self.blocks = parse_html_blocks(self._html)

    def get_html(self) -> str:
        return self._html

    def set_html(self, html: str) -> None:
        self._html = html
        self._sync_blocks()

    def export_for_prompt(self) -> str:
        """Hook for a later multi-turn refresh. Same as get_html today."""
        return self._html

    def get_content(self, **kwargs: Any) -> dict[str, Any]:
        scope = kwargs.get("scope")
        max_chars = kwargs.get("max_chars")
        if max_chars is not None and max_chars < 1:
            return make_tool_error("max_chars must be a positive integer.")
        text = self._html
        if scope == "range":
            start = kwargs.get("start")
            end = kwargs.get("end")
            if start is None or end is None:
                return make_tool_error("scope 'range' requires integer start and end.")
            if start < 0 or end <= start:
                return make_tool_error("scope 'range' requires 0 <= start < end.")
            text = text[start:end]
        elif scope == "selection":
            text = self._html
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... truncated ...]"
        return {
            "status": "ok",
            "content": text,
            "length": len(text),
            "document_length": len(self._html),
        }

    def apply_content(self, **kwargs: Any) -> dict[str, Any]:
        old_content = kwargs.get("old_content")
        start_anchor = kwargs.get("start_anchor")
        end_anchor = kwargs.get("end_anchor")
        target = kwargs.get("target")
        if not target and (
            old_content is not None or start_anchor is not None or end_anchor is not None
        ):
            target = "search"
        if start_anchor is not None or end_anchor is not None:
            return unsupported_in_eval("apply_content start_anchor/end_anchor")
        if kwargs.get("dry_run"):
            return unsupported_in_eval("apply_content dry_run=true")
        position = kwargs.get("position")
        if position in ("before", "after"):
            return unsupported_in_eval(f"apply_content position={position}")
        if target == "search":
            if kwargs.get("regex"):
                return unsupported_in_eval("apply_content regex=true")
            if (kwargs.get("hyperlink_url") or "").strip():
                return unsupported_in_eval("apply_content hyperlink_url")
        if not target:
            return make_tool_error(
                "Provide a target ('beginning', 'end', 'selection', 'full_document', 'search') "
                "or old_content for find-and-replace.",
                code="INVALID_PARAM",
            )
        if target == "search" and old_content is None:
            return make_tool_error(
                "target='search' requires old_content or start_anchor and end_anchor.",
                code="INVALID_PARAM",
            )

        all_matches = bool(kwargs.get("all_matches"))
        occurrence = kwargs.get("occurrence")
        occ_idx: int | None = None
        if occurrence is not None:
            if target != "search":
                return make_tool_error(
                    "occurrence only applies to target='search'.",
                    code="INVALID_PARAM",
                )
            if all_matches:
                return make_tool_error(
                    "occurrence cannot be combined with all_matches=true.",
                    code="INVALID_PARAM",
                )
            occ_idx, occ_err = parse_non_negative_int(occurrence)
            if occ_err:
                return make_tool_error(occ_err, code="INVALID_PARAM")

        content = normalize_apply_content(kwargs.get("content", ""))
        if target in ("beginning", "end", "selection") and not content.strip():
            return make_tool_error("content is empty; nothing was inserted.", code="INVALID_PARAM")

        if target == "full_document":
            self._html = content
            self._sync_blocks()
            from plugin.writer.html_import_styles import summarize_written_blocks

            summary = summarize_written_blocks(content)
            return {
                "status": "ok",
                "message": "Replaced entire document.",
                "paragraph_count": summary["paragraph_count"],
                "headings": summary["headings"],
            }
        if target == "end":
            self._html = self._html + content
            self._sync_blocks()
            return {"status": "ok", "message": "Inserted content at end."}
        if target == "beginning":
            self._html = content + self._html
            self._sync_blocks()
            return {"status": "ok", "message": "Inserted content at beginning."}
        if target == "selection":
            # Documented limitation: no cursor. Append so apply still mutates.
            self._html = self._html + content
            self._sync_blocks()
            return {"status": "ok", "message": "Inserted content (simulated selection)."}

        old = str(old_content).strip()
        if not old:
            return make_tool_error("old_content is empty after normalization.", code="INVALID_PARAM")
        spans = _find_spans(self._html, old, kwargs.get("case_sensitive"))
        if not spans:
            return _old_content_not_found(all_matches)
        if all_matches:
            html = self._html
            for start, end in reversed(spans):
                html = html[:start] + content + html[end:]
            self._html = html
            self._sync_blocks()
            return {
                "status": "ok",
                "message": "Replaced %d occurrence(s)." % len(spans),
                "replaced_count": len(spans),
            }
        chosen = occ_idx if occ_idx is not None else 0
        if chosen >= len(spans):
            return make_tool_error(
                _occurrence_message(chosen, len(spans)),
                code="OCCURRENCE_OUT_OF_RANGE",
                count=len(spans),
            )
        start, end = spans[chosen]
        self._html = self._html[:start] + content + self._html[end:]
        self._sync_blocks()
        res: dict[str, Any] = {
            "status": "ok",
            "message": "Replaced 1 occurrence (by old_content).",
            "replaced_count": 1,
        }
        if occ_idx is not None:
            res["occurrence"] = occ_idx
        return res

    def search(self, **kwargs: Any) -> dict[str, Any]:
        """Production name. Offsets are into exported HTML."""
        limit = kwargs["max_results"]
        if limit < 1:
            return make_tool_error("max_results must be a positive integer.", code="INVALID_PARAM")
        pattern = kwargs["pattern"]
        if not pattern:
            return make_tool_error("pattern is required.", code="INVALID_PARAM")
        if kwargs["regex"]:
            return unsupported_in_eval("search regex=true")
        spans = _find_spans(self._html, pattern, kwargs["case_sensitive"])[:limit]
        ranges = [
            {"start": start, "end": end, "text": self._html[start:end]}
            for start, end in spans
        ]
        matches = [
            {
                "location": "body",
                "text": item["text"],
                "start": item["start"],
                "end": item["end"],
            }
            for item in ranges
        ]
        return {"status": "ok", "ranges": ranges, "matches": matches}

    def comment_add(self, **kwargs: Any) -> dict[str, Any]:
        """Anchor a visible ``[comment]`` after the search hit (no UNO annotation)."""
        content = kwargs["content"]
        if not content:
            return make_tool_error("Provide content.")
        if (kwargs.get("parent_name") or "").strip():
            return unsupported_in_eval("comment_add parent_name")
        search = kwargs.get("search") or ""
        if not search:
            return make_tool_error("Provide search.")
        occ_val, occ_err = parse_non_negative_int(kwargs.get("occurrence"))
        if occ_err:
            return make_tool_error(
                occ_err,
                code="INVALID_PARAM",
                top_level={"matched": False, "comment_added": False},
            )
        occurrence = occ_val or 0
        spans = _find_spans(self._html, search, kwargs.get("case_sensitive"))
        if not spans:
            return make_tool_error(
                "search not found in document. Try a shorter, unique substring.",
                code="OLD_CONTENT_NOT_FOUND",
                top_level={"replaced_count": 0, "matched": False, "comment_added": False},
            )
        if occurrence >= len(spans):
            return make_tool_error(
                _occurrence_message(occurrence, len(spans)),
                code="OCCURRENCE_OUT_OF_RANGE",
                top_level={"matched": False, "comment_added": False},
            )
        start, end = spans[occurrence]
        anchor_text = self._html[start:end]
        insert = f" [{content}]"
        self._html = self._html[:end] + insert + self._html[end:]
        self._sync_blocks()
        return {
            "status": "ok",
            "comment_added": True,
            "matched": True,
            "anchor_text": anchor_text,
        }

    def apply_style(self, **kwargs: Any) -> dict[str, Any]:
        """Map production apply_style onto HTML tags / data-lo-style (no UNO families)."""
        style_name = (kwargs.get("style") or "").strip()
        if not style_name:
            return make_tool_error("style is required.", code="INVALID_PARAM")
        family = kwargs["family"]
        if family == "CharacterStyles":
            return unsupported_in_eval("apply_style family=CharacterStyles")
        target = kwargs.get("target", "selection")
        if kwargs["clear_direct"] == "all":
            if target == "full_document":
                return make_tool_error(
                    "clear_direct='all' is not allowed with target='full_document'.",
                    code="INVALID_PARAM",
                )
            return unsupported_in_eval("apply_style clear_direct=all")

        if target == "full_document":
            if style_name.casefold() == "quotations":
                html = re.sub(
                    r'data-lo-style\s*=\s*["\']Default["\']',
                    'data-lo-style="Quotations"',
                    self._html,
                    flags=re.I,
                )
                self.set_html(html)
                return {"status": "ok", "message": "Applied Quotations to Default paragraphs."}
            return unsupported_in_eval("apply_style target=full_document")
        if target != "search":
            return unsupported_in_eval(f"apply_style target={target}")
        needle = prepare_old_content(kwargs.get("old_content"))
        if not needle:
            return make_tool_error(
                "target='search' requires old_content.",
                code="INVALID_PARAM",
                top_level={"applied": False, "matched": False},
            )
        if "\n" in needle:
            return unsupported_in_eval("apply_style old_content across paragraphs")

        html = self._html
        blocks = _style_blocks(html)
        # Search visible text so an attribute value is not a match and does not shift occurrence.
        # One line per paragraph, so a match's line number is its paragraph's index.
        text = "\n".join(prepare_old_content(html[start:end]).replace("\n", " ") for start, end, _attrs, _inner in blocks)
        hits = [text.count("\n", 0, start) for start, _end in _find_spans(text, needle, kwargs.get("case_sensitive"))]
        occurrence = kwargs.get("occurrence")
        if not hits:
            return _old_content_not_found(
                kwargs["all_matches"] or occurrence is not None, target="search", applied=False, matched=False
            )
        if occurrence is not None:
            if not 0 <= occurrence < len(hits):
                return make_tool_error(_occurrence_message(occurrence, len(hits)), code="OCCURRENCE_OUT_OF_RANGE")
            hits = [hits[occurrence]]
        elif not kwargs["all_matches"]:
            hits = hits[:1]
        for idx in sorted(set(hits), reverse=True):
            start, end, attrs, inner = blocks[idx]
            html = html[:start] + _restyle_block(attrs, inner, style_name) + html[end:]
        self.set_html(html)
        return {
            "status": "ok",
            "style_name": style_name,
            "family": family,
            "target": "search",
            "applied": True,
            "matched": True,
            "applied_count": len(hits),
            "message": f"Applied style '{style_name}' ({family}) to {len(hits)} match(es).",
        }


# Same resolution as _execute_upsert_shape in plugin/draw/shapes.py: these names are UNO
# shapes, a UNO type name passes through, and every other name is a CustomShape preset.
_BASIC_SHAPE_TYPES = {"text": "TextShape", "line": "LineShape", "connector": "ConnectorShape"}
_BOX_KEYS = ("x", "y", "width", "height")


def _draw_node_type(shape_type: str) -> tuple[str, str | None]:
    mapped = _BASIC_SHAPE_TYPES.get(shape_type)
    if mapped:
        return mapped, None
    if "." in shape_type or shape_type.endswith("Shape"):
        return short_shape_type(shape_type), None
    return "CustomShape", shape_type


def _line_ends(args: dict[str, Any]) -> dict[str, Any]:
    """UNO line-end name properties for the arrowhead arguments."""
    out: dict[str, Any] = {}
    for key, prop in (("line_start", "LineStartName"), ("line_end", "LineEndName")):
        if key in args:
            out[prop] = "Arrow" if args[key] == "arrow" else ""
    return out


class _SimNode:
    """The XShape calls ``plugin.draw.tree`` and ``shape_get_summary`` read, kept in memory."""

    def __init__(self, shape_type: str, name: str = "") -> None:
        self.shape_type = shape_type
        self.Name = name
        self.props: dict[str, Any] = {}

    def getShapeType(self) -> str:
        return f"com.sun.star.drawing.{self.shape_type}"

    def getPropertyValue(self, prop: str) -> Any:
        return self.props.get(prop)

    def box(self) -> tuple[int, int, int, int]:
        raise NotImplementedError

    def set_box(self, x: int, y: int, width: int, height: int) -> None:
        raise NotImplementedError

    def getPosition(self) -> SimpleNamespace:
        x, y, _w, _h = self.box()
        return SimpleNamespace(X=x, Y=y)

    def getSize(self) -> SimpleNamespace:
        _x, _y, width, height = self.box()
        return SimpleNamespace(Width=width, Height=height)


class _SimShape(_SimNode):
    def __init__(self, shape_type: str, preset: str | None = None, name: str = "") -> None:
        super().__init__(shape_type, name)
        self.text = ""
        self._box = (0, 0, 0, 0)
        if preset:
            # build_shape_tree flags a CustomShape with neither Path nor ViewBox as unpainted.
            self.props["CustomShapeGeometry"] = (
                SimpleNamespace(Name="Type", Value=preset),
                SimpleNamespace(Name="ViewBox", Value=(0, 0, 21600, 21600)),
            )

    def getString(self) -> str:
        return self.text

    def box(self) -> tuple[int, int, int, int]:
        start, end = self.props.get("StartShape"), self.props.get("EndShape")
        if start is None or end is None:
            return self._box
        # A linked connector follows its shapes; LibreOffice uses glue points, this uses centres.
        (sx, sy, sw, sh), (ex, ey, ew, eh) = start.box(), end.box()
        x0, y0, x1, y1 = sx + sw // 2, sy + sh // 2, ex + ew // 2, ey + eh // 2
        return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)

    def set_box(self, x: int, y: int, width: int, height: int) -> None:
        # A live line keeps its direction but reads back as the same bounding box.
        self._box = normalize_negative_span(x, y, width, height)


class _SimGroup(_SimNode):
    """Page or GroupShape: an XShapes container (no text, like a live GroupShape)."""

    def __init__(self, items: list[Any] | None = None) -> None:
        super().__init__("GroupShape")
        self.items: list[Any] = items or []

    def getCount(self) -> int:
        return len(self.items)

    def getByIndex(self, index: int) -> Any:
        return self.items[index]

    def box(self) -> tuple[int, int, int, int]:
        boxes = [c.box() for c in self.items]
        x = min(b[0] for b in boxes)
        y = min(b[1] for b in boxes)
        return x, y, max(b[0] + b[2] for b in boxes) - x, max(b[1] + b[3] for b in boxes) - y

    def set_box(self, x: int, y: int, width: int, height: int) -> None:
        """Move and scale the children, as LibreOffice does for a group."""
        ox, oy, ow, oh = self.box()
        x, y, width, height = normalize_negative_span(x, y, width, height)
        fx, fy = width / (ow or 1), height / (oh or 1)
        for child in self.items:
            cx, cy, cw, ch = child.box()
            child.set_box(x + round((cx - ox) * fx), y + round((cy - oy) * fy), round(cw * fx), round(ch * fy))


class DrawWorld:
    """One Draw page of in-memory shapes, read and addressed through ``plugin.draw.tree``."""

    __slots__ = ("page",)

    def __init__(self) -> None:
        self.page = _SimGroup()

    def export_for_prompt(self) -> str:
        import json

        return json.dumps(self.get_tree(), ensure_ascii=False)

    def _miss(self, err: str | None, wanted: Any) -> dict[str, Any]:
        """Lookup error payload, as ``_lookup_ref`` in plugin/draw/shapes.py builds it."""
        message = err or "Shape not found."
        if not message.startswith("No shape named"):
            return make_tool_error(message)
        names = page_shape_names(self.page)[:40]
        return make_tool_error(message, available=names, **available_miss_details(str(wanted).strip(), names))

    def shape_upsert(self, *, action: str, **args: Any) -> dict[str, Any]:
        props = _line_ends(args)
        name = str(args.get("name") or "").strip()
        taken = f"A shape named {name!r} already exists on this page; choose another name or edit that shape."
        rename_to: str | None = None
        if action == "create":
            if name and name in page_shape_names(self.page):
                return make_tool_error(taken, code="INVALID_PARAM")
            shape_type = args["shape_type"]
            shape: Any = _SimShape(*_draw_node_type(shape_type), name=name)
            address: int | str = self.page.getCount()
            self.page.items.append(shape)
            ensure_shape_name(self.page, shape)
            shape.set_box(*(args[k] for k in _BOX_KEYS))
        else:
            if "index" in args:
                found, shape, _parent, err = find_shape_on_page(self.page, index=args["index"])
            else:
                found, shape, _parent, err = find_shape_on_page(self.page, name=name)
            if shape is None or found is None:
                return self._miss(err, name)
            address = found
            if "index" in args and name and name != shape.Name:
                if name in page_shape_names(self.page):
                    return make_tool_error(taken, code="INVALID_PARAM")
                rename_to = name
            if any(k in args for k in _BOX_KEYS):
                shape.set_box(*(args.get(k, cur) for k, cur in zip(_BOX_KEYS, shape.box())))
        if rename_to is not None:
            shape.Name = rename_to
        if "text" in args:
            shape.text = str(args["text"])
        shape.props.update(props)
        result: dict[str, Any] = {"status": "ok", "action": action, "message": "Shape updated", "page": 0, "index": address, "name": shape.Name}
        if action == "create":
            result.update(message=f"Created {shape_type}", shape_count_after=self.page.getCount())
        return result

    def shape_connect(self, *, start: Any, end: Any, **args: Any) -> dict[str, Any]:
        props = _line_ends(args)
        addresses = []
        for ref, prop in ((start, "StartShape"), (end, "EndShape")):
            address, shape, _parent, err = resolve_shape_ref(self.page, ref)
            if shape is None:
                return self._miss(err, ref)
            addresses.append(address)
            props[prop] = shape
        connector = _SimShape("ConnectorShape")
        connector.props.update(props)
        self.page.items.append(connector)
        ensure_shape_name(self.page, connector)
        return {
            "status": "ok",
            "message": f"Connected shape {addresses[0]} to {addresses[1]}",
            "index": self.page.getCount() - 1,
            "name": connector.Name,
            "page": 0,
        }

    def shape_group(self, *, indices: list[Any], **_args: Any) -> dict[str, Any]:
        if len(indices) < 2:
            return make_tool_error("At least two shape indices are required to group.")
        slots: set[int] = set()
        for ref in indices:
            address, shape, parent, err = resolve_shape_ref(self.page, ref)
            if shape is None:
                return self._miss(err, ref)
            if parent is not self.page:
                return make_tool_error(f"Cannot group shape {address}: it is not a top-level shape on the page.")
            slots.add(int(address))
        if len(slots) < len(indices):
            return make_tool_error("Duplicate shape in indices; pass each shape once.")
        # SdrEditView::GroupMarked (svx/source/svdraw/svdedtv2.cxx) keeps the z-order inside the
        # group and puts the group where its topmost member was.
        members = [self.page.items[i] for i in sorted(slots)]
        group = _SimGroup(members)
        index = max(slots) - len(slots) + 1
        rest = [s for i, s in enumerate(self.page.items) if i not in slots]
        self.page.items = [*rest[:index], group, *rest[index:]]
        ensure_shape_name(self.page, group)
        return {"status": "ok", "message": f"Grouped {len(indices)} shapes.", "index": index, "name": group.Name, "page": 0}

    def get_tree(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "page": 0, "tree": build_shape_tree(self.page)}

    def get_draw_summary(self, **_kwargs: Any) -> dict[str, Any]:
        """Same rows as ``GetDrawSummary.execute`` in plugin/draw/shapes.py."""
        rows = []
        for addr, shape, _parent in iter_shape_addresses(self.page):
            x, y, width, height = shape.box()
            row: dict[str, Any] = {
                "index": addr if "." in addr else int(addr),
                "name": shape.Name,
                "type": shape.getShapeType(),
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
            preset = custom_shape_geometry(shape).get("Type")
            if preset:
                row["custom_shape_type"] = preset
            if isinstance(shape, _SimShape):
                row["text"] = shape.text
            rows.append(row)
        return {"status": "ok", "page": 0, "shapes": rows}


def _calc_sort_key(raw: Any, ascending: bool) -> tuple[int, Any]:
    """Order one Calc cell value to match ScTable::CompareCell (numbers before text ascending, negated descending, empties last)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return (2, "")
    try:
        num = float(str(raw).replace(",", "").replace("$", ""))
        if math.isnan(num):
            raise ValueError
        return (0 if ascending else 1, num if ascending else -num)
    except (TypeError, ValueError):
        text = str(raw).lower()
        if ascending:
            return (1, text)
        return (0, tuple(-ord(ch) for ch in text) + (1,))


class CalcWorld:
    """Calc grid plus formula text, write dests, and read ranges."""

    __slots__ = (
        "_grid",
        "_headers",
        "sheets",
        "formulas",
        "writes",
        "reads",
        "active_sheet",
    )

    def __init__(self, initial: str = "") -> None:
        self._grid: list[list[Any]] = []
        self._headers: list[str] = []
        self.formulas: dict[str, str] = {}
        self.writes: list[dict[str, Any]] = []
        self.reads: list[list[str]] = []
        self.active_sheet = "Sheet1"
        self.sheets: dict[str, list[list[Any]]] = {"Sheet1": self._grid}
        if initial:
            self._parse_initial(initial)
            self.sheets["Sheet1"] = self._grid

    def _parse_initial(self, text: str) -> None:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        for line in lines:
            if "\t" in line:
                row = [cell.strip() for cell in line.split("\t")]
            else:
                row = [cell.strip() for cell in line.split(",") if cell.strip()]
            if row:
                self._grid.append(row)
        if self._grid:
            self._headers = [str(c) for c in self._grid[0]]

    def export_for_prompt(self) -> str:
        import json

        return json.dumps(self.snapshot(), ensure_ascii=False)

    def get_sheet_summary(self, **kwargs: Any) -> dict[str, Any]:
        unused = kwargs
        del unused
        rows = len(self._grid)
        cols = len(self._grid[0]) if self._grid else 0
        return {
            "status": "ok",
            "sheet_name": self.active_sheet,
            "row_count": rows,
            "col_count": cols,
            "headers": self._headers,
            "grid": self._grid,
        }

    def _inspector_grid(self, rng: str) -> list[list[dict[str, Any]]]:
        """Build the same per-cell dict grid ``CellInspector.read_range`` would return (sans UNO)."""
        c0, r0, c1, r1 = parse_a1_range(rng)
        out: list[list[dict[str, Any]]] = []
        for row_i in range(r0, r1 + 1):
            row_data: list[dict[str, Any]] = []
            for col_i in range(c0, c1 + 1):
                val: Any = ""
                if row_i < len(self._grid) and col_i < len(self._grid[row_i]):
                    val = self._grid[row_i][col_i]
                addr = col_row_to_a1(col_i, row_i)
                formula = self.formulas.get(addr)
                if isinstance(formula, str) and formula.startswith("="):
                    cell_type = "formula"
                    formula_text: str | None = formula
                else:
                    formula_text = None
                    if val == "" or val is None:
                        cell_type = "empty"
                        val = None
                    elif isinstance(val, str):
                        cell_type = "text"
                    else:
                        cell_type = "value"
                cell_info: dict[str, Any] = {
                    "address": addr,
                    "value": val,
                    "formula": formula_text,
                    "type": cell_type,
                }
                row_data.append(cell_info)
            out.append(row_data)
        return out

    def read_range(self, **kwargs: Any) -> dict[str, Any]:
        ranges = kwargs["range"]
        if not ranges:
            return make_tool_error("range is required", code="INVALID_PARAM")
        range_strs = [str(r) for r in ranges]
        self.reads.append(range_strs)
        return {
            "status": "ok",
            "result": [
                {
                    "status": "ok",
                    "range": rng,
                    "result": [grid_to_rows(self._inspector_grid(rng), range_name=rng)],
                }
                for rng in range_strs
            ],
        }

    def sort_range(self, **kwargs: Any) -> dict[str, Any]:
        """Stable sort over the given range(s) (sort_column and then_by columns are 0-based within range)."""
        col_idx = kwargs["sort_column"]
        ascending = kwargs["ascending"]
        has_header = kwargs["has_header"]
        then_by = parse_then_by(kwargs.get("then_by", []))
        if then_by is None:
            return make_tool_error(
                'then_by must be a list of {"column": <integer >= 0>, "ascending": true|false} objects',
                code="INVALID_PARAM",
            )

        ranges = kwargs["range"]
        if not ranges:
            return make_tool_error("range is required", code="INVALID_PARAM")

        # Resolve and validate all ranges before mutating any cells.
        parsed_ranges: list[tuple[str, int, int, int, int, list[SortKey]]] = []
        for rng_raw in ranges:
            range_str = str(rng_raw)
            c0, r0, c1, r1 = parse_a1_range(range_str)
            width = c1 - c0 + 1
            keys: list[SortKey] = [(col_idx, ascending), *then_by]
            width_error = _sort_keys_width_error(range_str, keys, width)
            if width_error is not None:
                return make_tool_error(width_error, code="INVALID_PARAM")
            parsed_ranges.append((range_str, c0, r0, c1, r1, keys))

        results: list[str] = []
        total_sorted_rows = 0

        for range_str, c0, r0, c1, r1, keys in parsed_ranges:
            first_data_row = 1 if has_header else 0
            data_r0 = r0 + first_data_row
            data_r1 = min(r1, len(self._grid) - 1)
            if data_r1 - data_r0 < 1:
                results.append(_sort_unchanged_message(range_str, keys))
                continue

            slices = [
                [
                    self._grid[r][c] if c < len(self._grid[r]) else ""
                    for c in range(c0, c1 + 1)
                ]
                for r in range(data_r0, data_r1 + 1)
            ]
            key_cols = list(dict.fromkeys(c for c, _unused in keys))
            before = [[s[c] for s in slices] for c in key_cols]

            slices.sort(
                key=lambda row: tuple(
                    _calc_sort_key(row[c] if len(row) > c else "", a) for c, a in keys
                )
            )
            after = [[s[c] for s in slices] for c in key_cols]
            for offset, row_slice in enumerate(slices):
                grid_row = self._grid[data_r0 + offset]
                while len(grid_row) <= c1:
                    grid_row.append("")
                grid_row[c0 : c1 + 1] = row_slice
            total_sorted_rows += len(slices)

            if after == before:
                results.append(_sort_unchanged_message(range_str, keys))
            else:
                results.append(_sort_done_message(range_str, keys))

        if self._grid:
            self._headers = [str(c) if c is not None else "" for c in self._grid[0]]
        self.sheets[self.active_sheet] = self._grid
        return {
            "status": "ok",
            "message": " ".join(results),
            "sorted_rows": total_sorted_rows,
        }

    @staticmethod
    def _plan_eval_write(
        rng: str, raw_values: Any
    ) -> tuple[list[Any], tuple[int, int, int, int]] | dict[str, Any]:
        c0, r0, c1, r1 = parse_a1_range(rng)
        num_rows = r1 - r0 + 1
        num_cols = c1 - c0 + 1
        total = num_rows * num_cols
        if raw_values is None or raw_values in ("", [], "[]", "{}"):
            return ([], (c0, r0, c1, r1))

        scalar = False
        values: Any = raw_values
        if isinstance(values, str):
            parsed = _parse_json_array_string(values)
            if parsed is None:
                refusal = _delimited_text_message(rng, values, single_cell=(c0 == c1 and r0 == r1))
                if refusal:
                    return make_tool_error(refusal, code="INVALID_PARAM")
                values = [values]
                scalar = True
            else:
                values = parsed
        elif isinstance(values, (list, tuple)):
            values = list(values)
        else:
            values = [values]
            scalar = True

        rows_2d = bool(values) and isinstance(values[0], (list, tuple))
        bad_row: int | None = None
        if rows_2d:
            r_cnt, c_cnt = len(values), len(values[0])
            bad_row = _first_ragged_row(values)
            if total > 1 and (bad_row is not None or (r_cnt, c_cnt) != (num_rows, num_cols)):
                return make_tool_error(
                    _grid_mismatch_message(rng, values, num_rows, num_cols, (c0, r0), bad_row),
                    code="CALC_ERROR",
                )
        elif len(values) != total and not scalar:
            return make_tool_error(
                _values_length_mismatch_message(rng, len(values), total, num_rows, num_cols),
                code="CALC_ERROR",
            )
        if not scalar:
            cell_err = _write_values_grid_cell_error(values)
            if cell_err:
                return make_tool_error(cell_err, code="CALC_ERROR")
        if not rows_2d:
            return (values, (c0, r0, c1, r1))
        if total == 1:
            if bad_row is not None:
                return make_tool_error(
                    _grid_mismatch_message(rng, values, 1, 1, (c0, r0), bad_row),
                    code="CALC_ERROR",
                )
            c1 = c0 + c_cnt - 1
            r1 = r0 + r_cnt - 1
        return ([v for row in values for v in row], (c0, r0, c1, r1))

    def write_range(self, **kwargs: Any) -> dict[str, Any]:
        ranges = kwargs["range"]
        if not ranges:
            return make_tool_error("range is required", code="INVALID_PARAM")
        if (kwargs.get("source") or "").strip():
            return unsupported_in_eval("write_range source")
        if "values" not in kwargs:
            return make_tool_error("values is required when source is omitted", code="INVALID_PARAM")
        raw_values = kwargs["values"]
        if kwargs.get("array") and isinstance(raw_values, str) and raw_values not in ("", "[]", "{}"):
            if not raw_values.startswith("="):
                return make_tool_error(
                    "array=true needs a formula starting with '='.",
                    code="CALC_ERROR",
                )
            return unsupported_in_eval("write_range array=true")
        written = 0
        dests: list[str] = []
        plans: list[tuple[str, list[Any], tuple[int, int, int, int]]] = []
        for rng in ranges:
            plan = self._plan_eval_write(str(rng), raw_values)
            if isinstance(plan, dict):
                return plan
            flat, bounds = plan
            plans.append((str(rng), flat, bounds))
        for rng_str, flat, bounds in plans:
            dests.append(rng_str.split(":")[0].split(".")[-1])
            written += self._write_a1_range_bounds(bounds, flat, raw_values)
        recorded_values = plans[-1][1]
        formula_text = ""
        if isinstance(raw_values, str) and raw_values.lstrip().startswith("="):
            formula_text = raw_values
        self.writes.append(
            {
                "range": [str(r) for r in ranges],
                "dests": dests,
                "formula": formula_text,
                "values": recorded_values,
            }
        )
        return {"status": "ok", "message": "Wrote cell range", "written": written, "dests": dests}

    def _write_a1_range_bounds(
        self,
        bounds: tuple[int, int, int, int],
        values: list[Any],
        raw_values: Any,
    ) -> int:
        c0, r0, c1, r1 = bounds
        cells: list[tuple[int, int]] = []
        for row_i in range(r0, r1 + 1):
            for col_i in range(c0, c1 + 1):
                cells.append((row_i, col_i))
        if not cells:
            return 0
        if not values:
            for row_i, col_i in cells:
                if row_i < len(self._grid) and col_i < len(self._grid[row_i]):
                    self._grid[row_i][col_i] = ""
                self.formulas.pop(col_row_to_a1(col_i, row_i), None)
            if self._grid:
                self._headers = [str(c) if c is not None else "" for c in self._grid[0]]
            self.sheets[self.active_sheet] = self._grid
            return 0
        # Production write_range fill-down-adjusts relative A1 refs
        # (plugin.calc.formula_fill) when one formula string hits a 1-D range.
        # JSON arrays / explicit lists stay exact (and warn if a column repeats
        # one row-aligned formula).
        formula_src = raw_values if isinstance(raw_values, str) else ""
        single = values[0] if values else None
        is_formula = isinstance(single, str) and single.lstrip().startswith("=")
        if len(values) == 1 and len(cells) > 1 and is_formula:
            num_rows = r1 - r0 + 1
            num_cols = c1 - c0 + 1
            fill = expand_single_formula(str(values[0]), num_rows, num_cols)
        elif len(values) == 1 and len(cells) > 1:
            fill = values * len(cells)
        else:
            fill = values
        max_row = max(row_i for row_i, _col_i in cells)
        max_col = max(col_i for _row_i, col_i in cells)
        while len(self._grid) <= max_row:
            self._grid.append([])
        for row_i, col_i in cells:
            row = self._grid[row_i]
            while len(row) <= max_col:
                row.append("")
        for (row_i, col_i), val in zip(cells, fill):
            self._grid[row_i][col_i] = val
            addr = col_row_to_a1(col_i, row_i)
            # Prefer the per-cell fill (adjusted or exact array entry), not
            # the original pin source on every addr.
            cell_txt = val if isinstance(val, str) else ""
            if cell_txt.lstrip().startswith("="):
                self.formulas[addr] = cell_txt
            elif formula_src.lstrip().startswith("="):
                self.formulas[addr] = formula_src
            else:
                self.formulas.pop(addr, None)
        if self._grid:
            self._headers = [str(c) if c is not None else "" for c in self._grid[0]]
        self.sheets[self.active_sheet] = self._grid
        return min(len(fill), len(cells))

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "snapshot": True,
            "sheet": self.active_sheet,
            "headers": self._headers,
            "rows": self._grid,
            "grid": self._grid,
            "row_count": len(self._grid),
            "formulas": dict(self.formulas),
            "writes": list(self.writes),
            "reads": list(self.reads),
        }
