# Multilingual embeddings fixtures

Synthetic locale metadata for unit tests lives in `tests/embeddings/test_embeddings_locale.py`.

For manual routing eval on non-English corpora:

1. Cold rebuild after schema v5: `.venv/bin/python scripts/index_embeddings_folder.py <folder> --mode cold`
2. Run: `HF_HUB_OFFLINE=1 .venv/bin/python scripts/eval_folder_search_routing.py --folder <folder> --mode hybrid --k 5 --no-mmr`

The default `~/Desktop/Writing` labeled query set is mostly English; add folder-specific labeled queries in `scripts/eval_folder_search_routing.py` for multilingual regression signal.
