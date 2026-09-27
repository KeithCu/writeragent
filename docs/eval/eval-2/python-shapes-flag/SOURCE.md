# Source

This directory is a **WriterAgent-native** headed experiment, not a
GDPval gold rewrite. There is **no** task id and **no** new tree under
[`docs/eval/gdpval/`](../../gdpval/). Do not invent a fake GDPval id.

| Field | Value |
|-------|--------|
| Gold task id | none — WriterAgent-native Ask |
| Untouched gold tree | none |
| Upstream | n/a — short Ask for python domain → shapes via `run_venv` |

OpenRouter-only `--backend string` cannot run this Ask: that world has
no honest `run_venv_python_script` and no `domain=python`. The mixed
eval-1 pack runs this row on headless LO
(`run_eval.py --backend auto -e python_shapes_flag`) and scores the
harness trace plus an exported Writer `.odt`. Headed LibreOffice plus
`writeragent_debug.log` stays the human path.

`--launch` writes only a blank `American Flag.odt` into a clean trial
dir and opens it in Writer. Prompt, rubric, and notes stay outside that
folder. Shapes are placed on that document’s draw page.
