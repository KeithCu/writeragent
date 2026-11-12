# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text analytics helper host RPC and Writer egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.text_analytics`` via ``__getattr__``.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from plugin.framework.doc_type import is_writer
from plugin.doc.text_helpers import (
    build_heading_tree,
    get_string_without_tracked_deletions,
    iter_elements,
    iter_heading_nodes,
    selected_text_range,
)
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed
from plugin.scripting._lazy_venv import install_lazy_exports

_TEXT_VENV_EXPORTS = frozenset({"analyze_text", "check_diagnostics", "run_text_analytics"})

install_lazy_exports(globals(), "text_analytics", _TEXT_VENV_EXPORTS)

log = logging.getLogger(__name__)


def resolve_text_analytics_document_inputs(
    doc: Any,
    helper: str,
    *,
    scope: str = "whole",
) -> tuple[str | list[str], dict[str, Any]]:
    """Resolve Writer document text and RPC context for a text analytics helper.

    :param scope: Text resolution scope. ``"selection"`` extracts text from the
        first selection range only (via ``_get_writer_selection_text``) for every
        helper without section splitting. ``"whole"`` (or any other value) extracts
        sections for ``topics`` and ``sentiment``, and flat document text for other helpers.
    """
    name = str(helper or "full").strip() or "full"
    if scope == "selection":
        text: str | list[str] = _get_writer_selection_text(doc)
    elif name in ("topics", "sentiment"):
        text = _get_writer_sections(doc)
    else:
        text = _get_writer_text(doc)
    context: dict[str, Any] = {}
    lang = get_doc_language(doc)
    if lang:
        context["lang"] = lang
    return text, context


def _get_writer_selection_text(doc: Any) -> str:
    """Extract text from the first selection range only (no tracked deletions).

    Selection scope reads only the first range and returns "" on failure; it does not fall back to full document text.
    """
    try:
        if not is_writer(doc):
            return ""
        controller = doc.getCurrentController()
        if not controller:
            return ""
        rng = selected_text_range(controller)
        if rng is None:
            return ""
        return get_string_without_tracked_deletions(rng) or ""
    except Exception as exc:
        # Re-raise DocumentDisposedError so callers handle closed documents.
        reraise_if_disposed(exc)
        log.debug("text analytics: selection text failed", exc_info=True)
        return ""


def _get_writer_text(doc: Any) -> str:
    """Best-effort extraction of document text for analysis (no tracked deletions)."""
    try:
        if not is_writer(doc):
            return ""
        text = doc.getText()
        return get_string_without_tracked_deletions(text) or ""
    except Exception as exc:
        # reraise_if_disposed stops on closed documents; getString() is omitted so tracked deletions do not leak into analysis text.
        reraise_if_disposed(exc)
        log.debug("text analytics: writer text extraction failed", exc_info=True)
        return ""


