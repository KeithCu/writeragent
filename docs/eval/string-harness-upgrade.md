# String-harness upgrade (no LO)

**Status:** 17-task `--backend string` ranking pack. Core schemas from
headless ``ToolRegistry.get_schemas``. Specialized work is a bounded
inner ``LlmClient`` loop (``delegate_to_specialized_*`` with
``active_domain`` ``shapes`` / ``ranges``), not SmolAgents. Worlds,
process/`=PY` score, quality judge after the hard gate, and
required ``--models`` are shipped. Eval does not set sampling
temperature. Multi-turn and ``--backend lo`` ranking remain out of
scope.

Plan for making `--backend string` a real WriterAgent eval: same core
tool catalog as chat, document worlds that can tell the truth, and a
second score for how the agent worked. **`--backend lo` is out of
scope.** Multi-turn chat (refresh `[DOCUMENT CONTENT]` after each user
turn) is **saved for later**; worlds should export state so that hook
is cheap when we want it.

Related: [eval-dev-plan.md](eval-dev-plan.md) (older hybrid/LO roadmap;
Phase F `=PY` rows live here), [ideas.md](ideas.md) (task ideas),
[scripts/prompt_optimization/README.md](../../scripts/prompt_optimization/README.md).

## Why

The string harness already runs a real `LlmClient` tool loop and grades
the exported document. That is enough to rank models **if** the
environment matches chat and the mocks are not theater.

Today it does not:

- (Shipped) `build_eval_tool_schemas()` uses live `ToolRegistry.get_schemas`
  for that doc type. Unimplemented names still return `unsupported_in_eval`.
- `StringDocState` is one HTML string. `DrawDocState` has no
  `shape_connect`. `CalcStringState` writes values, not formula text
  or dest. So `style_consistency`, `comment_management`, and flowchart
  connections cannot be honest.
- Scoring is result-only. Two models can both leave a correct table;
  one dumped the sheet into chat. IpD sees cost, not *why*.

Do **not** start with MIPROv2, a new judge, or more oracle-needle
tweaks. Those amplify a metric that is still too easy and too fake.

## Non-goals

- `--backend lo`, UNO fidelity, screenshots, vision/multimodal.
- Multi-turn user messages (later). Design worlds so
  `state.export_for_prompt()` exists; do not build the turn loop.
- Generated item banks / anti-memorization.
- A Python LibreOffice or a formula engine that computes `=B2*0.08`.
- Requiring a golden tool name for Writer table/cleanup tasks
  (`write_table_cells` traces are not coming back).
- Bootstrapping `plugin.main.get_tools()` inside string eval (needs
  services / UNO). Schemas come from tool **classes**.

## Target picture

```
LlmClient (core schemas)
   │  delegate_to_specialized_* (domain=shapes|ranges)
   ▼
inner LlmClient loop (domain schemas, same world)
   │  specialized_workflow_finished → parent
   ▼
WriterWorld / DrawWorld / CalcWorld
   │  export HTML / draw tree / calc snapshot   → result oracles
   │  trace (name, args, nested, domain)        → process oracles
   ▼
hard pass (document + process) ; quality judge after the gate
```

The 17 tasks export the same HTML / JSON the oracles already read.
`=PY` rows use dest/process oracles (any empty cell outside A1:H500;
all writes inspected; formula must mention the range). `apply_style`
maps onto HTML tags / `data-lo-style`. `sort_range` is one 0-based
column, stable, non-numeric last.

---

## 1. Document worlds (do this first)

Replace the three thin states in `string_eval_tools.py` with worlds
that can represent the features we score. Keep `dispatch_string_tool`
as the single entry. Split files only if `string_eval_tools.py` gets
unwieldy (`eval_worlds.py` is enough; do not invent a package tree).

### WriterWorld

Blocks, not a raw string:

| Field | Purpose |
|-------|---------|
| `type` | `paragraph`, `heading`, `table`, `list` |
| `text` / `rows` | Visible content |
| `style` | String style name (`Default`, `Quotations`, `Heading 1`) |
| `level` | Heading level |
| `comments` | List of `{anchor, text}` on a block |

`apply_document_content` still accepts HTML (production does). Parse
with `html.parser` into blocks; do not bring in a browser. `find_text`
/ production `search_in_document` search block text and return
offsets into the **exported** HTML so scripted tests stay stable.

Export HTML for oracles: `<h1>`, `<p class="Quotations">`,
`<span class="comment">…</span>` or a visible `[comment text]` next to
the anchor. Then `style_consistency` and `comment_management` score
real fields, not a word stuffed into the blob.

`target=selection` stays a documented limitation (append / marked
range). Do not fake a cursor.

### DrawWorld

Keep `shape_upsert`. Add:

- `shape_connect` (production name in `plugin/draw/shapes.py`)
- `connections: list[{from_index, to_index}]`
- z-order = list order
- optional `shape_group` as a parent node

