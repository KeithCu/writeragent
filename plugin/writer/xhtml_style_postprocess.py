# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Pure-string post-processing for XHTML Writer File export (semantic style model)."""

from __future__ import annotations

import html as html_mod
import logging
import re
from typing import Any

log = logging.getLogger("writeragent.writer")

_AUTOSTYLE_PARA_RE = re.compile(r"^P\d+$", re.IGNORECASE)
_CSS_RULE_RE = re.compile(
    r"\.(paragraph-[A-Za-z0-9_]+|text-[A-Za-z0-9_]+)\s*\{([^}]*)\}",
    re.IGNORECASE,
)
_CLASS_ATTR_RE = re.compile(r'\bclass=(["\'])(.*?)\1', re.IGNORECASE)
_DATA_LO_STYLE_RE = re.compile(
    r'\bdata-lo-style=(["\'])(.*?)\1',
    re.IGNORECASE,
)
_BLOCK_WITH_DATA_LO_STYLE_RE = re.compile(
    r"<(p|div|h[1-6]|blockquote|li)\b([^>]*)\bdata-lo-style=(['\"])(.*?)\3",
    re.IGNORECASE,
)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)


def decode_lo_css_class_suffix(suffix: str) -> str:
    """Reverse ODF URL encoding in CSS class suffixes (_20_ → space, etc.)."""
    if not suffix:
        return suffix
    return re.sub(
        r"_([0-9a-fA-F]{2})_",
        lambda m: chr(int(m.group(1), 16)),
        suffix,
    )


def _normalize_css_decl(decl: str) -> str:
    return re.sub(r"\s+", " ", (decl or "").strip())


def extract_css_rule_map(xhtml: str) -> dict[str, str]:
    """Map CSS class name (without dot) to normalized declaration text."""
    rules: dict[str, str] = {}
    for m in _CSS_RULE_RE.finditer(xhtml or ""):
        rules[m.group(1)] = _normalize_css_decl(m.group(2))
    return rules


def _paragraph_class_token(class_token: str) -> str | None:
    if class_token.startswith("paragraph-"):
        return class_token
    return None


def _is_autostyle_paragraph_class(class_token: str) -> bool:
    suffix = class_token[len("paragraph-") :]
    return bool(_AUTOSTYLE_PARA_RE.match(suffix))


def resolve_paragraph_style_from_class(
    class_token: str,
    css_rules: dict[str, str],
) -> str | None:
    """Map a paragraph-* export class to a UNO ParaStyleName, or None to omit."""
    if not class_token or not class_token.startswith("paragraph-"):
        return None
    suffix = class_token[len("paragraph-") :]
    if not _AUTOSTYLE_PARA_RE.match(suffix):
        return decode_lo_css_class_suffix(suffix)

    auto_decl = css_rules.get(class_token)
    if not auto_decl:
        return None

    named_matches: list[str] = []
    for cls, decl in css_rules.items():
        if not cls.startswith("paragraph-") or _is_autostyle_paragraph_class(cls):
            continue
        if decl == auto_decl:
            named_matches.append(decode_lo_css_class_suffix(cls[len("paragraph-") :]))

    if len(named_matches) == 1:
        return named_matches[0]
    if len(named_matches) > 1:
        log.debug(
            "resolve_paragraph_style_from_class: ambiguous fingerprint for %s matches %s",
            class_token,
            named_matches,
        )
    return None


def _merge_inline_style(existing: str | None, added: str) -> str:
    existing = (existing or "").strip()
    added = added.strip().rstrip(";")
    if not existing:
        return added + ";" if added else ""
    if not added:
        return existing if existing.endswith(";") else existing + ";"
    base = existing.rstrip(";")
    return base + "; " + added + ";"


