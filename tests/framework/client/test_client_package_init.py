"""plugin.framework.client package init must not load the LLM stack."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("import sys\n"
        "from plugin.framework.client.requests import sync_request\n"
        "assert sync_request is not None\n"
        "assert 'plugin.framework.client.llm_client' not in sys.modules\n"
        "assert 'plugin.embeddings.embedding_client' not in sys.modules\n"
        "assert 'plugin.embeddings.embeddings_service' not in sys.modules\n"
        "assert 'plugin.framework.client.stream_normalizer' not in sys.modules\n"
        "assert 'plugin.scripting.client' not in sys.modules\n", id="requests-only"),
        pytest.param("import sys\n"
        "from plugin.chatbot.extension_update_check import schedule_extension_update_check_once\n"
        "from plugin.framework.client.requests import sync_request\n"
        "assert schedule_extension_update_check_once is not None\n"
        "assert sync_request is not None\n"
        "assert 'plugin.framework.client.llm_client' not in sys.modules\n", id="extension-update-check"),
    ],
)
def test_import_requests_does_not_load_llm_client(value):
    code = (
        value
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


