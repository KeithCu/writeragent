# Source

This directory is a **WriterAgent-native** headed experiment, not a
GDPval gold rewrite. There is **no** task id and **no** new tree under
[`docs/eval/gdpval/`](../../gdpval/). Do not invent a fake GDPval id.

| Field | Value |
|-------|--------|
| Gold task id | none — WriterAgent-native Ask |
| Untouched gold tree | none |
| Upstream | n/a — short Ask for python domain → shapes via `run_venv` |

The OpenRouter string harness (`scripts/prompt_optimization/`) cannot
run this Ask: that world has no `run_venv_python_script` and no
`domain=python`. Score a headed LibreOffice Writer document, not a
string-eval tool trace.

`--launch` writes only a blank `American Flag.odt` into a clean trial
dir and opens it in Writer. Prompt, rubric, and notes stay outside that
folder. Shapes are placed on that document’s draw page.
