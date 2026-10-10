# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
UNO Service implementation for WriterAgent configuration.
"""

# crosshair: off
from __future__ import annotations

import os
import logging
from typing import Any, Callable, cast
from plugin.framework.deal_shim import deal

from plugin.framework.service import ServiceBase
from plugin.framework.event_bus import global_event_bus
from plugin.framework.errors import ConfigError, ConfigValidationError

from plugin.framework.config import get_config, set_config, remove_config, get_config_dict, get_current_endpoint, set_api_key_for_endpoint, parse_config_json_text, _config_store, AI_SIMPLE_FIELDS
from plugin.framework.config_schema import get_manifest_modules

# get_stt_model / set_image_model / set_text_model stay inside the ai.* branches.
# Importing them here pulls model_fetcher whenever ConfigService is imported.
# LibrePy must not gain that edge, and model_fetcher must not import
# ConfigService the other way.

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

    code: str = "CONFIG_ACCESS_ERROR"


def _dummy_impl(name: str, services: Any = ()) -> Any:
    def decorator(cls: type[Any]) -> Any:
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


def _modules_to_manifest(modules: Any) -> dict[str, Any]:
    """Turn ``get_manifest_modules()`` into the dict ``set_manifest`` already walks.

    The manifest list is ``{"name", "config"}`` records. ``set_manifest`` expects
    ``{name: {"config": {field: schema}}}``.
    """
    manifest: dict[str, Any] = {}
    if not isinstance(modules, list):
        return manifest
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        name = mod.get("name")
        if not isinstance(name, str) or not name:
            continue
        config = mod.get("config")
        if not isinstance(config, dict):
            config = {}
        manifest[name] = {"config": config}
    return manifest


@_implementation("org.extension.writeragent.ConfigService")
class ConfigService(ServiceBase):
    name: str | None = "config"

    # Declared so mypy can type initialize/set_events/get after those methods
    # gained annotations (same adjacent-field pattern as plugin/mcp).
    _defaults: dict[str, Any]
    _manifest: dict[str, Any]
    _events: Any
    _config_path: str | None

    def __init__(self) -> None:
        self._defaults = {}  # "module.key" -> default_value
        self._manifest = {}  # "module.key" -> field schema
        self._events = None  # EventBus, set after init
        self._config_path = None  # For testing

    def initialize(self, ctx: Any) -> None:
        """Load module.yaml defaults and public flags once.

        Bootstrap registers this service and set_events, not set_manifest.
        Build the dict set_manifest already expects from get_manifest_modules()
        or _defaults and _manifest stay empty and module.yaml public flags
        never apply. ctx is not used for I/O; init_config already ran.
        """
        del ctx
        if self._manifest:
            return
        self.set_manifest(_modules_to_manifest(get_manifest_modules()))

    def set_events(self, events: Any) -> None:
        """Wire the event bus."""
        self._events = events

    def set_manifest(self, manifest: Any) -> None:
        """Load config schemas from the merged manifest."""
        for mod_name, mod_data in manifest.items():
            for field_name, schema in mod_data.get("config", {}).items():
                full_key = f"{mod_name}.{field_name}"
                self._defaults[full_key] = schema.get("default")
                self._manifest[full_key] = schema

    def register_default(self, key: str, default: Any) -> None:
        """Register a single default value."""
        self._defaults[key] = default

    def get(self, key: str, default: Any = None, caller_module: str | None = None) -> Any:
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
                if field == "stt_model":
                    from plugin.framework.client.model_fetcher import get_stt_model

                    return get_stt_model()

                return get_config(field)

        # Test fallback
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = parse_config_json_text(f.read())
                    if data is None:
                        raise ConfigError("Config file must be a JSON object")
                    if key in data:
                        return data[key]
            except OSError as e:
                log.debug("ConfigService.get IO error for %s: %s", self._config_path, e)
            except ConfigError as e:
                log.debug("ConfigService.get ConfigError: %s", e)

        try:
            val = get_config(key)
            # Only None is missing. A stored empty string, False, and 0 are
            # real values; treating "" as missing returned the caller default.
            if val is not None:
                return val
        except ConfigError:
            val = None

        # A caller-supplied default wins over a registered None default.
        # Otherwise get("mcp.tool_exposure_mode", "delegate") returned None
        # when the key was registered with default None.
        if default is not None:
            return default
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value: Any, caller_module: str | None = None) -> None:
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
                # set_api_key_for_endpoint emits config:changed. Do not emit again.
                set_api_key_for_endpoint(endpoint, value or "", event_key=key)
                return

            if field in AI_SIMPLE_FIELDS:
                if field == "endpoint":
                    from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text

                    # str(None) is the literal "None". That is not an endpoint,
                    # and set_config would store it. An empty string fails below.
                    endpoint_text = "" if value is None else str(value)
                    resolved = endpoint_from_selector_text(endpoint_text)
                    # An empty resolve is a failure. Returning without writing
                    # looks like success to the caller.
                    if not resolved:
                        raise ConfigError("Endpoint text did not resolve to a URL", "CONFIG_INVALID_ENDPOINT", details={"value": value})
                    set_config("endpoint", resolved, event_key=key)
                elif field == "image_model":
                    from plugin.framework.client.model_fetcher import set_image_model

                    set_image_model(value or "", update_lru=True, event_key=key)
                elif field == "text_model":
                    from plugin.framework.client.model_fetcher import set_text_model

                    set_text_model(value or "", update_lru=True, event_key=key)
                elif field == "stt_model":
                    # Speech tab canonical key. Do not write legacy stt_model.
                    set_config("audio.stt_model", value, event_key=key)
                else:
                    # Direct 1:1 mapping to top-level key.
                    set_config(field, value, event_key=key)
                return

        # Test fallback. The store patches this file under the same lock as
        # production set_config, so a second writer cannot drop keys the way
        # a load-the-whole-JSON-and-write-it-back did. emit=False because
        # this method emits the one event below (old_value is the manifest
        # default, not the store's missing-key None).
        if self._config_path:

            def _replace(_current: Any) -> Any:
                return value

            try:
                changed = _config_store.apply(
                    self._config_path,
                    [(key, _replace)],
                    emit=False,
                    fail_on_unrepairable=False,
                )
            except ConfigValidationError:
                raise
            except ConfigError:
                raise

            if changed:
                bus = self._events or global_event_bus
                bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=None)
            return

        # set_config emits the one config:changed for this write.
        set_config(key, value)

    def remove(self, key: str, caller_module: str | None = None) -> None:
        """Reset a config key."""
        self._check_write_access(key, caller_module)
        if self._config_path:
            # Same store as production remove_config. emit=False so this
            # method emits the one event (test files have no UNO ctx).
            try:
                changed = _config_store.remove(self._config_path, key, emit=False)
            except ConfigError as e:
                log.warning("ConfigService.remove config file error for key %s: %s", key, e)
                return
            if changed:
                bus = self._events or global_event_bus
                bus.emit("config:changed", key=key, value=None, old_value=None, ctx=None)
        else:
            remove_config(key)

    def get_dict(self) -> dict[str, Any]:
        """Return all config."""
        # This is a simplification for now
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = parse_config_json_text(f.read())
                if not isinstance(data, dict):
                    return {}
                return data
            except OSError as e:
                log.debug("ConfigService.get_dict config file read error: %s", e)
                return {}
        return get_config_dict()

    @deal.raises(ConfigAccessError)
    def _check_read_access(self, key: str, caller_module: str | None) -> None:
        if caller_module is None or "." not in key:
            return
        module = key.split(".", 1)[0]
        if module == caller_module:
            return
        schema = self._manifest.get(key, {})
        if not schema.get("public", False):
            raise ConfigAccessError(f"Module '{caller_module}' cannot read private config '{key}'")

    @deal.raises(ConfigAccessError)
    def _check_write_access(self, key: str, caller_module: str | None) -> None:
        if caller_module is None or "." not in key:
            return
        module = key.split(".", 1)[0]
        if module != caller_module:
            raise ConfigAccessError(f"Module '{caller_module}' cannot write to '{key}'")

    def proxy_for(self, module_name: str) -> ModuleConfigProxy:
        return ModuleConfigProxy(self, module_name)


class ModuleConfigProxy:
    _config: ConfigService
    _module: str

    def __init__(self, config_service: ConfigService, module_name: str) -> None:
        self._config = config_service
        self._module = module_name

    def get(self, key: str, default: Any = None) -> Any:
        if "." not in key:
            key = f"{self._module}.{key}"
        return self._config.get(key, default, caller_module=self._module)

    def set(self, key: str, value: Any) -> None:
        if "." not in key:
            key = f"{self._module}.{key}"
        self._config.set(key, value, caller_module=self._module)

    def remove(self, key: str) -> None:
        if "." not in key:
            key = f"{self._module}.{key}"
        self._config.remove(key, caller_module=self._module)
