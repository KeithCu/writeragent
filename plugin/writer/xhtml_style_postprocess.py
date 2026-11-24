# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure string/CSS post-processing of LibreOffice "XHTML Writer File" output into the
# agent-facing semantic HTML described in docs/writer/html-style-model-plan.md (read path):
#   - named paragraph styles  -> data-lo-style="<compact token>" (no spaces: "Heading1")
#   - direct character overrides (text-* synthetic classes) -> inline style="..."
#   - synthetic paragraph-* autostyle classes (P1, P2, ...) -> name from FODT Pn->parent map
#     (when supplied), else CSS fingerprint, else omitted
#
# v1 limitations (see docs/writer/html-style-model-plan.md#v1-limitations-shipped):
#   - Whole-paragraph Para* overrides in data-lo-para: on write, the geometry properties
#     (alignment, margins, indent, line-height) and background-color round-trip on
#     full_document, beginning, end, position='before'/'after' block inserts, and
#     whole-paragraph selection and search replacements with Track Changes on or off;
#     paragraph-level font properties and color are read-only and arrive in warnings
#     (html_import_styles._READ_ONLY_PARA_NAMES says why). Table cells and partial
#     replacements do not apply data-lo-para. Read reports geometry, paragraph
#     font, and fo:color in data-lo-para from the flat-ODF sidecar (see
#     parse_fodt_autostyles): the XHTML CSS is flattened and cannot tell an override
#     from an inherited value.
#   - Inside <table>: paragraph-* classes stripped; no data-lo-style (cell styles out of scope).
#   - Colliding compact tokens (two UNO names -> same token): token omitted on read.
#
# Post-v1: cached UNO ParaStyleName index to drop dual export and improve resolution.
# No UNO / no model access in this module — string pipeline only.
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, final

# Block tags that carry a paragraph style (read) and that produce exactly one paragraph on
# import (write, where each consumes one positional data-lo-style slot). NOTE: <div> is
# deliberately EXCLUDED — LibreOffice never emits a paragraph style on a <div>, and on write a
# <div> is a transparent container (not its own paragraph). Treating it as a styleable block
# desynced styles (read/write asymmetry); keeping it out makes both paths agree.
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}

_AUTOSTYLE_RE = re.compile(r"^P\d+$")
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
_RULE_RE = re.compile(r"\.([A-Za-z0-9_\-]+)\s*\{([^}]*)\}")
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.DOTALL | re.IGNORECASE)
_PARA_CLASS_RE = re.compile(r"\bparagraph-[A-Za-z0-9_\-]+")
# Unicode noncharacter carried as text through the scratch copy, outside the math placeholder range (U+E000+).
DELETED_MARK = "\ufdd0"
_TRAILING_EMPTY_P_RE = re.compile(
    r"\s*<p\b(?![^>]*\bdata-lo-deleted)[^>]*>(?:\s|&nbsp;|&#160;|&#xa0;| )*</p>\s*$", re.IGNORECASE
)
_BODY_AUTOSTYLE_RE = re.compile(r"\bparagraph-P\d+(?![A-Za-z0-9_\-])")

from plugin.framework.deal_shim import deal


@deal.post(lambda result: isinstance(result, str))
def decode_lo_css_class_suffix(suffix: str) -> str:
    """Reverse ODF URL-style encoding in a CSS class suffix (``Heading_20_1`` -> ``Heading 1``)."""
    # re.sub on unbounded suffix hangs deep check.
    # crosshair: off
    # Plain str only — CrossHair LazyIntSymbolicStr is isinstance(str) but breaks re.sub.
    if type(suffix) is not str:
        return ""
    return re.sub(r"_([0-9a-fA-F]{2})_", lambda m: chr(int(m.group(1), 16)), suffix)


