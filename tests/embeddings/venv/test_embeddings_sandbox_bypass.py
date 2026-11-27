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

from tests.scripting.sandbox_support import sandbox_run
from plugin.scripting.venv.worker_harness import _handle_request

_SEARCH_PARAMS = {
    "db_path": "/tmp/corpus.db",
    "query": "hi",
    "k": 5,
    "model": "all-MiniLM-L6-v2",
}


def test_trusted_embeddings_stub_bypasses_local_executor():
    fake_result = {"hits": []}

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
                            "helper": "hybrid_search",
                            "params": _SEARCH_PARAMS,
                        },
                    }
                )

    assert response is not None
    assert response["status"] == "ok"
    assert response["result"] == fake_result
    mock_run.assert_called_once()
    mock_exec.assert_not_called()
    mock_sandbox.assert_not_called()


def test_user_code_still_uses_sandbox():
    with patch(
        "plugin.embeddings.venv.embeddings_index_dispatch.dispatch_trusted",
    ) as mock_trusted:
        with patch(
            "plugin.scripting.venv.venv_sandbox._run_on_executor",
            return_value={"status": "ok", "result": 2},
        ) as mock_run:
            with patch("plugin.scripting.venv.venv_sandbox._new_executor", return_value=MagicMock()):
                response = sandbox_run("result = 1 + 1")

    assert response["status"] == "ok"
    mock_trusted.assert_not_called()
    mock_run.assert_called_once()
