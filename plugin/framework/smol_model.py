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
from typing import Any, cast

from plugin.contrib.smolagents.models import Model, ChatMessage, TokenUsage


class WriterAgentSmolModel(Model):
    """
    A wrapper that implements `smolagents.models.Model` by delegating 
    requests to WriterAgent's `LlmClient` (`core.api`).
    """
    def __init__(self, llm_client, max_tokens=1024, status_callback=None, **kwargs):
        super().__init__(**kwargs)
        self.api = llm_client
        self.max_tokens = max_tokens
        self.model_id = self.api.config.get("model", "localwriter/model")
        self._status_callback = status_callback

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        completion_kwargs = self._prepare_completion_kwargs(
            messages=cast("list[ChatMessage | dict[str, Any]]", messages),
            stop_sequences=stop_sequences,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )
        
        msg_dicts = completion_kwargs.get("messages", [])

        if self._status_callback:
            self._status_callback("Thinking...")

        # Keeps smolagents' text-based tool parsing while gaining LlmClient's stripping/shims/pacing.
        # Do not forward OpenAI ``tools`` to the HTTP layer (many local servers 500). Tool schemas still
        # live in the smol system prompt via ``__TOOLS_LIST__``. Developers may replace ``None`` with
        # ``completion_kwargs.get("tools")`` locally when experimenting with wire tools—never ship that
        # as default user behavior without validating the endpoint.
        result = self.api.request_with_tools(
            msg_dicts,
            max_tokens=self.max_tokens,
            tools=None,
            model=self.model_id,
            response_format=response_format,
            prepend_dev_build_system_prefix=False,
        )
        
        if self._status_callback:
            self._status_callback("Model responded, processing...")

        usage = result.get("usage") or {}
        token_usage = (
            TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            )
            if usage
            else None
        )
        return ChatMessage.from_dict(
            {
                "role": "assistant",
                "content": result.get("content") or "",
                "tool_calls": result.get("tool_calls") or None,
            },
            raw=result,
            token_usage=token_usage,
        )

