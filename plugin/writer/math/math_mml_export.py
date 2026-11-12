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
from html import escape as _html_escape
from typing import Any, Iterator, Literal

from plugin.doc.text_helpers import iter_elements
from plugin.framework.errors import best_effort, reraise_if_disposed, uno_read
from plugin.framework.uno_context import (
    create_property_value,
    get_desktop,
    uno_same,
)
from plugin.framework.url_utils import path_to_file_url
from plugin.writer.math.math_mml_convert import (
    MATH_CLSID,
    _MATH_ROOT_RE,
    _exception_message,
    math_root,
)

log = logging.getLogger("writeragent.writer")

# API FilterName for File > Save As > MathML 2.0 (.mml). See LO Help convertfilters.
MATHML_EXPORT_FILTER = "MathML XML (Math)"
_MATH_FACTORY_URL = "private:factory/smath"

_OBJECT_REPLACEMENT = "\ufffc"
_MATH_CLSID_NORM = MATH_CLSID.lower()

# XHTML export of a Math frame keeps only draw:name as id (filter/source/xslt/odf2xhtml).
_WRAPPER_ID_RE = re.compile(r'<(?:span|div)\b[^>]*\bid="([^"]+)"[^>]*>', re.IGNORECASE)
_XML_ID_UNSAFE_RE = re.compile(r"[\n&<>.,;: %()\[\]/\\+]")


def _xhtml_id(name: str) -> str:
    """Mirror the XHTML export's id sanitizer so an OLE name can match a wrapper id."""
    safe = _XML_ID_UNSAFE_RE.sub("_", name or "")
    return f"a_{safe}" if safe[:1].isdigit() else safe


@dataclass(frozen=True)
class MathExportResult:
    ok: bool
    latex: str | None
    error_message: str | None


@dataclass(frozen=True)
class WriterMathHit:
    """One Writer formula object in document order."""

    starmath: str
    display_block: bool
    embed_name: str | None = None


def convert_mathml_to_latex(mathml_fragment: str) -> MathExportResult:
    """Presentation MathML string → LaTeX via vendored ``mathml-to-latex``. No UNO."""
    if not mathml_fragment:
        return MathExportResult(False, None, "empty_mathml")
    root = math_root(mathml_fragment)
    if not root:
        return MathExportResult(False, None, "not_math_root")
    try:
        from mathml_to_latex import MathMLToLaTeX
    except ImportError as exc:
        return MathExportResult(False, None, f"mathml_to_latex_import:{exc}")
    try:
        latex = MathMLToLaTeX.convert(root)
    except Exception as exc:
        log.debug("mathml-to-latex convert failed: %s", exc, exc_info=True)
        return MathExportResult(False, None, _exception_message(exc))
    if not isinstance(latex, str) or not latex.strip():
        return MathExportResult(False, None, "mathml_to_latex_empty_output")
    return MathExportResult(True, latex.strip(), None)


