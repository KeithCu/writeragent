# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted embeddings RPC runs outside LocalPythonExecutor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

_INDEX_STUB = """\
from plugin.embeddings.venv.embeddings_index import index_paragraphs as _index
result = _index(
    data["persist_dir"],
    data["collection_name"],
    data["meta_path"],
    data["model"],
    data["rows"],
)
"""


def test_trusted_embeddings_stub_bypasses_local_executor():
    fake_result = {"indexed": 2, "dim": 384}

    with patch(
        "plugin.scripting.venv.venv_sandbox._run_trusted_embeddings_payload",
        return_value={"status": "ok", "result": fake_result},
    ) as mock_run:
        response = run_sandboxed_code(
            _INDEX_STUB,
            data={
                "persist_dir": "/tmp/chroma",
                "collection_name": "folder",
                "meta_path": "/tmp/meta.json",
                "model": "all-MiniLM-L6-v2",
                "rows": [],
            },
        )

    assert response["status"] == "ok"
    assert response["result"] == fake_result
    mock_run.assert_called_once()


def test_trusted_embeddings_payload_calls_index_paragraphs():
    with patch("plugin.embeddings.venv.embeddings_index.index_paragraphs", return_value={"indexed": 1}) as mock_index:
        from plugin.scripting.venv.venv_sandbox import _run_trusted_embeddings_payload

        out = _run_trusted_embeddings_payload(
            _INDEX_STUB,
            {
                "persist_dir": "/tmp/chroma",
                "collection_name": "folder",
                "meta_path": "/tmp/meta.json",
                "model": "all-MiniLM-L6-v2",
                "rows": [{"text": "hi"}],
            },
        )

    assert out["status"] == "ok"
    assert out["result"]["indexed"] == 1
    mock_index.assert_called_once_with(
        "/tmp/chroma",
        "folder",
        "/tmp/meta.json",
        "all-MiniLM-L6-v2",
        [{"text": "hi"}],
    )


def test_user_code_still_uses_sandbox():
    with patch("plugin.scripting.venv.venv_sandbox._run_trusted_embeddings_payload") as mock_trusted:
        with patch("plugin.scripting.venv.venv_sandbox._run_on_executor", return_value={"status": "ok", "result": 2}) as mock_run:
            with patch("plugin.scripting.venv.venv_sandbox._new_executor", return_value=MagicMock()):
                response = run_sandboxed_code("result = 1 + 1")

    assert response["status"] == "ok"
    mock_trusted.assert_not_called()
    mock_run.assert_called_once()
