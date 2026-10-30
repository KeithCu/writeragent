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
Ingest boundaries (LLM JSON, user config, IPC / MCP codecs, calc ranges,
envelope detectors, ``host_unpack_data``, and the same class of external
input) are total on that pytest profile: a value that is merely large,
oddly shaped, or a real error object must not raise ``PreContractError``.
The body returns false or a structured error. Internal algorithms that
really have a fixed bound keep ``@deal.pre``. CrossHair binds the short
table only when
``WRITERAGENT_CROSSHAIR=1`` at import, which check-all, cover-all (including
process-pool workers), and ``scripts/crosshair_stream.py run`` set before
spawning CrossHair. Do not sniff ``sys.modules["crosshair"]`` or
``is_tracing()``, and do not branch inside ``@deal.pre`` lambdas — CrossHair
would explore both branches. Production mock-outs (errors, JSON repair, UNO
walks) use ``UNDER_CROSSHAIR`` (same import-time env), not ``sys.modules``.
Nested inverse ``@deal.ensure`` (format_address → parse_address,
column_to_index → index_to_column) is skipped under CrossHair via
``inverse_ensure``; cheap ``@deal.post`` still runs.

Dual-Profile Preconditions (UNDER_CROSSHAIR):
When restricting input domains strictly for CrossHair concolic execution (e.g.,
dropping ``hasattr(c, "dtype")``, restricting ``BaseException`` hierarchies, or
clamping dict/string alphabets), select between tiny CrossHair domains and wide
pytest/production domains via ``UNDER_CROSSHAIR`` at import time:
``_deal_fn = _deal_fn_crosshair if UNDER_CROSSHAIR else _deal_fn_pytest``.
This keeps CrossHair check-all fast while preserving NumPy and production runtime
type compatibility during ``make test``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# Import-time flag. CrossHair runners set this; pytest / make test do not.
CROSSHAIR_ENV = "WRITERAGENT_CROSSHAIR"


# Import-time only. Never sniff sys.modules["crosshair"].
UNDER_CROSSHAIR = os.environ.get(CROSSHAIR_ENV) == "1"


def _max_col_index(n: int) -> int:
    """0-based max column for names of 1..n letters (3 → 18277 / A–ZZZ, 1 → 25 / A–Z)."""
    return (26 ** (n + 1) - 51) // 25


# Finite @deal.pre domains, chosen once here. Pytest arm is product-faithful;
# CrossHair arm is tiny. These are not production limits.
DEAL_MAX_COL_LETTERS = 1 if UNDER_CROSSHAIR else 3
DEAL_MAX_COL_INDEX = _max_col_index(DEAL_MAX_COL_LETTERS)  # 25 or 18277; not 26**n - 1
DEAL_MAX_CELL_REF = 4 if UNDER_CROSSHAIR else 256  # sheet-qualified ranges; was 32
DEAL_MAX_ROW_INDEX = 20 if UNDER_CROSSHAIR else 1_048_575  # Calc max 0-based row (1_048_576 rows)
DEAL_MAX_ARGV = 32 if UNDER_CROSSHAIR else 4096  # wrap_command argv, including venv -c probes (~1–2k)
DEAL_MAX_CMD_ARGS = 4 if UNDER_CROSSHAIR else 32  # wrap_command list length
DEAL_MAX_SHAPE_DIM = 4 if UNDER_CROSSHAIR else 256  # 100×100 pack tests fit; CrossHair uses 4
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


def _profile(crosshair_fn: Any, pytest_fn: Any = lambda *args, **kwargs: True) -> Any:  # pyright: ignore[reportUnusedFunction]  # imported by payload_codec, calc_range, editor_errors, editor_ipc
    """Pick the contract predicate once at import.

    First argument is the CrossHair predicate, second is the pytest predicate.
    Do not add another ``_profile`` with those two arguments reversed: the
    same name then selects the tiny domain under pytest, or the wide domain
    under CrossHair, and the other profile's tests still pass.

    Do not branch on ``UNDER_CROSSHAIR`` inside ``@deal.pre``; CrossHair would
    explore both arms. ``pytest_fn`` defaults to always-true when only the
    CrossHair domain is restricted.
    """
    # crosshair: off  # import-time selector; covering both arms is the bug this avoids.
    return crosshair_fn if UNDER_CROSSHAIR else pytest_fn


# format_address → parse_address and column_to_index → index_to_column.
# Import-time only. No-op under CrossHair so nested inverse @deal.ensure is
# not explored; cheap @deal.post still runs. Do not branch inside @deal.pre.
inverse_ensure = _noop_contract if UNDER_CROSSHAIR else deal.ensure


def asserts_enabled() -> bool:
    """Return True if Python assert statements in production code are active."""
    try:
        assert False
        return False
    except AssertionError:
        return True

