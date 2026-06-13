#!/usr/bin/env python3
# WriterAgent - document-sized embedding encode + query bench (venv worker).
"""Benchmark batch encode via the warm venv worker (Pickle5 IPC).

Historical note: pre-schema-v2 benches used SQLite BLOB + NumPy search in index.db.
Production embeddings (schema v3) use unified corpus.db + sqlite-vec + LangGraph — see docs/embeddings.md.

Run outside LibreOffice:
  .venv/bin/python scripts/bench_embeddings.py
  .venv/bin/python scripts/bench_embeddings.py --models all-MiniLM-L6-v2,BAAI/bge-small-en-v1.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.scripting.venv_worker import (  # noqa: E402
    PythonWorkerManager,
    resolve_libreoffice_python,
    resolve_venv_python,
    scrub_subprocess_env,
)

TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
DEFAULT_ODT = project_root / "scripts" / "longdocsample.odt"
PARAGRAPHS_PATH = "/tmp/writeragent_embed_paragraphs.json"
SIDECAR_PATH = "/tmp/writeragent_embed_sidecar.bin"
SESSION_PREFIX = "embed_bench"
DEFAULT_QUERY = "offline-first data collection systems KoboToolbox"
DEFAULT_MODEL = "all-MiniLM-L6-v2"


def find_config_path() -> Path | None:
    if os.name == "nt":
        paths = [Path(os.environ.get("APPDATA", "")) / "LibreOffice" / "4" / "user" / "writeragent.json"]
    elif sys.platform == "darwin":
        paths = [Path("~/Library/Application Support/LibreOffice/4/user/writeragent.json").expanduser()]
    else:
        paths = [
            Path("~/.config/libreoffice/4/user/config/writeragent.json").expanduser(),
            Path("~/.config/libreoffice/4/user/writeragent.json").expanduser(),
            Path("~/.config/libreoffice/24/user/config/writeragent.json").expanduser(),
            Path("~/.config/libreoffice/24/user/writeragent.json").expanduser(),
        ]
    for path in paths:
        if path.exists():
            return path
    return None


def resolve_python_exe(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    config_path = find_config_path()
    if config_path:
        try:
            venv_dir = json.loads(config_path.read_text(encoding="utf-8")).get("scripting.python_venv_path")
            if venv_dir:
                exe = resolve_venv_python(str(venv_dir).strip())
                if exe:
                    return exe
        except (OSError, json.JSONDecodeError):
            pass
    return resolve_libreoffice_python() or sys.executable


def extract_odt_paragraphs(odt_path: Path) -> list[str]:
    texts: list[str] = []
    with zipfile.ZipFile(odt_path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    for el in root.iter(f"{TEXT_NS}p"):
        text = "".join(el.itertext()).strip()
        if text:
            texts.append(text)
    return texts


def write_paragraphs_json(odt_path: Path, texts: list[str]) -> None:
    payload = {
        "doc_path": str(odt_path.resolve()),
        "paragraph_count": len(texts),
        "texts": texts,
    }
    with open(PARAGRAPHS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def encode_corpus_code(model_name: str) -> str:
    return f'''
import time
from plugin.embeddings.venv.embeddings_ingest_graph import ingest_paragraphs

texts = data

rows = [
    {{
        "text": text,
        "doc_url": "file:///tmp/bench.odt",
        "para_index": idx,
        "content_hash": f"hash_{{idx}}",
        "file_mtime": 12345.67,
    }}
    for idx, text in enumerate(texts)
]

t0 = time.perf_counter()
res = ingest_paragraphs(
    db_path="/tmp/writeragent_embed_bench/corpus.db",
    meta_path="/tmp/writeragent_embed_bench/corpus_meta.json",
    model_name={model_name!r},
    rows=rows,
)
encode_corpus_sec = time.perf_counter() - t0

from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, corpus_chunk_count
conn = connect_corpus_db("/tmp/writeragent_embed_bench/corpus.db")
n = corpus_chunk_count(conn)
try:
    row = conn.execute("SELECT embedding FROM vec_chunks LIMIT 1").fetchone()
    dim = len(row[0]) if row else 0
except Exception:
    dim = 0
conn.close()

result = {{
    "model": {model_name!r},
    "n": int(n),
    "dim": int(dim),
    "encode_corpus_sec": encode_corpus_sec,
}}
'''


def search_corpus_code(query: str, k: int, search_iters: int) -> str:
    return f'''
import time
from plugin.embeddings.venv.embeddings_search_graph import search_embeddings_graph

search_times = []
hits = []

for _ in range({search_iters}):
    t0 = time.perf_counter()
    res = search_embeddings_graph(
        db_path="/tmp/writeragent_embed_bench/corpus.db",
        query_text={query!r},
        k={k},
        model_name=model_name,
    )
    search_times.append(time.perf_counter() - t0)
    hits = res.get("hits") or []

def _median(xs):
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

result = {{
    "query_total_median_ms": _median(search_times) * 1000.0,
    "top_k": [{{"para_index": h["para_index"], "score": h["score"]}} for h in hits],
}}
'''


def _require_ok(response: dict, phase: str) -> dict:
    if response.get("status") != "ok":
        message = response.get("message", "unknown error")
        raise RuntimeError(f"{phase} failed: {message}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{phase} returned unexpected result: {result!r}")
    return result


def bench_model(
    mgr: PythonWorkerManager,
    *,
    texts: list[str],
    model_name: str,
    query: str,
    k: int,
    search_iters: int,
    timeout_sec: int,
) -> dict:
    session_id = f"{SESSION_PREFIX}:{model_name.replace('/', '_')}"
    print(f"Model {model_name!r}: encoding corpus (first run may download weights)...", flush=True)
    encode = _require_ok(
        mgr.execute(
            encode_corpus_code(model_name),
            data=texts,
            timeout_sec=timeout_sec,
            session_id=session_id,
        ),
        "encode",
    )
    # Set model_name in worker namespace for search bench
    mgr.execute(f"model_name = {model_name!r}", timeout_sec=timeout_sec, session_id=session_id)

    print(f"Model {model_name!r}: search bench ({search_iters} iterations)...", flush=True)
    search = _require_ok(
        mgr.execute(
            search_corpus_code(query, k, search_iters),
            timeout_sec=timeout_sec,
            session_id=session_id,
        ),
        "search",
    )
    return {**encode, **search}


def print_row(model: str, stats: dict) -> None:
    print(f"Model: {model}")
    print(f"  paragraphs: {stats['n']}  dim: {stats['dim']}")
    print(f"  ingest corpus: {stats['encode_corpus_sec']:.3f}s")
    print(
        f"  query total median: {stats['query_total_median_ms']:.3f} ms"
    )
    print("  top hits:")
    for hit in stats.get("top_k", []):
        print(f"    para {hit['para_index']:4d}  score {hit['score']:.4f}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bench embedding encode + vector search via venv worker")
    parser.add_argument("--odt", type=Path, default=DEFAULT_ODT, help="Sample Writer document")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Single HuggingFace model id")
    parser.add_argument("--models", help="Comma-separated model ids (overrides --model)")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Semantic search query")
    parser.add_argument("--k", type=int, default=5, help="Top-k hits")
    parser.add_argument("--search-iters", type=int, default=50, help="Query iterations for median timing")
    parser.add_argument("--timeout", type=int, default=600, help="Worker timeout per execute (seconds)")
    parser.add_argument("--python", help="Python executable (default: writeragent.json venv or sys.executable)")
    args = parser.parse_args()

    if not args.odt.is_file():
        print(f"ODT not found: {args.odt}", file=sys.stderr)
        return 1

    exe = resolve_python_exe(args.python)
    if not exe:
        print("Could not resolve a Python executable.", file=sys.stderr)
        return 1

    models = [m.strip() for m in (args.models or args.model).split(",") if m.strip()]
    texts = extract_odt_paragraphs(args.odt)
    if not texts:
        print(f"No paragraphs extracted from {args.odt}", file=sys.stderr)
        return 1

    write_paragraphs_json(args.odt, texts)
    print("--- WriterAgent embedding bench (venv worker) ---")
    print(f"Python: {exe}")
    print(f"Document: {args.odt}  paragraphs: {len(texts)}")
    print(f"Paragraphs JSON: {PARAGRAPHS_PATH}")
    print(f"Sidecar: {SIDECAR_PATH}")
    print(f"Query: {args.query!r}")
    print()

    import shutil
    shutil.rmtree("/tmp/writeragent_embed_bench", ignore_errors=True)
    Path("/tmp/writeragent_embed_bench").mkdir(parents=True, exist_ok=True)

    mgr = PythonWorkerManager.get(exe, scrub_subprocess_env(dict(os.environ)))
    try:
        for model in models:
            stats = bench_model(
                mgr,
                texts=texts,
                model_name=model,
                query=args.query,
                k=args.k,
                search_iters=args.search_iters,
                timeout_sec=args.timeout,
            )
            print_row(model, stats)
    finally:
        PythonWorkerManager.shutdown_all()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
