# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deal contract shim.

Provides actual `deal` decorators when deal is installed, or no-op stubs
when running under standard LibreOffice Python runtime where deal is absent.
See docs/framework-formal-verification.md §8.1 E for string contract conventions.
"""

from typing import Any

# Domain caps for @deal.pre only. Release OXTs strip @deal.* and LibreOffice
# uses this shim as a no-op, so these lengths never bind shipped code.
DEAL_MAX_COL_LETTERS = 3
DEAL_MAX_CELL_REF = 32
DEAL_MAX_TOKEN = 64
DEAL_MAX_ORIGIN = 256
DEAL_MAX_URL = 2048
DEAL_MAX_PATH = 4096
DEAL_MAX_SOURCE = 64
# Int domains need caps too (same reason as §8.1 E): CrossHair otherwise
# unrolls `while index > 0` forever on a giant int in deep check.
DEAL_MAX_COL_INDEX = 26 + 26**2 + 26**3 - 1  # 18277, A–ZZZ (not 26**3-1)
DEAL_MAX_ROW_INDEX = 1_048_576 - 1  # Excel/Calc max row, 0-based


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
