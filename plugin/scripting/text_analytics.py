"""High-quality multilingual text analytics powered by spaCy + textdescriptives.

Topics uses scikit-learn (NMF). Sentiment uses transformers + a multilingual model
(default XLM-RoBERTa based for good coverage across 34 locales).

This module is intended to be imported and executed inside the user's Python venv
(via the trusted worker stub). It requires:

    uv pip install spacy textdescriptives
    # For sentiment (CPU wheels):
    uv pip install transformers torch --index-url https://download.pytorch.org/whl/cpu

And at least one suitable spaCy model, e.g.:

    python -m spacy download en_core_web_sm
    python -m spacy download xx_sent_ud_sm   # good multilingual base (parser + tagger)

The public entry point for the client/worker is `run_text_analytics`.
Direct high-level helpers are also provided for use from within venv scripts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from plugin.doc.document_helpers import HeadingTreeNode

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
            candidates.extend(["it_core_news_sm", "it_core_news_md", "it_core_news_lg"])
        elif lang == "pt":
            candidates.extend(["pt_core_news_sm", "pt_core_news_md", "pt_core_news_lg"])
        elif lang == "ru":
            candidates.extend(["ru_core_news_sm", "ru_core_news_md", "ru_core_news_lg"])
        elif lang == "zh":
            candidates.extend(["zh_core_web_sm", "zh_core_web_md", "zh_core_web_lg"])
        elif lang == "ja":
            candidates.extend(["ja_core_news_sm", "ja_core_news_md", "ja_core_news_lg"])
        elif lang == "nl":
            candidates.extend(["nl_core_news_sm", "nl_core_news_md", "nl_core_news_lg"])
        elif lang == "ko":
            candidates.extend(["ko_core_news_sm", "ko_core_news_md", "ko_core_news_lg"])
        elif lang == "pl":
            candidates.extend(["pl_core_news_sm", "pl_core_news_md", "pl_core_news_lg"])
        elif lang == "ca":
            candidates.extend(["ca_core_news_sm", "ca_core_news_md", "ca_core_news_lg"])
        elif lang == "da":
            candidates.extend(["da_core_news_sm", "da_core_news_md", "da_core_news_lg"])
        elif lang == "el":
            candidates.extend(["el_core_news_sm", "el_core_news_md", "el_core_news_lg"])
        elif lang == "fi":
            candidates.extend(["fi_core_news_sm", "fi_core_news_md", "fi_core_news_lg"])
        elif lang == "hr":
            candidates.extend(["hr_core_news_sm", "hr_core_news_md", "hr_core_news_lg"])
        elif lang == "lt":
            candidates.extend(["lt_core_news_sm", "lt_core_news_md", "lt_core_news_lg"])
        elif lang == "mk":
            candidates.extend(["mk_core_news_sm"])
        elif lang == "nb":
            candidates.extend(["nb_core_news_sm", "nb_core_news_md", "nb_core_news_lg"])
        elif lang == "ro":
            candidates.extend(["ro_core_news_sm", "ro_core_news_md", "ro_core_news_lg"])
        elif lang == "sl":
            candidates.extend(["sl_core_news_sm", "sl_core_news_md", "sl_core_news_lg"])
        elif lang == "sv":
            candidates.extend(["sv_core_news_sm", "sv_core_news_md", "sv_core_news_lg"])
        elif lang == "uk":
            candidates.extend(["uk_core_news_sm", "uk_core_news_md", "uk_core_news_lg"])
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
            # Add textdescriptives components for readability and stats.
            # In textdescriptives >=2 the factories are namespaced (e.g. textdescriptives/readability).
            # Adding the specific ones we need ensures the extensions are registered.
            try:
                import textdescriptives as td  # noqa: F401
                for comp in ("textdescriptives/descriptive_stats", "textdescriptives/readability"):
                    if comp not in nlp.pipe_names:
                        nlp.add_pipe(comp)
            except Exception:
                # textdescriptives not installed — we can still do entities + chunks.
                pass  # nosec B110

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
        "has_textdescriptives": any(p.startswith("textdescriptives/") for p in nlp.pipe_names),
    }


def _extract_readability(doc: Any) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Return (readability_dict, descriptive_stats_dict) from textdescriptives (or basic fallback).

    textdescriptives >=2 returns a flat dict (after the components have run on the doc).
    We split it into the two groups the rest of the code expects.
    """
    try:
        import textdescriptives as td

        m = cast("Any", td.extract_dict(doc))
        if isinstance(m, list):
            m = m[0]
        # readability metrics (the formula-based scores)
        readability_keys = {
            "flesch_reading_ease", "flesch_kincaid_grade", "smog",
            "gunning_fog", "automated_readability_index", "coleman_liau_index",
            "lix", "rix"
        }
        rd = {k: v for k, v in m.items() if k in readability_keys}
        # descriptive stats (counts and averages)
        descriptive_keys = {
            "n_tokens", "n_unique_tokens", "proportion_unique_tokens",
            "n_characters", "n_sentences",
            "token_length_mean", "token_length_median", "token_length_std",
            "sentence_length_mean", "sentence_length_median", "sentence_length_std",
            "syllables_per_token_mean", "syllables_per_token_median", "syllables_per_token_std"
        }
        ds = {k: v for k, v in m.items() if k in descriptive_keys or k.startswith("n_")}
        return rd, ds
    except Exception:
        # textdescriptives not available or failed to extract — fall back to basic spaCy stats.
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


