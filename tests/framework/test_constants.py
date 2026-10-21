from unittest.mock import patch

from plugin.framework.constants import CHAT_DOCUMENT_TRUNCATED_MARKER, folder_search_enabled, truncate_chat_context
import pytest


@pytest.mark.parametrize(
    "return_value, expected",
    [
        pytest.param(None, False, id="test_folder_search_disabled_by_default"),
        pytest.param("hybrid", True, id="test_folder_search_enabled_when_hybrid"),
        pytest.param("embeddings", False, id="test_folder_search_disabled_for_other_values"),
    ],
)
def test_folder_search_disabled_by_default(return_value, expected):
    with patch("plugin.framework.config.get_config", return_value=return_value):
        assert folder_search_enabled() is expected


def test_truncate_chat_context():
    # Short string returned unchanged
    assert truncate_chat_context("hello world", 50) == "hello world"

    # Truncated string
    assert truncate_chat_context("hello world", 5) == "hello" + CHAT_DOCUMENT_TRUNCATED_MARKER

    # bool limit -> empty prefix plus marker
    assert truncate_chat_context("hello world", True) == CHAT_DOCUMENT_TRUNCATED_MARKER
    assert truncate_chat_context("hello world", False) == CHAT_DOCUMENT_TRUNCATED_MARKER

    # Negative limit -> empty prefix plus marker
    assert truncate_chat_context("hello world", -5) == CHAT_DOCUMENT_TRUNCATED_MARKER

    # Non-int limit -> empty prefix plus marker
    assert truncate_chat_context("hello world", "50") == CHAT_DOCUMENT_TRUNCATED_MARKER


