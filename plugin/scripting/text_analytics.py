# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text analytics helper templates, host RPC, and Writer egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.text_analytics`` via ``__getattr__``.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from plugin.doc.text_helpers import iter_heading_nodes
from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    header_prefix,
    make_template_api,
)

_TEXT_VENV_EXPORTS = frozenset({"analyze_text", "check_diagnostics", "run_text_analytics"})

install_lazy_exports(globals(), "text_analytics", _TEXT_VENV_EXPORTS)


from plugin.scripting.calc_functions_common import TEXT_ANALYTICS_HELPER_NAMES as HELPER_NAMES

log = logging.getLogger(__name__)

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "topics": {"n_topics": 4},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "full": "Full spaCy + textdescriptives analysis (readability, entities, key phrases)",
    "readability": "Readability scores and descriptive stats via textdescriptives",
    "entities": "Named entity recognition (multilingual NER)",
    "key_phrases": "Key phrases via noun chunks (lemmatized)",
    "topics": "Topic modeling (NMF + TF-IDF). Best results when whole document yields section list.",
    "sentiment": "transformers + multilingual model (XLM-RoBERTa default) sentiment (score + label). Best with whole document for per-section results. Override model via JSON config.",
}

TEXT_ANALYTICS_HEADER_PREFIX = header_prefix("text")

_SHIPPED_TEMPLATES = frozenset({"full", "readability", "entities", "key_phrases", "topics", "sentiment"})

_API = make_template_api(
    DomainFacadeConfig(
        tag="text",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.text_analytics",
        run_name="run_text_analytics",
        shipped_templates=_SHIPPED_TEMPLATES,
        data_expr="text",
        context_expr="document_context",
        invoke="runner",
        extra_comment_lines=("# Works on Writer documents (document text is injected on Run).",),
    )
)

