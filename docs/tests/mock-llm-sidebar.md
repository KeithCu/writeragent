# Mock LLM Sidebar Test Plan & Reference

This document is the comprehensive reference for **automated** Rich Text Control Sidebar tests against the local mock LLM server (`make test-mock-sidebar`): server configuration, trigger phrases, harness hooks, and CI packets B–G.

Visual rendering, scroll pin, theme, resize, and “watch the sidebar” cases are **not** part of this harness. Use the product with `make mock-llm` if you care about those.

---

## 1. Executive Status Dashboard

Packets **B through G** run via `testing_runner`. There is no Packet A or H.

This plan is **finished** except one optional follow-up: **Calc deck URP hang** (E12 / G17). Mouse Stop, HITL Change dialog, live DuckDuckGo, and F11/F18 wait mismatches are dropped — not a backlog.

| Packet | Focus Area | Mode | Status |
|:------:|------------|:----:|--------|
| **[Packet B](#packet-b--stop-drain-loop-sendrecord-fsm)** | Stop button, drain loop, Send/Record FSM | Automated (CI) | **Done.** 16 Landed (incl. B13). |
| **[Packet C](#packet-c--empty--truncated-model)** | Empty / truncated model responses & banners | Automated (CI) | **Done.** 4 Landed. |
| **[Packet D](#packet-d--reasoning-vs-content)** | Reasoning deltas (`[Thinking]`) vs HTML content | Automated (CI) | **Done.** 4 Landed. |
| **[Packet E](#packet-e--tools-delegate-hitl-context-refresh)** | Tool loop, nested delegate, HITL, context refresh | Automated (CI) | **Done.** 17 Landed. Optional: E12 Calc hang. |
| **[Packet F](#packet-f--http--sse-errors-and-hangs)** | HTTP 4xx/5xx errors, socket hangs, SSE quirks | Automated (CI) | **Done.** 15 Landed. |
| **[Packet G](#packet-g--mocked-audio-and-stt)** | Mocked Record / Stop Rec, `input_audio`, STT | Automated (CI) | **Done.** 20 Landed. Optional: G17 same Calc hang as E12. |



---

## 2. Mock LLM Server & Test Execution

### 2.1. Server Setup & CLI Options

To run the local OpenAI-compatible mock LLM server:

```bash
make mock-llm
# Or run directly with custom options:
# .venv/bin/python scripts/mock_llm_server.py --delay-ms 30 --offline
# Soak Stop:     .venv/bin/python scripts/mock_llm_server.py --delay-ms 40 --scenario ramble
# Nested Stop:   .venv/bin/python scripts/mock_llm_server.py --delay-ms 80 --sync-delay-ms 8000
# Soak errors:   .venv/bin/python scripts/mock_llm_server.py --fail hang --fail-after-chunks 4
```

- **Default Endpoint:** `http://127.0.0.1:18766` (MCP uses ports `8765` / `18765`).
- **Settings Configuration:** In LibreOffice Settings, configure endpoint `http://127.0.0.1:18766`, text model `writeragent-mock`, and enable **Rich Text Control Sidebar**.
- **Audio / STT:** The server advertises `input_audio` support on chat completions and lists `writeragent-mock-whisper` for STT. The endpoint `POST /v1/audio/transcriptions` returns canned text (default: `"Hello from the mock microphone."`, configurable via `--transcript`).
- **Librarian / Smolagents Support:** Phrase matching inspects the `### CURRENT QUERY:` suffix so recovery turns (e.g. `hello` after `crash the stream`) do not match prior conversation history.

### 2.2. Trigger Phrase Master Table

The mock server matches incoming user queries (case-insensitive, first match wins) to specific behaviors:

| Phrase / Trigger | Server Behavior |
|------------------|-----------------|
| `hello` | Streams two HTML paragraphs with rotating templates (bold, lists, tables, `<pre>`). |
| `look up …` | Calls `web_research` (then executes smol search loop). |
| `comment` | Calls `add_comment` (or empty-doc `apply_document_content`). |
| `keep talking` / `ramble` / `stop me` | Streams ~200 content chunks (for testing **Stop**). |
| `say nothing` / `empty reply` | No content, `finish_reason=length` → `[Response truncated -- the model ran out of tokens...]`. |
| `empty finish stop` / `blank stop reason` | No content, `finish_reason=stop` → `[No text from model; any tool changes were still applied.]` + `[Debug: …]`. |
| `content filter` / `filtered reply` | No content, `finish_reason=content_filter` → `[Content filter: response was truncated.]`. |
| `think out loud` | Emits several `delta.reasoning` chunks, followed by HTML content. |
| `think tags` | Emits XML `<think>` markers inside `content`. |
| `reasoning details` | Emits `reasoning_content` + `reasoning_details`, followed by HTML content. |
| `fill the sidebar` / `very long` | Emits 40 paragraphs + HTML table + nested lists. |
| `outline this` / `use the writer toolset` | Calls `delegate_to_specialized_writer_toolset` (`document_research`). |
| `empty nested answer` | Specialized delegate emits inner `final_answer` with an empty `answer`. |
| `endless nested outline` | Specialized delegate loops without finishing until `max_tool_rounds`. |
| `mixed tools` / `one tool fails` | Calls `add_comment` (empty search → error) + `apply_document_content` filler. |
| `two tools` / `in parallel` | Calls `search_in_document` + `get_document_tree` in a single round. |
| `insert filler` / `append a paragraph` | Calls `apply_document_content` to mutate the document end. |
| `list sheets` / `list pages` | Calls Calc/Draw list tools (`list_sheets` / `list_pages`) when advertised. |
| `crash the stream` / `error 500` | Returns HTTP 500 JSON error payload. |
| `rate limit` / `error 429` | Returns HTTP 429 Rate Limit error. |
| `error 401` / `unauthorized` | Returns HTTP 401 Unauthorized error. |
| `error 403` / `forbidden` | Returns HTTP 403 Forbidden error. |
| `hang the stream` | Emits a few SSE chunks, then drops the TCP socket without sending `[DONE]`. |
| `sse pings` | Emits `: ping` comments between SSE data events (`--sse-comments` forces this). |
| `event ping` | Emits `event: ping` named SSE events between `data:` lines. |
| `malformed sse` | Emits invalid `data: {not json}`, followed by a valid stream + `[DONE]`. |
| `truncated json` | Emits incomplete `data: {`, followed by a valid stream + `[DONE]`. |
| `two dones` | Emits a normal stream followed by two `data: [DONE]` lines. |
| `empty body` | Returns HTTP 200 with `Content-Length: 0`. |
| `connection reset` | Closes socket before sending HTTP status line. |

### 2.3. Automated Test Execution (`make test-mock-sidebar`)

Run scripted tests using `make test-mock-sidebar`. Tests run out-of-process against a live LibreOffice instance via URP.

```bash
make test-mock-sidebar                 # Run all automated packets (F, B, C, D, E, G)
make test-mock-sidebar FILTER=B        # Run Packet B (Stop & Send/Record FSM)
make test-mock-sidebar FILTER=C        # Run Packet C (Empty/truncated responses)
make test-mock-sidebar FILTER=D        # Run Packet D (Reasoning vs content)
make test-mock-sidebar FILTER=E        # Run Packet E (Tools & HITL)
make test-mock-sidebar FILTER=F        # Run Packet F (HTTP/SSE errors)
make test-mock-sidebar FILTER=G        # Run Packet G (Mocked audio & STT)
make test-mock-sidebar FILTER=b13      # Run a single case by ID
make test-mock-sidebar FILTER="B E"    # Run multiple packets
```

#### GitHub Actions (PR CI option)

[`.github/workflows/pr-ci.yml`](../../.github/workflows/pr-ci.yml) exposes this as a **Run workflow** checkbox, default off. Pull requests never set it.

From the Actions tab → **PR CI** → **Run workflow**:

| Input | Meaning |
|-------|---------|
| `os` | Ubuntu / macOS / Windows / `all` (same picker as the rest of PR CI) |
| `test_mock_sidebar` | Off (default): typecheck + pytest/UNO + packaging. On: skip those and run `make test-mock-sidebar` only |
| `ci_debug` | Off (default). On: `WRITERAGENT_CI_DEBUG=1` — per-worker nodeid trail, faulthandler dump at 240s, `--max-worker-restart=0`, leftover process dump, upload `ci-debug-<os>-<run_id>`. Does **not** fix the Windows hang; it is how we get the crashed-worker nodeid. |
| `pytest_serial` | Off (default). On: `PYTEST_WORKERS=0` (no xdist). Use with `ci_debug` when you want a serial traceback. |

Linux starts a virtual framebuffer (`xvfb-run` + `dbus-run-session`) because `testing_runner` refuses `--user-profile` without `DISPLAY`. WriterAgent is still built and registered first.

#### Test Runner Architecture & UNO Invariants
- **Bootstrap:** LibreOffice is launched via `Popen` with `--norestore --writer --accept=socket,host=127.0.0.1,port=<port>;urp;` (TCP socket). `officehelper.bootstrap` is avoided because its `--nodefault` flag can cause GUI crashes.
- **Crash Recovery:** `--norestore` suppresses document recovery dialogs that would block the URP pipe.
- **Sidebar Deck Activation:** Tests dispatch `.uno:SidebarDeck.WriterAgentDeck` to show the deck. When already visible, `showDecks` / `XDeck.activate` is used to prevent accidental toggling.
- **Thread Guard:** Dev builds set `WRITERAGENT_UNO_THREAD_GUARD=0` in the child process so URP deck dispatch can initialize `ChatPanel`.
- **Out-of-Process URP:** Live `SendButtonListener` runs inside `soffice`. Tests drive actions via `uno_click`, query text manipulation, and polling `Enabled` properties. Do not call `processEventsToIdle()` directly on the URP bridge.

### 2.4. Test Harness & Hooks Reference

Debug test hooks live in [`plugin/chatbot/sidebar_test_hooks.py`](../../plugin/chatbot/sidebar_test_hooks.py) and [`tests/chatbot/mock_llm_harness.py`](../../tests/chatbot/mock_llm_harness.py). (These hooks are omitted in release builds).

| Hook | Description | Target Use Case |
|------|-------------|-----------------|
| `sidebar_panel()` / `send_listener()` | Retrieves active `SendButtonListener` after deck initialization | Base listener access |
| `set_query_text(s)` | Sets query text (`Text = s`) and fires `TEXT_UPDATED` | Initiating sends |
| `press_send()` | Dispatches `SEND_CLICKED` / Send button action | Starting chat stream |
| `press_stop()` | Dispatches `STOP_CLICKED` (ActionEvent path) | Cancelling stream (Windows/standard) |
| `press_stop_mouse()` | Calls `notify_stop_mouse_pressed(send_listener)` | GTK mouse-press path (B1b) |
| `pump_until(pred, timeout)` | Pumps idle events until predicate matches or timeout | Waiting for SSE arrival |
| `transcript_contains(s)` | Checks if rich or plain transcript contains substring | Verifying output banners / HTML |
| `send_state()` | Inspects `is_busy`, button labels (`Send`, `Stop`, `Record`, `Accept`, etc.) | FSM verification |
| `wait_idle()` | Waits until `is_busy is False` and not recording | Inter-test synchronization |
| `next_hello_ok()` | Sends `hello`, waits for idle, asserts valid assistant reply | **Mandatory test closer** |
| `mock_config(**flags)` | Reconfigures mock server dynamically (`delay_ms`, `offline`, `fail`) | Dynamic scenario setup |
| `press_record()` | Dispatches `RECORD_CLICKED` | Starting audio recording |
| `press_stop_rec()` | Dispatches `STOP_REC_CLICKED` | Stopping audio recording |
| `inject_wav(path or bytes)` | Injects mock WAV file to simulate child audio capture | Audio testing without microphone |
| `stub_recorder_child()` | Fakes IPC: `{"status":"ready"}` without opening hardware device. `hang_ready=True` never emits ready (G21 timeout) | Audio init vs recording state |
| `set_audio_supported(bool)` | Overrides `SendButtonState.audio_supported` | Audio support gating (G8, STT) |
| `audio_status()` | Returns `AudioRecorderState.status` and `has_audio` | Audio state assertions |
| `press_accept()` | Fires Send action when label is `Accept` | HITL approval |
| `press_change()` / `press_reject()` | Fires Stop listener `Change` / `Reject` branch | HITL change / rejection |
| `approval_active()` | Checks if `_approval_event is not None` | HITL state verification |

### 2.5. Universal Invariants & Pass/Fail Criteria

Every test must satisfy:
1. **LibreOffice Stays Alive:** No crash, no UI deadlock, and no `NestedDrainOwnerError`.
2. **Terminal State Cleanliness:** `is_busy` becomes `False`; Send button is re-enabled for text entry.
3. **Session Recoverability:** `next_hello_ok()` passes at the conclusion of the test.
4. **Clean Queue Types:** Background-to-UI streaming uses `StreamQueueKind` enum members, never bare strings.

---

## 3. Unified Packet Test Suites

---

### Packet B — Stop, Drain Loop, Send/Record FSM

- **Focus:** Cancelling streams while worker holds SSE socket, drain exit on main thread, `SendButtonState` transitions, latching stop state.
- **Mode:** Automated (`make test-mock-sidebar FILTER=B`).
- **Status Summary:**
  - **Landed / OK:** B1a, B1c, B2, B3, B3b, B6, B7, B9, B10, B11, B13, B14, B15, B16, B19, B21.
  - **Dropped (not backlog):** B1b (mouse), B8 (`TEXT_UPDATED` over URP), B5 (resize), B17, B18, B20, B22, B23. B4/B12 live in Packet G.

| ID | Mode | Mock / Trigger | Steps / Actions | Expected Pass Behavior | Status / Notes |
|:--:|:----:|----------------|-----------------|------------------------|:--------------:|
| **B1a** | CI | `--delay-ms 40`, `keep talking` | Pump ≥1 chunk, call `press_stop()` | Transcript has `[Stopped by user]` (not replaced by `No response.`); `is_busy` becomes False; `next_hello_ok()` succeeds | **OK / Landed** |
| **B1b** | CI | Same as B1a | Cancel using `press_stop_mouse()` | Same as B1a via GTK mouse listener path (`STOP_CLICKED (mousePressed)`) | **Dropped** (mouse hook) |
| **B1c** | CI | B1a scenario | Assert state after Stop | Rich tail is NOT re-pasted as full HTML of the ramble; no `_copy_formatted…` after stop | **OK / Landed** |
| **B2** | CI | Ramble | Call `press_stop()`, then immediately `press_send()` | Single in-flight send; no stuck "Starting…"; `_active_q` not dual-owned; `next_hello_ok()` | **OK / Landed** |
| **B3** | CI | Ramble | Call `press_stop()` twice quickly | Second click is a no-op; no exceptions; `next_hello_ok()` | **OK / Landed** |
| **B3b** | CI | Idle state | Call `press_stop()` while idle | No crash; button labels unchanged; Send still functional | **OK / Landed** |
| **B4** | CI | Empty query, venv configured | Record → Stop Rec without speaking | Button transitions Record ↔ Stop Rec ↔ Send; no send if empty | **Dropped** (Packet G) |
| **B6** | CI | Default | Call `press_send()` twice rapidly | FSM rejects second send (`is_busy`); exactly one stream; `next_hello_ok()` | **OK / Landed** |
| **B7** | CI | Empty query | Send with empty query and no audio | No `StartSendEffect`; zero HTTP requests sent to mock | **OK / Landed** |
| **B8** | CI | Empty ↔ text | `TEXT_UPDATED` empty ↔ nonempty transitions | Send enabled only when text is present | **Dropped** (URP `TEXT_UPDATED`) |
| **B9** | CI | Ramble | Allow stream to finish naturally (no Stop), wait idle | Button returns to Send; subsequent Stop on a second ramble works | **OK / Landed** |
| **B10** | CI | Ramble | Stop stream; `next_hello_ok()`; ramble + Stop again | Stop functions multiple times across a single panel lifetime | **OK / Landed** |
| **B11** | CI | Ramble | Stop stream; assert query box state | Query text restored as designed; no focus steal after stop | **OK / Landed** |
| **B12** | CI | Empty query | `RECORD_CLICKED` then `STOP_REC_CLICKED` (no WAV) | Returns to Send; no chat POST; typed hello works | Handled in **Packet G** |
| **B13** | CI | High `delay-ms` | Call `press_send()`, Stop **before** first SSE chunk | Cancels starting state cleanly; not stuck in Stop; no `LLM request lane lock` | **OK / Product Fix Landed** |
| **B14** | CI | `think out loud`, delay | Stop during `[Thinking]` stream | Thinking banner cleared/frozen; Stopped banner shown; `next_hello_ok()` | **OK / Landed** |
| **B15** | CI | Multi-step serial | hello → ramble + Stop → `say nothing` → hello | Four distinct terminal states; never gets stuck busy | **OK / Landed** |
| **B16** | CI | `add_comment` (tool-only), high delay | Stop **before** tool executes | Comment never added to document; `is_busy` becomes False; `next_hello_ok()` | **OK / Landed** |
| **B19** | CI | `insert a comment` on empty doc | Stop after first tool result (sequential rounds) | Second tool never runs; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **B21** | CI | Ramble | Click `controls["clear"]` during stream | Greeting visible; Stop still enabled; press Stop → idle; `next_hello_ok()` | **OK / Landed** |

#### Dropped Cases (Packet B)
- **B1b (GTK mouse Stop):** Same FSM as B1a (`press_stop()`). Not worth an in-process mouse hook.
- **B8 (Send enabled iff query nonempty):** Needs URP `TEXT_UPDATED` sync; covered enough by B7 (empty send) in practice.
- **B5 (Resize during stream):** Visual-only; no URP assertion. Use the product if you care about repaint.
- **B17 (Stop vs STREAM_DONE race):** Non-deterministic over URP; covered by B1a (mid-stream) and B9 (natural end).
- **B18 (Stop × 5 soak):** Redundant with B10 (cancelling twice in one panel lifetime).
- **B20 (Stop during UpdateDocumentContext):** Not observable over URP; covered by E5.
- **B22 (Clear after Stop):** Folded into B21.
- **B23 (Stop after STREAM_DONE):** FSM is already idle (covered by B3b).

#### Technical Scratchpad: B13 Fix Details
- **Issue:** Stopping before/during early SSE closed the socket, but `streaming_loop` continued reading while `finally: response.read()` blocked for `request_timeout`, holding `llm_request_lane`.
- **Resolution:** Latched `_stopped` in `LlmClient.stop()`, changed loop to `break` on stop, skipped body drain when stopped, ensured late `register_client` triggers `stop()`, and cleared stop state on next UI send.

---

### Packet C — Empty / Truncated Model

- **Focus:** Correct surfacing of `finish_reason=length`, `finish_reason=stop`, `finish_reason=content_filter`, preventing stale HTML re-render.
- **Mode:** Automated (`make test-mock-sidebar FILTER=C`).
- **Status:** **4 Landed** (C1, C3, C4, C5; C2 folded into C1).

| ID | Mode | Mock / Trigger | Steps / Actions | Expected Pass Behavior | Status / Notes |
|:--:|:----:|----------------|-----------------|------------------------|:--------------:|
| **C1** | CI | `say nothing` | Send `say nothing` | Suffix shows `[Response truncated -- the model ran out of tokens...]` (not Debug banner); `next_hello_ok()` succeeds | **OK / Landed** |
| **C2** | CI | Follow up to C1 | Send `hello` after C1 | Normal HTML chat recovery; verified by C1 `_hello_ok()` | **Folded into C1** |
| **C3** | CI | Scenario `empty` | Send `round one`, `round two`, `round three` | Truncated banner each round; transcript does not accumulate garbage HTML; `next_hello_ok()` | **OK / Landed** |
| **C4** | CI | `empty finish stop` | Send `empty finish stop` | Shows `[No text from model; any tool changes were still applied.]` plus `[Debug: … finish_reason='stop'…]`; `next_hello_ok()` | **OK / Landed** |
| **C5** | CI | `content filter` / `filtered reply` | Send `content filter` | Suffix shows `[Content filter: response was truncated.]` (not length or Debug banner); `next_hello_ok()` | **OK / Landed** |

---

### Packet D — Reasoning vs Content

- **Focus:** Separation of `delta.reasoning` (`[Thinking]`) from final HTML body, handling XML `<think>` tags, preventing reasoning from leaking into tool parameters.
- **Mode:** Automated (`make test-mock-sidebar FILTER=D`).
- **Status:** **4 Landed** (D1–D4).

| ID | Mode | Mock / Trigger | Steps / Actions | Expected Pass Behavior | Status / Notes |
|:--:|:----:|----------------|-----------------|------------------------|:--------------:|
| **D1** | CI | `think out loud`, `delay_ms=80` | Send query, poll `[Thinking]` while busy, wait idle | `[Thinking]` visible mid-stream; replaced by mock HTML body after idle; `decided_tools == []`; `next_hello_ok()` | **OK / Landed** |
| **D2** | CI | `think tags` | Send query, wait idle | Final transcript contains HTML body without raw `<think>` or `</think>` tags; `next_hello_ok()` | **OK / Landed** |
| **D3** | CI | `reasoning details` | Send query, poll `[Thinking]`, wait idle | Mid-stream `[Thinking]` displayed; clean HTML body on completion; `decided_tools == []`; `next_hello_ok()` | **OK / Landed** |
| **D4** | CI | `think out loud` then `look up cats` | Complete thinking turn, then send research query | Prior reasoning is NOT injected into `tool_calls` history; `last_assistant_tool_calls == []`; `next_hello_ok()` | **OK / Landed** |

---

### Packet E — Tools, Delegate, HITL, Context Refresh

- **Focus:** Executing UNO mutations on the UI thread during drain, nested agent delegation, Human-in-the-Loop (HITL) approval, document context refresh.
- **Mode:** Automated (`make test-mock-sidebar FILTER=E`).
- **Status Summary:**
  - **Landed:** E1, E3, E4, E5, E6, E7, E8a, E9, E9a, E9b, E9e, E10, E11, E13, E14, E15, E17, E21, E22.
  - **Optional later:** E12 (Calc URP hang — same as G17).
  - **Dropped:** E2 (live net), E8b/E9d (mouse), E9c (Change dialog), E16, E18, E19, E20, E23, E24.

| ID | Mode | Mock / Trigger | Steps / Actions | Expected Pass Behavior | Status / Notes |
|:--:|:----:|----------------|-----------------|------------------------|:--------------:|
| **E1** | CI | `--offline`, `look up latest Python` | Send query | Smol research steps execute; HTML summary in transcript; mock sees `### CURRENT QUERY:`; `next_hello_ok()` | **OK / Landed** |
| **E2** | CI | `look up …` (online) | Send query with live DuckDuckGo | Single `web_search` → `visit_webpage` → HTML wrap-up (no infinite loop) | **Dropped** (live network; E1 offline covers the loop) |
| **E3** | CI | Document with text "Welcome…", `add a comment` | Send query | Comment anchored on first word; log shows `add_comment`; sidebar mentions comment; `next_hello_ok()` | **OK / Landed** |
| **E4** | CI | **Empty** doc, `insert a comment` | Send query | Two-round loop: `apply_document_content` then `add_comment`; doc nonempty; `next_hello_ok()` | **OK / Landed** |
| **E5** | CI | `insert filler` | Send query | Paragraph appended; next turn's system prompt includes updated length (`refresh_document_context`); `next_hello_ok()` | **OK / Landed** |
| **E6** | CI | `two tools` / `in parallel` | Send query | Both `search_in_document` and `get_document_tree` execute in one turn; single HTML summary; `next_hello_ok()` | **OK / Landed** |
| **E7** | CI | `outline this` | Send query | Calls `delegate_to_specialized_writer_toolset`; inner discovery executes; returns canned outline; `next_hello_ok()` | **OK / Landed** |
| **E8a** | CI | `outline this`, `delay_ms=80`, `sync_delay_ms=8000` | Call `press_stop()` during nested POST | Nested agent stops; `is_busy` becomes False; log shows `resolve_stop_checker`; `next_hello_ok()` | **OK / Landed** |
| **E8b** | CI | Same as E8a | Cancel with `press_stop_mouse()` | Same cancellation behavior via mouse listener | **Dropped** (mouse; E8a covers Stop) |
| **E9** | CI | Prompt for web research on | Send query triggering HITL approval | Send button label becomes `Accept`; Stop label becomes `Change` / `Reject`; `is_busy` remains True | **OK / Landed** |
| **E9a** | CI | E9 state | Call `press_accept()` | Approval clears; tool execution completes; button labels revert to Send/Stop; `next_hello_ok()` | **OK / Landed** |
| **E9b** | CI | E9 state | Call `press_reject()` | Approval clears; tool aborted; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **E9c** | CI | E9 state | Call `press_change()` | Triggers change dialog or applies modified query; must not log stream cancel | **Dropped** (dialog hook) |
| **E9d** | CI | E9 state | Call `press_stop_mouse()` | No stream cancellation; remains in `approval_active()` | **Dropped** (mouse; E9e covers ActionEvent Stop) |
| **E9e** | CI | E9 state | Call `press_stop()` ActionEvent | Dispatches Change/Reject branch, NOT `StopSendEffect` | **OK / Landed** |
| **E10** | CI | Tool follow-up returning 500 | Send tool query | Tool error displayed in transcript; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **E11** | CI | `insert filler` then `add a comment` | Send as two sequential turns | Both mutations applied; context refreshed between turns; `next_hello_ok()` | **OK / Landed** |
| **E12** | CI | Calc doc, `list sheets` | Send in Calc sidebar | `list_sheets` executes; HTML wrap-up | **Optional** (URP hang on `factory/scalc` even isolated `FILTER=e12`; GUI two-window Calc chat does not repro) |
| **E13** | CI | `add_comment` with mock tool delay | Call `press_stop()` during tool execution | Partial/no mutation; not stuck busy; no UI freeze; `next_hello_ok()` | **OK / Landed** |
| **E14** | CI | `outline this` | Run delegate twice in succession | Nested agent functions repeatedly without stale session leaks; `next_hello_ok()` | **OK / Landed** |
| **E15** | CI | `insert filler` | Stop after tool result queued, before HTML | Mutation applied; UI returns to idle; no double drain; `next_hello_ok()` | **OK / Landed** |
| **E17** | CI | `empty nested answer` | Specialized delegate returns empty answer | Main wrap-up displays clean empty banner; no stale HTML paste-over; `next_hello_ok()` | **OK / Landed** |
| **E21** | CI | `mixed tools` / `one tool fails` | Send query | `apply_document_content` succeeds while `add_comment` fails; mutation kept, error surfaced; `next_hello_ok()` | **OK / Landed** |
| **E22** | CI | `endless nested outline` | Specialized delegate never finishes | Triggers max tool budget error; main UI returns to idle; `next_hello_ok()` | **OK / Landed** |

#### Dropped Cases (Packet E)
- **E2 (Live DuckDuckGo):** CI must stay offline; E1 covers the research loop.
- **E8b / E9d (Mouse Stop / HITL):** Same as B1b; ActionEvent paths landed (E8a, E9e).
- **E9c (HITL Change dialog):** No URP dialog hook; Accept/Reject landed.
- **E16 (Unknown domain):** Covered by unit tests and E10.
- **E18 (Zero search hits):** Tool content variation, not a drain hang.
- **E19 (Tool validation error):** Covered by unit tests (`test_tool.py`).
- **E20 (Close doc mid-tool):** Kills shared soffice; out of harness (same as former H4).
- **E23 (Shorter-doc context refresh):** Same code path as E5.
- **E24 (In-process listener gap):** Same as dropped E9c.

---

### Packet F — HTTP / SSE Errors and Hangs

- **Focus:** Network errors (500, 429, 401, 403), socket drops mid-stream without `[DONE]`, malformed SSE lines, connection resets, timeout recovery.
- **Mode:** Automated (`make test-mock-sidebar FILTER=F`).
- **Status Summary:**
  - **Landed:** F1, F2, F3a, F4, F5, F6, F7, F8, F9, F10, F12, F13, F14, F15, F16, F17.
  - **Dropped:** F3b (mouse), F11 (two DONE wait), F18 (event ping), F19–F32 (unit-tested envelopes).

| ID | Mode | Mock / Trigger | Steps / Actions | Expected Pass Behavior | Status / Notes |
|:--:|:----:|----------------|-----------------|------------------------|:--------------:|
| **F1** | CI | `crash the stream` | Send query, then `hello` | Error surfaced in UI (not a hang); `next_hello_ok()` succeeds | **OK / Landed** |
| **F2** | CI | `rate limit` / `error 429` | Send query | Distinct 429 error displayed; prior assistant text not overwritten; `next_hello_ok()` | **OK / Landed** |
| **F3a** | CI | `hang the stream` or `--fail hang --fail-after-chunks 4` | Send query, wait timeout or call `press_stop()` | UI does not freeze; returns to idle or Stopped; `next_hello_ok()` succeeds | **OK / Landed** |
| **F3b** | CI | Hang scenario | Cancel with `press_stop_mouse()` | Same cancellation behavior via GTK mouse hook | **Dropped** (mouse; F3a/F17 cover Stop) |
| **F4** | CI | `sse pings` / `--sse-comments` | Send `hello` | Stream parses cleanly; comment lines ignored; HTML renders | **OK / Landed** |
| **F5** | CI | `--fail http500` (all requests) | Send query, then disable fail, send `hello` | Consistent error display; Settings remain accessible; recovery `hello` succeeds | **OK / Landed** |
| **F6** | CI | Ramble + hang (`--scenario ramble --fail hang`) | Send query, call `press_stop()` | Stops or surfaces error; soffice never wedges; `next_hello_ok()` | **OK / Landed** |
| **F7** | CI | `error 401` / unauthorized | Send query | Auth error message surfaced; `next_hello_ok()` succeeds | **OK / Landed** |
| **F8** | CI | `error 403` / forbidden | Send query | Forbidden error message surfaced; `next_hello_ok()` succeeds | **OK / Landed** |
| **F9** | CI | `malformed sse` (`data: {not json}`) | Send query | Skips invalid chunk or surfaces error; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **F10** | CI | `truncated json` (`data: {`) | Send query | Handles incomplete JSON chunk; completes or surfaces error; `next_hello_ok()` | **OK / Landed** |
| **F11** | CI | `two dones` | Send query | Handles double `[DONE]` lines cleanly | **Dropped** (transcript wait; parser covered in unit tests) |
| **F12** | CI | `empty body` (HTTP 200, 0 bytes) | Send query | Surfaces empty model or error banner; `next_hello_ok()` succeeds | **OK / Landed** |
| **F13** | CI | `connection reset` (closed socket) | Send query | Surfaces network error; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **F14** | CI | 429 followed by `hello` | Send 429 query, immediately send `hello` | Rapid recovery; no sticky rate-limit state; `hello` succeeds | **OK / Landed** |
| **F15** | CI | F1 (500) → F2 (429) → `hello` | Send sequential failing queries | Both errors visible in history; FSM stays healthy; `hello` succeeds | **OK / Landed** |
| **F16** | CI | Mock delay > client timeout | Send query | `ERROR_OCCURRED` event fired; Send button re-enabled; `next_hello_ok()` | **OK / Landed** |
| **F17** | CI | Hang scenario | Call `press_stop()` during F3 hang | Stream cancelled cleanly; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **F18** | CI | `event ping` | Send query with named SSE events | Named events ignored or parsed without crashing | **Dropped** (transcript wait; F4 covers SSE comments) |

#### Dropped Cases (Packet F)
- **F3b (Mouse Stop during hang):** F3a / F17 cover Stop.
- **F11 / F18:** URP transcript wait mismatch; SSE envelopes covered by unit tests and F4 (`: ping` comments).
- **F19–F32:** Redundant HTTP/SSE envelope cases (redirects, 204, Content-Length header edge cases, BOM, charset encoding, `Retry-After`). These are fully verified in unit tests ([`tests/framework/test_client_llm.py`](../../tests/framework/test_client_llm.py)) and do not require full UNO sidebar execution.

---

### Packet G — Mocked Audio and STT

- **Focus:** Dual state machines (`SendButtonState` vs `AudioRecorderState`), mock audio child process via IPC stub (`/tmp/writeragent_stub_recorder.json`), native `input_audio` chat completions, fallback to `/v1/audio/transcriptions` STT.
- **Mode:** Automated (`make test-mock-sidebar FILTER=G`).
- **Status Summary:**
  - **Landed:** G1–G16, G21, G27, G28, G29.
  - **Optional later:** G17 (same Calc URP hang as E12).
  - **Dropped:** G18 (HITL Record), G19–G26, G30.

| ID | Mode | Mock / Trigger | Steps / Actions | Expected Pass Behavior | Status / Notes |
|:--:|:----:|----------------|-----------------|------------------------|:--------------:|
| **G1** | CI | Native audio model, empty query | `press_record()` → stub ready → `press_stop_rec()` → inject WAV | Native chat POST includes `input_audio`; transcript displays canned line; `has_audio` cleared; `next_hello_ok()` | **OK / Landed** |
| **G2** | CI | Typed query + audio | Type `hello` then Record → Stop Rec | Assistant reply acknowledges both typed text and audio transcript; `next_hello_ok()` | **OK / Landed** |
| **G3** | CI | G1 flow | Inspect SQLite/JSON session history | Audio payload stripped from stored history; replaced with `[Audio Attached]`; no large base64 strings | **OK / Landed** |
| **G4** | CI | Silence auto-stop | Trigger host silence auto-stop via `STOP_REC_CLICKED` | Dispatches on main thread; completes native audio send; `next_hello_ok()` | **OK / Landed** |
| **G5** | CI | Text-only model (`audio_supported=False`) | Record → Stop Rec | Calls `POST /v1/audio/transcriptions` (STT); query populated with transcribed text then sent | **OK / Landed** |
| **G6** | CI | `--transcript Custom line.` | G1 flow | Sidebar transcript displays `"Custom line."` | **OK / Landed** |
| **G7** | CI | Ramble in flight | Call `press_record()` during stream | Action rejected (`is_busy`); stream continues; Stop still functions; no second worker | **OK / Landed** |
| **G8** | CI | `audio_supported=False` | Empty query box | Record button disabled/hidden; typed Send functions normally | **OK / Landed** |
| **G9** | CI | Active recording | Call `press_record()` twice | Second click is a no-op; recording continues; single child stub | **OK / Landed** |
| **G10** | CI | Idle state | Call `press_stop_rec()` while idle | No crash; button remains Send | **OK / Landed** |
| **G11** | CI | Active recording | Call `press_stop()` (stream cancel) | Does not trigger Stop Rec; cleanly cancels or ignores; `next_hello_ok()` | **OK / Landed** |
| **G12** | CI | Recording with failed stub | Trigger child crash / error event | `audio_status` transitions to error then idle; Send re-enabled; no stuck Stop Rec | **OK / Landed** |
| **G13** | CI | Record → Stop Rec | Mock chat POST returns 500 | Error surfaced in transcript; `has_audio` cleared; can record again; `next_hello_ok()` | **OK / Landed** |
| **G14** | CI | Missing / 0-byte WAV | Record → Stop Rec with missing WAV | Send aborted or error surfaced; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **G15** | CI | Active recording | Call `press_send()` while recording | FSM ignores Send; recording continues; Stop Rec then send succeeds | **OK / Landed** |
| **G16** | CI | Rapid retake | Record → Stop Rec → immediately Record again | Second recording replaces previous audio; single in-flight capture | **OK / Landed** |
| **G17** | CI | Calc deck | G1 flow in Calc sidebar | Native audio path functions on Calc deck | **Optional** (same Calc URP hang as E12) |
| **G18** | CI | HITL active | Call `press_record()` during HITL approval | Record ignored (approval owns button states); E9 flow remains valid | **Dropped** (HITL; E9 covers approval buttons) |
| **G21** | CI | Stub hang (`hang_ready`) | Record with child that never reports `ready` | Init timeout fires; `audio_status` error; returns to idle Send; `next_hello_ok()` | **OK / Landed** |
| **G27** | CI | STT empty text | STT returns empty string from valid WAV | Query stays empty; no chat send; returns to idle; `next_hello_ok()` | **OK / Landed** |
| **G28** | CI | STT error JSON | STT endpoint returns error payload | Error surfaced in UI; `has_audio` cleared; `next_hello_ok()` | **OK / Landed** |
| **G29** | CI | Native audio returns 400 | Chat POST with `input_audio` fails 400 (`fail_native_audio`) | Falls back to STT on same drain; surfaces `[Model does not support audio. Falling back to STT...]`; `next_hello_ok()` | **OK / Landed** |

#### Dropped Cases (Packet G)
- **G18 (Record during HITL):** E9 already owns the buttons; not worth a second HITL hook.
- **G19 (Child auto-stop JSON):** Covered by unit tests ([`test_audio_silence_detector.py`](../../tests/scripting/test_audio_silence_detector.py)) and G4.
- **G20 (Corrupted IPC line):** Covered by unit tests.
- **G22 (Silent child exit):** Variant of G12.
- **G23–G24 (Corrupt / 0-byte WAV):** Folded into G14.
- **G25–G26 (Late WAV timing races):** Flaky over URP; covered by unit tests.
- **G30 (Stale WAV cleanup):** Covered by G16.

---

## 4. Out-of-Scope Items & Boundary Invariants

The following areas are intentionally excluded from the mock LLM sidebar test suite:
- **Visual watching:** Scroll, HTML look, resize, theme, focus steal. Use `make mock-llm` in the product.
- **Killing the shared soffice:** Close/switch document mid-stream, exit while ramble.
- **Speech Recognition Accuracy:** Testing real microphone audio quality or ASR word-error rate (covered by external benchmarks).
- **Non-Chat Sidebar Workflows:** Librarian full-corpus indexing, brainstorm mode, presentation slide deck generation, and image generation.
- **Calc Formula Add-Ins:** `=PROMPT()` and `=PYTHON()` formula execution (tested separately in `tests/calc/`).
- **External MCP Server Testing:** Live third-party MCP server communication (mocked in `tests/mcp/`).