class MathExportBatch:
    """Reuses one hidden Math document across formulas in a single export run."""

    def __init__(self, ctx: Any) -> None:
        self.ctx: Any = ctx
        self._doc: Any = None
        self._temp_path: str | None = None
        self._cache: dict[str, MathExportResult] = {}

    def __enter__(self) -> MathExportBatch:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> Literal[False]:
        self.close()
        return False

    def _ensure_doc(self) -> Any:
        if self._doc is None:
            desktop = get_desktop(self.ctx)
            if desktop is None:
                return None
            hidden = (create_property_value("Hidden", True),)
            self._doc = desktop.loadComponentFromURL(_MATH_FACTORY_URL, "_blank", 0, hidden)
            if self._doc is not None:
                fd, path = tempfile.mkstemp(suffix=".mml", prefix="writeragent-math-export-", text=False)
                os.close(fd)
                self._temp_path = path
        return self._doc

    def convert_starmath_to_latex(self, starmath: str) -> MathExportResult:
        key = (starmath or "").strip()
        cached = self._cache.get(key)
        if cached is None:
            cached = self._convert_uncached(key)
            self._cache[key] = cached
        return cached

    def _convert_uncached(self, starmath: str) -> MathExportResult:
        if not starmath or not starmath.strip():
            return MathExportResult(False, None, "empty_starmath")
        doc = self._ensure_doc()
        if doc is None or not self._temp_path:
            return MathExportResult(False, None, "smath_factory_returned_none")
        try:
            # Formula is a plain property on the Math model (starmath/source/unomodel.cxx
            # SmModel::_setPropertyValues HANDLE_FORMULA).
            doc.setPropertyValue("Formula", starmath.strip())
            props = (
                create_property_value("FilterName", MATHML_EXPORT_FILTER),
                create_property_value("Overwrite", True),
            )
            doc.storeToURL(path_to_file_url(self._temp_path), props)
            with open(self._temp_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            mml = math_root(raw)
            if not mml:
                return MathExportResult(False, None, "empty_mathml_export")
            return convert_mathml_to_latex(mml)
        except Exception as exc:
            log.debug("math batch convert failed: %s", exc)
            return MathExportResult(False, None, _exception_message(exc))

    def close(self) -> None:
        doc = self._doc
        path = self._temp_path
        self._doc = None
        self._temp_path = None
        if doc is not None:
            best_effort(lambda: doc.close(True), "math export: hidden Math document close failed")
        if path:
            best_effort(lambda: os.unlink(path), "math export: temp .mml unlink failed")


def convert_starmath_to_latex(ctx: Any, starmath: str) -> MathExportResult:
    """StarMath → MathML (LO) → LaTeX (``mathml-to-latex``)."""
    with MathExportBatch(ctx) as b:
        return b.convert_starmath_to_latex(starmath)


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


def _failure_markup(error: str | None, starmath: str) -> str:
    """Non-TeX failure marker: valid HTML, so StarMath is never mistaken for TeX."""
    reason = (error or "math_export_failed").strip()[:120]
    snippet = (starmath or "").strip()[:200]
    return (
        f'<span data-math-export-error="{_html_escape(reason)}">'
        f"{_html_escape(snippet)}</span>"
    )


def _formula_replacement(
    result: MathExportResult, starmath: str, *, display_block: bool
) -> str:
    """TeX when the export worked, otherwise a non-TeX marker."""
    if result.ok and result.latex:
        return wrap_latex_delimiters(result.latex, display_block=display_block)
    return _failure_markup(result.error_message, starmath)


def iter_writer_math_objects(model: Any) -> Iterator[WriterMathHit]:
    """Yield Math embeds in document order."""
    text = model.getText() if hasattr(model, "getText") else None
    if text is None:
        return
    for para in _iter_body_paragraphs(text):
        yield from _math_hits_in_paragraph(para)


def inject_math_tex_into_html(model: Any, ctx: Any, html: str) -> str:
    """Replace XHTML ``<math>`` from Writer Math OLE with delimited TeX.

    ``XHTML Writer File`` inlines Presentation MathML (measured on quadratic.odt).
    TeX is converted from the ``<math>`` already in the XHTML; the OLE walk only
    supplies the block/inline flag plus the StarMath fallback.
    """
    if not html:
        return html
    math_matches = list(_MATH_ROOT_RE.finditer(html))
    if not math_matches:
        return html

    # Display flag + StarMath fallback only: no ctx, so the walk cannot export a hidden Math doc.
    hits = list(iter_writer_math_objects(model))
    order_pairing = len(hits) == len(math_matches)
    if not order_pairing:
        log.warning(
            "inject_math_tex_into_html: Math OLE count %d != <math> count %d",
            len(hits),
            len(math_matches),
        )

    by_id: dict[str, WriterMathHit] = {}
    for h in hits:
        if h.embed_name:
            by_id.setdefault(_xhtml_id(h.embed_name), h)
    wrappers = list(_WRAPPER_ID_RE.finditer(html))
    next_wrapper = 0

    parts: list[str] = []
    pos = 0
    failures = 0
    with MathExportBatch(ctx) as batch:
        for index, match in enumerate(math_matches):
            while next_wrapper < len(wrappers) and wrappers[next_wrapper].start() < match.start():
                next_wrapper += 1
            wrapper = wrappers[next_wrapper - 1] if next_wrapper else None
            hit: WriterMathHit | None = None
            # The gap check keeps an earlier formula's wrapper from claiming this <math>.
            if wrapper is not None and "<" not in html[wrapper.end() : match.start()]:
                hit = by_id.pop(_xhtml_id(wrapper.group(1)), None)
            if hit is None and order_pairing:
                hit = hits[index]
            display_block = bool(hit and hit.display_block)
            res = convert_mathml_to_latex(match.group(0))
            if not res.ok and hit is not None and hit.starmath:
                res = batch.convert_starmath_to_latex(hit.starmath)
            if not res.ok:
                failures += 1
            parts.append(html[pos : match.start()])
            parts.append(
                _formula_replacement(res, hit.starmath if hit else "", display_block=display_block)
            )
            pos = match.end()
    parts.append(html[pos:])
    if failures:
        log.warning(
            "inject_math_tex_into_html: %d formula(s) could not be exported to TeX", failures
        )
    return "".join(parts)


def substitute_math_placeholders(
    content: str,
    math_records: list[tuple[str, str, bool]],
    ctx: Any,
) -> str:
    """Replace private-use formula placeholders with delimited TeX."""
    if not math_records or not content:
        return content

    with MathExportBatch(ctx) as batch:
        for placeholder, starmath, display_block in math_records:
            res = batch.convert_starmath_to_latex(starmath)
            wrapped = _formula_replacement(res, starmath, display_block=display_block)

            code = ord(placeholder)
            for target in (placeholder, f"&#x{code:x};", f"&#x{code:X};", f"&#{code};"):
                content = content.replace(target, wrapped)
    return content


def inject_exported_math_tex(model: Any, ctx: Any, content: str) -> str:
    """Replace formula OLE holes with delimited TeX for the model/chat.

    Failures stay as <span data-math-export-error="…"> StarMath (never $…$).
    """
    if not content or model is None or ctx is None:
        return content
    try:
        return inject_math_tex_into_html(model, ctx, content)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("inject_exported_math_tex failed")
        return content


def _iter_named_embeds(doc: Any) -> Iterator[tuple[str, Any]]:
    container = uno_read(lambda: doc.getEmbeddedObjects(), None)
    if container is None:
        return
    names: list[Any] = uno_read(lambda: list(container.getElementNames()), [])
    for name in names:
        obj = uno_read(lambda: container.getByName(name), None)
        if obj is not None:
            yield str(name), obj


def selected_math_embeds(doc: Any) -> list[Any]:
    """Math ``TextEmbeddedObject``s in the current selection (document order)."""
    found: list[Any] = []

    def _add(obj: Any) -> None:
        if obj is None or not _is_math_embed(obj):
            return
        if any(uno_same(obj, existing) for existing in found):
            return
        found.append(obj)

    sel = uno_read(lambda: doc.getCurrentSelection(), None)
    if sel is None:
        return []

    _add(sel)
    embed_objects = [obj for _name, obj in _iter_named_embeds(doc) if _is_math_embed(obj)]

    # A single selected OLE frame or table cursor has no getCount (unotxvw.cxx SwXTextView::getSelection).
    for i in range(uno_read(lambda: sel.getCount(), 0)):
        el = uno_read(lambda: sel.getByIndex(i), None)
        if el is None:
            continue
        _add(el)
        if hasattr(el, "getStart"):
            for obj in _math_embeds_in_text_range(el, embed_objects):
                _add(obj)
    if hasattr(sel, "getStart"):
        for obj in _math_embeds_in_text_range(sel, embed_objects):
            _add(obj)
    return found


def math_embed_name(embed: Any) -> str | None:
    # SwXFrame::getName (sw/source/core/unocore/unoframe.cxx) is present on every TextEmbeddedObject.
    return uno_read(lambda: str(embed.getName() or "").strip(), "") or None


def resolve_math_embed_name(doc: Any, embed: Any) -> str | None:
    """Stable ``getEmbeddedObjects()`` name for *embed*, or None."""
    named = math_embed_name(embed)
    if named:
        return named
    for name, obj in _iter_named_embeds(doc):
        if uno_same(obj, embed):
            return str(name)
    return None


def lookup_math_embed(doc: Any, name: str) -> Any | None:
    if not name:
        return None

    def _find() -> Any | None:
        container = doc.getEmbeddedObjects()
        if not container.hasByName(name):
            return None
        return container.getByName(name)

    obj = uno_read(_find, None)
    return obj if _is_math_embed(obj) else None


def _math_embeds_in_text_range(range_obj: Any, embed_objects: list[Any]) -> list[Any]:
    """Math embeds whose anchors fall inside *range_obj*."""
    bounds = uno_read(
        lambda: (range_obj.getText(), range_obj.getStart(), range_obj.getEnd()),
        None,
    )
    if bounds is None or any(x is None for x in bounds):
        return []
    text, sel_start, sel_end = bounds

    found: list[Any] = []
    for obj in embed_objects:
        # compareRegionStarts throws IllegalArgumentException when a range lies
        # in another XText (sw/source/core/unocore/unotext.cxx:955), so an anchor
        # belonging to a different text container is safely treated as not inside.
        def _inside() -> bool:
            a = obj.getAnchor().getStart()
            return text.compareRegionStarts(sel_start, a) >= 0 and text.compareRegionStarts(a, sel_end) >= 0

        if uno_read(_inside, False):
            found.append(obj)
    return found


def _is_math_embed(obj: Any) -> bool:
    # A non-frame object raises AttributeError; uno_read treats it as not an embed.
    return uno_read(lambda: str(obj.CLSID or ""), "").lower() == _MATH_CLSID_NORM


def portion_math_embed(portion: Any) -> Any | None:
    """Return the Math TextEmbeddedObject from a text portion, or None.

    Text portions have no property naming the embedded object (sw/source/core/unocore/unomap1.cxx).
    Writer exposes an as-character object only through the Frame portion's content enumeration
    (sw/source/core/unocore/unoport.cxx SwXTextPortion::createContentEnumeration). Text portions
    enumerate objects anchored nearby too, so only a Frame portion counts (otherwise the text
    before a formula is lost).
    """
    if uno_read(lambda: str(portion.getPropertyValue("TextPortionType") or ""), "") != "Frame":
        return None
    enum = uno_read(
        lambda: portion.createContentEnumeration("com.sun.star.text.TextContent"),
        None,
    )
    for obj in iter_elements(enum):
        if obj is not None and _is_math_embed(obj):
            return obj
    return None


def read_starmath(embed: Any) -> str:
    """Read the StarMath formula string from a Math embedded object.

    Formula lives on the Math model, not the Writer frame. getEmbeddedObject()
    returns null for an unloadable OLE (sw/source/core/unocore/unoframe.cxx
    SwXTextEmbeddedObject::getEmbeddedObject).
    """
    return uno_read(lambda: str(embed.getEmbeddedObject().Formula or "").strip(), "")


def _iter_body_paragraphs(text_obj: Any) -> Iterator[Any]:
    """Yield paragraphs in document order, descending into table cells."""
    enum = uno_read(lambda: text_obj.createEnumeration(), None)
    for el in iter_elements(enum):
        if uno_read(lambda: bool(el.supportsService("com.sun.star.text.TextTable")), False):
            cell_names: tuple[Any, ...] = uno_read(lambda: tuple(el.getCellNames() or ()), ())
            for name in cell_names:
                cell = uno_read(lambda: el.getCellByName(name), None)
                if cell is not None:
                    yield from _iter_body_paragraphs(cell)
            continue
        if hasattr(el, "createEnumeration"):
            yield el


def _math_hits_in_paragraph(para: Any) -> list[WriterMathHit]:
    embeds: list[Any] = []
    non_ws_text = False
    portion_enum = uno_read(lambda: para.createEnumeration(), None)
    for portion in iter_elements(portion_enum):
        emb = portion_math_embed(portion)
        if emb is not None:
            # Writer can report one as-character object in two portions; count it once.
            if not any(uno_same(emb, seen) for seen in embeds):
                embeds.append(emb)
            continue
        chunk = uno_read(lambda: portion.getString() or "", "")
        if chunk.strip() and chunk != _OBJECT_REPLACEMENT:
            non_ws_text = True

    display_block = bool(embeds) and not non_ws_text
    return [
        WriterMathHit(
            starmath=read_starmath(emb),
            display_block=display_block,
            embed_name=math_embed_name(emb),
        )
        for emb in embeds
    ]
