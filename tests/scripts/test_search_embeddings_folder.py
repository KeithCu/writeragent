# WriterAgent tests for scripts/search_embeddings_folder.py
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from search_embeddings_folder import (  # noqa: E402
    DEFAULT_FOLDER,
    DEFAULT_K,
    SearchFolderError,
    format_hits,
    main,
    search_folder,
    search_folder_fts_cli,
)


def test_search_folder_requires_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(SearchFolderError, match="Not a directory"):
        search_folder(missing, "query")


def test_search_folder_requires_query(tmp_path: Path) -> None:
    with pytest.raises(SearchFolderError, match="query is required"):
        search_folder(tmp_path, "   ")


def test_search_folder_empty_cache(tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    with pytest.raises(SearchFolderError, match="No indexed embeddings cache"):
        search_folder(folder, "budget figures")


def test_search_folder_uses_meta_model_and_default_k(tmp_path: Path) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    cache_dir = folder / "writeragent_embeddings"
    chroma_dir = cache_dir / "chroma"
    chroma_dir.mkdir(parents=True)
    meta_path = cache_dir / "corpus_meta.json"
    meta_path.write_text(
        json.dumps({"embedding_model": "custom-model", "chunk_count": "3"}),
        encoding="utf-8",
    )

    fake_hits = [{"doc_url": "file:///a.odt", "para_index": 1, "snippet": "text", "score": 0.9}]
    with patch("search_embeddings_folder.knn_search", return_value={"hits": fake_hits}) as mock_search:
        result = search_folder(folder, "remote work")

    mock_search.assert_called_once()
    args, kwargs = mock_search.call_args
    assert args[2] == "remote work"
    assert args[3] == DEFAULT_K
    assert kwargs["model_name"] == "custom-model"
    assert kwargs["doc_url_filter"] is None
    assert result["status"] == "ok"
    assert result["hits"] == fake_hits
    assert result["model"] == "custom-model"


def test_search_folder_model_override(tmp_path: Path) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    cache_dir = folder / "writeragent_embeddings"
    chroma_dir = cache_dir / "chroma"
    chroma_dir.mkdir(parents=True)
    meta_path = cache_dir / "corpus_meta.json"
    meta_path.write_text(json.dumps({"embedding_model": "stored-model", "chunk_count": "1"}), encoding="utf-8")

    with patch("search_embeddings_folder.knn_search", return_value={"hits": []}) as mock_search:
        search_folder(folder, "q", model="override-model", k=5, doc_url="file:///x.odt")

    kwargs = mock_search.call_args.kwargs
    assert kwargs["model_name"] == "override-model"
    assert mock_search.call_args.args[3] == 5
    assert kwargs["doc_url_filter"] == "file:///x.odt"


def test_format_hits_includes_score_and_snippet() -> None:
    text = format_hits(
        {
            "folder": "/tmp/Writing",
            "query": "policy",
            "model": "all-MiniLM-L6-v2",
            "hits": [
                {
                    "doc_url": "file:///a.odt",
                    "para_index": 2,
                    "snippet": "remote work policy",
                    "score": 0.8765,
                }
            ],
        }
    )
    assert "score=0.8765" in text
    assert "file:///a.odt" in text
    assert "remote work policy" in text


def test_main_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    cache_dir = folder / "writeragent_embeddings"
    chroma_dir = cache_dir / "chroma"
    chroma_dir.mkdir(parents=True)
    meta_path = cache_dir / "corpus_meta.json"
    meta_path.write_text(json.dumps({"embedding_model": "m", "chunk_count": "1"}), encoding="utf-8")

    with patch("search_embeddings_folder.knn_search", return_value={"hits": [{"score": 0.5}]}):
        code = main(["query text", "--folder", str(folder), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["query"] == "query text"
    assert isinstance(payload["hits"], list)


def test_main_default_folder_constant() -> None:
    assert DEFAULT_FOLDER == Path("~/Desktop/Writing")


def test_main_missing_query_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    code = main(["", "--folder", str(folder)])
    assert code == 2
    assert "query is required" in capsys.readouterr().err


def test_search_folder_fts_empty_cache(tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    with pytest.raises(SearchFolderError, match="No FTS index"):
        search_folder_fts_cli(folder, "web search")


def test_search_folder_fts_happy_path(tmp_path: Path) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    cache_dir = folder / "writeragent_embeddings"
    cache_dir.mkdir(parents=True)
    db_path = cache_dir / "fts5.db"
    db_path.write_text("", encoding="utf-8")
    meta_path = cache_dir / "fts_meta.json"
    meta_path.write_text(json.dumps({"schema_version": "1", "row_count": "2"}), encoding="utf-8")

    fake_hits = [{"doc_url": "file:///part2.odt", "para_index": 1, "snippet": "web search", "score": -1.2}]
    with patch("search_embeddings_folder.search_folder_fts", return_value={"hits": fake_hits, "match": 'NEAR("web" "search", 10)'}) as mock_fts:
        result = search_folder_fts_cli(folder, "web search", k=5)

    mock_fts.assert_called_once()
    assert result["backend"] == "fts"
    assert result["hits"] == fake_hits
    assert result["match"] == 'NEAR("web" "search", 10)'


def test_main_fts_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    cache_dir = folder / "writeragent_embeddings"
    cache_dir.mkdir(parents=True)
    (cache_dir / "fts5.db").write_text("", encoding="utf-8")
    meta_path = cache_dir / "fts_meta.json"
    meta_path.write_text(json.dumps({"schema_version": "1", "row_count": "1"}), encoding="utf-8")

    with patch(
        "search_embeddings_folder.search_folder_fts",
        return_value={"hits": [{"doc_url": "file:///a.odt", "score": -1.0, "snippet": "x", "para_index": 0}], "match": "m"},
    ):
        code = main(["--fts", "web search", "--folder", str(folder), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "fts"
    assert payload["query"] == "web search"


def test_format_hits_fts_backend() -> None:
    text = format_hits(
        {
            "folder": "/tmp/Writing",
            "query": "web search",
            "backend": "fts",
            "match": 'NEAR("web" "search", 10)',
            "hits": [{"doc_url": "file:///a.odt", "para_index": 0, "snippet": "web search tools", "score": -0.5}],
        }
    )
    assert "Backend: FTS" in text
    assert 'NEAR("web" "search", 10)' in text
    assert "Model:" not in text
