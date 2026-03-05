---
name: DSPy prompt optimization plan
overview: DSPy can optimize your Writer system prompt by treating it as the "instruction" in a DSPy program and using MIPROv2 to search for instruction variants that maximize a custom metric (correctness plus token efficiency). This plan explains how it works and how to implement it with your chosen evaluation tasks (table formatting, resume reformatting).
todos: []
isProject: false
---

# DSPy for optimal DEFAULT_CHAT_SYSTEM_PROMPT

## Can DSPy help? Yes.

DSPy is built for exactly this: **optimizing prompts (instructions) to maximize a metric** without hand-tuning. Its optimizers (especially **MIPROv2**) propose and search over natural-language instructions using your program, a small train/val set, and a scoring function. So your goal—find a better system prompt—maps directly onto DSPy’s instruction optimization.

- **What gets optimized**: The “instruction” of a DSPy predictor. That instruction is what MIPROv2 varies; you seed it with your current `DEFAULT_CHAT_SYSTEM_PROMPT` and optional formatting rules from [core/constants.py](core/constants.py).
- **How**: You define a **program** (e.g. a tool-using agent that mirrors Writer chat), a **metric** (correctness + token count), and a **dataset** of (document, question) pairs. MIPROv2 runs many instruction candidates and keeps the one that scores best on your metric.
- **0-shot**: You can optimize **only the instruction** (no few-shot examples) by using MIPROv2 with `max_bootstrapped_demos=0, max_labeled_demos=0`.

So: **DSPy can help you find a more optimal prompt** by systematically exploring instruction space and measuring outcomes (including token use) on fixed tasks.

---

## Why your evaluation tasks fit well

You already identified the right kind of tasks:


| Task                                                        | Why it works                                                                                                                                                 |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **“Turn this mess of text with markup into a clean table”** | Fixed input length and clear success criterion (valid table structure, content preserved). Token count is comparable across runs.                            |
| **“Reformat this plain text resume as professional”**       | Same input every time (or a small set of fixed resumes). Output length and structure are stable; you can score formatting + content preservation and tokens. |
| **Avoid “create a resume”**                                 | Open-ended; length and content vary too much to compare prompts fairly.                                                                                      |


For optimization you want **reproducible, bounded tasks** so that (1) correctness is checkable (e.g. table structure, formatting rules, or an LLM judge), and (2) **token count is a meaningful signal**. DSPy supports **custom metrics** that can combine correctness with token usage (see below).

---

## Tasks from your eval suite (15 for DSPy; narrow to 5–10)

Your eval frame lives in **[core/eval_runner.py](core/eval_runner.py)** (`EvalRunner`, `run_benchmark_suite`) and **[EVALUATION_PLAN_DETAILED.md](EVALUATION_PLAN_DETAILED.md)**. The suite has 20 Writer, 20 Calc, 5 Draw, and 5 Multimodal tasks. For **Writer prompt optimization** we want tasks with **fixed or reproducible document content** and **clear success criteria** so token count and correctness are comparable across prompt variants.

Below are **15 tasks** drawn from your suite (plus your two). Use these as the pool for the DSPy dataset; you can narrow to **5–10** for the first optimization run.

### Your two (keep both)

1. **Table from mess** – “Turn this mess of text with markup into a clean table.” (Fixed messy input; check table structure + content.)
2. **Reformat resume** – “Reformat this plain text resume as professional.” (Fixed resume text; check headings, content preserved, token count.)

### From run_benchmark_suite – Writer (best for fixed input + token comparison)

