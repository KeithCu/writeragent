# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Domain bounds and helper functions for sidecar contracts.

DEAL_MAX_* are finite @deal.pre domains used by contract sidecars.
Pytest / make test bind the wide table.
CrossHair binds the short table only when WRITERAGENT_CROSSHAIR=1 at import.
"""

from __future__ import annotations

import os
from typing import Any

import deal

# Import-time flag. CrossHair runners set this; pytest / make test do not.
CROSSHAIR_ENV = "WRITERAGENT_CROSSHAIR"

# Import-time only. Never sniff sys.modules["crosshair"].
UNDER_CROSSHAIR = os.environ.get(CROSSHAIR_ENV) == "1"


def _max_col_index(n: int) -> int:
    """0-based max column for names of 1..n letters (3 -> 18277 / A-ZZZ, 1 -> 25 / A-Z)."""
    return (26 ** (n + 1) - 51) // 25


DEAL_MAX_COL_LETTERS = 1 if UNDER_CROSSHAIR else 3
DEAL_MAX_COL_INDEX = _max_col_index(DEAL_MAX_COL_LETTERS)  # 25 or 18277; not 26**n - 1
DEAL_MAX_CELL_REF = 4 if UNDER_CROSSHAIR else 256  # sheet-qualified ranges; was 32
DEAL_MAX_ROW_INDEX = 20 if UNDER_CROSSHAIR else 1_048_575  # Calc max 0-based row (1_048_576 rows)
DEAL_MAX_ARGV = 32 if UNDER_CROSSHAIR else 4096  # wrap_command argv, including venv -c probes (~1-2k)
DEAL_MAX_CMD_ARGS = 4 if UNDER_CROSSHAIR else 32  # wrap_command list length
DEAL_MAX_SHAPE_DIM = 4 if UNDER_CROSSHAIR else 256  # 100x100 pack tests fit; CrossHair uses 4
DEAL_MAX_SHAPE_RANK = 2 if UNDER_CROSSHAIR else 4  # ndarray rank; grids are 2-D, pytest uses up to 4
DEAL_MAX_PLACEHOLDER_INDEX = 4 if UNDER_CROSSHAIR else 64  # Excel %Pn% deps; f-string of the int
DEAL_MAX_SOURCE = 16 if UNDER_CROSSHAIR else 8192  # real =PY() / Excel scripts; CrossHair stays 16
DEAL_MAX_MSGID = 1024  # gettext _(); longest shipped msgid is ~500 chars
DEAL_MAX_PATH = 32 if UNDER_CROSSHAIR else 256  # filesystem paths (is_safe_workspace_path); not PATH_MAX
DEAL_MAX_TOKEN = 16 if UNDER_CROSSHAIR else 64
DEAL_MAX_XL_EXPR = 32 if UNDER_CROSSHAIR else 64  # DAG xl("%Pn%",headers=False); CrossHair uses 32
DEAL_MAX_ORIGIN = 32 if UNDER_CROSSHAIR else 256  # CORS Origin; CrossHair uses 32
DEAL_MAX_URL = 32 if UNDER_CROSSHAIR else 256  # endpoint URLs; CrossHair uses 32
DEAL_MAX_RETRY = 8
DEAL_MAX_BACKOFF = 300.0
DEAL_MAX_BACKOFF_FACTOR = 10.0
DEAL_MAX_HTML_CHUNK = 16 if UNDER_CROSSHAIR else 4096  # feed() / strip_html_tags slice to this (was 512)


def ascii_bounded(s: object, max_len: int, min_len: int = 0) -> bool:
    """True iff *s* is an ASCII str with min_len <= len(s) <= max_len.

    Use in @deal.pre for closed alphabets (cell refs, tokens, origins).
    max_len is required: pick a domain cap, do not invent a global default.
    """
    return isinstance(s, str) and s.isascii() and min_len <= len(s) <= max_len


def str_bounded(s: object, max_len: int, min_len: int = 0) -> bool:
    """True iff *s* is a str with min_len <= len(s) <= max_len (Unicode allowed).

    Use in @deal.pre for open text (gettext, HTML, source). Length still
    caps CrossHair; isascii would reject real dev pytest call sites.
    """
    return isinstance(s, str) and min_len <= len(s) <= max_len


def profile(crosshair_fn: Any, pytest_fn: Any = lambda *args, **kwargs: True) -> Any:
    """Pick the contract predicate once at import.

    First argument is the CrossHair predicate, second is the pytest predicate.
    """
    return crosshair_fn if UNDER_CROSSHAIR else pytest_fn


_profile = profile


def _noop_contract(*args: Any, **kwargs: Any) -> Any:
    def decorator(fn: Any) -> Any:
        return fn
    return decorator


inverse_ensure = _noop_contract if UNDER_CROSSHAIR else deal.ensure