@deal.post(lambda result: isinstance(result, str) and " " not in result)
def compact_lo_style_name(uno_name: str) -> str:
    """Agent-facing token: drop spaces (``Heading 1`` -> ``Heading1``)."""
    # crosshair: off
    if type(uno_name) is not str:
        return ""
    return uno_name.replace(" ", "")


@deal.post(lambda result: isinstance(result, bool))
def xhtml_has_paragraph_autostyle(xhtml: str) -> bool:
    """True when body contains at least one paragraph-P<digits> autostyle class."""
    # crosshair: off
    if type(xhtml) is not str:
        return False
    match = _BODY_RE.search(xhtml)
    body = match.group(1) if match else _STYLE_BLOCK_RE.sub("", xhtml)
    return bool(_BODY_AUTOSTYLE_RE.search(body))


# Direct paragraph overrides surfaced to the agent via ``data-lo-para`` so an agent can tell an
# indented block quote from body text, or verify an indent it had just set.
#
# Read from the flat-ODF sidecar, NOT from the XHTML CSS: an autostyle's XHTML rule is FLATTENED
# (base style + override merged, with no rule emitted for the base at all), so it cannot say which
# values were hand-set — reporting it would present a style's own indent as a direct override. The
# .fodt automatic style holds exactly the overrides and nothing inherited.
#
# Geometry, paragraph-level font, and paragraph colour are reported: a .docx reused as a model
# carries its font as a whole-paragraph override, which is precisely what makes an applied style
# look like it did nothing. fo:color is included so a centred red paragraph shows its red in
# data-lo-para, not only in char spans.
#
# On write, html_import_styles._para_writers() maps each name here onto a paragraph property after
# the style is set (full_document, beginning, end, position='before'/'after' block inserts, and
# whole-paragraph selection/search writes), except the font names and color in
# html_import_styles._READ_ONLY_PARA_NAMES, which arrive in warnings. A unit test fails when a name
# added here is in neither. Table cells and partial replacements do not apply
# data-lo-para.
# Range, selection and header reads copy paragraphs into a scratch document first, so
# range_copy._COPIED_PARA_PROPERTIES must hold every UNO property a writer sets; test_every_para_writer_property_is_copied_by_range_reads pins that.
# Range and selection reads also paint the five font and colour names through range_copy._PARA_LEVEL_CHAR_PROPERTIES.
# Not emitted as style="": the import reads style="" as a character override.
# ODF attribute -> CSS name, in emit order.
_FODT_OVERRIDE_ATTRS = (
    ("fo:margin-left", "margin-left"),
    ("fo:margin-right", "margin-right"),
    ("fo:margin-top", "margin-top"),
    ("fo:margin-bottom", "margin-bottom"),
    ("fo:text-indent", "text-indent"),
    ("fo:text-align", "text-align"),
    ("fo:line-height", "line-height"),
    ("fo:background-color", "background-color"),
    ("style:font-name", "font-family"),
    ("fo:font-family", "font-family"),
    ("fo:font-size", "font-size"),
    ("fo:font-weight", "font-weight"),
    ("fo:font-style", "font-style"),
    ("fo:color", "color"),
)

# Since LibreOffice 26.2 fo:text-align "start" and "end" are the direction-relative START and
# END. Earlier versions write LEFT and RIGHT that way (pXML_Para_Adjust_Enum in
# xmloff/source/style/adjushdl.cxx), so there they mean left and right.
_PRE_26_2_TEXT_ALIGN = {"start": "left", "end": "right"}
_GENERATOR_VERSION_RE = re.compile(r"<meta:generator>[^<]*?/(\d+)\.(\d+)")

_FODT_PARA_STYLE_RE = re.compile(
    r'<style:style\b([^>]*?)style:family="paragraph"([^>]*?)(?:/>|>(.*?)</style:style>)',
    re.IGNORECASE | re.DOTALL)


