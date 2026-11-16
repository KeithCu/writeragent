# MCP client: calling other MCP servers from the sidebar chat

WriterAgent already runs an MCP **server** ([mcp-protocol.md](mcp-protocol.md)) so outside agents can drive LibreOffice. This feature is the other direction: the built-in sidebar chat can call tools on **other** MCP servers, for example a home server that exposes Nextcloud, email, calendar or notes.

Code: [`plugin/mcp/client.py`](../plugin/mcp/client.py). Tests: [`tests/mcp/test_client.py`](../tests/mcp/test_client.py).

## How the model sees it

All remote tools sit behind one chat tool, `external_mcp`. A long tool list makes small local models choose badly, so the model sees one entry and asks for the catalog only when it needs it:

1. `external_mcp(action="list_tools")` returns every configured server with its tools (name, first paragraph of the description, argument names). A server that is down appears with an `error` and the others still list.
2. `external_mcp(action="list_tools", tool="<name>")` returns that tool's full input schema.
3. `external_mcp(action="call_tool", server="<server>", tool="<name>", arguments={...})` runs it. `server` may be left out when only one server is configured.

When **Ask before each remote tool call** is on (the default), step 3 first shows `[Run <server>: <tool> {arguments}?]` in the chat and swaps Send / Stop / Clear for **Accept / Change / Reject**, the same step web research uses (`chatbot.prompt_for_web_research`). Accept runs the call. Reject stops it and the turn. Change opens the arguments as JSON to edit; the edited object is sent and shown as `[Changed to: …]`, and text that is not a JSON object stops the call and passes the text back to the model. Listing tools never asks.

Each result repeats the arguments that were sent. If none were sent to a tool that takes some, a hint tells the model to nest them in `arguments`: models often put them next to `action` and `tool`, where the tool schema drops them, and then repeat the same empty call.

The result text is capped at 20,000 characters and carries a note telling the model to treat it as data, not instructions.

## Configuration

Open **Settings → MCP**. The **MCP Client** section, below the MCP server and agent backend settings, sets up one server:

| Field | Config key | Meaning |
|-------|------------|---------|
| Use this MCP server in chat | `mcp_client.enabled` | Off by default. |
| Ask before each remote tool call | `mcp_client.confirm_calls` | On by default. Applies to every server, including those in the list below. |
| Name | `mcp_client.name` | Short label the model uses in `server`. Blank becomes `external`. |
| Server URL | `mcp_client.url` | Streamable-HTTP MCP endpoint. Required. |
| Keychain item | `mcp_client.token_keychain` | macOS only: name (service) of a Keychain generic password holding the bearer token. Preferred. |
| Token | `mcp_client.token` | Bearer token saved in plain text in `writeragent.json`. Used only when Keychain item is empty. |

The fields come from [`plugin/mcp_client/module.yaml`](../plugin/mcp_client/module.yaml), shown on the MCP tab through `config_inline: mcp` like the agent backend section.

### More than one server

Further servers go in the `mcp_client.servers` list in `writeragent.json` (next to `writeragent_debug.log` in the LibreOffice user profile). Edit it while LibreOffice is closed. The Settings server is listed first; a list entry with the same name is ignored.

```json
"mcp_client.servers": [
    {
        "name": "home",
        "url": "http://localhost:8000/mcp",
        "token_keychain": "writeragent-mcp"
    }
]
```

| List field | Meaning |
|-------|---------|
| `name` | Short label the model uses in `server`. Required. |
| `url` | Streamable-HTTP MCP endpoint. Required. |
| `token_keychain` | macOS only: name (service) of a Keychain generic password holding the bearer token. Preferred. |
| `token` | Bearer token in plain text. Fallback when the Keychain is not available. |
| `enabled` | `false` hides the server without deleting the entry. Default `true`. |

To store a token in the Keychain:

```bash
security add-generic-password -a "$USER" -s writeragent-mcp -w
```

`-w` with no value prompts for the token so it does not land in shell history. The first time WriterAgent reads it, macOS asks whether LibreOffice may use the item. Choose **Always Allow**. If access is denied, the tool returns an error and does not call the server without auth.

Config is read on every call and connections are made lazily, so edits take effect on the next tool call and startup never touches the network.

## Protocol support

- MCP streamable HTTP, protocol version `2025-06-18`: `initialize`, `notifications/initialized`, `tools/list` (with paging), `tools/call`.
- Replies may be plain JSON or a server-sent-event stream.
- `Mcp-Session-Id` is kept and sent back. If the server answers 404 (it restarted and forgot the session), the client starts a new session and sends the request once more.
- Only the standard library is used. The shared chat HTTP transport is not used: it retries failed POSTs, which could run a remote tool twice, and treats the `202 Accepted` reply to notifications as an error.
- Not supported: the older HTTP+SSE transport, stdio servers, OAuth, resources, prompts, sampling, and server-initiated requests.

## Security model

- **Sidebar chat only.** `external_mcp` uses `tier = "chat"`, which WriterAgent's own MCP server excludes from both `tools/list` and `tools/call`. That server has no authentication; exposing the tool there would let any local program use the remote servers with the user's tokens.
- **Not reachable from Python scripts.** It is in `_BLOCKED_FROM_VENV` (`plugin/scripting/host_rpc.py`) and left out of the generated `writeragent` proxy module. `=PY()` code can arrive inside someone else's spreadsheet and must not read the user's mail or files.
- **Approval.** Every `call_tool` asks first unless **Ask before each remote tool call** is off. If it must ask but the approval step is not available (any caller other than the sidebar chat), the call is refused, not sent. Turn asking off only when every configured server's tools just read.
- **Prompt injection.** Email bodies, notes and files can contain text written to steer the model. The result note asks the model to ignore such instructions, but that is not a guarantee. The main ways data could leave the machine are tools that reach the internet: remote search tools, a remote "ping host" tool, and WriterAgent's own `web_research`. Setting **Doc → Agent edit review mode** to *Record* makes any document edits that follow reviewable.
- **Tokens are never logged.** Errors log only the server name and the failure.
- `is_mutation = False`: the tool never changes the open document, so it takes no document lock, does not trigger a document refresh, and works in read-only files.

## Limitations

- Settings edits one server; more need the `mcp_client.servers` list in `writeragent.json`.
- Configured servers are not passed to ACP agent backends (`plugin/acp/acp_backend.py` still sends only WriterAgent's own server in `session/new`). External agents can be given the same servers in their own MCP configuration.
- Each tool call waits up to 60 seconds for the remote server.
- The Change dialog is shared with web research, so its title still reads "Edit search query".
