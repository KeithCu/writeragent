# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side embedding client — routes batch encode to the warm venv worker (Phase A)."""
from __future__ import annotations

import dataclasses
from typing import Any

from plugin.framework.config import get_config
from plugin.framework.constants import DEFAULT_EMBEDDING_MODEL, EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ConfigError, ToolExecutionError
from plugin.scripting.config_limits import embeddings_worker_timeout_sec
from plugin.scripting.venv_worker import run_code_in_user_venv

# --- Client ---


@dataclasses.dataclass(frozen=True)
class EmbeddingBatch:
    """Batch embedding result (host-safe floats, no NumPy on the LO side)."""

    model: str
    dim: int
    vectors: list[list[float]]
    indices: list[int]


def get_embedding_model() -> str:
    """Return configured local embedding model id (HuggingFace sentence-transformers name)."""
    val = str(get_config("embedding_model") or "").strip()
    return val or DEFAULT_EMBEDDING_MODEL


def _embedding_session_id(model: str) -> str:
    slug = model.replace("/", "_").replace(":", "_")
    return f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:{slug}"


def _parse_worker_result(payload: dict[str, Any], *, model: str) -> EmbeddingBatch:
    if not isinstance(payload, dict):
        raise ToolExecutionError(
            "Embedding worker returned an unexpected result.",
            code="EMBEDDING_ERROR",
            details={"result_type": type(payload).__name__},
        )
    dim = payload.get("dim")
    vectors = payload.get("vectors")
    indices = payload.get("indices")
    if not isinstance(dim, int) or not isinstance(vectors, list) or not isinstance(indices, list):
        raise ToolExecutionError(
            "Embedding worker returned a malformed result.",
            code="EMBEDDING_ERROR",
            details={"keys": sorted(payload.keys())},
        )
    return EmbeddingBatch(
        model=str(payload.get("model") or model),
        dim=dim,
        vectors=vectors,
        indices=indices,
    )


def embed_texts(ctx: Any, texts: list[str], *, model: str | None = None, timeout_sec: int | None = None) -> EmbeddingBatch:
    """Encode *texts* to float32 vectors via the user venv (sentence-transformers).

    Empty strings are skipped on the worker side; see ``EmbeddingBatch.indices`` for alignment.
    """
    provider = str(get_config("embedding_provider") or "local").strip().lower() or "local"
    if provider != "local":
        raise ConfigError(
            f"Embedding provider {provider!r} is not implemented yet. Use embedding_provider=local with a configured Python venv.",
            code="EMBEDDING_PROVIDER_UNSUPPORTED",
            details={"provider": provider},
        )

    model_name = (model or get_embedding_model()).strip()
    if not model_name:
        raise ConfigError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")

    if texts is None:
        texts = []

    resolved_timeout_sec = embeddings_worker_timeout_sec(ctx) if timeout_sec is None else int(timeout_sec)
    response = run_code_in_user_venv(
        ctx,
        code=None,
        data={"domain": "embedding", "model": model_name, "texts": list(texts)},
        timeout_sec=resolved_timeout_sec,
        session_id=_embedding_session_id(model_name),
        worker_pool=WORKER_POOL_EMBEDDINGS,
        action="run_trusted_action",
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Embedding worker failed.")
        raise ToolExecutionError(message, code="EMBEDDING_ERROR", details={"worker": response})

    return _parse_worker_result(response.get("result") or {}, model=model_name)
