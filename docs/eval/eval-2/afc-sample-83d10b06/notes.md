# Notes — what changed vs gold

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical to gold `prompt.txt`):

1. `The attached spreadsheet titled ‘Population’` → `This spreadsheet titled ‘Population’` so the trial assumes the Population workbook is already open in Calc.
2. Step 4 only: instead of creating a **separate** spreadsheet file titled ‘Sample’ with two tabs, produce sheets **in this open workbook**:
   - sheet titled ‘Sample’
   - sheet titled ‘Sample Size Calculation’
3. Step 2 parenthetical: gold says “columns H and I” (I is not on Population). Writer prompt uses fixture axes plus the in-workbook oracle: Q2 is in H, Q3 is in G; variance = (G−H)/H into J; flags in K.
4. **Smoother-path cut (2026-09-07):** step 3 exact keys were rewritten so every searched string is cell text in the fixture (entities, KRIs, Trade Finance). The data sheet tab was renamed `Sheet1` → `Population`. See [`SMOOTHER_CHANGES.md`](SMOOTHER_CHANGES.md) to restore GDPVal-harder criteria.

## In-workbook axes (fixture + oracle, not gold letters)

Population headers are A–H only: **G = Q3 2024 KRI**, **H = Q2 2024 KRI**. QoQ variance is (Q3−Q2)/Q2 = (G−H)/H. Eval-2 writes that into **J** and sample flags into **K** (already the writer prompt / harness convention).

Gold `Sample v2` is a **different** deliverable (variance in **I**, flags in **J**). `rubric_pretty.txt` is not a letter-shift of that layout (S-total wording even points at **K** while other lines treat **J** as variance or as the flag). Do not rewrite gold rubric letters for eval-2.

## Not changed

- Zero-both-quarters; Cayman Islands / Pakistan / UAE
- Coverage across all Divisions and sub-Divisions
- Sample-size parameters (90% confidence, 10% tolerable error)
- Step 1 wording (“second tab titled ‘Sample Size Calculation’”)
- Gold rubric, `task.json`, `meta.txt`, `prompt.gdpval.txt`, Sample gold workbook
- `docs/eval/gdpval/` (gold Population fixture there is still tab `Sheet1`)

The gold rubric still describes a separate Excel file named `Sample`. This variant is for iterating WriterAgent/Calc in-workbook behavior; do not treat gold rubric items about a separate deliverable filename as automatically rewritten.

Harness pass/fail for this variant is [`rubric.eval2.md`](rubric.eval2.md) (fixture + in-workbook oracle), not a letter-shift of `rubric_pretty.txt`.

## Catalog stamps

First multi-model catalog cell: `openai/gpt-oss-120b` as `:nitro`, stamp
`20260912-0142-gpt-oss-120b` (Scrolly headed). Product bar **NOT_HAPPY**,
oracle **FAIL** (`R from Sample Size Calculation is missing or < 1`).
Sample+SSC nonempty but wrong shape (`sample_data_rows=1516`); S=585,
R=None, husks 0/15159. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0142-gpt-oss-120b/`.
Scoreboard cell: [`../eval2_benchmark_results.json`](../eval2_benchmark_results.json).

Second catalog cell: `openai/gpt-5.6-luna`, stamp `20260912-0150-gpt-5.6-luna`
(Scrolly headed). Product bar **NOT_HAPPY**, oracle **FAIL** (same R
hole). Sample+SSC present but incomplete (`sample_data_rows=81`; S=68;
husks 0/810) — better shape than the 120b dump. OpenRouter usage sum
~$0.0419 stored; model_configs estimate ~$0.1252 is an alternate only.
Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0150-gpt-5.6-luna/`.

Third catalog cell: `openai/gpt-oss-20b` as `:nitro` (resolved to bare
20b; no batch 404), stamp `20260912-0202-gpt-oss-20b-nitro`. Product
bar **NOT_HAPPY**, oracle **FAIL** (same R hole). Sample/SSC present
but incomplete (`sample_data_rows=80`; S=0; husks 0/800) — weaker
than Luna’s S=68. OpenRouter usage sum ~$0.00122 stored; model_configs
estimate ~$0.00049 is an alternate only. Nitro routing OK. Run dir is
box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0202-gpt-oss-20b-nitro/`.

Fourth catalog cell: `google/gemini-3.5-flash-lite`, stamp
`20260912-0216-gemini-3.5-flash-lite`. Attempt1 aborted (nitro
contamination); attempt2 used a clean history. Product bar
**NOT_HAPPY**, oracle **FAIL** (same R hole). Sample/SSC present
(`sample_data_rows=81`; S=0; husks 0/648). Shot showed SSC label
truncated “Required Sar”=73; oracle parsed R=None — do not store 73.
OpenRouter usage sum ~$0.00629 stored; model_configs estimate ~$0.00821
is an alternate only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0216-gemini-3.5-flash-lite/`.

