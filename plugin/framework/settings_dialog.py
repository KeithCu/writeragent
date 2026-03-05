from plugin.modules.core.services.config import get_config, set_config, get_current_endpoint, as_bool, populate_combobox_with_lru, populate_endpoint_selector, endpoint_from_selector_text, get_image_model, set_image_model, get_api_key_for_endpoint, set_api_key_for_endpoint, populate_image_model_selector, notify_config_changed

def get_settings_field_specs(ctx):
    """Return field specs for Settings dialog (single source for dialog and apply keys)."""
    openai_compatibility_value = "true" if as_bool(get_config(ctx, "openai_compatibility", True)) else "false"
    is_openwebui_value = "true" if as_bool(get_config(ctx, "is_openwebui", False)) else "false"
    current_endpoint_for_specs = get_current_endpoint(ctx)
    field_specs = [
        {"name": "endpoint", "value": str(get_config(ctx, "endpoint", "http://127.0.0.1:5000"))},
        {"name": "text_model", "value": str(get_config(ctx, "text_model", "") or get_config(ctx, "model", ""))},
        {"name": "image_model", "value": str(get_image_model(ctx))},
        {"name": "api_key", "value": str(get_api_key_for_endpoint(ctx, current_endpoint_for_specs))},
        {"name": "api_type", "value": str(get_config(ctx, "api_type", "chat"))},
        {"name": "is_openwebui", "value": is_openwebui_value, "type": "bool"},
        {"name": "openai_compatibility", "value": openai_compatibility_value, "type": "bool"},
        {"name": "temperature", "value": str(get_config(ctx, "temperature", "0.5")), "type": "float"},
        {"name": "seed", "value": str(get_config(ctx, "seed", ""))},
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
                schema_type = schema.get("type", "string")
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
    from plugin.modules.core.services.config import update_lru_history
    # Keys to set directly from result; derived from dialog field specs (exclude specially handled ones)
    _apply_skip = ("endpoint", "api_key", "use_aihorde", "api_type", "mcp_port")
    apply_keys = [f["name"] for f in get_settings_field_specs(ctx) if f["name"] not in _apply_skip]

    # Resolve endpoint first so LRU updates use the endpoint being saved
    effective_endpoint = endpoint_from_selector_text(result.get("endpoint", "")) if "endpoint" in result else get_current_endpoint(ctx)
    if "endpoint" in result and effective_endpoint:
        set_config(ctx, "endpoint", effective_endpoint)
    current_endpoint = effective_endpoint or get_current_endpoint(ctx)

    # Set keys from result (endpoint, api_key, use_aihorde, api_type, mcp_port handled below)
    for key in apply_keys:
        if key in result:
            val = result[key]
            
            # Map module__field to module.field for saving in JSON
            save_key = key.replace("__", ".")
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
    
    if "api_type" in result:
        api_type_value = str(result["api_type"]).strip().lower()
        if api_type_value not in ("chat", "completions"):
            api_type_value = "completions"
        set_config(ctx, "api_type", api_type_value)

    if "mcp_port" in result:
        try:
            port = int(result["mcp_port"])
            if 1 <= port <= 65535:
                set_config(ctx, "mcp_port", port)
        except (TypeError, ValueError):
            pass

    if "api_key" in result:
        set_api_key_for_endpoint(ctx, current_endpoint, result["api_key"])

    notify_config_changed(ctx)
