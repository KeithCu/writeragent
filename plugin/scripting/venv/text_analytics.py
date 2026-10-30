# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv text analytics compute — runs in user venv worker."""

from __future__ import annotations

from collections import Counter
import importlib.metadata
import importlib.util
import re
from typing import Any, cast

_LANG_MODELS: dict[str, tuple[str, ...]] = {
    "en": ("en_core_web_sm", "en_core_web_md", "en_core_web_lg"),
    "de": ("de_core_news_sm", "de_core_news_md", "de_core_news_lg"),
    "fr": ("fr_core_news_sm", "fr_core_news_md", "fr_core_news_lg"),
    "es": ("es_core_news_sm", "es_core_news_md", "es_core_news_lg"),
    "it": ("it_core_news_sm", "it_core_news_md", "it_core_news_lg"),
    "pt": ("pt_core_news_sm", "pt_core_news_md", "pt_core_news_lg"),
    "ru": ("ru_core_news_sm", "ru_core_news_md", "ru_core_news_lg"),
    "zh": ("zh_core_web_sm", "zh_core_web_md", "zh_core_web_lg"),
    "ja": ("ja_core_news_sm", "ja_core_news_md", "ja_core_news_lg"),
    "nl": ("nl_core_news_sm", "nl_core_news_md", "nl_core_news_lg"),
    "ko": ("ko_core_news_sm", "ko_core_news_md", "ko_core_news_lg"),
    "pl": ("pl_core_news_sm", "pl_core_news_md", "pl_core_news_lg"),
    "ca": ("ca_core_news_sm", "ca_core_news_md", "ca_core_news_lg"),
    "da": ("da_core_news_sm", "da_core_news_md", "da_core_news_lg"),
    "el": ("el_core_news_sm", "el_core_news_md", "el_core_news_lg"),
    "fi": ("fi_core_news_sm", "fi_core_news_md", "fi_core_news_lg"),
    "hr": ("hr_core_news_sm", "hr_core_news_md", "hr_core_news_lg"),
    "lt": ("lt_core_news_sm", "lt_core_news_md", "lt_core_news_lg"),
    "mk": ("mk_core_news_sm",),
    "nb": ("nb_core_news_sm", "nb_core_news_md", "nb_core_news_lg"),
    "ro": ("ro_core_news_sm", "ro_core_news_md", "ro_core_news_lg"),
    "sl": ("sl_core_news_sm", "sl_core_news_md", "sl_core_news_lg"),
    "sv": ("sv_core_news_sm", "sv_core_news_md", "sv_core_news_lg"),
    "uk": ("uk_core_news_sm", "uk_core_news_md", "uk_core_news_lg"),
}

_READABILITY_KEYS = frozenset({
    "flesch_reading_ease", "flesch_kincaid_grade", "smog",
    "gunning_fog", "automated_readability_index", "coleman_liau_index",
    "lix", "rix",
})

_DESCRIPTIVE_KEYS = frozenset({
    "proportion_unique_tokens",
    "token_length_mean", "token_length_median", "token_length_std",
    "sentence_length_mean", "sentence_length_median", "sentence_length_std",
    "syllables_per_token_mean", "syllables_per_token_median", "syllables_per_token_std",
})

_INSTALL_PACKAGES = ("spacy", "textdescriptives", "transformers", "torch", "scikit-learn")
_MAX_ANALYZED_CHARS = 1_000_000  # spaCy default nlp.max_length


def _text_analytics_install_command(model: str) -> str:
    return "uv pip install " + " ".join(_INSTALL_PACKAGES) + f" && python -m spacy download {model}"


def _lang_prefix(lang: Any) -> str | None:
    if not isinstance(lang, str):
        return None
    prefix = lang.lower().strip()[:2]
    return prefix or None


_SENTIMENT_PIPELINE: dict[str, Any] = {}


def _sentiment_pipeline(model: str) -> Any:
    """Load or retrieve the cached sentiment pipeline for *model*.

    The worker is warm and handles one text call at a time; store only
    the last successful pipeline; do not store failures.
    """
    # What was wrong: pipeline("sentiment-analysis", model=model) was called on every invocation, reloading heavy models.
    # How it happened: there was no cache holding the pipeline across calls within the warm worker.
    # Why this change: cache the last successfully loaded pipeline for that exact model string.
    if model in _SENTIMENT_PIPELINE:
        return _SENTIMENT_PIPELINE[model]
    from transformers import pipeline

    pipe = pipeline("sentiment-analysis", model=model)
    _SENTIMENT_PIPELINE.clear()
    _SENTIMENT_PIPELINE[model] = pipe
    return pipe


