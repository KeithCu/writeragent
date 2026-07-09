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
from plugin.scripting.trusted_rpc import run_trusted_worker_action

_LANGDETECT_SESSION_ID = f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:langdetect"


def detect_languages(ctx: Any, texts: list[str]) -> list[str | None]:
    """Detect BCP-47 tags for *texts* via the embeddings venv worker."""
    if texts is None:
        texts = []

    timeout_sec = embeddings_worker_timeout_sec(ctx)
    try:
        result = run_trusted_worker_action(
            ctx,
            domain="langdetect",
            helper="detect",
            params={},
            additional_data={"texts": list(texts)},
            session_id=_LANGDETECT_SESSION_ID,
            timeout_sec=timeout_sec,
            worker_pool=WORKER_POOL_EMBEDDINGS,
            error_code="LANGDETECT_ERROR",
            error_label="Language detection",
        )
    except ToolExecutionError as exc:
        message = str(exc)
        if "venv" in message.lower() or "langdetect" in message.lower():
            raise ToolExecutionError(
                f"{message} Install with: {EMBEDDINGS_VENV_PIP_INSTALL}",
                code="LANGDETECT_ERROR",
                details=getattr(exc, "details", None),
            ) from exc
        raise
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
