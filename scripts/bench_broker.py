#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Benchmark: time-to-first-token with Writer tool schemas on Ollama.

Usage:
    python scripts/bench_broker.py [--model qwen2.5:32b] [--runs 5] [--endpoint URL]

Discovers ToolBase subclasses under selected plugin packages, filters to
Writer-compatible tools, builds OpenAI-style tool schemas, and measures TTFT
for a simple chat completion.
"""

import argparse
import http.client
import json
import os
import sys
import time
import urllib.parse

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def collect_tools():
    """Instantiate all ToolBase subclasses from the codebase."""
    from plugin.framework.tool import ToolBase
    import importlib
    import inspect
    import pkgutil

    tools = []
    # Scan tool packages
    packages = [
        "plugin.writer",
        "plugin.common.tools",
        "plugin.calc.tools",
        "plugin.draw.tools",
    ]
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        assert pkg.__file__ is not None
        pkg_path = os.path.dirname(pkg.__file__)
        for _imp, modname, _ispkg in pkgutil.iter_modules([pkg_path]):
            if modname.startswith("_"):
                continue
            fqn = "%s.%s" % (pkg_name, modname)
            try:
                mod = importlib.import_module(fqn)
            except Exception:
                continue
            for _name, obj in inspect.getmembers(mod, inspect.isclass):
                if (issubclass(obj, ToolBase) and obj is not ToolBase
                        and getattr(obj, "name", None)):
                    try:
                        tools.append(obj())
                    except Exception:
                        pass

    return tools


def filter_for_writer(tools):
    """Return tools compatible with 'writer' doc type."""
    return [t for t in tools if t.doc_types is None or "writer" in t.doc_types]


def make_schemas(tools):
    from plugin.framework.tool import to_openai_schema
    return [to_openai_schema(t) for t in tools]


def _endpoint_parts(endpoint):
    """(https, host, port, path) for an Ollama/OpenAI-compatible base URL."""
    # Without ://, urlsplit parses host:port (e.g. 127.0.0.1:11434) as scheme:path
    if "://" not in endpoint:
        endpoint = "http://" + endpoint
    parsed = urllib.parse.urlsplit(endpoint)
    https = parsed.scheme == "https"
    base = (parsed.path or "").rstrip("/")
    path = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    return https, parsed.hostname or "localhost", parsed.port or (443 if https else 11434), path


def parse_stream(lines, started_at, clock=time.perf_counter):
    """Return (ttft_ms or None, text, unique tool_call count) from SSE lines."""
    ttft = None
    text_parts = []
    indexes = set()
    for line in lines:
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            raise RuntimeError("server error: %s" % (chunk["error"],))
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        content = delta.get("content") or ""
        tcs = delta.get("tool_calls") or []
        if (content or tcs) and ttft is None:
            ttft = (clock() - started_at) * 1000
        if content:
            text_parts.append(content)
        for tc in tcs:
            indexes.add(tc.get("index", 0))
    return ttft, "".join(text_parts), len(indexes)


def ollama_stream(model, messages, tools, endpoint="http://localhost:11434"):
    """Send a streaming request and return (ttft_ms, total_ms, text, tool_calls_count)."""
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": 128,
        "temperature": 0.1,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    data = json.dumps(body).encode("utf-8")
    https, host, port, path = _endpoint_parts(endpoint)
    conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=300)

    t0 = time.perf_counter()
    conn.request("POST", path, body=data, headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })
    resp = conn.getresponse()

    if resp.status != 200:
        err = resp.read().decode()
        conn.close()
        raise RuntimeError("HTTP %d: %s" % (resp.status, err[:200]))

    try:
        ttft, text, tc_count = parse_stream(resp, t0)
        total = (time.perf_counter() - t0) * 1000
    finally:
        # Close connection on success or if parse_stream raises on an error chunk
        conn.close()

    if ttft is None:
        raise RuntimeError("response had no content and no tool calls")

    return ttft, total, text, tc_count


def run_bench(model, runs, endpoint="http://localhost:11434"):
    print("=" * 60)
    print("Writer tool schema TTFT — Ollama + %s" % model)
    print("=" * 60)

    all_tools = collect_tools()
    writer_tools = filter_for_writer(all_tools)
    schemas = make_schemas(writer_tools)

    print("\nTotal tools discovered: %d" % len(all_tools))
    print("Writer-compatible:     %d" % len(writer_tools))
    print("Schemas:               %d" % len(schemas))

    json_size = len(json.dumps(schemas))
    print("\nSchema payload size: %d bytes (%.1f KB)" % (json_size, json_size / 1024))

    messages = [
        {"role": "system", "content": "You are a helpful assistant for editing LibreOffice Writer documents."},
        {"role": "user", "content": "What is the current word count of my document?"},
    ]

    print("\nWarming up model...")
    try:
        ollama_stream(model, messages, schemas, endpoint)
    except Exception as e:
        print("Warmup failed: %s" % e)
        return

    print("\nRunning %d iterations...\n" % runs)

    results = []
    print("--- Writer tools (%d) ---" % len(schemas))
    for i in range(runs):
        try:
            ttft, total, text, tc = ollama_stream(model, messages, schemas, endpoint)
            results.append({"ttft": ttft, "total": total})
            preview = text[:60].replace("\n", " ") if text else "(tool_call)"
            print("  Run %d: TTFT=%.0fms  Total=%.0fms  [%s]"
                  % (i + 1, ttft, total, preview))
        except Exception as e:
            print("  Run %d: ERROR — %s" % (i + 1, e))
    print()

    print("=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    if results:
        ttfts = [d["ttft"] for d in results]
        totals = [d["total"] for d in results]
        avg_ttft = sum(ttfts) / len(ttfts)
        avg_total = sum(totals) / len(totals)
        print("  Writer tools: avg TTFT=%.0fms  avg Total=%.0fms" % (avg_ttft, avg_total))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark TTFT with Writer tool schemas (Ollama)")
    parser.add_argument("--model", default="qwen2.5:32b")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--endpoint", default=os.environ.get("OLLAMA_HOST") or "http://localhost:11434", help="Ollama/OpenAI-compatible base URL (default: %(default)s)")
    args = parser.parse_args()
    run_bench(args.model, args.runs, args.endpoint)
