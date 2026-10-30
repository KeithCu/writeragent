# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
WriterAgent configuration service. There is no per-instance path.
"""

# crosshair: off
from __future__ import annotations

from typing import Any

from plugin.framework.service import ServiceBase
from plugin.framework.errors import ConfigError

from plugin.framework.config import (
    get_config,
    get_config_dict,
    set_config,
)
from plugin.framework.config_schema import get_config_schema


class ConfigService(ServiceBase):
    name: str | None = "config"

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value, fallback to defaults."""
        try:
            val = get_config(key)
        except ConfigError:
            val = None
        if val is not None:
            return val
        if default is not None:
            return default
        schema = get_config_schema(key) or {}
        if "default" in schema:
            return schema["default"]
        return default

    def set(self, key: str, value: Any) -> None:
        """Set a config value."""
        set_config(key, value)

    def get_dict(self) -> dict[str, Any]:
        """Return all config."""
        return get_config_dict()

    def proxy_for(self, module_name: str) -> ModuleConfigProxy:
        return ModuleConfigProxy(self, module_name)


class ModuleConfigProxy:
    _config: ConfigService
    _module: str

    def __init__(self, config_service: ConfigService, module_name: str) -> None:
        self._config = config_service
        self._module = module_name

    def _qualify(self, key: str) -> str:
        return key if "." in key else f"{self._module}.{key}"

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(self._qualify(key), default)

    def set(self, key: str, value: Any) -> None:
        self._config.set(self._qualify(key), value)
