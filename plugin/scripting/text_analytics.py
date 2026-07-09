# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text analytics helper templates, host RPC, and Writer egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.text_analytics`` via ``__getattr__``.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from plugin.scripting._lazy_venv import make_getattr
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    HelperScriptMeta,
    header_prefix,
    make_template_api,
)

if TYPE_CHECKING:
    from plugin.doc.document_helpers import HeadingTreeNode

_TEXT_VENV_EXPORTS = frozenset({"analyze_text", "check_diagnostics", "run_text_analytics"})

__getattr__ = make_getattr("text_analytics", _TEXT_VENV_EXPORTS)


from plugin.scripting.calc_functions_common import TEXT_ANALYTICS_HELPER_NAMES as HELPER_NAMES

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "full": {},
    "readability": {},
    "entities": {},
    "key_phrases": {},
    "topics": {"n_topics": 4},
    "sentiment": {},
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
        extra_comment_lines=("# Works on Writer documents (document text is injected on Run).",),
    )
)

get_text_analytics_script_templates = _API.get_templates
parse_text_analytics_script_header = _API.parse_header


TextAnalyticsScriptMeta = HelperScriptMeta


def supports_text_analytics_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Text Analytics helpers on *doc*."""
    if doc is None:
        return False
    try:
        from plugin.doc.document_helpers import is_writer

        return is_writer(doc)
    except Exception:
        return False


def resolve_text_analytics_document_inputs(doc: Any, helper: str) -> tuple[str | list[str], dict[str, Any]]:
    """Resolve Writer document text and RPC context for a text analytics helper."""
    name = str(helper or "full").strip() or "full"
    if name in ("topics", "sentiment"):
        text: str | list[str] = _get_writer_sections(doc)
    else:
        text = _get_writer_text(doc)
    context: dict[str, Any] = {}
    lang = _get_doc_lang(doc)
    if lang:
        context["lang"] = lang
    return text, context


def run_trusted_text_analytics(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a trusted text analytics helper against the Writer document text."""
    from plugin.doc.document_helpers import is_writer
    from plugin.framework.errors import ToolExecutionError
    from plugin.scripting.client import run_text_analytics as client_run

    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="TEXT_ANALYTICS_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="TEXT_ANALYTICS_ERROR")
    if not is_writer(doc):
        raise ToolExecutionError("Text analytics helpers require a Writer document.", code="TEXT_ANALYTICS_ERROR")

    text, context = resolve_text_analytics_document_inputs(doc, name)

    spec: dict[str, Any] = {"helper": name}
    if params:
        spec["params"] = params

    return client_run(uno_ctx, spec, text=text, context=context)


def _get_writer_text(doc: Any) -> str:
    """Best-effort extraction of document text for analysis (no tracked deletions)."""
    try:
        from plugin.doc.document_helpers import get_string_without_tracked_deletions, is_writer

        if not is_writer(doc):
            return ""
        text = doc.getText()
        return get_string_without_tracked_deletions(text) or ""
    except Exception:
        try:
            return str(doc.getText().getString() or "")
        except Exception:
            return ""


def _get_writer_sections(doc: Any) -> list[str]:
    """Return document split into section texts (heading + following body).

    Uses heading tree + paragraph walk for topic modeling "by section".
    Falls back to single full-text element when headings are absent.
    """
    try:
        from plugin.doc.document_helpers import build_heading_tree, get_string_without_tracked_deletions, is_writer
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
        heading_levels = {h.get("para_index"): h.get("text", "") for h in _flatten_headings(tree)}  # type: ignore[arg-type]  # build_heading_tree returns HeadingTreeNode which is dict-like

        para_index = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            try:
                if el.supportsService("com.sun.star.text.Paragraph"):
                    ptext = get_string_without_tracked_deletions(el) or ""
                    if para_index in heading_levels and heading_levels[para_index]:
                        # Flush previous section
                        if current_parts:
                            sections.append(f"{current_title}\n" + "\n".join(current_parts))
                        current_title = heading_levels[para_index]
                        current_parts = []
                    if ptext.strip():
                        current_parts.append(ptext)
            except Exception:
                pass
            para_index += 1

        if current_parts:
            sections.append(f"{current_title}\n" + "\n".join(current_parts))

        # Clean and dedup empties
        sections = [s.strip() for s in sections if len(s.strip()) > 20]
        return sections or ([_get_writer_text(doc)] if _get_writer_text(doc) else [])
    except Exception:
        # Safe fallback to flat text
        full = _get_writer_text(doc)
        return [full] if full.strip() else []


def _flatten_headings(node: dict[str, Any] | "HeadingTreeNode") -> list[dict[str, Any]]:
    """Utility: flatten heading tree to {para_index: text} for grouping."""
    out: list[dict[str, Any]] = []
    for ch in node.get("children", []):
        if ch.get("text"):
            out.append({"para_index": ch.get("para_index"), "text": ch.get("text")})
        out.extend(_flatten_headings(ch))
    return out


