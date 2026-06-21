"""High-quality multilingual text analytics powered by spaCy + textdescriptives.

This module is intended to be imported and executed inside the user's Python venv
(via the trusted worker stub). It requires:

    uv pip install spacy textdescriptives

And at least one suitable spaCy model, e.g.:

    python -m spacy download en_core_web_sm
    python -m spacy download xx_sent_ud_sm   # good multilingual base (parser + tagger)

The public entry point for the client/worker is `run_text_analytics`.
Direct high-level helpers are also provided for use from within venv scripts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast

# These will only succeed when the module is imported inside a properly equipped venv.
# On the LibreOffice host side we never import this directly for computation.


def _load_nlp(lang: str | None = None) -> Any:
    """Load the best available spaCy pipeline for the requested language.

    Strategy for high quality + multilingual support:
    - Prefer a language-specific model when we know the language (better accuracy).
    - Fall back to strong multilingual models that include senter/tagger/parser
      (required for good readability and linguistic metrics via textdescriptives).
    - `xx_sent_ud_sm` is an excellent multilingual base for 100+ languages.
    """
    import spacy

    lang = (lang or "").lower().strip()[:2] or None

    # Ordered preference lists. We try until one loads.
    candidates: list[str] = []

    if lang:
        # Language-specific first (highest quality for that language)
        if lang == "en":
            candidates.extend(["en_core_web_sm", "en_core_web_md", "en_core_web_lg"])
        elif lang == "de":
            candidates.extend(["de_core_news_sm", "de_core_news_md", "de_core_news_lg"])
        elif lang == "fr":
            candidates.extend(["fr_core_news_sm", "fr_core_news_md", "fr_core_news_lg"])
        elif lang == "es":
            candidates.extend(["es_core_news_sm", "es_core_news_md", "es_core_news_lg"])
        elif lang == "it":
            candidates.extend(["it_core_news_sm"])
        elif lang == "pt":
            candidates.extend(["pt_core_news_sm"])
        elif lang == "ru":
            candidates.extend(["ru_core_news_sm"])
        elif lang == "zh":
            candidates.extend(["zh_core_web_sm"])
        elif lang == "ja":
            candidates.extend(["ja_core_news_sm"])
        elif lang == "nl":
            candidates.extend(["nl_core_news_sm"])
        # Add more as needed; the multilingual fallback will catch the rest.

        # Also try the xx multilingual for the language if a specific one exists
        candidates.append("xx_sent_ud_sm")

    # Always have strong multilingual fallbacks at the end.
    candidates.extend([
        "xx_sent_ud_sm",   # excellent multilingual: tokenizer + senter + tagger + parser
        "xx_ent_wiki_sm",  # good for cross-lingual NER
    ])

    last_err: Exception | None = None
    for model_name in candidates:
        try:
            nlp = spacy.load(model_name)
            # Add textdescriptives for high-quality readability, stats, complexity, etc.
            # It registers several components; using the package's convenience is fine.
            try:
                import textdescriptives as td  # noqa: F401

                # textdescriptives >= 2 adds components under "textdescriptives/..."
                # The package also provides a simple way to ensure the basics are there.
                # We add the main pipeline if not already present.
                if "textdescriptives" not in nlp.pipe_names:
                    nlp.add_pipe("textdescriptives")
            except Exception:
                # textdescriptives not installed — we can still do entities + chunks.
                pass  # nosec B110 - best-effort optional enhancement

            return nlp
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(
        f"Could not load any suitable spaCy model. Tried: {candidates}. "
        f"Last error: {last_err}. "
        "Install a model with `python -m spacy download xx_sent_ud_sm` (or a language-specific one)."
    ) from last_err


def _get_lang_from_context(context: dict[str, Any] | None) -> str | None:
    if not context:
        return None
    # Common places we might receive language info from the host.
    for key in ("lang", "language", "locale", "doc_lang"):
        val = context.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


# ---------------------------------------------------------------------------
# Core high-quality analyses
# ---------------------------------------------------------------------------

def _run_nlp_doc(text: str, lang: str | None, context: dict[str, Any] | None) -> "tuple[Any, Any, str | None]":
    """Load the model and run the spaCy pipeline once. Returns (nlp, doc, resolved_lang).

    Factored out so narrow helpers (entities, key_phrases, readability) each call
    nlp(text) exactly once without running the full analyze_text stack.
    """
    if lang is None:
        lang = _get_lang_from_context(context)
    nlp = _load_nlp(lang)
    doc = nlp(text)
    return nlp, doc, lang


def _extract_meta(nlp: Any, doc: Any, lang: str | None) -> dict[str, Any]:
    return {
        "model": nlp.meta.get("name") or getattr(nlp, "path", None) or "unknown",
        "lang": getattr(doc._, "lang", None) or (lang or "unknown"),
        "has_textdescriptives": "textdescriptives" in nlp.pipe_names,
    }


def _extract_readability(doc: Any) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Return (readability_dict, descriptive_stats_dict) from textdescriptives (or basic fallback)."""
    try:
        import textdescriptives as td

        td_metrics = cast("Any", td.extract_dict(doc))
        return td_metrics.get("readability", {}), td_metrics.get("descriptive_stats", {})
    except Exception:
        # textdescriptives not available — fall back to basic spaCy stats.
        return {}, {"n_tokens": len(doc), "n_sents": len(list(doc.sents))}


