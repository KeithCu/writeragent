# MCP client: calling other MCP servers from the sidebar chat

WriterAgent already runs an MCP **server** ([mcp-protocol.md](mcp-protocol.md)) so outside agents can drive LibreOffice. This feature is the other direction: the built-in sidebar chat can call tools on **other** MCP servers, for example a home server that exposes Nextcloud, email, calendar or notes.

Code: [`plugin/mcp/client.py`](../plugin/mcp/client.py), [`plugin/mcp/client_helpers.py`](../plugin/mcp/client_helpers.py). Tests: [`tests/mcp/test_client.py`](../tests/mcp/test_client.py), [`tests/mcp/test_client_helpers.py`](../tests/mcp/test_client_helpers.py).

## How the model sees it

All remote tools sit behind one chat tool, `external_mcp`. A long tool list makes small local models choose badly, so the model sees one entry (and only when at least one server is configured) and asks for the catalog when it needs it:

1. `external_mcp(action="list_tools")` returns every configured server with its tools (name, first paragraph of the description, argument names) and the untrusted-data `note`. A server that is down appears with an `error` and the others still list.
2. `external_mcp(action="list_tools", tool="<name>")` returns that tool's description (capped at 2,000 characters), full input schema, and the untrusted-data `note`.
3. `external_mcp(action="call_tool", server="<server>", tool="<name>", arguments={...})` runs it. `server` may be left out when only one server is configured.

Every `call_tool` asks first unless the server is trusted (`mcp_client.trusted` or `"trust": "full"`) **and** the tool's `annotations.readOnlyHint` is `true`. When asking, step 3 shows `[Run <server>: <tool>?]` and the indented JSON arguments (up to 1,200 characters, with invisible/control characters escaped) in the chat and swaps Send / Stop / Clear for **Accept / Change / Reject**, the same step web research uses (`chatbot.prompt_for_web_research`). Accept runs the call. Reject stops it and the turn. Change opens the arguments as JSON to edit; the edited object is sent and shown as `[Changed to: …]`, and text that is not a JSON object stops the call and passes the text back to the model. Listing tools never asks.

Each result repeats the arguments that were sent. If none were sent to a tool that takes some, a hint tells the model to nest them in `arguments`: models often put them next to `action` and `tool`, where the tool schema drops them, and then repeat the same empty call.

Result text is head/tail truncated at 20,000 characters (40% head, 60% tail) and carries a note telling the model to treat it as data, not instructions. `structuredContent` is forwarded unless a text block already duplicates its JSON, and protocol-reserved `_meta` keys are stripped. Unicode TAG characters are stripped from results; TAG and bidi controls from tool descriptions.

## Configuration

Open **Settings → Integrations**. The **MCP servers for the sidebar chat** section sets up one server, plus a JSON box for additional servers:

| Field | Config key | Meaning |
|-------|------------|---------|
| Use this MCP server in chat | `mcp_client.enabled` | Off by default. |
| Trust its read-only tools (no prompt) | `mcp_client.trusted` | Off by default. When on, tools marked `readOnlyHint: true` run without prompting; all other tools still ask. |
| Name | `mcp_client.name` | Short label the model uses in `server`. Blank becomes `external`. |
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

Config is read on every call and connections are made lazily, so edits take effect on the next tool call and startup never touches the network.

## Protocol, resilience, and Stop

- **Transport:** MCP streamable HTTP using `LlmHttpTransport.send` (a fresh connection per request so failed POSTs are never retried). Negotiates `protocolVersion` (default `2025-06-18`) and sends `MCP-Protocol-Version` on subsequent requests. Skips `tools/list` when server capabilities omit `tools`.
- **Sessions & paging:** `Mcp-Session-Id` is kept and sent back; a `404` with an active session re-initializes once. `tools/list` is capped at 50 pages.
- **Redirects & URLs:** Only `http://` and `https://` URLs with a host are accepted. Same-origin `307`/`308` redirects are followed up to 3 hops; all other redirects are refused. Literal `${VAR}` placeholders in `url` or `headers` fail closed.
- **Body & SSE caps:** Finite JSON bodies and individual SSE events are capped at 10 MiB; SSE boundaries accept CR, LF, and CRLF. Replies match the first JSON-RPC response (`id` without `method`) without waiting for the stream to close.
- **Errors & circuit breaker:** HTTP errors include the status and up to 300 characters of credential-redacted body text. Failures after sending a `tools/call` report an uncertain outcome. Three consecutive failures open a 60-second circuit breaker for that server (distinguishing unreachable servers from rejected calls).
- **Stop:** Clicking Stop aborts in-flight HTTP waits and Keychain lookups immediately, sends one best-effort `notifications/cancelled` for in-flight requests, and does not trip the circuit breaker.

## Security model

- **Sidebar chat only.** `external_mcp` uses `tier = "chat"`, which WriterAgent's own MCP server excludes from both `tools/list` and `tools/call`, and `filter_external_mcp` hides it from the chat tool list when no servers are configured.
- **Not reachable from Python scripts.** It is in `_BLOCKED_FROM_VENV` (`plugin/scripting/host_rpc.py`) and left out of the generated `writeragent` proxy module.
- **Approval.** Every `call_tool` asks unless the server is trusted and the tool has `readOnlyHint: true`. If approval is required but unavailable, the call is refused.
- **Prompt injection & taint.** Email bodies, notes and files can contain text written to steer the model. Once `external_mcp` returns a result in a chat turn, `web_research` in that same turn always asks for user approval even when `chatbot.prompt_for_web_research` is off.
- **Tokens are never logged.** Errors redact credential patterns (`Bearer`, `ghp_`, `sk-`, `token=`, etc.).
- `is_mutation = False`: the tool never changes the open document, so it takes no document lock, does not trigger a document refresh, and works in read-only files.

## Limitations

- Stdio servers (`command`), OAuth, resources, prompts, sampling, and server-initiated requests are not supported.
- Literal `${VAR}` references in `url` or `headers` are not expanded from the environment and are refused.
- A single-tool `list_tools` lookup returns the schema as text; configured servers are not passed to ACP agent backends (`plugin/acp/acp_backend.py`).
- The Change dialog is shared with web research, so its title still reads "Edit search query".
