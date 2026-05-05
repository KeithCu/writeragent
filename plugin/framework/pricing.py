# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import json
import logging
import os
import time
from plugin.modules.http.requests import sync_request
from plugin.framework.config import user_config_dir

log = logging.getLogger(__name__)

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
            log.debug("Using cached OpenRouter pricing.")
            return

    log.info("Fetching fresh OpenRouter pricing...")
    url = "https://openrouter.ai/api/v1/models"
    try:
        data = sync_request(url, parse_json=True)
        if data and "data" in data:
            if not cache_path:
                return
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data["data"], f, indent=2)
            log.info(f"Cached {len(data['data'])} models.")
    except (OSError, ValueError, TypeError) as e:
        log.error(f"Failed to fetch OpenRouter pricing (IO/Parse error): {e}")
    except Exception as e:
        from plugin.framework.errors import NetworkError

        if isinstance(e, NetworkError):
            log.error(f"Failed to fetch OpenRouter pricing (NetworkError): {e}")
        else:
            log.error(f"Failed to fetch OpenRouter pricing (unexpected): {e}")


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
