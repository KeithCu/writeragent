# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import annotations

from enum import IntFlag

import os

APP_REFERER = "https://github.com/KeithCu/writeragent"
APP_TITLE = "WriterAgent"
USER_AGENT = f"{APP_TITLE} ({APP_REFERER})"


def get_plugin_dir():
    """Returns the absolute path to the plugin/ directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_locales_dir():
    """Absolute path to gettext ``locales/`` (sibling of ``plugin/`` in repo and in the .oxt bundle)."""
    return os.path.join(os.path.dirname(get_plugin_dir()), "locales")


PLUGIN_DIR = get_plugin_dir()

# Max characters of Writer document text embedded in chat system context (excerpt, not model window).
CHAT_DOCUMENT_CONTEXT_MAX_CHARS = 8000

# Local sentence-transformers default until multi-model bench picks a winner (docs/embeddings.md).
DEFAULT_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDINGS_WORKER_SESSION_PREFIX = "embeddings"
# Host corpus_meta.json schema version (docs/embeddings.md).
EMBEDDINGS_SCHEMA_VERSION = "6"
# Background folder index tick when embeddings cache is enabled (docs/embeddings.md).
EMBEDDINGS_INDEX_INTERVAL_S = 300
# Worker heartbeat during long folder maintain RPC (docs/embeddings.md).
EMBEDDINGS_HEARTBEAT_INTERVAL_S = 5
EMBEDDINGS_HEARTBEAT_GRACE_S = 90
# Max sub-chunks per embed+upsert window during ingest (docs/embeddings.md).
EMBEDDINGS_INGEST_BATCH_SIZE = 64
# Warm venv worker pools (docs/embeddings.md — dedicated embeddings subprocess).
WORKER_POOL_DEFAULT = "default"
WORKER_POOL_EMBEDDINGS = "embeddings"
# In-worker read-through corpus matrix cache TTL (seconds since last access).
EMBEDDINGS_CORPUS_CACHE_TTL_S = 60


# Model capabilities bitmasks (compatible with OnlyOfficeAI values)
class ModelCapability(IntFlag):
    NONE = 0
    CHAT = 1
    IMAGE = 2
    EMBEDDINGS = 4
    AUDIO = 8
    MODERATIONS = 16
    REALTIME = 32
    CODE = 64
    VISION = 128
    TOOLS = 256


# Toggle for specialized delegation approach.
# Approach A: The Sub-Agent Model (True) - Spins up a separate agent.
# Approach B: In-Place Tool Switching (False) - Switches the main model's tools.
USE_SUB_AGENT = True

# document_research cross-file index (Settings: embeddings.folder_search_mode none | hybrid).

_FOLDER_SEARCH_MODE_KEY = "embeddings.folder_search_mode"


def folder_search_enabled() -> bool:
    """True when cross-file corpus index (FTS + embeddings) is enabled."""
    from plugin.framework.config import get_config

    val = str(get_config(_FOLDER_SEARCH_MODE_KEY) or "none").strip().lower()
    return val in ("hybrid", "llama_index", "zvec", "lancedb")


# LlamaIndex cross-encoder rerank (Settings: embeddings.folder_rerank_enabled / folder_rerank_model).
FOLDER_RERANK_ENABLED_KEY = "embeddings.folder_rerank_enabled"
FOLDER_RERANK_MODEL_KEY = "embeddings.folder_rerank_model"
# English-only MS MARCO reranker — fast; pair with multilingual embeddings for non-English retrieve.
FOLDER_RERANK_MODEL_ENGLISH_SMALL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# Multilingual cross-encoder — slower, ~2.3 GB; use when folder queries are not English-only.
FOLDER_RERANK_MODEL_MULTILINGUAL = "BAAI/bge-reranker-v2-m3"
FOLDER_RERANK_MODEL_CHOICES = frozenset({
    FOLDER_RERANK_MODEL_ENGLISH_SMALL,
    FOLDER_RERANK_MODEL_MULTILINGUAL,
})
DEFAULT_FOLDER_RERANK_MODEL = FOLDER_RERANK_MODEL_ENGLISH_SMALL


def folder_rerank_enabled() -> bool:
    """True when LlamaIndex cross-encoder rerank is enabled in Settings."""
    from plugin.framework.config import get_config_bool

    return get_config_bool(FOLDER_RERANK_ENABLED_KEY)


def resolve_folder_rerank_model() -> str:
    """Resolve Settings rerank model id; unknown values fall back to English MiniLM."""
    from plugin.framework.config import get_config

    model = str(get_config(FOLDER_RERANK_MODEL_KEY) or DEFAULT_FOLDER_RERANK_MODEL).strip()
    if model in FOLDER_RERANK_MODEL_CHOICES:
        return model
    return DEFAULT_FOLDER_RERANK_MODEL


# Browser-style user agent for a small, whitelisted set of sites
# (e.g. DuckDuckGo and Wikipedia) that expect a real browser UA.
BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0"


# Prepend these in venv_sandbox when the module is available and not already imported.
AUTO_IMPORTS: dict[str, str] = {
    "numpy": "import numpy as np",
    "pandas": "import pandas as pd",
    "sympy": "import sympy as sp",
    "math": "import math",
    "datetime": "import datetime",
    "re": "import re",
    "random": "import random",
    "statistics": "import statistics",
    "collections": "import collections",
    "itertools": "import itertools",
    "json": "import json",
    "csv": "import csv",
    "plugin.scripting.calc_functions": "import plugin.scripting.calc_functions as xl",
}


# -------------------------------------------------
# Timezone utilities (pure Python, cross‑platform)
# -------------------------------------------------
import datetime as _dt
from typing import Optional


def get_local_timezone() -> Optional[_dt.tzinfo]:
    """Return the local timezone as a tzinfo object.

    Uses the standard library only: obtains the current UTC time,
    converts it to the local timezone and extracts the tzinfo.
    This works on any platform where the system timezone is set.
    """
    # Obtain an aware UTC datetime, then convert to local time.
    return _dt.datetime.now(_dt.timezone.utc).astimezone().tzinfo


def now_aware() -> _dt.datetime:
    """Return the current local time as a timezone‑aware datetime.

    The returned datetime has ``tzinfo`` set to the local timezone
    obtained via :func:`get_local_timezone`. This avoids the common
    pitfall of naive ``datetime.now()`` values that lack timezone
    information.
    """
    return _dt.datetime.now(get_local_timezone())
