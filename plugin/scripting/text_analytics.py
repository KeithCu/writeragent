"""High-quality multilingual text analytics powered by spaCy + textdescriptives.

This module is intended to be imported and executed inside the user's Python venv
(via the trusted worker stub). It requires:

    pip install spacy textdescriptives

And at least one suitable spaCy model, e.g.:

    python -m spacy download en_core_web_sm
    python -m spacy download xx_sent_ud_sm   # good multilingual base (parser + tagger)

The public entry point for the client/worker is `run_text_analytics`.
Direct high-level helpers are also provided for use from within venv scripts.
"""

from __future__ import annotations

from typing import Any

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
    import spacy  # ty: ignore[unresolved-import]

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
                import textdescriptives as td  # noqa: F401  # ty: ignore[unresolved-import]

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

    if lang is None:
        lang = _get_lang_from_context(context)

    nlp = _load_nlp(lang)

    doc = nlp(text)

    result: dict[str, Any] = {"status": "ok"}

    # --- textdescriptives metrics (the high-quality path) ---
    try:
        import textdescriptives as td  # ty: ignore[unresolved-import]

        # extract_dict works on a single Doc and returns a flat dict of metrics.
        td_metrics = td.extract_dict(doc)
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

    # --- Entities (high quality multilingual NER) ---
    ents = []
    for ent in doc.ents:
        ents.append({
            "text": ent.text,
            "label": ent.label_,
            "start_char": ent.start_char,
            "end_char": ent.end_char,
        })
    result["entities"] = ents

    # --- Key phrases via noun chunks (good across languages with a parser) ---
    # We lemmatize and deduplicate for usefulness.
    seen = set()
    key_phrases = []
    for chunk in doc.noun_chunks:
        # Use lemma for canonical form when possible
        lemma = " ".join([t.lemma_ if t.lemma_ != "-PRON-" else t.text.lower() for t in chunk])
        key = lemma.lower().strip()
        if key and key not in seen and len(key) > 1:
            seen.add(key)
            key_phrases.append({
                "text": chunk.text,
                "lemma": lemma,
                "root": chunk.root.text if chunk.root else None,
            })
    # Limit to a reasonable number for UI / reports
    result["key_phrases"] = key_phrases[:25]

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

    # Meta
    result["meta"] = {
        "model": nlp.meta.get("name") or getattr(nlp, "path", None) or "unknown",
        "lang": getattr(doc._, "lang", None) or (lang or "unknown"),
        "has_textdescriptives": "textdescriptives" in nlp.pipe_names,
    }

    return {"status": "ok", "result": result}


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

    # Normalize text input (support list of sections)
    if isinstance(text, list):
        text = "\n\n".join(str(t) for t in text if t)

    if not text or not str(text).strip():
        return {"status": "ok", "result": {}, "note": "no text provided"}

    lang = params.get("lang") or (context or {}).get("lang")

    # For the minimal dialog we mainly use "full" or specific helpers.
    if helper in ("full", "analyze", "all"):
        return analyze_text(str(text), lang=lang, context=context)

    if helper in ("readability", "stats", "descriptive"):
        # Still run full analyze but the caller can pick the subset.
        # This keeps the implementation simple and high quality.
        full = analyze_text(str(text), lang=lang, context=context)
        res = full.get("result", {})
        return {
            "status": "ok",
            "result": {
                "readability": res.get("readability", {}),
                "descriptive_stats": res.get("descriptive_stats", {}),
                "meta": res.get("meta", {}),
            },
        }

    if helper in ("entities", "ner"):
        full = analyze_text(str(text), lang=lang, context=context)
        return {"status": "ok", "result": {"entities": full.get("result", {}).get("entities", [])} }

    if helper in ("key_phrases", "keyphrases", "chunks"):
        full = analyze_text(str(text), lang=lang, context=context)
        return {"status": "ok", "result": {"key_phrases": full.get("result", {}).get("key_phrases", [])} }

    # Default to full high-quality analysis
    return analyze_text(str(text), lang=lang, context=context)


# ---------------------------------------------------------------------------
# Light registration surface (for Run Python Script category, etc.)
# No pure-Python implementations live here anymore.
# ---------------------------------------------------------------------------

TEXT_ANALYTICS_HEADER_PREFIX = "# writeragent:text"


def get_text_analytics_script_templates() -> dict[str, str]:
    """Return templates for the Run Python Script dialog (empty for now).

    Users can write their own using `from plugin.scripting.text_analytics import analyze_text, run_text_analytics`.
    The modeless dialog is the primary minimal UI.
    """
    return {}
