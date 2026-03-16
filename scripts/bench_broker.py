#!/usr/bin/env python3
"""Benchmark: time-to-first-token with all tools vs core-only tools.

Usage:
    python scripts/bench_broker.py [--model qwen2.5:32b] [--runs 2]

Loads the real tool registry, generates OpenAI schemas for writer doc type,
and sends a simple prompt to Ollama measuring TTFT and total time.
"""

import argparse
import http.client
import json
import os
import sys
import time

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def collect_tools():
    """Instantiate all ToolBase subclasses from the codebase."""
    from plugin.framework.tool_base import ToolBase
    import importlib
    import inspect
    import pkgutil

    tools = []
    # Scan tool packages
    packages = [
        "plugin.modules.writer",
        "plugin.modules.common.tools",
        "plugin.modules.calc.tools",
        "plugin.modules.draw.tools",
    ]
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
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
    from plugin.framework.schema_convert import to_openai_schema
    return [to_openai_schema(t) for t in tools]


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
    conn = http.client.HTTPConnection("localhost", 11434, timeout=300)

    t0 = time.perf_counter()
    conn.request("POST", "/v1/chat/completions", body=data,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()

    if resp.status != 200:
        err = resp.read().decode()
        conn.close()
        raise RuntimeError("HTTP %d: %s" % (resp.status, err[:200]))

    ttft = None
    text_parts = []
    tc_count = 0

    for line in resp:
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
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        content = delta.get("content") or ""
        if content and ttft is None:
            ttft = (time.perf_counter() - t0) * 1000
        if content:
            text_parts.append(content)
        tcs = delta.get("tool_calls")
        if tcs:
            tc_count += len(tcs)
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000

    total = (time.perf_counter() - t0) * 1000
    conn.close()

    if ttft is None:
        ttft = total

    return ttft, total, "".join(text_parts), tc_count


def run_bench(model, runs):
    print("=" * 60)
    print("Tool Broker Benchmark — Ollama + %s" % model)
    print("=" * 60)

    # Collect tools
    all_tools = collect_tools()
    writer_tools = filter_for_writer(all_tools)
    core_tools = [t for t in writer_tools if t.tier == "core"]
    extended_tools = [t for t in writer_tools if t.tier == "extended"]

    print("\nTotal tools discovered: %d" % len(all_tools))
    print("Writer-compatible:     %d" % len(writer_tools))
    print("  Core:                %d" % len(core_tools))
    print("  Extended:            %d" % len(extended_tools))

    all_schemas = make_schemas(writer_tools)
    core_schemas = make_schemas(core_tools)

    all_json_size = len(json.dumps(all_schemas))
    core_json_size = len(json.dumps(core_schemas))

    print("\nSchema payload sizes:")
    print("  All tools:  %d bytes (%.1f KB)" % (all_json_size, all_json_size / 1024))
    print("  Core only:  %d bytes (%.1f KB)" % (core_json_size, core_json_size / 1024))
    print("  Reduction:  %.0f%%" % ((1 - core_json_size / all_json_size) * 100))

    messages = [
        {"role": "system", "content": "You are a helpful assistant for editing LibreOffice Writer documents."},
        {"role": "user", "content": "What is the current word count of my document?"},
    ]

    # Warmup run (not counted)
    print("\nWarming up model...")
    try:
        ollama_stream(model, [{"role": "user", "content": "hi"}], None)
    except Exception as e:
        print("Warmup failed: %s" % e)
        return

    print("\nRunning %d iterations each...\n" % runs)

    results = {"all": [], "core": []}

    for scenario, schemas, label in [
        ("all", all_schemas, "ALL tools (%d)" % len(all_schemas)),
        ("core", core_schemas, "CORE tools (%d)" % len(core_schemas)),
    ]:
        print("--- %s ---" % label)
        for i in range(runs):
            try:
                ttft, total, text, tc = ollama_stream(model, messages, schemas)
                results[scenario].append({"ttft": ttft, "total": total})
                preview = text[:60].replace("\n", " ") if text else "(tool_call)"
                print("  Run %d: TTFT=%.0fms  Total=%.0fms  [%s]"
                      % (i + 1, ttft, total, preview))
            except Exception as e:
                print("  Run %d: ERROR — %s" % (i + 1, e))
        print()

    # Summary
    print("=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    for scenario, label in [("all", "All tools"), ("core", "Core only")]:
        data = results[scenario]
        if not data:
            continue
        ttfts = [d["ttft"] for d in data]
        totals = [d["total"] for d in data]
        avg_ttft = sum(ttfts) / len(ttfts)
        avg_total = sum(totals) / len(totals)
        print("  %s: avg TTFT=%.0fms  avg Total=%.0fms" % (label, avg_ttft, avg_total))

    if results["all"] and results["core"]:
        avg_all = sum(d["ttft"] for d in results["all"]) / len(results["all"])
        avg_core = sum(d["ttft"] for d in results["core"]) / len(results["core"])
        if avg_all > 0:
            speedup = avg_all / avg_core
            saved = avg_all - avg_core
            print("\n  TTFT speedup: %.1fx (saved %.0fms)" % (speedup, saved))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark tool broker TTFT")
    parser.add_argument("--model", default="qwen2.5:32b")
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    run_bench(args.model, args.runs)
