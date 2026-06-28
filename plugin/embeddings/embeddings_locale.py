# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document and run-level BCP-47 locale for embeddings prose sentence breaking (no UNO)."""
from __future__ import annotations

import logging
import os
import zipfile
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as ET

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element  # nosemgrep: use-defused-xml  # type hints only; parse via defusedxml above

from plugin.embeddings.embeddings_fs import LocaleTextRun

log = logging.getLogger(__name__)

OFFICE_NS = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
STYLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
FO_NS = "{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
XML_NS = "{http://www.w3.org/XML/1998/namespace}"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_LANGDETECT_SAMPLE_MAX = 8000


def _normalize_locale_tag(raw: str | None) -> str | None:
    from plugin.writer.locale.grammar_proofread_locale import normalize_detected_bcp47

    return normalize_detected_bcp47(raw)


def _uno_shim_from_lang_country(lang: str | None, country: str | None) -> Any:
    return SimpleNamespace(Language=str(lang or "").strip(), Country=str(country or "").strip().upper())


def _bcp47_from_lang_country(lang: str | None, country: str | None) -> str | None:
    from plugin.writer.locale.grammar_proofread_locale import normalize_uno_locale_to_bcp47

    shim = _uno_shim_from_lang_country(lang, country)
    if not shim.Language:
        return None
    return normalize_uno_locale_to_bcp47(shim)


def _odf_locale_from_styles_root(root: Element) -> str | None:
    paragraph_props: Element | None = None
    text_props: Element | None = None
    for default_style in root.iter(f"{STYLE_NS}default-style"):
        family = default_style.get(f"{STYLE_NS}family")
        props = default_style.find(f"{STYLE_NS}text-properties")
        if props is None:
            continue
        if family == "paragraph":
            paragraph_props = props
        elif family == "text":
            text_props = props
    props = paragraph_props if paragraph_props is not None else text_props
    if props is None:
        return None
    return _bcp47_from_lang_country(props.get(f"{FO_NS}language"), props.get(f"{FO_NS}country"))


def _odf_locale_from_meta_root(root: Element) -> str | None:
    for el in root.iter(f"{DC_NS}language"):
        text = (el.text or "").strip()
        if text:
            return _normalize_locale_tag(text)
    return None


def _read_zip_member(zf: zipfile.ZipFile, member: str) -> Element | None:
    try:
        return ET.fromstring(zf.read(member))
    except (KeyError, ET.ParseError, OSError):
        return None


