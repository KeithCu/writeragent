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