Fifth catalog cell: `google/gemma-4-31b-it`, stamp
`20260912-0224-gemma-4-31b-it`. Product bar **NOT_HAPPY**, oracle
**FAIL** (same R hole). Sample+SSC present (`sample_data_rows=81`;
S=0; husks 81/810). SSC R is Err:508/#NAME?; oracle parsed R=None.
Husk-heavy Sample flags, still under the 50% husk-dominate fail.
OpenRouter usage sum ~$0.10841 stored; model_configs estimate ~$0.01404
is an alternate only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0224-gemma-4-31b-it/`.

Sixth catalog cell: `google/gemma-4-26b-a4b-it`, stamp
`20260912-0240-gemma-4-26b-a4b-it`. First catalog model besides the
Gemini 3.8 gate to produce a parseable R (**R=65**). Product bar
**NOT_HAPPY**, oracle **FAIL** (`S=0 < R=65`). Sample undersized
(`sample_data_rows=10`; husks 0/70). Attempt1 contaminated; attempt2
wipe. OpenRouter usage sum ~$0.04316 stored; model_configs estimate
~$0.05011 is an alternate only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0240-gemma-4-26b-a4b-it/`.

Seventh catalog cell: `nvidia/nemotron-3.5-lightning`, stamp
`20260912-0310-nemotron-3.5-lightning`. Product bar **NOT_HAPPY**,
oracle **FAIL** (`failure_count=2`: missing Sample and Sample Size
Calculation). Population only; Err:507 in J/K; `sample_data_rows=0`;
S=0; R=None; husks 0/0. Save dialog flicker mid-flight. Tokens stored
are the second-send OpenRouter usage (in=760171 out=3189); all-from-first
totals were higher (not stored). OpenRouter second-send usage sum
~$0.04282 stored; model_configs estimate ~$0.06145 is an alternate
only. Wall ~540–600s. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0310-nemotron-3.5-lightning/`.

Eighth catalog cell: `inception/mercury-2.5-preview`, stamp
`20260912-0330-mercury-2.5-preview`. First non-gate catalog **HAPPY** /
oracle **PASS** (`failure_count=0`). Sample is a near-full Population
copy (`sample_data_rows=1518`) but S=494 ≥ R=68. Husks 2/16680.
Attempt1 contaminated; attempt2 wipe. OpenRouter usage sum ~$0.00468
stored; model_configs estimate ~$0.03144 is an alternate only. First
HAPPY cell with recorded USD (C²/$ lights). Run dir is box-local /
untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0330-mercury-2.5-preview/`.

Ninth catalog cell: `x-ai/grok-4.6`, stamp `20260912-0342-grok-4.6`.
Product bar **NOT_HAPPY**, oracle **FAIL** (`failure_count=2`: Sample
has no data rows; R missing). SSC present; Sample empty
(`sample_data_rows=0`); S=0; R=None; husks 0/0. OpenRouter usage sum
~$0.02375 stored; model_configs estimate ~$0.05831 is an alternate
only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0342-grok-4.6/`.

Tenth catalog cell: `meta/muse-glimmer-30b`, stamp
`20260912-0353-muse-glimmer-30b`. Product bar **NOT_HAPPY**, oracle
**FAIL** (same R hole; `failure_count=1`). Sample+SSC present
(`sample_data_rows=81`; S=0; husks 0/729). SSC B9 visually 65; oracle
parsed R=None — do not store 65. Hit max 50 tool rounds. Recorded
wall ~155s (observer ~16 min). tip_at_run `e0bb6321`. PreContract
lines=2 (PY deal); UNO/assert_main_thread=0. OpenRouter usage sum
~$0.15404 stored; model_configs estimate ~$0.66230 is an alternate
only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0353-muse-glimmer-30b/`.

Eleventh catalog cell: `meta/muse-spark-1.3-contributor`, stamp
`20260912-0409-muse-spark-1.3-contributor`. Product bar **NOT_HAPPY**,
oracle **FAIL** (`failure_count=2`: Sample has no data rows; R
missing). SSC present; Sample empty (`sample_data_rows=0`); S=0;
R=None; husks 0/0. Same empty-Sample shape as Grok. tip_at_run
`6c6017b7`. UNO/assert/PreContract=0. OpenRouter usage sum ~$0.00157
stored; model_configs estimate ~$0.00247 is an alternate only. Run
dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0409-muse-spark-1.3-contributor/`.

Twelfth catalog cell: `poolside/laguna-s-2.1`, stamp
`20260912-0419-laguna-s-2.1`. Product bar **NOT_HAPPY**, oracle
**FAIL** (`failure_count=1`: missing sheet 'Sample'). SSC present
with parseable R=66; Sample sheet missing (`sample_data_rows=0`);
S=0; husks 0/0. `finish_reason=length` — response truncated mid
tool loop. tip_at_run `6c6017b7`. UNO/assert/PreContract=0.
OpenRouter usage sum ~$0.01480 stored; model_configs estimate
~$0.02142 is an alternate only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0419-laguna-s-2.1/`.

