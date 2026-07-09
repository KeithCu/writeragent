# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side langdetect RPC — PyPI langdetect in the embeddings venv worker."""

from __future__ import annotations

from typing import Any

from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL
from plugin.framework.constants import EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import embeddings_worker_timeout_sec
from plugin.scripting.venv_worker import run_code_in_user_venv

_LANGDETECT_SESSION_ID = f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:langdetect"


def detect_languages(ctx: Any, texts: list[str]) -> list[str | None]:
    """Detect BCP-47 tags for *texts* via the embeddings venv worker."""
    if texts is None:
        texts = []

    timeout_sec = embeddings_worker_timeout_sec(ctx)
    response = run_code_in_user_venv(
        ctx,
        code=None,
        data={"domain": "langdetect", "texts": list(texts)},
        timeout_sec=timeout_sec,
        session_id=_LANGDETECT_SESSION_ID,
        worker_pool=WORKER_POOL_EMBEDDINGS,
        action="run_trusted_action",
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Language detection worker failed.")
        if "venv" in message.lower() or "langdetect" in message.lower():
            message = f"{message} Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        raise ToolExecutionError(message, code="LANGDETECT_ERROR", details={"worker": response})

    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Language detection worker returned an unexpected result.",
            code="LANGDETECT_ERROR",
            details={"result_type": type(result).__name__},
        )
    languages = result.get("languages")
    if not isinstance(languages, list):
        raise ToolExecutionError(
            "Language detection worker returned a malformed result.",
            code="LANGDETECT_ERROR",
            details={"keys": sorted(result.keys())},
        )
    if len(languages) != len(texts):
        raise ToolExecutionError(
            "Language detection worker returned mismatched batch length.",
            code="LANGDETECT_ERROR",
            details={"expected": len(texts), "got": len(languages)},
        )
    return languages
