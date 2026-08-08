# Calc / MCP follow-ups from discussion #374 (design)

**Status:** easy fixes & post-ship review items shipped (see §0.1, §11); P1 column inheritance shipped.  
**Source:** [Discussion #374](https://github.com/KeithCu/writeragent/discussions/374) (bell07 + KeithCu).  
**Related shipped doc:** [calc-date-time-handling.md](calc-date-time-handling.md).  
**Out of scope here:** sidebar scroll / sticky thinking viewport (separate UX problem).

This document covers four remaining product bugs from the latest user report, answers two architecture questions (`initialize.instructions` visibility; `include_format_info`), and proposes **simple structural** directions rather than long “when to call `set_style`” prompt rules.

---

## 0. What already shipped (baseline)

| Release | What landed |
| :--- | :--- |
| 0.8.54 | Read enrichment: date/time cells → ISO (or `PT…` duration) in `value`, plus `type` / `format_category`. MCP clock string added to `initialize.instructions`. |
| 0.8.55 | Write ingestion: ISO / `PT…` → real Calc serials via `detectNumberFormat` / isodate; M1 preserve/apply for destination formats; coercion report. |

User confirmed: **reads and writes of dates/times work**. Remaining pain is context (clock), MCP schema friction, missing display-format visibility, and the model changing sheet style when adding a row.

### 0.1 Easy fixes shipped (post-review implementation)

Implemented without waiting for P1 (column format inheritance). Long feature **P1 remains open**.

| Item | What landed | Primary code |
| :--- | :--- | :--- |
| **Bug 2** | MCP-only widen: `formula_or_values` is `["string","array"]` on MCP `inputSchema`; OpenAI/Gemini schema stays `"string"` | [`tool.py`](../plugin/framework/tool.py) `to_mcp_schema` |
| **Bug 1** | Clock piggyback: `current_local_datetime` on `list_open_documents` and on `get_guidance()` with no topic (reuses `_format_mcp_clock_context`) | [`document_research_tools.py`](../plugin/doc/document_research_tools.py) |
| **Bug 3** | `format_code` (UNO `FormatString`) on temporally enriched `read_cell_range` cells; sidebar Calc selection uses `include_format_info=True` | [`inspector.py`](../plugin/calc/inspector.py), [`document_helpers.py`](../plugin/doc/document_helpers.py) |
| **P3** | `number_format` removed from `set_style` LLM/MCP schema; kept via `scripting_only_parameters` for the scripting API proxy | [`cells.py`](../plugin/calc/cells.py), [`tool.py`](../plugin/framework/tool.py), [`generate_tool_proxies.py`](../scripts/generate_tool_proxies.py) |
| **Guidance** | One sentence: prefer plain values/ISO for static cells; `=` only when the cell must stay live | [`cells.py`](../plugin/calc/cells.py) `WriteCellRange.description`, [`prompts.py`](../plugin/framework/prompts.py) |

**Still open (do separately):** P1 column / nearest-above format inheritance on write (Mechanism W for new empty rows).

Wire schema today (LLM `read_cell_range`):

```json
{"address": "A22", "value": "2026-08-06", "formula": null, "type": "date", "format_category": "date", "format_code": "TT.MM.JJ"}
```

`format_code` is **observability** only (not a re-apply path after P3). There is no separate `iso8601` field (older 0.8.54 responses had it; current contract folded ISO into `value`).

---

## 1. Bug inventory (latest comment)

| # | Symptom (bell07) | Surface |
| :---: | :--- | :--- |
| **1** | Model: “Let’s check the current date if possible. I don’t have it.” Wrote the wrong calendar day for “today/yesterday.” | MCP / Page Assist (also relevant to how we publish clock context) |
| **2** | `write_formula_range` rejected when `formula_or_values` was a JSON **array**; retry with a stringified array worked. Error looked like host schema validation. | MCP tool schema vs execute coerce |
| **3** | `read_cell_range` does not show the display format code (`TT.MM.JJ` / short year). Model cannot see how neighboring rows look. | Read wire / enrichment |
| **4** | Sidebar agent wrote a new row: used a formula where a constant would do; called `set_style`; date display went `08.08.26` → `08.08.2026`; number `5` → `5,00000`. | Write + `set_style` + missing “match this sheet” behavior |

Bugs 3 and 4 are tightly coupled: without seeing (or inheriting) column style, the model (or our write-path “apply detected format”) invents a new display style.

---

## 2. Bug 1 — Current date on the MCP path

### 2.1 Do we always send the clock?

**Yes, on every successful `initialize` response from LibreOffice.**

Implementation:

- [`plugin/mcp/mcp_protocol.py`](../plugin/mcp/mcp_protocol.py) `_mcp_initialize` → `wire_types.initialize_result(..., instructions=build_initialize_instructions(...))`.
- `build_initialize_instructions` always prepends `_format_mcp_clock_context(...)`, e.g.  
  `Current local date and time: Friday, 2026-08-07T11:04:25 (EDT).`
- Plus a short Calc hint: write ISO without offset/`Z`.

Sidebar chat is separate: [`llm_client.py`](../plugin/framework/client/llm_client.py) injects `Today's date is {Weekday}, {YYYY-MM-DD}.` into the first system message on **every** chat request. MCP does **not** go through that path for the host’s outer model.

Edge case already documented: if the stdio bridge answers `initialize` while LibreOffice is still down, the client may get **placeholder** instructions and never refresh them when LO comes up — only `tools/list` refreshes ([mcp-protocol.md](mcp-protocol.md) “Stale instructions”). A reconnect after LO is up is required for full instructions.

### 2.2 Do hosts *see* it? (the real question)

The MCP spec treats `InitializeResult.instructions` as an **optional hint**:

> This can be used by clients to improve the LLM's understanding of available tools… It can be thought of like a “hint” to the model. For example, this information **MAY** be added to the system prompt.

So:

| Layer | Guaranteed? |
| :--- | :--- |
| WriterAgent puts `instructions` on the wire | **Yes** (when LO-backed initialize succeeds) |
| Host stores `result.instructions` | Typical SDKs do (e.g. Vercel AI SDK exposes `client.instructions`) |
| Host injects that string into the **model** prompt | **Not required by the protocol** — `MAY` |

We have **not** verified Page Assist’s behavior in code. What we have is field evidence: after 0.8.54/0.8.55, Page Assist’s model still reasoned that it did not have the current date. That is consistent with either:

1. Host ignores / never forwards `instructions` to the model, or  
2. Host forwards them once at connect, but the model’s turn context drops or never includes them, or  
3. Host uses a tools-only binding and its own system prompt.

Until someone inspects Page Assist (or asks bell07 to dump whether the system prompt contains `Current local date and time:`), treat “instructions may not reach the model” as a **working hypothesis**, not a measured fact.

Later MCP specs (2026-07-28) move away from session `initialize` toward stateless discovery; `instructions` may live on `server/discover` instead. WriterAgent today still speaks the session/`initialize` shape ([wire_types.py](../plugin/mcp/wire_types.py)). Design should not assume forever-stable delivery of connection-time prose.

### 2.3 Is a `get_current_datetime` tool useful?

**Maybe — but it is not obviously the best or simplest fix.** Tradeoffs:

| Approach | Pros | Cons |
| :--- | :--- | :--- |
| **A. Keep only `initialize.instructions`** | Zero tools; already implemented | Hosts may never show it; clock freezes for the session; stale if bridge raced LO |
| **B. Dedicated `get_current_datetime` tool** | Model can fetch on demand (“I don’t have it” → call tool); works if host only binds tools | Extra core tool forever; models must *think* to call it; still fails if they don’t |
| **C. Piggyback on a tool models already call** (e.g. `list_open_documents` returns `current_local_datetime`) | No new tool name; high chance of appearing in early context | Easy to miss if the model skips that call; mixes concerns |
| **D. Put a fresh stamp in a tool description** (e.g. rewrite `write_formula_range` description at `tools/list` time) | Visible in tools/list payload many hosts *do* put in context | Descriptions become dynamic/stale between list calls; noisy; odd for caching |
| **E. Sidebar-style injection for MCP is impossible from our side** | — | We are not the host’s LLM client; we cannot prepend to Page Assist’s system prompt |

**Design lean (for review):** Prefer proving whether Page Assist surfaces `instructions` before adding a tool. If it does not, prefer **C** (stamp on `list_open_documents` and/or the empty-topic `get_guidance()` index) over **B**, unless we want an explicit “ask the clock” affordance. A dedicated tool is only clearly justified if hosts ignore every ambient channel and models already look for a date tool (bell07’s first comment requested one).

**Do not** invent a second clock string format — reuse `_format_mcp_clock_context` (offset-free wall ISO) wherever the stamp is published.

### 2.4 Open questions for Bug 1

1. Does Page Assist (Firefox) put `initialize.instructions` into the model context? (Ask user / inspect addon.)
2. If yes, why did the model still claim it lacked the date — truncation, session reconnect with placeholder, or model blindness?
3. Should the stamp refresh more than once per session (tool / list_open_documents) even when instructions work?

---

## 3. Bug 2 — `formula_or_values` array vs string (MCP schema)

### 3.1 What happened

Host called roughly:

```json
{
  "formula_or_values": ["2026-08-07", "09:00:00", ...],
  "range_name": ["A26:L26"]
}
```

Host/schema layer rejected with “Received tool input did not match expected schema.” Model retried with a **string** containing a JSON array and succeeded.

### 3.2 Current code

In [`WriteCellRange`](../plugin/calc/cells.py):

- JSON Schema declares `"type": "string"` (comment: Gemini-friendly).
- `execute` **already** accepts `list` / number and coerces (`list` → `json.dumps`).
- Failure is **before** execute: the MCP host validates arguments against `tools/list` → `inputSchema`.

Both [`to_mcp_schema`](../plugin/framework/tool.py) and [`to_openai_schema`](../plugin/framework/tool.py) run `_normalize_schema_for_strict_providers`, which **collapses** `"type": ["string", "array"]` to `"array"` (asserted in [`tests/framework/test_tool_schema_convert.py`](../tests/framework/test_tool_schema_convert.py)). A naive source-schema change to `["string","array"]` is therefore **not** widen — it silently becomes array-only on every wire. Real widen (MCP-only or everywhere) must preserve that union through normalization, or skip collapse for this property.

### 3.3 Design options

| Option | Idea |
| :--- | :--- |
| **A. MCP-only widen** | Keep OpenAI/Gemini schema as `string`. In `to_mcp_schema`, advertise `["string","array"]` (with `items`) and **do not** collapse that union for MCP. Execute coerce stays. |
| **B. Widen everywhere** | Declare `["string","array"]` on `WriteCellRange` so **both** MCP and OpenAI/chat schemas keep the union (string = fill-all; array = per-cell; no length-1 fill-all redefinition). Adjust `_collapse_union_type` (or skip collapse for this property) so the union is not forced to array-only. Smallest *uniform* fix; `execute` already coerces lists. Main risk: strict providers (Gemini) rejecting the union on the sidebar path — smoke-test `write_formula_range` after the collapse change. |
| **C. Array everywhere + length-1 fill-all** | Schema becomes array-only; redefine “one element = fill whole range” so fill-all survives. Larger behavior change than widen. |
| **D. Description-only** | Tell models to stringify. Cheap; does not stop hosts that emit native arrays from failing validation. |

**Design lean:** **A** if we want to avoid Gemini risk; **B** is the better try when we want one contract on all surfaces and are willing to smoke-test the sidebar with Gemini after preserving `string\|array` through normalize. Lean **A** exists precisely to avoid that Gemini risk — not because widen-everywhere is larger in the write path (it is not).

### 3.4 Tests (when implemented)

- Schema unit: `formula_or_values` allows array on the chosen wire(s) (MCP for A; MCP + OpenAI for B).
- For **B**: assert normalize/`to_openai_schema`/`to_mcp_schema` preserve `["string","array"]` rather than collapsing to `"array"`.
- Existing execute list coerce tests unchanged.
- Do not treat “source type is a union” alone as success — without preserving the union through collapse, the wire is array-only and fill-all via string is gone.

---

## 4. Bug 3 — Format codes on read + `include_format_info`

### 4.1 What the user asked for

```text
read_cell_range →
  {"address":"A22","value":"2026-08-06","type":"date","format_category":"date"}

Wanted: also the format code (e.g. German TT.MM.JJ / short year).
```

They compared to rows above; without format codes, the model cannot tell `08.08.26` from `08.08.2026` from ISO display.

### 4.2 What `include_format_info` actually is

**It is not an LLM-facing tool parameter.** The model never passes it.

| Caller | Flag | Why |
| :--- | :--- | :--- |
| Chat/MCP tool `read_cell_range` ([cells.py](../plugin/calc/cells.py)) | **Always `True`** | LLM wire contract (ISO / duration) |
| Internal `CellInspector.read_range` default | **`False`** | Raw Calc serials for NumPy, DuckDB ingest, `=PY`, analysis |
| `get_calc_context_for_chat` selection CSV | **Default `False`** | Sidebar selection still dumps **raw serials** today — a related gap |
| Performance probe / unit tests | Both | Measure cost of format walk |

So: for the LLM tool path we **already always enrich** when the cell has a date/time/duration number format. The flag exists so **internal numeric pipelines** are not rewritten to strings.

“Always provide enriched data if relevant” is already the tool policy. The open product question is **what else** enrichment should include (`format_code`, number formats, etc.), not whether the tool should opt in.

Enrichment is also gated by a preflight: if the range has no date/time formats and no formulas, the format walk can return early ([inspector.py](../plugin/calc/inspector.py) `_range_format_rows`). That is a performance feature, not a user toggle.

### 4.3 Design options for format visibility

| Option | Idea | Notes |
| :--- | :--- | :--- |
| **A. Add `format_code` on temporally enriched cells** | Expose UNO `FormatString` next to `format_category` | Directly answers the user; localized letters (`TT` vs `DD`) — models must not invent ASCII codes for other locales |
| **B. Add `format_code` for any non-General format in the range** | Also covers `0` vs `0.00000` | Helps Bug 4’s number regression; larger payloads |
| **C. Separate `read_cell_formats` tool** | Keep `read_cell_range` value-focused | Extra round-trip; models often skip second tools |
| **D. Return display string + ISO** | e.g. `display: "08.08.26"` | Nice for humans; still does not give a code to re-apply via `set_style` |

**Design lean:** Prefer **A**, and strongly consider **B** for the same format-group walk (we already touch format keys for temporal classification — attaching `FormatString` is cheap once the group is loaded). Keep `include_format_info=False` for internal float pipelines (do **not** remove the flag; do **not** enrich DuckDB/`=PY` paths).

Caveat from [calc-date-time-handling.md](calc-date-time-handling.md) §6: format letters are locale-specific. Exposing `format_code` is for **observation / copy**, not for teaching the model to hardcode `YYYY-MM-DD` into `queryKey`.

### 4.4 Open questions for Bug 3

1. Is `format_code` enough, or do we also want a `display` string (what the user sees)?
2. Should sidebar `get_calc_context_for_chat` selection use enrichment (`True`) so the first context block is not raw serials?
3. Payload policy: omit `format_code` when General / empty?

---

## 5. Bug 4 — New row broke formatting (and “don’t call set_style” is a weak fix)

### 5.1 What went wrong (mechanism)

Two different mechanisms can change display; the report likely hit **both**:

**Mechanism W — Write into General / empty cells (our code path)**  
M1 ([calc-date-time-handling.md](calc-date-time-handling.md) S14 / M1) **preserves** a destination format only when that cell already has a category-compatible temporal format. A **new empty row** is typically General → M1 **applies** the key from `detectNumberFormat`. That key is locale-preferred and often a **full-year** date, not the sheet’s short `TT.MM.JJ` from the rows above. So even a “perfect” ISO write can change how the column looks for the new row.

**Mechanism S — Model called `set_style`**  
[`set_style`](../plugin/calc/cells.py) exposes `number_format` as an ordinary optional string. Calling it with something like `dd.mm.yyyy` or a number format with decimals **replaces** the cell’s format key. Other style props (bold, color, …) do **not** wipe number formats; only a truthy `number_format` argument does. The `5` → `5,00000` symptom is almost certainly Mechanism S (or an applied number format), not ISO write.

**Mechanism F — Formula vs constant**  
Leading `=` skips the date gate and goes to the formula overlay. Using `=TODAY()` / complex `IF` when the user asked to “set” a value is a policy/prompt issue, not a Calc serial bug.

### 5.2 Why “prompt the model not to call set_style” is a poor primary fix

- Easy to get wrong or ignore under tool pressure.
- Does not fix Mechanism W (detected format on empty cells).
- Couples product quality to prompt compliance.
- Confusing: when *should* the user/agent change number formats? Rarely for “add a row like the others.”

Prompts can be a thin backup, not the spine.

### 5.3 Simpler structural directions (preferred discussion)

Goal: **adding a row like the rows above should look like the rows above**, without the model needing format expertise.

| Option | Idea | Complexity | Addresses |
| :--- | :--- | :--- | :--- |
| **P1. Column / neighbor format inheritance on write** | When M1 would **apply** because destination is General/empty, instead **copy** the NumberFormat key from a natural template: same column previous non-empty data row, or dominant format in the column of the written range. Only fall back to `detectNumberFormat` when no template exists. | Medium; needs clear rules for mixed columns | Mechanism W (date short year, sheet look) without `set_style` |
| **P2. Expose `format_code` on read (Bug 3)** | Visibility so a careful model can re-apply | Small | Helps models; still relies on them calling `set_style` correctly |
| **P3. Remove `number_format` from `set_style` schema** | Remove the property from `SetCellStyle.parameters` entirely (not just the description — MCP hosts expose `inputSchema.properties` to models). Keep `CellManipulator` methods for internal/scripting callers. Fix scripting API to apply `number_format` directly via `CellManipulator` instead of through the tool registry. Three files: `cells.py`, `prompts.py`, `writeragent_api.py`. | Small | Mechanism S foot-gun |
| **P4. `write_formula_range` optional `match_formats_from`** | e.g. copy formats from `A22:L22` onto `A26:L26` after values | Small API surface | Explicit template; model must pass it |
| **P5. Long prompt rules** | “Don’t call set_style(number_format) unless…” | Small | Weak; reject as primary |

**Design lean for review:**

1. **P1 as the general simple product fix** for “new row should match the sheet.” It extends the existing M1 idea (“prefer existing style”) from *same cell* to *column template when the cell has no style yet*. That matches user expectation better than “apply whatever Calc detects for ISO.”
2. **P3** as the foot-gun reduction for Mechanism S (or rename so number format is not a casual sibling of `bold`).
3. **P2** as observability (Bug 3), useful for debugging and for rare intentional format edits — not as the main preservation strategy.
4. Keep formula-vs-constant guidance **one short sentence** on `write_formula_range` only (constants for set values; `=` when the cell must stay live). No multi-paragraph `set_style` policy.

### 5.4 Sketch of P1 rules (to debate, not implement yet)

When `write_formula_range` commits a coerced temporal (or any numeric?) value and the destination category is non-temporal (General / `@` after S17 handling):

1. Look upward in the same column for the nearest non-empty cell with a NumberFormat key whose category is compatible with the input (reuse M1 compatibility matrix).
2. If found → **apply that key** (inherit), not `detectNumberFormat`’s key.
3. If not found → current behavior (detect / formatindex 43 for duration).
4. Do not inherit across sheet boundaries; do not invent format strings in Python.

Open details: empty cells between; header row exclusion; should inheritance apply to plain numbers too (fixes `5,00000` if caused by detect — usually not); interaction with S17 (`@` → must not keep showing raw serial).

### 5.5 Open questions for Bug 4

1. Is column inheritance (P1) acceptable when the column mixes formats?
2. Should `number_format` leave the default `set_style` tool entirely?
3. For “used Formula instead of values,” is a one-line tool description enough, or is that solely model/Page Assist behavior?

---

## 6. Cross-cutting: keep internal vs LLM wires separate

```text
                    ┌─────────────────────────────┐
  LLM tools         │ include_format_info=True      │  ISO / PT / (proposed format_code)
  read_cell_range   │ always for ToolBase path      │
                    └─────────────────────────────┘

                    ┌─────────────────────────────┐
  Internal          │ include_format_info=False     │  raw serial floats
  DuckDB, =PY,      │ default on CellInspector      │
  analysis, …       └─────────────────────────────┘
```

Do **not** collapse these into “always enrich everywhere.” NumPy / DuckDB / `=PY` need floats. Do consider enriching **sidebar Calc selection context**, which is LLM-facing but currently uses the internal default.

---

## 7. Recommended package (for later implementation — not approved)

Pending review; ordered by dependency:

1. **Bug 2:** MCP schema widen for `formula_or_values` (Option A) — clear win, low risk.  
2. **Bug 1:** Verify Page Assist `instructions` delivery; if missing, stamp on `list_open_documents` / `get_guidance()` before adding a dedicated clock tool.  
3. **Bug 3 + 4 spine:** Design/implement **column format inheritance on write (P1)**; add **`format_code` on read (P2/B)** for visibility; consider **removing or quarantining `number_format` on default `set_style` (P3)**.  
4. Skip long `set_style` prompt essays.  
5. Sidebar scroll: not in this doc.

---

## 8. Suggested verification (when someone implements)

- MCP: capture Page Assist (or a minimal MCP client) initialize payload and whether the model system prompt contains the clock line.  
- Schema: native array `tools/call` succeeds without stringify.  
- Read: German short-date cell returns a `format_code` containing day/month pattern (locale letters may be `T`/`J`).  
- Write: ISO date into empty row under a short-date column → display matches the column (inheritance), not full-year detect.  
- `set_style` without `number_format` still never changes NumberFormat (regression guard — already true).

---

## 9. Review — resolved questions and revised recommendations (2026-08-08)

**Reviewer context:** Page Assist source was inspected directly (GitHub `main`, `src/libs/mcp/`).
The original design document's open questions in §2.4 are now answered, and several
recommendations are adjusted accordingly.

### 9.1 Bug 1 — Page Assist does not forward `instructions` (confirmed)

**§2.4 question 1 is resolved.** Page Assist's `normal-chat.ts` builds the model's system
prompt from the user's configured prompt, selected prompts, memory context, and an optional
`extraSystemPrompt`. It never reads or injects the MCP `InitializeResult.instructions` field.
The `HttpOnlyMcpClient` (`http-client.ts`) connects via `openMcpServerConnection`, calls
`client.listTools()`, and discards everything else from the initialize handshake. The clock
string WriterAgent puts in `instructions` genuinely never reaches the model.

**Recommendation — prefer C (piggyback) over B (dedicated tool).**

The design doc's §2.3 lean toward C was right. A dedicated `get_current_datetime` tool adds a
permanent core-tier entry that every request pays for in schema tokens, and it still requires
the model to *decide* to call it. The piggyback approach is cheaper and more likely to reach the
model passively:

1. Add a `current_local_datetime` field (reusing `_format_mcp_clock_context`) to the
   `list_open_documents` response — models on the MCP path naturally call this tool early.
2. Include the same stamp in the `get_guidance()` no-topic index output.
3. Keep the existing `initialize.instructions` clock for hosts that do honor it.

If piggybacking proves insufficient in practice (models still claim they lack the date after
having called `list_open_documents`), then add a dedicated tool as a second step. Do not start
there.

**Rationale vs a dedicated tool:** The model that exhibited the "I don't have it" behavior would
have had access to `list_open_documents` output in its context if it had called it — and it
likely did, since MCP sessions typically start with a document listing. A clock stamp on that
response would have prevented the failure without adding schema weight. A dedicated tool only
helps when the model explicitly searches its tool list for a clock affordance, which is a
narrower scenario.

### 9.2 Bug 2 — MCP-only widen (agreed)

**Option A is correct.** No changes to the design doc's recommendation. The
`_collapse_union_type` trap is real and well-documented in §3.2; the MCP-only override after
normalization is the right scalpel.

This should be **implemented first** — lowest risk, highest confidence, no product ambiguity.

### 9.3 Bug 3 — start with temporal-only format codes (Option A, not B)

**Scale back to Option A** (temporal cells only) as the initial step, rather than Option B
(all non-General formats).

The user's complaint was specifically about date formats (`TT.MM.JJ` vs `TT.MM.JJJJ`). That
is purely a temporal-cell issue. Option B would also add `format_code` to currency cells,
percentage cells, custom number formats, etc. — data that is not needed for the reported
problem and increases every read payload for cells the model has no reason to act on.

The `5` → `5,00000` symptom from Bug 4 was caused by Mechanism S (`set_style` applying a bad
number format), not by the read path lacking visibility into number formats. Knowing a
number cell's format code does not prevent the model from calling `set_style` incorrectly —
that is a write-side structural problem (P1 / P3), not a read-side information problem.

If there is later demand for non-temporal format visibility (e.g., debugging currency columns),
Option B is a straightforward extension of the same code path. The format-group walk already
touches the keys; attaching `FormatString` is cheap.

**Sidebar enrichment is a separate, worthwhile fix** regardless of A vs B.
`get_calc_context_for_chat` currently uses `include_format_info=False`, so the sidebar's
first context block for a Calc selection sends raw serials to an LLM. Switching that to `True`
is a small change that closes a genuine gap.

### 9.4 Bug 4 — P1 agreed; P3 simpler than proposed

**P1 (column format inheritance):** Agreed. The §5.4 sketch is sound. One refinement:
use "nearest non-empty cell *above* in the same column with a compatible category" rather than
"immediately preceding cell." This handles empty rows between data sections (e.g., a blank
row after headers) without scanning arbitrarily far or across sheet boundaries.

Restrict inheritance to **temporal writes only** — do not extend to ordinary numbers. Plain
numeric writes into General cells stay General, which is usually correct (the user is not
expecting `5` to inherit a date column's format). The Mechanism W problem is specifically
that ISO temporal strings get a *detected* format that differs from the column's existing
short-date style.

**P3 (quarantine `number_format`):** The design doc's §5.3 proposed "move to a specialized /
rarely listed tool." That adds a new tool class, schema, tests, and documentation to solve a
foot-gun. A simpler approach:

**Remove `number_format` from `set_style`'s `parameters` dict and description entirely.**
MCP hosts expose `inputSchema.properties` directly to the model — hiding `number_format`
from only the description is not enough; models will still see and fill the property from
the schema. Full removal from `parameters` is required.

The internal methods `_set_number_format` / `_set_range_number_format` on `CellManipulator`
remain for programmatic callers (spreadsheet import, write-path format apply, etc.). The LLM
simply loses the ability to accidentally change number formats when asked to "make it bold."
If a user later needs agent-driven number-format changes ("format column B as currency"),
add a dedicated tool *then*, when there is concrete demand and a clear UX for it. This
follows the AGENTS.md principle of least complexity.

**Concrete implementation (three files):**

1. [`plugin/calc/cells.py`](../plugin/calc/cells.py) — remove `number_format` from
   `SetCellStyle.parameters["properties"]`. Remove from the description if mentioned.
   `execute` can drop any leftover `number_format` kwarg silently (or just not pass it to
   `manipulator.set_cell_style`).
2. [`plugin/framework/prompts.py`](../plugin/framework/prompts.py) — remove `number_format`
   from the `set_style properties` line in the Calc prompt.
3. [`plugin/scripting/writeragent_api.py`](../plugin/scripting/writeragent_api.py) — the
   scripting API `set_style` calls `_rpc_call("set_style", ...)`, which goes through the
   tool registry and validates kwargs against the schema. With `number_format` gone from the
   schema, `_rpc_call` would reject it via `ToolBase.validate` ("Unknown parameter"). Fix:
   if `number_format` is passed, pop it before the `_rpc_call`, then apply it directly via
   `CellManipulator._set_number_format` / `_set_range_number_format` in the host-process
   path. The venv-worker path (IPC) does not need this — `=PY()` scripts are the internal
   numeric pipeline and do not call `set_style(number_format=...)` in practice.

**Why not keep it in the schema but hide from descriptions only?** MCP `inputSchema` is the
model's primary affordance surface; description text is secondary. A property visible in the
schema will be used regardless of whether prose mentions it.

The alternative — keeping `number_format` on `set_style` but behind the specialized tier — is
also acceptable but more mechanism than removing one dict key.

### 9.5 Implementation order

Ordered by risk and dependency, matching the design doc's §7 but with the revised
recommendations:

1. **Bug 2** — MCP-only `formula_or_values` schema widen (Option A). **Shipped** (§0.1).
2. **Bug 1** — Stamp clock on `list_open_documents` output and `get_guidance()` index (Option C). **Shipped** (§0.1).
3. **Bug 3** — Add `format_code` on temporally enriched cells (Option A); enrich sidebar Calc
   selection context. **Shipped** (§0.1).
4. **Bug 4** — Column format inheritance on write (P1) — **still open**; remove `number_format` from `set_style`
   schema (simplified P3) and one-sentence formula-vs-constant guidance — **shipped** (§0.1).

### 9.6 Open questions resolved

| Original question | Resolution |
| :--- | :--- |
| §2.4 Q1: Does Page Assist forward `instructions`? | **No.** Confirmed by source inspection (2026-08-08). |
| §2.4 Q2: If yes, why did the model still lack the date? | Moot — instructions are never forwarded. |
| §2.4 Q3: Should the stamp refresh more than once per session? | Yes, via tool results (C). `list_open_documents` is called per-session or per-task, providing a fresh clock. |
| §4.4 Q1: `format_code` vs `display` string? | Start with `format_code` only. A display string is nice for humans but does not give the model a code to re-apply. Add later if needed. |
| §4.4 Q2: Sidebar selection enrichment? | **Yes.** `get_calc_context_for_chat` should use `include_format_info=True`. |
| §4.4 Q3: Omit `format_code` when General? | **Yes.** Omit for General / empty to limit payload noise. |
| §5.5 Q1: Column inheritance with mixed formats? | Do not inherit if the column mixes incompatible categories. Fall back to `detectNumberFormat`. |
| §5.5 Q2: Remove `number_format` from `set_style`? | **Yes.** Remove from `parameters` dict entirely (not just the description — `inputSchema.properties` is model-visible). Internal `CellManipulator` methods stay. Scripting API calls `CellManipulator` directly for `number_format` instead of routing through the tool registry. |
| §5.5 Q3: Formula vs constant guidance? | One sentence on `write_formula_range` description is sufficient. |

---

## 10. Disagreements and refinements (second review)

Only points that change or tighten §9. Everything else in this document stands.

### 10.1 Mixed-column inheritance (§5.5 Q1 / §9.6)

**Disagree with “do not inherit if the column mixes incompatible categories.”** That implies a full-column mix scan and a binary fail-closed rule that is harder to define and test than it is worth.

**Prefer:** when M1 would apply because the destination is non-temporal, copy the `NumberFormat` key from the **nearest cell above in the same column** that already has a **category-compatible** temporal format. If none is found (within a scan cap), fall back to `detectNumberFormat` / formatindex 43. Do not sample the whole column for “mixed.”

“Non-empty” must mean “has a compatible temporal NumberFormat,” not merely non-blank text — skip headers and wrong categories while scanning up. Cap the upward scan (e.g. used-range start or a fixed max row distance) so large sheets stay bounded.

### 10.2 `format_code` vs re-apply after P3

Original §5.3 framed P2 as visibility so a careful model can re-apply formats via `set_style`. After **P3** (remove `number_format` from the tool schema), that path is gone.

**Document explicitly:** `format_code` on read is **observability** (and a check that inheritance matched the column), **not** a re-apply affordance. Intentional agent-driven number-format changes stay out of tools until a dedicated tool is justified by demand. Do not ship `format_code` with wording that implies models should call `set_style(number_format=…)`.

### 10.3 Page Assist `instructions` is not an open design gate

§9.1 already resolved that Page Assist never forwards `initialize.instructions`. Do not treat “verify Page Assist” as a blocker before implementing piggyback (C). Keep the clock on `instructions` for hosts that honor it; implement C for hosts that do not.

### 10.4 P1 must still honor S17 (`@`)

Nearest-above inheritance must not leave a coerced temporal serial under a Text (`@`) format. If the destination is `@` (or otherwise non-temporal), apply the **inherited key** when a template exists, else detect — same product intent as S17 (raw serial must not stay the visible presentation).

### 10.5 Docs / shipping debt when P3 lands

- Update [calc-date-time-handling.md](calc-date-time-handling.md): M1 “apply” becomes “inherit template key if found, else detect”; S26’s reference to `set_style(number_format=…)` must change when the property leaves the tool.
- Scripting: `writeragent_api.set_style(..., number_format=...)` must keep working via `CellManipulator`, not tool-schema validation — required for the P3 cut, not optional cleanup.

### 10.6 PR split preference

Do not couple Bug 3 (`format_code` + sidebar enrich) and Bug 4 (P1 + P3) in one mega-change if avoidable: inheritance alone fixes Mechanism W; `format_code` alone does not fix new-row display without P1/P3.

---

## 11. Post-ship review of easy fixes (commit `c4e5f5d8`)

Review of the “easy fixes for calc days bugs” commit against this design package (Bug 1–3 + P3; P1 still open). Related unit tests (schema, list/guidance clock, inspector enrich) passed at review time.

### 11.1 Bug — `number_format` still reaches chat/MCP execute (P3 incomplete)

P3 removed `number_format` from the **LLM/MCP schema**, but `scripting_only_parameters` is honored for **every** caller in both kwargs strip and `ToolBase.validate` ([`tool.py`](../plugin/framework/tool.py) registry `execute` + `validate`).

Verified at review:

| `ctx.caller` | `number_format="0.00"` reaches `SetCellStyle.execute`? |
| :--- | :--- |
| `chat` / `chatbot` | **yes** |
| `mcp` | **yes** |
| `script` | yes (intended) |

Consequences:

- **Strict MCP hosts** that validate against `inputSchema` (property absent) will reject the extra arg before tools/call — good for those hosts.
- **Sidebar / any path that forwards model kwargs** can still apply `number_format` if the model invents the parameter from training memory. That reopens **Mechanism S** (the same foot-gun P3 was meant to close).
- The new unit test only covers `caller="script"`; it does **not** assert that chat/MCP drop `number_format`.

**Fix direction:** allow `scripting_only_parameters` only when `ctx.caller == "script"` (or a small allowlist). Add regression tests: chat and MCP strip `number_format`; script preserves it.

Until that lands, treat P3 as **schema-only**, not fully closed on the chat path.

### 11.2 Polish — clock field not described on the tools

`list_open_documents` and `get_guidance()` (no topic) return `current_local_datetime`, but tool **descriptions** still only document documents / topics. Models often ignore unknown result keys. Not a functional defect; a one-line description note would make Bug 1 piggyback more reliable.

### 11.3 Polish — MCP `formula_or_values` items are flat-only

MCP widen sets:

```text
type: ["string", "array"]
items: { type: ["string", "number"] }
```

A native **nested** 2D array (multi-row grid) may fail host validation. Flat arrays and stringified JSON still work via existing execute coerce / write path. Fine for the reported one-row case; worth knowing if multi-row native arrays appear in the field.

Also: widen is applied **after** `_normalize_schema_for_strict_providers`. If anything re-normalizes the MCP schema afterward, `_collapse_union_type` would collapse `["string","array"]` → `"array"` and `["string","number"]` → `"string"`. Current order is correct; keep widen last.

> **Skipped (2026-08-08):** Not actionable — the widen handles the reported bug (single-row arrays). Nested 2D native arrays are hypothetical; the execute coerce + write path already handle stringified JSON for multi-row. No code change needed unless a real field failure appears.

### 11.4 Polish — private MCP import from doc tools

`ListOpenDocuments` / `GetGuidance` import `_format_mcp_clock_context` from [`mcp_protocol.py`](../plugin/mcp/mcp_protocol.py). No circular import today, but document tools now depend on the MCP module. Prefer a small shared helper (e.g. under `framework/`) if layering matters.

> **Skipped (2026-08-08):** No circular import exists today. Creating a new `framework/clock.py` module for a 3-line helper adds a file for minimal gain. Revisit only if a circular import actually appears.

### 11.5 Polish — stale type annotation

In [`inspector.py`](../plugin/calc/inspector.py) `read_range`, `format_rows` is still annotated as `dict[int, list[tuple[int, int, str]]]`, but spans are now 4-tuples `(start, end, category, format_code)`. Runtime is fine; typing is wrong.

### 11.6 Polish — proxy generator hardcodes `str = ""`

[`generate_tool_proxies.py`](../scripts/generate_tool_proxies.py) always emits `extra: str = ""` for scripting-only params. Correct for `number_format`; brittle if a non-string scripting-only param is added later. Empty string is coerced to `None` in `SetCellStyle.execute` so it does not wipe formats.

> **Skipped (2026-08-08):** There is exactly one `scripting_only_parameter` (`number_format`) and it is a string. Generalizing the type inference for a hypothetical future non-string param is speculative — fix it when a second param is actually added.

### 11.7 What looked correct

| Area | Notes |
| :--- | :--- |
| Bug 2 MCP widen | After normalize; OpenAI stays `"string"`; fill-all via single string preserved |
| Bug 3 `format_code` | Only on temporal enrich; omitted for non-temporal / General |
| Sidebar selection | `include_format_info=True` → ISO/`PT…` in CSV values |
| Scripting empty `number_format` | `""` → `None`; does not clear NumberFormat |
| Docs | Observability-only `format_code`; S26 updated away from LLM `set_style(number_format=…)` |
| Tests | MCP union wire shape; schema omits `number_format`; clock keys on list/guidance |

### 11.8 Summary table

| Severity | Issue | Action | Status |
| :--- | :--- | :--- | :--- |
| **Bug** | `scripting_only_parameters` accepted for chat/MCP, not only script | Gate on `ctx.caller`; add tests | **Fixed** |
| Polish | Describe `current_local_datetime` on the two tools | Description one-liners | **Fixed** |
| Polish | Nested-array MCP items; keep widen after normalize | Document / only if field needs 2D native arrays | Skipped (not actionable) |
| Polish | Private MCP import from doc tools | Optional shared clock helper | Skipped (no circular import) |
| Polish | Stale `format_rows` annotation | Fix 3-tuple → 4-tuple | **Fixed** |
| Polish | Proxy `str = ""` default | Generalize when a second param is added | Skipped (speculative) |

**P1** (column / nearest-above inheritance) is now shipped as the product fix for Mechanism W on new empty rows.

---

## 12. References

- Discussion: https://github.com/KeithCu/writeragent/discussions/374  
- Shipped lifecycle: [calc-date-time-handling.md](calc-date-time-handling.md)  
- MCP surfaces: [mcp-protocol.md](mcp-protocol.md) (`initialize.instructions` MAY be ignored by hosts)  
- Easy-fix commit reviewed in §11: `c4e5f5d8`  
- Code: [`mcp_protocol.py`](../plugin/mcp/mcp_protocol.py) (`_format_mcp_clock_context`, `build_initialize_instructions`), [`cells.py`](../plugin/calc/cells.py) (`ReadCellRange`, `WriteCellRange`, `SetCellStyle`), [`inspector.py`](../plugin/calc/inspector.py), [`datetime_wire.py`](../plugin/calc/datetime_wire.py), [`tool.py`](../plugin/framework/tool.py) (`to_mcp_schema`, `_collapse_union_type`, `scripting_only_parameters`)  
- MCP spec note: `InitializeResult.instructions` is a client-optional hint (`MAY` add to system prompt), not a guaranteed model-visible field.
