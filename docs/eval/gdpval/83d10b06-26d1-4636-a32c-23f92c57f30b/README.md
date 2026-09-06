# GDPval gold task port: `83d10b06-26d1-4636-a32c-23f92c57f30b`

Faithful one-task package from [openai/gdpval](https://huggingface.co/datasets/openai/gdpval)
(OpenAI GDPval gold subset). No harness redesign — materials only.

| Field | Value |
|-------|--------|
| Task id | `83d10b06-26d1-4636-a32c-23f92c57f30b` |
| Sector | Professional, Scientific, and Technical Services |
| Occupation | Accountants and Auditors |
| Deliverable | Excel `Sample` workbook (sample size + variance + sample flags) |

## Contents

- `prompt.txt` — exact gold prompt
- `task.json` — HF row metadata (refs, deliverables, rubric)
- `rubric.json` / `rubric_pretty.txt` — gold rubric
- `reference_files/.../Population v2.xlsx` — input population
- `deliverable_files/.../Sample v2.xlsx` — expert gold deliverable
- `meta.txt` — short provenance

## License / attribution

Source: Hugging Face `openai/gdpval`. See the [dataset card](https://huggingface.co/datasets/openai/gdpval)
and OpenAI GDPval terms before redistributing or scoring. This tree is a
straight copy of one gold item for WriterAgent eval experimentation.

## Next (not in this PR)

Wiring into the eval runner, short-prompt/cwd layouts, and pairwise grading
will be discussed separately.