`get_draw_tree` must emit the same semantic keys as
`plugin/draw/tree.py`: `type`, `text`, `geometry`,
`connected_start` / `connected_end` (by name/text). The flowchart
oracle then requires **edges**, not just the words Start / Process /
End.

### CalcWorld

Still a grid, plus:

| Field | Purpose |
|-------|---------|
| `sheets` | `Sheet1` plus optional extra sheet |
| cell `{value, formula}` | `write_formula_range` stores formula text when `values` starts with `=` |
| last write dest | A1 (or `Sheet2.J1`) of each write |

No formula evaluator. Tax column can still write `0.8` as a value.
`=PY("result = …"; A1:H500)` is stored as formula text at dest; process
oracles parse dest vs DataRange.

A single formula string into a **1-D** range fill-down/across-adjusts
relative A1 refs via [`formula_fill.expand_single_formula`](../../plugin/calc/formula_fill.py)
(same as production `write_formula_range`). A JSON array of formula
strings stays exact per-cell (pin). 2-D + one formula, or an import
failure, falls back to the honest pin. Snapshot `formulas` stores the
**per-cell** text, not the original pin source on every address.

`sort_range` is production-shaped: one 0-based column, stable,
non-numeric last (tie-break = two calls). `get_sheet_summary` returns
the full grid. Snapshot JSON grows `formulas` / `writes` without
breaking `grid` / `rows` that `oracle_data_sorting` and
`oracle_tax_column` already use.

### Migration of the 15

- Export compatibility: current `oracles.py` fixtures and
  `gold_standards.json` must still pass.
- `style_consistency` / `comment_management`: rewrite golds and
  scripted student to set `style` / `comments` on blocks (via apply
  HTML or a small world API). If a task cannot be honest on the world,
  drop it from the default pack rather than keep theater.
- `flowchart_gen`: scripted student calls `shape_connect`; gold tree
  includes connections; oracle requires them.
- `find_text`: implement as an alias of `search_in_document` on
  WriterWorld so we stop advertising an eval-only name.

### Tests (worlds)

`tests/scripts/test_string_eval_tools.py` and `test_eval_oracles.py`:

- HTML apply → blocks → export round-trip for a heading + table.
- Style field survives export (`Quotations` is a class/style, not
  only a word).
- Comment is on the `uncertain` block.
- Draw: two shapes + `shape_connect` → tree has
  `connected_start` / `connected_end`.
- Calc: write `=PY("result = 1"; A1:H500)` at `J1`; snapshot records
  dest `J1` and formula text; grid oracles unchanged for tax/sort.
- Scripted pack (`test_scripted_eval_pack.py`) stays green.

---

## 2. Real tool catalog (after worlds)

### Schemas

`build_eval_tool_schemas()` uses a headless `ToolRegistry` (Writer/Calc/Draw
+ Chatbot modules, no `plugin.main.bootstrap`) and
`get_schemas("openai", doc_type=kind)` — the same filter as sidebar
(`tool_loop.py`). Specialized tools stay off the list; Draw
`shape_upsert` / Calc `sort_range` are advertised only via
`delegate_to_specialized_*`. There is no committed JSON snapshot.

Eval-only (not sidebar): `filter_eval_tool_schemas` / named presets
(`full`, `calc_minimal`, `calc_core`, `writer_minimal`) and
`apply_schema_density` (`full` \| `skinny`) hang off `prepare_eval_tool_schemas`
and the `--tools` / `--schema-density` flags on `run_eval.py` /
`run_eval_multi.py` / `run_optimize.py`. Outer allowlists do not filter
specialized inner schemas. Recipe: [prompt_optimization README](../../scripts/prompt_optimization/README.md).

Do not include `specialized` / `specialized_control`. Do not
bootstrap `MainJob`.

`eval_prompts.py` calls `get_chat_system_prompt_for_document` (stub
Writer/Calc/Draw model, `ctx=None`) and appends `EVAL_HARNESS_NOTE`
via `additional_instructions`. Pin tests compare equality with that
builder.

### Dispatch

One table, not a growing `if name ==`:

| Kind | Implemented on the world (minimum) |
|------|--------------------------------------|
| Writer | `get_document_content`, `apply_document_content`, `search_in_document` (and `find_text` alias) |
| Draw | `shape_upsert`, `shape_connect`, `get_draw_tree`, `shape_summary` |
| Calc | `get_sheet_summary`, `read_cell_range`, `write_formula_range`, `sort_range` |

Everything else in the advertised catalog returns:

```json
{"status": "error", "code": "unsupported_in_eval",
 "message": "shape_delete is not implemented in the string harness"}
```

The model must recover or finish without it. That *is* the test.

Grow the implemented set only when a new task needs it (e.g.
`add_comment` once WriterWorld comments exist — then
`comment_management` can call the real name).

