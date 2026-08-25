# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deal contract shim.

Provides actual `deal` decorators when deal is installed, or no-op stubs
when running under standard LibreOffice Python runtime where deal is absent.
See docs/framework-formal-verification.md §8.1 E for string contract conventions.

``DEAL_MAX_*`` are finite ``@deal.pre`` domains, not production limits (release
OXTs strip ``@deal.*``; LibreOffice uses this shim as a no-op). Pytest /
``make test`` bind the wide, product-faithful table (ZZZ, Calc max row,
CELL_REF=32). CrossHair binds the short table only when
``WRITERAGENT_CROSSHAIR=1`` at import, which check-all, cover-all (including
process-pool workers), and ``scripts/crosshair_stream.py run`` set before
spawning CrossHair. Do not sniff ``sys.modules["crosshair"]`` or
``is_tracing()``, and do not branch inside ``@deal.pre`` lambdas — CrossHair
would explore both branches. Nested inverse ``@deal.ensure`` (format_address
→ parse_address, column_to_index → index_to_column) is skipped under CrossHair
via ``inverse_ensure``; cheap ``@deal.post`` still runs.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

# Import-time flag. CrossHair runners set this; pytest / make test do not.
CROSSHAIR_ENV = "WRITERAGENT_CROSSHAIR"


class DealMaxima(NamedTuple):
    """Finite ``@deal.pre`` domains. Pytest is product-faithful; CrossHair is tiny."""

    col_letters: int
    col_index: int
    cell_ref: int
    row_index: int
    argv: int
    cmd_args: int
    shape_dim: int
    shape_rank: int
    placeholder_index: int
    source: int
    msgid: int
    path: int
    token: int
    xl_expr: int
    origin: int
    url: int
    retry: int
    backoff: float
    backoff_factor: float
    html_chunk: int


def deal_maxima(*, crosshair: bool) -> DealMaxima:
    """Return the pytest (wide) or CrossHair (tiny) ``DEAL_MAX_*`` table.

    Import-time only. Do not call from inside ``@deal.pre`` lambdas — CrossHair
    would explore both branches. Pair ``col_letters`` with ``col_index``
    (3 with 18277 / A–ZZZ, 1 with 25 / A–Z).
    """
    # Shared: CORS / URL / MCP backoff. No test-backed reason to shrink.
    origin = 256
    url = 256
    retry = 8
    backoff = 300.0
    backoff_factor = 10.0
    if crosshair:
        # Short table: CrossHair is testing our code, not Calc's grid. Speed
        # comes from tiny domains, not from timeouts.
        return DealMaxima(
            col_letters=1,
            col_index=25,  # A–Z; must pair with col_letters=1
            cell_ref=4,
            row_index=20,
            argv=32,
            cmd_args=4,
            shape_dim=4,  # 100×100 pack tests are pytest-only
            shape_rank=2,
            placeholder_index=4,
            source=16,
            msgid=1024,  # real UI _() strings run at import; do not shrink
            path=32,
            token=16,
            xl_expr=32,  # xl("%Pn%",headers=False) is 24; TOKEN=16 is too small
            origin=origin,
            url=url,
            retry=retry,
            backoff=backoff,
            backoff_factor=backoff_factor,
            html_chunk=64,
        )
    return DealMaxima(
        col_letters=3,
        col_index=26 + 26**2 + 26**3 - 1,  # 18277, A–ZZZ (not 26**3-1)
        cell_ref=32,
        row_index=1_048_575,  # Calc max 0-based row (1_048_576 rows)
        argv=4096,  # wrap_command argv, including venv ``-c`` probes (~1–2k)
        cmd_args=32,  # wrap_command list length
        shape_dim=256,  # 100×100 pack tests fit; CrossHair uses 4
        shape_rank=4,  # ndarray rank; grids are 2-D, pytest uses up to 4
        placeholder_index=64,  # Excel %Pn% deps; f-string of the int
        source=8192,  # real =PY() / Excel scripts; CrossHair stays 16
        msgid=1024,  # gettext _(); longest shipped msgid is ~500 chars
        path=256,  # filesystem paths (is_safe_workspace_path); not PATH_MAX
        token=64,
        xl_expr=64,  # DAG xl("%Pn%",headers=False); CrossHair uses 32
        origin=origin,
        url=url,
        retry=retry,
        backoff=backoff,
        backoff_factor=backoff_factor,
        html_chunk=512,  # wider than 256 so pytest still hits the tag-flush path
    )


_CROSSHAIR = os.environ.get(CROSSHAIR_ENV) == "1"
_MAXIMA = deal_maxima(crosshair=_CROSSHAIR)
DEAL_MAX_COL_LETTERS = _MAXIMA.col_letters
DEAL_MAX_COL_INDEX = _MAXIMA.col_index
DEAL_MAX_CELL_REF = _MAXIMA.cell_ref
DEAL_MAX_ROW_INDEX = _MAXIMA.row_index
DEAL_MAX_ARGV = _MAXIMA.argv
DEAL_MAX_CMD_ARGS = _MAXIMA.cmd_args
DEAL_MAX_SHAPE_DIM = _MAXIMA.shape_dim
DEAL_MAX_SHAPE_RANK = _MAXIMA.shape_rank
DEAL_MAX_PLACEHOLDER_INDEX = _MAXIMA.placeholder_index
DEAL_MAX_SOURCE = _MAXIMA.source
DEAL_MAX_MSGID = _MAXIMA.msgid
DEAL_MAX_PATH = _MAXIMA.path
DEAL_MAX_TOKEN = _MAXIMA.token
DEAL_MAX_XL_EXPR = _MAXIMA.xl_expr
DEAL_MAX_ORIGIN = _MAXIMA.origin
DEAL_MAX_URL = _MAXIMA.url
DEAL_MAX_RETRY = _MAXIMA.retry
DEAL_MAX_BACKOFF = _MAXIMA.backoff
DEAL_MAX_BACKOFF_FACTOR = _MAXIMA.backoff_factor
DEAL_MAX_HTML_CHUNK = _MAXIMA.html_chunk


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

deal: Any

try:
    import deal as _deal  # type: ignore[no-redef]
    deal = _deal
except ImportError:

    class _DealStub:
        """No-op stub for deal contract decorators when deal is not installed."""

        def pre(self, *args, **kwargs):
            return lambda f: f

        def post(self, *args, **kwargs):
            return lambda f: f

        def inv(self, *args, **kwargs):
            return lambda f: f

        def pure(self, f=None, *args, **kwargs):
            return f if f is not None else (lambda fn: fn)

        def chain(self, *args, **kwargs):
            return lambda f: f

        def raises(self, *args, **kwargs):
            return lambda f: f

        def example(self, *args, **kwargs):
            return lambda f: f

        def ensure(self, *args, **kwargs):
            return lambda f: f

        def reason(self, *args, **kwargs):
            return lambda f: f

    deal = _DealStub()


def _identity_contract(*args: Any, **kwargs: Any) -> Any:
    """No-op decorator: skip nested inverse ensures under CrossHair."""
    return lambda f: f


def inverse_ensure_for(*, crosshair: bool) -> Any:
    """Nested inverse ``@deal.ensure``: live under pytest, no-op under CrossHair.

    Bound at import, not inside a ``@deal.pre`` lambda — CrossHair would explore
    both branches of an in-lambda ``if``. Cheap ``@deal.post`` still runs.
    """
    if crosshair:
        return _identity_contract
    return deal.ensure


# format_address → parse_address and column_to_index → index_to_column.
inverse_ensure = inverse_ensure_for(crosshair=_CROSSHAIR)
