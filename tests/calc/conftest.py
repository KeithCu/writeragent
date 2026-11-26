# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures shared by the Calc unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def calc_ctx() -> MagicMock:
    """A mock tool context for a Calc document (mock ``doc`` and UNO ``ctx``)."""
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx
