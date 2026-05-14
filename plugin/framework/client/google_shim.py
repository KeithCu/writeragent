# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""
Google Gemini provider shim.
"""

import json
import logging
from typing import Any

from plugin.framework.config import get_url_path_and_query
from .llm_client import BaseProviderShim

log = logging.getLogger(__name__)


class GoogleShim(BaseProviderShim):
    """Shim for Google Gemini native API."""

    def build_chat_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format):
        endpoint = self.client._endpoint()
        auth_info = self.client._resolve_auth()
        key = auth_info.get("api_key", "")
        m_id = model_name
        if not m_id:
            m_id = "gemini-1.5-flash"
        if not m_id.startswith("models/"):
            m_id = f"models/{m_id}"
        action = ":streamGenerateContent" if stream else ":generateContent"
        url = f"{endpoint}/v1beta/{m_id}{action}?key={key}"

        contents: list[dict[str, Any]] = []
        system_instruction = None
        for m in messages:
            role = m["role"]
            parts: list[dict[str, Any]] = []

            content = m.get("content")
            if content:
                if isinstance(content, str):
                    parts.append({"text": content})
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            parts.append({"text": part.get("text", "")})

            tool_calls = m.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    try:
                        args_obj = json.loads(args) if isinstance(args, str) else args
                    except Exception:
                        args_obj = {}
                    parts.append({"functionCall": {"name": fn.get("name"), "args": args_obj}})

            if role == "system":
                system_instruction = {"parts": parts}
            elif role == "tool":
                try:
                    resp_obj = json.loads(content) if isinstance(content, str) else content
                except Exception:
                    resp_obj = {"result": content}
                if not isinstance(resp_obj, dict):
                    resp_obj = {"result": resp_obj}
                contents.append({"role": "function", "parts": [{"functionResponse": {"name": m.get("name") or m.get("tool_call_id"), "response": resp_obj}}]})
            else:
                if role == "assistant":
                    role = "model"
                contents.append({"role": role, "parts": parts})

        google_data: dict[str, Any] = {"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
        if system_instruction:
            google_data["system_instruction"] = system_instruction
        if tools:
            decls = []
            for t in tools:
                fn = t.get("function", {})
                decls.append({"name": fn.get("name"), "description": fn.get("description", ""), "parameters": fn.get("parameters", {"type": "object", "properties": {}})})
            google_data["tools"] = [{"function_declarations": decls}]

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(google_data).encode("utf-8"), self.client._headers()

    def parse_response_chunk(self, chunk):
        candidates = chunk.get("candidates", [])
        choice = candidates[0] if candidates else {}
        content = ""
        tool_calls = []
        parts = choice.get("content", {}).get("parts", [])
        for p in parts:
            if "text" in p:
                content += p.get("text") or ""
            if "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append({"id": fc.get("id", "call_" + str(len(tool_calls))), "type": "function", "function": {"name": fc.get("name"), "arguments": json.dumps(fc.get("args", {}))}})
        finish_reason = choice.get("finishReason")
        if finish_reason == "STOP":
            finish_reason = "stop"

        usage = chunk.get("usageMetadata", {})
        delta: dict[str, Any] = {"usage": usage}
        if tool_calls:
            delta["tool_calls"] = tool_calls
        return content, finish_reason, None, delta

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        endpoint = self.client._endpoint()
        key = self.client._resolve_auth().get("api_key", "")
        model_name = model or "imagen-3.0-generate-002"

        if model_name.startswith("imagen"):
            # Imagen models use :predict
            url = f"{endpoint}/v1beta/models/{model_name}:predict?key={key}"
            aspect = "1:1"
            if width > height * 1.5:
                aspect = "16:9"
            elif height > width * 1.5:
                aspect = "9:16"

            data = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": aspect}}
        else:
            # Gemini multimodal use :generateContent
            url = f"{endpoint}/v1beta/models/{model_name}:generateContent?key={key}"
            data = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data):
        out = []
        if "error" in response_data:
            msg = response_data["error"].get("message", "Unknown Google API error")
            log.error(f"Google image generation error: {msg}")
            return []

        if "predictions" in response_data:
            # Imagen response
            preds = response_data.get("predictions", [])
            for pr in preds:
                if b64 := pr.get("bytesBase64Encoded"):
                    out.append(b64)

        # Gemini multimodal response
        candidates = response_data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for p in parts:
                inline = p.get("inlineData", {})
                if inline and inline.get("data"):
                    out.append(inline["data"])
        return out
