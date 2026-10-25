"""DrawModule discovery skips the disabled transform module."""

from unittest.mock import MagicMock


def test_initialize_skips_disabled_transform_module() -> None:
    from plugin.draw import DrawModule

    services = MagicMock()
    DrawModule().initialize(services)
    services.tools.auto_discover_package.assert_called_once_with(
        "plugin.draw", skip=frozenset({"transform"})
    )
