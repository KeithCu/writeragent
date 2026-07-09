# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv text analytics compute — runs in user venv worker."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, cast

# Worker-local helper names (mirror host facade; do not import plugin.scripting.* here).
HELPER_NAMES = frozenset({"full", "readability", "entities", "key_phrases", "topics", "sentiment", "diagnostics", "check"})

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