def _model_candidates(lang: Any) -> list[str]:
    # What was wrong: xx_sent_ud_sm was appended inside the lang block and again
    # in the multilingual list, so a known language tried it twice.
    # How it happened: both the per-language candidate builder and the general
    # fallback list independently appended xx_sent_ud_sm.
    # Why this change: xx_sent_ud_sm is tried once; xx_ent_wiki_sm stays last
    # because it is NER-only.
    code = _lang_prefix(lang)
    return [*_LANG_MODELS.get(code, ()), "xx_sent_ud_sm", "xx_ent_wiki_sm"]


def _load_nlp(lang: str | None = None) -> Any:
    """Load the best available spaCy pipeline for the requested language.

    Strategy for high quality + multilingual support:
    - Prefer a language-specific model when we know the language (better accuracy).
    - Fall back to strong multilingual models that include senter/tagger/parser
      (required for good readability and linguistic metrics via textdescriptives).
    - `xx_sent_ud_sm` is an excellent multilingual base for 100+ languages.
    """
    import spacy

    candidates = _model_candidates(lang)

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

def _truncate_for_nlp(text: str) -> tuple[str, dict[str, Any] | None]:
    """Cap text at spaCy's max_length; return (text, truncated note or None)."""
    # What was wrong: long text raised ValueError inside nlp(text).
    # How it happened: there was no input cap; spaCy's default nlp.max_length is 1,000,000.
    # Why this change: cap input to bound work and report what was analyzed in the truncated note.
    if len(text) <= _MAX_ANALYZED_CHARS:
        return text, None
    return text[:_MAX_ANALYZED_CHARS], {
        "analyzed_chars": _MAX_ANALYZED_CHARS,
        "total_chars": len(text),
    }


def _run_nlp_doc(
    text: str, lang: str | None, context: dict[str, Any] | None
) -> tuple[Any, Any, str | None, dict[str, Any] | None]:
    """Load the model and run the spaCy pipeline once. Returns (nlp, doc, resolved_lang, truncated).

    Factored out so narrow helpers (entities, key_phrases, readability) each call
    nlp(text) exactly once without running the full analyze_text stack.
    """
    if lang is None:
        lang = _get_lang_from_context(context)
    nlp = _load_nlp(lang)
    text, truncated = _truncate_for_nlp(text)
    doc = nlp(text)
    return nlp, doc, lang, truncated


def _extract_meta(nlp: Any, doc: Any, lang: str | None) -> dict[str, Any]:
    # What was wrong: doc._.lang is not standard spaCy and doc.lang_ was ignored, reporting "unknown".
    # How it happened: doc._ was checked for an unregistered attribute instead of standard doc.lang_.
    # Why this change: inspect doc.lang_ first before falling back to lang or "unknown".
    return {
        "model": nlp.meta.get("name") or getattr(nlp, "path", None) or "unknown",
        "lang": getattr(doc, "lang_", None) or (lang or "unknown"),
        "has_textdescriptives": any(p.startswith("textdescriptives/") for p in nlp.pipe_names),
    }


def _sentence_count(doc: Any) -> int:
    # What was wrong: Doc.sents raises ValueError E030 when SENT_START is unset.
    # How it happened: xx_ent_wiki_sm leaves SENT_START unset, and the readability
    # fallback called list(doc.sents) inside the except, so the fallback crashed too.
    # Why this change: inspect doc.has_annotation("SENT_START") first, returning 0
    # when sentence annotations are absent without catching broad exceptions.
    if callable(getattr(doc, "has_annotation", None)) and doc.has_annotation("SENT_START"):
        return len(list(doc.sents))
    return 0