def _inline_text_autostyle_classes(html_fragment: str, css_rules: dict[str, str]) -> str:
    """Replace text-* classes with inline style from the export stylesheet."""

    def _replace_tag(match: re.Match[str]) -> str:
        tag = match.group(1)
        before = match.group(2)
        quote = match.group(3)
        class_val = match.group(4)
        after = match.group(5)
        tokens = class_val.split()
        kept: list[str] = []
        inline = ""
        for token in tokens:
            if token.startswith("text-") and token in css_rules:
                inline = _merge_inline_style(inline, css_rules[token])
            else:
                kept.append(token)
        attrs = before + after
        if kept:
            attrs = _CLASS_ATTR_RE.sub("", attrs, count=1)
            attrs = attrs.strip()
            class_attr = ' class="%s"' % " ".join(kept)
        else:
            attrs = _CLASS_ATTR_RE.sub("", attrs, count=1).strip()
            class_attr = ""
        style_match = _DATA_LO_STYLE_RE  # noqa: F841 — placeholder to avoid shadow; use inline style re
        style_re = re.compile(r'\bstyle=(["\'])(.*?)\1', re.IGNORECASE)
        sm = style_re.search(attrs)
        if inline:
            if sm:
                merged = _merge_inline_style(sm.group(2), inline.rstrip(";"))
                attrs = style_re.sub('style=%s%s%s' % (sm.group(1), merged, sm.group(1)), attrs, count=1)
            else:
                attrs = (attrs + ' style="%s"' % html_mod.escape(inline, quote=True)).strip()
        attrs = re.sub(r"\s+", " ", attrs).strip()
        if attrs:
            return "<%s %s%s>%s" % (tag, attrs, class_attr, match.group(6))
        if class_attr:
            return "<%s%s>%s" % (tag, class_attr, match.group(6))
        return "<%s>%s" % (tag, match.group(6))

    tag_re = re.compile(
        r"<(span|font)\b([^>]*)\bclass=(['\"])(.*?)\3([^>]*)>(.*?)</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    return tag_re.sub(_replace_tag, html_fragment)


def _transform_paragraph_tag(match: re.Match[str], css_rules: dict[str, str]) -> str:
    tag = match.group(1)
    attrs = match.group(2)
    cm = _CLASS_ATTR_RE.search(attrs)
    if not cm:
        return match.group(0)
    tokens = cm.group(2).split()
    para_tokens = [t for t in tokens if t.startswith("paragraph-")]
    other_tokens = [t for t in tokens if not t.startswith("paragraph-")]
    style_name = None
    for pt in para_tokens:
        resolved = resolve_paragraph_style_from_class(pt, css_rules)
        if resolved:
            style_name = resolved
            break
    new_attrs = _CLASS_ATTR_RE.sub("", attrs, count=1).strip()
    if style_name:
        if _DATA_LO_STYLE_RE.search(new_attrs):
            new_attrs = _DATA_LO_STYLE_RE.sub("", new_attrs).strip()
        new_attrs = (new_attrs + ' data-lo-style="%s"' % html_mod.escape(style_name, quote=True)).strip()
    if other_tokens:
        new_attrs = (new_attrs + ' class="%s"' % " ".join(other_tokens)).strip()
    new_attrs = re.sub(r"\s+", " ", new_attrs).strip()
    if new_attrs:
        return "<%s %s>%s" % (tag, new_attrs, match.group(3))
    return "<%s>%s" % (tag, match.group(3))


def transform_paragraph_tags(html_fragment: str, css_rules: dict[str, str]) -> str:
    para_re = re.compile(r"<(p|div|h[1-6]|blockquote|li)\b([^>]*)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)

    def _sub(m: re.Match[str]) -> str:
        return _transform_paragraph_tag(m, css_rules)

    return para_re.sub(_sub, html_fragment)


def postprocess_xhtml_export(xhtml: str) -> str:
    """Convert XHTML Writer File export to agent-facing HTML with data-lo-style."""
    if not xhtml or not isinstance(xhtml, str):
        return xhtml
    css_rules = extract_css_rule_map(xhtml)
    body_match = re.search(r"<body[^>]*>(.*?)</body>", xhtml, re.DOTALL | re.IGNORECASE)
    if not body_match:
        return xhtml
    body = body_match.group(1)
    body = _inline_text_autostyle_classes(body, css_rules)
    body = transform_paragraph_tags(body, css_rules)
    return body.strip()


def collect_data_lo_styles(html: str) -> tuple[str, list[str | None]]:
    """Strip data-lo-style from block tags; return cleaned HTML and styles in document order."""
    styles: list[str | None] = []

    def _repl(m: re.Match[str]) -> str:
        styles.append(m.group(4))
        attrs = m.group(2)
        attrs = _DATA_LO_STYLE_RE.sub("", attrs)
        attrs = re.sub(r"\s+", " ", attrs).strip()
        if attrs:
            return "<%s %s>" % (m.group(1), attrs)
        return "<%s>" % m.group(1)

    cleaned = _BLOCK_WITH_DATA_LO_STYLE_RE.sub(_repl, html or "")
    return cleaned, styles


def count_body_paragraphs(doc) -> int:
    """Count paragraph elements in the document body text."""
    count = 0
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if hasattr(el, "getString"):
            count += 1
    return count


def apply_paragraph_styles_in_order(doc, style_names: list[str | None], skip: int = 0) -> None:
    """Apply collected data-lo-style names to body paragraphs in order."""
    from .format import apply_paragraph_style_preserving_direct_char

    if not style_names:
        return
    text = doc.getText()
    enum = text.createEnumeration()
    style_idx = 0
    skipped = 0
    while enum.hasMoreElements() and style_idx < len(style_names):
        el = enum.nextElement()
        if not hasattr(el, "getString"):
            continue
        if skipped < skip:
            skipped += 1
            continue
        name = style_names[style_idx]
        style_idx += 1
        if not name:
            continue
        try:
            cursor = text.createTextCursorByRange(el.getStart())
            apply_paragraph_style_preserving_direct_char(doc, cursor, name)
        except Exception:
            log.debug("apply_paragraph_styles_in_order: could not apply %r", name, exc_info=True)


def preprocess_html_for_import(html: str) -> tuple[str, list[str | None]]:
    """Prepare agent HTML for StarWriter import; return HTML and paragraph styles to apply after."""
    cleaned, styles = collect_data_lo_styles(html)
    return cleaned, styles


def apply_data_lo_styles_after_import(doc, styles: list[str | None], paragraph_offset: int = 0) -> None:
    """Apply paragraph styles collected from data-lo-style attributes."""
    apply_paragraph_styles_in_order(doc, styles, skip=paragraph_offset)