### Tests (catalog)

- Schema list for writer/draw/calc includes production names
  (`search_in_document`, `write_formula_range`, `shape_connect`,
  `get_draw_tree`) and is larger than the current 3–5.
- Calling `add_comment` / `list_sheets` / `shape_delete` returns
  `unsupported_in_eval` until implemented.
- Scripted student still only uses implemented names.
- Prompt pin tests updated; no “only three tools” wording.

---

## 3. Agent process score (after catalog + CalcWorld dest)

Result oracles stay the hard document gate. Add a **trace** and
**process oracles**. Do not fold process into `correctness` in a way
that breaks old IpD CSV comparability.

### Trace

`llm_chat_eval.py` already loops tool calls. Record a list of:

`{name, arguments, result_status, result_chars, error_code}`

No full result bodies in the saved JSON (those can be huge). Keep
counts.

### Process oracles

New helpers next to `oracles.py` (same file or `process_oracles.py`
if it stays small). Checks are **constraints and waste**, not golden
names:

| Check | When |
|-------|------|
| Dest outside DataRange | `=PY` tasks |
| Formula text is `=PY(...)` | `=PY` tasks |
| No `domain=python` | Calc |
| No `read_cell_range` of the full DataRange | `=PY` / “no bulk read” |
| Tool-result chars over a cap | optional penalty |
| `unsupported_in_eval` then recovery or abort | informational; fail only if the final doc is wrong **and** the model spun on unsupported tools |
| Failed tool then retry with different args | informational |

Writer table/cleanup tasks get **no** “must call apply” process
oracle. The document oracle is enough.

### Scoring and reports

`ExampleEval` gains `process_failures` and `agent_score`.

- `correctness` — unchanged meaning: substring + **result** oracles
  (document). Used for historical IpD.
- `agent_score` — `0` if result oracles fail; else `1` minus process
  penalties (start simple: any required process fail → `0`).
- `run_eval.py` / `run_eval_multi.py` print both. `--out` JSON/CSV
  grow columns; do not rename `avg_correctness`.

### New tasks (the point of this axis)

Two Phase F rows from [eval-dev-plan.md](eval-dev-plan.md) (the unique-beside and
in-place clones were dropped so the pack stays even):

| id | Ask | Pass | Fail |
|----|-----|------|------|
| `py_refuse_overlap` | put the formula in **H1**, data A1:H500 | dest J1/I1 and says H1 is inside the range | writes H1 |
| `py_no_bulk_read` | unique-rows via `=PY` | no `read_cell_range` of A1:H500 | dumping the block into chat |

Use a **small** fixture (e.g. A1:C8), not 500 rows. The names stay
`A1:H500` in the user question if we want production wording; the
world only needs a handful of rows and a declared DataRange.

Scripted student: one `write_formula_range` to `J1` with
`=PY("result = data.to_pandas().drop_duplicates()"; A1:H500)`.

### Tests (process)

- Trace records names and dest args.
- Good `=PY` at J1 passes process; write at H1 fails dest check.
- `read_cell_range` of the whole DataRange fails `py_no_bulk_read`.
- Tax/sort result oracles unchanged when process is empty.
- Scripted pack includes the four new ids.

---

## Sequence

| Step | What | Done when |
|------|------|-----------|
| A | Writer/Draw/Calc worlds + export-compatible oracles | **Shipped.** Scripted 15 green; new world unit tests |
| B | Production core schemas + `unsupported_in_eval` + prompt pins | **Shipped.** Schema tests; scripted still green |
| C | Trace + process oracles + two `=PY` rows + report columns | **Shipped.** Process unit tests; scripted 17 green |

C is merged. Live `--backend string` ranking uses
`openai/gpt-oss-120b:nitro` (student and judge). Relative `--out` is
cwd-relative (repo root from `make run_eval`), not
`scripts/prompt_optimization/`. Scoring folds NBSP/NNBSP so ``100 K`` /
``45 ms`` match ``100K`` / ``45ms``. Flowchart oracles require Start/End,
login/credentials, shape types, and edges — not the words Process/Decision.

Do **not** compare scores to the Apr 2026 8-task CSV
(`eval_results.csv`); treat that as history. Rank by hard pass /
`agent_score` / quality. Keep the hand-written `gold_standards.json`
(do not bulk `--generate-golds`).

Typecheck and targeted pytest after each step (`test_string_eval_tools`,
`test_eval_oracles`, `test_scripted_eval_pack`, `test_eval_prompts`).
No `make test-uno`. No LO scripted pack work.

## Later (not this plan)

- Multi-turn user messages using `export_for_prompt()`.
- Implementing more core tools on the worlds (`add_comment`,
  `list_sheets`, …) when a task needs them.
- MIPROv2 on `agent_score` once the metric is stable.
- Periodic `--backend lo` smoke only; not part of the ranking loop.