def _extract_topics(text: str | list[str], n_topics: int = 4) -> dict[str, Any]:
    """Topic modeling via TF-IDF + NMF (from scikit-learn).

    Accepts either a single string (whole document) or list[str] of section texts.
    When given sections, also returns per-section dominant topic assignments.

    This is intentionally simple and dependency-light on the scientific stack.
    No spaCy required for this helper.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import NMF
    except ImportError:
        # Signal clearly; callers (dialog, insert, scripts) will surface a helpful message.
        return {
            "topics": [],
            "error": "MISSING_PACKAGE",
            "install_hint": "scikit-learn (part of the Data Analysis / EDA stack)",
            "install": "uv pip install scikit-learn",
        }

    # Normalize input: support list of sections for better "by section" topic structure.
    if isinstance(text, (list, tuple)):
        docs = [str(t).strip() for t in text if str(t).strip()]
        is_multi_section = True
    else:
        docs = [str(text).strip()] if str(text or "").strip() else []
        is_multi_section = False

    if not docs:
        return {"topics": [], "note": "no text"}

    # Reasonable bounds: at least 1, at most ~8 or number of sections.
    n_topics = max(1, min(int(n_topics or 4), len(docs), 8))

    # Drop extremely short docs for vectorization stability; keep at least one.
    filtered = [d for d in docs if len(d.split()) >= 8]
    if not filtered:
        filtered = docs[:1]

    try:
        # Multilingual-friendly: no hardcoded English stop_words (users can have mixed docs).
        # ngram up to 2 helps with short technical phrases.
        vectorizer = TfidfVectorizer(
            max_features=1200,
            ngram_range=(1, 2),
            min_df=1,
            strip_accents="unicode",
        )
        X = vectorizer.fit_transform(filtered)
        if X.shape[0] < 1 or X.shape[1] < 2:
            return {"topics": [], "note": "insufficient distinct terms"}

        nmf = NMF(n_components=n_topics, random_state=42, init="nndsvd", max_iter=300, tol=1e-4)
        W = nmf.fit_transform(X)  # (n_docs, n_topics) weights
        H = nmf.components_       # (n_topics, n_terms)

        feature_names = vectorizer.get_feature_names_out()

        topics = []
        for i in range(n_topics):
            top_idx = H[i].argsort()[-7:][::-1]
            terms = [str(feature_names[j]) for j in top_idx]
            total = float(W[:, i].sum() + 1e-9)
            topics.append({
                "id": i,
                "terms": terms,
                "weight": round(total / (W.sum() + 1e-9), 3),
            })

        result: dict[str, Any] = {"topics": topics}

        if is_multi_section and len(W) > 0:
            assignments = []
            for sec_idx, row in enumerate(W):
                if row.size == 0:
                    continue
                dom = int(row.argmax())
                strength = float(row[dom])
                assignments.append({
                    "section_index": sec_idx,
                    "dominant_topic": dom,
                    "strength": round(strength, 3),
                })
            result["assignments"] = assignments
            result["n_sections"] = len(W)

        return result
    except Exception as exc:
        # Don't crash the whole analysis; surface what happened.
        return {"topics": [], "error": f"topic_model_failed: {exc}"}


def _extract_sentiment(text: str | list[str], params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sentiment analysis using transformers + a strong multilingual model (XLM-RoBERTa based).

    Default model provides good cross-lingual performance across many languages.
    This replaced the previous spacytextblob implementation (which had limited multilingual support).

    Returns overall score/label + per-section results when a list of sections is passed
    (reusing the same section extraction as topics for "by section" analysis on Writer docs).

    Config override via text_analytics_sentiment_model (JSON setting) for future flexibility.
    For now only "transformers" engine is supported.
    """
    params = params or {}
    if isinstance(text, (list, tuple)):
        sections = [str(t).strip() for t in text if str(t).strip()]
        is_multi = True
    else:
        sections = [str(text).strip()] if str(text or "").strip() else []
        is_multi = False

    if not sections:
        return {"overall": {"score": 0.0, "label": "neutral"}, "note": "no text"}

    model = params.get("model") or "cardiffnlp/twitter-xlm-roberta-base-sentiment"

    try:
        from transformers import pipeline
    except Exception as exc:
        return {
            "overall": {"score": 0.0, "label": "neutral"},
            "error": "MISSING_PACKAGE",
            "install_hint": "transformers (with CPU torch) for multilingual sentiment",
            "install": "uv pip install transformers torch --index-url https://download.pytorch.org/whl/cpu",
            "detail": f"Could not import transformers: {str(exc)}",
        }

    try:
        # Let device fall back to default behavior.
        # The model is loaded once per warm worker invocation.
        clf = pipeline("sentiment-analysis", model=model)
    except Exception as exc:
        return {
            "overall": {"score": 0.0, "label": "neutral"},
            "error": "SENTIMENT_MODEL_LOAD_FAILED",
            "install_hint": f"Failed to load model '{model}'. This may be a download, cache, network, compatibility, or missing tokenizer library issue (e.g. sentencepiece). Not just the base package.",
            "install": "uv pip install transformers torch sentencepiece  (and ensure network access for first model download)",
            "detail": str(exc),
        }

    per_section: list[dict[str, Any]] = []
    scores: list[float] = []

    for i, sec in enumerate(sections):
        # Truncate conservatively for transformer limits; sections from headings are usually reasonable.
        text_for_clf = sec[:512] if len(sec) > 512 else sec
        res = clf(text_for_clf)[0]
        # HF returns label like 'positive'/'negative'/'neutral' and score 0-1.
        label = str(res.get("label", "neutral")).lower()
        sc = float(res.get("score", 0.0))
        # Map to our consistent labels.
        if label in ("pos", "positive", "1", "5"):
            label = "positive"
        elif label in ("neg", "negative", "0"):
            label = "negative"
        else:
            label = "neutral"
        scores.append(sc if label == "positive" else (-sc if label == "negative" else 0.0))
        per_section.append({
            "section_index": i,
            "score": round(sc if label != "negative" else -sc, 3),
            "label": label,
        })

    if is_multi and scores:
        avg_score = sum(scores) / len(scores)
        # Derive overall label from avg.
        if avg_score > 0.15:
            overall_label = "positive"
        elif avg_score < -0.15:
            overall_label = "negative"
        else:
            overall_label = "neutral"
        overall = {
            "score": round(avg_score, 3),
            "label": overall_label,
        }
    else:
        overall = per_section[0] if per_section else {"score": 0.0, "label": "neutral"}

    res: dict[str, Any] = {"overall": overall}
    if is_multi:
        res["per_section"] = per_section
        res["n_sections"] = len(per_section)
    return res


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
        m = cast("Any", td.extract_dict(doc))
        if isinstance(m, list):
            m = m[0]
        # textdescriptives now returns flat dict. Split into the groups we document.
        readability_keys = {"flesch_reading_ease", "flesch_kincaid_grade", "smog", "gunning_fog",
                            "automated_readability_index", "coleman_liau_index", "lix", "rix"}
        descriptive_keys = {"n_tokens", "n_unique_tokens", "proportion_unique_tokens", "n_characters", "n_sentences",
                            "token_length_mean", "token_length_median", "token_length_std",
                            "sentence_length_mean", "sentence_length_median", "sentence_length_std",
                            "syllables_per_token_mean", "syllables_per_token_median", "syllables_per_token_std"}
        result["readability"] = {k: m[k] for k in readability_keys if k in m}
        result["descriptive_stats"] = {k: m[k] for k in descriptive_keys if k in m}
        # other groups if present (they may be flat too)
        for other in ("dependency_distance", "pos_proportions", "coherence", "quality"):
            if other in m:
                result[other] = m[other]
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