def get_doc_language(doc: Any) -> str | None:
    """Try to read the document or paragraph CharLocale language code (e.g. 'en').

    Used by the dialog and runner to bias spaCy model selection for better accuracy.
    """
    try:
        # Document level
        for prop in ("CharLocale", "CharLocaleAsian", "CharLocaleComplex"):
            try:
                loc = doc.getPropertyValue(prop)
                if loc and getattr(loc, "Language", None):
                    lang = str(loc.Language).strip().lower()[:2]
                    if lang and lang != "zxx":
                        return lang
            except Exception:
                pass
        # Current paragraph / selection
        try:
            controller = doc.getCurrentController()
            sel = controller.getSelection()
            if sel and hasattr(sel, "getByIndex"):
                rng = sel.getByIndex(0)
                for prop in ("CharLocale", "CharLocaleAsian", "CharLocaleComplex"):
                    try:
                        loc = rng.getPropertyValue(prop)
                        if loc and getattr(loc, "Language", None):
                            lang = str(loc.Language).strip().lower()[:2]
                            if lang and lang != "zxx":
                                return lang
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception:
        pass
    return None


# Back-compat alias (internal)
_get_doc_lang = get_doc_language


def is_text_analytics_result(value: Any) -> bool:
    """True when *value* looks like a text analytics helper result."""
    if not isinstance(value, dict):
        return False
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
    """Shared: turn analysis result data into a compact bordered HTML table."""
    rows: list[str] = []

    rd = data.get("readability") or {}
    ds = data.get("descriptive_stats") or {}
    for k, v in {**rd, **ds}.items():
        if isinstance(v, (int, float, str)):
            val = f"{v:.3f}" if isinstance(v, float) else str(v)
            rows.append(f"<tr><td>{k}</td><td>{val}</td></tr>")

    ents = data.get("entities") or []
    if ents:
        labels: dict[str, int] = {}
        for e in ents:
            lab = e.get("label", "?")
            labels[lab] = labels.get(lab, 0) + 1
        for lab, cnt in sorted(labels.items(), key=lambda x: -x[1])[:12]:
            rows.append(f"<tr><td>entity:{lab}</td><td>{cnt}</td></tr>")

    kps = data.get("key_phrases") or []
    if kps:
        top = ", ".join(kp.get("lemma") or kp.get("text") for kp in kps[:8])
        rows.append(f"<tr><td>key_phrases</td><td>{top}</td></tr>")

    # Topics (fancier text analytics): show top terms per topic + section assignments if present.
    topics = data.get("topics") or []
    if topics:
        for t in topics[:6]:
            tid = t.get("id", "?")
            terms = ", ".join(t.get("terms", [])[:5])
            rows.append(f"<tr><td>topic {tid}</td><td>{terms}</td></tr>")
        assigns = data.get("assignments") or []
        if assigns:
            # Compact: show how many sections map to each topic
            counts = Counter(a.get("dominant_topic") for a in assigns)
            summary = "; ".join(f"t{tid}:{cnt}" for tid, cnt in sorted(counts.items()))
            rows.append(f"<tr><td>topic sections</td><td>{summary}</td></tr>")

    # Sentiment: overall + summary of per-section if available
    sent = data.get("sentiment") or data.get("overall") or {}
    if sent and isinstance(sent, dict) and "score" in sent:
        sc = sent.get("score", 0)
        lab = sent.get("label", "neutral")
        rows.append(f"<tr><td>sentiment</td><td>{lab} ({sc})</td></tr>")
    per_sec = data.get("per_section") or []
    if per_sec:
        # Count labels across sections
        labels = Counter(str(p.get("label")) for p in per_sec if isinstance(p, dict) and p.get("label") is not None)
        if labels:
            summary = "; ".join(f"{lab}:{cnt}" for lab, cnt in labels.most_common())
            rows.append(f"<tr><td>sections</td><td>{summary}</td></tr>")

    if not rows:
        return ""
    return '<table border="1" style="border-collapse:collapse"><tbody>' + "".join(rows) + "</tbody></table>"


def insert_text_analytics_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any]) -> None:
    """Insert a compact HTML report for the text analytics result (Writer)."""
    from plugin.doc.document_helpers import is_writer
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

    data = (result or {}).get("result") or result or {}
    html = _result_to_html_table(data)
    if not html:
        # Fallback to a small JSON snippet so something is inserted
        import json

        html = "<pre>" + json.dumps(data, indent=2, ensure_ascii=False)[:1500] + "</pre>"

    html = "<h4>Text Analytics</h4>" + html

    # Position after selection/caret
    try:
        controller = doc.getCurrentController()
        vc = controller.getViewCursor()
        sel = controller.getSelection()
        if sel and hasattr(sel, "getCount") and sel.getCount() > 0:
            end = sel.getByIndex(0).getEnd()
            vc.gotoRange(end, False)
        controller.select(vc)
    except Exception:
        pass

    insert_content_at_position(doc, ctx, html, "selection")


def insert_text_analytics_result_into_writer(ctx: Any, doc: Any, result: dict[str, Any]) -> None:
    """Writer-specific alias for insert."""
    insert_text_analytics_result_into_doc(ctx, doc, result)
