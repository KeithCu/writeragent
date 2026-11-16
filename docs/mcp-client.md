# MCP client: calling other MCP servers from the sidebar chat

WriterAgent already runs an MCP **server** ([mcp-protocol.md](mcp-protocol.md)) so outside agents can drive LibreOffice. This feature is the other direction: the built-in sidebar chat can call tools on **other** MCP servers, for example a home server that exposes Nextcloud, email, calendar or notes.

Code: [`plugin/mcp/client.py`](../plugin/mcp/client.py), [`plugin/mcp/client_schema.py`](../plugin/mcp/client_schema.py), [`plugin/mcp/client_helpers.py`](../plugin/mcp/client_helpers.py). Tests: [`tests/mcp/test_client.py`](../tests/mcp/test_client.py), [`tests/mcp/test_client_schema.py`](../tests/mcp/test_client_schema.py), [`tests/mcp/test_client_helpers.py`](../tests/mcp/test_client_helpers.py).

## How the model sees it

Remote tools are exposed through the `mcp` sub-agent domain (`delegate_to_subagent(domain="mcp", task=...)`). The domain is hidden from the chat prompt, tool enums, and descriptions when no server is configured.

On each `mcp` delegation:

1. WriterAgent lists each configured server once, then reuses that list from memory. A server that is down becomes a note in the sub-agent's instructions while the remaining servers still run; if no usable tools remain, the delegation returns `MCP_CLIENT_NO_TOOLS`.
2. Each remote tool becomes a native function tool named `mcp__<server>__<tool>` (non-alphanumeric/underscore characters become `_`, clamped to 64 characters with an 8-hex hash suffix). Any names that collide across or within servers after normalization are all skipped.
3. Each tool's `inputSchema` is normalized (`$defs`/`definitions` inlined, nullable unions and const unions collapsed, object shapes repaired) and converted to a native schema. Top-level parameter unions (`anyOf`/`oneOf`) of differently typed branches are merged into a type list when lossless, otherwise the first sendable branch is used; nested unions pass unchanged. Property names outside `[a-zA-Z0-9_.-]{1,64}` are renamed for the model and mapped back to the server's original names before dispatch. Tools whose schemas cannot be converted (recursive, remote, or dangling `$ref`s, oversized expansions over 5,000 nodes, unions with no sendable branch, `allOf`, typeless parameters, or itemless arrays) are skipped and logged.

Every remote call asks first unless the server is trusted (`mcp_client.trusted` or `"trust": "full"`) **and** the tool's `annotations.readOnlyHint` is `true` (read from the delegation's own listing). When asking, the call shows `[Run <server>: <tool>?]` and the indented JSON arguments (up to 1,200 characters, with invisible/control characters escaped) in the chat and swaps Send / Stop / Clear for **Accept / Change / Reject**, the same step web research uses (`chatbot.prompt_for_web_research`). While the approval card is open, the status line keeps "Waiting for approval…" and the hang watchdog does not count that wait. Accept runs the call. Reject stops it and the turn. Change opens the arguments as JSON to edit; the edited object is sent and shown as `[Changed to: …]`, and text that is not a JSON object stops the call and passes the text back to the model. Listing tools never asks.

Result text is head/tail truncated at 20,000 characters (40% head, 60% tail) and carries a note telling the model to treat it as data, not instructions. `structuredContent` is forwarded unless a text block already duplicates its JSON, and protocol-reserved `_meta` keys are stripped. Unicode TAG characters are stripped from results; TAG and bidi controls from tool descriptions and schemas. The delegation payload itself also carries the untrusted-data `note`.

## Configuration

Open **Settings → Integrations**. The **MCP servers for the sidebar chat** section sets up one server, plus a JSON box for additional servers:

| Field | Config key | Meaning |
|-------|------------|---------|
| Use this MCP server in chat | `mcp_client.enabled` | Off by default. |
| Trust its read-only tools (no prompt) | `mcp_client.trusted` | Off by default. When on, tools marked `readOnlyHint: true` run without prompting; all other tools still ask. |
| Name | `mcp_client.name` | Prefixes this server's tool names (`mcp__<name>__<tool>`). Blank becomes `external`. |
| Server URL | `mcp_client.url` | Streamable-HTTP MCP endpoint (`http` or `https`). Required. |
| Keychain item (macOS) | `mcp_client.token_keychain` | macOS only: name (service) of a Keychain generic password holding the bearer token. Preferred. |
| Token | `mcp_client.token` | Bearer token saved in plain text in `writeragent.json`. Used only when Keychain item is empty. |
| More servers | `mcp_client.servers` | JSON object keyed by server name (empty by default). |

### More than one server

Additional servers go in the **More servers** JSON box on **Settings → Integrations** (or `mcp_client.servers` in `writeragent.json`). The Settings form server is listed first; an entry with the same name is ignored.

```json
{
  "home": {
    "url": "http://localhost:8000/mcp",
    "token_keychain": "writeragent-mcp",
    "trust": "full",
    "timeout": 300,
    "connect_timeout": 60,
    "tools": {
      "include": ["nextcloud_*"]
    }
  }
}
```