def _extract_entities(doc: Any) -> list[dict[str, Any]]:
    return [
        {"text": ent.text, "label": ent.label_, "start_char": ent.start_char, "end_char": ent.end_char}
        for ent in doc.ents
    ]


def _extract_key_phrases(doc: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    key_phrases = []
    for chunk in doc.noun_chunks:
        lemma = " ".join([t.lemma_ if t.lemma_ != "-PRON-" else t.text.lower() for t in chunk])
        key = lemma.lower().strip()
        if key and key not in seen and len(key) > 1:
            seen.add(key)
            key_phrases.append({"text": chunk.text, "lemma": lemma, "root": chunk.root.text if chunk.root else None})
    return key_phrases[:25]


def analyze_text(text: str, *, lang: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a rich multilingual analysis on a single text using spaCy + textdescriptives.

    Returns a dict with:
      - readability (via textdescriptives when available)
      - descriptive_stats
      - entities
      - key_phrases (from noun chunks)
      - linguistic_profile (pos proportions, complexity signals)
      - meta (model used, language, etc.)
    """
    if not text or not text.strip():
        return {"status": "ok", "result": {}, "note": "empty text"}

    nlp, doc, lang = _run_nlp_doc(text, lang, context)

    result: dict[str, Any] = {"status": "ok"}

    # --- textdescriptives metrics (the high-quality path) ---
    try:
        import textdescriptives as td

        # extract_dict works on a single Doc and returns a flat dict of metrics.
        td_metrics = cast("Any", td.extract_dict(doc))
        # td_metrics is usually a dict with keys like 'readability', 'descriptive_stats', etc.
        # We surface the most useful top-level groups.
        result["descriptive_stats"] = td_metrics.get("descriptive_stats", {})
        result["readability"] = td_metrics.get("readability", {})
        result["dependency_distance"] = td_metrics.get("dependency_distance", {})
        result["pos_proportions"] = td_metrics.get("pos_proportions", {})
        # coherence and quality may or may not be present depending on version
        if "coherence" in td_metrics:
            result["coherence"] = td_metrics["coherence"]
        if "quality" in td_metrics:
            result["quality"] = td_metrics["quality"]
    except Exception:
        # textdescriptives not available — fall back to basic spaCy stats.
        result["descriptive_stats"] = {
            "n_tokens": len(doc),
            "n_sents": len(list(doc.sents)),
        }

    result["entities"] = _extract_entities(doc)
    result["key_phrases"] = _extract_key_phrases(doc)

    # --- Linguistic profile (always useful) ---
    pos_counts: dict[str, int] = {}
    for token in doc:
        if token.is_punct or token.is_space:
            continue
        pos = token.pos_
        pos_counts[pos] = pos_counts.get(pos, 0) + 1

    total = sum(pos_counts.values()) or 1
    pos_props = {k: round(v / total, 4) for k, v in pos_counts.items()}

    result["linguistic_profile"] = {
        "pos_proportions": pos_props,
        "n_tokens": len(doc),
        "n_sents": len(list(doc.sents)),
    }

    result["meta"] = _extract_meta(nlp, doc, lang)

    return {"status": "ok", "result": result}


def check_diagnostics() -> dict[str, Any]:
    """Perform self-diagnostics of the spaCy + textdescriptives installation."""
    try:
        import spacy
        has_td = False
        try:
            import textdescriptives as td  # noqa: F401
            has_td = True
        except ImportError:
            pass

        # Try to find installed models
        models: list[str] = []
        try:
            models = spacy.util.get_installed_models()
        except Exception:
            for m in ("xx_sent_ud_sm", "en_core_web_sm", "de_core_news_sm", "fr_core_news_sm", "es_core_news_sm"):
                try:
                    spacy.load(m)
                    models.append(m)
                except Exception:
                    pass

        return {
            "status": "ok",
            "spacy_version": getattr(spacy, "__version__", "unknown"),
            "has_textdescriptives": has_td,
            "models": models,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


def run_text_analytics(
    spec: dict[str, Any] | str,
    text: str | list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatcher used by the trusted worker stub (and by Run Python Script templates).

    `spec` can be a string like "full", "readability", "entities", "key_phrases"
    or a dict {"helper": "...", "params": {...}}.

    `text` is the document text (or list of section texts).
    """
    if isinstance(spec, dict):
        helper = spec.get("helper") or spec.get("type") or "full"
        params = spec.get("params", {}) or {}
    else:
        helper = str(spec or "full")
        params = {}

    if helper in ("diagnostics", "check"):
        return {"status": "ok", "result": check_diagnostics()}

    # Normalize text input (support list of sections)
    if isinstance(text, list):
        text = "\n\n".join(str(t) for t in text if t)

    if not text or not str(text).strip():
        return {"status": "ok", "result": {}, "note": "no text provided"}

    lang = params.get("lang") or (context or {}).get("lang")

    # For the minimal dialog we mainly use "full" or specific helpers.
    if helper in ("full", "analyze", "all"):
        return analyze_text(str(text), lang=lang, context=context)

    # Narrow helpers: run nlp(text) once and extract only the requested subset,
    # skipping the unneeded NER / noun-chunk / textdescriptives work.
    if helper in ("readability", "stats", "descriptive"):
        nlp, doc, resolved_lang = _run_nlp_doc(str(text), lang, context)
        rd, ds = _extract_readability(doc)
        return {
            "status": "ok",
            "result": {
                "readability": rd,
                "descriptive_stats": ds,
                "meta": _extract_meta(nlp, doc, resolved_lang),
            },
        }

    if helper in ("entities", "ner"):
        nlp, doc, resolved_lang = _run_nlp_doc(str(text), lang, context)
        return {"status": "ok", "result": {"entities": _extract_entities(doc), "meta": _extract_meta(nlp, doc, resolved_lang)}}

    if helper in ("key_phrases", "keyphrases", "chunks"):
        nlp, doc, resolved_lang = _run_nlp_doc(str(text), lang, context)
        return {"status": "ok", "result": {"key_phrases": _extract_key_phrases(doc), "meta": _extract_meta(nlp, doc, resolved_lang)}}

    # Default to full high-quality analysis
    return analyze_text(str(text), lang=lang, context=context)


# ---------------------------------------------------------------------------
# Light registration surface (for Run Python Script category, etc.)
# No pure-Python implementations live here anymore.
# ---------------------------------------------------------------------------

TEXT_ANALYTICS_HEADER_PREFIX = "# writeragent:text"


def get_text_analytics_script_templates() -> dict[str, str]:
    """Return built-in text analytics helper scripts keyed by helper name.

    These appear under Text Analytics Helpers in Run Python Script (Writer documents).
    The implementation requires spaCy + textdescriptives + a model in the venv.
    """
    # Exclude internal/UI helper commands from script templates
    public_helpers = {h for h in HELPER_NAMES if h not in ("diagnostics", "check")}
    return {helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {}))) for helper in sorted(public_helpers)}


