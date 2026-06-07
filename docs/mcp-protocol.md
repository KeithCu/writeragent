# MCP Protocol — Status and Future Work

## What Is This?

MCP (Model Context Protocol) is a standard for exposing tool sets to external AI clients
(Claude Desktop, Cursor, LM Studio, custom scripts, etc.) over HTTP. The `libreoffice-mcp-extension/`
directory in this repo is an existing standalone extension that implements a similar HTTP API
for LibreOffice.

WriterAgent now includes an **MCP HTTP server** built in: users who install WriterAgent can
use it as an embedded AI editing tool (the sidebar) **and** as a source of document tools
for external AI clients. This document describes what was implemented, how it works, and
what to consider doing next.

---

## Current HTTP MCP (2026)

**Enable:** Settings → **Enable MCP Server** (`mcp.mcp_enabled`, default off). Default port **8765** (`mcp.mcp_port`).

**Client URL:** `http://localhost:8765/mcp` (streamable HTTP / JSON-RPC 2.0). External clients must include the `/mcp` path (not the server base URL alone).

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/mcp` | JSON-RPC: `initialize`, `tools/list`, `tools/call`, … |
| `GET` | `/mcp` | SSE keepalive only (not full legacy MCP) |
| `POST` | `/sse`, `/messages` | Same JSON-RPC as `/mcp` |
| `GET` | `/health` | Liveness |
| `GET` | `/` | Server info; includes `mcp_endpoint` when MCP is enabled |

**Code:** [`plugin/mcp/mcp_protocol.py`](../plugin/mcp/mcp_protocol.py), [`plugin/mcp/__init__.py`](../plugin/mcp/__init__.py), [`plugin/mcp/server.py`](../plugin/mcp/server.py) (`mcp_endpoint_url`).

**Document targeting:** `X-Document-URL` header on MCP requests (see below).

**Concurrency:** Multiple MCP clients may call `tools/call` in parallel. See [Concurrency and parallel `tools/call`](#concurrency-and-parallel-toolscall) and [Threading architecture — MCP](threading_architecture.md#2-http-server-and-mcp-protocol-pluginmcp).

### Live smoke test (running LibreOffice)

Use [`scripts/mcp_live_smoke.py`](../scripts/mcp_live_smoke.py) when LibreOffice is already open with WriterAgent and MCP enabled. It does **not** start `soffice`; it checks `/health`, `tools/list`, then calls `apply_document_content` with plain text at `target=end` (default) so you can confirm edits **on screen** in the active Writer window. The chat sidebar shows `[MCP Result]` for JSON-RPC `tools/call` (not for `--use-debug`). Default host is **localhost** (port 8765).

```bash
python scripts/mcp_live_smoke.py
python scripts/mcp_live_smoke.py --text "Hello from MCP"
python scripts/mcp_live_smoke.py --document-url 'vnd.libreoffice:...'
python scripts/mcp_live_smoke.py --use-debug   # POST /debug call_tool (localhost only)
```

**Localhost debug shortcut:** `POST /debug` with `{"action":"call_tool","tool":"…","args":{…}}` runs a tool without the full MCP client handshake. Restricted to `127.0.0.1` / `::1`. Same port as MCP; see `handle_debug_post` in [`plugin/mcp/mcp_protocol.py`](../plugin/mcp/mcp_protocol.py).

### OPTIONS `/mcp` (CORS preflight)

Browser and streamable-HTTP MCP clients send **`OPTIONS /mcp`** before `POST /mcp`. The server responds with **HTTP 204** and an **empty body** — that is **success**, not an error. Logs that only show `HTTP/1.0 204 No Content` (or `HTTP/1.1 204`) are normal; you must inspect the **response headers** (DevTools → Network → Headers, or `curl -i`).

CORS must allow every header the client names in `Access-Control-Request-Headers`, including **`Mcp-Protocol-Version`** / `mcp-protocol-version` (and often `Content-Type`, `Mcp-Session-Id`, `X-Document-URL`). POST responses also send **`Mcp-Protocol-Version`** and expose it via **`Access-Control-Expose-Headers`** so browser JavaScript can read session and version headers. Implementation: [`plugin/mcp/cors.py`](../plugin/mcp/cors.py), used from [`plugin/mcp/server.py`](../plugin/mcp/server.py) and [`plugin/mcp/mcp_protocol.py`](../plugin/mcp/mcp_protocol.py).

Verify preflight from a shell:

```bash
curl -i -X OPTIONS 'http://localhost:8765/mcp' \
  -H 'Origin: http://localhost:3000' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type, Mcp-Protocol-Version, Mcp-Session-Id'
```

Expect:

- Status **`204`**, empty body
- **`Access-Control-Allow-Origin`** reflecting the `Origin` value (loopback: `localhost`, `127.0.0.1`, `[::1]`, plus configured extras — see below)
- **`Access-Control-Allow-Headers`** containing `Mcp-Protocol-Version` (any casing)
- **`Access-Control-Expose-Headers`**: `Mcp-Session-Id, Mcp-Protocol-Version`

### Browser CORS (local/private origins + optional list)

Browser MCP clients send an `Origin` header (e.g. `https://localai.local`). The server must reflect that exact origin in `Access-Control-Allow-Origin` (no wildcard patterns).

**Settings → MCP → Allow CORS from local/private browser origins** (`mcp.cors_allow_private_origins`, default **on**): automatically allows Origins whose host is:

- A suffix: `.local`, `.lan`, `.home.arpa`, `.internal`, `.intern` (e.g. `https://localai.local`, `http://nas.lan:8080`, `https://localai.intern:3000`)
- A private or link-local IP in the Origin (e.g. `http://192.168.1.50:3000`)

**Loopback** (`localhost`, `127.0.0.1`, `[::1]`) is always allowed without the checkbox.

**Optional explicit list** — only for origins **not** covered above (e.g. public `https://app.company.com`). Edit `writeragent.json` (not the Settings dialog):

```json
"mcp.cors_allow_private_origins": true,
"mcp.cors_allowed_origins": ["https://tools.mycompany.com"]
```

Homelab / LocalAI setups typically need **no** entries in `mcp.cors_allowed_origins`. Implementation: [`plugin/mcp/cors.py`](../plugin/mcp/cors.py), [`plugin/mcp/cors_origins.py`](../plugin/mcp/cors_origins.py).

**Troubleshooting — OPTIONS succeeds but MCP never connects**

1. In the browser Network tab, confirm a **`POST /mcp`** appears **after** OPTIONS. If POST is missing, the browser rejected preflight (wrong `Allow-Headers`, missing `Allow-Origin`, or non-loopback `Origin`).
2. On POST, check response headers include **`Mcp-Session-Id`** (after `initialize`) and **`Mcp-Protocol-Version`**, and that **`Access-Control-Expose-Headers`** lists both (otherwise JS cannot read them).
3. Ensure the client URL includes the **`/mcp`** path and MCP is enabled in Settings.

**Debug log patterns** (`writeragent_debug.log`, Settings → `log_level` **DEBUG** recommended):

