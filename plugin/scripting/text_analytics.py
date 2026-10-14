# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text analytics helper templates, host RPC, and Writer egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.text_analytics`` via ``__getattr__``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    header_prefix,
    make_template_api,
)

if TYPE_CHECKING:
    from plugin.doc.text_helpers import HeadingTreeNode

_TEXT_VENV_EXPORTS = frozenset({"analyze_text", "check_diagnostics", "run_text_analytics"})

install_lazy_exports(globals(), "text_analytics", _TEXT_VENV_EXPORTS)


from plugin.scripting.calc_functions_common import TEXT_ANALYTICS_HELPER_NAMES as HELPER_NAMES

log = logging.getLogger(__name__)

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
        invoke="runner",
        extra_comment_lines=("# Works on Writer documents (document text is injected on Run).",),
    )
)

get_text_analytics_script_templates = _API.get_templates
parse_text_analytics_script_header = _API.parse_header




def supports_text_analytics_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Text Analytics helpers on *doc*."""
    if doc is None:
        return False
    try:
        from plugin.doc.doc_type import is_writer

        return is_writer(doc)
    except Exception:
        return False


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
        from plugin.doc.text_helpers import get_string_without_tracked_deletions  # local import only

        if not is_writer(doc):
            return ""
        controller = doc.getCurrentController()
        if not controller:
            return ""
        sel = controller.getSelection()
        if not sel or not hasattr(sel, "getCount") or sel.getCount() == 0:
            return ""
        rng = sel.getByIndex(0)
        return get_string_without_tracked_deletions(rng) or ""
    except Exception as exc:
        # What was wrong: disposed document exceptions were caught and swallowed, returning empty text.
        # How it happened: generic except Exception caught DisposedException without reraising.
        # Why this change: reraise_if_disposed re-raises DocumentDisposedError so callers handle closed documents.
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
        heading_levels = {h.get("para_index"): h.get("text", "") for h in _flatten_headings(tree)}  # type: ignore[arg-type]  # build_heading_tree returns HeadingTreeNode which is dict-like

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


def _text_analytics_error_line(result: dict[str, Any] | None) -> str:
    """Format generic error line from result dict."""
    if isinstance(result, dict):
        msg = result.get("message")
        if isinstance(msg, str) and msg.strip():
            return f"Error: {msg.strip()}"
    return "Error: Text analytics failed."


def _format_metric_lines(mapping: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for k, v in mapping.items():
        if isinstance(v, bool):
            lines.append(f"  {k}: {v}")
        elif isinstance(v, float):
            lines.append(f"  {k}: {v:.2f}")
        else:
            lines.append(f"  {k}: {v}")
    return lines


def format_text_analytics_result_for_display(helper: str, result: dict[str, Any] | None) -> str:
    """Format a text analytics result dict for UI display in the analytics dialog."""
    if not isinstance(result, dict):
        return "No result."

    raw_data = result.get("result")
    data = raw_data if isinstance(raw_data, dict) else {}

    if helper in ("diagnostics", "check"):
        if result.get("status") == "error" and data.get("status") != "error":
            return _text_analytics_error_line(result)

        if data.get("status") == "error":
            install = data.get("text_analytics_install", "")
            py = data.get("python_used_by_worker", "unknown")
            msg = f"Diagnostics Error (worker python: {py}):\n{data.get('message')}"
            if install:
                msg += f"\n\nFull requirements for Text Analytics features:\n  {install}"
            return msg

        lines = ["Text Analytics Diagnostics (inside the actual worker python):"]
        py = data.get("python_used_by_worker", "unknown")
        lines.append(f"  Python executable used: {py}")
        lines.append(f"  spaCy version: {data.get('spacy_version', 'N/A')}")
        lines.append(f"  textdescriptives: {'Installed' if data.get('has_textdescriptives') else 'Missing'}")

        models = data.get("models") or []
        if models:
            lines.append(f"  Installed Models ({len(models)}):")
            for m in models:
                lines.append(f"    - {m}")
        else:
            lines.append("  No models detected/loaded.")

        lines.append(f"  transformers: {'Installed' if data.get('has_transformers') else 'Missing'}")
        if data.get("has_transformers"):
            lines.append(f"  transformers version: {data.get('transformers_version', 'N/A')}")
        lines.append(f"  torch: {'Installed' if data.get('has_torch') else 'Missing'}")
        if data.get("has_torch"):
            lines.append(f"  torch version: {data.get('torch_version', 'N/A')}")
        has_sklearn = bool(data.get("has_sklearn"))
        lines.append(f"  scikit-learn: {'Installed' if has_sklearn else 'Missing'}")
        if has_sklearn:
            lines.append(f"  scikit-learn version: {data.get('sklearn_version', 'N/A')}")

        install = data.get("text_analytics_install")
        if install:
            lines.append("\nPackages needed for everything this dialog supports")
            lines.append("(Readability with real scores, Entities, Key Phrases, Topics, Sentiment, etc.):")
            lines.append(f"  {install}")

        lines.append("\nInstall the packages listed above into the Python the worker is using.")
        lines.append("Click Check Venv again after installing.")

        return "\n".join(lines)

    if helper in ("readability", "entities", "key_phrases", "chunks"):
        if result.get("status") != "ok":
            return _text_analytics_error_line(result)

        if helper == "readability":
            lines = []
            rd = data.get("readability") or {}
            ds = data.get("descriptive_stats") or {}
            if rd:
                lines.append("Readability (via textdescriptives):")
                lines.extend(_format_metric_lines(rd))
            if ds:
                lines.append("\nDescriptive stats:")
                lines.extend(_format_metric_lines(ds))
            meta = data.get("meta") or {}
            if meta:
                lines.append(f"\nModel: {meta.get('model')}  Lang: {meta.get('lang')}")
            return "\n".join(lines) if lines else "No readability metrics returned."

        if helper == "entities":
            ents = data.get("entities") or []
            if not ents:
                return "No entities found."
            lines = [f"Entities ({len(ents)}):"]
            for e in ents[:30]:
                lines.append(f"  {e.get('text')} [{e.get('label')}]")
            if len(ents) > 30:
                lines.append(f"  ... and {len(ents) - 30} more")
            return "\n".join(lines)

        if helper in ("key_phrases", "chunks"):
            kps = data.get("key_phrases") or []
            if not kps:
                return "No key phrases found."
            lines = [f"Key Phrases ({len(kps)}):"]
            for kp in kps[:30]:
                lines.append(f"  {kp.get('text')} (lemma: {kp.get('lemma')})")
            if len(kps) > 30:
                lines.append(f"  ... and {len(kps) - 30} more")
            return "\n".join(lines)

    if helper == "topics":
        tops = data.get("topics") or []
        if result.get("status") == "ok" and tops:
            lines = [f"Topics ({len(tops)}):"]
            for t in tops:
                tid = t.get("id")
                terms = ", ".join(t.get("terms", [])[:6])
                lines.append(f"  Topic {tid}: {terms}")
            assigns = data.get("assignments") or []
            if assigns:
                lines.append(f"\nSection assignments ({len(assigns)}):")
                cnt = Counter(a.get("dominant_topic") for a in assigns)
                for tid, c in sorted(cnt.items()):
                    lines.append(f"  Topic {tid}: {c} section(s)")
            meta = data.get("meta") or {}
            if meta:
                lines.append(f"\nMeta: {meta}")
            return "\n".join(lines)

        # Bugfix: topics error formatting
        # What was wrong: topics MISSING_PACKAGE in venv returned a raw dict because of early status != "ok" return,
        # hiding the install hint and suggested install command.
        # How it happened: venv status was "error" for missing packages, which bypassed helper formatting.
        # Why this change: inspect payload error and hint fields even when status is not ok.
        err = data.get("error") or data.get("note")
        if err and "MISSING_PACKAGE" in str(err):
            parts = ["Topics: MISSING_PACKAGE"]
            hint = str(data.get("install_hint") or "").strip()
            install = str(data.get("install") or "").strip()
            if hint:
                parts.append(hint)
            if install:
                parts.append(f"Suggested install: {install}")
            return "\n\n".join(parts)
        if err:
            return f"Topics: {err}"
        if result.get("status") != "ok":
            return _text_analytics_error_line(result)
        return "No topics found."

    if helper == "sentiment":
        # Bugfix: sentiment error formatting
        # What was wrong: sentiment MISSING_PACKAGE and load failures returned a raw dict because of
        # early status != "ok" return, or displayed neutral (score: 0.0) if payload had default scores.
        # How it happened: status: "error" was not handled by sentiment formatter, and payload error
        # wasn't checked before score.
        # Why this change: inspect payload error first; format install hints, install commands, and details;
        # fall back to generic line when outer status is error without payload error.
        err = data.get("error")
        if result.get("status") != "ok" or err is not None:
            if err is None:
                return _text_analytics_error_line(result)
            err_str = str(err)
            if "MISSING_PACKAGE" in err_str:
                msg = "Sentiment: MISSING_PACKAGE (the transformers package or its dependencies could not be imported)"
            elif "MODEL_LOAD_FAILED" in err_str or "LOAD_FAILED" in err_str:
                msg = "Sentiment: model load failed (package is present, but loading the model/tokenizer failed)"
            else:
                msg = f"Sentiment error: {err}"
            hint = str(data.get("install_hint") or "").strip()
            if hint:
                msg += f"\n\n{hint}"
            install = str(data.get("install") or "").strip()
            if install:
                msg += f"\n\nSuggested install: {install}"
            detail = str(data.get("detail") or "").strip()
            if detail:
                msg += f"\n\nReal error details from the worker process:\n{detail}"
            return msg

        sent = data.get("sentiment") or {}
        if not sent or "score" not in sent:
            return "No sentiment data."
        lines = [f"Sentiment: {sent.get('label')} (score: {sent.get('score')})"]
        per = data.get("per_section") or []
        if per:
            lines.append(f"\nBy section ({len(per)}):")
            for p in per:
                idx = p.get("section_index")
                lines.append(f"  [{idx}] {p.get('label')} ({p.get('score')})")
        return "\n".join(lines)

    if result.get("status") != "ok":
        return _text_analytics_error_line(result)

    try:
        return json.dumps(data, indent=2, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return "Error: Text analytics failed."


def _result_to_html_table(data: dict[str, Any]) -> str:
    """Shared: turn analysis result data into a compact bordered HTML table.

    Cell text is double-escaped. ``insert_content_at_position`` unescapes once
    before the StarWriter HTML filter, so a single ``html.escape`` would turn
    a lemma such as ``<script>`` back into markup.
    """
    from plugin.scripting.python_runner import _html_insert_text

    rows: list[str] = []

    rd = data.get("readability") or {}
    ds = data.get("descriptive_stats") or {}
    for k, v in {**rd, **ds}.items():
        if isinstance(v, (int, float, str)):
            val = f"{v:.3f}" if isinstance(v, float) else str(v)
            rows.append(f"<tr><td>{_html_insert_text(k)}</td><td>{_html_insert_text(val)}</td></tr>")

    ents = data.get("entities") or []
    if ents:
        labels: dict[str, int] = {}
        for e in ents:
            lab = e.get("label", "?")
            labels[lab] = labels.get(lab, 0) + 1
        for lab, cnt in sorted(labels.items(), key=lambda x: -x[1])[:12]:
            rows.append(f"<tr><td>{_html_insert_text(f'entity:{lab}')}</td><td>{cnt}</td></tr>")

    kps = data.get("key_phrases") or []
    if kps:
        top = ", ".join(kp.get("lemma") or kp.get("text") or "" for kp in kps[:8])
        rows.append(f"<tr><td>key_phrases</td><td>{_html_insert_text(top)}</td></tr>")

    # Topics (fancier text analytics): show top terms per topic + section assignments if present.
    topics = data.get("topics") or []
    if topics:
        for t in topics[:6]:
            tid = t.get("id", "?")
            terms = ", ".join(t.get("terms", [])[:5])
            rows.append(
                f"<tr><td>{_html_insert_text(f'topic {tid}')}</td><td>{_html_insert_text(terms)}</td></tr>"
            )
        assigns = data.get("assignments") or []
        if assigns:
            # Compact: show how many sections map to each topic
            counts = Counter(a.get("dominant_topic") for a in assigns)
            summary = "; ".join(f"t{tid}:{cnt}" for tid, cnt in sorted(counts.items()))
            rows.append(f"<tr><td>topic sections</td><td>{_html_insert_text(summary)}</td></tr>")

    # Sentiment: overall + summary of per-section if available
    sent = data.get("sentiment") or {}
    if sent and isinstance(sent, dict) and "score" in sent:
        sc = sent.get("score", 0)
        lab = sent.get("label", "neutral")
        rows.append(f"<tr><td>sentiment</td><td>{_html_insert_text(f'{lab} ({sc})')}</td></tr>")
    per_sec = data.get("per_section") or []
    if per_sec:
        # Count labels across sections
        labels = Counter(str(p.get("label")) for p in per_sec if isinstance(p, dict) and p.get("label") is not None)
        if labels:
            summary = "; ".join(f"{lab}:{cnt}" for lab, cnt in labels.most_common())
            rows.append(f"<tr><td>sections</td><td>{_html_insert_text(summary)}</td></tr>")

    if not rows:
        return ""
    return '<table border="1" style="border-collapse:collapse"><tbody>' + "".join(rows) + "</tbody></table>"


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

    # What was wrong: an empty inner result ({}) fell back to the outer envelope, dumping status and note into the doc.
    # How it happened: (result or {}).get("result") or result or {} treated an empty inner dict as falsy.
    # Why this change: select result["result"] when it is a dict (including {}), falling back to result only when "result" key is absent.
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        data = result["result"]
    elif isinstance(result, dict) and "result" not in result:
        data = result
    else:
        data = {}
    html = _result_to_html_table(data)
    if not html:
        # Fallback to a small JSON snippet so something is inserted
        from plugin.scripting.python_runner import _html_insert_text

        html = "<pre>" + _html_insert_text(json.dumps(data, indent=2, ensure_ascii=False)[:1500]) + "</pre>"

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
    return 1

