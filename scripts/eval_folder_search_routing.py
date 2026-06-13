#!/usr/bin/env python3
# WriterAgent - offline folder search routing eval (hybrid / FTS / vec legs).
"""Measure top-1 file routing on a labeled query set (see docs/embeddings.md § Performance).

Example:
  .venv/bin/python scripts/eval_folder_search_routing.py --folder ~/Desktop/Writing
  .venv/bin/python scripts/eval_folder_search_routing.py --mode hybrid --no-mmr
  .venv/bin/python scripts/eval_folder_search_routing.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.embeddings.embeddings_cache import corpus_db_path, corpus_meta_path, index_is_empty, read_corpus_meta  # noqa: E402
from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL, hybrid_search, knn_search  # noqa: E402
from plugin.embeddings.venv.folder_fts import search_folder_fts  # noqa: E402

DEFAULT_FOLDER = Path("~/Desktop/Writing")
DEFAULT_K = 10
DEFAULT_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_NEAR_SLOP = 10

SearchMode = Literal["hybrid", "fts", "vec", "all"]
QuerySetName = Literal["short", "long"]


@dataclass(frozen=True)
class LabeledQuery:
    query: str
    expected: str | None
    set_name: QuerySetName
    note: str = ""


# Seeded from docs/embeddings.md § Performance (2026-06 corpus on ~/Desktop/Writing).
SHORT_QUERIES: tuple[LabeledQuery, ...] = (
    LabeledQuery("web search", "part2.odt", "short"),
    LabeledQuery("tool loop", "part3.odt", "short"),
    LabeledQuery("numpy", "part5.odt", "short"),
    LabeledQuery("grammar checker", "part4.odt", "short"),
    LabeledQuery("venv worker process", "part5.odt", "short"),
    LabeledQuery("document research", "part3.odt", "short"),
    LabeledQuery("type checking", "part3.odt", "short"),
    LabeledQuery("math import", "part5.odt", "short"),
    LabeledQuery("async grammar checking", "part5.odt", "short"),
    LabeledQuery("software wars", "SoftwareWars.odt", "short"),
    LabeledQuery("conduit geometry", "ConduitGeometry.odt", "short"),
    LabeledQuery("formal verification", "FormalVerificationText.odt", "short"),
    LabeledQuery("MCP", "part3.odt", "short", note="both OK in historical grep vs embed bench"),
    LabeledQuery("microphone", "part2.odt", "short"),
    LabeledQuery("OpenRouter", "part2.odt", "short", note="both OK in historical bench"),
    LabeledQuery("LibreOffice proofreading API", "Translation Test.odt", "short"),
    LabeledQuery("cross document search", "part3.odt", "short"),
    LabeledQuery("grammar", "part4.odt", "short"),
    LabeledQuery("venv worker", "part5.odt", "short"),
    LabeledQuery("WriterAgent", "part5.odt", "short"),
    LabeledQuery("python calc formula", "part5.odt", "short"),
    LabeledQuery("streaming", None, "short", note="ambiguous draft vs partN"),
    LabeledQuery("semantic search", None, "short", note="ambiguous draft vs partN"),
    LabeledQuery("web research subagent", None, "short", note="ambiguous draft vs partN"),
    LabeledQuery("streaming sidebar tokens", None, "short", note="ambiguous draft vs partN"),
    LabeledQuery("agent delegation", "part3.odt", "short"),
    LabeledQuery("asynchronous execution", "part5.odt", "short"),
    LabeledQuery("linguistic extension", "Translation Test.odt", "short"),
    LabeledQuery("multilingual spellcheck", "writerchat.odt", "short"),
    LabeledQuery("curved shapes", "ConduitGeometry.odt", "short"),
    LabeledQuery("logical proofs", "FormalVerificationText.odt", "short"),
    LabeledQuery("browser competition", "SoftwareWars.odt", "short"),
    LabeledQuery("equation formatting", "part4.odt", "short"),
)

LONG_QUERIES: tuple[LabeledQuery, ...] = (
    LabeledQuery(
        "proofreader called back by libreoffice linguistic subsystem",
        "Translation Test.odt",
        "long",
    ),
    LabeledQuery("real time multilingual spell and grammar engine", "writerchat.odt", "long"),
    LabeledQuery(
        "natural language questions better than google search box",
        "part2.odt",
        "long",
    ),
    LabeledQuery("type checking makes python look like c plus plus", "part3.odt", "long"),
    LabeledQuery("frustrated with grammar checker switched to math", "part4.odt", "long"),
    LabeledQuery("two level toolset like web research subagent", "part3.odt", "long"),
    LabeledQuery("week six async grammar and latex math", "part5.odt", "long"),
    LabeledQuery("grammar checker that blocks the ui thread", "part4.odt", "long"),
    LabeledQuery(
        "reasoning tokens shown before the final answer",
        "blog_draft_cursor_for_libreoffice.odt",
        "long",
    ),
    LabeledQuery(
        "small specialized agent returns distilled answer not bloating context",
        "part2.odt",
        "long",
    ),
    LabeledQuery("cross platform microphone audio input challenges", "part2.odt", "long"),
    LabeledQuery("geometry of curved conduit surfaces", "ConduitGeometry.odt", "long"),
)

ALL_LABELED_QUERIES: tuple[LabeledQuery, ...] = SHORT_QUERIES + LONG_QUERIES


class EvalRoutingError(Exception):
    """Preflight or runtime failure for routing eval."""


def doc_basename(doc_url: str) -> str:
    raw = unquote(str(doc_url or "").replace("file://", ""))
    return Path(raw).name


def matches_expected(doc_url: str, expected: str) -> bool:
    base = doc_basename(doc_url).lower()
    token = expected.lower()
    return token in base


def top_hit_doc_url(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    return str(hits[0].get("doc_url") or "")


def _resolve_model(meta_path: Path, model_override: str | None) -> str:
    if model_override and model_override.strip():
        return model_override.strip()
    meta = read_corpus_meta(meta_path)
    model = (meta.get("embedding_model") or "").strip()
    return model or DEFAULT_MODEL


def _preflight(folder: Path) -> tuple[Path, Path, Path, str]:
    listing_root = folder.expanduser().resolve()
    if not listing_root.is_dir():
        raise EvalRoutingError(f"Not a directory: {listing_root}")
    db_path = corpus_db_path(str(listing_root), create_parent=False)
    meta_path = corpus_meta_path(str(listing_root), create_parent=False)
    if not db_path.is_file() or index_is_empty(meta_path, db_path):
        raise EvalRoutingError(
            f"No indexed cache under {listing_root / 'writeragent_embeddings'}. "
            f"Build with: .venv/bin/python scripts/index_embeddings_folder.py {listing_root}"
        )
    return listing_root, db_path, meta_path, _resolve_model(meta_path, None)


def run_search_leg(
    leg: Literal["hybrid", "fts", "vec"],
    *,
    db_path: Path,
    model_name: str,
    query: str,
    k: int,
    near_slop: int,
    use_mmr: bool,
    backend: str = "hybrid",
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    if leg == "hybrid":
        if backend == "llama_index":
            # Use LlamaIndex hybrid search instead of default embeddings hybrid
            from plugin.embeddings.venv.embeddings_llama_index import llama_index_hybrid_search
            return llama_index_hybrid_search(
                str(db_path),
                query,
                k,
                model_name=model_name,
                near_slop=near_slop,
                doc_url_filter=doc_url_filter,
                use_mmr=use_mmr,
            )
        else:
            return hybrid_search(
                str(db_path),
                query,
                k,
                model_name=model_name,
                near_slop=near_slop,
                doc_url_filter=doc_url_filter,
                use_mmr=use_mmr,
            )
    if leg == "vec":
        return knn_search(str(db_path), query, k, model_name=model_name)
    return search_folder_fts(str(db_path), query, k=k, near_slop=near_slop)


def evaluate_query(
    labeled: LabeledQuery,
    *,
    db_path: Path,
    model_name: str,
    k: int,
    near_slop: int,
    use_mmr: bool,
    legs: tuple[Literal["hybrid", "fts", "vec"], ...],
    backend: str = "hybrid",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "query": labeled.query,
        "set": labeled.set_name,
        "expected": labeled.expected,
        "note": labeled.note,
        "legs": {},
    }
    for leg in legs:
        try:
            result = run_search_leg(
                leg,
                db_path=db_path,
                model_name=model_name,
                query=labeled.query,
                k=k,
                near_slop=near_slop,
                use_mmr=use_mmr,
                backend=backend,
            )
        except ImportError as exc:
            raise EvalRoutingError(
                f"Embeddings packages not available ({exc}). Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
            ) from exc
        hits = result.get("hits") or []
        top_url = top_hit_doc_url(hits)
        top = hits[0] if hits else {}
        leg_info: dict[str, Any] = {
            "top_doc_url": top_url,
            "top_basename": doc_basename(top_url) if top_url else "",
            "score": float(top.get("score") or 0.0),
            "matched_by": list(top.get("matched_by") or []) if leg == "hybrid" else [],
            "correct": False,
            "correct_top_3": False,
            "mrr": 0.0,
        }
        if labeled.expected:
            rank_found = 0
            for idx, hit in enumerate(hits):
                url = hit.get("doc_url") or ""
                if matches_expected(url, labeled.expected):
                    rank_found = idx + 1
                    break
            leg_info["correct"] = (rank_found == 1)
            leg_info["correct_top_3"] = (0 < rank_found <= 3)
            leg_info["mrr"] = (1.0 / rank_found) if rank_found > 0 else 0.0

        row["legs"][leg] = leg_info
    return row


def classify_fts_vec_bucket(row: dict[str, Any]) -> str | None:
    if not row.get("expected"):
        return None
    fts = row.get("legs", {}).get("fts", {})
    vec = row.get("legs", {}).get("vec", {})
    fts_ok = bool(fts.get("correct"))
    vec_ok = bool(vec.get("correct"))
    if fts_ok and vec_ok:
        return "both"
    if fts_ok and not vec_ok:
        return "fts_only"
    if vec_ok and not fts_ok:
        return "vec_only"
    return "neither"


def summarize_rows(rows: list[dict[str, Any]], *, leg: str) -> dict[str, Any]:
    labeled = [r for r in rows if r.get("expected")]
    correct = sum(1 for r in labeled if r.get("legs", {}).get(leg, {}).get("correct"))
    correct_top_3 = sum(1 for r in labeled if r.get("legs", {}).get(leg, {}).get("correct_top_3"))
    total_mrr = sum(r.get("legs", {}).get(leg, {}).get("mrr", 0.0) for r in labeled)
    total = len(labeled)
    
    pct = (100.0 * correct / total) if total else 0.0
    pct_top_3 = (100.0 * correct_top_3 / total) if total else 0.0
    mean_mrr = (total_mrr / total) if total else 0.0
    
    by_set: dict[str, dict[str, Any]] = {}
    for r in labeled:
        set_name = str(r.get("set") or "unknown")
        bucket = by_set.setdefault(
            set_name, 
            {"labeled": 0, "correct": 0, "correct_top_3": 0, "mrr_sum": 0.0}
        )
        bucket["labeled"] += 1
        leg_data = r.get("legs", {}).get(leg, {})
        if leg_data.get("correct"):
            bucket["correct"] += 1
        if leg_data.get("correct_top_3"):
            bucket["correct_top_3"] += 1
        bucket["mrr_sum"] += leg_data.get("mrr", 0.0)
        
    return {
        "leg": leg, 
        "labeled": total, 
        "correct": correct, 
        "accuracy_pct": round(pct, 1), 
        "correct_top_3": correct_top_3,
        "accuracy_top_3_pct": round(pct_top_3, 1),
        "mean_mrr": round(mean_mrr, 3),
        "by_set": by_set
    }


def summarize_fts_vec_buckets(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"both": 0, "fts_only": 0, "vec_only": 0, "neither": 0}
    for row in rows:
        bucket = classify_fts_vec_bucket(row)
        if bucket is None:
            continue
        counts[bucket] += 1
    return counts


def run_eval(
    folder: Path,
    *,
    mode: SearchMode = "all",
    k: int = DEFAULT_K,
    near_slop: int = DEFAULT_NEAR_SLOP,
    model: str | None = None,
    use_mmr: bool = True,
    query_set: QuerySetName | None = None,
    backend: str = "hybrid",
) -> dict[str, Any]:
    listing_root, db_path, meta_path, default_model = _preflight(folder)
    model_name = model or default_model

    if mode == "all":
        legs: tuple[Literal["hybrid", "fts", "vec"], ...] = ("hybrid", "fts", "vec")
    elif mode == "hybrid":
        legs = ("hybrid",)
    elif mode == "fts":
        legs = ("fts",)
    else:
        legs = ("vec",)

    queries = ALL_LABELED_QUERIES
    if query_set == "short":
        queries = SHORT_QUERIES
    elif query_set == "long":
        queries = LONG_QUERIES

    rows = [
        evaluate_query(
            labeled,
            db_path=db_path,
            model_name=model_name,
            k=k,
            near_slop=near_slop,
            use_mmr=use_mmr,
            legs=legs,
            backend=backend,
        )
        for labeled in queries
    ]

    summary: dict[str, Any] = {
        "folder": str(listing_root),
        "model": model_name,
        "mode": mode,
        "use_mmr": use_mmr,
        "k": k,
        "query_count": len(rows),
    }
    for leg in legs:
        summary[f"{leg}_routing"] = summarize_rows(rows, leg=leg)
    if "fts" in legs and "vec" in legs:
        summary["fts_vec_buckets"] = summarize_fts_vec_buckets(rows)
    return {"status": "ok", "summary": summary, "rows": rows}


def format_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        f"Folder: {summary.get('folder', '?')}",
        f"Model:  {summary.get('model', '?')}",
        f"Mode:   {summary.get('mode', '?')}  MMR: {summary.get('use_mmr', '?')}",
        f"Queries: {summary.get('query_count', 0)}",
        "=" * 72,
    ]
    for key, value in summary.items():
        if not key.endswith("_routing"):
            continue
        leg = value.get("leg", key.replace("_routing", ""))
        lines.append(
            f"{leg}: {value.get('correct', 0)}/{value.get('labeled', 0)} top-1 correct ({value.get('accuracy_pct', 0)}%) | "
            f"top-3: {value.get('correct_top_3', 0)} ({value.get('accuracy_top_3_pct', 0)}%) | "
            f"MRR: {value.get('mean_mrr', 0.0)}"
        )
        for set_name, counts in sorted((value.get("by_set") or {}).items()):
            set_labeled = counts.get("labeled", 0)
            set_correct = counts.get("correct", 0)
            set_top_3 = counts.get("correct_top_3", 0)
            set_mrr_sum = counts.get("mrr_sum", 0.0)
            set_pct = (100.0 * set_correct / set_labeled) if set_labeled else 0.0
            set_pct_top_3 = (100.0 * set_top_3 / set_labeled) if set_labeled else 0.0
            set_mrr = (set_mrr_sum / set_labeled) if set_labeled else 0.0
            lines.append(
                f"  {set_name}: {set_correct}/{set_labeled} ({round(set_pct, 1)}%) | "
                f"top-3: {set_top_3} ({round(set_pct_top_3, 1)}%) | "
                f"MRR: {round(set_mrr, 3)}"
            )
    buckets = summary.get("fts_vec_buckets")
    if buckets:
        lines.append("FTS vs vec buckets (labeled queries):")
        for name in ("both", "fts_only", "vec_only", "neither"):
            lines.append(f"  {name}: {buckets.get(name, 0)}")
    lines.append("=" * 72)
    for row in payload.get("rows") or []:
        expected = row.get("expected") or "(none)"
        hybrid = row.get("legs", {}).get("hybrid") or {}
        fts = row.get("legs", {}).get("fts") or {}
        vec = row.get("legs", {}).get("vec") or {}
        parts = [f"q={row.get('query')!r}", f"exp={expected}"]
        if hybrid:
            parts.append(f"hybrid={hybrid.get('top_basename', '?')} (correct={hybrid.get('correct')}, rank_correct={round(1.0/hybrid.get('mrr'), 1) if hybrid.get('mrr') > 0 else 'N/A'})")
        if fts:
            parts.append(f"fts={fts.get('top_basename', '?')}")
        if vec:
            parts.append(f"vec={vec.get('top_basename', '?')} (rank={round(1.0/vec.get('mrr'), 1) if vec.get('mrr') > 0 else 'N/A'})")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eval top-1 file routing on labeled folder search queries")
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER, help=f"Document folder (default: {DEFAULT_FOLDER})")
    parser.add_argument(
        "--backend",
        choices=("hybrid", "llama_index"),
        default="hybrid",
        help="Backend to evaluate (default hybrid).",
    )
    parser.add_argument(
        "--mode",
        choices=("hybrid", "fts", "vec", "all"),
        default="all",
        help="Search leg(s) to evaluate (default: all)",
    )
    parser.add_argument("--set", dest="query_set", choices=("short", "long"), help="Restrict to short or long query set")
    parser.add_argument("--k", type=int, default=5, help="Top-k for search (default: 5)")
    parser.add_argument("--near-slop", type=int, default=DEFAULT_NEAR_SLOP, help="FTS NEAR slop (default: 10)")
    parser.add_argument("--model", help="Override embedding model id")
    parser.add_argument(
        "--no-mmr",
        action="store_true",
        help="Hybrid leg only: disable MMR after RRF (compare baseline)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON payload")
    args = parser.parse_args(argv)

    try:
        payload = run_eval(
            args.folder,
            mode=args.mode,
            k=max(1, int(args.k)),
            near_slop=args.near_slop,
            model=args.model,
            use_mmr=not args.no_mmr,
            query_set=args.query_set,
            backend=args.backend,
        )
    except EvalRoutingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
