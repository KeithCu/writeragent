# Org-chart gallery hand scores (2026-11-11)

Keith's visual grades of `/workspace/eval1-org-chart/new-20261111/*.png` for `org_chart_gen` on the five models added to the board on 2026-11-11.
Catalog oracle PASS is **overridden** for board hard-pass; only visual HARD PASS keeps `org hard=1`.

These PNGs were **not** saved during the live eval. They were rebuilt by replaying the recorded shape tool calls from the spliced-board transcripts into a fresh Draw document (no paid model calls), then exporting with headless `soffice --convert-to png:draw_png_Export`. Transcripts:

- `anthropic/claude-sonnet-5.5`, `anthropic/claude-opus-5.5`, `anthropic/claude-haiku-5.5`: `scripts/prompt_optimization/transcripts/20261111-004555/<model>/org_chart_gen.json`
- `meta/muse-ember`: `scripts/prompt_optimization/transcripts/20261111-005401/meta_muse-ember/org_chart_gen.json`
- `liquid/lfm-3-1.7b`: `scripts/prompt_optimization/transcripts/20261111-005655/liquid_lfm-3-1.7b/org_chart_gen.json`

Net: **2 / 5** of these models keep org_chart hard PASS for the board (Opus 5.5, Sonnet 5.5). Haiku 5.5 and Muse Ember are visual PARTIAL (board org hard=0; 0.9 on correctness / document / agent). LFM 3 1.7B is a visual HARD FAIL (0). Together with the 2026-09-28 gallery, visual HARD PASS is **7** models.

The per-task `hard_pass` flag is binary, so PARTIAL does not set `hard_pass=true`. The 0.9 credit is stored on `correctness`, `document_score`, and `agent_score`.

## Scoring follow-up (code oracle needs work)

The current `org_chart_gen` **code scoring** (`oracles.oracle_org_chart_gen` / `org_chart_eval`) is **too loose**: it produced a **false PASS** here for LFM 3 1.7B (tiny unreadable boxes that still met the 10-box / 9-connector count floor). This hand-score board overrides that oracle result for ranking, as the 2026-09-28 gallery did for blobs, stacks, merged bars, overlays, and truncated labels. See [org-chart-hand-scores-2026-09-28.md](org-chart-hand-scores-2026-09-28.md).

**Do not treat catalog oracle PASS as a trustworthy visual gate** until the scorer is tightened. A later improvement may use a **vision / scoring model** on the exported Draw PNG (or equivalent) instead of (or in addition to) structural box/connector floors — **not implemented here**; this file only records the grades.

## Table

| Model | Visual | Board org hard | Reason |
| ----- | ------ | -------------- | ------ |
| `anthropic/claude-opus-5.5` | HARD PASS | 1 | Cleanest of the five: titled 3-level tree with all 10 people, and every label fits. |
| `anthropic/claude-sonnet-5.5` | HARD PASS | 1 | Titled, color-coded tree with all 10; Engineer wraps inside its box but stays readable. |
| `anthropic/claude-haiku-5.5` | PARTIAL | 0 (0.9 correctness) | Clean tree with all 10 and no title; the last r of Engineer spills below two boxes. |
| `meta/muse-ember` | PARTIAL | 0 (0.9 correctness) | Good tree, but CTO/CFO/COO are clipped at the bottom of their boxes and Engineer spills out. |
| `liquid/lfm-3-1.7b` | HARD FAIL | 0 | Tiny 0.12 by 0.08 cm boxes in a vertical stack, unreadable; the code check passed it only on counts. |

## Visual HARD PASS (keep org hard=1)

- `anthropic/claude-opus-5.5` — cleanest of the five: titled 3-level tree with all 10 people, and every label fits
- `anthropic/claude-sonnet-5.5` — titled, color-coded tree with all 10; Engineer wraps inside its box but stays readable

## Visual PARTIAL (org hard=0; 0.9 on correctness / document / agent)

- `anthropic/claude-haiku-5.5` — clean tree with all 10 and no title; the last r of Engineer spills below two boxes
- `meta/muse-ember` — good tree, but CTO/CFO/COO are clipped at the bottom of their boxes and Engineer spills out

## Visual HARD FAIL (0)

- `liquid/lfm-3-1.7b` — tiny 0.12 by 0.08 cm boxes in a vertical stack, unreadable; the code check passed it only on counts