def _get_writer_sections(doc: Any) -> list[str]:
    """Return document split into section texts (heading + following body).

    Uses heading tree + paragraph walk for topic modeling "by section".
    Falls back to single full-text element when headings are absent.
    """
    def _fallback() -> list[str]:
        full = _get_writer_text(doc)
        return [full] if full.strip() else []

    try:
        if not is_writer(doc):
            return []

        tree = build_heading_tree(doc)
        # If no real headings, just return the whole doc as one "section".
        if not tree.get("children"):
            return _fallback()

        # Walk the document paragraphs once, associating body with the active heading.
        text_obj = doc.getText()
        enum = text_obj.createEnumeration()
        sections: list[str] = []
        current_title = "Introduction / preamble"
        current_parts: list[str] = []
        heading_levels = {n.get("para_index"): n.get("text", "") for n in iter_heading_nodes(tree) if n.get("text")}

        # Heading paragraphs become section titles only, not body lines, so topic/sentiment models do not double-count headings.
        def _flush() -> None:
            body = "\n".join(current_parts).strip()
            if body:
                sections.append(f"{current_title}\n{body}" if current_title else body)
            elif current_title and current_title != "Introduction / preamble":
                sections.append(current_title)

        para_index = 0
        for el in iter_elements(enum):
            try:
                if el.supportsService("com.sun.star.text.Paragraph"):
                    ptext = get_string_without_tracked_deletions(el) or ""
                    if para_index in heading_levels:
                        # Flush previous section
                        _flush()
                        current_title = ptext.strip() or heading_levels[para_index]
                        current_parts = []
                    elif ptext.strip():
                        current_parts.append(ptext)
            except Exception as exc:
                # Propagate disposal during the paragraph walk instead of skipping silently.
                reraise_if_disposed(exc)
                log.debug("text analytics: skipped paragraph %s", para_index, exc_info=True)
            para_index += 1

        _flush()

        # Clean and dedup empties
        sections = [s.strip() for s in sections if len(s.strip()) > 20]
        if sections:
            return sections
        return _fallback()
    except Exception as exc:
        # reraise_if_disposed runs before flat-text fallback so a closed document does not masquerade as empty sections.
        reraise_if_disposed(exc)
        log.debug("text analytics: section extraction failed", exc_info=True)
        # Safe fallback to flat text
        return _fallback()


def _lang_of(obj: Any, where: str) -> str | None:
    for prop in ("CharLocale", "CharLocaleAsian", "CharLocaleComplex"):
        try:
            loc = obj.getPropertyValue(prop)
            if loc and getattr(loc, "Language", None):
                lang = str(loc.Language).strip().lower()[:2]
                if lang and lang != "zxx":
                    return lang
        except Exception:
            log.debug("text analytics: %s locale %s unreadable", where, prop, exc_info=True)
    return None


def get_doc_language(doc: Any) -> str | None:
    """Try to read the document or paragraph CharLocale language code (e.g. 'en').

    Used by the dialog and runner to bias spaCy model selection for better accuracy.
    """
    lang = _lang_of(doc, "doc")
    if lang:
        return lang
    try:
        controller = doc.getCurrentController()
        sel = controller.getSelection()
        if sel and hasattr(sel, "getByIndex"):
            return _lang_of(sel.getByIndex(0), "selection")
    except Exception:
        log.debug("text analytics: selection locale probe failed", exc_info=True)
    return None



def is_text_analytics_result(value: Any) -> bool:
    """True when *value* looks like a text analytics helper result."""
    if not isinstance(value, dict):
        return False
    # Error dicts must take this insert path. Falling through inserted the
    # raw dict as generic Writer text and reported success.
    status = value.get("status")
    helper = value.get("helper")
    from plugin.scripting.helper_registry import names

    if status == "error":
        return str(value.get("code") or "") in ("TEXT_ANALYTICS_ERROR", "UNKNOWN_HELPER") or (
            isinstance(helper, str) and helper in names("text")
        )
    if isinstance(helper, str) and helper in ("diagnostics", "check"):
        return status == "ok"
    if status != "ok":
        return False
    # Our results have a top-level "result" with known keys, or the helper dispatch shape.
    res = value.get("result")
    if isinstance(res, dict) and any(k in res for k in (
        "readability", "descriptive_stats", "entities", "key_phrases", "topics", "sentiment", "meta",
    )):
        return True
    # Also accept the narrow shapes returned by specific helpers
    return any(k in value for k in ("entities", "key_phrases", "readability", "topics", "sentiment"))


