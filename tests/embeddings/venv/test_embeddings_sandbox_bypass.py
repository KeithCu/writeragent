# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted embeddings RPC runs outside LocalPythonExecutor.

Trusted index/search goes through ``run_trusted_action`` (worker harness →
``embeddings_index_dispatch``). User code still executes in the sandbox.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code
from plugin.scripting.venv.worker_harness import _handle_request

_INDEX_PARAMS = {
    "db_path": "/tmp/corpus.db",
    "meta_path": "/tmp/meta.json",
    "model": "all-MiniLM-L6-v2",
    "rows": [{"text": "hi"}],
}


def test_trusted_embeddings_stub_bypasses_local_executor():
    fake_result = {"indexed": 2, "dim": 384}

    with patch(
        "plugin.embeddings.venv.embeddings_index_dispatch.dispatch_trusted",
        return_value=fake_result,
    ) as mock_run:
        with patch("plugin.scripting.venv.venv_sandbox._run_on_executor") as mock_exec:
            with patch("plugin.scripting.venv.venv_sandbox.run_sandboxed_code") as mock_sandbox:
                response = _handle_request(
                    {
                        "action": "run_trusted_action",
                        "data": {
                            "domain": "embeddings_index",
                            "helper": "index_paragraphs",
                            "params": _INDEX_PARAMS,
                        },
                    }
                )

    assert response is not None
    assert response["status"] == "ok"
    assert response["result"] == fake_result
    mock_run.assert_called_once()
    mock_exec.assert_not_called()
    mock_sandbox.assert_not_called()


def test_trusted_embeddings_payload_calls_index_paragraphs():
    with patch(
        "plugin.embeddings.venv.embeddings_index.index_paragraphs",
        return_value={"indexed": 1},
    ) as mock_index:
        out = _handle_request(
            {
                "action": "run_trusted_action",
                "data": {
                    "domain": "embeddings_index",
                    "helper": "index_paragraphs",
                    "params": _INDEX_PARAMS,
                },
            }
        )

    assert out is not None
    assert out["status"] == "ok"
    assert out["result"]["indexed"] == 1
    mock_index.assert_called_once_with(
        "/tmp/corpus.db",
        "/tmp/meta.json",
        "all-MiniLM-L6-v2",
        [{"text": "hi"}],
        build_fts=False,
        build_vectors=True,
    )


def test_user_code_still_uses_sandbox():
    with patch(
        "plugin.embeddings.venv.embeddings_index_dispatch.dispatch_trusted",
    ) as mock_trusted:
        with patch(
            "plugin.scripting.venv.venv_sandbox._run_on_executor",
            return_value={"status": "ok", "result": 2},
        ) as mock_run:
            with patch("plugin.scripting.venv.venv_sandbox._new_executor", return_value=MagicMock()):
                response = run_sandboxed_code("result = 1 + 1")

    assert response["status"] == "ok"
    mock_trusted.assert_not_called()
    mock_run.assert_called_once()
