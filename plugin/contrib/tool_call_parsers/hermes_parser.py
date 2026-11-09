"""
Hermes tool call parser.

Format: <tool_call>{"name": "func", "arguments": {...}}</tool_call>
Based on Hermes / VLLM's Hermes2ProToolParser.extract_tool_calls()
"""

import json
import re
import uuid
from typing import List

from plugin.framework.json_utils import safe_json_loads
from plugin.contrib.tool_call_parsers.openai_compat import ChatCompletionMessageToolCall, Function
from plugin.contrib.tool_call_parsers import ParseResult, ToolCallParser


class HermesToolCallParser(ToolCallParser):
    """
    Parser for Hermes-format tool calls.

    Matches <tool_call>...</tool_call> tags containing JSON with "name" and "arguments".
    Also handles unclosed <tool_call> at end-of-string (truncated generation).
    """

    # Matches both closed and unclosed tool_call tags
    PATTERN = re.compile(
        r"<tool_call>\s*(.*?)\s*</tool_call>|<tool_call>\s*(.*)", re.DOTALL
    )

    def parse(self, text: str) -> ParseResult:
        if "<tool_call>" not in text:
            return text, None

        try:
            matches = self.PATTERN.findall(text)
            if not matches:
                return text, None

            tool_calls: List[ChatCompletionMessageToolCall] = []
            for match in matches:
                # match is a tuple: (closed_content, unclosed_content)
                raw_json = match[0] if match[0] else match[1]
                if not raw_json.strip():
                    continue

                tc_data = safe_json_loads(raw_json, default=None)
                if tc_data is not None and isinstance(tc_data, dict) and "name" in tc_data:
                    raw_args = tc_data.get("arguments", {})
                    # Avoid double-encoding when the model outputs arguments as an already-encoded JSON string
                    if isinstance(raw_args, str):
                        args_str = raw_args
                    else:
                        args_str = json.dumps(raw_args, ensure_ascii=False)
                    call_id = tc_data.get("id") or tc_data.get("call_id") or f"call_{uuid.uuid4().hex[:8]}"
                    tool_calls.append(
                        ChatCompletionMessageToolCall(
                            id=str(call_id),
                            type="function",
                            function=Function(
                                name=tc_data["name"],
                                arguments=args_str,
                            ),
                        )
                    )

            if not tool_calls:
                return text, None

            # Content is everything before the first <tool_call> tag
            content = text[: text.find("<tool_call>")].strip()
            return content if content else None, tool_calls

        except Exception:
            return text, None