def _result_to_html_table(data: dict[str, Any]) -> str:
    """Shared: turn analysis result data into a compact bordered HTML table.

    Cell text is double-escaped. ``insert_content_at_position`` unescapes once
    before the StarWriter HTML filter, so a single ``html.escape`` would turn
    a lemma such as ``<script>`` back into markup.
    """
    from plugin.scripting.python_runner import _html_insert_text

    rows: list[str] = []

    def _row(label: object, value: object) -> None:
        rows.append(
            f"<tr><td>{_html_insert_text(label)}</td><td>{_html_insert_text(value)}</td></tr>"
        )

    rd = data.get("readability") or {}
    ds = data.get("descriptive_stats") or {}
    for k, v in {**rd, **ds}.items():
        if isinstance(v, (int, float, str)):
            val = f"{v:.3f}" if isinstance(v, float) else str(v)
            _row(k, val)

    ents = data.get("entities") or []
    if ents:
        counts = Counter(e.get("label", "?") for e in ents)
        for lab, cnt in counts.most_common()[:12]:
            _row(f"entity:{lab}", cnt)

    kps = data.get("key_phrases") or []
    if kps:
        top = ", ".join(kp.get("lemma") or kp.get("text") or "" for kp in kps[:8])
        _row("key_phrases", top)

    # Topics (fancier text analytics): show top terms per topic + section assignments if present.
    topics = data.get("topics") or []
    if topics:
        for t in topics[:6]:
            tid = t.get("id", "?")
            terms = ", ".join(t.get("terms", [])[:5])
            _row(f"topic {tid}", terms)
        assigns = data.get("assignments") or []
        if assigns:
            # Compact: show how many sections map to each topic
            counts = Counter(a.get("dominant_topic") for a in assigns)
            summary = "; ".join(f"t{tid}:{cnt}" for tid, cnt in sorted(counts.items()))
            _row("topic sections", summary)

    # Sentiment: overall + summary of per-section if available
    sent = data.get("sentiment") or {}
    if sent and isinstance(sent, dict) and "score" in sent:
        sc = sent.get("score", 0)
        lab = sent.get("label", "neutral")
        _row("sentiment", f"{lab} ({sc})")
    per_sec = data.get("per_section") or []
    if per_sec:
        # Count labels across sections
        labels = Counter(str(p.get("label")) for p in per_sec if isinstance(p, dict) and p.get("label") is not None)
        if labels:
            summary = "; ".join(f"{lab}:{cnt}" for lab, cnt in labels.most_common())
            _row("sections", summary)

    if not rows:
        return ""
    return '<table border="1" style="border-collapse:collapse"><tbody>' + "".join(rows) + "</tbody></table>"


def _result_payload(result: Any) -> dict[str, Any]:
    """Extract inner payload dict if present, else outer dict if no result key."""
    if not isinstance(result, dict):
        return {}
    if "result" in result:
        res = result["result"]
        return res if isinstance(res, dict) else {}
    return result


def text_analytics_payload_error(data: Any) -> str | None:
    """Return a top-level error or note from a payload dict."""
    if not isinstance(data, dict):
        return None
    for key in ("error", "note"):
        val = data.get(key)
        if val:
            return str(val)
    return None


def insert_text_analytics_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any]) -> int:
    """Insert a compact HTML report for the text analytics result (Writer)."""
    from plugin.writer.html_import import insert_content_at_position

    if not is_writer(doc):
        raise ToolExecutionError("Text analytics insert requires a Writer document.", code="TEXT_ANALYTICS_ERROR")

    if result.get("status") != "ok":
        raise ToolExecutionError(
            str(result.get("message") or "Text analytics failed."),
            code="TEXT_ANALYTICS_ERROR",
            details={"result": result},
        )

    data = _result_payload(result)
    html = _result_to_html_table(data)
    if not html:
        err = text_analytics_payload_error(data) or text_analytics_payload_error(result) or "Nothing to insert."
        raise ToolExecutionError(err, code="TEXT_ANALYTICS_ERROR", details={"result": result})

    html = "<h4>Text Analytics</h4>" + html

    # Position after selection/caret
    try:
        controller = doc.getCurrentController()
        vc = controller.getViewCursor()
        rng = selected_text_range(controller)
        if rng is not None:
            end = rng.getEnd()
            vc.gotoRange(end, False)
        controller.select(vc)
    except Exception:
        log.debug("text analytics: could not move caret to selection end", exc_info=True)

    insert_content_at_position(doc, ctx, html, "selection")
    return 1

