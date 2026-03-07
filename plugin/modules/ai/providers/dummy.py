"""Homer Simpson dummy LLM provider — always answers D'oh!"""

import time
import logging

from plugin.modules.ai.provider_base import LlmProvider

log = logging.getLogger("writeragent.ai_dummy")

_RESPONSE = "D'oh!"


class HomerProvider(LlmProvider):
    """Fake LLM that streams 'D'oh!' one character at a time."""

    name = "ai_dummy"

    def __init__(self, config):
        self._config = config

    def stream(self, messages, tools=None, **kwargs):
        delay = (self._config.get("delay") or 50) / 1000.0
        for ch in _RESPONSE:
            if delay > 0:
                time.sleep(delay)
            yield {
                "content": ch,
                "thinking": "",
                "delta": {"content": ch},
                "finish_reason": None,
            }
        yield {
            "content": "",
            "thinking": "",
            "delta": {},
            "finish_reason": "stop",
        }

    def complete(self, messages, tools=None, **kwargs):
        delay = (self._config.get("delay") or 50) / 1000.0
        if delay > 0:
            time.sleep(delay * len(_RESPONSE))
        return {
            "content": _RESPONSE,
            "tool_calls": None,
            "finish_reason": "stop",
        }

    def supports_tools(self):
        return False