def _odf_locale_from_zip(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            styles_root = _read_zip_member(zf, "styles.xml")
            if styles_root is not None:
                tag = _odf_locale_from_styles_root(styles_root)
                if tag:
                    return tag
            meta_root = _read_zip_member(zf, "meta.xml")
            if meta_root is not None:
                return _odf_locale_from_meta_root(meta_root)
    except (OSError, zipfile.BadZipFile):
        log.debug("odf locale read failed for %s", path, exc_info=True)
    return None


def _odf_locale_from_fodt(path: str) -> str | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        log.debug("fodt locale read failed for %s", path, exc_info=True)
        return None
    if root is None:
        return None
    tag = _odf_locale_from_styles_root(root)
    if tag:
        return tag
    return _odf_locale_from_meta_root(root)


def _docx_locale_from_core(zf: zipfile.ZipFile) -> str | None:
    root = _read_zip_member(zf, "docProps/core.xml")
    if root is None:
        return None
    return _odf_locale_from_meta_root(root)


def _docx_locale_from_styles(zf: zipfile.ZipFile) -> str | None:
    root = _read_zip_member(zf, "word/styles.xml")
    if root is None:
        return None
    doc_defaults = root.find(f"{W_NS}docDefaults")
    if doc_defaults is not None:
        r_pr = doc_defaults.find(f"{W_NS}rPrDefault")
        if r_pr is not None:
            r_pr = r_pr.find(f"{W_NS}rPr")
        if r_pr is not None:
            lang_el = r_pr.find(f"{W_NS}lang")
            if lang_el is not None:
                val = (lang_el.get(f"{W_NS}val") or "").strip()
                if val:
                    return _normalize_locale_tag(val)
    for style in root.iter(f"{W_NS}style"):
        if style.get(f"{W_NS}type") != "paragraph":
            continue
        if style.get(f"{W_NS}default") != "1":
            continue
        r_pr = style.find(f"{W_NS}rPr")
        if r_pr is None:
            continue
        lang_el = r_pr.find(f"{W_NS}lang")
        if lang_el is not None:
            val = (lang_el.get(f"{W_NS}val") or "").strip()
            if val:
                return _normalize_locale_tag(val)
    return None


def _docx_locale_from_settings(zf: zipfile.ZipFile) -> str | None:
    root = _read_zip_member(zf, "word/settings.xml")
    if root is None:
        return None
    for el in root.iter(f"{W_NS}lang"):
        val = (el.get(f"{W_NS}val") or "").strip()
        if val:
            return _normalize_locale_tag(val)
    return None


def _docx_locale_from_zip(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            for fn in (_docx_locale_from_core, _docx_locale_from_styles, _docx_locale_from_settings):
                tag = fn(zf)
                if tag:
                    return tag
    except (OSError, zipfile.BadZipFile):
        log.debug("docx locale read failed for %s", path, exc_info=True)
    return None


def _langdetect_from_sample(sample: str) -> str | None:
    text = str(sample or "").strip()
    if not text:
        return None
    if len(text) > _LANGDETECT_SAMPLE_MAX:
        text = text[:_LANGDETECT_SAMPLE_MAX]
    try:
        from plugin.contrib.langdetect import detect_langs
        from plugin.contrib.langdetect.lang_detect_exception import LangDetectException
    except ImportError:
        log.debug("contrib langdetect not available for embeddings locale")
        return None
    try:
        hits = detect_langs(text)
    except LangDetectException:
        return None
    if not hits:
        return None
    top = hits[0]
    raw = f"{top.lang}"
    if top.lang == "zh-cn":
        raw = "zh-CN"
    elif top.lang == "zh-tw":
        raw = "zh-TW"
    else:
        parts = top.lang.split("-")
        if len(parts) == 2:
            raw = f"{parts[0]}-{parts[1].upper()}"
    return _normalize_locale_tag(raw)


def resolve_document_locale_bcp47(path: str, body_sample: str | None = None) -> str | None:
    """Resolve one BCP-47 tag per indexed file for prose sentence breaking."""
    from plugin.embeddings.embeddings_fs import path_uses_prose_chunking

    if not path_uses_prose_chunking(path):
        return None

    ext = os.path.splitext(path)[1].lower()
    tag: str | None = None

    if ext in {".odt", ".ott"}:
        tag = _odf_locale_from_zip(path)
    elif ext == ".fodt":
        tag = _odf_locale_from_fodt(path)
    elif ext == ".docx":
        tag = _docx_locale_from_zip(path)
    elif ext in {".txt", ".rtf"}:
        tag = None
    else:
        tag = None

    if tag:
        return tag

    sample = body_sample
    if sample is None and ext in {".txt", ".rtf"}:
        try:
            sample = open(path, encoding="utf-8", errors="replace").read(_LANGDETECT_SAMPLE_MAX)
        except OSError:
            sample = None

    if sample:
        detected = _langdetect_from_sample(sample)
        if detected:
            return detected

    return None


def locale_runs_for_plain_passage(text: str, fallback_doc_locale: str | None = None) -> list[LocaleTextRun]:
    """Wrap one plain-text paragraph in a single run with per-paragraph langdetect."""
    passage = str(text or "")
    if not passage.strip():
        return []
    detected = _langdetect_from_sample(passage) or fallback_doc_locale
    return [LocaleTextRun(char_start=0, char_end=len(passage), locale_bcp47=detected)]


def _odf_locale_from_text_properties(props: Element | None) -> str | None:
    if props is None:
        return None
    return _bcp47_from_lang_country(props.get(f"{FO_NS}language"), props.get(f"{FO_NS}country"))


def _odf_locale_from_element(el: Element) -> str | None:
    xml_lang = (el.get(f"{XML_NS}lang") or "").strip()
    if xml_lang:
        return _normalize_locale_tag(xml_lang)
    inline_props = el.find(f"{STYLE_NS}text-properties")
    if inline_props is not None:
        tag = _odf_locale_from_text_properties(inline_props)
        if tag:
            return tag
    return None


def _odf_style_entry_locale(style_el: Element) -> str | None:
    props = style_el.find(f"{STYLE_NS}text-properties")
    return _odf_locale_from_text_properties(props)


def load_odf_style_locale_map(*roots: Element | None) -> dict[str, str]:
    """Build ``style_name -> bcp47`` from ODF styles sections (styles.xml + content.xml)."""
    style_map: dict[str, str] = {}
    parent_map: dict[str, str | None] = {}
    for root in roots:
        if root is None:
            continue
        for style_el in root.iter(f"{STYLE_NS}style"):
            name = (style_el.get(f"{STYLE_NS}name") or "").strip()
            if not name:
                continue
            parent = (style_el.get(f"{STYLE_NS}parent-style-name") or "").strip() or None
            parent_map[name] = parent
            tag = _odf_style_entry_locale(style_el)
            if tag:
                style_map[name] = tag
    resolved: dict[str, str] = dict(style_map)
    for name in parent_map:
        if name in resolved:
            continue
        current: str | None = parent_map.get(name)
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if current in style_map:
                resolved[name] = style_map[current]
                break
            current = parent_map.get(current)
    return resolved


def _locale_from_style_name(style_name: str | None, style_map: dict[str, str], doc_default: str | None) -> str | None:
    if style_name:
        tag = style_map.get(style_name.strip())
        if tag:
            return tag
    return doc_default


def _odf_note_body_paragraphs(content_root: Element) -> set[Element]:
    excluded: set[Element] = set()
    for note_body in content_root.iter(f"{TEXT_NS}note-body"):
        for p_el in note_body.iter(f"{TEXT_NS}p"):
            excluded.add(p_el)
    return excluded


def _merge_locale_text_pieces(pieces: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    merged: list[tuple[str, str | None]] = []
    for text, locale in pieces:
        if not text:
            continue
        if merged and merged[-1][1] == locale:
            prev_text, prev_locale = merged[-1]
            merged[-1] = (prev_text + text, prev_locale)
        else:
            merged.append((text, locale))
    return merged


def _passage_and_runs_from_merged_pieces(merged: list[tuple[str, str | None]], fallback: str | None) -> tuple[str, list[LocaleTextRun]]:
    runs = _locale_runs_from_merged_pieces(merged, fallback)
    passage = "".join(text for text, _locale in merged).strip()
    return passage, runs


def _locale_runs_from_merged_pieces(merged: list[tuple[str, str | None]], fallback: str | None) -> list[LocaleTextRun]:
    full_text = "".join(text for text, _locale in merged)
    passage = full_text.strip()
    if not passage:
        return []
    lead = len(full_text) - len(full_text.lstrip())
    content_end = len(full_text.rstrip())

    runs: list[LocaleTextRun] = []
    offset = 0
    for text, locale in merged:
        piece_start = offset
        piece_end = offset + len(text)
        offset = piece_end
        start = max(piece_start, lead)
        end = min(piece_end, content_end)
        if start >= end:
            continue
        if not full_text[start:end].strip():
            continue
        runs.append(
            LocaleTextRun(
                char_start=start - lead,
                char_end=end - lead,
                locale_bcp47=locale or fallback,
            )
        )

    if not runs:
        return [LocaleTextRun(char_start=0, char_end=len(passage), locale_bcp47=fallback)]
    return runs


def _odf_collect_text_and_locale(
    el: Element,
    *,
    style_map: dict[str, str],
    paragraph_style: str | None,
    doc_default: str | None,
    inherited_locale: str | None,
) -> list[tuple[str, str | None]]:
    """Return ``(text, locale)`` pieces from one ODF subtree (may recurse into spans)."""
    tag = el.tag
    if tag == f"{TEXT_NS}note-body":
        return []
    if tag == f"{TEXT_NS}s":
        count_raw = el.get(f"{TEXT_NS}c") or "1"
        try:
            count = max(1, int(count_raw))
        except ValueError:
            count = 1
        locale = _odf_locale_from_element(el) or inherited_locale or _locale_from_style_name(paragraph_style, style_map, doc_default)
        return [(" " * count, locale)]
    if tag == f"{TEXT_NS}tab":
        locale = _odf_locale_from_element(el) or inherited_locale or _locale_from_style_name(paragraph_style, style_map, doc_default)
        return [("\t", locale)]

    direct_locale = _odf_locale_from_element(el)
    style_name = (el.get(f"{TEXT_NS}style-name") or "").strip() or None
    current_locale = direct_locale or _locale_from_style_name(style_name, style_map, None) or inherited_locale or _locale_from_style_name(paragraph_style, style_map, doc_default)

    if tag == f"{TEXT_NS}span" or tag.endswith("span"):
        pieces: list[tuple[str, str | None]] = []
        if el.text:
            pieces.append((el.text, current_locale))
        for child in el:
            pieces.extend(
                _odf_collect_text_and_locale(
                    child,
                    style_map=style_map,
                    paragraph_style=paragraph_style,
                    doc_default=doc_default,
                    inherited_locale=current_locale,
                )
            )
            if child.tail:
                pieces.append((child.tail, current_locale))
        return pieces

    if el.text and tag not in {f"{TEXT_NS}p"}:
        locale = current_locale
        pieces = [(el.text, locale)]
        for child in el:
            pieces.extend(
                _odf_collect_text_and_locale(
                    child,
                    style_map=style_map,
                    paragraph_style=paragraph_style,
                    doc_default=doc_default,
                    inherited_locale=current_locale,
                )
            )
            if child.tail:
                pieces.append((child.tail, current_locale))
        return pieces

    pieces = []
    for child in el:
        pieces.extend(
            _odf_collect_text_and_locale(
                child,
                style_map=style_map,
                paragraph_style=paragraph_style,
                doc_default=doc_default,
                inherited_locale=current_locale,
            )
        )
        if child.tail:
            pieces.append((child.tail, current_locale))
    return pieces


def _odf_paragraph_to_passage_and_runs(p_el: Element, *, style_map: dict[str, str], doc_default: str | None) -> tuple[str, list[LocaleTextRun]]:
    paragraph_style = (p_el.get(f"{TEXT_NS}style-name") or "").strip() or None
    para_locale = _odf_locale_from_element(p_el) or _locale_from_style_name(paragraph_style, style_map, doc_default)

    pieces: list[tuple[str, str | None]] = []
    if p_el.text:
        pieces.append((p_el.text, para_locale))
    for child in p_el:
        pieces.extend(
            _odf_collect_text_and_locale(
                child,
                style_map=style_map,
                paragraph_style=paragraph_style,
                doc_default=doc_default,
                inherited_locale=para_locale,
            )
        )
        if child.tail:
            pieces.append((child.tail, para_locale))

    return _passage_and_runs_from_merged_pieces(_merge_locale_text_pieces(pieces), para_locale or doc_default)


def _odf_paragraph_runs_from_roots(
    content_root: Element,
    *,
    styles_root: Element | None,
    doc_default: str | None,
) -> list[tuple[str, list[LocaleTextRun]]]:
    style_map = load_odf_style_locale_map(styles_root, content_root)
    excluded = _odf_note_body_paragraphs(content_root)
    paragraphs: list[tuple[str, list[LocaleTextRun]]] = []
    for p_el in content_root.iter(f"{TEXT_NS}p"):
        if p_el in excluded:
            continue
        passage, runs = _odf_paragraph_to_passage_and_runs(p_el, style_map=style_map, doc_default=doc_default)
        if passage and runs:
            paragraphs.append((passage, runs))
    return paragraphs


def extract_odf_paragraph_runs(path: str) -> list[tuple[str, list[LocaleTextRun]]]:
    """Extract locale-tagged runs per paragraph from Writer ODF on disk."""
    ext = os.path.splitext(path)[1].lower()
    doc_default = resolve_document_locale_bcp47(path)
    try:
        if ext == ".fodt":
            root = ET.parse(path).getroot()
            if root is None:
                return []
            return _odf_paragraph_runs_from_roots(root, styles_root=root, doc_default=doc_default)
        with zipfile.ZipFile(path) as zf:
            content_root = _read_zip_member(zf, "content.xml")
            styles_root = _read_zip_member(zf, "styles.xml")
            if content_root is None:
                return []
            return _odf_paragraph_runs_from_roots(content_root, styles_root=styles_root, doc_default=doc_default)
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        log.debug("extract_odf_paragraph_runs failed for %s", path, exc_info=True)
    return []


def _docx_lang_from_r_pr(r_pr: Element | None) -> str | None:
    if r_pr is None:
        return None
    lang_el = r_pr.find(f"{W_NS}lang")
    if lang_el is None:
        return None
    val = (lang_el.get(f"{W_NS}val") or "").strip()
    if val:
        return _normalize_locale_tag(val)
    return None


def load_docx_style_locale_map(styles_root: Element | None, doc_default: str | None) -> dict[str, str]:
    """Build ``style_id -> bcp47`` from ``word/styles.xml``."""
    style_map: dict[str, str] = {}
    if styles_root is None:
        return style_map
    doc_defaults = styles_root.find(f"{W_NS}docDefaults")
    default_run_lang: str | None = doc_default
    if doc_defaults is not None:
        r_pr_default = doc_defaults.find(f"{W_NS}rPrDefault")
        if r_pr_default is not None:
            r_pr = r_pr_default.find(f"{W_NS}rPr")
            default_run_lang = _docx_lang_from_r_pr(r_pr) or doc_default
    for style_el in styles_root.iter(f"{W_NS}style"):
        style_id = (style_el.get(f"{W_NS}styleId") or "").strip()
        if not style_id:
            continue
        r_pr = style_el.find(f"{W_NS}rPr")
        tag = _docx_lang_from_r_pr(r_pr)
        if tag:
            style_map[style_id] = tag
        elif default_run_lang:
            style_map[style_id] = default_run_lang
    return style_map


def _docx_paragraph_runs_from_document(
    document_root: Element,
    *,
    style_map: dict[str, str],
    doc_default: str | None,
) -> list[tuple[str, list[LocaleTextRun]]]:
    paragraphs: list[tuple[str, list[LocaleTextRun]]] = []
    for p_el in document_root.iter(f"{W_NS}p"):
        p_pr = p_el.find(f"{W_NS}pPr")
        para_style_id: str | None = None
        if p_pr is not None:
            p_style = p_pr.find(f"{W_NS}pStyle")
            if p_style is not None:
                para_style_id = (p_style.get(f"{W_NS}val") or "").strip() or None
        para_locale = _locale_from_style_name(para_style_id, style_map, doc_default) if para_style_id else doc_default

        pieces: list[tuple[str, str | None]] = []
        for r_el in p_el.findall(f"{W_NS}r"):
            r_pr = r_el.find(f"{W_NS}rPr")
            run_locale = _docx_lang_from_r_pr(r_pr) or para_locale
            for t_el in r_el.findall(f"{W_NS}t"):
                text = t_el.text or ""
                if text:
                    pieces.append((text, run_locale))
            if r_el.find(f"{W_NS}tab") is not None:
                pieces.append(("\t", run_locale))

        passage, runs = _passage_and_runs_from_merged_pieces(_merge_locale_text_pieces(pieces), para_locale or doc_default)
        if passage and runs:
            paragraphs.append((passage, runs))
    return paragraphs


def extract_docx_paragraph_runs(path: str) -> list[tuple[str, list[LocaleTextRun]]]:
    """Extract locale-tagged runs per paragraph from DOCX on disk."""
    doc_default = resolve_document_locale_bcp47(path)
    try:
        with zipfile.ZipFile(path) as zf:
            document_root = _read_zip_member(zf, "word/document.xml")
            styles_root = _read_zip_member(zf, "word/styles.xml")
            if document_root is None:
                return []
            style_map = load_docx_style_locale_map(styles_root, doc_default)
            return _docx_paragraph_runs_from_document(document_root, style_map=style_map, doc_default=doc_default)
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        log.debug("extract_docx_paragraph_runs failed for %s", path, exc_info=True)
    return []


__all__ = [
    "LocaleTextRun",
    "extract_docx_paragraph_runs",
    "extract_odf_paragraph_runs",
    "load_docx_style_locale_map",
    "load_odf_style_locale_map",
    "locale_runs_for_plain_passage",
    "resolve_document_locale_bcp47",
]
