# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export LibreOffice Math objects to MathML and LaTeX.

Inverse of ``math_mml_convert`` (TeX/MathML → StarMath). Canonical in-document
form stays StarMath; TeX is a serialization for chat and tools.

LibreOffice MathML filter name: ``MathML XML (Math)`` (Help: MathML 2.0 / .mml).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Iterator


from plugin.framework.uno_context import get_desktop
from plugin.writer.math.math_mml_convert import MATH_CLSID, _file_url


def _exception_message(exc: Exception) -> str:
    # UNO exceptions often have an empty str() and the text on .Message.
    name = type(exc).__name__
    raw = getattr(exc, "Message", None) or str(exc)
    text = raw.strip() if isinstance(raw, str) else ""
    return f"{name}: {text}" if text else name

log = logging.getLogger("writeragent.writer")

# API FilterName for File > Save As > MathML 2.0 (.mml). See LO Help convertfilters.
MATHML_EXPORT_FILTER = "MathML XML (Math)"
_MATH_FACTORY_URL = "private:factory/smath"

_OBJECT_REPLACEMENT = "\ufffc"
_MATH_ROOT_RE = re.compile(r"(?is)<math[\s>][\s\S]*?</math>")


@dataclass(frozen=True)
class MathExportResult:
    ok: bool
    latex: str | None
    mathml: str | None
    error_message: str | None


@dataclass(frozen=True)
class WriterMathHit:
    """One Writer formula object in document order."""

    starmath: str
    latex: str | None
    mathml: str | None
    error_message: str | None
    display_block: bool
    para_index: int


def convert_mathml_to_latex(mathml_fragment: str) -> MathExportResult:
    """Presentation MathML string → LaTeX via vendored ``mathml-to-latex``. No UNO."""
    if not mathml_fragment or not isinstance(mathml_fragment, str):
        return MathExportResult(False, None, None, "empty_mathml")
    text = mathml_fragment.strip()
    if not text.lower().startswith("<math"):
        m = _MATH_ROOT_RE.search(text)
        if m is None:
            return MathExportResult(False, None, None, "not_math_root")
        text = m.group(0)
    try:
        from mathml_to_latex import MathMLToLaTeX
    except ImportError as exc:
        return MathExportResult(False, None, None, f"mathml_to_latex_import:{exc}")
    try:
        latex = MathMLToLaTeX.convert(text)
    except Exception as exc:
        log.debug("mathml-to-latex convert failed: %s", exc, exc_info=True)
        return MathExportResult(False, None, None, _exception_message(exc))
    if not isinstance(latex, str) or not latex.strip():
        return MathExportResult(False, None, None, "mathml_to_latex_empty_output")
    return MathExportResult(True, latex.strip(), text, None)


