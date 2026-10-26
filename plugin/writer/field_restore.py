# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Swap and restore XHTML field placeholders across Writer HTML imports.

LibreOffice XHTML export of Writer fields produces titled <span> tags
(e.g. <span title="page-number"/>). StarWriter import drops these spans.
These helpers rewrite exported field spans to placeholders before import and
restore them back into live UNO text fields afterwards.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.uno_context import uno_same

log = logging.getLogger("writeragent.writer")
# uno_read default where None is a real UNO result.
_FAILED: Any = object()


# XHTML export of Writer fields (body and copied header XText) is a titled
# span, e.g. ``<span title="page-number"/>``. StarWriter HTML import drops
# those spans (probed: no TextField after insert). Swap in a token the
# filter keeps, import, then replace the token with a real field.
#
# LO's XHTML filter (filter/source/xslt/odf2xhtml/export/xhtml/body.xsl)
# sets ``title`` to ``local-name()`` of the ODF field element. Full list
# LO can emit (kept here so widening the map is a one-place edit):
#   author-initials, author-name, chapter, character-count, creation-date,
#   creation-time, creator, date, description, editing-cycles,
#   editing-duration, file-name, image-count, initial-creator, keywords,
#   modification-date, modification-time, object-count, page-continuation,
#   page-count, page-number, paragraph-count, print-date, print-time,
#   printed-by, sender-city, sender-company, sender-country, sender-email,
#   sender-fax, sender-firstname, sender-initials, sender-lastname,
#   sender-phone-private, sender-phone-work, sender-position,
#   sender-postal-code, sender-state-or-province, sender-street,
#   sender-title, sheet-name, subject, table-count, time, title,
#   user-defined, word-count
#
# Restore only a letterhead-useful subset for now (page/date plus chapter,
# author, file name, doc title/subject). Extending ``_EXPORTED_FIELD_TITLES``
# / ``_FIELD_TITLE_TO_SERVICE`` is intentional and easy when a real bug
# arrives. GetReference / cross-ref is a separate loss class (different
# markup, not this XSLT ``title=`` path).
_EXPORTED_FIELD_TITLES = (
    "page-number",
    "page-count",
    "time",
    "date",
    "chapter",
    "author-name",
    "author-initials",
    "file-name",
    "title",
    "subject",
)
_TAG_BODY = r'(?:"[^"]*"|\'[^\']*\'|[^>"\'])*'
_FIELD_SPAN_OPEN_RE = re.compile(r'<span\b' + _TAG_BODY + r'>', re.IGNORECASE)
_SPAN_TAG_RE = re.compile(r'<span\b' + _TAG_BODY + r'>|</span\s*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_FIELD_TOKEN_RE = re.compile(
    r"\[\[WA-FIELD:([a-zA-Z0-9]+):([a-z-]+)(?:\|([^\]]+))?\]\]",
    re.IGNORECASE,
)
_FIELD_TITLE_TO_SERVICE = {
    "page-number": "com.sun.star.text.textfield.PageNumber",
    "page-count": "com.sun.star.text.textfield.PageCount",
    "time": "com.sun.star.text.textfield.DateTime",
    "date": "com.sun.star.text.textfield.DateTime",
    "chapter": "com.sun.star.text.textfield.Chapter",
    "author-name": "com.sun.star.text.textfield.Author",
    "author-initials": "com.sun.star.text.textfield.Author",
    "file-name": "com.sun.star.text.textfield.FileName",
    "title": "com.sun.star.text.textfield.docinfo.Title",
    "subject": "com.sun.star.text.textfield.docinfo.Subject",
}

_FIELD_FLAG_PROPERTIES: dict[str, tuple[str, bool]] = {
    "date": ("IsDate", True),
    "time": ("IsDate", False),
    "author-name": ("FullName", True),
    "author-initials": ("FullName", False),
}

_PAGE_NUMBER_SUBTYPE_VALS: dict[str, str] = {
    "PREV": "PREV",
    "CURRENT": "CURRENT",
    "NEXT": "NEXT",
    "0": "PREV",
    "1": "CURRENT",
    "2": "NEXT",
}

_MAX_NESTED_TEXT_DEPTH = 4