1. **Writer: Table Engineering** – “Convert this comma-separated list into a 2-column table with headers.” (Provide fixed CSV-like text in document context.)
2. **Writer: Bulk Cleanup** – “Remove all double spaces and ensure every sentence is followed by exactly one space.” (Fixed text in; fixed text out; easy to verify.)
3. **Writer: Logical Rewriting** – “Rewrite the third paragraph to be 'professional and concise' while preserving all technical terms.” (Fixed paragraph; bounded output; check terms preserved.)
4. **Writer: Format Preservation** – “Replace 'John Doe' with 'Jane Smith' in the header (Bold, 14pt).” (Fixed doc with that header; verify replacement + formatting.)
5. **Writer: Style Application** – “Make 'Introduction' a Heading 1.” (Fixed doc containing “Introduction”.)
6. **Writer: Bullet Consistency** – “Ensure all bullet points in this list end with a period.” (Fixed list; verify periods.)
7. **Writer: Bibliography Fix** – “Locate all brackets [1], [2] and ensure they are superscripted.” (Fixed text with [1], [2]; verify formatting.)
8. **Writer: Markdown Import** – “Replace the second paragraph with a Markdown table.” (Fixed two-paragraph doc + fixed Markdown table string.)
9. **Writer: Smart Summarization** – “Summarize the 'Finding' section into 5 bullet points and insert it into the 'Executive Summary'.” (Fixed “Finding” and “Executive Summary” text; verify 5 bullets and content.)
10. **Writer: Font Audit** – “Change all text in 'Comic Sans' to 'Inter'.” (Short fixed doc with Comic Sans.)
11. **Writer: Comment Management** – “Add a comment 'Review this' to the word 'Uncertain'.” (Fixed sentence containing “Uncertain”.)
12. **Writer: Header/Footer** – “Add page numbers in the footer and the document title in the header.” (Fixed title; verify footer/header content.)
13. **Writer: Style Consistency** – “Find all text in 'Default' style and change it to 'Quotations'.” (Short fixed doc with Default-style text.)

### Suggested narrow set (5–10 to start)

- **Must-have**: (1) Table from mess, (2) Reformat resume, (3) Table Engineering, (4) Bulk Cleanup, (5) Logical Rewriting — all fixed input, clear output, token-comparable.
- **Add 2–3** from: Format Preservation, Style Application, Bullet Consistency, Bibliography Fix, Smart Summarization — each needs one fixed fixture (short doc or paragraph).
- **Skip for first run**: Refactoring Sections, Track Changes Audit, Conflict Resolution, TOC Generation, Section Break — they depend on richer doc structure or multi-step state and are harder to fixture for prompt-only comparison.

**Note**: [core/eval_runner_tests.py](core/eval_runner_tests.py) only has a unit test for `EvalRunner` init; the task definitions themselves are in `run_benchmark_suite()` in [core/eval_runner.py](core/eval_runner.py). For DSPy you’ll build a separate dataset (e.g. JSON or `dataset.py`) that supplies the **fixed document_content** and **user_question** for each of the 15 (or your chosen 5–10) so every run sees the same inputs.

---

## How to use DSPy for this

### 1. Metric: correctness + token efficiency

DSPy metrics are functions `(example, pred, trace=None) -> float` (higher = better). You can:

- **Correctness**: For “mess → table”, check that the model’s final output is a valid table (e.g. parse rows/columns, or use a small LM/judge to verify). For “reformat resume”, check headings, consistency, and that content was preserved (exact or semantic).
- **Token usage**: Run with `dspy.settings.context(..., track_usage=True)` and no cache. After the program runs, call `pred.get_lm_usage()` (or aggregate usage from the trace if multi-step) to get `prompt_tokens` and `completion_tokens`. Penalize total tokens in the metric, e.g.:
`score = correctness_score - lambda * (total_tokens / 1000)`
so that fewer tokens improve the score. You can tune `lambda` to balance quality vs cost.

### 2. Program: mirror Writer chat with tools

Your real flow is: **system prompt + document context + user message → model → tool calls (get_document_content, apply_document_content, …) → execution → possibly more turns**. To optimize the **system prompt**, the DSPy program should use that prompt as the **instruction** of the predictor that drives tool use.

Two practical options:

- **Option A – ReAct with mock tools (recommended)**  
  - Implement **mock** `get_document_content` and `apply_document_content` that operate on an in-memory document (e.g. a string or simple structure).  
  - Use `dspy.ReAct` (or a custom `dspy.Module` that wraps a predictor + tools) with your current system prompt as the predictor’s initial instruction.  
  - Inputs: e.g. `document_content` (the “mess” or the resume text) and `user_question`.  
  - Run the full ReAct loop; the model calls your mock tools; you capture the final “document” state and use it in the metric.  
  - This matches real behavior (multi-step, tool-calling) and gives you realistic token counts.
- **Option B – Single-step “proposed edit”**  
  - One call: “Given this document and user question, output the exact content you would write (e.g. the table or the reformatted resume).”  
  - No real tool execution; you only score that single output and its token usage.  
  - Simpler to implement but less faithful to the real chat/tool loop; token counts may not reflect production.

Recommendation: **Option A** so that the optimized prompt is tested in the same tool-calling setting as LocalWriter.

