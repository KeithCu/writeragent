# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared module.yaml → settings field-spec builders (WriterAgent + LibrePy)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from plugin.chatbot.dialogs import (
    get_checkbox_state,
    get_control_text,
    is_checkbox_control,
    set_checkbox_state,
    set_control_text,
)
from plugin.framework.config import get_config, set_configs
from plugin.framework.config_schema import as_bool
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

        services = None
        try:
            from plugin.main import get_services

            services = get_services()
        except ImportError:
            # LibrePy does not ship plugin.main; providers that need services skip.
            pass
        options = func(services)
        log.debug("call_options_provider success: %s options returned", len(options))
        return options
    except Exception as e:
        log.exception("call_options_provider failed for %s", provider_path)
        from plugin.framework.errors import ConfigError

        raise ConfigError(f"Options provider {provider_path} failed: {e}") from e


def _display_label_for_stored_value(opts: Any, val: Any) -> Any:
    """Map a stored option value to its label when the list uses value/label dicts.

    Compare case-insensitively: Piper ids such as ``en_US-lessac-medium`` do not
    match a lowercased stored value against the raw option value.
    """
    if not isinstance(opts, list) or not opts or not isinstance(opts[0], dict):
        return val
    stored = str(val).strip().lower()
    for opt in opts:
        if isinstance(opt, dict) and str(opt.get("value", "")).strip().lower() == stored:
            return _(str(opt.get("label", val)))
    return val


def _resolve_field_options(schema: dict[str, Any], config_key: str, ctx: Any | None) -> Any:
    """Prefer ``options_provider``; yaml ``options`` stay the fallback stub."""
    provider_path = schema.get("options_provider")
    if provider_path and isinstance(provider_path, str) and ctx is not None:
        try:
            provided = call_options_provider(ctx, provider_path)
            if isinstance(provided, list) and provided:
                return provided
        except Exception:
            log.exception("options_provider failed for %s", config_key)
    if schema.get("options"):
        return schema["options"]
    return None


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
        if schema.get("internal") or schema.get("widget") in ("list_detail", "separator"):
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
        # Provider options (when present) drive the dropdown; yaml options are the fallback.
        resolved_opts = _resolve_field_options(schema, config_key, ctx)
        val = _display_label_for_stored_value(resolved_opts, val)

        field: dict[str, Any] = {"name": ctrl_id, "config_key": config_key, "value": str(val)}
        if resolved_opts:
            field["options"] = resolved_opts

        schema_type = schema.get("type", "string")
        if schema_type == "boolean":
            schema_type = "bool"
        if schema_type in ("bool", "int", "float"):
            field["type"] = str(schema_type)
            if schema_type == "bool":
                field["value"] = "true" if as_bool(val) else "false"

        field_specs.append(field)
    return field_specs


def _settings_option_labels(field: dict[str, Any]) -> tuple[str, ...]:
    opts = field.get("options")
    if not isinstance(opts, list):
        return ()
    labels: list[str] = []
    for opt in opts:
        if isinstance(opt, dict):
            raw = opt.get("label", opt.get("value", ""))
            labels.append(_(str(raw)))
        elif opt is not None:
            labels.append(_(str(opt)))
    return tuple(labels)


def populate_settings_control(ctrl: Any, field: dict[str, Any]) -> None:
    """Fill one settings control from a field spec.

    Checkboxes use state 0/1. int/float specs use ``setValue`` when the control
    has it: a numeric edit also inherits ``setText``, but the live value is
    ``setValue``. Option lists are ``StringItemList`` on the model.
    """
    if not ctrl:
        return
    field_type = str(field.get("type") or "")
    if is_checkbox_control(ctrl):
        set_checkbox_state(ctrl, 1 if as_bool(field.get("value")) else 0)
        return
    if "options" in field:
        try:
            labels = _settings_option_labels(field)
            model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
            if labels and model is not None and hasattr(model, "StringItemList"):
                model.StringItemList = labels
        except Exception:
            log.debug("set options failed for %s", field.get("name"), exc_info=True)
    if field_type in ("int", "float") and hasattr(ctrl, "setValue"):
        try:
            ctrl.setValue(float(field["value"]))
            return
        except Exception:
            log.debug("setValue failed for %s", field.get("name"), exc_info=True)
    if hasattr(ctrl, "setText"):
        ctrl.setText(str(field.get("value", "")))
        return
    set_control_text(ctrl, str(field.get("value", "")))


