# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deal contract shim.

Provides actual `deal` decorators when deal is installed, or no-op stubs
when running under standard LibreOffice Python runtime where deal is absent.
See docs/framework/formal-verification.md §8.1 E for string contract conventions.

``DEAL_MAX_*`` are finite ``@deal.pre`` domains, not production limits (release
OXTs strip ``@deal.*``; LibreOffice uses this shim as a no-op). Pytest /
``make test`` bind the wide table (ZZZ, Calc max row, CELL_REF=256).
CrossHair runner sets the short table via scripts/crosshair_domains.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def _max_col_index(n: int) -> int:
    """0-based max column for names of 1..n letters (3 → 18277 / A–ZZZ, 1 → 25 / A–Z)."""
    return (26 ** (n + 1) - 51) // 25


# Finite @deal.pre domains. Pytest arm is product-faithful.
# These are not production limits.
DEAL_MAX_COL_LETTERS = 3
DEAL_MAX_COL_INDEX = _max_col_index(DEAL_MAX_COL_LETTERS)  # 18277; not 26**n - 1
DEAL_MAX_CELL_REF = 256  # sheet-qualified ranges; was 32
DEAL_MAX_ROW_INDEX = 1_048_575  # Calc max 0-based row (1_048_576 rows)
DEAL_MAX_ARGV = 4096  # wrap_command argv, including venv -c probes (~1–2k)
DEAL_MAX_CMD_ARGS = 32  # wrap_command list length
DEAL_MAX_SHAPE_DIM = 256  # 100×100 pack tests fit
DEAL_MAX_SHAPE_RANK = 4  # ndarray rank; grids are 2-D, pytest uses up to 4
DEAL_MAX_PLACEHOLDER_INDEX = 64  # Excel %Pn% deps; f-string of the int
DEAL_MAX_SOURCE = 8192  # real =PY() / Excel scripts
DEAL_MAX_MSGID = 1024  # gettext _(); longest shipped msgid is ~500 chars
DEAL_MAX_PATH = 256  # filesystem paths (is_safe_workspace_path); not PATH_MAX
DEAL_MAX_TOKEN = 64
DEAL_MAX_XL_EXPR = 64  # DAG xl("%Pn%",headers=False)
DEAL_MAX_ORIGIN = 256  # CORS Origin
DEAL_MAX_URL = 256  # endpoint URLs
DEAL_MAX_RETRY = 8
DEAL_MAX_BACKOFF = 300.0
DEAL_MAX_BACKOFF_FACTOR = 10.0
DEAL_MAX_HTML_CHUNK = 4096  # feed() / strip_html_tags slice to this (was 512)


def ascii_bounded(s: object, max_len: int, min_len: int = 0) -> bool:
    """True iff *s* is an ASCII str with min_len <= len(s) <= max_len.

    Use in ``@deal.pre`` for closed alphabets (cell refs, tokens, origins).
    max_len is required: pick a domain cap, do not invent a global default.
    """
    return isinstance(s, str) and s.isascii() and min_len <= len(s) <= max_len


def str_bounded(s: object, max_len: int, min_len: int = 0) -> bool:
    """True iff *s* is a str with min_len <= len(s) <= max_len (Unicode allowed).

    Use in ``@deal.pre`` for open text (gettext, HTML, source). Length still
    caps CrossHair; ``isascii`` would reject real **dev pytest** call sites.
    """
    return isinstance(s, str) and min_len <= len(s) <= max_len


def _noop_contract(*args: Any, **kwargs: Any) -> Any:
    """No-op decorator for deal contracts when deal is not installed."""
    return lambda f: f


deal: Any

try:
    import deal as _deal  # type: ignore[no-redef]

    deal = _deal
except ImportError:
    deal = SimpleNamespace(
        pre=_noop_contract,
        post=_noop_contract,
        ensure=_noop_contract,
        raises=_noop_contract,
    )


inverse_ensure = deal.ensure


def asserts_enabled() -> bool:
    """Return True if Python assert statements in production code are active."""
    try:
        assert False
        return False
    except AssertionError:
        return True
