# LLM Evaluation Suite & Benchmarks

WriterAgent includes an in-LibreOffice **LLM Evaluation Suite** for real-world tasks in Writer, Calc, and Draw. Runs track accuracy and **Intelligence-per-Dollar**: **Value (C²/$)** = average correctness squared ÷ average dollars per run (higher is better), using live OpenRouter pricing where available.

How to run evals from the repo: [scripts/prompt_optimization/README.md](../scripts/prompt_optimization/README.md). Broader plan notes: [eval-dev-plan.md](eval-dev-plan.md).

## Snapshot ranking (Apr 2026)

Apr 2026 snapshot — slugs and prices may be stale. Current default eval models live in [`scripts/prompt_optimization/model_configs.py`](../scripts/prompt_optimization/model_configs.py).

| Rank | Model | Avg correctness | Avg score | Avg tokens | Avg cost ($) | Value (C²/$) |
| ---- | -------------------------------------- | --------------- | --------- | ---------- | ------------ | ------------ |
| 1 | openai/gpt-oss-120b | 0.980 | 0.942 | 3767.1 | 0.00025 | 3827.240 |
| 2 | google/gemini-3-flash-preview | 0.890 | 0.860 | 2957.2 | 0.00035 | 2234.257 |
| 3 | qwen/qwen3.5-9b | 0.730 | 0.691 | 4645.0 | 0.00050 | 1068.806 |
| 4 | nvidia/nemotron-3-nano-30b-a3b | 0.922 | 0.851 | 7195.5 | 0.00082 | 1037.536 |
| 5 | mistralai/devstral-2512 | 0.980 | 0.950 | 3000.8 | 0.00154 | 623.434 |
| 6 | inception/mercury-2 | 0.948 | 0.896 | 5150.9 | 0.00160 | 562.405 |
| 7 | minimax/minimax-m2.7 | 0.990 | 0.943 | 4671.9 | 0.00191 | 512.581 |
| 8 | deepseek/deepseek-v3.2 | 0.985 | 0.909 | 7575.4 | 0.00206 | 470.222 |
| 9 | qwen/qwen3.5-35b-a3b | 0.990 | 0.933 | 5671.1 | 0.00220 | 445.760 |
| 10 | x-ai/grok-4.1-fast | 0.950 | 0.886 | 6431.9 | 0.00204 | 442.733 |
| 11 | qwen/qwen3.5-27b | 0.993 | 0.942 | 5049.9 | 0.00259 | 380.538 |
| 12 | qwen/qwen3.5-122b-a10b | 0.990 | 0.950 | 3958.8 | 0.00308 | 318.312 |
| 13 | nvidia/nemotron-3-super-120b-a12b:free | 0.757 | 0.696 | 6388.4 | 0.00181 | 317.859 |
| 14 | allenai/olmo-3.1-32b-instruct | 0.323 | 0.306 | 1912.4 | 0.00046 | 226.704 |
| 15 | z-ai/glm-5.1 | 0.890 | 0.843 | 4677.8 | 0.00524 | 151.141 |

## Key insights from that snapshot

1. **Verbosity vs cost**: Qwen 3.5-35B-A3B used more tokens than Qwen 3.5-122B-A10B but still scored higher Value (C²/$) because average $/run was lower. Token count alone does not determine dollar efficiency.
2. **C² punishes unreliable cheap runs**: Low correctness (e.g. OLMo ~0.32) collapses value even at low cost; “free” models with middling correctness also underperform on this metric.
3. **Value leader**: `openai/gpt-oss-120b` paired high correctness (~0.98) with very low average cost (~$0.00025). Gemini 3 Flash was a strong second.
4. **Near-ceiling mid-pack**: Qwen 3.5-27B (~0.993 correctness) and Grok 4.1 Fast (~0.95) remain useful accuracy-first picks when cheaper high-Value models dominate the ranking.

## Scoring approach

Structural tasks are scored from the **exported final document** (HTML / Draw tree / Calc grid) via result oracles — not tool-name traces. Creative tasks (resume, logical rewriting, summarization) still use an LLM judge (default `openai/gpt-oss-120b`; the retired `x-ai/grok-4.1-fast` 404s) plus gold references in `gold_standards.json` (hand-written from the rubrics). That separates fast “Flash” models from frontier models with better taste for professional documents.

**Fine-tuning direction:** the same eval signal (correct vs incorrect tool use, minimal vs verbose traces) could train a smaller specialist for this tool distribution—fewer tokens at similar correctness, better Value (C²/$).