def _matching_span_end(html: str, start_pos: int) -> int | None:
    """Find the end offset of the matching </span> for an open span tag starting at start_pos."""
    depth = 1
    for m in _SPAN_TAG_RE.finditer(html, start_pos):
        tag_text = m.group(0)
        if tag_text.startswith("</") or tag_text.startswith("</"):
            depth -= 1
            if depth == 0:
                return m.end()
        else:
            # Open span tag. Self-closing spans do not increase depth.
            is_self_closing = tag_text[:-1].rstrip().endswith("/")
            if not is_self_closing:
                depth += 1
    return None


def _field_token(field_nonce: str, title: str, attrs: dict[str, str]) -> str:
    from . import format as format_mod

    props = []
    if title == "page-number":
        st_raw = attrs.get("data-subtype")
        if st_raw is not None:
            st_raw_upper = st_raw.strip().upper()
            st_name = _PAGE_NUMBER_SUBTYPE_VALS.get(st_raw_upper)
            if st_name is None:
                log.warning("Unknown PageNumber SubType attribute %r, defaulting to CURRENT", st_raw)
                st_name = "CURRENT"
            if st_name != "CURRENT":
                props.append(f"subtype={st_name}")
        off_raw = attrs.get("data-offset")
        if off_raw is not None:
            try:
                off_val = int(off_raw)
                if off_val != 0:
                    props.append(f"offset={off_val}")
            except ValueError:
                pass
        num_raw = attrs.get("data-numbering-type")
        if num_raw is not None:
            nt_raw = num_raw.strip().upper()
            if nt_raw in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
                nt_name = nt_raw
            elif nt_raw.isdigit() and int(nt_raw) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
                nt_name = format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES[int(nt_raw)]
            else:
                log.warning("Unknown PageNumber NumberingType attribute %r, defaulting to ARABIC", num_raw)
                nt_name = "ARABIC"
            if nt_name != "ARABIC":
                props.append(f"numbering_type={nt_name}")

    prop_str = ("|" + "|".join(props)) if props else ""
    return f"[[WA-FIELD:{field_nonce}:{title}{prop_str}]]"


def rewrite_exported_field_spans(html: str, nonce: str | None = None) -> str:
    """Replace XHTML field spans with placeholders the HTML import will keep."""
    if not html or not _FIELD_SPAN_OPEN_RE.search(html):
        return html

    field_nonce = nonce if nonce is not None else uuid.uuid4().hex[:8]

    out_chunks = []
    pos = 0

    for m in _FIELD_SPAN_OPEN_RE.finditer(html):
        if m.start() < pos:
            continue
        tag_str = m.group(0)
        # Parse attributes with lowercased names
        attrs = {}
        for attr_m in _ATTR_RE.finditer(tag_str):
            name = attr_m.group(1).lower()
            val = attr_m.group(2) if attr_m.group(2) is not None else attr_m.group(3)
            attrs[name] = val

        title = attrs.get("title", "").strip().lower()
        if title not in _EXPORTED_FIELD_TITLES:
            continue

        is_self_closing = tag_str[:-1].rstrip().endswith("/")
        if is_self_closing:
            end: int | None = m.end()
        else:
            end = _matching_span_end(html, m.end())
        if end is None:
            continue

        tok = _field_token(field_nonce, title, attrs)
        out_chunks.append(html[pos:m.start()])
        out_chunks.append(tok)
        pos = end

    out_chunks.append(html[pos:])
    return "".join(out_chunks)


def _page_number_properties(props: dict[str, str]) -> list[tuple[str, Any]]:
    from . import format as format_mod
    import uno

    subtype_str = props.get("subtype", "CURRENT").strip().upper()
    st_name = _PAGE_NUMBER_SUBTYPE_VALS.get(subtype_str)
    if st_name is None:
        log.warning("Unknown PageNumber SubType: %r, defaulting to CURRENT", props.get("subtype"))
        st_name = "CURRENT"

    offset_val = int(props.get("offset", "0"))

    num_type_str = props.get("numbering_type", "ARABIC").strip().upper()
    if num_type_str in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
        nt_val = format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS[num_type_str]
    elif num_type_str.isdigit() and int(num_type_str) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
        nt_val = int(num_type_str)
    else:
        log.warning("Unknown PageNumber NumberingType: %r, defaulting to ARABIC", props.get("numbering_type"))
        nt_val = 4

    return [
        ("SubType", uno.Enum("com.sun.star.text.PageNumberType", st_name)),
        ("Offset", offset_val),
        ("NumberingType", nt_val),
    ]


