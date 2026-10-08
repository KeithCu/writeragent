from unittest.mock import patch

from plugin.framework.constants import folder_search_enabled
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

