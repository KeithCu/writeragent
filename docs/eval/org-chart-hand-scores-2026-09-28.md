# Org-chart gallery hand scores (2026-09-28)

Chief visual grades of `/workspace/eval1-org-chart/passes/*.png` for `org_chart_gen`.
Catalog oracle PASS is **overridden** for board hard-pass; only visual HARD PASS keeps `org hard=1`.

Net: **5 / 26** models keep org_chart hard PASS for the board. Soft borderline also flipped to 0.


## Scoring follow-up (code oracle needs work)

The current `org_chart_gen` **code scoring** (`oracles.oracle_org_chart_gen` / `org_chart_eval`) is **too loose**: it produced many **false PASSes** (origin blobs, linear stacks, merged bars, overlays, truncated labels) that only failed under Chief visual review of gallery PNGs. This hand-score board overrides those oracle results for ranking.

**Do not treat catalog oracle PASS as a trustworthy visual gate** until the scorer is tightened. A later improvement may use a **vision / scoring model** on the exported Draw PNG (or equivalent) instead of (or in addition to) structural box/connector floors — **not implemented here**; this file only records the gap.

## Table

| Model | Visual | Board org hard | Reason |
| ----- | ------ | -------------- | ------ |
| `meta/muse-spark-1.3-contributor` | HARD PASS | 1 | clean 3-level tree, all 10, clear connectors |
| `qwen/qwen3.8-flash` | HARD PASS | 1 | clean titled tree, all 10 |
| `qwen/qwen3.8-27b` | HARD PASS | 1 | clean color-coded tree, all 10 |
| `z-ai/glm-5.3-flash` | HARD PASS | 1 | clean tree, all 10 (pairs touch OK) |
| `openai/gpt-6-luna` | HARD PASS | 1 | chart looks fine; a bit big / edge crop (Keith override → HARD PASS) |
| `x-ai/grok-4.6` | SOFT / borderline FAIL | 0 | structure OK but bottom-row box overlaps |
| `meta/muse-glimmer-30b` | SOFT / borderline FAIL | 0 | bottom row merged into one bar; truncated titles |
| `poolside/laguna-s-2.1` | SOFT / borderline FAIL | 0 | bottom merged bar; messy connectors; truncated Engineer |
| `google/gemma-4-31b-it` | HARD FAIL | 0 | linear vertical stack, not a tree |
| `openai/gpt-oss-20b` | HARD FAIL | 0 | giant background Ava (CEO) overlay; incomplete look |
| `google/gemini-3.5-flash-lite` | HARD FAIL | 0 | severe overlap pile |
| `google/gemma-4-26b-a4b-it` | HARD FAIL | 0 | text list + stray lines, not a chart |
| `inception/mercury-2.5-preview` | HARD FAIL | 0 | all shapes at origin blob |
| `poolside/laguna-xs-2.1` | HARD FAIL | 0 | origin blob (wrong names Alice/Bob…) |
| `upstage/solar-pro4` | HARD FAIL | 0 | truncated names; vertical letter titles; overlaps |
| `bytedance-seed/seed-2.0-mini` | HARD FAIL | 0 | linear stack like Gemma 31B |
| `minimax/minimax-m3` | HARD FAIL | 0 | no boxes; connectors miss text |
| `nvidia/nemotron-3-super-120b-a12b` | HARD FAIL | 0 | overlaps; literal \n in labels |
| `cohere/command-a-plus` | HARD FAIL | 0 | merged bottom bar + duplicate vertical list |
| `openai/gpt-oss-120b` | catalog FAIL (stay 0) | 0 | catalog FAIL (connectors/edges short) |
| `nvidia/nemotron-3.5-lightning` | catalog FAIL (stay 0) | 0 | catalog FAIL |
| `mistralai/mistral-small-2603` | catalog FAIL (stay 0) | 0 | catalog FAIL (rate limit / empty) |
| `ibm-granite/granite-4.2-8b` | catalog FAIL (stay 0) | 0 | catalog FAIL (max_tool_rounds / missing edge) |
| `deepseek/deepseek-v4.1-flash` | catalog FAIL (stay 0) | 0 | catalog FAIL (no boxes/connectors) |
| `nvidia/nemotron-3-ultra-550b-a55b` | catalog FAIL (stay 0) | 0 | catalog FAIL (no boxes/connectors) |
| `prism-ml/ternary-bonsai-2-27b` | catalog FAIL (stay 0) | 0 | catalog FAIL (hang / rate limit) |

## Visual HARD PASS (keep)

- `meta/muse-spark-1.3-contributor` — clean 3-level tree, all 10, clear connectors
- `qwen/qwen3.8-flash` — clean titled tree, all 10
- `qwen/qwen3.8-27b` — clean color-coded tree, all 10
- `z-ai/glm-5.3-flash` — clean tree, all 10 (pairs touch OK)
- `openai/gpt-6-luna` — chart looks fine; a bit big / edge crop (Keith override → HARD PASS)

## Flipped oracle PASS → board FAIL

### Soft / borderline

- `x-ai/grok-4.6` — structure OK but bottom-row box overlaps
- `meta/muse-glimmer-30b` — bottom row merged into one bar; truncated titles
- `poolside/laguna-s-2.1` — bottom merged bar; messy connectors; truncated Engineer

### Hard fail

- `google/gemma-4-31b-it` — linear vertical stack, not a tree
- `openai/gpt-oss-20b` — giant background Ava (CEO) overlay; incomplete look
- `google/gemini-3.5-flash-lite` — severe overlap pile
- `google/gemma-4-26b-a4b-it` — text list + stray lines, not a chart
- `inception/mercury-2.5-preview` — all shapes at origin blob
- `poolside/laguna-xs-2.1` — origin blob (wrong names Alice/Bob…)
- `upstage/solar-pro4` — truncated names; vertical letter titles; overlaps
- `bytedance-seed/seed-2.0-mini` — linear stack like Gemma 31B
- `minimax/minimax-m3` — no boxes; connectors miss text
- `nvidia/nemotron-3-super-120b-a12b` — overlaps; literal \n in labels
- `cohere/command-a-plus` — merged bottom bar + duplicate vertical list

## Already catalog FAIL (unchanged)

- `openai/gpt-oss-120b` — catalog FAIL (connectors/edges short)
- `nvidia/nemotron-3.5-lightning` — catalog FAIL
- `mistralai/mistral-small-2603` — catalog FAIL (rate limit / empty)
- `ibm-granite/granite-4.2-8b` — catalog FAIL (max_tool_rounds / missing edge)
- `deepseek/deepseek-v4.1-flash` — catalog FAIL (no boxes/connectors)
- `nvidia/nemotron-3-ultra-550b-a55b` — catalog FAIL (no boxes/connectors)
- `prism-ml/ternary-bonsai-2-27b` — catalog FAIL (hang / rate limit)