def _insert_restored_field(model: Any, text_range: Any, title: str, props: dict[str, str] | None = None) -> bool:
    service = _FIELD_TITLE_TO_SERVICE.get(title)
    if not service:
        raise ToolExecutionError("Could not restore the %s field." % title)
    try:
        field = model.createInstance(service)
    except Exception as e:
        reraise_if_disposed(e)
        log.debug("_insert_restored_field createInstance failed for %s: %s", service, e)
        raise ToolExecutionError("Could not restore the %s field." % title) from e
    props = props or {}
    pairs = (
        _page_number_properties(props)
        if title == "page-number"
        else ([_FIELD_FLAG_PROPERTIES[title]] if title in _FIELD_FLAG_PROPERTIES else [])
    )
    for name, value in pairs:
        try:
            field.setPropertyValue(name, value)
        except Exception as e:
            reraise_if_disposed(e)
            log.debug("_insert_restored_field failed to set %s for %s: %s", name, title, e)
            raise ToolExecutionError("Could not restore the %s field." % title) from e
    try:
        text = text_range.getText()
        cursor = text.createTextCursorByRange(text_range)
        placeholder = cursor.getString() or ""
        cursor.setString("")
        try:
            text.insertTextContent(cursor, field, False)
        except Exception:
            # insertTextContent failed after the token was cleared. page_set
            # does not undo the header text, so put the placeholder back.
            try:
                cursor.setString(placeholder)
            except Exception:
                log.exception(
                    "_insert_restored_field: insert failed and the %s placeholder could not be restored",
                    title,
                )
            raise
        return True
    except Exception as e:
        reraise_if_disposed(e)
        log.debug("_insert_restored_field failed title=%s", title, exc_info=True)
        raise ToolExecutionError("Could not restore the %s field." % title) from e


def _parse_field_token_props(props_str: str | None) -> dict[str, str]:
    props = {}
    if props_str:
        for p in props_str.split('|'):
            if '=' in p:
                k, v = p.split('=', 1)
                props[k] = v
    return props


def _iter_field_texts(xtext: Any, depth: int = 0):
    """Yield xtext and recurse into text tables, frames, and sections up to depth limit."""
    yield xtext
    if depth >= _MAX_NESTED_TEXT_DEPTH:
        return
    enum = uno_read(lambda: xtext.createEnumeration(), None)
    if enum is None:
        return
    while enum.hasMoreElements() is True:
        el = uno_read(lambda: enum.nextElement(), None)
        if el is None:
            break
        # Table cells have their own XText
        is_table = uno_read(lambda: el.supportsService("com.sun.star.text.TextTable"), False) is True
        if is_table:
            names: tuple[str, ...] | list[str] = uno_read(lambda: el.getCellNames(), ()) or ()
            for name in names:
                cell = uno_read(lambda: el.getCellByName(name), None)
                if cell is not None:
                    yield from _iter_field_texts(cell, depth + 1)
            continue

        # TextFrame and TextSection expose inner text via getText()
        is_frame_or_section = (
            uno_read(lambda: el.supportsService("com.sun.star.text.TextFrame"), False) is True
            or uno_read(lambda: el.supportsService("com.sun.star.text.TextSection"), False) is True
        )
        if is_frame_or_section:
            inner_text = uno_read(lambda: el.getText(), None)
            if inner_text is not None:
                yield from _iter_field_texts(inner_text, depth + 1)


def _first_remaining_field_token(text_obj: Any, nonce: str | None) -> Any:
    """Return the first remaining token as str, None if none, or _FAILED if read failed."""
    for xtext in _iter_field_texts(text_obj):
        plain = uno_read(lambda: xtext.getString() or "", _FAILED)
        if plain is _FAILED:
            return _FAILED
        if not isinstance(plain, str):
            plain = str(plain)
        for m in _FIELD_TOKEN_RE.finditer(plain):
            if nonce is None or m.group(1) == nonce:
                return m.group(0)
    return None


def _is_body_text(model: Any, text_obj: Any) -> bool:
    return uno_same(text_obj, uno_read(lambda: model.getText(), None))


