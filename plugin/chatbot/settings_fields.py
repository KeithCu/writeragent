# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared module.yaml → settings field-spec builders (WriterAgent + LibrePy)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from plugin.framework.config import as_bool, get_config, set_config
from plugin.framework.event_bus import global_event_bus
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

ControlIdStyle = Literal["flat", "prefixed"]


def find_module_manifest(module_name: str) -> dict[str, Any] | None:
    """Return the MODULES entry for *module_name*, or None if missing."""
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return None
    for m in MODULES:
        if not isinstance(m, dict):
            continue
        if str(m.get("name", "")) == module_name:
            return m
    return None


def call_options_provider(ctx: Any, provider_path: str) -> Any:
    """Import a module and call a function to get options (``module:func`` path)."""
    log.debug("call_options_provider: %s", provider_path)
    try:
        module_path, func_name = provider_path.rsplit(":", 1)
        import importlib

        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)

        from plugin.main import get_services

        services = get_services()
        options = func(services)
        log.debug("call_options_provider success: %s options returned", len(options))
        return options
    except Exception as e:
        log.error("call_options_provider FAILED for %s: %s", provider_path, e)
        import traceback

        log.error(traceback.format_exc())
        from plugin.framework.errors import ConfigError

        raise ConfigError(f"Options provider {provider_path} failed: {e}") from e


def build_module_field_specs(
    module_name: str,
    *,
    ctx: Any | None = None,
    control_ids: ControlIdStyle = "flat",
    skip_librepy_exclude: bool = False,
) -> list[dict[str, Any]]:
    """Build load/save field specs for one module's ``config`` block in module.yaml."""
    manifest = find_module_manifest(module_name)
    if not manifest:
        return []

    field_specs: list[dict[str, Any]] = []
    m_config = manifest.get("config") or {}
    if not isinstance(m_config, dict):
        return field_specs

    prefix = module_name.replace(".", "_")
    for field_name, schema in m_config.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("internal") or schema.get("widget") == "list_detail":
            continue
        # Action-only controls (e.g. Test) exist in XDL but are not load/save fields.
        if schema.get("settings_persist") is False:
            continue
        if skip_librepy_exclude and schema.get("librepy_exclude"):
            continue

        config_key = f"{module_name}.{field_name}"
        if control_ids == "prefixed":
            ctrl_id = f"{prefix}__{field_name}"
        else:
            ctrl_id = field_name

        val = get_config(config_key)
        opts = schema.get("options", [])

        # For select/combo with value/label options, use label for display so dropdown shows correctly.
        if isinstance(opts, list) and opts and isinstance(opts[0], dict):
            v_str = str(val).strip().lower()
            for opt in opts:
                if isinstance(opt, dict) and str(opt.get("value", "")) == v_str:
                    val = _(str(opt.get("label", val)))
                    break

        field: dict[str, Any] = {"name": ctrl_id, "config_key": config_key, "value": str(val)}

        provider_path = schema.get("options_provider")
        if provider_path and isinstance(provider_path, str) and ctx is not None:
            try:
                field["options"] = call_options_provider(ctx, provider_path)
            except Exception:
                log.exception("options_provider failed for %s", config_key)
        elif schema.get("options"):
            field["options"] = schema["options"]

        schema_type = schema.get("type", "string")
        if schema_type == "boolean":
            schema_type = "bool"
        if schema_type in ("bool", "int", "float"):
            field["type"] = str(schema_type)
            if schema_type == "bool":
                field["value"] = "true" if as_bool(val) else "false"

        field_specs.append(field)
    return field_specs


def apply_field_specs_result(ctx: Any, result: dict[str, Any], field_specs: list[dict[str, Any]]) -> None:
    """Persist dialog values using each spec's ``config_key`` (or name with ``__`` → ``.``)."""
    by_name = {f["name"]: f for f in field_specs}
    for key, val in result.items():
        spec = by_name.get(key)
        if spec is None:
            continue
        save_key = str(spec.get("config_key") or key.replace("__", "."))
        set_config(save_key, val)
    global_event_bus.emit("config:changed", ctx=ctx)