def convert_starmath_to_mathml(ctx: Any, starmath: str) -> MathExportResult:
    """StarMath command string → MathML via a hidden LibreOffice Math document."""
    if not starmath or not isinstance(starmath, str) or not starmath.strip():
        return MathExportResult(False, None, None, "empty_starmath")

    from plugin.writer.format import create_property_value

    desktop = get_desktop(ctx)
    hidden = (create_property_value("Hidden", True),)
    doc = desktop.loadComponentFromURL(_MATH_FACTORY_URL, "_blank", 0, hidden)
    if doc is None:
        return MathExportResult(False, None, None, "smath_factory_returned_none")

    fd, path = tempfile.mkstemp(suffix=".mml", prefix="writeragent-math-export-", text=False)
    os.close(fd)
    try:
        try:
            doc.setPropertyValue("Formula", starmath.strip())
        except Exception:
            doc.Formula = starmath.strip()

        url = _file_url(path)
        props = (
            create_property_value("FilterName", MATHML_EXPORT_FILTER),
            create_property_value("Overwrite", True),
        )
        doc.storeToURL(url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        mathml = _extract_math_root(raw)
        if not mathml:
            return MathExportResult(False, None, None, "empty_mathml_export")
        return MathExportResult(True, None, mathml, None)
    except Exception as exc:
        log.debug("convert_starmath_to_mathml failed: %s", exc, exc_info=True)
        return MathExportResult(False, None, None, _exception_message(exc))
    finally:
        try:
            doc.close(True)
        except Exception as exc:
            log.debug("math export doc close: %s", exc)
        try:
            os.unlink(path)
        except OSError:
            pass


def convert_starmath_to_latex(ctx: Any, starmath: str) -> MathExportResult:
    """StarMath → MathML (LO) → LaTeX (``mathml-to-latex``)."""
    mml = convert_starmath_to_mathml(ctx, starmath)
    if not mml.ok or not mml.mathml:
        return MathExportResult(False, None, mml.mathml, mml.error_message)
    tex = convert_mathml_to_latex(mml.mathml)
    if not tex.ok:
        return MathExportResult(False, None, mml.mathml, tex.error_message)
    return MathExportResult(True, tex.latex, mml.mathml, None)


def wrap_latex_delimiters(latex: str, *, display_block: bool) -> str:
    """Wrap TeX for ``apply_document_content`` / chat (``$…$`` vs ``$$…$$``)."""
    inner = (latex or "").strip()
    if inner.startswith("$$") and inner.endswith("$$"):
        return inner
    if inner.startswith("$") and inner.endswith("$") and not inner.startswith("$$"):
        if not display_block:
            return inner
        inner = inner[1:-1].strip()
    if display_block:
        return f"$${inner}$$"
    return f"${inner}$"


def iter_writer_math_objects(model: Any, ctx: Any | None = None) -> Iterator[WriterMathHit]:
    """Yield Math embeds in document order.

    Prefer ``XTextDocument.getEmbeddedObjects()`` (reliable for Math OLE). Fall
    back to paragraph/portion walk if that container is empty or missing.
    """
    ordered = _math_embeds_from_container(model)
    if not ordered:
        text = model.getText()
        para_enum = text.createEnumeration()
        para_index = 0
        while para_enum.hasMoreElements():
            para = para_enum.nextElement()
            for hit in _math_hits_in_paragraph(para, para_index, ctx):
                yield hit
            para_index += 1
        return

    for para_index, embed, display_block in ordered:
        yield _hit_from_embed(embed, para_index, display_block, ctx)


def inject_math_tex_into_html(model: Any, ctx: Any, html: str) -> str:
    """Replace XHTML ``<math>`` from Writer Math OLE with delimited TeX.

    ``XHTML Writer File`` inlines Presentation MathML (measured on quadratic.odt).
    Pair those ``<math>`` subtrees with OLE hits in order. Do not append leftovers.
    """
    if not html or not isinstance(html, str):
        return html
    hits = list(iter_writer_math_objects(model, ctx))
    math_matches = list(_MATH_ROOT_RE.finditer(html))
    n_hits = len(hits)
    n_math = len(math_matches)
    if n_hits == 0:
        return html
    if n_math == 0:
        log.error(
            "inject_math_tex_into_html: %d Math OLE(s) but no <math> in XHTML export; leaving HTML unchanged",
            n_hits,
        )
        return html
    if n_hits != n_math:
        log.error(
            "inject_math_tex_into_html: Math OLE count %d != <math> count %d; replacing min() only",
            n_hits,
            n_math,
        )

    parts: list[str] = []
    pos = 0
    for hit, match in zip(hits, math_matches):
        tex = hit.latex
        if not tex:
            snippet = (hit.starmath or hit.error_message or "")[:200]
            tex = f"[Math export fallback] {snippet}".strip()
        wrapped = wrap_latex_delimiters(tex, display_block=hit.display_block)
        parts.append(html[pos : match.start()])
        parts.append(wrapped)
        pos = match.end()
    parts.append(html[pos:])
    return "".join(parts)


def selected_math_embeds(doc: Any) -> list[Any]:
    """Math ``TextEmbeddedObject``s in the current selection (document order)."""
    found: list[Any] = []
    seen: set[int] = set()

    def _add(obj: Any) -> None:
        if obj is None or not _is_math_embed(obj):
            return
        key = id(obj)
        if key in seen:
            return
        seen.add(key)
        found.append(obj)

    try:
        sel = doc.getCurrentSelection()
    except Exception:
        return []
    if sel is None:
        return []

    _add(sel)
    get_count = getattr(sel, "getCount", None)
    get_by_index = getattr(sel, "getByIndex", None)
    if callable(get_count) and callable(get_by_index):
        try:
            count_val = get_count()
            n = count_val if isinstance(count_val, int) else int(str(count_val))
        except Exception:
            n = 0
        for i in range(n):
            try:
                el = get_by_index(i)
            except Exception:
                continue
            _add(el)
            if hasattr(el, "getStart"):
                for obj in _math_embeds_in_text_range(doc, el):
                    _add(obj)
    if hasattr(sel, "getStart"):
        for obj in _math_embeds_in_text_range(doc, sel):
            _add(obj)
    return found


def math_embed_from_selection(doc: Any) -> Any | None:
    """Return the selected Math object when exactly one is selected."""
    hits = selected_math_embeds(doc)
    if len(hits) == 1:
        return hits[0]
    return None


def math_embed_name(embed: Any) -> str | None:
    try:
        name = embed.getName() if hasattr(embed, "getName") else getattr(embed, "Name", None)
    except Exception:
        name = getattr(embed, "Name", None)
    text = str(name or "").strip()
    return text or None


def resolve_math_embed_name(doc: Any, embed: Any) -> str | None:
    """Stable ``getEmbeddedObjects()`` name for *embed*, or None."""
    named = math_embed_name(embed)
    if named:
        return named
    try:
        container = doc.getEmbeddedObjects()
        names = list(container.getElementNames())
    except Exception:
        return None
    for name in names:
        try:
            obj = container.getByName(name)
        except Exception:
            continue
        # Identity is only `obj is embed`. Repeating that compare cannot match a different object.
        if obj is embed:
            return str(name)
    return None


def lookup_math_embed(doc: Any, name: str) -> Any | None:
    if not name:
        return None
    try:
        container = doc.getEmbeddedObjects()
        if not container.hasByName(name):
            return None
        obj = container.getByName(name)
    except Exception:
        return None
    return obj if _is_math_embed(obj) else None


def _math_embeds_in_text_range(doc: Any, range_obj: Any) -> list[Any]:
    """Math embeds whose anchors fall inside *range_obj*."""
    try:
        text = range_obj.getText()
        sel_start = range_obj.getStart()
        sel_end = range_obj.getEnd()
    except Exception:
        return []
    if text is None or sel_start is None or sel_end is None:
        return []
    try:
        container = doc.getEmbeddedObjects()
        names = list(container.getElementNames())
    except Exception:
        return []
    found: list[Any] = []
    for name in names:
        try:
            obj = container.getByName(name)
        except Exception:
            continue
        if not _is_math_embed(obj):
            continue
        try:
            anchor = obj.getAnchor()
            anchor_start = anchor.getStart() if hasattr(anchor, "getStart") else anchor
            if text.compareRegionStarts(sel_start, anchor_start) < 0:
                continue
            if text.compareRegionStarts(anchor_start, sel_end) < 0:
                continue
        except Exception:
            continue
        found.append(obj)
    return found


def _extract_math_root(raw: str) -> str | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.lower().startswith("<math"):
        return text
    m = _MATH_ROOT_RE.search(text)
    return m.group(0) if m else None


def _is_math_embed(obj: Any) -> bool:
    try:
        clsid = str(getattr(obj, "CLSID", "") or "")
    except Exception:
        return False
    return clsid.lower() == MATH_CLSID.lower()


def _portion_embedded(portion: Any) -> Any | None:
    # A false _is_math_embed result is the CLSID check. Repeating it cannot succeed.
    for key in ("TextEmbeddedObject", "EmbeddedObject", "TextContent"):
        try:
            obj = portion.getPropertyValue(key)
        except Exception:
            obj = getattr(portion, key, None)
        if obj is not None and _is_math_embed(obj):
            return obj
    return None


def _read_starmath(embed: Any) -> str:
    try:
        inner = embed.getEmbeddedObject() if hasattr(embed, "getEmbeddedObject") else embed
        formula = inner.Formula if inner is not None else ""
        return str(formula or "").strip()
    except Exception:
        try:
            return str(embed.Formula or "").strip()
        except Exception:
            return ""


def _math_embeds_from_container(model: Any) -> list[tuple[int, Any, bool]]:
    """Return ``(sort_key, embed, display_block)`` sorted by anchor offset."""
    try:
        container = model.getEmbeddedObjects()
        names = list(container.getElementNames())
    except Exception:
        return []
    rows: list[tuple[int, Any, bool]] = []
    text = model.getText()
    for name in names:
        try:
            obj = container.getByName(name)
        except Exception:
            continue
        if not _is_math_embed(obj):
            continue
        offset = 0
        display_block = True
        try:
            anchor = obj.getAnchor()
            start_cursor = text.createTextCursor()
            start_cursor.gotoStart(False)
            start_cursor.gotoRange(anchor.getStart(), True)
            offset = len(start_cursor.getString() or "")
            display_block = _anchor_is_display_block(anchor)
        except Exception:
            pass
        rows.append((offset, obj, display_block))
    rows.sort(key=lambda row: row[0])
    return rows


def _anchor_is_display_block(anchor: Any) -> bool:
    """True when the formula's paragraph has no other visible text."""
    try:
        para = anchor
        try:
            cursor = anchor.getText().createTextCursorByRange(anchor)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            raw = cursor.getString() or ""
        except Exception:
            raw = para.getString() if hasattr(para, "getString") else ""
        stripped = raw.replace(_OBJECT_REPLACEMENT, "").strip()
        return stripped == ""
    except Exception:
        return True


def _hit_from_embed(embed: Any, para_index: int, display_block: bool, ctx: Any | None) -> WriterMathHit:
    starmath = _read_starmath(embed)
    latex = None
    mathml = None
    err = None
    if ctx is not None and starmath:
        res = convert_starmath_to_latex(ctx, starmath)
        latex = res.latex
        mathml = res.mathml
        err = None if res.ok else res.error_message
    elif not starmath:
        err = "empty_formula_property"
    return WriterMathHit(
        starmath=starmath,
        latex=latex,
        mathml=mathml,
        error_message=err,
        display_block=display_block,
        para_index=para_index,
    )


def _math_hits_in_paragraph(para: Any, para_index: int, ctx: Any | None) -> list[WriterMathHit]:
    embeds: list[Any] = []
    non_ws_text = False
    try:
        portion_enum = para.createEnumeration()
    except Exception:
        return []
    while portion_enum.hasMoreElements():
        portion = portion_enum.nextElement()
        # TextPortionType does not change _portion_embedded. A second call after None is the same miss.
        emb = _portion_embedded(portion)
        if emb is not None:
            embeds.append(emb)
            continue
        try:
            chunk = portion.getString() or ""
        except Exception:
            chunk = ""
        if chunk.strip() and chunk != _OBJECT_REPLACEMENT:
            non_ws_text = True

    display_block = bool(embeds) and not non_ws_text
    hits: list[WriterMathHit] = []
    for emb in embeds:
        starmath = _read_starmath(emb)
        latex = None
        mathml = None
        err = None
        if ctx is not None and starmath:
            res = convert_starmath_to_latex(ctx, starmath)
            latex = res.latex
            mathml = res.mathml
            err = None if res.ok else res.error_message
        elif not starmath:
            err = "empty_formula_property"
        hits.append(
            WriterMathHit(
                starmath=starmath,
                latex=latex,
                mathml=mathml,
                error_message=err,
                display_block=display_block,
                para_index=para_index,
            )
        )
    return hits
