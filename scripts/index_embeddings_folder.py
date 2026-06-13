#!/usr/bin/env python3
# WriterAgent - offline folder embeddings index maintenance.
"""Index or refresh writeragent_embeddings for a document folder (no LibreOffice).

Example:
  .venv/bin/python scripts/index_embeddings_folder.py ~/Desktop/Writing
  .venv/bin/python scripts/index_embeddings_folder.py ~/Desktop/Writing --mode cold
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.embeddings.venv.embeddings_folder_maintain import maintain_folder_index


def main() -> int:
    parser = argparse.ArgumentParser(description="Maintain per-folder WriterAgent embeddings cache (.odt, .ods, .odp, .odg siblings)")
    parser.add_argument("folder", type=Path, help="Document directory containing LibreOffice siblings")
    parser.add_argument("--model", default="paraphrase-multilingual-MiniLM-L12-v2", help="SentenceTransformer model id")
    parser.add_argument(
        "--mode",
        choices=("auto", "cold", "incremental"),
        default="auto",
        help="Index mode (default: auto)",
    )
    parser.add_argument(
        "--search-mode",
        choices=("embeddings", "hybrid", "fts"),
        default="hybrid",
        help="Search mode (default: hybrid)",
    )
    args = parser.parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 1

    def _heartbeat(payload: dict) -> None:
        phase = payload.get("phase", "")
        print(f"[heartbeat] {phase}: {payload}", flush=True)

    try:
        result = maintain_folder_index(
            str(folder),
            embedding_model=args.model,
            mode=args.mode,
            heartbeat_fn=_heartbeat,
            search_mode=args.search_mode,
        )
    except Exception as exc:
        print(f"Index failed: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