# ---------------------------------------------------------------------------
# Host-side registration + runner support (templates, header parsing, run/insert)
# These are called on the LibreOffice host; compute still happens in the venv.
# Imports are inside functions so importing this module inside the venv child
# (for the trusted stub) does not pull host-only deps.
# ---------------------------------------------------------------------------

HELPER_NAMES = frozenset({"full", "readability", "entities", "key_phrases", "diagnostics", "check"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "full": {},
    "readability": {},
    "entities": {},
    "key_phrases": {},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "full": "Full spaCy + textdescriptives analysis (readability, entities, key phrases)",
    "readability": "Readability scores and descriptive stats via textdescriptives",
    "entities": "Named entity recognition (multilingual NER)",
    "key_phrases": "Key phrases via noun chunks (lemmatized)",
}


def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{TEXT_ANALYTICS_HEADER_PREFIX} helper={helper} params={params_json}\n"
        f"# {desc}\n"
        f"# Works on Writer documents (uses current document text).\n"
        f"from writeragent.scripting.text_analytics import run_text_analytics\n\n"
        f"result = run_text_analytics(\n"
        f'    {{"helper": "{helper}", "params": {params_json}}},\n'
        f"    text=None,  # filled by runner from document\n"
        f"    context={{}},\n"
        f")\n"
    )


_TEXT_ANALYTICS_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:text\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TextAnalyticsScriptMeta:
    helper: str
    params: dict[str, Any]


def parse_text_analytics_script_header(code: str) -> TextAnalyticsScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied text analytics script."""
    if not code or TEXT_ANALYTICS_HEADER_PREFIX not in code:
        return None
    match = _TEXT_ANALYTICS_HEADER_RE.search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper not in HELPER_NAMES:
        return None
    try:
        params = json.loads(match.group(2))
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return TextAnalyticsScriptMeta(helper=helper, params=params)


def supports_text_analytics_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Text Analytics Helpers for *doc*."""
    if doc is None:
        return False
    try:
        from plugin.doc.document_helpers import is_writer

        return is_writer(doc)
    except Exception:
        return False


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

    # Extract text on host (whole document; dialog offers selection variant).
    text = _get_writer_text(doc)

    spec: dict[str, Any] = {"helper": name}
    if params:
        spec["params"] = params

    # Pass doc language when available so the venv side can pick a better model.
    context: dict[str, Any] = {}
    lang = _get_doc_lang(doc)
    if lang:
        context["lang"] = lang

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
        for k in ("readability", "descriptive_stats", "entities", "key_phrases", "meta"):
            if k in res:
                return True
    # Also accept the narrow shapes returned by specific helpers
    for k in ("entities", "key_phrases", "readability"):
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
