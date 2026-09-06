# Notes — what changed vs gold

Intentional delta vs [`prompt.gdpval.txt`](prompt.gdpval.txt) (byte-identical to gold `prompt.txt`):

1. `The attached spreadsheet titled ‘Population’` → `This spreadsheet titled ‘Population’` so the trial assumes the Population workbook is already open in Calc.
2. Step 4 only: instead of creating a **separate** spreadsheet file titled ‘Sample’ with two tabs, produce sheets **in this open workbook**:
   - sheet titled ‘Sample’
   - sheet titled ‘Sample Size Calculation’

Unified diff of the two prompt files should show only those lines.

## Not changed

- Column letters **J** (variance) and **K** (sample flag “1”)
- Entity list (CB Cash Italy, CB Correspondent Banking Greece, IB Debt Markets Luxembourg, CB Trade Finance Brazil, PB EMEA UAE)
- Metrics A1 / C1, zero-both-quarters, Trade Finance / Correspondent Banking, Cayman Islands / Pakistan / UAE
- Coverage across all Divisions and sub-Divisions
- Sample-size parameters (90% confidence, 10% tolerable error)
- Formula / workings wording in steps 1–3 (including “second tab titled ‘Sample Size Calculation’” in step 1)
- Gold rubric, `task.json`, `meta.txt`, Population fixture, Sample gold workbook

The gold rubric still describes a separate Excel file named `Sample`. This variant is for iterating WriterAgent/Calc in-workbook behavior; do not treat gold rubric items about a separate deliverable filename as automatically rewritten.
