# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deal contract shim.

Provides actual `deal` decorators when deal is installed, or no-op stubs
when running under standard LibreOffice Python runtime where deal is absent.
"""

from typing import Any

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
