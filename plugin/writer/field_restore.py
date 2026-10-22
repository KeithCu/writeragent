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
# Built from the tuple so the span regex and restore loop stay in sync.
_FIELD_SPAN_RE = re.compile(
    r'<span\b(?=[^>]*\btitle\s*=\s*["\'](%s)["\'])'
    r'(?:[^>]*/>|[^>]*>.*?</span>)' % "|".join(_EXPORTED_FIELD_TITLES),
    re.IGNORECASE | re.DOTALL,
)
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

_PAGE_NUMBER_SUBTYPE_VALS: dict[str, str] = {
    "PREV": "PREV",
    "CURRENT": "CURRENT",
    "NEXT": "NEXT",
    "0": "PREV",
    "1": "CURRENT",
    "2": "NEXT",
}


def rewrite_exported_field_spans(html: str, nonce: str | None = None) -> str:
    """Replace XHTML field spans with placeholders the HTML import will keep."""
    if not html or "title=" not in html:
        return html
    from . import format as format_mod

    field_nonce = nonce if nonce is not None else uuid.uuid4().hex[:8]

    def _repl(match: re.Match[str]) -> str:
        title = match.group(1).lower()
        span_html = match.group(0)
        props = []
        if title == "page-number":
            subtype_m = re.search(r'data-subtype=["\']([^"\']+)["\']', span_html)
            offset_m = re.search(r'data-offset=["\'](-?\d+)["\']', span_html)
            num_m = re.search(r'data-numbering-type=["\']([^"\']+)["\']', span_html)
            if subtype_m:
                st_raw = subtype_m.group(1).strip().upper()
                st_name = _PAGE_NUMBER_SUBTYPE_VALS.get(st_raw)
                if st_name is None:
                    log.warning("Unknown PageNumber SubType attribute %r, defaulting to CURRENT", subtype_m.group(1))
                    st_name = "CURRENT"
                if st_name != "CURRENT":
                    props.append(f"subtype={st_name}")
            if offset_m:
                # The group is (-?\d+), so int() cannot fail.
                off_val = int(offset_m.group(1))
                if off_val != 0:
                    props.append(f"offset={off_val}")
            if num_m:
                nt_raw = num_m.group(1).strip().upper()
                if nt_raw in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
                    nt_name = nt_raw
                elif nt_raw.isdigit() and int(nt_raw) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
                    nt_name = format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES[int(nt_raw)]
                else:
                    log.warning("Unknown PageNumber NumberingType attribute %r, defaulting to ARABIC", num_m.group(1))
                    nt_name = "ARABIC"
                if nt_name != "ARABIC":
                    props.append(f"numbering_type={nt_name}")

        prop_str = ("|" + "|".join(props)) if props else ""
        return f"[[WA-FIELD:{field_nonce}:{title}{prop_str}]]"

    return _FIELD_SPAN_RE.sub(_repl, html)


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
    if title == "page-number":
        from . import format as format_mod
        try:
            subtype_str = props.get("subtype", "CURRENT").strip().upper()
            st_name = _PAGE_NUMBER_SUBTYPE_VALS.get(subtype_str)
            if st_name is None:
                log.warning("Unknown PageNumber SubType: %r, defaulting to CURRENT", props.get("subtype"))
                st_name = "CURRENT"
            import uno
            # SubType accepts only this enum. An int is refused, and uno is
            # always importable inside LibreOffice.
            field.setPropertyValue(
                "SubType", uno.Enum("com.sun.star.text.PageNumberType", st_name),
            )
        except Exception as e:
            log.debug("_insert_restored_field failed to set SubType: %s", e)
        try:
            offset_val = int(props.get("offset", "0"))
            field.setPropertyValue("Offset", offset_val)
        except Exception as e:
            log.debug("_insert_restored_field failed to set Offset: %s", e)
        try:
            num_type_str = props.get("numbering_type", "ARABIC").strip().upper()
            if num_type_str in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
                nt_val = format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS[num_type_str]
            elif num_type_str.isdigit() and int(num_type_str) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
                nt_val = int(num_type_str)
            else:
                log.warning("Unknown PageNumber NumberingType: %r, defaulting to ARABIC", props.get("numbering_type"))
                nt_val = 4
            field.setPropertyValue("NumberingType", nt_val)
        except Exception as e:
            log.debug("_insert_restored_field failed to set NumberingType: %s", e)
    # DateTime.IsDate and Author.FullName are on the service just created.
    # A swallowed failure inserted the other form and still returned True.
    elif title == "date":
        field.setPropertyValue("IsDate", True)
    elif title == "time":
        field.setPropertyValue("IsDate", False)
    elif title == "author-name":
        # Same Author service as initials; FullName selects the display form.
        field.setPropertyValue("FullName", True)
    elif title == "author-initials":
        field.setPropertyValue("FullName", False)
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


