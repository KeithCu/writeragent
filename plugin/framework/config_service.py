# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
UNO Service implementation for WriterAgent configuration.
"""
import json
import os
import logging
from typing import Any, Callable, cast

from plugin.framework.service import ServiceBase
from plugin.framework.event_bus import global_event_bus
from plugin.framework.uno_context import get_ctx
from plugin.framework.errors import ConfigError

from plugin.framework.config import (
    get_config,
    set_config,
    remove_config,
    get_config_dict,
    get_current_endpoint,
    set_api_key_for_endpoint,
    _load_config_dict,
    _write_config_file,
    _emit_config_changed_ctx,
    AI_SIMPLE_FIELDS,
)
from plugin.framework.client.model_fetcher import set_image_model

_unohelper_mod: Any
try:
    import unohelper as _unohelper_impl
    _unohelper_mod = _unohelper_impl
except ImportError:
    _unohelper_mod = None
unohelper: Any = _unohelper_mod


log = logging.getLogger(__name__)

class ConfigAccessError(ConfigError):
    """Raised when a module tries to access a private config key."""

    def __init__(self, message, code="CONFIG_ACCESS_ERROR", context=None):
        super().__init__(message, code=code, context=context)

def _dummy_impl(name, services=()):
    def decorator(cls):
        return cls

    return decorator

def _uno_service_implementation_decorator() -> Callable[..., Any]:
    """Return UNO's ``unohelper.implementation`` or a no-op when unohelper is mocked.

    Headless pytest loads ``conftest`` before ``plugin.framework.config``; ``unohelper``
    may be a ``MagicMock``. That exposes a fake ``implementation`` callable, which is
    not LibreOffice's registration helper and breaks ``ConfigService()`` if used as a
    class decorator (tests would get nested mocks instead of the real class).
    """
    if not unohelper:
        return _dummy_impl
    impl = getattr(unohelper, "implementation", None)
    if impl is None:
        return _dummy_impl
    try:
        from unittest.mock import Mock

        if isinstance(impl, Mock):
            return _dummy_impl
    except ImportError:
        pass
    if not callable(impl):
        return _dummy_impl
    return cast("Callable[..., Any]", impl)

_implementation: Callable[..., Any] = _uno_service_implementation_decorator()

@_implementation("org.extension.writeragent.ConfigService")
class ConfigService(ServiceBase):
    name = "config"

    def __init__(self):
        self._defaults = {}  # "module.key" -> default_value
        self._manifest = {}  # "module.key" -> field schema
        self._events = None  # EventBus, set after init
        self._config_path = None  # For testing

    def initialize(self, ctx):
        pass

    def set_events(self, events):
        """Wire the event bus."""
        self._events = events

    def set_manifest(self, manifest):
        """Load config schemas from the merged manifest."""
        for mod_name, mod_data in manifest.items():
            for field_name, schema in mod_data.get("config", {}).items():
                full_key = f"{mod_name}.{field_name}"
                self._defaults[full_key] = schema.get("default")
                self._manifest[full_key] = schema

    def register_default(self, key, default):
        """Register a single default value."""
        self._defaults[key] = default

    def get(self, key, default=None, caller_module=None):
        """Get a config value, fallback to defaults."""
        self._check_read_access(key, caller_module)

        # Simple mapping: ai.<field> keys from the AI Options page should read
        # from the corresponding top-level settings so Tools → Options and the
        # legacy Settings dialog stay in sync.
        if key.startswith("ai."):
            field = key.split(".", 1)[1]

            # Internal mappings for missing AI_SIMPLE_FIELDS mapping if needed
            if field == "api_key":
                endpoint = get_current_endpoint()
                from plugin.framework.config import get_api_key_for_endpoint
                return str(get_api_key_for_endpoint(endpoint) or "")

            if field in AI_SIMPLE_FIELDS:
                if field == "endpoint":
                    return str(get_config("endpoint") or "").strip()

                return get_config(field)

        # Test fallback
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        raise ConfigError("Config file must be a JSON object")
                    if key in data:
                        return data[key]
            except json.JSONDecodeError as e:
                log.debug("ConfigService.get invalid JSON in %s: %s", self._config_path, e)
            except OSError as e:
                log.debug("ConfigService.get IO error for %s: %s", self._config_path, e)
            except ConfigError as e:
                log.debug("ConfigService.get ConfigError: %s", e)

        try:
            val = get_config(key)
            if val is not None and val != "":
                return val
        except ConfigError:
            pass

        if key not in self._defaults:
            return default
        return self._defaults[key]

    def set(self, key, value, caller_module=None):
        """Set a config value."""
        self._check_write_access(key, caller_module)
        old_value = self.get(key)

        # Simple mapping: ai.<field> keys from the AI Options page should write
        # into the corresponding top-level settings (endpoint, model, etc.).
        if key.startswith("ai."):
            field = key.split(".", 1)[1]

            # Internal mappings for keys missing from AI_SIMPLE_FIELDS if they map to methods
            if field == "api_key":
                endpoint = get_current_endpoint()
                set_api_key_for_endpoint(endpoint, value or "")
                if value != old_value:
                    bus = self._events or global_event_bus
                    bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=_emit_config_changed_ctx())
                return

            if field in AI_SIMPLE_FIELDS:
                if field == "endpoint":
                    from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
                    resolved = endpoint_from_selector_text(str(value))
                    if resolved:
                        set_config("endpoint", resolved)
                elif field == "image_model":
                    set_image_model(value or "", update_lru=True)
                else:
                    # Direct 1:1 mapping to top-level key.
                    set_config(field, value)

                if value != old_value:
                    bus = self._events or global_event_bus
                    bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=_emit_config_changed_ctx())
                return

        # Test fallback
        if self._config_path:
            if os.path.exists(self._config_path):
                data = _load_config_dict(self._config_path, allow_repair=True, persist_repair=False)
            else:
                data = {}
            data[key] = value
            try:
                _write_config_file(self._config_path, data)
            except OSError as e:
                log.error("ConfigService.set config file save error: %s", e)

            ctx = None  # No UNO context in file-based test mode
        else:
            set_config(key, value)
            ctx = _emit_config_changed_ctx()

        if value != old_value:
            bus = self._events or global_event_bus
            bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=ctx)

    def set_batch(self, changes, old_values=None):
        """Set multiple config values at once. Returns dict of changed keys.

        Used by the generic Options handler; delegates to set() so that
        ai.<field> keys are also mapped through the simple AI settings layer.
        """
        diffs = {}
        for key, value in (changes or {}).items():
            before = self.get(key)
            if value == before:
                continue
            self.set(key, value)
            diffs[key] = (before, value)
        return diffs

    def remove(self, key, caller_module=None):
        """Reset a config key."""
        self._check_write_access(key, caller_module)
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and key in data:
                    del data[key]
                    with open(self._config_path, "w", encoding="utf-8") as f:
                        json.dump(data, f)
            except (OSError, json.JSONDecodeError) as e:
                log.warning("ConfigService.remove config file error for key %s: %s", key, e)
        else:
            remove_config(key)

    def get_dict(self):
        """Return all config."""
        # This is a simplification for now
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return {}
                return data
            except (OSError, json.JSONDecodeError) as e:
                log.debug("ConfigService.get_dict config file read error: %s", e)
                return {}
        return get_config_dict()

    def _check_read_access(self, key, caller_module):
        if caller_module is None or "." not in key:
            return
        module = key.split(".", 1)[0]
        if module == caller_module:
            return
        schema = self._manifest.get(key, {})
        if not schema.get("public", False):
            raise ConfigAccessError(f"Module '{caller_module}' cannot read private config '{key}'")

    def _check_write_access(self, key, caller_module):
        if caller_module is None or "." not in key:
            return
        module = key.split(".", 1)[0]
        if module != caller_module:
            raise ConfigAccessError(f"Module '{caller_module}' cannot write to '{key}'")

    def proxy_for(self, module_name):
        return ModuleConfigProxy(self, module_name)


class ModuleConfigProxy:
    def __init__(self, config_service, module_name):
        self._config = config_service
        self._module = module_name

    def get(self, key, default=None):
        if "." not in key:
            key = f"{self._module}.{key}"
        return self._config.get(key, default, caller_module=self._module)

    def set(self, key, value):
        if "." not in key:
            key = f"{self._module}.{key}"
        self._config.set(key, value, caller_module=self._module)

    def remove(self, key):
        if "." not in key:
            key = f"{self._module}.{key}"
        self._config.remove(key, caller_module=self._module)
