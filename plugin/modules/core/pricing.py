import os
import json
import time
from plugin.modules.http.client import sync_request
from plugin.modules.core.services.config import user_config_dir
from plugin.framework.logging import debug_log

PRICING_FILENAME = "openrouter_pricing.json"
CACHE_TTL = 86400 * 7  # 7 days

def _get_cache_path(ctx):
    config_dir = user_config_dir(ctx)
    if not config_dir:
        return None
    return os.path.join(config_dir, PRICING_FILENAME)

def fetch_openrouter_pricing(ctx, force=False):
    """Fetch all model pricing from OpenRouter and cache it locally."""
    cache_path = _get_cache_path(ctx)
    
    if not force and cache_path and os.path.exists(cache_path):
        mtime = os.path.getmtime(cache_path)
        if time.time() - mtime < CACHE_TTL:
            debug_log("Using cached OpenRouter pricing.", context="Pricing")
            return
            
    debug_log("Fetching fresh OpenRouter pricing...", context="Pricing")
    url = "https://openrouter.ai/api/v1/models"
    try:
        data = sync_request(url, parse_json=True)
        if data and "data" in data:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data["data"], f, indent=2)
            debug_log(f"Cached {len(data['data'])} models.", context="Pricing")
    except Exception as e:
        debug_log(f"Failed to fetch OpenRouter pricing: {e}", context="Pricing")

def get_model_pricing(ctx, model_id):
    """Return (prompt_rate, completion_rate) per token in USD."""
    cache_path = _get_cache_path(ctx)
    if not cache_path or not os.path.exists(cache_path):
        return None
        
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            models = json.load(f)
            
        for m in models:
            if m.get("id") == model_id:
                p = m.get("pricing", {})
                # Rates are often given per 1000 tokens or similar in some APIs, 
                # but OpenRouter /models returns USD per 1 token.
                return float(p.get("prompt", 0)), float(p.get("completion", 0))
    except (IOError, json.JSONDecodeError, ValueError):
        pass
        
    return None

def calculate_cost(ctx, usage, model_id):
    """Calculate USD cost for a turn based on usage dict and model hardware."""
    if not usage:
        return 0.0
        
    # OpenRouter often includes 'cost' directly in usage (estimated)
    if "cost" in usage:
        try:
            return float(usage["cost"])
        except (ValueError, TypeError):
            pass

    # Fallback to manual calculation
    prompt_tokens = usage.get("prompt_tokens", 0)
    # completion_tokens includes reasoning tokens on OpenRouter
    completion_tokens = usage.get("completion_tokens", 0)
    
    rates = get_model_pricing(ctx, model_id)
    if rates:
        prompt_rate, completion_rate = rates
        return (prompt_tokens * prompt_rate) + (completion_tokens * completion_rate)
    
    # Generic fallback: $1 per 1M tokens ($0.000001 per token)
    return (prompt_tokens + completion_tokens) * 0.000001