def check_diagnostics(lang: str | None = None) -> dict[str, Any]:
    """Perform self-diagnostics of the packages needed by Text Analytics features.

    This runs *inside the worker* using whatever python the extension resolved
    (based on Settings → Python → python_venv_path, or falling back to LO's python).

    Reports on:
      - spaCy + models (for entities, key phrases, basic stats, language)
      - textdescriptives (for real Readability scores)
      - transformers + torch (for the Sentiment feature using a good multilingual model)

    The output includes the python executable being used and the exact install command.
    """
    # Always capture the python the worker is actually using.
    import sys
    python_exe = getattr(sys, "executable", "unknown")

    lang = (lang or "").lower().strip()[:2] or None
    recommended_model = "xx_sent_ud_sm"
    if lang:
        lang_model_map = {
            "en": "en_core_web_sm",
            "de": "de_core_news_sm",
            "es": "es_core_news_sm",
            "fr": "fr_core_news_sm",
            "it": "it_core_news_sm",
            "pt": "pt_core_news_sm",
            "ru": "ru_core_news_sm",
            "zh": "zh_core_web_sm",
            "ja": "ja_core_news_sm",
            "nl": "nl_core_news_sm",
            "ko": "ko_core_news_sm",
            "pl": "pl_core_news_sm",
            "ca": "ca_core_news_sm",
            "da": "da_core_news_sm",
            "el": "el_core_news_sm",
            "fi": "fi_core_news_sm",
            "hr": "hr_core_news_sm",
            "lt": "lt_core_news_sm",
            "mk": "mk_core_news_sm",
            "nb": "nb_core_news_sm",
            "ro": "ro_core_news_sm",
            "sl": "sl_core_news_sm",
            "sv": "sv_core_news_sm",
            "uk": "uk_core_news_sm",
        }
        recommended_model = lang_model_map.get(lang, "xx_sent_ud_sm")

    # Full command needed for everything the dialog supports.
    text_analytics_install = (
        "uv pip install spacy textdescriptives transformers torch && "
        f"python -m spacy download {recommended_model}"
    )

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

        # Transformers + torch (for Sentiment)
        has_transformers = False
        transformers_version = None
        has_torch = False
        torch_version = None
        try:
            import transformers
            has_transformers = True
            transformers_version = getattr(transformers, "__version__", "unknown")
            try:
                import torch
                has_torch = True
                torch_version = str(getattr(torch, "__version__", "unknown"))
            except ImportError:
                pass
        except ImportError:
            pass

        return {
            "status": "ok",
            "python_used_by_worker": python_exe,
            "spacy_version": getattr(spacy, "__version__", "unknown"),
            "has_textdescriptives": has_td,
            "models": models,
            "has_transformers": has_transformers,
            "transformers_version": transformers_version,
            "has_torch": has_torch,
            "torch_version": torch_version,
            "text_analytics_install": text_analytics_install,
        }
    except Exception as e:
        return {
            "status": "error",
            "python_used_by_worker": python_exe,
            "message": str(e),
            "text_analytics_install": text_analytics_install,
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
        lang = params.get("lang") or (context or {}).get("lang")
        return {"status": "ok", "result": check_diagnostics(lang=lang)}

    # Preserve original list form for section-aware helpers (topics, sentiment).
    # Only join for helpers that expect a single string.
    original_text = text
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

    if helper in ("topics", "topic", "topic_model"):
        # Use preserved original (may still be list) for per-section support.
        raw_for_topics = original_text if isinstance(original_text, list) else str(text or "")
        topic_data = _extract_topics(raw_for_topics, n_topics=params.get("n_topics", 4))
        # Normalize into the same result envelope the rest of the system expects.
        if topic_data.get("error") == "MISSING_PACKAGE":
            return {"status": "ok", "result": topic_data, "note": "install scikit-learn for topics"}
        return {"status": "ok", "result": {"topics": topic_data.get("topics", []), "assignments": topic_data.get("assignments"), "meta": {"n_topics": topic_data.get("n_sections") or len(topic_data.get("topics", [])) or None }}}

    if helper in ("sentiment", "polarity"):
        # Use preserved original (may still be list) for per-section support.
        raw_for_sent = original_text if isinstance(original_text, list) else str(text or "")
        # Pass params so model can be overridden via JSON config (see WriterAgentConfig).
        sent_data = _extract_sentiment(raw_for_sent, params=params)
        if sent_data.get("error"):
            # Surface missing package or load errors at result level
            return {"status": "ok", "result": sent_data}
        res = {
            "sentiment": sent_data.get("overall", {}),
            "meta": {"n_sections": sent_data.get("n_sections")}
        }
        if "per_section" in sent_data:
            res["per_section"] = sent_data["per_section"]
        return {"status": "ok", "result": res}

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

HELPER_NAMES = frozenset({"full", "readability", "entities", "key_phrases", "topics", "sentiment", "diagnostics", "check"})

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

    # For topics and sentiment we prefer structured sections (heading + body)
    # so the helper can report per-section results. This is the main "fancy" improvement.
    text: str | list[str]
    if name in ("topics", "sentiment"):
        text = _get_writer_sections(doc)
    else:
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
        labels = Counter(p.get("label") for p in per_sec if isinstance(p, dict))
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