def _extract_readability(doc: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (readability_dict, descriptive_stats_dict) from textdescriptives (or basic fallback).

    textdescriptives >=2 returns a flat dict (after the components have run on the doc).
    We split it into the groups the rest of the code expects.
    """
    # What was wrong: the full-analysis copy of these sets had already dropped the n_* keys
    # and named the fallback n_sents, while readability used n_sentences.
    # How it happened: duplicate copies of key sets diverged across helpers.
    # Why this change: share module-level _READABILITY_KEYS and _DESCRIPTIVE_KEYS, and use
    # n_sentences with _sentence_count.
    try:
        import textdescriptives as td

        m = cast("Any", td.extract_dict(doc))
        if isinstance(m, list):
            m = m[0]
        rd = {k: v for k, v in m.items() if k in _READABILITY_KEYS}
        ds = {k: v for k, v in m.items() if k in _DESCRIPTIVE_KEYS or k.startswith("n_")}
        # What was wrong: _extract_readability returned extra groups that were always empty in textdescriptives >= 2.0.
        # How it happened: textdescriptives flattens metrics to top-level keys rather than nested group dicts.
        # Why this change: return only (readability_dict, descriptive_stats_dict) without unused empty extra groups.
        return rd, ds
    except Exception:
        # textdescriptives not available or failed to extract — fall back to basic spaCy stats.
        return {}, {"n_tokens": len(doc), "n_sentences": _sentence_count(doc)}


def _extract_entities(doc: Any) -> list[dict[str, Any]]:
    return [
        {"text": ent.text, "label": ent.label_, "start_char": ent.start_char, "end_char": ent.end_char}
        for ent in doc.ents
    ]


def _extract_key_phrases(doc: Any) -> list[dict[str, Any]]:
    # What was wrong: Doc.noun_chunks raises ValueError E029 when the English iterator
    # sees no DEP, and NotImplementedError E894 when noun_chunks_iterator is None (xx models).
    # How it happened: xx_ent_wiki_sm is NER-only and lacks a parser/DEP annotation.
    # Why this change: guard with has_annotation("DEP") and handle NotImplementedError
    # or ValueError on noun_chunks iteration without catching Exception.
    if not callable(getattr(doc, "has_annotation", None)) or not doc.has_annotation("DEP"):
        return []
    try:
        chunks = list(doc.noun_chunks)
    except (NotImplementedError, ValueError):
        return []

    seen: set[str] = set()
    key_phrases = []
    for chunk in chunks:
        lemma = " ".join([t.lemma_ if t.lemma_ != "-PRON-" else t.text.lower() for t in chunk])
        key = lemma.lower().strip()
        if key and key not in seen and len(key) > 1:
            seen.add(key)
            key_phrases.append({"text": chunk.text, "lemma": lemma, "root": chunk.root.text if chunk.root else None})
    return key_phrases[:25]


def _as_sections(text: str | list[str]) -> tuple[list[str], bool]:
    """Normalize a string or list of section texts to (non-empty sections, is_multi)."""
    if isinstance(text, (list, tuple)):
        return [str(t).strip() for t in text if str(t).strip()], True
    return ([str(text).strip()] if str(text or "").strip() else []), False


def _extract_topics(text: str | list[str], n_topics: Any = 4) -> dict[str, Any]:
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

    docs, is_multi_section = _as_sections(text)

    if not docs:
        return {"topics": [], "note": "no text"}

    # What was wrong: n_topics was capped by len(docs) before the 8-word filter,
    # and NMF init="nndsvd" raises when n_components > min(n_samples, n_features),
    # turning into topic_model_failed instead of fitting fewer topics.
    # The assignment index was enumerate(W), so a dropped short section renumbered
    # every later section.
    # How it happened: the cap used len(docs) rather than len(filtered), did not
    # cap n_components against feature count, and ignored input section indexes.
    # Why this change: filter while tracking input indexes, cap n_topics by len(filtered),
    # cap n_components by min(n_topics, X.shape[1]) (n_topics is <= row count), and use the input index.
    filtered = [(idx, d) for idx, d in enumerate(docs) if len(d.split()) >= 8]
    if not filtered:
        filtered = [(0, docs[0])]

    try:
        # What was wrong: non-numeric n_topics (e.g. "auto" or a list) raised ValueError or TypeError outside the fit try, crashing the analysis.
        # How it happened: int(n_topics or 4) conversion happened before the try block that catches fit errors.
        # Why this change: moving int conversion inside the fit try routes conversion failures through the topic_model_failed envelope.
        n_topics = max(1, min(int(n_topics or 4), len(filtered), 8))

        # Multilingual-friendly: no hardcoded English stop_words (users can have mixed docs).
        # ngram up to 2 helps with short technical phrases.
        vectorizer = TfidfVectorizer(
            max_features=1200,
            ngram_range=(1, 2),
            min_df=1,
            strip_accents="unicode",
        )
        X = vectorizer.fit_transform([d for _, d in filtered])
        if X.shape[1] < 2:
            return {"topics": [], "note": "insufficient distinct terms"}

        n_components = min(n_topics, X.shape[1])
        nmf = NMF(n_components=n_components, random_state=42, init="nndsvd", max_iter=300, tol=1e-4)
        W = nmf.fit_transform(X)  # (n_docs, n_topics) weights
        H = nmf.components_       # (n_topics, n_terms)

        feature_names = vectorizer.get_feature_names_out()

        topics = []
        for i in range(n_components):
            top_idx = H[i].argsort()[-7:][::-1]
            terms = [str(feature_names[j]) for j in top_idx]
            total = float(W[:, i].sum() + 1e-9)
            topics.append({
                "id": i,
                "terms": terms,
                "weight": round(total / (W.sum() + 1e-9), 3),
            })

        result: dict[str, Any] = {"topics": topics}

        if is_multi_section:
            assignments = []
            for row_idx, row in enumerate(W):
                dom = int(row.argmax())
                strength = float(row[dom])
                assignments.append({
                    "section_index": filtered[row_idx][0],
                    "dominant_topic": dom,
                    "strength": round(strength, 3),
                })
            result["assignments"] = assignments
            result["n_sections"] = len(docs)

        return result
    except Exception as exc:
        # Don't crash the whole analysis; surface what happened.
        return {"topics": [], "error": f"topic_model_failed: {exc}"}


_STAR_RE = re.compile(r"^([1-5])\s*stars?$")
_LABEL_N_RE = re.compile(r"^label_(\d+)$")


def _polarity_from_class_index(index: int, num_labels: int) -> str:
    if num_labels < 2 or not (0 <= index < num_labels):
        return "neutral"
    midpoint = (num_labels - 1) / 2
    if index < midpoint:
        return "negative"
    if index > midpoint:
        return "positive"
    return "neutral"


def _normalize_sentiment_label(label: str, num_labels: int | None = None) -> str:
    cleaned = str(label or "").lower().strip()
    if cleaned in ("pos", "positive", "1", "5"):
        return "positive"
    if cleaned in ("neg", "negative", "0"):
        return "negative"

    star_match = _STAR_RE.match(cleaned)
    if star_match:
        star = int(star_match.group(1))
        return _polarity_from_class_index(star - 1, 5)

    label_match = _LABEL_N_RE.match(cleaned)
    if label_match and isinstance(num_labels, int):
        return _polarity_from_class_index(int(label_match.group(1)), num_labels)
    return "neutral"


def _pipeline_num_labels(clf: Any) -> int | None:
    model = getattr(clf, "model", None)
    config = getattr(model, "config", None)
    num_labels = getattr(config, "num_labels", None)
    if isinstance(num_labels, int) and num_labels > 1:
        return num_labels
    id2label = getattr(config, "id2label", None)
    if isinstance(id2label, dict) and len(id2label) > 1:
        return len(id2label)
    return None


def _extract_sentiment(text: str | list[str], params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sentiment analysis using transformers + a strong multilingual model (XLM-RoBERTa based).

    Default model provides good cross-lingual performance across many languages.
    This replaced the previous spacytextblob implementation (which had limited multilingual support).

    Returns overall score/label + per-section results when a list of sections is passed
    (reusing the same section extraction as topics for "by section" analysis on Writer docs).

    Config override via text_analytics_sentiment_model (JSON setting) for future flexibility.
    Only the transformers engine is implemented.
    """
    params = params or {}
    sections, is_multi = _as_sections(text)

    if not sections:
        return {"overall": {"score": 0.0, "label": "neutral"}, "note": "no text"}

    model = params.get("model") or "cardiffnlp/twitter-xlm-roberta-base-sentiment"

    try:
        import transformers
    except Exception as exc:
        return {
            "overall": {"score": 0.0, "label": "neutral"},
            "error": "MISSING_PACKAGE",
            "install_hint": "transformers (with CPU torch) for multilingual sentiment",
            "install": "uv pip install transformers torch --index-url https://download.pytorch.org/whl/cpu",
            "detail": f"Could not import transformers: {str(exc)}",
        }

    try:
        clf = _sentiment_pipeline(model)
    except Exception as exc:
        return {
            "overall": {"score": 0.0, "label": "neutral"},
            "error": "SENTIMENT_MODEL_LOAD_FAILED",
            "install_hint": f"Failed to load model '{model}'. This may be a download, cache, network, compatibility, or missing tokenizer library issue (e.g. sentencepiece). Not just the base package.",
            "install": "uv pip install transformers torch sentencepiece  (and ensure network access for first model download)",
            "detail": str(exc),
        }

    num_labels = _pipeline_num_labels(clf)
    per_section: list[dict[str, Any]] = []
    scores: list[float] = []

    for i, sec in enumerate(sections):
        # Truncate conservatively for transformer limits; sections from headings are usually reasonable.
        text_for_clf = sec[:512]
        res = clf(text_for_clf)[0]
        # HF returns label like 'positive'/'negative'/'neutral' and score 0-1.
        raw_label = str(res.get("label", "neutral"))
        sc = float(res.get("score", 0.0))
        label = _normalize_sentiment_label(raw_label, num_labels)
        # What was wrong: LABEL_0/1/2 fell through to neutral for any override model
        # whose id2label is still the default, and a neutral section stored +confidence
        # in per_section while the average added 0.
        # How it happened: label matching only handled fixed strings and per_section["score"]
        # used sc directly for positive/neutral labels.
        # Why this change: normalize LABEL_n and star ratings with num_labels, and use
        # a single signed polarity score for both per_section and overall average.
        signed = sc if label == "positive" else (-sc if label == "negative" else 0.0)
        scores.append(signed)
        per_section.append({
            "section_index": i,
            "score": round(signed, 3),
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
        # What was wrong: single-string sentiment included section_index in the overall dictionary.
        # How it happened: overall was assigned per_section[0] directly, which contained section_index.
        # Why this change: copy only score and label to overall so section_index is present only in per_section.
        src = per_section[0] if per_section else {"score": 0.0, "label": "neutral"}
        overall = {"score": src["score"], "label": src["label"]}

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

    nlp, doc, lang, truncated = _run_nlp_doc(text, lang, context)

    result: dict[str, Any] = {}

    rd, ds = _extract_readability(doc)
    result["readability"] = rd
    result["descriptive_stats"] = ds

    result["entities"] = _extract_entities(doc)
    result["key_phrases"] = _extract_key_phrases(doc)

    # --- Linguistic profile (always useful) ---
    # What was wrong: models like xx_ent_wiki_sm leave token.pos_ empty, adding "" to pos_proportions.
    # How it happened: pos_ was added to counts without checking whether it was non-empty.
    # Why this change: skip empty pos tags.
    pos_counts = Counter(
        token.pos_ for token in doc
        if token.pos_ and not (token.is_punct or token.is_space)
    )

    total = sum(pos_counts.values()) or 1
    pos_props = {k: round(v / total, 4) for k, v in pos_counts.items()}

    result["linguistic_profile"] = {
        "pos_proportions": pos_props,
        "n_tokens": len(doc),
        "n_sents": _sentence_count(doc),
    }

    result["meta"] = _extract_meta(nlp, doc, lang)
    if truncated:
        result["truncated"] = truncated

    return {"status": "ok", "result": result}


def _probe_package(package: str) -> tuple[bool, str | None]:
    """Presence and version from package metadata; never imports the package."""
    try:
        return True, importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return False, None


def check_diagnostics(lang: Any = None) -> dict[str, Any]:
    """Perform self-diagnostics of the packages needed by Text Analytics features.

    This runs *inside the worker* using whatever python the extension resolved
    (based on Settings → Python → python_venv_path, or falling back to LO's python).

    Reports on:
      - spaCy + models (for entities, key phrases, basic stats, language)
      - textdescriptives (for real Readability scores)
      - transformers + torch (for the Sentiment feature using a good multilingual model)
      - scikit-learn (for topics)

    The output includes the python executable being used and the exact install command.
    """
    # Always capture the python the worker is actually using.
    import sys
    python_exe = getattr(sys, "executable", "unknown")

    # What was wrong: non-string lang raised AttributeError on .lower() before the try block, returning a raw worker crash instead of diagnostics.
    # How it happened: check_diagnostics assumed lang was always a string or None.
    # Why this change: _lang_prefix treats non-string lang as None.
    lang = _lang_prefix(lang)
    recommended_model = _LANG_MODELS[lang][0] if lang and lang in _LANG_MODELS else "xx_sent_ud_sm"

    # Full command needed for everything the dialog supports.
    text_analytics_install = _text_analytics_install_command(recommended_model)

    try:
        import spacy
        has_td = importlib.util.find_spec("textdescriptives") is not None
        models = list(spacy.util.get_installed_models())

        # Transformers + torch (for Sentiment). Probed independently: torch used
        # to sit inside the transformers import, so a missing transformers
        # reported torch missing too.
        has_transformers, transformers_version = _probe_package("transformers")
        has_torch, torch_version = _probe_package("torch")

        # scikit-learn (for topics). Metadata name, not the "sklearn" import name.
        has_sklearn, sklearn_version = _probe_package("scikit-learn")

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
            "has_sklearn": has_sklearn,
            "sklearn_version": sklearn_version,
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
        return {"status": "ok", "helper": helper, "result": check_diagnostics(lang=lang)}

    # Preserve original list form for section-aware helpers (topics, sentiment).
    # Only join for helpers that expect a single string.
    raw = text
    if isinstance(text, list):
        text = "\n\n".join(str(t) for t in text if t)

    if not text or not str(text).strip():
        return {"status": "ok", "result": {}, "note": "no text provided"}

    text_s = str(text)
    section_text = raw if isinstance(raw, list) else text_s
    lang = params.get("lang") or (context or {}).get("lang")

    # For the minimal dialog we mainly use "full" or specific helpers.
    if helper in ("full", "analyze", "all"):
        return analyze_text(text_s, lang=lang, context=context)

    # Narrow helpers: run nlp(text) once and extract only the requested subset,
    # skipping the unneeded NER / noun-chunk / textdescriptives work.
    def _nlp_ok(extract: Any) -> dict[str, Any]:
        nlp, doc, resolved_lang, truncated = _run_nlp_doc(text_s, lang, context)
        res = extract(doc)
        res["meta"] = _extract_meta(nlp, doc, resolved_lang)
        if truncated:
            res["truncated"] = truncated
        return {"status": "ok", "result": res}

    if helper in ("readability", "stats", "descriptive"):
        def _readability_body(doc: Any) -> dict[str, Any]:
            rd, ds = _extract_readability(doc)
            return {"readability": rd, "descriptive_stats": ds}
        return _nlp_ok(_readability_body)

    if helper in ("entities", "ner"):
        return _nlp_ok(lambda doc: {"entities": _extract_entities(doc)})

    if helper in ("key_phrases", "keyphrases", "chunks"):
        return _nlp_ok(lambda doc: {"key_phrases": _extract_key_phrases(doc)})

    if helper in ("topics", "topic", "topic_model"):
        # Use preserved original (may still be list) for per-section support.
        topic_data = _extract_topics(section_text, n_topics=params.get("n_topics", 4))
        # Normalize into the same result envelope the rest of the system expects.
        if topic_data.get("error") == "MISSING_PACKAGE":
            return {
                "status": "error",
                "code": "TEXT_ANALYTICS_ERROR",
                "helper": helper,
                "message": "install scikit-learn for topics",
                "result": topic_data,
            }
        # What was wrong: meta.n_topics was n_sections, the filtered row count,
        # so the dialog's meta line showed the section count.
        # How it happened: n_topics was computed as n_sections or len(topics),
        # and topic_data note/error were omitted from the result dict.
        # Why this change: build meta with n_topics as len(topics) and n_sections
        # when an int, and forward note/error from topic_data onto result.
        topics = topic_data.get("topics", [])
        meta: dict[str, Any] = {"n_topics": len(topics)}
        if isinstance(topic_data.get("n_sections"), int):
            meta["n_sections"] = topic_data["n_sections"]

        res_dict: dict[str, Any] = {
            "topics": topics,
            "assignments": topic_data.get("assignments"),
            "meta": meta,
        }
        if "error" in topic_data:
            res_dict["error"] = topic_data["error"]
        if "note" in topic_data:
            res_dict["note"] = topic_data["note"]

        return {"status": "ok", "result": res_dict}

    if helper in ("sentiment", "polarity"):
        # Pass params so model can be overridden via JSON config (see WriterAgentConfig).
        sent_data = _extract_sentiment(section_text, params=params)
        if sent_data.get("error"):
            # A missing model used to be status ok, so the runner reported success.
            return {
                "status": "error",
                "code": "TEXT_ANALYTICS_ERROR",
                "helper": helper,
                "message": str(sent_data.get("error")),
                "result": sent_data,
            }
        res = {
            "sentiment": sent_data.get("overall", {}),
            "meta": {"n_sections": sent_data.get("n_sections")}
        }
        if "per_section" in sent_data:
            res["per_section"] = sent_data["per_section"]
        return {"status": "ok", "result": res}

    return {
        "status": "error",
        "code": "UNKNOWN_HELPER",
        "helper": str(helper),
        "message": f"Unknown text analytics helper {helper!r}",
    }