Thirteenth catalog cell: `poolside/laguna-xs-2.1`, stamp
`20260912-0429-laguna-xs-2.1`. Product bar **NOT_HAPPY**, oracle
**FAIL** (`failure_count=2`: missing sheet 'Sample Size
Calculation'; Sample has no data rows). SSC tab named
`Sample_Size_Calculation`, not "Sample Size Calculation"; Sample
empty (`sample_data_rows=0`); S=0; R=None; husks 0/0. Raw
`tool_call` text in the reply. Recorded wall ~85s (observer
~3m12s). tip_at_run `6c6017b7`. UNO/assert/PreContract=0.
OpenRouter usage sum ~$0.04341 stored; model_configs estimate
~$0.07804 is an alternate only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0429-laguna-xs-2.1/`.

Fourteenth catalog cell: `qwen/qwen3.8-27b`, stamp
`20260912-0443-qwen3.8-27b`. Product bar **NOT_HAPPY**, oracle
**FAIL** (`failure_count=2`: missing sheet 'Sample'; missing sheet
'Sample Size Calculation'). No Sample/SSC sheets
(`sample_data_rows=0`); S=0; R=None; husks 0/0. Stayed on
Population with `=PY` attempts. ~50 tool rounds, all
`finish_reason=tool_calls`. Tokens are n=51 OpenRouter generations.
PreContract=63; UNO/assert=0. Recorded wall ~720s (12 min LLM).
tip_at_run `6c6017b7`. OpenRouter usage sum ~$1.42823 stored;
model_configs estimate ~$1.31711 is an alternate only. Run dir is
box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0443-qwen3.8-27b/`.

Fifteenth catalog cell: `qwen/qwen3.8-flash`, stamp
`20260912-0502-qwen3.8-flash`. Product bar **NOT_HAPPY**, oracle
**FAIL** (`failure_count=1`: R from Sample Size Calculation is
missing or < 1). Sample+SSC present (`sample_data_rows=68`); S=68;
R=None; husks 0/680. Same R hole as Luna; slightly smaller Sample.
SSC Err:508/509/#VALUE!; Scratch Err:513. Tokens are n=51
OpenRouter generations. UNO/assert/PreContract=0. Recorded wall
~540s (9 min LLM; observer ~12 min). tip_at_run `6c6017b7`.
OpenRouter usage sum ~$0.11382 stored; model_configs estimate
~$0.35238 is an alternate only. Run dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0502-qwen3.8-flash/`.

Sixteenth catalog cell: `z-ai/glm-5.3-flash`, stamp
`20260912-0515-glm-5.3-flash`. Product bar **NOT_HAPPY**, oracle
**FAIL** (`failure_count=2`: missing sheet 'Sample'; missing sheet
'Sample Size Calculation'). Population only
(`sample_data_rows=0`); S=0; R=None; husks 0/0. Hung
`tool_loop` at round 0 — `finish_reason=length`; no tools; truncated
reasoning dump. Tokens are n=1 (`reasoning_tokens=15160` noted, not
stored). UNO/assert/PreContract=0. Recorded wall ~155s. tip_at_run
`6c6017b7`. OpenRouter usage sum ~$0.00900 stored; model_configs
estimate ~$0.00450 is an alternate only. Run dir is box-local /
untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0515-glm-5.3-flash/`.

Seventeenth catalog cell: `upstage/solar-pro4`, stamp
`20260912-0522-solar-pro4`. Product bar **BLOCKED** (infra, not a
model **NOT_HAPPY**). Oracle unscored — Send never ran, so missing
Sample+SSC is not an oracle FAIL. `SendButton` crashed before any
LLM call (`sqlite3.OperationalError: no such table: message_store`;
`writeragent_history.db` wiped empty/broken between trials). Send
failed twice. Tokens 0; cost $0. Headed wait ~1080s. tip_at_run
`6c6017b7`. Scrolly recreating schema before the next trial. Run
dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0522-solar-pro4/`.

Eighteenth catalog cell: `ibm-granite/granite-4.2-8b`, stamp
`20260912-0546-granite-4.2-8b`. Product bar **NOT_HAPPY**, oracle
**FAIL** (`failure_count=2`: Sample has no data rows; R from Sample
Size Calculation is missing or < 1). SSC present; Sample blank
(`sample_data_rows=0`); S=0; R=None; husks 0/0. Hung `tool_loop` at
round 46 on `read_cell_range`; all `finish_reason=tool_calls`.
Tokens are n=48. UNO/assert/PreContract=0. Recorded wall ~840s
(14 min LLM). tip_at_run `71640e30`. OpenRouter usage sum ~$0.14353
stored; model_configs estimate ~$0.24564 is an alternate only. Run
dir is box-local / untracked:
`docs/eval/eval-2/afc-sample-83d10b06/runs/20260912-0546-granite-4.2-8b/`.