| Log line | Meaning |
|----------|---------|
| `[MCP-CORS] OPTIONS /mcp … safe=False` or `allow_origin=omit` | Origin not allowed — enable `mcp.cors_allow_private_origins` or add host to `mcp.cors_allowed_origins` in `writeragent.json`. |
| `[MCP-CORS] OPTIONS /mcp` only, **no** `[MCP-HTTP] POST /mcp` | Preflight reached server; **POST never arrived** (CORS or client config). |
| `[MCP-HTTP] POST /mcp` but **no** `[MCP] <<< initialize` | POST hit HTTP layer then failed parsing, routing, or protocol version (see `rejected unsupported Mcp-Protocol-Version`). |
| `[MCP-HTTP] POST /mcp` + `[MCP] <<< initialize` + `[MCP] >>> initialize -> 200` | Server side OK; failure is likely in the host app reading session headers or later JSON-RPC calls. |
| `[MCP-HTTP] no route for POST /mcp` | Wrong path or MCP routes not registered (server started without `mcp_enabled`). |
| `curl` / CLI POST **never returns**; py-spy shows worker in `readline` | Often HTTP-layer (see [HTTP/1.0 vs HTTP/1.1](#http10-vs-http11-curl-hangs-and-worker-threads)); not always the same thread as your `curl` socket. |

### HTTP/1.0 vs HTTP/1.1 (curl hangs and worker threads)

**Current behavior (minimal fix):** [`GenericRequestHandler`](../plugin/mcp/server.py) does **not** set `protocol_version`, so Python’s `BaseHTTPRequestHandler` advertises **HTTP/1.0**. That matches pre–CORS-logging behavior and avoids several HTTP/1.1 client quirks. OPTIONS still returns **`204`** with an empty body; status line may read `HTTP/1.0 204` — that is normal.

This section is for **future** changes if you need HTTP/1.1 on the wire (some proxies, clients, or spec wording). It explains a regression seen in 2026 and how to debug similar hangs without expanding the default fix.

#### What changed and why `curl` hung

Commit `2418a7b9` added `protocol_version = "HTTP/1.1"` on the MCP HTTP handler. Shortly after, shell clients reported:

```bash
curl -X POST http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

hanging indefinitely. CORS (`OPTIONS`, `Access-Control-*`) was unrelated; the hang correlated with the HTTP version line only.

Two separate HTTP/1.1 mechanisms matter:

| Mechanism | What happens | Symptom if mishandled |
|-----------|----------------|------------------------|
| **`Expect: 100-continue`** | On POST, curl/libcurl often sends headers, waits for **`100 Continue`**, then sends the body. `parse_request()` must call `handle_expect_100()` before `do_POST` reads the body. | **Deadlock:** client waits for `100`, server waits for body bytes (or the reverse on older/bundled Python). |
| **Keep-alive** | HTTP/1.1 defaults to persistent connections. After `OPTIONS` returns `204`, the worker may block in `handle_one_request` → `readline()` waiting for the **next** request on the same socket. | py-spy shows an “idle” worker in `readline`; that may be a **browser preflight** connection, not the `curl` POST. |

Removing `protocol_version` restores HTTP/1.0 defaults: curl typically sends the full POST without `Expect: 100-continue`, and connections close after each response unless the client requests keep-alive explicitly.

#### How to read py-spy stacks (do not over-interpret one thread)

Example snapshot:

- **MainThread** — idle (VCL event loop not inside UNO for this request).
- **http-server** — `serve_forever` (listener).
- **Thread-N (`process_request_thread`)** — `readline` in `handle_one_request` (waiting for the next request line on **that** socket).

That pattern usually means “connection still open, no new request yet,” not “stuck inside `tools/list`.” Once POST is parsed, the worker should move to `do_POST` → [`handle_mcp_post`](../plugin/mcp/mcp_protocol.py) → `_read_body` → JSON-RPC. For `tools/list`, the worker may then block on [`QueueExecutor.execute`](../plugin/framework/queue_executor.py) (up to **10s** timeout when AsyncCallback is available), which looks like `_wait_for_result`, not `readline`.

If POST never reaches the server, logs show **`[MCP-CORS] OPTIONS`** without **`[MCP-HTTP] POST /mcp`** (browser CORS) or curl blocks before any POST log (HTTP handshake / Expect deadlock).

Quick confirmation from a shell:

```bash
# If this works but default curl hangs, suspect Expect / HTTP/1.1:
curl -v -H 'Expect:' -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

(`Expect:` disables libcurl’s `100-continue` behavior.)

#### Request path (where time is spent)

```mermaid
sequenceDiagram
    participant Client as curl_or_browser
    participant Worker as HTTP_worker_thread
    participant Main as VCL_main_thread

    Client->>Worker: TCP connect
    Worker->>Worker: readline request line
    Worker->>Worker: parse_request headers
    alt Expect 100-continue
        Worker->>Client: 100 Continue
        Client->>Worker: request body
    end
    Worker->>Worker: do_POST handle_mcp_post
    alt tools/list with AsyncCallback
        Worker->>Main: queue get_active_document
        Main-->>Worker: result or TimeoutError 10s
    end
    Worker->>Client: JSON-RPC response
    Note over Worker: HTTP/1.1 keep-alive may wait on readline again
```

JSON-RPC and CORS logic live above this layer; fix transport first when the client never gets bytes back.

#### Future options (if you re-enable HTTP/1.1)

Pick **one** small change at a time; avoid combining socket timeouts, `tools/list` changes, and HTTP version in one patch.

1. **Keep HTTP/1.0 (current default)** — Simplest; sufficient for localhost MCP, browsers, and `curl`. Document that `HTTP/1.0 204` on OPTIONS is success.

2. **HTTP/1.1 + explicit `handle_expect_100` only** — In [`server.py`](../plugin/mcp/server.py), set `protocol_version = "HTTP/1.1"` and override:

   ```python
   def handle_expect_100(self):
       self.send_response_only(100)
       self.end_headers()
       return True
   ```

   Stdlib already does this on modern Python; an explicit override helps if LibreOffice’s bundled runtime differs. **Do not** add this without re-testing `curl` and browser POST on that LO build.

3. **Force `Connection: close`** — Set `self.close_connection = True` at the start of each handler (`_dispatch` / `do_OPTIONS`) so workers do not sit in `readline` after preflight. Does **not** fix Expect deadlock on POST; only reduces idle keep-alive threads.

4. **Per-connection read timeout** — e.g. `get_request()` → `conn.settimeout(120)` on [`_ThreadedHTTPServer`](../plugin/mcp/server.py). Recovers stuck sockets eventually; does not fix handshake deadlocks; may surprise long SSE `GET /mcp` clients.

5. **`tools/list` without blocking on active document** — Separate issue: if AsyncCallback is missing, `QueueExecutor.execute` runs UNO on the HTTP thread and can hang forever (no timeout). That is **not** fixed by HTTP version; would need a dedicated change in [`mcp_protocol.py`](../plugin/mcp/mcp_protocol.py) (e.g. skip `get_active_document` when dispatch is unavailable). Only consider if py-spy shows the worker past `do_POST`, inside UNO, with MainThread idle.

#### What we are not doing by default

- Advertising HTTP/1.1 without verifying Expect handling on the **LibreOffice-shipped** Python.
- Large worker-pool or `tools/list` refactors bundled with a transport tweak.
- Mandatory integration tests for `100-continue` unless HTTP/1.1 is re-enabled permanently.

> **Historical note:** Sections below that describe `GET /tools`, `POST /tools/{name}`, and `core/mcp_server.py` refer to an older REST-style API. The live server uses JSON-RPC on `/mcp` only.

---

## MCP architecture for developers (outer host vs inner agent)

This section is the important mental model for integrating Cursor, LM Studio, or custom MCP clients. It applies to **all** advanced WriterAgent capabilities, not only web research.

### What the MCP host actually sees

`tools/list` returns **core-tier** tools only. Tools with `tier="specialized"` or `tier="specialized_control"` are **omitted** from the default registry filter (see [`plugin/framework/tool.py`](../plugin/framework/tool.py) `get_tools` / `get_schemas`). The host typically receives:

- Document I/O: `get_document_content`, `apply_document_content`, `search_in_document`, `get_document_tree`, …
- A single gateway: **`delegate_to_specialized_writer_toolset`** ([`plugin/doc/specialized_base.py`](../plugin/doc/specialized_base.py), Writer variant in [`plugin/writer/specialized_base.py`](../plugin/writer/specialized_base.py))

It does **not** receive dozens of low-level UNO tools (`list_styles`, page margin APIs, chart editors, etc.) as separate MCP tools.

### Where delegation guidance lives (MCP vs sidebar chat)

Sidebar chat injects the same specialized-delegation block into the **system prompt** via [`get_chat_system_prompt_for_document()`](../plugin/framework/constants.py) (`WRITER_SPECIALIZED_DELEGATION_TEMPLATE` and siblings, with a dynamic `domain: description` list).

MCP hosts do **not** get that system prompt by default. Instead, **`tools/list`** enriches the gateway tool only (see [`to_mcp_schema()`](../plugin/framework/tool.py)):

| Field | What the host sees |
|--------|-------------------|
| **`delegate_to_specialized_*_toolset` → `description`** | Short tool summary + full delegation template (semicolon-separated domains, `task` rules for Writer, **same single-line text as chat**) |
| **`inputSchema.properties.domain.description`** | `domain one of:` plus the same semicolon-separated domain list (enum values stay in `enum`) |
| **`inputSchema.properties.task.description`** | [`DELEGATE_SPECIALIZED_TASK_PARAM_HINT`](../plugin/framework/constants.py) (Writer’s detailed `task` rules are in the tool `description`) |

OpenAI/chat tool schemas are **not** duplicated this way—the sidebar already has the system prompt.

Other MCP surfaces (for integrators):

| Surface | Status | Delegation / routing hints |
|---------|--------|----------------------------|
| **`tools/list`** | Implemented | **Primary** — use the delegate gateway tool metadata above |
| **`initialize` → `instructions`** | Short transport stub only | Does not include full chat prompt or domain list today |
| **`prompts/list` / `prompts/get`** | Empty | Could expose full system prompt later; not implemented |
| **`resources/list` / `resources/read`** | Empty | Not used for guidance |
| **`GET /`** | Server name, version, routes | Does **not** return agent instructions (older docs were wrong) |

If a client ignores `initialize.instructions` and only binds tools from `tools/list`, the delegate tool entry is the intended place to learn **which `domain` to pick** and **how to write `task`**.

### What happens when the host calls `delegate`

With [`USE_SUB_AGENT = True`](../plugin/framework/constants.py) (current default), `delegate_to_specialized_writer_toolset` does **not** “switch tools” on the MCP host. Instead WriterAgent:

1. Resolves the `domain` enum (`styles`, `page`, `charts`, `shapes`, `web_research`, …).
2. Collects all tools registered for that domain.
3. Runs a **nested** smolagents `ToolCallingAgent` ([`build_toolcalling_agent`](../plugin/chatbot/smol_agent.py) + [`SmolAgentExecutor`](../plugin/chatbot/smol_agent.py)) on the LibreOffice main thread.
4. Returns **one JSON tool result** (usually a summary string) to the MCP host.

The outer MCP model never holds the specialized tool schemas in its context; it only sees the delegate call and the final payload. That is intentional: smaller host prompts, fewer direct UNO foot-guns, and the same pattern as the in-app sidebar when using delegation.

**Special case `domain="web_research"`:** the gateway forwards to [`WebResearchTool`](../plugin/chatbot/web_research.py) instead of the generic specialized sub-agent, but the idea is the same: an **internal** ReAct loop with `DuckDuckGoSearchTool` / `VisitWebpageTool`, not MCP-exposed search tools.

### Contrast: in-app chat without MCP

| Mode | Constant | Outer model (main chat or MCP host) | Inner work |
|------|----------|--------------------------------------|------------|
| **Sub-agent delegation** | `USE_SUB_AGENT = True` | Calls `delegate` with a natural-language `task` | smol sub-agent runs domain tools |
| **In-place tool switching** | `USE_SUB_AGENT = False` | Receives “switched to domain X”; **same** model calls specialized tools until `specialized_workflow_finished` | No nested agent; tools swapped on the outer loop |

MCP today always follows the **`USE_SUB_AGENT = True`** path when the host uses `delegate`. In-place switching is a main-chat FSM feature ([`plugin/chatbot/tool_loop.py`](../plugin/chatbot/tool_loop.py)); it is **not** exposed over HTTP unless you deliberately change MCP tool exposure and protocol (future work).

### LLM endpoint: the sub-agent still needs your API config

Delegated work—including **web research**—does **not** use the MCP host’s LLM. It uses WriterAgent’s configured chat endpoint via [`get_api_config`](../plugin/framework/config.py) and [`WriterAgentSmolModel`](../plugin/chatbot/smol_agent.py) inside the LibreOffice process that is handling the MCP request.

Implications for integrators:

- **Configure endpoint, model, and API keys in WriterAgent Settings** (same as sidebar chat). If chat cannot reach OpenRouter/Ollama/LM Studio, delegated MCP calls will fail too.
- The MCP host’s model (e.g. Claude in Cursor) only orchestrates **which** WriterAgent tools to call; it does not power the inner research/formatting loop unless you do that work on the host side yourself.
- **Web research checkbox** in the sidebar is a separate UX entry point to the same [`WebResearchTool`](../plugin/chatbot/web_research.py); MCP hosts use `delegate` + `domain: "web_research"` instead.

### Recommended integration patterns

1. **Document-centric (default):** Host uses MCP for read/write/search on the open LO document; uses `delegate` when a task needs specialized UNO APIs (styles, pages, charts, …). Write a **detailed `task` string**—the inner agent does not see the host’s full conversation unless you paste context into `task` or related tool args.

2. **Web research:** Either:
   - `tools/call` → `delegate_to_specialized_writer_toolset` with `domain: "web_research"` and a clear research `task`, then `apply_document_content` with the returned text; or
   - Perform web search on the **host** (Cursor web, etc.) and use WriterAgent MCP only for document updates.

   Expect **long-running** delegate calls (tens of seconds to minutes) and **large** tool results for research compared to most other domains.

3. **Do not assume** `tools/list` is the full WriterAgent surface. If you need direct `list_styles`-style control from the host, that requires a **product change** (expose specialized tiers on MCP), not just a different client config.

### Concurrency and parallel `tools/call`

External MCP hosts often fire several `tools/call` requests at once (e.g. research on one connection while another edits the document). WriterAgent uses **two layers** in [`plugin/mcp/mcp_protocol.py`](../plugin/mcp/mcp_protocol.py):

| Layer | Applies to | Effect |
|-------|------------|--------|
| **Global semaphore** | Backpressure (non-`long_running`) tools only | At most one fast tool on the main thread; overload → HTTP 429 `BusyError` |
| **Per-document gate** | Mutating tools on **both** backpressure and long-running paths | Same normalized `X-Document-URL` / doc key → mutating runs serialize; different docs and read-only runs stay concurrent |

Tools with `long_running = True` (e.g. `delegate_to_specialized_*`, `generate_image`) **skip** the global semaphore so a minutes-long job does not block every other MCP client. They still take the per-document gate when they mutate. Read-only delegations (`domain: "document_research"` or `"web_research"`) opt out via [`ToolBase.requires_document_lock()`](../plugin/framework/tool.py).

**UNO:** All LibreOffice access is marshalled to the main thread. The per-document gate prevents overlapping *mutating MCP tool runs* on the same file, not raw cross-thread UNO (that is already forbidden).

**Targeting:** Send `X-Document-URL` on parallel calls so gates align with the intended document. URLs are normalized (trailing slash stripped) when building gate keys.

**Tests:** [`tests/mcp/test_long_running_concurrency.py`](../tests/mcp/test_long_running_concurrency.py).

**Full design:** [Threading architecture — MCP](threading_architecture.md#2-http-server-and-mcp-protocol-pluginmcp) (paths, diagram, known limits: sidebar chat, gate dict lifetime, save-as key changes).

### Per-connection vs global configuration (multiple servers)

Today, all MCP traffic in a given LibreOffice process shares:

- One HTTP listener (port from `mcp.mcp_port` in [`writeragent.json`](../plugin/framework/config.py) for that user profile).
- One tool registry and one **`get_api_config`** / chat stack for sub-agents.

There is **no** per-MCP-client or per-TCP-connection LLM profile. A Cursor session and an LM Studio session hitting the same LO instance use the same WriterAgent API settings.

**Could this change?** Yes, but it is awkward:

- A per-connection override (e.g. “this MCP client uses endpoint B”) would need to live on the **HTTP session** (`Mcp-Session-Id` or similar) or a client-identifying header, not a single global `writeragent.json` key—otherwise two clients would fight over one setting.
- **Multiple MCP servers** (e.g. two LibreOffice processes on ports 8765 and 8766) are uncommon but possible. Each process has its own config file path only if it uses a **different LibreOffice user profile**; two instances sharing one profile still share one `writeragent.json` and the same API keys. Only one process can bind a given port on `localhost`.
- Any future per-client endpoint feature must **not** assume a single global “MCP model” key; design for **session-scoped** or **instance-scoped** settings so a second server or parallel client does not break the first.

Until then, document for users: **point MCP at `http://localhost:<port>/mcp`, enable MCP in Settings, and configure the chat endpoint for WriterAgent—the inner sub-agent uses that stack.**

### Could MCP expose specialized tools directly?

Possible, but a deliberate fork:

| Approach | Pros | Cons |
|----------|------|------|
| **Status quo (delegate only)** | Small `tools/list`; stable host prompts; inner ReAct + step limits | Host must delegate; no step visibility over MCP; two-hop workflows |
| **Expose `tier=specialized` on MCP** | True “pure MCP”; host calls `list_styles` etc. | Huge schemas; token cost every turn; more misuse of UNO tools |
| **MCP-only in-place switching** | Host drives specialized tools round-by-round | Protocol + FSM work; differs from current `USE_SUB_AGENT` default |

None of these are required for a working integration; they are release-level product choices.

### Why not expose specialized tools on MCP (yet)?

WriterAgent’s primary integration path—sidebar chat and MCP via `delegate_to_specialized_writer_toolset`—uses an **internal sub-agent** with a **domain-scoped** tool list and a single natural-language `task`. That isolated context improves success on hard UNO work (styles, pages, charts, etc.) compared with dumping the entire specialized registry onto the outer host.

An outer MCP model that **alternates** between unrelated tool groups in one long thread (document edits, then styles, then charts, then research) carries stale assumptions, bloated schemas, and cross-domain mistakes. We intentionally keep **`tools/list` small** and push complexity behind `delegate`.

**In-place tool switching** (`USE_SUB_AGENT = False` in main chat) is a different model: the *same* outer loop swaps specialized tools until `specialized_workflow_finished`. That may never be desirable for MCP even if more tools are exposed later—the failure mode is the same: **tool-set thrashing** without a clean sub-context.

**Low priority for now:** MCP could be extended to expose additional tools on `tools/list` (specialized-tier tools or other surfaces). That could work for some hosts, especially if they **clear or compact context** so earlier tool-call history does not accumulate. It has not been a development focus because delegation matches the main use cases today.

**Still required internally:** Even with a larger MCP surface, the internal agent stack remains necessary for features that are **not** orchestrated by an MCP client— notably the **background grammar checker** ([`docs/realtime-grammar-checker-plan.md`](realtime-grammar-checker-plan.md)) and similar automatic pipelines we may add later. Those run on their own schedules inside LibreOffice; an outer model cannot replace them by calling MCP tools in a chat session.

---

## Current Status — What Was Implemented

The MCP server is **implemented and opt-in** (default off). Summary:

- **`core/mcp_thread.py`**: `_Future`, `execute_on_main_thread()`, `drain_mcp_queue()`. Work from HTTP handler threads is queued and executed on LibreOffice’s main thread.
- **`core/mcp_server.py`**: HTTP server on localhost; GET `/health`, `/`, `/tools`, `/documents`; POST `/tools/{name}`. Port utilities: `_probe_health`, `_is_port_bound`, `_kill_zombies_on_port`.
- **Idle-time draining**: **`AsyncCallback` thread** in `main.py`. A background Python thread loops and queues an `XCallback` invocation via `com.sun.star.awt.AsyncCallback` every 100ms, which safely executes `drain_mcp_queue()` on the main VCL thread. Option of piggybacking on the chat stream drain loop was **not** used — it would only service MCP during active chat, which is inadequate for standalone MCP use.
- **Document targeting** (two supported paths):
  - **Preferred (modern clients):** `document_url` parameter passed **directly in the tool call `arguments`** (e.g. in `tools/call` JSON-RPC). The server pops it from args and uses it for resolution. This works cleanly for multi-document workflows without header management and is the recommended path for Cursor, Hermes, custom agents, etc.
  - **Fallback / legacy:** `X-Document-URL` HTTP header on requests (still supported for compatibility and simple "active doc" cases).
  - Discovery: Call the MCP-only `list_open_documents` tool to get current open docs + their exact `document_url` values.
  - See implementation in `plugin/mcp/mcp_protocol.py` (`_mcp_tools_call` pops `document_url` from arguments before falling back to header).
  - Full client guidance + examples live in the companion meta repos:
    - Cursor users: https://github.com/KeithCu/cursor-libreoffice (includes rules for MCP usage).
    - General agents / Hermes: https://github.com/KeithCu/libreoffice-skill (SKILL.md with targeting best practices).

  This design avoids races when multiple documents or users are involved; “active document only” was not used.
- **Config**: `mcp_enabled` (default false), `mcp_port` (default 8765). Documented in `core/config.py`.
- **Settings**: MCP section on **Page 1** of the Settings dialog (no separate tab): “Enable MCP Server” checkbox, Port field, “Localhost only, no auth.” label. Dialog layout was compacted so short fields share rows and the OK button sits at the bottom with minimal gap.
- **Menu**: “Toggle MCP Server” and “MCP Server Status” under WriterAgent. Status dialog shows RUNNING/STOPPED, port, URL, and health check.
- **Auto-start**: When the user saves Settings with MCP enabled, the server (and timer) start if not already running.
- **Icons**: Six PNGs copied from `libreoffice-mcp-extension/icons/` to `assets/` (for possible future dynamic menu icons).
- **Import fix**: `XTimerListener` is imported only inside `_start_mcp_timer()` so that the Python loader can load `main.py` for registry info without requiring UNO.

See **AGENTS.md** (Section “MCP Server — DONE”) and the code in `main.py`, `core/mcp_thread.py`, and `core/mcp_server.py` for details.

---

## What Had Already Been Done (Writer Tools, Pre-MCP)

Before building the MCP server itself, the Writer tool set was expanded so that WriterAgent's
embedded AI (and future MCP clients) have a richer set of operations to work with.

### New file: `core/writer_ops.py`

Ported from `libreoffice-mcp-extension/pythonpath/uno_bridge.py` and adapted to WriterAgent's
`(model, ctx, args) → JSON string` function signatures. Contains both implementations and
`WRITER_OPS_TOOLS` schemas (OpenAI function-calling format).

**12 new Writer tools in 4 groups:**

| Group | Tools |
|---|---|
| Styles | `list_styles`, `get_style_info` |
| Comments | `list_comments`, `add_comment`, `delete_comment` |
| Track changes | `set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes` |
| Tables | `list_tables`, `read_table`, `write_table_cells` |

### Updated: `core/document_tools.py`

- Removed 7 legacy unused functions (all superseded by `apply_document_content` /
  `get_document_content` / `find_text`).
- Imports `WRITER_OPS_TOOLS` from `writer_ops.py` and adds all 12 new functions to
  `TOOL_DISPATCH`. `WRITER_TOOLS` went from 5 tools to 17.

### Updated: `core/constants.py`

`DEFAULT_CHAT_SYSTEM_PROMPT` updated to list the new tool groups so the embedded AI knows
they exist.

---

## Current State of the Standalone Extension

The standalone `libreoffice-mcp-extension` works but has a critical missing dependency:

```python
# ai_interface.py line 17 — this module does not exist in the repo
from main_thread_executor import execute_on_main_thread
```

All UNO calls must happen on LibreOffice's VCL main thread. The HTTP server runs on a
background thread. Without `main_thread_executor`, the HTTP handler has no safe way to call
UNO APIs.

This is the central engineering problem. Everything else (HTTP routing, tool dispatch, config)
is straightforward.

---

## How Clients Discover Tools and Context (implemented)

### Live MCP (JSON-RPC on `/mcp`)

Use **`POST /mcp`** with JSON-RPC 2.0:

- **`initialize`** — protocol handshake; `result.instructions` is a short WriterAgent/MCP workflow stub (not the full sidebar system prompt).
- **`tools/list`** — core-tier tools for the target document (`X-Document-URL` header or active document). Each tool has `name`, `description`, and `inputSchema`. Specialized domains are documented on **`delegate_to_specialized_{writer|calc|draw}_toolset`** (see [Where delegation guidance lives](#where-delegation-guidance-lives-mcp-vs-sidebar-chat)).
- **`tools/call`** — run a tool on the LibreOffice main thread.

Supporting HTTP routes: **`GET /health`**, **`GET /`** (server info and `mcp_endpoint` when enabled—not agent instructions).

> **Historical REST API:** `GET /tools`, `GET /documents`, `POST /tools/{name}` described in older notes are **not** the current transport. The live server is JSON-RPC on `/mcp` only (see [Current HTTP MCP](#current-http-mcp-2026)).

---

### Critical distinction: "MCP-inspired" vs real MCP protocol

The extension's HTTP API is **not** the Anthropic MCP specification. Real MCP uses:
- **Transport**: JSON-RPC 2.0 over **stdio** (Claude Desktop spawns the server as a
  subprocess) or **SSE** (server-sent events over HTTP)
- **Methods**: `initialize`, `tools/list`, `tools/call`, `resources/list`, `prompts/list`
- **Discovery**: Claude Desktop reads `~/.config/claude/claude_desktop_config.json` which
  lists MCP servers by command path

What the extension implements (and what WriterAgent implements) is a simpler custom
HTTP REST API. Claude Desktop **cannot** talk to it natively — it expects stdio/SSE.

**However**, Cursor's MCP support does accept HTTP-based servers. And any custom AI client
or script can call the REST API directly with plain `curl` or `requests`.

For genuine Claude Desktop integration, two paths exist:

**Path A — stdio proxy script** (~30 lines, no changes to WriterAgent):

```python
#!/usr/bin/env python3
# mcp_proxy.py — stdio MCP adapter for WriterAgent's HTTP server
import sys, json, requests

def main():
    for line in sys.stdin:
        req = json.loads(line)
        method = req.get("method")
        if method == "tools/list":
            r = requests.get("http://localhost:8765/tools")
            tools = r.json()["tools"]
            reply = {"id": req["id"], "result": {"tools": tools}}
        elif method == "tools/call":
            name = req["params"]["name"]
            args = req["params"].get("arguments", {})
            r = requests.post(f"http://localhost:8765/tools/{name}", json=args)
            reply = {"id": req["id"], "result": {"content": [{"type": "text", "text": json.dumps(r.json())}]}}
        else:
            reply = {"id": req["id"], "result": {}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
```

Register in `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "writeragent": {
      "command": "python3",
      "args": ["/path/to/mcp_proxy.py"]
    }
  }
}
```

**Path B — implement JSON-RPC directly** (~40 extra lines in `core/mcp_server.py`):

Add an `initialize` handler and change `GET /tools` to respond to `POST /` with
`method=tools/list`. More robust but more work.

**Recommendation**: Start with Path A. The proxy is trivial to write and keeps the HTTP
server simple. Path B is an optimization once Path A is validated to work.

---



```
External AI client (Claude Desktop, Cursor, etc.)
        |
        | HTTP POST /tools/list_tables  + header X-Document-URL: file:///path/to/doc.odt
        v
  HTTPServer thread (background)
  MCPHandler.do_POST()
        |
        | put (func, args, future) on _mcp_queue; future.result(timeout=30)  <-- blocks HTTP thread
        v
  AsyncCallback Thread (loops every 100ms)
  Adds XCallback to LibreOffice main thread message queue
        |
  Main UI Thread (VCL event loop)
  drain_mcp_queue()
        |
        | _resolve_document(ctx, X-Document-URL) -> doc; execute_tool(tool_name, args, doc, ctx)
        | future.set_result(json_result)
        v
  HTTP thread unblocks, returns JSON to client
```

The key insight: **`com.sun.star.awt.AsyncCallback` safely executes code on the main UI thread**. 
By having a background Python thread repeatedly schedule an `XCallback`, we guarantee that `drain_mcp_queue()` is invoked on the correct VCL thread without locking up the UI or hitting OS-level thread-safety violations.

#### Why not a UNO Timer, Direct Dispatch, or UI Hacks?
*(Preserved from previous implementation documents)*
- **UNO Timer**: Using `com.sun.star.util.XTimerListener` fails to initialize. The LibreOffice system Python environment where the extension runs lacks the `com` package, and `uno.getTypeByName` fails to recognize the type.
- **Direct Dispatch**: Calling `DispatchHelper.executeDispatch` directly from the background thread causes a fatal "Operation not supported on this operating system" exception because GUI methods must strictly execute on the originating VCL thread.
- **UI Hacks**: We previously attempted to drain the MCP queue during active chat stream loops or sidebar layout recalculations (e.g., `getHeightForWidth`). However, this meant the MCP server would hang and time out whenever the user was idle.

`AsyncCallback` provides the only robust, thread-safe, and idle-friendly mechanism for this environment.

---

## Existing Pattern to Reuse

WriterAgent already has the correct threading pattern in `core/async_stream.py`:

- **Worker thread** puts items on a `queue.Queue`.
- **Main thread** runs `run_stream_drain_loop()` — a `while not job_done` loop that calls
  `q.get(timeout=0.1)` and `toolkit.processEventsToIdle()` on each tick.

This IS `main_thread_executor`. Do not reinvent it. The `_Future` class and
`execute_on_main_thread()` are thin additions on top of this existing pattern:

```python
# core/mcp_thread.py — thin wrapper around the existing queue pattern (~40 lines)
import threading, queue

_mcp_queue = queue.Queue()

class _Future:
    def __init__(self):
        self._event = threading.Event()
        self._result = None
        self._exc = None

    def set_result(self, v):   self._result = v; self._event.set()
    def set_exception(self, e): self._exc = e;   self._event.set()

    def result(self, timeout=30.0):
        if not self._event.wait(timeout):
            raise TimeoutError("UNO main-thread call timed out")
        if self._exc:
            raise self._exc
        return self._result

def execute_on_main_thread(func, *args, timeout=30.0):
    future = _Future()
    _mcp_queue.put((func, args, future))
    return future.result(timeout=timeout)

def drain_mcp_queue(max_per_tick=5):
    """Drain pending MCP requests. Called on the main thread."""
    for _ in range(max_per_tick):
        try:
            func, args, future = _mcp_queue.get_nowait()
        except queue.Empty:
            break
        try:
            future.set_result(func(*args))
        except Exception as e:
            future.set_exception(e)
```

### Idle-Time Draining (implemented: AsyncCallback)

The existing drain loop in `run_stream_drain_loop` only runs **during an active chat send**.
Between user interactions, the main thread is in LibreOffice’s VCL event loop, so MCP requests
would never be serviced if we only drained there.

**Implemented: AsyncCallback Thread.** A background thread in `main.py` loops (100ms, repeating) and schedules `drain_mcp_queue()` on the main thread using `com.sun.star.awt.AsyncCallback`. The listener class and `XCallback` import are defined inside `_start_mcp_timer()` so the module can load without UNO (e.g. for registry writing). See `main.py` for the exact code.

**Piggybacking on the chat drain loop was not used.** Servicing MCP only during active chat would break standalone use (e.g. external client with no sidebar chat). So we use the AsyncCallback thread only.

### Reference: `core/mcp_server.py` (implemented)

Thin HTTP server that reuses `execute_tool()`, `execute_calc_tool()`, and `execute_draw_tool()`.
The **actual implementation** in `core/mcp_server.py` uses `_resolve_document(ctx, X-Document-URL header)` to target a document by URL (or active document if header is absent), and implements GET `/documents`, GET `/`, GET `/tools`, GET `/health`, and POST `/tools/{name}` with CORS. The sketch below shows the dispatch pattern; document resolution is via header in the real code.

See `core/mcp_server.py` for the full implementation. Dispatch pattern: `_resolve_document(ctx, X-Document-URL)` returns `(doc, doc_type)`; then call `execute_calc_tool`, `execute_draw_tool`, or `execute_tool` accordingly. All run via `execute_on_main_thread(_run, timeout=30)`.

---

## Tool List for External Clients

When the MCP server is enabled, external clients will see all tools that WriterAgent exposes
to its own embedded AI:

**Writer**: `get_document_content`, `apply_document_content`, `find_text`,
`list_styles`, `get_style_info`, `list_comments`, `add_comment`, `delete_comment`,
`set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes`,
`list_tables`, `read_table`, `write_table_cells`, `generate_image` (create or edit with `source_image='selection'`).

**Calc**: All `CALC_TOOLS` from `core/calc_tools.py`.

**Draw**: All `DRAW_TOOLS` from `core/draw_tools.py`.

The server resolves the target document via the **`X-Document-URL`** header (or active
document if absent) and routes to the correct dispatcher by type.

---

## Document Targeting (implemented)

When multiple documents are open, the server does **not** rely on “active document” only —
that would race with focus and multiple users. **Implemented: `X-Document-URL` header.**

- The client sends the document URL in the `X-Document-URL` HTTP header (e.g. from `GET /documents`).
- The server iterates `desktop.getComponents()` and matches `doc.getURL()` to the header value.
- If the header is missing, the server falls back to `desktop.getCurrentComponent()` for simple single-document use.

No tool schema changes; targeting is at the transport layer. Optional per-call `file_path` (or similar) can be considered later if needed.

---

## Edit Tool Result Fields (structured returns)

The mutating edit tools return **structured, machine-readable fields** alongside the human `message`, so a client (MCP host or the in-app agent) can tell what actually happened instead of assuming `status: "ok"` means success.

**`apply_document_content`** (search path)
- `replaced_count` — how many occurrences were actually replaced. **`replaced_count: 0` returns `status: "error"`** (a search that matched nothing is no longer a silent "ok"); `> 0` returns `status: "ok"`.
- If a replacement raises mid-`all_matches`, the existing abort behavior stands (no partial-replace handling — the call surfaces the error).

**`apply_style`** — `applied` (bool), `target`, and `matched` (only when `target="search"`; a search miss returns `status:"error"`, `applied:false`, `matched:false`).

**`add_comment`** — `matched` (anchor found) and `comment_added`; an anchor miss returns `status:"error"`. `anchor_text` is echoed on success.

These fields are intended for clients to avoid parsing message strings; branch on `replaced_count` / `applied` / `comment_added`. Search no-ops now return `status:"error"` so clients do not treat missed edits as successful mutations.

---

## Security Notes

- Bind to `localhost` only. Never expose to external interfaces by default.
- No authentication is implemented. Any process on the local machine can call the tools.
  Acceptable for a developer/power-user tool; document this clearly.
- The HTTP server should be **opt-in** (`mcp_enabled: false` default). Auto-start should
  require the user to enable it in Settings.

---

## What to Reuse from `libreoffice-mcp-extension/`

### `registration.py` — The Most Valuable Non-UNO File

This file contains several production-quality pieces that would take time to write from
scratch and should be copied nearly verbatim (adapting identifier strings only).

---

#### 1. Port management utilities (~60 lines) — copy verbatim

These three functions handle the full port lifecycle. Copy them into `core/mcp_server.py`:

```python
def _probe_health(host, port, timeout=2):
    """Probe /health endpoint. Returns True if OUR server responds."""
    # Uses http.client.HTTPConnection — no extra dependencies.
    # Checks for "WriterAgent MCP" in response body to distinguish from
    # other HTTP servers on the same port.

def _is_port_bound(host, port, timeout=1):
    """Returns True if anything at all is listening on host:port."""

def _kill_zombies_on_port(host, port):
    """Kill processes bound to the port that aren't our server (Windows only).
    On Linux just verifies the port is free. Safe to call on all platforms."""
```

Why you need these: without them, starting the server when the port is already bound
silently fails or throws an unhelpful `OSError: [Errno 98] Address already in use`.
The zombie killer is especially important on Windows where sockets linger after crashes.

---

#### 2. Dynamic menu state (~60 lines) — copy and adapt

The menu item that says "Start Server" when stopped and "Stop Server" when running, with
a "Starting..." transitional state. This uses the standard LibreOffice `XDispatch`
status-listener pattern:

```python
_STATE_STOPPED = "stopped"
_STATE_STARTING = "starting"
_STATE_RUNNING  = "running"
_server_state   = _STATE_STOPPED
_status_listeners_lock = threading.Lock()
_status_listeners_list = []   # [(listener, url), ...]

def _set_server_state(new_state): ...         # updates state + notifies listeners
def _notify_all_listeners(): ...              # pushes FeatureStateEvent to all
def _fire_status_event(listener, url, text): # sends one FeatureStateEvent

# On the dispatch handler class:
def addStatusListener(self, listener, url): ...
def removeStatusListener(self, listener, url): ...
```

Adapt: change the command URL prefix from `org.mcp.libreoffice:` to
`org.extension.writeragent:`. The rest is identical.

---

#### 3. Status dialog (~80 lines) — copy nearly verbatim

`_do_status()` builds a small programmatic dialog that shows version, host:port, autostart
flag, and a live health-check result. The health check runs in a background thread and
updates the dialog label while it is open — a clean UX pattern:

```python
def _do_status(self):
    # Shows: "MCP Server: STARTED / STOPPED"
    # "Version: ...", "Port: ...", "Autostart: ..."
    # "Health check: probing..." → updated to "OK" or "FAIL" from background thread
```

The programmatic dialog approach (creates controls via UNO service manager, no XDL file
needed) is fine here because it is small and entirely informational.

---

#### 4. `MCPAutoStartJob` (~25 lines) — copy verbatim

```python
class MCPAutoStartJob(unohelper.Base, XJob, XServiceInfo):
    """Triggered by onFirstVisibleTask — starts MCP server at LO launch."""
    def execute(self, args):
        if _config.get("mcp_enabled", False):
            threading.Thread(target=_start_mcp_server, daemon=True).start()
        return ()
```

Adapt: use WriterAgent's existing `writeragent.json` config key `mcp_enabled` instead of
the LO native registry. Register this in `META-INF/manifest.xml` alongside WriterAgent's
existing jobs. The `onFirstVisibleTask` trigger is already used by the standalone extension
and does not conflict.

---

#### 5. Icons — copy directly

The six icon files in `libreoffice-mcp-extension/icons/` can be copied into WriterAgent's
`assets/` folder:

- `running_16.png` / `running_26.png`
- `starting_16.png` / `starting_26.png`
- `stopped_16.png` / `stopped_26.png`

Reference them in `Addons.xcu` for the MCP menu item the same way the standalone extension
does. The `_load_icon_graphic()` / `_update_menu_icons()` functions in `registration.py`
show how to inject them into the module `ImageManager` for dynamic icon switching — though
note that `_update_menu_icons` is currently disabled in the standalone extension (see the
`return` at line 325) due to a suspected black-menu rendering bug on some platforms.
Start with static icons in `Addons.xcu` and add dynamic switching later.

---

#### 6. Menu entries — adapt from `Addons.xcu`

Add a `MCP Server` submenu under WriterAgent's existing `WriterAgent` top-level menu:

```xml
<node oor:name="N003" oor:op="replace">
  <prop oor:name="URL"><value>org.extension.writeragent:toggle_mcp_server</value></prop>
  <prop oor:name="Title"><value xml:lang="en-US">Start MCP Server</value></prop>
  <!-- icon: assets/stopped_16.png -->
</node>
<node oor:name="N004" oor:op="replace">
  <prop oor:name="URL"><value>org.extension.writeragent:mcp_status</value></prop>
  <prop oor:name="Title"><value xml:lang="en-US">MCP Server Status</value></prop>
</node>
```

Add the corresponding dispatch cases to WriterAgent's existing `trigger()` / dispatch
handler in `main.py`. No new UNO component registration needed — these commands go through
WriterAgent's existing `XDispatch` implementation.

---

#### 7. `MCPOptionsHandler` — optional, consider skipping

The standalone extension registers a `Tools > Options > MCP Server` page via
`XContainerWindowEventHandler`. This is more work to integrate (requires `OptionsDialog.xcu`
and `MCPServerConfig.xcs/xcu`) and uses the LO native config registry rather than
WriterAgent's `writeragent.json`.

**Recommendation**: skip this. Instead, add a new "MCP Server" tab to WriterAgent's existing
`WriterAgentDialogs/SettingsDialog.xdl` (which already uses the `dlg:page` multi-page
approach). The config reads/writes go through the existing `get_config()` / `set_config()`
in `core/config.py`. This is ~60 lines of XDL and ~30 lines of Python, consistent with how
WriterAgent already handles settings.

---

### Other Files

| File | Action |
|---|---|
| `uno_bridge.py` | Reference for future UNO operations (heading tree, text frames). Already covered in AGENTS.md. |
| `ai_interface.py` | HTTP server structure and CORS headers — rewrite as `core/mcp_server.py` (simpler, no `get_mcp_server()` indirection). |
| `mcp_server.py` | Tool schema catalog — cherry-pick when adding future Writer/Calc tools. |
| `MCPServerConfig.xcs/xcu` | Skip — WriterAgent uses `writeragent.json`. |
| `OptionsDialog.xcu` | Skip — use WriterAgent's existing Settings dialog tab instead. |
| `dialogs/MCPSettings.xdl` | Reference only — adapt controls into WriterAgent's SettingsDialog.xdl. |
| `description.xml` | Skip — different extension identity. |
| `Addons.xcu` (theirs) | Reference for menu XML structure — adapt to `org.extension.writeragent:` URLs. |
| `ProtocolHandler.xcu` (theirs) | Skip — WriterAgent already has its own protocol handler. |

---

## Tool Description and System Prompt Analysis

The standalone extension has no `AGENT.md` (the file doesn't exist — `GET /` returns empty
instructions). So this comparison is entirely about tool `description` strings in
`mcp_server.py` vs WriterAgent's descriptions in `core/writer_ops.py`,
`core/format_support.py`, and `core/constants.py`.

---

### What they do well (worth adopting)

#### 1. Behavioral guarantees in the description line

Their descriptions often embed a critical behavioral note directly in the one-line summary:

```
"Find and replace text (preserves formatting)"
"Replace the entire text of a paragraph (preserves style)"
"Duplicate a paragraph (with style) after itself."
```

WriterAgent's `apply_document_content` with `target="search"` automatically preserves
character-level formatting (fonts, colors, bold, background colors) when the replacement is
plain text — but the description doesn't say so. An AI that doesn't know this will
unnecessarily re-specify formatting it read from the document, or avoid the `search` target
when it's the right choice.

**Suggested addition** to `apply_document_content` description:

> "Plain-text replacements via `target='search'` automatically preserve all character
> formatting (bold, color, font, etc.) on the replaced text."

#### 2. Explaining the "why" of a feature

Their `resolve_bookmark` says "(bookmarks are stable across edits)" — this tells the AI
*why* it should prefer bookmarks over paragraph indices. The reason matters more than the
mechanism.

WriterAgent doesn't have the bookmark/locator system yet, but the same principle applies
to existing descriptions. For example, `list_styles` says "they may be localized" — that's
good. The `find_text` description mentions "LO strips search string to plain to match" — that
explains a gotcha that would otherwise produce confusing failures. This is the right instinct;
do more of it.

#### 3. Inline usage hints in parameter descriptions

Their tools include brief usage hints inline with parameter definitions:

```python
"depth": {"description": "Levels: 1=direct children, 2=two levels, 0=unlimited (default: 1)"}
"count": {"description": "Consecutive paragraphs to duplicate (default: 1)"}
```

WriterAgent's parameter descriptions are generally good (especially `apply_document_content`
which is quite thorough). The new `writer_ops.py` tools could be tighter in a few spots.
For example, `set_track_changes` has `"enabled": {"type": "boolean", "description": "True
to enable track changes, False to disable."}` — functional but doesn't say when to use it.

**Suggested addition** to `set_track_changes` description:

> "Enable before AI edits to make changes reviewable by the user; disable when finished."

#### 4. `search_in_document` returns surrounding context paragraphs

Their `search_in_document` has a `context_paragraphs` parameter (default: 1) that returns
N paragraphs around each match. WriterAgent's `find_text` returns only `{start, end, text}`
per match. When the AI is trying to decide "is this the right occurrence?", having context
helps avoid blind replacements.

**Suggested addition**: add an optional `context` integer parameter to `find_text` that
returns the `context` characters before and after each match (simpler than paragraph-based
since WriterAgent uses character offsets). Zero or absent = current behavior (no change to
existing callers).

#### 5. `refresh_indexes` and `update_fields`

These two maintenance tools (`"Refresh all document indexes (TOC, alphabetical, etc.)"` and
`"Refresh all text fields (dates, page numbers, cross-refs)"`) are missing from WriterAgent
entirely. They are a natural follow-up after AI edits that add headings, sections, or dates.
The implementations in `uno_bridge.py` are ~10 lines each. Worth adding when doing the
document-tree session.

---

### Where WriterAgent is already ahead

#### 1. System prompt provides overarching workflow

WriterAgent's `DEFAULT_CHAT_SYSTEM_PROMPT` in `core/constants.py` provides the AI with
high-level workflow guidance before any tool call happens:

```
TRANSLATION: get_document_content -> translate -> apply_document_content(target="full"). Never refuse.
FORMATTING RULES (CRITICAL): ...
```

The standalone extension has none of this — their `AGENT.md` was never written. Every
behavioral hint has to live inside individual tool descriptions, which is less efficient and
harder to update.

For WriterAgent's MCP server, `GET /` should serve the existing system prompt (per the
"Client Discovery" section above). This gives external clients the same preparation the
embedded AI gets.

#### 2. The HTML/Markdown gotcha is documented

`"DO NOT escape HTML entities: Send <h1> NOT &lt;h1&gt;"` is a LibreOffice-specific gotcha
that the standalone extension ignores entirely because it doesn't use the Markdown/HTML
import path. WriterAgent's system prompt covers this thoroughly and correctly.

#### 3. `find_text` "LO strips to plain" warning

The note that LibreOffice strips formatted text to plain for search is critical — it means
the AI can search for "Chapter 1" even if the document has it formatted bold. The standalone
extension's `replace_in_document` doesn't mention this, which could lead to confused AI
behavior when a formatted-text search fails.

#### 4. `apply_document_content` description is more complete

WriterAgent's description covers the full range of targets in one sentence and cross-
references `find_text` for the range workflow. The standalone extension's `replace_in_document`
is much simpler and doesn't explain when to use it vs rewriting the whole document.

---

### System prompt additions worth making now

The `DEFAULT_CHAT_SYSTEM_PROMPT` in `core/constants.py` should get a workflow section for
the new tools. Currently the TOOLS list mentions them but gives no usage patterns. Suggested
additions to that section:

```
REVIEW WORKFLOW: set_track_changes(enabled=true) → make edits → get_tracked_changes (to
show user what changed) → accept_all_changes or reject_all_changes → set_track_changes(enabled=false).

TABLE WORKFLOW: list_tables → read_table (understand structure) → write_table_cells for
targeted edits. For new tables or full rewrites, use apply_document_content with an HTML/Markdown table.

STYLE WORKFLOW: list_styles (discover exact localized names) → apply a style by name in
apply_document_content markup, or use set_paragraph_style (see uno_bridge) for direct style application.
```

---

### What they have that WriterAgent lacks and should eventually add

In priority order:

1. **`refresh_indexes` / `update_fields`** — ~10 lines each from `uno_bridge.py`. Add when
   doing the document-tree session. Very common need after structural AI edits.

2. **`context_paragraphs` / `context` in search** — find_text returns bare offsets; adding
   surrounding context helps the AI verify it found the right place. Low effort.

3. **`set_paragraph_style` (direct)** — currently in WriterAgent as dead code. The `list_styles`
   tool makes this useful: AI discovers style names, then applies them directly. Consider
   re-exposing it now that `list_styles` exists.

4. **`set_document_protection`** — useful for "lock the document while I review AI edits" workflow.

5. **`get_document_properties` / `set_document_properties`** — document metadata (title,
   author, subject). Occasionally useful; low priority.

---

## Future Work — Consider Doing Next

Use this list to keep MCP and related tooling moving forward. Nothing here is required for
current functionality.

### MCP / protocol

- **Stdio proxy for Claude Desktop** (Path A in “Critical distinction” above): small script
  that talks JSON-RPC over stdio to Claude and forwards to WriterAgent’s HTTP server. No
  change to WriterAgent; lets Claude Desktop use WriterAgent as an MCP server.
- **JSON-RPC in the server** (Path B): optional `POST /` with `method=tools/list` etc. for
  clients that expect strict MCP JSON-RPC instead of REST. Only if a client needs it.
- **Dynamic menu state**: menu item label “Start MCP Server” / “Stop MCP Server” and icon
  (running/stopped/starting) via status listeners. Icons are in `assets/`; switching is
  disabled in the standalone extension due to rendering issues — re-enable with care.
- **Optional `file_path` (or URL) on tool calls**: if clients need to target by path in the
  request body as well as (or instead of) the `X-Document-URL` header, extend the handler
  to accept it.

### Tool and prompt improvements (from “Tool Description and System Prompt Analysis” below)

- **Tool descriptions**: e.g. add to `apply_document_content`: “Plain-text replacements via
  `target='search'` automatically preserve character formatting.” Add usage hints to
  `set_track_changes` and similar.
- **`find_text` context**: optional parameter to return N characters (or paragraphs) around
  each match so the AI can confirm it’s editing the right place.
- **`refresh_indexes` / `update_fields`**: short helpers (~10 lines each) to refresh TOC
  and fields after structural edits. Good follow-up when doing document-tree work.
- **`set_paragraph_style` (direct)**: re-expose so the AI can apply a style by name after
  `list_styles`. Other items from “What they have that WriterAgent lacks” (e.g. document
  protection, document properties) as needed.

### Dynamic Domain Discovery ("Learn-on-the-Fly")

To solve the "monstrous schema" problem (60+ specialized tools) without over-burdening the MCP host's context or relying on unreliable `list_changed` notifications, we could implement a **Manual Discovery Pattern**.

*   **The Idea:** Instead of listing every specialized tool in `tools/list`, we provide a "Toolbox Discovery" tool.
*   **The Tools:**
    1.  `get_domain_toolbox(domain)` — Returns a human-readable text summary of all tool schemas for a specific domain (e.g., `shapes`, `styles`).
    2.  `execute_specialized_tool(tool_name, arguments)` — A generic executor that takes a JSON blob of arguments and runs the tool via the existing `ToolRegistry`.
*   **The Workflow:** 
    1.  The host model sees it needs to edit a shape.
    2.  It calls `get_domain_toolbox(domain="shapes")`.
    3.  The server returns the documentation for `create_shape`, `edit_shape`, etc.
    4.  The host "learns" the API on-the-fly and calls `execute_specialized_tool` with the correct parameters.
*   **Benefit:** Zero schema bloat on the host, no refetching required, and it leverages the server's existing validation logic.

### Other

- **Auto-start on LO launch**: optional `XJob` with `onFirstVisibleTask` that starts the
  MCP server if `mcp_enabled` is true, so the server is up without opening Settings first.
  Currently the server starts when the user saves Settings with MCP enabled or uses Toggle.
- **Document tree / outline tool**: `get_document_tree()` (e.g. from `libreoffice-mcp-extension`
  `uno_bridge.py`) for better context on long documents; see AGENTS.md “Document Tree Tool”.