def read_settings_control(ctrl: Any, field: dict[str, Any] | None = None) -> Any:
    """Read one settings control. ``None`` means the control is missing.

    Missing controls are skipped (callers must not persist ``""``).
    Checkboxes are bool (WriterAgent). LibrePy maps that to ``\"true\"`` /
    ``\"false\"``. int/float specs prefer ``getValue`` when the control has
    it. ``getText`` on a spin button is the stale UnoControlEdit caption
    and overwrites the live value.
    """
    if not ctrl:
        return None
    if is_checkbox_control(ctrl):
        return get_checkbox_state(ctrl) == 1
    field_type = str((field or {}).get("type") or "")
    if field_type in ("int", "float") and hasattr(ctrl, "getValue"):
        try:
            return ctrl.getValue()
        except Exception:
            log.debug("getValue failed for %s", (field or {}).get("name"), exc_info=True)
    if hasattr(ctrl, "getText"):
        return ctrl.getText()
    return get_control_text(ctrl)


def stored_select_value(val: Any, options: Any) -> Any:
    """Map a combo's visible text back to the option value.

    Map the visible caption back to the option value. ``getText()`` is
    the translated label; matching only the English spec label leaves
    "Shared kernel" and Dutch "Uit" as captions instead of ``shared`` /
    ``off``. A plain string option is its own id; the combo shows
    ``_(that string)``.
    """
    if not isinstance(options, list):
        return val
    for opt in options:
        if isinstance(opt, dict):
            label = opt.get("label")
            value = opt.get("value", val)
            if val == value or val == label:
                return value
            if isinstance(label, str) and label and val == _(label):
                return value
            continue
        if opt is None:
            continue
        raw = str(opt)
        if val == raw or (raw and val == _(raw)):
            return raw
    return val


def changed_config_values(pending: dict[str, Any], read: Any) -> dict[str, Any]:
    """Keys whose value is not already on disk.

    A value equal to the stored value, or to the schema default when the
    key is omitted, is not a change. The default itself must not become a
    write. ``read`` is ``get_config`` from the caller so a test patch of
    that name is the disk this compare sees. This does not touch the config
    lock or the per-key writer; the caller makes one ``set_configs`` of
    what remains.
    """
    from plugin.framework.config_schema import coerce_config_value
    from plugin.framework.errors import ConfigError, ConfigValidationError

    changed: dict[str, Any] = {}
    for key, value in pending.items():
        try:
            current = read(key)
        except ConfigError:
            changed[key] = value
            continue
        if _config_assignment_matches(key, value, current, coerce_config_value, ConfigError, ConfigValidationError):
            continue
        changed[key] = value
    return changed


def _config_assignment_matches(
    key: str,
    proposed: Any,
    current: Any,
    coerce_config_value: Any,
    config_error: type[BaseException],
    validation_error: type[BaseException],
) -> bool:
    """True when *proposed* would not change *current* after the same coerce OK uses.

    An invalid value does not match, so ``set_configs`` still raises it.
    Endpoint text may be a preset label; both sides go through the selector
    normalizer before compare. Dict values (the API-key map) compare equal
    as maps, not as rewritten blobs.
    """
    if key == "endpoint":
        # config_ui_helpers is WriterAgent-only. LibrePy ships this module
        # and must still import when that helper is absent.
        try:
            from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
        except (ImportError, ModuleNotFoundError):
            return str(proposed or "").strip() == str(current or "").strip()
        return endpoint_from_selector_text(str(proposed or "")) == endpoint_from_selector_text(str(current or ""))
    if isinstance(proposed, dict) or isinstance(current, dict):
        return proposed == current
    try:
        coerced = coerce_config_value(key, proposed, fallback_value=current, strict=True)
    except (config_error, validation_error):
        return False
    return coerced == current


def apply_field_specs_result(ctx: Any, result: dict[str, Any], field_specs: list[dict[str, Any]]) -> None:
    """Persist dialog values using each spec's ``config_key`` (or name with ``__`` → ``.``).

    ``set_configs`` writes once and emits only when something changed, so
    this function must not emit. Per-key ``set_config`` rewrites the file
    and emits ``config:changed`` again, including when every value already
    matched.
    """
    del ctx  # set_configs emits with the main-thread ctx; a second emit refreshed the sidebar.
    by_name = {f["name"]: f for f in field_specs}
    pending: dict[str, Any] = {}
    for key, val in result.items():
        spec = by_name.get(key)
        if spec is None:
            continue
        save_key = str(spec.get("config_key") or key.replace("__", "."))
        pending[save_key] = stored_select_value(val, spec.get("options"))
    # One write, and only keys that differ from disk or from an omitted default.
    pending = changed_config_values(pending, get_config)
    if pending:
        set_configs(pending)
