# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-document BCP-47 locale for embeddings prose sentence breaking (no UNO)."""
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

OFFICE_NS = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
STYLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
FO_NS = "{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
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


def _odf_locale_from_styles_root(root: ET.Element) -> str | None:
    paragraph_props: ET.Element | None = None
    text_props: ET.Element | None = None
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


def _odf_locale_from_meta_root(root: ET.Element) -> str | None:
    for el in root.iter(f"{DC_NS}language"):
        text = (el.text or "").strip()
        if text:
            return _normalize_locale_tag(text)
    return None


def _read_zip_member(zf: zipfile.ZipFile, member: str) -> ET.Element | None:
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


__all__ = ["resolve_document_locale_bcp47"]