def _cursor_over(text_obj: Any, offset: int, needle: str) -> Any | None:
    """Cursor selecting *needle* at cursor step *offset* of *text_obj*, or None."""
    cursor = text_obj.createTextCursor()
    cursor.gotoStart(False)
    if offset and cursor.goRight(offset, False) is False:
        return None
    if cursor.goRight(len(needle), True) is False:
        return None
    if cursor.getString() != needle:
        return None
    return cursor


def _parse_field_token_props(props_str: str | None) -> dict[str, str]:
    props = {}
    if props_str:
        for p in props_str.split('|'):
            if '=' in p:
                k, v = p.split('=', 1)
                props[k] = v
    return props


def _check_no_remaining_field_placeholders(text_obj: Any, prefix: str, nonce: str | None = None) -> None:
    if text_obj is None:
        return
    plain = uno_read(lambda: text_obj.getString() or "", "")
    if prefix in plain:
        match = None
        for m in _FIELD_TOKEN_RE.finditer(plain):
            if nonce is None or m.group(1) == nonce:
                match = m
                break
        tok = match.group(0) if match else "[[WA-FIELD:...]]"
        raise ToolExecutionError(
            f"The header/footer region was imported, but field placeholder {tok} remains visible in the document."
        )


def _restore_fields_by_cursor(model: Any, text_obj: Any, nonce: str | None = None) -> int:
    """Replace ``[[WA-FIELD:…]]`` tokens inside *text_obj* without document search.

    A document findFirst misses headers and footers on unused page styles (such as
    cloned styles). Walking portions addresses that text directly. A text field
    counts as one cursor step; getString() expands it, so steps come from the portion
    walk, not from getString().
    """
    prefix = f"[[WA-FIELD:{nonce}:" if nonce else "[[WA-FIELD:"
    try:
        plain = text_obj.getString() or ""
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("_restore_fields_by_cursor getString failed: %s", exc)
        return 0

    if prefix not in plain:
        return 0

    try:
        enum = text_obj.createEnumeration()
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("_restore_fields_by_cursor createEnumeration failed: %s", exc)
        return 0

    if enum is None:
        return 0

    hits: list[tuple[int, str, str, dict[str, str]]] = []
    steps = 0
    paras = 0

    try:
        while enum.hasMoreElements() is True and paras < 5000:
            paras += 1
            try:
                para = enum.nextElement()
            except Exception as exc:
                reraise_if_disposed(exc)
                break
            if paras > 1:
                # The paragraph break is one cursor step and is not in portion text.
                steps += 1
            try:
                portions = para.createEnumeration()
            except Exception as exc:
                reraise_if_disposed(exc)
                continue
            if portions is None:
                continue
            seen = 0
            while portions.hasMoreElements() is True and seen < 5000:
                seen += 1
                try:
                    portion = portions.nextElement()
                    kind = portion.getPropertyValue("TextPortionType")
                except Exception as exc:
                    reraise_if_disposed(exc)
                    break
                if kind == "TextField":
                    steps += 1
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
                        hits.append((steps + m.start(), tok, title, props))
                steps += len(chunk)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("_restore_fields_by_cursor enumeration failed: %s", exc)

    restored = 0
    for offset, token, title, props in reversed(hits):
        cursor = _cursor_over(text_obj, offset, token)
        if cursor is None:
            continue
        if props:
            _insert_restored_field(model, cursor, title, props)
        else:
            _insert_restored_field(model, cursor, title)
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
    prefix = f"[[WA-FIELD:{nonce}:" if nonce else "[[WA-FIELD:"
    if text_obj is not None:
        walked = _restore_fields_by_cursor(model, text_obj, nonce=nonce)
        plain = uno_read(lambda: text_obj.getString() or "", _FAILED)
        if plain is not _FAILED and prefix not in plain:
            return walked

    if model is None or not hasattr(model, "createSearchDescriptor"):
        _check_no_remaining_field_placeholders(text_obj, prefix, nonce)
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
        if text_obj is not None:
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

    _check_no_remaining_field_placeholders(text_obj, prefix, nonce)
    return restored