def _restore_fields_by_cursor(model: Any, text_obj: Any, nonce: str | None = None) -> int:
    """Replace ``[[WA-FIELD:…]]`` tokens inside *text_obj* without document search.

    Selects tokens from portion ranges per XText, descending into table cells
    and frames so unused-style headers or nested structures are fully restored.
    """
    prefix = f"[[WA-FIELD:{nonce}:" if nonce else "[[WA-FIELD:"
    hits = []

    for xtext in _iter_field_texts(text_obj):
        plain = uno_read(lambda: xtext.getString() or "", "")
        if prefix not in plain:
            continue

        try:
            enum = xtext.createEnumeration()
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("_restore_fields_by_cursor createEnumeration failed: %s", exc)
            continue

        if enum is None:
            continue

        try:
            while enum.hasMoreElements() is True:
                try:
                    para = enum.nextElement()
                except Exception as exc:
                    reraise_if_disposed(exc)
                    break
                try:
                    portions = para.createEnumeration()
                except Exception as exc:
                    reraise_if_disposed(exc)
                    continue
                if portions is None:
                    continue
                while portions.hasMoreElements() is True:
                    try:
                        portion = portions.nextElement()
                        kind = portion.getPropertyValue("TextPortionType")
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        break
                    if kind == "TextField":
                        continue
                    try:
                        chunk = portion.getString() or ""
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        chunk = ""
                    if not isinstance(chunk, str):
                        chunk = str(chunk)
                    for m in _FIELD_TOKEN_RE.finditer(chunk):
                        if nonce is None or m.group(1) == nonce:
                            tok = m.group(0)
                            title = m.group(2).lower()
                            props = _parse_field_token_props(m.group(3))
                            cur = xtext.createTextCursorByRange(portion.getStart())
                            if cur.goRight(m.start(), False) is False:
                                continue
                            if cur.goRight(len(tok), True) is False:
                                continue
                            if cur.getString() != tok:
                                continue
                            hits.append((cur, title, props))
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("_restore_fields_by_cursor enumeration failed: %s", exc)

    restored = 0
    for cursor, title, props in reversed(hits):
        _insert_restored_field(model, cursor, title, props)
        restored += 1

    return restored


def _restore_field_placeholders(model: Any, text_obj: Any = None, nonce: str | None = None) -> int:
    """Turn ``[[WA-FIELD:…]]`` tokens back into UNO fields.

    When *text_obj* is set, walk that XText first. ``findFirst`` misses a
    header/footer on a page style no page is using (a cloned page style).
    The document search remains the path for body text, and the fallback
    when the cursor walk cannot see the token.
    """
    walked = 0
    if text_obj is not None:
        walked = _restore_fields_by_cursor(model, text_obj, nonce=nonce)
        remaining = _first_remaining_field_token(text_obj, nonce)
        if remaining is None:
            return walked

    if model is None or not hasattr(model, "createSearchDescriptor"):
        if text_obj is not None:
            tok = _first_remaining_field_token(text_obj, nonce)
            if isinstance(tok, str):
                raise ToolExecutionError(
                    f"The header/footer region was imported, but field placeholder {tok} remains visible in the document."
                )
        return walked

    restored = walked

    sd = model.createSearchDescriptor()
    sd.SearchRegularExpression = True

    nonce_pat = re.escape(nonce) if nonce else r"[a-zA-Z0-9]+"
    titles_pat = "|".join(_EXPORTED_FIELD_TITLES)
    sd.SearchString = r"\[\[WA-FIELD:" + nonce_pat + r":(?:(?:" + titles_pat + r"))(?:\|[^\]]+)?\]\]"
    found = model.findFirst(sd)

    while found is not None:
        in_region = True
        if text_obj is not None and not _is_body_text(model, text_obj):
            in_region = uno_same(found.getText(), text_obj)

        props: dict[str, str] = {}
        field_title = ""
        token = found.getString()
        if isinstance(token, str):
            match = _FIELD_TOKEN_RE.match(token)
            if match and (nonce is None or match.group(1) == nonce):
                field_title = match.group(2).lower()
                props = _parse_field_token_props(match.group(3))

        nxt = model.findNext(found.getEnd(), sd)

        if in_region and field_title:
            if props:
                _insert_restored_field(model, found, field_title, props)
            else:
                _insert_restored_field(model, found, field_title)
            restored += 1

        found = nxt

    if text_obj is not None:
        tok = _first_remaining_field_token(text_obj, nonce)
        if isinstance(tok, str):
            raise ToolExecutionError(
                f"The header/footer region was imported, but field placeholder {tok} remains visible in the document."
            )
    return restored