def _fodt_override_css(style_block: str, text_align: dict[str, str]) -> str:
    """CSS-ish ``prop: value`` list for one flat-ODF automatic paragraph style's own attributes."""
    # crosshair: off
    parts = []
    seen = set()
    for odf_attr, css_name in _FODT_OVERRIDE_ATTRS:
        if css_name in seen:
            continue
        m = re.search(r'\b%s="([^"]*)"' % re.escape(odf_attr), style_block)
        if not m:
            continue
        value = _html.unescape(m.group(1)).strip()
        if not value:
            continue
        if css_name == "text-align":
            value = text_align.get(value, value)
        parts.append("%s:%s" % (css_name, value))
        seen.add(css_name)
    return "; ".join(parts)


def parse_fodt_autostyles(fodt: str) -> tuple[dict[str, str], dict[str, str]]:
    # crosshair: off
    # FODT autostyle extraction over unbounded XML.
    parents: dict[str, str] = {}
    overrides: dict[str, str] = {}
    text = fodt if type(fodt) is str else ""
    gen = _GENERATOR_VERSION_RE.search(text)
    is_26_2_plus = gen is not None and (int(gen.group(1)), int(gen.group(2))) >= (26, 2)
    text_align = {} if is_26_2_plus else _PRE_26_2_TEXT_ALIGN
    for m in _FODT_PARA_STYLE_RE.finditer(text):
        head = (m.group(1) or "") + (m.group(2) or "")
        name = re.search(r'style:name="([^"]*)"', head)
        if not name or not _AUTOSTYLE_RE.match(name.group(1)):
            continue
        style_name = name.group(1)
        parent = re.search(r'style:parent-style-name="([^"]*)"', head)
        if parent:
            parents[style_name] = _html.unescape(parent.group(1))
        css = _fodt_override_css(head + (m.group(3) or ""), text_align)
        if css:
            overrides[style_name] = css
    return parents, overrides


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], dict) and isinstance(result[1], dict))
def parse_style_block(xhtml: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(raw_map, norm_map)`` for every class rule in the ``<style>`` block(s).

    ``raw_map[class] = "decl; decl"`` (order preserved) is used to inline char overrides;
    ``norm_map[class] = "decl;decl"`` (sorted) is used to fingerprint autostyles.
    """
    # Style-block regex on unbounded XHTML hangs deep check.
    # crosshair: off
    raw_map = {}
    norm_map = {}
    text = xhtml if type(xhtml) is str else ""
    for block in _STYLE_BLOCK_RE.findall(text):
        for name, decl in _RULE_RE.findall(block):
            parts = [p.strip() for p in decl.split(";") if p.strip()]
            raw_map[name] = "; ".join(parts)
            norm_map[name] = ";".join(sorted(parts))
    return raw_map, norm_map


def _drop_trailing_empty_paragraphs(html: str) -> str:
    """Drop whitespace/``&nbsp;``-only ``<p>`` blocks at the very end (LO export ghost paras)."""
    # crosshair: off
    while True:
        new = _TRAILING_EMPTY_P_RE.sub("", html)
        if new == html:
            return html
        html = new


def inject_attr(start_tag: str, attr: str) -> str:
    """Insert *attr* (e.g. ``' data-lo-style="X"'``) just before the closing ``>`` of a tag."""
    # crosshair: off
    if start_tag.endswith("/>"):
        return start_tag[:-2].rstrip() + attr + "/>"
    if start_tag.endswith(">"):
        return start_tag[:-1].rstrip() + attr + ">"
    return start_tag + attr


def _attr_value(attrs: list[tuple[str, str | None]], key: str) -> str:
    # crosshair: off
    for k, v in attrs:
        if k == key:
            return v or ""
    return ""


class HtmlPassthroughParser(HTMLParser):
    _table_depth: int
    _out: list[str]

    def __init__(self) -> None:
        # crosshair: off
        HTMLParser.__init__(self, convert_charrefs=False)
        self._table_depth = 0
        self._out = []

    def handle_data(self, data: str) -> None:
        # crosshair: off
        self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        # crosshair: off
        self._out.append("&%s;" % name)

    def handle_charref(self, name: str) -> None:
        # crosshair: off
        self._out.append("&#%s;" % name)

    def handle_comment(self, data: str) -> None:
        # crosshair: off
        self._out.append("<!--%s-->" % data)


@final
class _SemanticTransformer(HtmlPassthroughParser):
    """Rewrite LO XHTML body into the agent-facing semantic HTML.

    Top-level paragraph blocks get ``data-lo-style="<compact token>"`` (named styles only;
    autostyles resolved by CSS fingerprint or omitted). Char overrides (``text-*`` classes)
    are inlined as ``style="..."``. Inside ``<table>`` the synthetic ``paragraph-*`` class is
    stripped (no leak) but no ``data-lo-style`` is emitted — cell styles are out of scope for
    v1 round-trip. Tags are re-emitted verbatim via ``get_starttag_text()``; only the
    attributes we change are rewritten with string ops.
    """

    _raw: dict[str, str]
    _autostyle_parents: dict[str, str]
    _autostyle_overrides: dict[str, str]
    _colliding_tokens: set[str]
    _norm: dict[str, str]
    _open: list[str]
    _box_next: bool
    _block_at: int

    def __init__(self, raw_map: dict[str, str], norm_map: dict[str, str], autostyle_parents: dict[str, str] | None = None, autostyle_overrides: dict[str, str] | None = None) -> None:
        # crosshair: off
        super().__init__()
        self._raw = raw_map
        self._autostyle_parents = autostyle_parents or {}
        self._autostyle_overrides = autostyle_overrides or {}
        self._open = []
        self._box_next = False
        self._block_at = 0
        # Map a named paragraph rule's fingerprint -> list of its compact tokens.
        self._named_fingerprint: dict[str, list[str]] = {}
        # Map a compact token -> set of distinct UNO names that compact to it. When more than
        # one named style produces the same token (e.g. "Heading 1" and a literal "Heading1"),
        # the token is AMBIGUOUS and must not be emitted — see _colliding_tokens below.
        token_names: dict[str, set[str]] = {}
        for cls, norm in norm_map.items():
            if not cls.startswith("paragraph-"):
                continue
            suffix = cls[len("paragraph-"):]
            if _AUTOSTYLE_RE.match(suffix):
                continue
            name = decode_lo_css_class_suffix(suffix)
            token = compact_lo_style_name(name)
            self._named_fingerprint.setdefault(norm, []).append(token)
            token_names.setdefault(token, set()).add(name)
        # Tokens produced by >1 distinct named style collide: the write path could not tell
        # them apart, so we omit (write treats a missing token as the default body style)
        # rather than emit a token that would silently resolve to the wrong style.
        self._colliding_tokens = {t for t, names in token_names.items() if len(names) > 1}
        self._norm = norm_map

    def _paragraph_token(self, suffix: str) -> str | None:
        """Compact ``data-lo-style`` token for a ``paragraph-<suffix>`` class, or ``None``.

        For autostyles (``paragraph-Pn``) the flat-ODF parent map (when supplied) is
        authoritative: the XHTML export flattens the parent away, so the autostyle CSS often
        matches no named rule (the common case after a StarWriter HTML import), but the .fodt
        export keeps the parent and ``Pn`` is the same name in both exports — so the real style
        name is recovered. CSS fingerprint is the fallback when there is no FODT map.

        Whole-paragraph direct overrides travel separately in ``data-lo-para``;
        which properties a write applies, and on which targets, is described once
        in the comment above ``_FODT_OVERRIDE_ATTRS``.
        """
        # crosshair: off
        if _AUTOSTYLE_RE.match(suffix):
            # Authoritative: the flat-ODF parent of this autostyle (Pn -> named base).
            parent = self._autostyle_parents.get(suffix)
            if parent:
                token = compact_lo_style_name(decode_lo_css_class_suffix(parent))
                if token not in self._colliding_tokens:
                    return token
            # Fallback: resolve by CSS fingerprint against named rules.
            norm = self._norm.get("paragraph-" + suffix)
            matches = self._named_fingerprint.get(norm) if norm else None
            if matches and len(matches) == 1 and matches[0] not in self._colliding_tokens:
                return matches[0]
            return None  # not uniquely resolvable -> omit (write treats absence as body)
        token = compact_lo_style_name(decode_lo_css_class_suffix(suffix))
        if token in self._colliding_tokens:
            return None  # ambiguous compact token -> omit (Issue 2)
        return token

    def _rewrite_block(self, raw: str, attrs: list[tuple[str, str | None]]) -> str:
        # crosshair: off
        classes = _attr_value(attrs, "class")
        para_classes = _PARA_CLASS_RE.findall(classes)
        token = None
        para_css = ""
        if para_classes and self._table_depth == 0:
            suffix = para_classes[0][len("paragraph-"):]
            token = self._paragraph_token(suffix)
            # Only an autostyle (Pn) carries direct overrides — a plain named-style class
            # describes the style itself — and only the flat-ODF sidecar can say which values
            # those are, so without it nothing is reported rather than a guess.
            if _AUTOSTYLE_RE.match(suffix):
                para_css = self._autostyle_overrides.get(suffix, "")
        # Strip every paragraph-* class (top-level or cell) so it never leaks to the agent.
        raw = _strip_class_names(raw)
        if token:
            raw = inject_attr(raw, ' data-lo-style="%s"' % _html.escape(token, quote=True))
        if para_css:
            # ODF values are unescaped before this (a font name can contain "). Without
            # quote=True the injected attribute closes early and the tag is not well-formed.
            raw = inject_attr(raw, ' data-lo-para="%s"' % _html.escape(para_css, quote=True))
        return raw

    def _rewrite_span(self, raw: str, attrs: list[tuple[str, str | None]]) -> str:
        # crosshair: off
        classes = _attr_value(attrs, "class")
        names = classes.split()
        text_names = [c for c in names if c.startswith("text-")]
        if not text_names:
            return raw
        css_parts = [self._raw[c] for c in text_names if c in self._raw]
        remaining = [c for c in names if not c.startswith("text-")]
        raw = re.sub(r'\s+class="[^"]*"', "", raw)
        raw = re.sub(r"\s+class='[^']*'", "", raw)
        ins = ""
        if css_parts:
            ins += ' style="%s"' % "; ".join(css_parts)
        if remaining:
            ins += ' class="%s"' % " ".join(remaining)
        return inject_attr(raw, ins) if ins else raw

    def handle_comment(self, data: str) -> None:
        # crosshair: off
        # filter/source/xslt/odf2xhtml/export/xhtml/body.xsl (draw:text-box) emits this comment
        # before the text-box div inside a frame wrapper (createDrawFrame2).
        if data.strip() == "Next 'div' was a 'draw:text-box'.":
            self._box_next = True
        super().handle_comment(data)

    def _in_frame_after_box(self) -> bool:
        # filter/source/xslt/odf2xhtml/export/xhtml/body.xsl (createDrawFrame2) has no svg:title
        # template, so a frame's title falls through as text in the wrapper after the text-box div closes.
        return bool(self._open and self._open[-1] == "after_box")

    def handle_data(self, data: str) -> None:
        # crosshair: off
        if self._in_frame_after_box():
            return
        if DELETED_MARK in data:
            self._out[self._block_at] = inject_attr(self._out[self._block_at], ' data-lo-deleted="true"')
            # A non-breaking space, as in a live blank block: StarWriter import drops an empty <p>,
            # and every later data-lo-style would land one paragraph late.
            data = data.replace(DELETED_MARK, "\xa0")
        super().handle_data(data)

    def handle_entityref(self, name: str) -> None:
        # crosshair: off
        if self._in_frame_after_box():
            return
        super().handle_entityref(name)

    def handle_charref(self, name: str) -> None:
        # crosshair: off
        if self._in_frame_after_box():
            return
        super().handle_charref(name)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # crosshair: off
        raw = self.get_starttag_text() or ("<%s>" % tag)
        t = tag.lower()
        if t in ("div", "span"):
            self._open.append("box" if self._box_next else "")
            self._box_next = False
        if t == "table":
            self._table_depth += 1
            self._out.append(raw)
        elif t in BLOCK_TAGS or (t == "div" and _PARA_CLASS_RE.search(_attr_value(attrs, "class"))):
            # A paragraph that holds a frame (an image) is exported as <div class="paragraph-X">:
            # it is one Writer paragraph, so it gets its data-lo-style like a <p>. Without it the
            # write side's positional styles skipped that paragraph and the next ones shifted.
            self._block_at = len(self._out)
            self._out.append(self._rewrite_block(raw, attrs))
        elif t == "span":
            self._out.append(self._rewrite_span(raw, attrs))
        else:
            self._out.append(raw)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # crosshair: off
        if tag.lower() in ("div", "span") and self._box_next:
            self._box_next = False
            if self._open:
                self._open[-1] = "after_box"
        self._out.append(self.get_starttag_text() or ("<%s/>" % tag))

    def handle_endtag(self, tag: str) -> None:
        # crosshair: off
        t = tag.lower()
        if t in ("div", "span") and self._open:
            closed = self._open.pop()
            if closed == "box" and self._open:
                self._open[-1] = "after_box"
        if t == "table" and self._table_depth > 0:
            self._table_depth -= 1
        self._out.append("</%s>" % tag)

    def result(self) -> str:
        # crosshair: off
        return "".join(self._out)


def _strip_class_names(start_tag: str) -> str:
    """Remove class names matching _PARA_CLASS_RE from a start tag; drop the attr if it empties."""
    # crosshair: off
    def _sub(m: Any) -> str:
        kept = [c for c in m.group(2).split() if not _PARA_CLASS_RE.fullmatch(c)]
        return (" class=%s%s%s" % (m.group(1), " ".join(kept), m.group(1))) if kept else ""

    return re.sub(r'\s+class=(["\'])(.*?)\1', _sub, start_tag)


_IMG_TITLE_TEXT_RE = re.compile(
    r'(<img\b[^>]*\balt="([^"]*)"[^>]*>(?:</p>)?)([^<]+)(?=</(?:span|div)>)'
)


def _drop_image_title_text(body: str) -> str:
    """Drop the duplicate title text LibreOffice's XHTML XSLT emits after <img>.

    filter/source/xslt/odf2xhtml/export/xhtml/body.xsl has no template for
    svg:title, so createDrawFrame2 prints the title as text after the image
    (alt holds the escaped form, body text the unescaped form).
    """
    # crosshair: off
    def _sub(match: re.Match[str]) -> str:
        if _html.unescape(match.group(2)) == _html.unescape(match.group(3)):
            return match.group(1)
        return match.group(0)

    return _IMG_TITLE_TEXT_RE.sub(_sub, body)


def xhtml_to_semantic_html(full_xhtml: str, autostyle_parents: dict[str, str] | None = None, autostyle_overrides: dict[str, str] | None = None) -> str:
    """Convert raw LO ``XHTML Writer File`` output to the agent-facing semantic HTML.

    Single entry point: parse the ``<style>`` block, extract the body, inline char overrides,
    emit ``data-lo-style`` compact tokens for named paragraph styles, drop trailing ghost
    paragraphs. Pure string work — fully unit-testable without LibreOffice.

    *autostyle_parents* — ``{Pn: parent_style_name}`` and *autostyle_overrides* —
    ``{Pn: css}``, both from ``parse_fodt_autostyles`` on a paired flat-ODF
    export, joined by autostyle name (``paragraph-Pn`` suffix), not block
    index. The parent recovers the base style name after a StarWriter
    write→read when the CSS fingerprint fails; the overrides are emitted as
    ``data-lo-para`` on autostyle paragraphs outside tables.
    """
    # crosshair: off
    raw_map, norm_map = parse_style_block(full_xhtml)
    m = _BODY_RE.search(full_xhtml or "")
    body = _drop_image_title_text(m.group(1).strip() if m else (full_xhtml or ""))
    tr = _SemanticTransformer(raw_map, norm_map, autostyle_parents, autostyle_overrides)
    tr.feed(body)
    tr.close()
    out = _drop_trailing_empty_paragraphs(tr.result())
    return out.strip()


_ODF_TAG_RE = re.compile(r"<(/?)([\w.-]+:[\w.-]+)((?:\s[^<>]*?)?)(/?)>")
_TRACKED_CHANGES_RE = re.compile(r"<text:tracked-changes\b.*?</text:tracked-changes>", re.DOTALL)
_CHANGED_REGION_RE = re.compile(
    r'<text:changed-region\b[^<>]*?\btext:id="([^"]+)"[^<>]*>(.*?)</text:changed-region>',
    re.DOTALL,
)
_DELETION_BODY_RE = re.compile(r"<text:deletion>(.*?)</text:deletion>", re.DOTALL)
_CHANGE_ID_RE = re.compile(r'text:change-id="([^"]+)"')
_XML_ID_ATTR_RE = re.compile(r'\s+xml:id="[^"]*"')
_START_VALUE_ATTR_RE = re.compile(r'\s+text:start-value="[^"]*"')


@dataclass
class _DeletedBlock:
    name: str
    attrs: str
    has_content: bool = False


@dataclass
class _OpenTag:
    name: str
    attrs: str
    live: bool = False
    struck: bool = False
    slot: int | None = None


def _deleted_blocks(deletion: str) -> list[_DeletedBlock]:
    """Return top-level ``text:p`` / ``text:h`` blocks in *deletion*.

    Tracks nesting depth across ``table:table``, ``text:p``, and ``text:h`` so
    paragraphs inside tables, annotations, notes, or text boxes are not counted.
    """
    blocks: list[_DeletedBlock] = []
    current: _DeletedBlock | None = None
    depth = 0
    prev_end = 0
    for m in _ODF_TAG_RE.finditer(deletion):
        if current is not None and deletion[prev_end:m.start()].strip():
            current.has_content = True
        prev_end = m.end()
        close, name, attrs, empty = m.groups()
        if current is not None and not close and name.startswith("draw:"):
            current.has_content = True
        if name not in ("table:table", "text:p", "text:h"):
            continue
        if close:
            depth = max(0, depth - 1)
            if not depth:
                current = None
            continue
        if name in ("text:p", "text:h") and not depth:
            block = _DeletedBlock(name, attrs)
            blocks.append(block)
            if not empty:
                current = block
        if not empty:
            depth += 1
    return blocks


def expand_tracked_deletions(fodt: str) -> str | None:
    """Split deleted breaks and mark wholly struck paragraphs in flat ODF, or return None.

    ``SwXMLExport::exportDoc`` sets ``RedlineFlags::ShowInsert`` and ``odf2xhtml``
    ``body.xsl`` has no ``text:change`` rule, so XHTML export otherwise joins
    paragraphs across a multi-paragraph deletion. A deletion of *n* top-level
    blocks produces *n* - 1 breaks, each taking the deleted block's tag and style.
    Also puts ``DELETED_MARK`` in wholly struck paragraphs: otherwise ``body.xsl``
    (``match="text:h"``) drops a heading without text, an empty ``text:p`` comes
    out as ``<p class=…/>`` without ``data-lo-style``, and
    ``_drop_trailing_empty_paragraphs`` removes a trailing one.
    """
    # crosshair: off
    regions: dict[str, list[_DeletedBlock]] = {}
    for cid, region in _CHANGED_REGION_RE.findall(fodt):
        del_match = _DELETION_BODY_RE.search(region)
        if del_match:
            blocks = _deleted_blocks(del_match.group(1))
            if blocks:
                regions[cid] = blocks
    if not regions:
        return None
    skip = iter([m.span() for m in _TRACKED_CHANGES_RE.finditer(fodt)])
    span = next(skip, None)
    out: list[str] = []
    pos = 0
    scan = 0
    changed = False
    stack: list[_OpenTag] = []

    def finish(tag: _OpenTag) -> None:
        nonlocal changed
        if tag.struck and tag.slot is not None and not tag.live:
            out[tag.slot] = DELETED_MARK
            changed = True

    for m in _ODF_TAG_RE.finditer(fodt):
        while span and m.start() >= span[1]:
            span = next(skip, None)
        if span and m.start() >= span[0]:
            scan = m.end()
            continue
        if fodt[scan:m.start()].strip():
            for t in stack:
                t.live = True
        scan = m.end()
        close, name, attrs, empty = m.groups()
        if not close and name.startswith("draw:"):
            for t in stack:
                t.live = True
        if close:
            finish(stack.pop())
            continue
        if not empty:
            if name in ("text:p", "text:h"):
                # Mark before any text:a: odf2xhtml builds an outline href from link text,
                # and a marked link would be counted by _put_outline_urls.
                out.append(fodt[pos:m.end()])
                pos = m.end()
                slot = len(out)
                out.append("")
                stack.append(_OpenTag(name, attrs, slot=slot))
            else:
                stack.append(_OpenTag(name, attrs))
            continue
        if name != "text:change":
            continue
        para = next((i for i in reversed(range(len(stack))) if stack[i].name in ("text:p", "text:h")), None)
        cid_match = _CHANGE_ID_RE.search(attrs)
        cid_blocks = regions.get(cid_match.group(1)) if cid_match else None
        if para is None or not cid_blocks:
            continue
        # A paragraph in a list item becomes a new item, so it keeps its number.
        top = para - 1 if para > 0 and stack[para - 1].name == "text:list-item" else para
        out.append(fodt[pos:m.start()] if len(cid_blocks) > 1 else fodt[pos:m.end()])
        pos = m.end()
        lead = stack[para].live
        stack[para].struck = True
        for i, block in enumerate(cid_blocks[1:], start=1):
            changed = True
            out.extend("</%s>" % t.name for t in reversed(stack[top:]))
            finish(stack[para])
            stack[para] = _OpenTag(block.name, block.attrs)
            for idx, t in enumerate(stack[top:], start=top):
                cleaned = _XML_ID_ATTR_RE.sub("", t.attrs)
                if idx != para:
                    cleaned = _START_VALUE_ATTR_RE.sub("", cleaned)
                out.append("<%s%s>" % (t.name, cleaned))
                if idx == para:
                    stack[para].slot = len(out)
                    out.append("")
            if i < len(cid_blocks) - 1 or block.has_content or lead:
                stack[para].struck = True
    return "".join(out) + fodt[pos:] if changed else None


# filter/source/xslt/odf2xhtml/export/xhtml/body.xsl skips <text:section text:display="none">;
# xmloff/source/text/XMLSectionExport.cxx writes text:display="condition" when IsVisible is false,
# which odf2xhtml leaks into HTML as literal "display:condition; ".
_HIDDEN_SECTION_DISPLAY_RE = re.compile(
    r'(<text:section\b[^<>]*?)\s+text:display="(?:none|condition)"([^<>]*>)',
)


def show_hidden_sections(fodt: str) -> str:
    """Strip ``text:display="none|condition"`` from ``<text:section>`` open tags in flat ODF."""
    return _HIDDEN_SECTION_DISPLAY_RE.sub(r"\1\2", fodt)
