# Calc `=PROMPT()`

**WriterAgent only.** LibrePy must not register this add-in ([librepy-split](../scripting/librepy-split.md)).

`=PROMPT()` is a Calc cell function that sends a short chat completion and writes the assistant text into the calling cell. It is **not** the sidebar: no tools, no document snapshot, no streaming, no spill.

| Need | Start |
| --- | --- |
| This doc | Shipped behavior + proposed range args |
| Empty / reasoning-model blank cells | [llm-hacks §10](../chat/llm-hacks.md#10-calc-prompt-empty-cell-reasoning-vs-content) |
| Recalc / yellow context | [plugin/calc/AGENTS.md](../../plugin/calc/AGENTS.md), [uno-thread-safety](../framework/uno-thread-safety.md) |
| Code Oracle (`=PROMPT()` → pasteable `=PY()`) | [enabling-numpy](../enabling_numpy_in_libreoffice.md#code-oracle-prompt--py) |
| `=PY()` ranges (`CalcRange`, varargs) | [py-data-shapes](py-data-shapes.md) |

Code: [`plugin/calc/prompt_addin.py`](../../plugin/calc/prompt_addin.py), [`plugin/calc/prompt_function.py`](../../plugin/calc/prompt_function.py), IDL [`extension/idl/XPromptFunction.idl`](../../extension/idl/XPromptFunction.idl).

## Table of contents

1. [Shipped today](#shipped-today)
2. [Proposed: range arguments](#proposed-range-arguments)
3. [Implementation sketch](#implementation-sketch)
4. [Out of scope](#out-of-scope)

---

## Shipped today

### Signature

```text
=PROMPT(message; system_prompt; model; max_tokens)
```

Locale argument separator is `;` in many locales and `,` in US English. Examples below use `;`.

| # | Wizard name | IDL | Required | Meaning |
| --- | --- | --- | --- | --- |
| 0 | `message` | `string` | yes | User message. A single cell (`A1`) is coerced to text by Calc. A multi-cell range is **not** delivered as a 2D array (implicit intersection or `#VALUE!`). |
| 1 | `system_prompt` | `any` | no | `None` → `extend_selection_system_prompt` if set, else `CALC_PROMPT_CELL_SYSTEM_PROMPT`. A non-`None` value is `str(…)`’d; empty string omits the system message. |
| 2 | `model` | `any` | no | `None` → `get_text_model()`. Otherwise `str(model)` overrides the client config. |
| 3 | `max_tokens` | `any` | no | `None` or non-int → `calc_prompt_max_tokens` (default **4096**; values `< 100` upgraded on load). Else `int(max_tokens)`. |

IDL today:

```idl
string prompt( [in] string message, [in] any systemPrompt, [in] any model, [in] any maxTokens );
```

Contrast `=PY()`: last parameter is `sequence<any>`, which is how Calc packs trailing ranges. `=PROMPT()` has no such parameter, so live grids never reach Python.

### Execution

1. Calc calls UNO `PromptFunction.prompt`.
2. `execute_prompt_addin` runs in a yellow / sync-host context (`sync_host_dispatch`).
3. Messages are `[optional system] + {role: user, content: message}`.
4. `LlmClient.request_with_tools` via `run_blocking_in_thread(..., pump_idle=False, stream=False, tools=None)`.
5. The cell gets a **string**. Empty assistant text becomes a diagnostic (`finish_reason`, token usage, model, short reasoning excerpt) — never a silent blank.

`pump_idle=False` is required: pumping VCL during recalc re-enters the formula engine (`#VALUE!`). The wait still polls `pump_main_thread_work_queue`; that helper returns without running items when the caller is not the main thread, while the same poll still drains the marshal queue on the UI thread.

The add-in instance keeps one `LlmClient` across recalcs. XAddIn never names the calling cell; `=PROMPT()` does not locate the origin or spill.

### Config

| Key | Default | Role |
| --- | --- | --- |
| `extend_selection_system_prompt` | `""` | Fallback system prompt when arg 2 is omitted |
| `calc_prompt_max_tokens` | `4096` | Fallback max tokens when arg 4 is omitted or invalid |
| `text_model` | provider default | Fallback model when arg 3 is omitted |

Default cell system prompt (`CALC_PROMPT_CELL_SYSTEM_PROMPT`): answer in plain text suitable for a spreadsheet cell; no HTML or markdown fences unless asked. This is **not** the long `=PY()` sandbox hint.

### Range workaround today

Users who want live cells in the prompt concatenate in the formula:

```text
=PROMPT("Summarize:" & CHAR(10) & TEXTJOIN(CHAR(10); TRUE; A1:B10))
=PROMPT("Write a Python formula using numpy for the 95th percentile of B1:B100")
```

The second example is the Code Oracle pattern: natural language in, pasteable `=PY("…")` out. The model does not see the values in `B1:B100` unless they are concatenated into `message`.

---

## Proposed: range arguments

**Status: design only — not shipped.**

`=PY()` later grew trailing ranges. `=PROMPT()` should be able to take the same kind of live grid and append it to the user message as text, without `TEXTJOIN`.

### Constraints (why not “like Python kwargs”)

LibreOffice Calc has **no** Excel-style named call syntax (`model:="gpt-4"`). Function Wizard names are labels only.

UNO varargs only work as the **last** IDL parameter (`sequence<any>`). A signature like `PROMPT(message; ranges…; system; model; max)` is not legal: everything after `sequence<any>` is swallowed into that sequence.

After Calc evaluates, a quoted system prompt and a **single-cell** ref are the same type (a scalar). The add-in never sees `B2` vs `"be brief"`. Locating the caller and parsing formula tokens would distinguish them; that is too much code for this feature.

### Recommended signature

Keep the four named formal parameters. Add PY’s trailing varargs as a fifth:

```idl
string prompt(
    [in] string message,
    [in] any systemPrompt,
    [in] any model,
    [in] any maxTokens,
    [in] sequence< any > data
);
```

Function Wizard: `message`, `system_prompt`, `model`, `max_tokens`, `data`. `optional_from` stays 1; argument count becomes 5. Existing 4-arg formulas stay valid (`data` omitted → empty).

### The proper call: skip named optionals with `;;;`

```text
=PROMPT("Summarize";;;; A1:B10)
=PROMPT("Summarize";;;; A1:B10; C1:C5)
=PROMPT("Summarize"; "be brief"; "gpt-4"; 500; A1:B10)
```

The Function Wizard does the same thing: leave `system_prompt` / `model` / `max_tokens` blank and pick the range in `data`.

### Convenience: a multi-cell grid in a named slot

Users will type `=PROMPT("Summarize"; A1:B10)`. That lands in `system_prompt` (`any`), not `data`. Calc already delivers a multi-cell range to `any` as nested tuples, e.g. `(("Sales", 10.0), ("Cost", 4.0))`.

**Rule:** if a named optional slot (`system_prompt` / `model` / `max_tokens`) is a **multi-cell grid** (non-string sequence of sequences, more than one cell), move it to data and leave that option at its default. 1×1 values stay as that option so `=PROMPT("hello"; Settings!A1)` keeps working.

| Formula | What Python sees in arg 2 | Result |
| --- | --- | --- |
| `=PROMPT("hello"; "be brief")` | `"be brief"` | system prompt |
| `=PROMPT("hello"; Settings!A1)` | one scalar | system prompt |
| `=PROMPT("Summarize"; A1:B10)` | 2D tuple, >1 cell | **data** (convenience) |
| `=PROMPT("Translate"; B2)` | one scalar | system prompt — **cannot** tell this from `"be brief"` |
| `=PROMPT("Summarize";;;; A1:B10)` | `None` + range in `data` | **data** (explicit) |

Single-cell context stays `=PROMPT("Translate: " & B2)` (or a 2+ cell range). Do not steal 1×1 values from the named slots.

### Flattening

PROMPT does **not** inject `CalcRange` / pickle / the venv. Reuse [`split_python_addin_data_args`](../../plugin/calc/calc_addin_data.py) and [`calc_addin_data_to_python`](../../plugin/calc/calc_addin_data.py) to get rectangular grids, then:

- Tab-separated rows, newline between rows.
- Blank line between multiple ranges; `[Range N]` header when there is more than one.
- Append that text to the user `message`.
- Cap cell count with existing `configured_python_max_data_cells` (no new config key). Overflow → error string in the cell.

Return type stays `string` (no spill, no matrix index).

---

## Implementation sketch

When this is built (not done yet):

- IDL + RDB: [`XPromptFunction.idl`](../../extension/idl/XPromptFunction.idl), [`scripts/rebuild_xprompt_rdb.sh`](../../scripts/rebuild_xprompt_rdb.sh), commit [`XPromptFunction.rdb`](../../extension/XPromptFunction.rdb)
- Wizard: [`prompt_addin.py`](../../plugin/calc/prompt_addin.py) `CalcFunctionSpec` and [`CalcAddIns.xcu`](../../extension/registry/org/openoffice/Office/CalcAddIns.xcu)
- Logic: [`prompt_function.py`](../../plugin/calc/prompt_function.py) `execute_prompt_addin(..., data=None)` — pull grids, flatten, append; existing system/model/max resolution unchanged
- Tests: [`tests/calc/test_prompt_function.py`](../../tests/calc/test_prompt_function.py) (compat 4-arg, range-in-system-slot, explicit 5th arg, two ranges, 1×1 not stolen, size cap); metadata count in [`tests/calc/test_prompt_function_uno.py`](../../tests/calc/test_prompt_function_uno.py)

---

## Out of scope

- Excel `name:=value` formula syntax (Calc cannot)
- Treating a single cell as data without `&` / `TEXTJOIN`
- Spill / matrix return
- Tools, document context, or the `=PY()` sandbox hint
- Registering `=PROMPT()` in LibrePy
