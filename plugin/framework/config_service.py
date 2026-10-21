# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
WriterAgent configuration service. There is no per-instance path.
"""

# crosshair: off
from __future__ import annotations

import logging
from typing import Any
from plugin.framework.deal_shim import deal

from plugin.framework.service import ServiceBase
from plugin.framework.errors import ConfigError

from plugin.framework.config import (
    get_config,
    get_config_dict,
    remove_config,
    set_config,
)
from plugin.framework.config_schema import get_config_schema

log = logging.getLogger(__name__)


class ConfigAccessError(ConfigError):
    """Raised when a module tries to access a private config key."""

    code: str = "CONFIG_ACCESS_ERROR"


class ConfigService(ServiceBase):
    name: str | None = "config"

    def get(self, key: str, default: Any = None, caller_module: str | None = None) -> Any:
        """Get a config value, fallback to defaults."""
        self._check_read_access(key, caller_module)
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

    def set(self, key: str, value: Any, caller_module: str | None = None) -> None:
        """Set a config value."""
        self._check_write_access(key, caller_module)
        set_config(key, value)

    def remove(self, key: str, caller_module: str | None = None) -> None:
        """Reset a config key."""
        self._check_write_access(key, caller_module)
        remove_config(key)

    def get_dict(self) -> dict[str, Any]:
        """Return all config."""
        return get_config_dict()

    def _caller_blocked(self, key: str, caller_module: str | None) -> str | None:
        if caller_module is None or "." not in key:
            return None
        module = key.split(".", 1)[0]
        if module == caller_module:
            return None
        return module

    @deal.raises(ConfigAccessError)
    def _check_read_access(self, key: str, caller_module: str | None) -> None:
        if self._caller_blocked(key, caller_module) is None:
            return
        schema = get_config_schema(key) or {}
        if not schema.get("public", False):
            raise ConfigAccessError(f"Module '{caller_module}' cannot read private config '{key}'")

    @deal.raises(ConfigAccessError)
    def _check_write_access(self, key: str, caller_module: str | None) -> None:
        if self._caller_blocked(key, caller_module) is not None:
            raise ConfigAccessError(f"Module '{caller_module}' cannot write to '{key}'")

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
        return self._config.get(self._qualify(key), default, caller_module=self._module)

    def set(self, key: str, value: Any) -> None:
        self._config.set(self._qualify(key), value, caller_module=self._module)

    def remove(self, key: str) -> None:
        self._config.remove(self._qualify(key), caller_module=self._module)
