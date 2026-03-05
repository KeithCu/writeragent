#!/usr/bin/env python3
"""
Run streaming + tool-calling against your configured endpoint. No LibreOffice required.
Shows content, thinking, and tool_calls as they accumulate on screen.

Usage:
  python tests/run_streaming_test.py

Requires CHAT_ENDPOINT and OPENROUTER_API_KEY in the environment.
"""

import json
import os
import ssl
import sys
import urllib.request

# Add project 
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "x-ai/grok-4.1-fast"

from plugin.modules.core.streaming_deltas import accumulate_delta
from plugin.main import get_tools
try:
    WRITER_TOOLS = get_tools().get_openai_schemas(doc_type="writer")
except Exception:
    WRITER_TOOLS = []

def _extract_thinking(delta):
    # Check common reasoning keys
    reasoning = delta.get("reasoning_content") or delta.get("thought") or delta.get("thinking") or ""
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    details = delta.get("reasoning_details")
    if not isinstance(details, list):
        return ""
    parts = []
    for item in details:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning.text":
            parts.append(item.get("text") or "")
        elif item.get("type") == "reasoning.summary":
            parts.append(item.get("summary") or "")
    return "".join(parts) if parts else ""


def _normalize_content(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif isinstance(item, dict) and "text" in item:
                parts.append(item.get("text") or "")
        return "".join(parts) if parts else None
    return str(raw)


def run_streaming_with_tools(prompt: str):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: OPENROUTER_API_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    endpoint = (os.environ.get("CHAT_ENDPOINT") or "").strip().rstrip("/")
    if not endpoint:
        print("Error: CHAT_ENDPOINT environment variable is required (your API URL).", file=sys.stderr)
        sys.exit(1)
    url = endpoint + "/chat/completions"

    messages = [{"role": "user", "content": prompt}]
    data = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.5,
        "stream": True,
        "tools": WRITER_TOOLS,
        "tool_choice": "auto",
        "reasoning": {"effort": "minimal"},
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    request = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers)
    request.get_method = lambda: "POST"

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    content_chunks = []
    thinking_chunks = []
    thinking_started = False

    def on_content(t):
        content_chunks.append(t)
        print(t, end="", flush=True)

    def on_thinking(t):
        nonlocal thinking_started
        thinking_chunks.append(t)
        if not thinking_started:
            print("[Thinking] ", end="", flush=True)
            thinking_started = True
        print(t, end="", flush=True)

    from plugin.framework.http import LlmClient
    config = {
        "endpoint": endpoint.replace("/v1", ""), # LlmClient._api_path() adds /v1 or /api
        "api_key": api_key,
        "model": MODEL,
        "api_type": "chat",
        "request_timeout": 120
    }
    client = LlmClient(config, None) # None for ctx as it's not needed for basic logging
    
    message_snapshot = {}
    
    def on_delta(delta):
        accumulate_delta(message_snapshot, delta)

    print("Streaming from endpoint using LlmClient...\n")
    print("--- Content ---")

    try:
        last_finish_reason = client.stream_request_with_tools(
            messages,
            max_tokens=2048,
            tools=WRITER_TOOLS,
            append_callback=on_content,
            append_thinking_callback=on_thinking,
            # We don't need on_delta here as stream_request_with_tools handles it,
            # but we can pass it if we want to monitor raw deltas.
        )
        # Note: stream_request_with_tools returns the final message dict, 
        # but we also accumulate into message_snapshot via its internal logic (if we could tap it).
        # Actually, stream_request_with_tools returns the result. Let's use it.
        result = last_finish_reason
        message_snapshot["content"] = result["content"]
        message_snapshot["tool_calls"] = result["tool_calls"]
        last_finish_reason = result["finish_reason"]
    except Exception as e:
        print(f"\n\nError: {e}", file=sys.stderr)
        raise

    if thinking_started:
        print(" /thinking", flush=True)

    print("\n\n--- Result ---")
    content = _normalize_content(message_snapshot.get("content"))
    tool_calls = message_snapshot.get("tool_calls")

    if content:
        print(f"Content: {content!r}")
    if tool_calls:
        print(f"Tool calls ({len(tool_calls)}):")
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if args_str else {}
                print(f"  [{i}] {name}({args})")
                if name == "apply_markdown":
                    md = args.get("markdown")
                    print(f"    -> Analysis: json.loads successful. markdown type={type(md)}")
                    if isinstance(md, str):
                        print(f"    -> content preview: {repr(md[:50])}...")
                        if md.strip().startswith("['") and md.strip().endswith("']"):
                             print("    -> WARNING: Content looks like a stringified list!")
                    elif isinstance(md, list):
                        print(f"    -> content is list of {len(md)} items.")
            except json.JSONDecodeError:
                print(f"  [{i}] {name}(raw: {args_str})")
                print("    -> Analysis: json.loads FAILED.")
                try:
                    import ast
                    args_ast = ast.literal_eval(args_str)
                    print(f"    -> Analysis: ast.literal_eval SUCCEEDED. args={args_ast}")
                    if name == "apply_markdown":
                        md = args_ast.get("markdown")
                        print(f"    -> markdown type via AST={type(md)}")
                except Exception as e:
                    print(f"    -> Analysis: ast.literal_eval also FAILED: {e}")
    print(f"Finish reason: {last_finish_reason}")


if __name__ == "__main__":
    prompt = (
        "You have access to document tools. Call get_document_text with max_chars 100 "
        "to read the document, then briefly summarize what you would do with it. "
        "If you cannot call tools, just say 'No tools available'."
    )
    if len(sys.argv) > 1 and sys.argv[1] == "--analyze-resume":
        print("Analyzing Markdown generation behavior...")
        prompt = (
            "You are a helpful assistant. Write a brief resume for a software engineer in Markdown format. "
            "Use headings, bullet points, and bold text. "
            "Call the apply_markdown tool to insert it into the document. "
            "Do not just output text, you MUST call the tool."
        )
    elif len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = (
            "You have access to document tools. Call get_document_text with max_chars 100 "
            "to read the document, then briefly summarize what you would do with it. "
            "If you cannot call tools, just say 'No tools available'."
        )
    
    run_streaming_with_tools(prompt)