**Mock tools: full set available, subset in use.** You can implement mocks for the **full** Writer tool set (e.g. all of `WRITER_TOOLS` from [core/document_tools.py](core/document_tools.py)) so the harness is realistic, but (a) your **dataset tasks** only require a subset (e.g. get_document_content, apply_document_content, find_text, maybe list_styles). That already tests “can the model handle this subset?” (b) You can also **parameterize which tools are passed to the model** per run: e.g. pass only 3 tools, or 5, or the full set. That lets you analyze “how many is too many” (see below) without reimplementing mocks.

### 3. Dataset

- **5–20 examples** are enough to start (DSPy docs often cite 5–10).  
- Each example: `document_content` (string), `user_question` (string). Optionally `expected` or a rubric for automated correctness (e.g. expected table row count, or key phrases that must appear in the reformatted resume).  
- Split into **train** and **val** (or use the same small set for both if data is limited); MIPROv2 will use the val set to select the best instruction.

Example rows:

- Task “mess → table”: one or more fixed “messy” texts with markup; question = “Turn this into a clean table.”
- Task “reformat resume”: one or more plain-text resumes; question = “Reformat this as a professional resume.”

You can add more tasks later (e.g. “translate this paragraph”) as long as they are fixed-input and scoreable.

### 4. MIPROv2: 0-shot instruction-only

To optimize **only the system prompt** (no few-shot examples):

- Use **MIPROv2** with `max_bootstrapped_demos=0` and `max_labeled_demos=0` so it only searches over **instructions**.  
- Pass your current `DEFAULT_CHAT_SYSTEM_PROMPT` (and any format rules) as the **initial instruction** of the predictor in your program.  
- Set `auto="light"` (or `"medium"`) to control cost/time; `auto` sets `num_trials` and candidate counts.

Result: MIPROv2 will propose alternative instructions, evaluate each on your metric (correctness − token penalty), and return the **compiled program** with the best-performing instruction. That instruction is your candidate new system prompt.

### 5. LM and endpoint

- Use the **same OpenAI-compatible endpoint** as LocalWriter (e.g. Ollama, OpenRouter, or your current API). Configure DSPy with `dspy.LM(..., api_base=..., api_key=...)` so optimization runs against the same model/endpoint you care about.  
- Run optimization in a **separate Python script** (e.g. under `scripts/` or `prompt_opt/`), not inside the LibreOffice extension. No need to wire DSPy into [core/api.py](core/api.py); the script only needs to call your endpoint.

### 6. Extracting and using the optimized prompt

- After `teleprompter.compile(...)`, save the compiled program: `optimized_program.save("optimized_writer_prompt.json")`.  
- The saved JSON contains the winning **instruction** text. You can either:  
  - **Manually** copy that instruction into `DEFAULT_CHAT_SYSTEM_PROMPT` in [core/constants.py](core/constants.py), or  
  - Add a small helper that reads the JSON, extracts the instruction, and optionally overwrites a constant or a config file (if you later move the prompt to config).
- Keep `FORMAT_RULES` (and any other non-optimized parts) out of the optimized instruction or append them after optimization so formatting rules stay consistent.

---

## Implementation plan (concrete steps)

1. **Create a small optimization project** (e.g. `scripts/prompt_optimization/` or repo root `prompt_opt/`):
  - `dataset.py` (or JSON): 5–20 examples for “mess → table” and “reformat resume” (and optionally 1–2 more fixed tasks).
  - `tools_mock.py`: Mock `get_document_content` / `apply_document_content` on an in-memory document (and optionally other Writer tools you care about).
  - `program.py`: DSPy program (ReAct or custom Module) that takes `document_content` + `user_question`, uses your current system prompt as the predictor instruction, and runs the tool loop; expose the final document state for the metric.
  - `metric.py`: `def metric(example, pred, trace=None)`: compute correctness (table validity / resume formatting and content), get token usage from `pred.get_lm_usage()` (or trace), return `correctness - lambda * (total_tokens / 1000)`.
  - `run_optimize.py`: Load dataset, configure `dspy.LM` with your endpoint, create MIPROv2 with `max_bootstrapped_demos=0`, `max_labeled_demos=0`, run `compile(program, trainset=..., valset=...)`, save compiled program and optionally print the winning instruction.
  - `requirements.txt`: `dspy-ai` (and any deps for your endpoint).
2. **Correctness sub-metrics** (implement in `metric.py`):
  - For “mess → table”: parse model output (or tool result) into a table; check it has ≥ N rows/columns and key content; or use a tiny LM/judge: “Is this a valid table that contains the same information as the source?”
  - For “reformat resume”: check for section headings, no content loss (e.g. key names/dates present), and optionally an LM judge for “professional formatting.”
