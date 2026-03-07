from plugin.framework.config import get_config, set_config, get_current_endpoint, as_bool, endpoint_from_selector_text, get_image_model, set_image_model, get_api_key_for_endpoint, set_api_key_for_endpoint, notify_config_changed

def get_settings_field_specs(ctx):
    """Return field specs for Settings dialog (single source for dialog and apply keys)."""
    from plugin.framework.logging import debug_log
    debug_log("get_settings_field_specs entry", context="Settings")
    current_endpoint_for_specs = get_current_endpoint(ctx)
    field_specs = [
        {"name": "endpoint", "value": str(get_config(ctx, "endpoint", "http://127.0.0.1:5000"))},
        {"name": "text_model", "value": str(get_config(ctx, "text_model", "") or get_config(ctx, "model", ""))},
        {"name": "image_model", "value": str(get_image_model(ctx))},
        {"name": "api_key", "value": str(get_api_key_for_endpoint(ctx, current_endpoint_for_specs))},
        {"name": "temperature", "value": str(get_config(ctx, "temperature", "-1")), "type": "float"},
        {"name": "use_aihorde", "value": "true" if get_config(ctx, "image_provider", "aihorde") == "aihorde" else "false", "type": "bool"},
        {"name": "aihorde_api_key", "value": str(get_config(ctx, "aihorde_api_key", ""))},
        {"name": "image_base_size", "value": str(get_config(ctx, "image_base_size", "512")), "type": "int"},
        {"name": "image_default_aspect", "value": str(get_config(ctx, "image_default_aspect", "Square"))},
        {"name": "image_cfg_scale", "value": str(get_config(ctx, "image_cfg_scale", "7.5")), "type": "float"},
        {"name": "image_steps", "value": str(get_config(ctx, "image_steps", "30")), "type": "int"},
        {"name": "image_nsfw", "value": "true" if as_bool(get_config(ctx, "image_nsfw", False)) else "false", "type": "bool"},
        {"name": "image_censor_nsfw", "value": "true" if as_bool(get_config(ctx, "image_censor_nsfw", True)) else "false", "type": "bool"},
        {"name": "image_max_wait", "value": str(get_config(ctx, "image_max_wait", "5")), "type": "int"},
        {"name": "image_auto_gallery", "value": "true" if as_bool(get_config(ctx, "image_auto_gallery", True)) else "false", "type": "bool"},
        {"name": "image_insert_frame", "value": "true" if as_bool(get_config(ctx, "image_insert_frame", False)) else "false", "type": "bool"},
        {"name": "image_translate_prompt", "value": "true" if as_bool(get_config(ctx, "image_translate_prompt", True)) else "false", "type": "bool"},
        {"name": "image_translate_from", "value": str(get_config(ctx, "image_translate_from", ""))},
        {"name": "seed", "value": str(get_config(ctx, "seed", ""))},
        {"name": "show_search_thinking", "value": "true" if as_bool(get_config(ctx, "show_search_thinking", False)) else "false", "type": "bool"},
    ]

    try:
        from plugin._manifest import MODULES
        for m in MODULES:
            if m["name"] in ("main", "ai"):
                continue
            for field_name, schema in m.get("config", {}).items():
                if schema.get("internal") or schema.get("widget") == "list_detail":
                    continue
                
                prefix = m["name"].replace(".", "_")
                ctrl_id = f"{prefix}__{field_name}"
                config_key = f"{m['name']}.{field_name}"
                
                default = str(schema.get("default", ""))
                val = get_config(ctx, config_key, default)
                
                field = {"name": ctrl_id, "value": str(val)}
                
                # Resolve dynamic options if options_provider is present
                provider_path = schema.get("options_provider")
                if provider_path:
                    try:
                        field["options"] = _call_options_provider(ctx, provider_path)
                    except Exception:
                        from plugin.framework.logging import debug_log
                        debug_log(f"Failed to resolve options_provider: {provider_path}", context="Settings")

                schema_type = schema.get("type", "string")
                if schema_type == "boolean":
                    schema_type = "bool"
                if schema_type in ("bool", "int", "float"):
                    field["type"] = schema_type
                    if schema_type == "bool":
                        field["value"] = "true" if as_bool(val) else "false"
                        
                field_specs.append(field)
    except ImportError:
        pass

    return field_specs

def apply_settings_result(ctx, result):
    """Apply settings dialog result to config. Shared by Writer and Calc."""
    from plugin.framework.config import update_lru_history
    # Keys to set directly from result; derived from dialog field specs (exclude specially handled ones)
    _apply_skip = ("endpoint", "api_key", "use_aihorde")
    apply_keys = [f["name"] for f in get_settings_field_specs(ctx) if f["name"] not in _apply_skip]

    # Resolve endpoint first so LRU updates use the endpoint being saved
    effective_endpoint = endpoint_from_selector_text(result.get("endpoint", "")) if "endpoint" in result else get_current_endpoint(ctx)
    if "endpoint" in result and effective_endpoint:
        set_config(ctx, "endpoint", effective_endpoint)
    current_endpoint = effective_endpoint or get_current_endpoint(ctx)

    # Set keys from result (endpoint, api_key, use_aihorde handled below)
    for key in apply_keys:
        if key in result:
            val = result[key]
            
            # Map module__field to module.field for saving in JSON
            save_key = key.replace("__", ".")
            
            # Special validation for temperature
            if save_key == "temperature":
                try:
                    f_val = float(val)
                    if f_val > 1.0:
                        from plugin.framework.dialogs import msgbox
                        msgbox(ctx, "Invalid Setting", "Temperature must be <= 1.0")
                        continue
                    if f_val < 0:
                        val = -1.0
                except (ValueError, TypeError):
                    pass
            
            set_config(ctx, save_key, val)
            
            # Update LRU history
            if key == "text_model" and val:
                update_lru_history(ctx, val, "model_lru", current_endpoint)
            elif key == "image_model" and val:
                set_image_model(ctx, val)
            elif key == "additional_instructions" and val:
                update_lru_history(ctx, val, "prompt_lru", "")
            elif key == "image_base_size" and val:
                update_lru_history(ctx, str(val), "image_base_size_lru", "")

    # Handle provider toggle from checkbox
    if "use_aihorde" in result:
        provider = "aihorde" if result["use_aihorde"] else "endpoint"
        set_config(ctx, "image_provider", provider)

    # Update endpoint_lru when user changed endpoint (endpoint already set above)
    if "endpoint" in result and effective_endpoint:
        update_lru_history(ctx, effective_endpoint, "endpoint_lru", "")
    
    if "api_key" in result:
        set_api_key_for_endpoint(ctx, current_endpoint, result["api_key"])

    notify_config_changed(ctx)


def _call_options_provider(ctx, provider_path):
    """Import a module and call a function to get options.
    
    provider_path format: "plugin.framework.ai:get_text_instance_options"
    The function receives the ServiceRegistry as its argument.
    """
    from plugin.framework.logging import debug_log
    debug_log(f"_call_options_provider: {provider_path}", context="Settings")
    try:
        module_path, func_name = provider_path.rsplit(":", 1)
        import importlib
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        
        from plugin.main import get_services
        services = get_services()
        options = func(services)
        debug_log(f"_call_options_provider success: {len(options)} options returned", context="Settings")
        return options
    except Exception as e:
        debug_log(f"_call_options_provider FAILED for {provider_path}: {e}", context="Settings")
        import traceback
        debug_log(traceback.format_exc(), context="Settings")
        raise
