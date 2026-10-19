#!/usr/bin/env python3
"""Live MCP smoke test against a running LibreOffice + WriterAgent instance.

Exercises /health, tools/list, and apply_document_content so you can verify
on-screen edits in the active Writer document (and [MCP Result] in the sidebar).

Does not start LibreOffice. Enable MCP in WriterAgent Settings first.

Usage (from repo root):
  python scripts/mcp_live_smoke.py
  python scripts/mcp_live_smoke.py --text "Hello from MCP"
  python scripts/mcp_live_smoke.py --host localhost --port 18765
  python scripts/mcp_live_smoke.py --use-debug
  python scripts/mcp_live_smoke.py --document-url 'vnd.libreoffice:...'
  python scripts/mcp_live_smoke.py --target full_document
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _base_url(host: str, port: int, use_ssl: bool) -> str:
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}:{port}"


def _request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, Any]:
    """HTTP request; returns (status_code, parsed JSON or raw text)."""
    headers = {"Expect": ""}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except urllib.error.URLError as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(2)

    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _mcp_post(
    mcp_url: str,
    method: str,
    params: dict,
    req_id: int,
    *,
    document_url: str | None,
    timeout: float,
) -> dict:
    extra = {"X-Document-URL": document_url} if document_url else None
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    status, data = _request("POST", mcp_url, body=payload, extra_headers=extra, timeout=timeout)
    if status != 200:
        print(f"MCP {method} HTTP {status}: {data}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"MCP {method} invalid response: {data!r}", file=sys.stderr)
        sys.exit(1)
    if "error" in data:
        print(f"MCP {method} error: {json.dumps(data['error'], indent=2)}", file=sys.stderr)
        sys.exit(1)
    return data


def _print_step(label: str) -> None:
    print(f"\n=== {label} ===")


def _default_smoke_text() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"MCP smoke test — {ts} — if you see this, apply_document_content works."


def _debug_token_path() -> Path:
    candidates = []
    if sys.platform == "darwin":
        candidates.append(Path("~/Library/Application Support/LibreOffice/4/user/config").expanduser())
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "LibreOffice" / "4" / "user" / "config")
    else:
        candidates.extend([
            Path("~/.config/libreoffice/4/user/config").expanduser(),
            Path("~/.config/libreoffice/24/user/config").expanduser(),
        ])
    for d in candidates:
        p = d / "writeragent_mcp_debug_token"
        if p.is_file():
            return p
    return candidates[0] / "writeragent_mcp_debug_token" if candidates else Path("writeragent_mcp_debug_token")


def _read_debug_token() -> str:
    path = _debug_token_path()
    if not path.is_file():
        print(f"Debug token file not found at {path}.", file=sys.stderr)
        sys.exit(1)
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"Failed to read debug token file at {path}: {e}", file=sys.stderr)
        sys.exit(1)


def run_smoke(
    host: str,
    port: int,
    use_ssl: bool,
    target: str,
    document_url: str | None,
    use_debug: bool,
    timeout: float,
    skip_apply: bool,
    insert_text: str,
) -> None:
    base = _base_url(host, port, use_ssl)
    mcp_url = f"{base}/mcp"
    health_url = f"{base}/health"
    root_url = f"{base}/"
    debug_url = f"{base}/debug"

    print(f"WriterAgent MCP live smoke → {base}")
    if document_url:
        print(f"Document URL: {document_url}")
    else:
        print("Document: active window (no X-Document-URL)")

    _print_step("GET /health")
    status, health = _request("GET", health_url, timeout=timeout)
    print(f"HTTP {status}")
    print(json.dumps(health, indent=2) if isinstance(health, dict) else health)
    if status != 200 or not isinstance(health, dict) or health.get("status") != "healthy":
        print("Health check failed — is MCP enabled in WriterAgent Settings?", file=sys.stderr)
        sys.exit(1)

    _print_step("GET /")
    status, root = _request("GET", root_url, timeout=timeout)
    print(f"HTTP {status}")
    if isinstance(root, dict):
        print(json.dumps({k: root[k] for k in ("server", "version", "mcp_endpoint") if k in root}, indent=2))
        if root.get("mcp_endpoint") and root["mcp_endpoint"] != mcp_url:
            print(f"Note: advertised mcp_endpoint is {root['mcp_endpoint']!r}")

    _print_step("tools/list")
    if use_debug:
        print("(Skipping JSON-RPC tools/list — --use-debug uses POST /debug only for apply)")
        tool_names = ["apply_document_content"]
    else:
        listed = _mcp_post(mcp_url, "tools/list", {}, 1, document_url=document_url, timeout=timeout)
        tools = listed.get("result", {}).get("tools", [])
        tool_names = [t.get("name") for t in tools if isinstance(t, dict)]
        print(f"Tools ({len(tool_names)}): {', '.join(n for n in tool_names if n)}")
        if "apply_document_content" not in tool_names:
            print("apply_document_content not in tools/list — wrong document type or registry?", file=sys.stderr)
            sys.exit(1)

    if skip_apply:
        print("\n--skip-apply: done.")
        return

    content = [insert_text]
    args = {"content": content, "target": target}

    _print_step("apply_document_content")
    print(f"target={target!r}")
    print(f"text={insert_text!r}")
    print("Watch the Writer window (and chat sidebar [MCP Result] if open).")

    if use_debug:
        debug_token = _read_debug_token()
        extra_headers = {"X-WriterAgent-Debug-Token": debug_token}
        if document_url:
            extra_headers["X-Document-URL"] = document_url
        status, data = _request(
            "POST",
            debug_url,
            body={"action": "call_tool", "tool": "apply_document_content", "args": args},
            extra_headers=extra_headers,
            timeout=timeout,
        )
        print(f"HTTP {status}")
        print(json.dumps(data, indent=2) if isinstance(data, dict) else data)
        if status != 200 or not isinstance(data, dict) or not data.get("ok"):
            sys.exit(1)
        result = data.get("result")
    else:
        called = _mcp_post(
            mcp_url,
            "tools/call",
            {"name": "apply_document_content", "arguments": args},
            2,
            document_url=document_url,
            timeout=timeout,
        )
        print(json.dumps(called, indent=2))
        result_block = called.get("result", {})
        content_blocks = result_block.get("content") if isinstance(result_block, dict) else None
        if content_blocks and isinstance(content_blocks, list) and content_blocks:
            text = content_blocks[0].get("text") if isinstance(content_blocks[0], dict) else None
            if text:
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    result = text
            else:
                result = result_block
        else:
            result = result_block
        if isinstance(result_block, dict) and result_block.get("isError"):
            print("Tool returned isError=true", file=sys.stderr)
            sys.exit(1)

    if isinstance(result, dict) and result.get("status") == "error":
        print(f"Tool error: {result}", file=sys.stderr)
        sys.exit(1)

    print("\nOK — check the document for the inserted text.")


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP live smoke test (running LibreOffice required).")
    parser.add_argument("--host", default="localhost", help="MCP host (default: localhost)")
    parser.add_argument(
        "--text",
        default=None,
        help="Plain text to insert (default: timestamped MCP smoke message)",
    )
    parser.add_argument("--port", type=int, default=18765, help="MCP port (default: 18765)")
    parser.add_argument("--ssl", action="store_true", help="Use https://")
    parser.add_argument(
        "--target",
        choices=("beginning", "end", "selection", "full_document", "search"),
        default="end",
        help="apply_document_content target (default: end)",
    )
    parser.add_argument("--document-url", default=None, help="X-Document-URL for a specific open document")
    parser.add_argument(
        "--use-debug",
        action="store_true",
        help="Use POST /debug call_tool instead of JSON-RPC tools/call (localhost only)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds (default: 60)")
    parser.add_argument("--skip-apply", action="store_true", help="Only health + tools/list")
    args = parser.parse_args()

    if args.target == "search":
        print("--target search requires old_content; use end or full_document for smoke.", file=sys.stderr)
        sys.exit(2)

    insert_text = args.text if args.text is not None else _default_smoke_text()

    run_smoke(
        host=args.host,
        port=args.port,
        use_ssl=args.ssl,
        target=args.target,
        document_url=args.document_url,
        use_debug=args.use_debug,
        timeout=args.timeout,
        skip_apply=args.skip_apply,
        insert_text=insert_text,
    )


if __name__ == "__main__":
    main()