3. **Run optimization** (e.g. `python run_optimize.py`). Use `auto="light"` first to limit cost; inspect the winning instruction and metric scores, then try `"medium"` if needed.
4. **Apply the result**: Copy the best instruction from the saved program into `DEFAULT_CHAT_SYSTEM_PROMPT` in [core/constants.py](core/constants.py) (or merge with `FORMAT_RULES` as you do now). Test in LocalWriter with the same evaluation tasks to confirm behavior and token usage.
5. **(Optional) Document**: Add a short `scripts/prompt_optimization/README.md` explaining how to run the optimizer, how the metric works (correctness + token penalty), and how to update constants.py from the output.

---

## Caveats and tips

- **Token usage in multi-step ReAct**: If the program makes multiple LM calls, usage may be per-step. Aggregate total tokens from all steps in the trace (or use DSPy’s `track_usage=True` and any documented API for program-level usage) so the metric reflects full task cost.
- **Cost**: MIPROv2 does many evaluations. Use a cheap model (e.g. `gpt-4o-mini` or your smallest local model) and `auto="light"` to keep cost low; once you’re happy with the setup, you can run a heavier run.
- **Stability**: Run optimization a couple of times and compare winning instructions; if they’re similar, the result is more reliable.
- **Format rules**: Keep `FORMAT_RULES` (and `_FORMAT_HINT` etc.) out of the search space or fixed so that only the “assistant behavior” part of the prompt is optimized; then append format rules when building the final `DEFAULT_CHAT_SYSTEM_PROMPT`.

---

## “How many tools is too many?” and DSPy’s role

You want to know whether **tool-set size** hurts small models (more tools → longer prompt, more choices, more mistakes) and whether to optimize or analyze that.

**What DSPy does *not* do:** There is no built-in optimizer that searches over “number of tools” or “which subset of tools.” That’s a **structural** choice (which tools you pass into the ReAct/module), not an instruction variant MIPROv2 can propose.

**What you can do (with or without DSPy):** Run a **sweep**: same model, same dataset, same metric. Vary the **tool set** the model sees (e.g. minimal 2–3 tools for the task, then 5, then 10, then full WRITER_TOOLS). For each configuration, run evaluation (and optionally run MIPROv2 to get a best prompt for that configuration). Record **pass rate** and **total tokens** (or your full metric). Plot “score vs number of tools” (or vs specific subsets). That tells you “how many is too many” for that model and task set. You run the sweep yourself; it’s a loop over configurations, not something DSPy automates.

**Where DSPy *does* add value for this:**

1. **Same harness and metric** – One eval pipeline (program + metric + dataset). You only change the tool list passed to the program. No separate “tool count” experiment framework needed.
2. **Prompt optimization *per* tool subset** – For each tool-set size (e.g. 3 vs 10 tools), you can run MIPROv2 and get an **optimal prompt for that size**. If the best instruction for “3 tools” differs a lot from “10 tools,” that’s a signal (e.g. with more tools, the optimal prompt might emphasize “prefer get_document_content and apply_document_content first”).
3. **Instruction can emphasize a subset** – Even when the *program* has many tools available, the **instruction** (system prompt) can list only a subset or say “for this task, use only …”. MIPROv2 might find instructions that reduce confusion for small models by narrowing the *described* set, which you can then compare against runs where the model actually only receives that subset.

So: **“How many is too many?”** is best answered by your own **sweep over tool-set sizes** (or subsets), using the **same DSPy eval** (metric + dataset). DSPy doesn’t optimize tool count for you, but it gives you a consistent way to measure and, if you like, to optimize the prompt **for each** tool-set size so you can compare and decide.

---

## Summary

- **DSPy can help** by treating your Writer system prompt as the instruction of a DSPy program and using MIPROv2 to search for a better instruction on fixed, scoreable tasks.
- **Your evaluation tasks** (“mess → table”, “reformat resume”) are well-suited: fixed inputs, clear correctness criteria, and comparable token counts.
- **Metric**: correctness (table/resume quality) minus a token penalty, using `get_lm_usage()` (and trace if multi-step).
- **Implementation**: Separate script, ReAct (or custom module) with mock Writer tools, small dataset, MIPROv2 with 0-shot (instruction-only), then copy the winning instruction into [core/constants.py](core/constants.py).

This gives you a repeatable, data-driven way to improve `DEFAULT_CHAT_SYSTEM_PROMPT` and keep token use under control.