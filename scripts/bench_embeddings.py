#!/usr/bin/env python3
# WriterAgent - document-sized embedding encode + query bench (venv worker).
"""Benchmark batch encode via the warm venv worker (Pickle5 IPC).

Historical note: pre-schema-v2 benches used SQLite BLOB + NumPy search in index.db.
Production embeddings (schema v2) use Chroma + LangGraph — see docs/embeddings.md.

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
import numpy as np
from sentence_transformers import SentenceTransformer

texts = data

t0 = time.perf_counter()
embedder = SentenceTransformer({model_name!r})
load_sec = time.perf_counter() - t0

t0 = time.perf_counter()
batch = embedder.encode(texts, convert_to_tensor=False, show_progress_bar=False)
encode_corpus_sec = time.perf_counter() - t0

corpus_matrix = np.stack(batch).astype(np.float32)
n, dim = corpus_matrix.shape

result = {{
    "model": {model_name!r},
    "n": int(n),
    "dim": int(dim),
    "load_sec": load_sec,
    "encode_corpus_sec": encode_corpus_sec,
    "sidecar_bytes": 8 + int(corpus_matrix.nbytes),
}}
'''


def search_corpus_code(query: str, k: int, search_iters: int) -> str:
    return f'''
import time
import numpy as np

n = corpus_matrix.shape[0]

encode_query_times = []
dot_topk_times = []
total_times = []
top_k = []

for _ in range({search_iters}):
    t0 = time.perf_counter()
    query_emb = embedder.encode({query!r}, convert_to_tensor=False, show_progress_bar=False)
    encode_query_times.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    similarities = np.clip(np.dot(corpus_matrix, query_emb), -1.0, 1.0)
    if {k} >= n:
        top_idx = np.argsort(similarities)[-{k}:][::-1]
    else:
        part = np.argpartition(similarities, -{k})[-{k}:]
        top_idx = part[np.argsort(similarities[part])][::-1]
    dot_topk_times.append(time.perf_counter() - t0)
    total_times.append(encode_query_times[-1] + dot_topk_times[-1])
    top_k = [{{"para_index": int(i), "score": float(similarities[i])}} for i in top_idx]

def _median(xs):
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

result = {{
    "encode_query_median_ms": _median(encode_query_times) * 1000.0,
    "dot_topk_median_ms": _median(dot_topk_times) * 1000.0,
    "query_total_median_ms": _median(total_times) * 1000.0,
    "top_k": top_k,
}}
'''


def write_sidecar_bin(path: str, matrix: object) -> None:
    """Write float32 sidecar on the host (venv sandbox forbids open() in worker code)."""
    import numpy as np

    arr = np.asarray(matrix, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError(f"expected 2D corpus matrix, got shape {arr.shape!r}")
    n, dim = arr.shape
    header = np.array([n, dim], dtype=np.uint32).tobytes()
    with open(path, "wb") as out:
        out.write(header)
        out.write(arr.tobytes())


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
    print(f"Model {model_name!r}: search bench ({search_iters} iterations)...", flush=True)
    search = _require_ok(
        mgr.execute(
            search_corpus_code(query, k, search_iters),
            timeout_sec=timeout_sec,
            session_id=session_id,
        ),
        "search",
    )
    matrix_resp = mgr.execute(
        "result = corpus_matrix",
        timeout_sec=timeout_sec,
        session_id=session_id,
    )
    if matrix_resp.get("status") == "ok" and matrix_resp.get("result") is not None:
        write_sidecar_bin(SIDECAR_PATH, matrix_resp["result"])
    return {**encode, **search}


def print_row(model: str, stats: dict) -> None:
    sidecar_mb = stats["sidecar_bytes"] / (1024 * 1024)
    print(f"Model: {model}")
    print(f"  paragraphs: {stats['n']}  dim: {stats['dim']}  sidecar: {sidecar_mb:.2f} MiB")
    print(f"  load: {stats['load_sec']:.3f}s  encode corpus: {stats['encode_corpus_sec']:.3f}s")
    print(
        f"  query encode: {stats['encode_query_median_ms']:.3f} ms  "
        f"dot+top-k: {stats['dot_topk_median_ms']:.3f} ms  "
        f"total: {stats['query_total_median_ms']:.3f} ms"
    )
    print("  top hits:")
    for hit in stats.get("top_k", [])[:5]:
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
