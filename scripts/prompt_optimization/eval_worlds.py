# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory Writer / Draw / Calc worlds for string-harness eval (no LibreOffice).

HTML apply/search stays on a string so existing oracles keep working. Blocks,
connections, and formula dests are parsed or recorded so style, comments,
flowchart edges, and =PY placement can be scored honestly.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from plugin.framework.errors import safe_json_loads

_COMMENT_RE = re.compile(r"\[([^\]]+)\]")
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_TAGGED_BLOCK_RE = re.compile(r"<(p|h[1-6])(\s[^>]*)?>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_HEADING_STYLE_RE = re.compile(r"^heading\s*([1-6])$", re.IGNORECASE)


def heading_level_from_style(style_name: str) -> int | None:
    compact = re.sub(r"[\s_]+", " ", (style_name or "").strip())
    m = _HEADING_STYLE_RE.match(compact)
    return int(m.group(1)) if m else None


def _restyle_tagged_block(
    tag: str,
    attrs: str,
    inner: str,
    *,
    heading_level: int | None,
    quotations: bool,
    style_name: str,
) -> str:
    unused = tag
    del unused
    if heading_level is not None:
        return f"<h{heading_level}>{inner}</h{heading_level}>"
    token = "Quotations" if quotations else style_name
    attrs = attrs or ""
    if re.search(r"data-lo-style\s*=", attrs, re.I):
        attrs = re.sub(r'data-lo-style\s*=\s*["\'][^"\']*["\']', f'data-lo-style="{token}"', attrs, flags=re.I)
    elif re.search(r"class\s*=", attrs, re.I):
        attrs = re.sub(r'class\s*=\s*["\'][^"\']*["\']', f'class="{token}"', attrs, flags=re.I)
    else:
        attrs = f'{attrs} data-lo-style="{token}"'
    return f"<p{attrs}>{inner}</p>"


def restyle_html_needle(
    html: str,
    needle: str,
    *,
    heading_level: int | None,
    quotations: bool,
    style_name: str,
    all_matches: bool,
    occurrence: int,
) -> str:
    """Apply a paragraph/heading style to search hits (string-harness HTML)."""
    raw = html or ""
    needle = needle or ""
    if not needle:
        return raw

    tagged = list(_TAGGED_BLOCK_RE.finditer(raw))
    hits = [m for m in tagged if needle in (m.group(3) or "")]
    if hits:
        chosen: list[re.Match[str]]
        if all_matches:
            chosen = hits
        elif 0 <= occurrence < len(hits):
            chosen = [hits[occurrence]]
        else:
            chosen = [hits[0]]
        result = raw
        for m in reversed(chosen):
            repl = _restyle_tagged_block(
                m.group(1),
                m.group(2) or "",
                m.group(3),
                heading_level=heading_level,
                quotations=quotations,
                style_name=style_name,
            )
            result = result[: m.start()] + repl + result[m.end() :]
        return result

    lines = raw.split("\n")
    count = 0
    out: list[str] = []
    replaced = False
    for line in lines:
        is_hit = needle in line or line.strip() == needle.strip()
        if is_hit:
            use = all_matches or (not replaced and count == occurrence)
            count += 1
            if use:
                inner = line.strip() or needle
                if heading_level is not None:
                    line = f"<h{heading_level}>{inner}</h{heading_level}>"
                else:
                    token = "Quotations" if quotations else style_name
                    line = f'<p data-lo-style="{token}">{inner}</p>'
                replaced = not all_matches
        out.append(line)
    return "\n".join(out)


def a1_to_col_row(addr: str) -> tuple[int, int]:
    """Parse A1 / Sheet1.C2 into 0-based (col, row)."""
    text = (addr or "").strip()
    if "." in text:
        text = text.split(".")[-1]
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
    """Return (c0, r0, c1, r1) inclusive from ``A1:H500`` or ``J1``."""
    raw = (rng or "").strip()
    if "." in raw:
        raw = raw.split(".")[-1]
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


def _leaf_write_count(values: list[Any]) -> int:
    n = 0
    for item in values:
        n += len(item) if isinstance(item, list) else 1
    return n


def _write_values_length_mismatch(rng: str, values: list[Any]) -> str:
    """Fail loud when a JSON/list write does not cover every cell (no zip-truncate)."""
    n_cells = range_cell_count(rng)
    if n_cells <= 0:
        return ""
    n_vals = _leaf_write_count(values)
    if n_vals == 0:
        return ""  # empty array clears, same as production
    # Single scalar fills the whole range — same as production write_formula_range.
    if n_vals == 1 and n_cells > 1:
        return ""
    if n_vals == n_cells:
        return ""
    return (
        f"Array has {n_vals} values but range {rng} has {n_cells} cells. "
        "JSON array must match range size exactly, or pass a single string to "
        "fill the whole range."
    )


def normalize_apply_content(content: Any) -> str:
    """Mirror ApplyDocumentContent list/string normalization (content.py)."""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("[") and "<" in stripped:
            parsed = safe_json_loads(stripped)
            if isinstance(parsed, list):
                content = parsed
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    if isinstance(content, str):
        content = content.replace("\\n", "\n").replace("\\t", "\t")
    return content if isinstance(content, str) else ""


def _unsupported(name: str) -> dict[str, Any]:
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

    def get_document_content(self, **kwargs: Any) -> dict[str, Any]:
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        text = self._html
        if scope == "range":
            start = int(kwargs.get("start") or 0)
            end = int(kwargs.get("end") or len(text))
            start = max(0, min(start, len(text)))
            end = max(start, min(end, len(text)))
            text = text[start:end]
        elif scope == "selection":
            text = self._html
        if max_chars is not None and len(text) > int(max_chars):
            text = text[: int(max_chars)] + "\n\n[... truncated ...]"
        return {
            "status": "ok",
            "content": text,
            "length": len(text),
            "document_length": len(self._html),
        }

    def apply_document_content(self, **kwargs: Any) -> dict[str, Any]:
        content = kwargs.get("content", "")
        old_content = kwargs.get("old_content")
        target = kwargs.get("target")
        if not target and old_content is not None:
            target = "search"
        if not target:
            return {
                "status": "error",
                "message": "Provide target or old_content for search.",
            }
        if target == "search" and old_content is None:
            return {"status": "error", "message": "target='search' requires old_content."}

        content = normalize_apply_content(content)
        all_matches = bool(kwargs.get("all_matches", False))

        if target == "full_document":
            self._html = content
            self._sync_blocks()
            return {"status": "ok", "message": "Replaced entire document."}
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
        if target == "search":
            old = str(old_content).strip()
            if not old:
                return {"status": "error", "message": "old_content is empty after normalization."}
            if old not in self._html:
                if all_matches:
                    return {
                        "status": "error",
                        "message": "Replaced 0 occurrence(s). No matches found. Try a shorter substring.",
                        "replaced_count": 0,
                    }
                return {
                    "status": "error",
                    "message": "old_content not found in document. Try a shorter, unique substring.",
                    "replaced_count": 0,
                }
            if all_matches:
                count = self._html.count(old)
                self._html = self._html.replace(old, content)
                self._sync_blocks()
                return {
                    "status": "ok",
                    "message": "Replaced %d occurrence(s)." % count,
                    "replaced_count": count,
                }
            self._html = self._html.replace(old, content, 1)
            self._sync_blocks()
            return {
                "status": "ok",
                "message": "Replaced 1 occurrence (by old_content).",
                "replaced_count": 1,
            }
        return {"status": "error", "message": f"Unknown target: {target!r}"}

    def find_text(
        self,
        search: str,
        start: int = 0,
        limit: int | None = None,
        case_sensitive: bool = True,
    ) -> dict[str, Any]:
        if not search:
            return {"status": "error", "message": "search is required."}
        hay = self._html
        if not case_sensitive:
            hay_l = hay.lower()
            needle_l = search.lower()
        else:
            hay_l = hay
            needle_l = search
        ranges: list[dict[str, Any]] = []
        pos = max(0, start)
        while True:
            idx = hay_l.find(needle_l, pos)
            if idx == -1:
                break
            ranges.append(
                {
                    "start": idx,
                    "end": idx + len(search),
                    "text": hay[idx : idx + len(search)],
                }
            )
            pos = idx + 1
            if limit is not None and len(ranges) >= limit:
                break
        return {"status": "ok", "ranges": ranges}

    def search_in_document(self, **kwargs: Any) -> dict[str, Any]:
        """Production name. Offsets are into exported HTML (same as find_text)."""
        pattern = str(kwargs.get("pattern") or kwargs.get("search") or "")
        if not pattern:
            return {"status": "error", "message": "pattern is required."}
        start = int(kwargs.get("start") or 0)
        limit = kwargs.get("limit")
        if limit is None:
            limit = kwargs.get("max_results", 20)
        case_sensitive = bool(kwargs.get("case_sensitive", False))
        found = self.find_text(
            pattern,
            start=start,
            limit=int(limit) if limit is not None else None,
            case_sensitive=case_sensitive,
        )
        ranges = found.get("ranges") or []
        matches = [
            {
                "location": "body",
                "text": item.get("text", ""),
                "start": item.get("start"),
                "end": item.get("end"),
            }
            for item in ranges
            if isinstance(item, dict)
        ]
        found["matches"] = matches
        return found

    def add_comment(self, **kwargs: Any) -> dict[str, Any]:
        """Anchor a visible ``[comment]`` after the search hit (no UNO annotation)."""
        content = str(kwargs.get("content") or "")
        search = str(kwargs.get("search") or "")
        if not search:
            return {"status": "error", "message": "Provide search.", "comment_added": False}
        if not content:
            return {"status": "error", "message": "Provide content.", "comment_added": False}
        try:
            occurrence = int(kwargs.get("occurrence", 0) or 0)
        except (TypeError, ValueError):
            return {"status": "error", "message": "occurrence must be an integer.", "comment_added": False}
        hay = self._html
        idx = -1
        pos = 0
        for _i in range(occurrence + 1):
            idx = hay.find(search, pos)
            if idx < 0:
                break
            pos = idx + 1
        if idx < 0:
            where = (" at occurrence %d" % occurrence) if occurrence else ""
            return {
                "status": "error",
                "message": "Text '%s' not found%s." % (search, where),
                "matched": False,
                "comment_added": False,
            }
        insert = f" [{content}]"
        self._html = hay[: idx + len(search)] + insert + hay[idx + len(search) :]
        self._sync_blocks()
        return {
            "status": "ok",
            "comment_added": True,
            "matched": True,
            "anchor_text": search,
        }

    def apply_style(self, **kwargs: Any) -> dict[str, Any]:
        """Map production apply_style onto HTML tags / data-lo-style (no UNO families)."""
        style_name = str(kwargs.get("style") or "").strip()
        if not style_name:
            return {"status": "error", "message": "style is required."}
        family = str(kwargs.get("family") or "ParagraphStyles")
        if family == "CharacterStyles":
            return {
                "status": "ok",
                "message": "Character style noted (string harness is paragraph-level).",
            }
        old_content = kwargs.get("old_content")
        target = kwargs.get("target") or ("search" if old_content is not None else "selection")
        all_matches = bool(kwargs.get("all_matches", False))
        try:
            occurrence = int(kwargs.get("occurrence", 0) or 0)
        except (TypeError, ValueError):
            return {"status": "error", "message": "occurrence must be an integer."}

        heading_level = heading_level_from_style(style_name)
        quotations = style_name.casefold() == "quotations"

        if target == "full_document":
            if quotations:
                html = re.sub(
                    r'data-lo-style\s*=\s*["\']Default["\']',
                    'data-lo-style="Quotations"',
                    self._html,
                    flags=re.I,
                )
                self.set_html(html)
                return {"status": "ok", "message": "Applied Quotations to Default paragraphs."}
            return {
                "status": "error",
                "message": "full_document apply_style in the string harness needs target=search.",
            }
        if target not in ("search", "selection") or old_content is None:
            return {
                "status": "error",
                "message": "Provide target='search' and old_content for apply_style.",
            }
        needle = str(old_content).strip()
        if not needle:
            return {"status": "error", "message": "old_content is empty."}
        self.set_html(
            restyle_html_needle(
                self._html,
                needle,
                heading_level=heading_level,
                quotations=quotations,
                style_name=style_name,
                all_matches=all_matches,
                occurrence=occurrence,
            )
        )
        return {"status": "ok", "message": f"Applied {style_name}."}


class DrawWorld:
    """Draw page: shapes, z-order (list order), and connectors."""

    __slots__ = ("shapes", "connections", "groups", "_next_index")

    def __init__(self) -> None:
        self.shapes: list[dict[str, Any]] = []
        self.connections: list[dict[str, Any]] = []
        self.groups: list[list[int]] = []
        self._next_index = 0

    def export_for_prompt(self) -> str:
        import json

        return json.dumps(self.get_draw_tree(), ensure_ascii=False)

    def shape_upsert(
        self,
        action: str = "create",
        index: int | None = None,
        shape_type: str = "rectangle",
        text: str = "",
        x: int = 1000,
        y: int = 1000,
        width: int = 2000,
        height: int = 1000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        extra = dict(kwargs)
        if action == "create":
            idx = self._next_index
            self._next_index += 1
            entry = {
                "index": idx,
                "type": shape_type,
                "text": text,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "custom_shape_type": shape_type if "flowchart" in shape_type.lower() else None,
            }
            self.shapes.append(entry)
            return {
                "status": "ok",
                "message": f"Created {shape_type}",
                "index": idx,
                "page": 0,
                "shape_count_after": len(self.shapes),
            }
        if action == "edit":
            if index is None:
                return {"status": "error", "message": "index is required for edit"}
            found = next((s for s in self.shapes if s["index"] == index), None)
            if not found:
                return {"status": "error", "message": f"Shape index {index} not found"}
            if "x" in extra:
                found["x"] = extra["x"]
            if "y" in extra:
                found["y"] = extra["y"]
            if "width" in extra:
                found["width"] = extra["width"]
            if "height" in extra:
                found["height"] = extra["height"]
            if "text" in extra:
                found["text"] = extra["text"]
            return {"status": "ok", "message": "Shape updated", "page": 0}
        return {"status": "error", "message": f"Unknown action {action}"}

    def shape_connect(self, **kwargs: Any) -> dict[str, Any]:
        start = kwargs.get("start")
        end = kwargs.get("end")
        if start is None or end is None:
            return {"status": "error", "message": "start and end are required."}
        try:
            start_i = int(start)
            end_i = int(end)
        except (TypeError, ValueError):
            return {"status": "error", "message": "start and end must be integers."}
        indexes = {s["index"] for s in self.shapes}
        if start_i not in indexes or end_i not in indexes:
            return {"status": "error", "message": "Failed to find shapes at given indices."}
        label = str(kwargs.get("label") or kwargs.get("text") or "")
        entry: dict[str, Any] = {"from_index": start_i, "to_index": end_i}
        if label:
            entry["label"] = label
        self.connections.append(entry)
        return {
            "status": "ok",
            "message": f"Connected shape {start_i} to {end_i}",
            "index": start_i,
        }

    def shape_group(self, **kwargs: Any) -> dict[str, Any]:
        raw = kwargs.get("indexes") or kwargs.get("indices") or []
        if not isinstance(raw, list) or len(raw) < 2:
            return {"status": "error", "message": "indexes must be a list of at least two shape indexes."}
        try:
            idxs = [int(i) for i in raw]
        except (TypeError, ValueError):
            return {"status": "error", "message": "indexes must be integers."}
        self.groups.append(idxs)
        return {"status": "ok", "message": f"Grouped {len(idxs)} shapes", "indexes": idxs}

    def _shape_by_index(self, idx: int) -> dict[str, Any] | None:
        return next((s for s in self.shapes if s["index"] == idx), None)

    def get_draw_tree(self, **kwargs: Any) -> dict[str, Any]:
        unused = kwargs
        del unused
        by_idx = {s["index"]: s for s in self.shapes}
        starts: dict[int, list[int]] = {}
        ends: dict[int, list[int]] = {}
        for conn in self.connections:
            starts.setdefault(conn["from_index"], []).append(conn["to_index"])
            ends.setdefault(conn["to_index"], []).append(conn["from_index"])
        tree = []
        for s in self.shapes:
            node: dict[str, Any] = {
                "type": s["type"],
                "name": f"shape_{s['index']}",
                "text": s.get("text", ""),
                "geometry": {
                    "x": s["x"],
                    "y": s["y"],
                    "width": s["width"],
                    "height": s["height"],
                },
            }
            if s.get("custom_shape_type"):
                node["custom_shape_type"] = s["custom_shape_type"]
            dests = starts.get(s["index"]) or []
            srcs = ends.get(s["index"]) or []
            if dests:
                other = by_idx.get(dests[0])
                if other:
                    node["connected_end"] = {
                        "name": f"shape_{other['index']}",
                        "text": other.get("text", ""),
                    }
            if srcs:
                other = by_idx.get(srcs[0])
                if other:
                    node["connected_start"] = {
                        "name": f"shape_{other['index']}",
                        "text": other.get("text", ""),
                    }
            tree.append(node)
        children: list[dict[str, Any]] = []
        for g in self.groups:
            children.append({"type": "group", "indexes": list(g)})
        return {
            "status": "ok",
            "page_index": 0,
            "tree": tree,
            "connections": list(self.connections),
            "children": children,
        }

    def get_draw_summary(self, **kwargs: Any) -> dict[str, Any]:
        unused = kwargs
        del unused
        return {
            "status": "ok",
            "page_index": 0,
            "shapes": [
                {
                    "index": s["index"],
                    "type": s["type"],
                    "x": s["x"],
                    "y": s["y"],
                    "width": s["width"],
                    "height": s["height"],
                    "text": s.get("text", ""),
                }
                for s in self.shapes
            ],
            "connections": list(self.connections),
        }


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

    def read_cell_range(self, **kwargs: Any) -> dict[str, Any]:
        ranges = kwargs.get("range") or []
        if isinstance(ranges, str):
            ranges = [ranges]
        if not ranges:
            return {"status": "error", "message": "range is required"}
        range_strs = [str(r) for r in ranges]
        self.reads.append(range_strs)
        cells: list[dict[str, Any]] = []
        for rng in range_strs:
            c0, r0, c1, r1 = parse_a1_range(rng)
            for row_i in range(r0, r1 + 1):
                for col_i in range(c0, c1 + 1):
                    val: Any = ""
                    if row_i < len(self._grid) and col_i < len(self._grid[row_i]):
                        val = self._grid[row_i][col_i]
                    addr = col_row_to_a1(col_i, row_i)
                    cells.append(
                        {
                            "address": addr,
                            "value": val,
                            "formula": self.formulas.get(addr, ""),
                        }
                    )
        return {
            "status": "ok",
            "range": range_strs,
            "cells": cells,
            "cell_count": len(cells),
        }

    def sort_range(self, **kwargs: Any) -> dict[str, Any]:
        """One-column sort (production sort_column is a 0-based int). Stable."""
        if not self._grid or len(self._grid) < 2:
            return {"status": "ok", "message": "Nothing to sort"}
        col = kwargs.get("sort_column", 0)
        ascending = kwargs.get("ascending", True)
        has_header = kwargs.get("has_header", True)
        if isinstance(ascending, str):
            ascending = ascending.strip().lower() not in {"false", "0", "no"}
        if isinstance(has_header, str):
            has_header = has_header.strip().lower() not in {"false", "0", "no"}
        if isinstance(col, str) and not str(col).strip().lstrip("-").isdigit():
            col_name = str(col)
            col_idx = self._headers.index(col_name) if col_name in self._headers else 0
        else:
            try:
                col_idx = int(col)
            except (TypeError, ValueError):
                col_idx = 0
            col_name = (
                self._headers[col_idx] if 0 <= col_idx < len(self._headers) else str(col_idx)
            )
        header = [self._grid[0]] if has_header else []
        data_rows = self._grid[1:] if has_header else list(self._grid)

        def _key(row: list[Any]) -> tuple[int, float | str]:
            raw = row[col_idx] if row and len(row) > col_idx else ""
            try:
                num = float(str(raw).replace(",", "").replace("$", ""))
                return (0, num if ascending else -num)
            except (TypeError, ValueError):
                return (1, str(raw))

        data_rows.sort(key=_key)
        self._grid = header + data_rows
        self.sheets[self.active_sheet] = self._grid
        return {
            "status": "ok",
            "message": f"Sorted by column {col_idx} ({col_name})",
            "sorted_rows": len(data_rows),
        }

    def write_cell_range(self, **kwargs: Any) -> dict[str, Any]:
        return self.write_formula_range(**kwargs)

    def write_formula_range(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("values", [])
        raw_values = values
        if isinstance(values, str):
            parsed = safe_json_loads(values)
            if isinstance(parsed, list):
                values = parsed
            elif values:
                values = [values]
            else:
                values = []
        if not isinstance(values, list):
            values = [values]
        ranges = kwargs.get("range") or []
        if isinstance(ranges, str):
            ranges = [ranges]
        written = 0
        dests: list[str] = []
        if ranges:
            for rng in ranges:
                mismatch = _write_values_length_mismatch(str(rng), values)
                if mismatch:
                    return {"status": "error", "message": mismatch}
                dests.append(str(rng).split(":")[0].split(".")[-1])
                written += self._write_a1_range(str(rng), values, raw_values)
        elif self._grid and values:
            for i, row in enumerate(self._grid[1:]):
                if i < len(values):
                    if len(row) < 3:
                        row.extend([0] * (3 - len(row)))
                    row[2] = values[i]
                    written += 1
            if self._headers and len(self._headers) < 3:
                self._headers.extend([""] * (3 - len(self._headers)))
        formula_text = ""
        if isinstance(raw_values, str) and raw_values.lstrip().startswith("="):
            formula_text = raw_values
        self.writes.append(
            {
                "range": [str(r) for r in ranges] if ranges else [],
                "dests": dests,
                "formula": formula_text,
                "values": values,
            }
        )
        return {"status": "ok", "message": "Wrote cell range", "written": written, "dests": dests}

    def _write_a1_range(self, rng: str, values: list[Any], raw_values: Any) -> int:
        c0, r0, c1, r1 = parse_a1_range(rng)
        cells: list[tuple[int, int]] = []
        for row_i in range(r0, r1 + 1):
            for col_i in range(c0, c1 + 1):
                cells.append((row_i, col_i))
        if not cells:
            return 0
        if len(values) == 1 and len(cells) > 1:
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
        formula_src = raw_values if isinstance(raw_values, str) else ""
        for (row_i, col_i), val in zip(cells, fill):
            self._grid[row_i][col_i] = val
            addr = col_row_to_a1(col_i, row_i)
            if formula_src.lstrip().startswith("="):
                self.formulas[addr] = formula_src
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


# Back-compat names used across the harness and tests.
StringDocState = WriterWorld
DrawDocState = DrawWorld
CalcStringState = CalcWorld