get_text_analytics_script_templates = _API.get_templates

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

    What was wrong: selection scope could fall back to whole document text.
    How it happened: previous implementation fell back to doc.getText() if selection was missing/empty.
    Why this change: selection scope must strictly read the first selection range only and return ""
    if nothing is selected or extraction fails, never falling back to doc.getText().
    """
    try:
        from plugin.doc.doc_type import is_writer
        from plugin.doc.text_helpers import get_string_without_tracked_deletions, selected_text_range

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
        from plugin.framework.errors import reraise_if_disposed
        reraise_if_disposed(exc)
        log.debug("text analytics: selection text failed", exc_info=True)
        return ""


def _get_writer_text(doc: Any) -> str:
    """Best-effort extraction of document text for analysis (no tracked deletions)."""
    try:
        from plugin.doc.doc_type import is_writer
        from plugin.doc.text_helpers import get_string_without_tracked_deletions  # local import only

        if not is_writer(doc):
            return ""
        text = doc.getText()
        return get_string_without_tracked_deletions(text) or ""
    except Exception as exc:
        # What was wrong: DisposedException was swallowed, and the secondary getString() fallback
        # reintroduced tracked deletions and duplicated log warnings.
        # How it happened: nested try/except caught Exception and tried doc.getText().getString().
        # Why this change: reraise_if_disposed propagates DocumentDisposedError, and omitting getString()
        # prevents deleted text leakage and duplicate logs.
        from plugin.framework.errors import reraise_if_disposed
        reraise_if_disposed(exc)
        log.debug("text analytics: writer text extraction failed", exc_info=True)
        return ""


def _get_writer_sections(doc: Any) -> list[str]:
    """Return document split into section texts (heading + following body).

    Uses heading tree + paragraph walk for topic modeling "by section".
    Falls back to single full-text element when headings are absent.
    """
    try:
        from plugin.doc.doc_type import is_writer
        from plugin.doc.text_helpers import build_heading_tree, get_string_without_tracked_deletions  # local import only
        if not is_writer(doc):
            return []

        tree = build_heading_tree(doc)
        # If no real headings, just return the whole doc as one "section".
        if not tree.get("children"):
            full = _get_writer_text(doc)
            return [full] if full.strip() else []

        # Walk the document paragraphs once, associating body with the active heading.
        text_obj = doc.getText()
        enum = text_obj.createEnumeration()
        sections: list[str] = []
        current_title = "Introduction / preamble"
        current_parts: list[str] = []
        heading_levels = {n.get("para_index"): n.get("text", "") for n in iter_heading_nodes(tree) if n.get("text")}

        # What was wrong: the heading paragraph was both current_title and the first body
        # line, so topics and sentiment counted the heading twice; the fallback also called
        # _get_writer_text twice.
        # How it happened: heading paragraphs were appended to current_parts, and the fallback
        # condition used _get_writer_text(doc) twice in a list comprehension.
        # Why this change: flush previous section, set current_title without appending the
        # heading paragraph to body parts, emit title-only when non-empty and not preamble,
        # and evaluate _get_writer_text once on fallback.
        def _flush() -> None:
            body = "\n".join(current_parts).strip()
            if body:
                sections.append(f"{current_title}\n{body}" if current_title else body)
            elif current_title and current_title != "Introduction / preamble":
                sections.append(current_title)

        para_index = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            try:
                if el.supportsService("com.sun.star.text.Paragraph"):
                    ptext = get_string_without_tracked_deletions(el) or ""
                    if para_index in heading_levels and heading_levels[para_index]:
                        # Flush previous section
                        _flush()
                        current_title = ptext.strip() or heading_levels[para_index]
                        current_parts = []
                    elif ptext.strip():
                        current_parts.append(ptext)
            except Exception as exc:
                # What was wrong: disposed document during paragraph iteration was ignored.
                # How it happened: inner except Exception only logged debug without checking disposal.
                # Why this change: reraise_if_disposed propagates DocumentDisposedError immediately.
                from plugin.framework.errors import reraise_if_disposed
                reraise_if_disposed(exc)
                log.debug("text analytics: skipped paragraph %s", para_index, exc_info=True)
            para_index += 1

        _flush()

        # Clean and dedup empties
        sections = [s.strip() for s in sections if len(s.strip()) > 20]
        if sections:
            return sections
        fallback = _get_writer_text(doc)
        return [fallback] if fallback.strip() else []
    except Exception as exc:
        # What was wrong: outer section extraction caught DisposedException and fell back to _get_writer_text.
        # How it happened: generic except Exception caught DisposedException.
        # Why this change: reraise_if_disposed propagates DocumentDisposedError before fallback.
        from plugin.framework.errors import reraise_if_disposed
        reraise_if_disposed(exc)
        log.debug("text analytics: section extraction failed", exc_info=True)
        # Safe fallback to flat text
        full = _get_writer_text(doc)
        return [full] if full.strip() else []


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
    if value.get("status") == "error":
        code = str(value.get("code") or "")
        helper = value.get("helper")
        if code in ("TEXT_ANALYTICS_ERROR", "UNKNOWN_HELPER"):
            return True
        if isinstance(helper, str) and helper in HELPER_NAMES:
            return True
    helper_name = value.get("helper")
    if isinstance(helper_name, str) and helper_name in ("diagnostics", "check"):
        return value.get("status") in ("ok", "error")
    if value.get("status") != "ok":
        return False
    # Our results have a top-level "result" with known keys, or the helper dispatch shape.
    res = value.get("result")
    if isinstance(res, dict):
        for k in ("readability", "descriptive_stats", "entities", "key_phrases", "topics", "sentiment", "meta"):
            if k in res:
                return True
    # Also accept the narrow shapes returned by specific helpers
    for k in ("entities", "key_phrases", "readability", "topics", "sentiment"):
        if k in value:
            return True
    return False


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
    """Return error or note message from payload, checking top-level and inner result."""
    if not isinstance(data, dict):
        return None
    for key in ("error", "note"):
        val = data.get(key)
        if val:
            return str(val)
    res = data.get("result")
    if isinstance(res, dict):
        for key in ("error", "note"):
            val = res.get(key)
            if val:
                return str(val)
    return None


def text_analytics_payload_has_content(result: Any) -> bool:
    """True if analysis payload contains renderable rows."""
    return bool(_result_to_html_table(_result_payload(result)))


def insert_text_analytics_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any]) -> int:
    """Insert a compact HTML report for the text analytics result (Writer)."""
    from plugin.doc.doc_type import is_writer
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.format import insert_content_at_position

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
        from plugin.doc.text_helpers import selected_text_range

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

