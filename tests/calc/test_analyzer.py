"""Unit tests for SheetAnalyzer helpers, including Calc chat context."""

import pytest

from plugin.calc.analyzer import get_calc_context_for_chat


def test_get_calc_context_for_chat_requires_ctx():
    with pytest.raises(ValueError, match="ctx is required"):
        get_calc_context_for_chat(object())
