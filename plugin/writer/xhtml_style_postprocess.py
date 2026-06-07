# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure string/CSS post-processing of LibreOffice "XHTML Writer File" output into the
# agent-facing semantic HTML described in docs/html_style_model_plan.md (read path):
#   - named paragraph styles  -> data-lo-style="<compact token>" (no spaces: "Heading1")
#   - direct character overrides (text-* synthetic classes) -> inline style="..."
#   - synthetic paragraph-* autostyle classes (P1, P2, ...) -> name from FODT Pn->parent map
#     (when supplied), else CSS fingerprint, else omitted
#
# v1 limitations (see docs/html_style_model_plan.md#v1-limitations-shipped):
#   - Whole-paragraph Para* overrides (center, para colour, margins) are dropped on read;
#     FODT recovers the base style NAME only. Char-level overrides via text-* spans survive.
#   - Inside <table>: paragraph-* classes stripped; no data-lo-style (cell styles out of scope).
#   - Colliding compact tokens (two UNO names -> same token): token omitted on read.
#
# Post-v1: cached UNO ParaStyleName index to drop dual export and improve resolution.
# No UNO / no model access in this module — string pipeline only.
import html as _html
import re
from html.parser import HTMLParser

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
_TRAILING_EMPTY_P_RE = re.compile(
    r"\s*<p\b[^>]*>(?:\s|&nbsp;|&#160;|&#xa0;| )*</p>\s*$", re.IGNORECASE
)


def decode_lo_css_class_suffix(suffix):
    """Reverse ODF URL-style encoding in a CSS class suffix (``Heading_20_1`` -> ``Heading 1``)."""
    return re.sub(r"_([0-9a-fA-F]{2})_", lambda m: chr(int(m.group(1), 16)), suffix)


def compact_lo_style_name(uno_name):
    """Agent-facing token: drop spaces (``Heading 1`` -> ``Heading1``)."""
    return uno_name.replace(" ", "")


_FODT_STYLE_RE = re.compile(r"<style:style\b([^>]*?)/?>", re.IGNORECASE)


def extract_autostyle_parents_from_fodt(fodt):
    """Map automatic paragraph style name (``P1``, ``P2``, ...) -> its parent named style, from a
    flat ODF (.fodt) export. The XHTML export flattens this parent away, so an autostyle's CSS
    often matches no named rule (the common case after a StarWriter HTML import); the flat ODF
    keeps ``style:parent-style-name``, and the automatic style name (``Pn``) is identical to the
    XHTML ``paragraph-Pn`` class suffix — a reliable, order-independent join. Parent values may
    still be ODF-encoded (``Text_20_body``); decode at use via ``decode_lo_css_class_suffix``.
    """
    out = {}
    for m in _FODT_STYLE_RE.finditer(fodt or ""):
        attrs = m.group(1)
        fam = re.search(r'style:family="([^"]*)"', attrs)
        if not fam or fam.group(1) != "paragraph":
            continue
        name = re.search(r'style:name="([^"]*)"', attrs)
        parent = re.search(r'style:parent-style-name="([^"]*)"', attrs)
        if name and parent and _AUTOSTYLE_RE.match(name.group(1)):
            # XML attribute values are entity-escaped (a style named "A & B" -> "A &amp; B").
            out[name.group(1)] = _html.unescape(parent.group(1))
    return out


def _clean_decl_ordered(decl):
    """Cleaned declaration, original order preserved, no trailing ``;`` (for inlining)."""
    parts = [p.strip() for p in decl.split(";") if p.strip()]
    return "; ".join(parts)


def _normalize_decl(decl):
    """Order-independent fingerprint of a declaration set (for autostyle matching)."""
    parts = [p.strip() for p in decl.split(";") if p.strip()]
    return ";".join(sorted(parts))


def parse_style_block(xhtml):
    """Return ``(raw_map, norm_map)`` for every class rule in the ``<style>`` block(s).

    ``raw_map[class] = "decl; decl"`` (order preserved) is used to inline char overrides;
    ``norm_map[class] = "decl;decl"`` (sorted) is used to fingerprint autostyles.
    """
    raw_map = {}
    norm_map = {}
    for block in _STYLE_BLOCK_RE.findall(xhtml or ""):
        for name, decl in _RULE_RE.findall(block):
            raw_map[name] = _clean_decl_ordered(decl)
            norm_map[name] = _normalize_decl(decl)
    return raw_map, norm_map


def _strip_body(xhtml):
    """Return the inner HTML of ``<body>`` (or the input unchanged if there is no body)."""
    m = _BODY_RE.search(xhtml or "")
    return m.group(1).strip() if m else (xhtml or "")


def _drop_trailing_empty_paragraphs(html):
    """Drop whitespace/``&nbsp;``-only ``<p>`` blocks at the very end (LO export ghost paras)."""
    while True:
        new = _TRAILING_EMPTY_P_RE.sub("", html)
        if new == html:
            return html
        html = new


def _inject_attr(start_tag, attr):
    """Insert *attr* (e.g. ``' data-lo-style="X"'``) just before the closing ``>`` of a tag."""
    if start_tag.endswith("/>"):
        return start_tag[:-2].rstrip() + attr + "/>"
    if start_tag.endswith(">"):
        return start_tag[:-1].rstrip() + attr + ">"
    return start_tag + attr


def _attr_value(attrs, key):
    for k, v in attrs:
        if k == key:
            return v or ""
    return ""


class _SemanticTransformer(HTMLParser):
    """Rewrite LO XHTML body into the agent-facing semantic HTML.

    Top-level paragraph blocks get ``data-lo-style="<compact token>"`` (named styles only;
    autostyles resolved by CSS fingerprint or omitted). Char overrides (``text-*`` classes)
    are inlined as ``style="..."``. Inside ``<table>`` the synthetic ``paragraph-*`` class is
    stripped (no leak) but no ``data-lo-style`` is emitted — cell styles are out of scope for
    v1 round-trip. Tags are re-emitted verbatim via ``get_starttag_text()``; only the
    attributes we change are rewritten with string ops.
    """

    def __init__(self, raw_map, norm_map, autostyle_parents=None):
        super().__init__(convert_charrefs=False)
        self._raw = raw_map
        self._autostyle_parents = autostyle_parents or {}
        # Map a named paragraph rule's fingerprint -> list of its compact tokens.
        self._named_fingerprint = {}
        # Map a compact token -> set of distinct UNO names that compact to it. When more than
        # one named style produces the same token (e.g. "Heading 1" and a literal "Heading1"),
        # the token is AMBIGUOUS and must not be emitted — see _colliding_tokens below.
        token_names = {}
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
        self._table_depth = 0
        self._out = []

    def _paragraph_token(self, suffix):
        """Compact ``data-lo-style`` token for a ``paragraph-<suffix>`` class, or ``None``.

        For autostyles (``paragraph-Pn``) the flat-ODF parent map (when supplied) is
        authoritative: the XHTML export flattens the parent away, so the autostyle CSS often
        matches no named rule (the common case after a StarWriter HTML import), but the .fodt
        export keeps the parent and ``Pn`` is the same name in both exports — so the real style
        name is recovered. CSS fingerprint is the fallback when there is no FODT map.

        KNOWN LIMITATION (v1): the whole-paragraph DIRECT *overrides* themselves (alignment,
        margins, whole-paragraph colour baked into the autostyle CSS) are still dropped — only
        the style NAME is recovered, and character-level overrides survive via inline ``text-*``
        spans. Whole-paragraph Para* overrides do not round-trip (the write path cannot restore
        Para* when applying a named style).
        """
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

    def _rewrite_block(self, raw, attrs):
        classes = _attr_value(attrs, "class")
        para_classes = _PARA_CLASS_RE.findall(classes)
        token = None
        if para_classes and self._table_depth == 0:
            suffix = para_classes[0][len("paragraph-"):]
            token = self._paragraph_token(suffix)
        # Strip every paragraph-* class (top-level or cell) so it never leaks to the agent.
        raw = _strip_class_names(raw, _PARA_CLASS_RE)
        if token:
            raw = _inject_attr(raw, ' data-lo-style="%s"' % token)
        return raw

    def _rewrite_span(self, raw, attrs):
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
        return _inject_attr(raw, ins) if ins else raw

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text() or ("<%s>" % tag)
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
            self._out.append(raw)
        elif t in BLOCK_TAGS:
            self._out.append(self._rewrite_block(raw, attrs))
        elif t == "span":
            self._out.append(self._rewrite_span(raw, attrs))
        else:
            self._out.append(raw)

    def handle_startendtag(self, tag, attrs):
        self._out.append(self.get_starttag_text() or ("<%s/>" % tag))

    def handle_endtag(self, tag):
        if tag.lower() == "table" and self._table_depth > 0:
            self._table_depth -= 1
        self._out.append("</%s>" % tag)

    def handle_data(self, data):
        self._out.append(data)

    def handle_entityref(self, name):
        self._out.append("&%s;" % name)

    def handle_charref(self, name):
        self._out.append("&#%s;" % name)

    def handle_comment(self, data):
        self._out.append("<!--%s-->" % data)

    def result(self):
        return "".join(self._out)


def _strip_class_names(start_tag, name_re):
    """Remove class names matching *name_re* from a start tag; drop the attr if it empties."""
    def _sub(m):
        kept = [c for c in m.group(2).split() if not name_re.fullmatch(c)]
        return (" class=%s%s%s" % (m.group(1), " ".join(kept), m.group(1))) if kept else ""

    return re.sub(r'\s+class=(["\'])(.*?)\1', _sub, start_tag)


def xhtml_to_semantic_html(full_xhtml, autostyle_parents=None):
    """Convert raw LO ``XHTML Writer File`` output to the agent-facing semantic HTML.

    Single entry point: parse the ``<style>`` block, extract the body, inline char overrides,
    emit ``data-lo-style`` compact tokens for named paragraph styles, drop trailing ghost
    paragraphs. Pure string work — fully unit-testable without LibreOffice.

    *autostyle_parents* — ``{Pn: parent_style_name}`` from ``extract_autostyle_parents_from_fodt``
    on a paired flat-ODF export. Joined by autostyle name (``paragraph-Pn`` suffix), not block
    index. Recovers the base style **name** after StarWriter write→read when CSS fingerprint
    fails; does not recover whole-paragraph Para* overrides (v1 limitation — see plan doc).
    """
    raw_map, norm_map = parse_style_block(full_xhtml)
    body = _strip_body(full_xhtml)
    tr = _SemanticTransformer(raw_map, norm_map, autostyle_parents)
    tr.feed(body)
    tr.close()
    out = _drop_trailing_empty_paragraphs(tr.result())
    return out.strip()