Accepted keys per entry: `url` (required), `headers`, `enabled` (default `true`), `timeout` (tool-call socket idle budget in seconds, default `300`), `connect_timeout` (connect/initialize/list budget in seconds, default `60`), `tools.include`, `tools.exclude`, `trust` (`"full"` or `"untrusted"`, default `"untrusted"`), `token`, `token_keychain`.

To store a token in the Keychain:

```bash
security add-generic-password -a "$USER" -s writeragent-mcp -w
```

`-w` with no value prompts for the token so it does not land in shell history. The first time WriterAgent reads it, macOS asks whether LibreOffice may use the item. Choose **Always Allow**. If access is denied, the tool returns an error and does not call the server without auth. A pasted `Bearer ` prefix is stripped automatically.

### Tool list cache

Each server's tool list is cached in memory only, filled on first use (`tools/list`), and shared across concurrent turns so two delegations asking at once make one fetch. It is cleared or refreshed when:

- **The server config changes:** the connection is keyed by the full server entry, so editing it makes a new connection and re-lists.
- **Settings OK is pressed:** drops all cached connections even when no setting changed, so the next delegation re-initializes and re-lists every server; this also resets the server's pause (circuit breaker) and session, since pressing OK is an explicit user retry.
- **A new session is initialized (for example after HTTP 404):** clears that server's cached list so the next delegation re-lists.
- **`notifications/tools/list_changed` arrives in a POST SSE reply:** clears that server's cached list so the next delegation re-lists.
- **An unknown-tool reply arrives (`tools/call`):** re-lists once and retries the already-approved call once if the tool is still offered.
- **A transport failure occurs on `tools/call`:** drops the cached list so the next delegation re-lists (and notes the server unavailable if it is still down).

Config is read on every delegation and connections are made lazily, so edits take effect on the next delegation and startup never touches the network.

## Protocol, resilience, and Stop

- **Transport:** MCP streamable HTTP using `LlmHttpTransport.send` (a fresh connection per request so failed POSTs are never retried). Negotiates `protocolVersion` (default `2025-06-18`) and sends `MCP-Protocol-Version` on subsequent requests. Skips `tools/list` when server capabilities omit `tools`.
- **Sessions & paging:** `Mcp-Session-Id` is kept and sent back; a `404` with an active session re-initializes once. `tools/list` is capped at 50 pages.
- **Redirects & URLs:** Only `http://` and `https://` URLs with a host are accepted. Same-origin `307`/`308` redirects are followed up to 3 hops; all other redirects are refused. Literal `${VAR}` placeholders in `url` or `headers` fail closed.
- **Body & SSE caps:** Finite JSON bodies and individual SSE events are capped at 10 MiB; SSE boundaries accept CR, LF, and CRLF. Replies match the first JSON-RPC response (`id` without `method`) without waiting for the stream to close.
- **Errors & circuit breaker:** HTTP errors include the status and up to 300 characters of credential-redacted body text. Failures after sending a `tools/call` report an uncertain outcome. Three consecutive failures open a 60-second circuit breaker for that server (distinguishing unreachable servers from rejected calls).
- **Stop:** Clicking Stop aborts in-flight HTTP waits and Keychain lookups immediately, sends one best-effort `notifications/cancelled` for in-flight requests, and does not trip the circuit breaker.

## Security model

- **Sidebar chat only.** `mcp_domain_tools` refuses callers other than `caller == "chat"` (`MCP_DOMAIN_CHAT_ONLY`), so WriterAgent's own MCP server and Python scripts cannot invoke it. Remote tools never enter `ToolRegistry`, and `filter_unavailable_delegate_domains` hides `mcp` from the chat tool enum and prompt when no servers are configured.
- **Approval.** Every remote call asks unless the server is trusted and the tool has `readOnlyHint: true`. If approval is required but unavailable, the call is refused.
- **Prompt injection & taint.** Email bodies, notes and files can contain text written to steer the model. Once an `mcp` delegation runs in a chat turn, `web_research` in that same turn always asks for user approval even when `chatbot.prompt_for_web_research` is off.
- **Tokens are never logged.** Errors redact credential patterns (`Bearer`, `ghp_`, `sk-`, `token=`, etc.).
- `mcp` is in `READ_ONLY_DOMAINS` and `RemoteMcpTool` sets `is_mutation = False`: it never changes the open document, so it takes no document lock, does not trigger a document refresh, and works in read-only files.

## Limitations

- Stdio servers (`command`), OAuth, resources, prompts, sampling, and server-initiated requests are not supported.
- `notifications/tools/list_changed` sent on the standalone streamable-HTTP GET stream is not received (WriterAgent only reads SSE events inside POST replies), so press OK in Settings to pick up newly added server tools.
- Literal `${VAR}` references in `url` or `headers` are not expanded from the environment and are refused.
- Recursive `$ref` schemas, top-level `allOf`/typeless parameters, unions with no sendable branch, and arrays without `items` are skipped; a lossy union (e.g. two object shapes) offers the model only its first branch; tools with open-ended `object` schemas (no declared `properties`) cannot accept arbitrary undeclared arguments; configured servers are not passed to ACP agent backends (`plugin/acp/acp_backend.py`).
- WriterAgent's own MCP server may still list the `mcp` domain when servers are configured; calls there are refused.
- The Change dialog is shared with web research, so its title still reads "Edit search query".
